# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Bench-free unit tests for the SQLite persistence (pacioli.store) — the real implementation of the
guarantees the pure cores delegate to the glue: an atomic CAS marker claim, single-writer append,
and an atomic outcome+marker settle. Plus an end-to-end run of the spine against a real store.

Run: `python3 -m unittest pacioli.tests.test_store` from the broker app root. No frappe required.
"""
import os
import sqlite3
import tempfile
import threading
import unittest

from pacioli.consent import CONSUMED, LIVE, RESERVED, new_marker, reserve
from pacioli.plan import new_plan
from pacioli.spine import governed_submit
from pacioli.store import BrokerStore, SubmitEffects

KEY = b"seal-key-on-box-until-increment-2"
TOKEN = "out-of-band-token"
PLAN_ID = "p1"
FAR = 1e12  # expiry far in the future


def _store():
    # a fixed clock keeps receipt timestamps deterministic
    return BrokerStore(sqlite3.connect(":memory:"), KEY, now_iso=lambda: "2026-07-01T00:00:00Z")


class TestMarkerClaimCAS(unittest.TestCase):
    def test_mint_then_claim_wins(self):
        s = _store()
        s.mint_marker(TOKEN, PLAN_ID, expires_at=FAR)
        _, _, reserved = reserve(new_marker(TOKEN, PLAN_ID, FAR), TOKEN, PLAN_ID, now=1.0)
        self.assertTrue(s.claim_marker(reserved))

    def test_second_claim_loses(self):
        # the concurrency guard: once claimed (reserved), a racing claim on the same marker fails
        s = _store()
        s.mint_marker(TOKEN, PLAN_ID, expires_at=FAR)
        _, _, reserved = reserve(new_marker(TOKEN, PLAN_ID, FAR), TOKEN, PLAN_ID, now=1.0)
        self.assertTrue(s.claim_marker(reserved))
        self.assertFalse(s.claim_marker(reserved))  # already reserved → CAS finds no live row

    def test_claim_unminted_marker_loses(self):
        s = _store()
        _, _, reserved = reserve(new_marker(TOKEN, PLAN_ID, FAR), TOKEN, PLAN_ID, now=1.0)
        self.assertFalse(s.claim_marker(reserved))


class TestReceiptAppend(unittest.TestCase):
    def test_intents_chain(self):
        s = _store()
        r0 = s.record_intent({"doc": "A"})
        r1 = s.record_intent({"doc": "B"})
        self.assertEqual((r0.seq, r1.seq), (0, 1))
        self.assertEqual(r1.prev_hash, r0.hmac)
        self.assertTrue(s.verify()[0])

    def test_head_exposed_for_off_box_pin(self):
        s = _store()
        self.assertIsNone(s.head())
        r0 = s.record_intent({"doc": "A"})
        self.assertEqual(s.head(), r0.hmac)

    def test_tamper_detected(self):
        s = _store()
        s.record_intent({"amount": 100})
        # rewrite a posted amount directly in the DB (an API user with store access)
        s._conn.execute("UPDATE receipts SET body = ? WHERE seq = 0", ('{"amount": 999}',))
        s._conn.commit()
        self.assertFalse(s.verify()[0])

    def test_truncation_detected_with_pinned_head(self):
        s = _store()
        s.record_intent({"n": 0})
        s.record_intent({"n": 1})
        pinned = s.head()
        s._conn.execute("DELETE FROM receipts WHERE seq = 1")  # drop the newest receipt
        s._conn.commit()
        self.assertTrue(s.verify()[0])  # internal chain still consistent...
        self.assertFalse(s.verify(expected_head=pinned)[0])  # ...but the off-box anchor catches it


class TestOutcomeSettle(unittest.TestCase):
    def test_committed_outcome_consumes_marker_and_clears_orphan(self):
        s = _store()
        s.mint_marker(TOKEN, PLAN_ID, expires_at=FAR)
        _, _, reserved = reserve(new_marker(TOKEN, PLAN_ID, FAR), TOKEN, PLAN_ID, now=1.0)
        s.claim_marker(reserved)
        intent = s.record_intent({"doc": "SINV-1"})
        s.record_outcome(intent, "committed", {"docstatus": 1}, final_marker=reserved.__class__(
            reserved.token_hash, reserved.plan_id, reserved.expires_at, CONSUMED))
        self.assertEqual(s.marker_state(TOKEN), CONSUMED)
        self.assertEqual(s.orphans(), [])

    def test_failed_outcome_releases_marker_and_keeps_orphan(self):
        s = _store()
        s.mint_marker(TOKEN, PLAN_ID, expires_at=FAR)
        _, _, reserved = reserve(new_marker(TOKEN, PLAN_ID, FAR), TOKEN, PLAN_ID, now=1.0)
        s.claim_marker(reserved)
        intent = s.record_intent({"doc": "SINV-1"})
        s.record_outcome(intent, "failed", {"error": "boom"}, final_marker=reserved.__class__(
            reserved.token_hash, reserved.plan_id, reserved.expires_at, LIVE))
        self.assertEqual(s.marker_state(TOKEN), LIVE)  # grant spared
        self.assertEqual([r.seq for r in s.orphans()], [0])  # intent still needs reconciliation

    def test_outcome_marker_update_is_state_guarded(self):
        # defense-in-depth: settle only a marker we actually reserved — never clobber a live/other one
        s = _store()
        s.mint_marker(TOKEN, PLAN_ID, expires_at=FAR)  # LIVE, never claimed
        intent = s.record_intent({"doc": "X"})
        unreserved = new_marker(TOKEN, PLAN_ID, FAR).__class__(
            new_marker(TOKEN, PLAN_ID, FAR).token_hash, PLAN_ID, FAR, CONSUMED)
        s.record_outcome(intent, "committed", {"ok": 1}, final_marker=unreserved)
        # the marker was LIVE (not reserved) → must NOT have been flipped to consumed
        self.assertEqual(s.marker_state(TOKEN), LIVE)

    def test_settle_retry_against_a_real_store_writes_exactly_one_receipt(self):
        # Redteam (consent lens): the `_settle` retry was proven only against FakeEffects mocks —
        # the "poisoned first attempt rolls back leaving nothing partial, retry writes exactly one
        # receipt, marker settles once" claim was inferential. Drive spine._settle against a REAL
        # BrokerStore with a genuinely non-JSON-native (NaN) outcome body to close that last step.
        from pacioli.spine import _settle
        s = _store()
        s.mint_marker(TOKEN, PLAN_ID, expires_at=FAR)
        _, _, reserved = reserve(new_marker(TOKEN, PLAN_ID, FAR), TOKEN, PLAN_ID, now=1.0)
        s.claim_marker(reserved)
        intent = s.record_intent({"doc": "SINV-1"})
        before = len(s.receipts())  # plan-independent: intent receipt(s) already on the chain
        effects = SubmitEffects(s, execute=lambda: None)
        consumed = reserved.__class__(reserved.token_hash, reserved.plan_id,
                                      reserved.expires_at, CONSUMED)

        recorded, retry_exc = _settle(effects, intent, "committed",
                                      {"amount": float("nan")},  # real prove.append ValueError
                                      consumed)

        self.assertTrue(recorded)             # the sanitized retry landed
        self.assertIsNotNone(retry_exc)       # the first attempt genuinely raised
        # exactly ONE outcome receipt — the poisoned first attempt rolled back, nothing partial
        outcomes = [r for r in s.receipts() if r.kind == "outcome"]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(s.receipts()[before].body["status"], "unconfirmed")  # degraded, not committed
        self.assertEqual(s.marker_state(TOKEN), CONSUMED)  # settled exactly once
        self.assertTrue(s.verify()[0])        # chain still verifies end-to-end


class TestConcurrentAppend(unittest.TestCase):
    def test_parallel_intents_never_collide_or_fork(self):
        # the append-race fix: many threads appending to one file-backed ledger must all succeed
        # (no unhandled IntegrityError) and leave a contiguous, verifying chain.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            BrokerStore(sqlite3.connect(path), KEY)  # create schema
            threads_n, per = 4, 25
            errors = []

            def worker():
                st = BrokerStore(sqlite3.connect(path), KEY)
                for _ in range(per):
                    try:
                        st.record_intent({"w": 1})
                    except Exception as e:  # noqa: BLE001
                        errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(threads_n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            final = BrokerStore(sqlite3.connect(path), KEY)
            receipts = final.receipts()
            self.assertEqual(len(receipts), threads_n * per)
            self.assertEqual([r.seq for r in receipts], list(range(threads_n * per)))  # contiguous, no fork
            self.assertTrue(final.verify()[0])
        finally:
            os.unlink(path)


class TestSpineEndToEnd(unittest.TestCase):
    def test_governed_submit_against_a_real_store(self):
        s = _store()
        s.mint_marker(TOKEN, PLAN_ID, expires_at=FAR)
        plan = new_plan(PLAN_ID, "acme/Acme Corp", doc_version="v1", posting_date="2026-06-30")
        effects = SubmitEffects(s, execute=lambda: {"docstatus": 1, "name": "SINV-1"})
        res = governed_submit(
            plan=plan, marker=new_marker(TOKEN, PLAN_ID, FAR), token=TOKEN,
            current_doc_version="v1", now_epoch=1.0, now_date="2026-07-01", locks={}, effects=effects,
        )
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(s.marker_state(TOKEN), CONSUMED)
        self.assertTrue(s.verify()[0])
        self.assertEqual(s.orphans(), [])
        # the trail is exactly one intent + one committed outcome
        self.assertEqual([r.kind for r in s.receipts()], ["intent", "outcome"])


if __name__ == "__main__":
    unittest.main()


class TestPlanPersistence(unittest.TestCase):
    """Plans must survive across MCP tool calls (plan in one call, submit in another) and land in
    the durable record the human consented against."""

    def setUp(self):
        self.store = BrokerStore(sqlite3.connect(":memory:"), key=b"k" * 32)

    def _plan(self, plan_id="p1"):
        from pacioli.plan import new_plan
        return new_plan(plan_id, "prod", "v-2026-07-01", "2026-07-01",
                        projected_gl=[{"account": "Debtors", "debit": 100.0}],
                        risk_flags=["future_posting_date"], ts="2026-07-01T00:00:00Z",
                        docname="SI-1")

    def test_record_then_get_roundtrips_every_field(self):
        self.store.record_plan(self._plan())
        p = self.store.get_plan("p1")
        self.assertEqual(p.docname, "SI-1")
        self.assertEqual(p.target, "prod")
        self.assertEqual(p.doc_version, "v-2026-07-01")
        self.assertEqual(p.posting_date, "2026-07-01")
        self.assertEqual(p.projected_gl, [{"account": "Debtors", "debit": 100.0}])
        self.assertEqual(p.risk_flags, ["future_posting_date"])

    def test_unknown_plan_is_none(self):
        self.assertIsNone(self.store.get_plan("nope"))

    def test_duplicate_plan_id_refused(self):
        self.store.record_plan(self._plan())
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_plan(self._plan())


class TestGetMarker(unittest.TestCase):
    def setUp(self):
        self.store = BrokerStore(sqlite3.connect(":memory:"), key=b"k" * 32)

    def test_get_marker_by_raw_token(self):
        self.store.mint_marker("raw-token", "p1", 999.0)
        m = self.store.get_marker("raw-token")
        self.assertEqual(m.plan_id, "p1")
        self.assertEqual(m.state, "live")
        self.assertEqual(m.expires_at, 999.0)

    def test_unknown_or_blank_token_is_none(self):
        self.assertIsNone(self.store.get_marker("nope"))
        self.assertIsNone(self.store.get_marker(""))

    def test_second_live_marker_for_same_plan_refused(self):
        self.store.mint_marker("t1", "p1", 999.0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.mint_marker("t2", "p1", 999.0)


class TestKeylessStore(unittest.TestCase):
    """The mint CLI runs as the human with NO seal key in reach (least exposure) — marker ops work
    keyless; receipt ops refuse."""

    def test_marker_ops_work_without_key(self):
        store = BrokerStore(sqlite3.connect(":memory:"), key=None)
        store.mint_marker("t1", "p1", 999.0)
        self.assertEqual(store.marker_state("t1"), "live")

    def test_receipt_ops_refuse_without_key(self):
        store = BrokerStore(sqlite3.connect(":memory:"), key=None)
        with self.assertRaises(ValueError):
            store.record_intent({"tool": "submit"})


class TestPlanOpPersistence(unittest.TestCase):
    """The op column: round-trip + the schema evolution an installed pre-UNDO store needs."""

    def test_op_round_trips(self):
        import sqlite3
        from pacioli.plan import new_plan
        from pacioli.store import BrokerStore
        store = BrokerStore(sqlite3.connect(":memory:"), key=b"k" * 32)
        store.record_plan(new_plan(plan_id="c1", target="prod", doc_version="v",
                                   posting_date="2026-07-01", docname="SI-9", op="cancel"))
        self.assertEqual(store.get_plan("c1").op, "cancel")

    def test_pre_undo_db_is_migrated_and_history_reads_as_submit(self):
        import sqlite3
        from pacioli.store import BrokerStore
        conn = sqlite3.connect(":memory:")
        # A state db exactly as 0.2.0 created it: plans has NO op column, with a recorded plan.
        conn.executescript("""
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY, target TEXT NOT NULL, docname TEXT NOT NULL,
                doc_version TEXT NOT NULL, posting_date TEXT NOT NULL,
                projected_gl TEXT NOT NULL, risk_flags TEXT NOT NULL, ts TEXT NOT NULL
            );
            INSERT INTO plans VALUES ('old1','prod','SI-1','v1','2026-07-01','[]','[]','2026-07-01');
        """)
        store = BrokerStore(conn, key=b"k" * 32)  # opening migrates
        old = store.get_plan("old1")
        self.assertEqual(old.op, "submit")  # pre-UNDO history WAS all submits — honest backfill


class TestPlanDoctypePersistence(unittest.TestCase):
    """The doctype column: round-trip + the schema evolution a pre-breadth store needs. Mirrors
    TestPlanOpPersistence exactly — same shape, one column later."""

    def test_doctype_round_trips(self):
        import sqlite3
        from pacioli.plan import new_plan
        from pacioli.store import BrokerStore
        store = BrokerStore(sqlite3.connect(":memory:"), key=b"k" * 32)
        store.record_plan(new_plan(plan_id="pi1", target="prod", doc_version="v",
                                   posting_date="2026-07-01", docname="PINV-1",
                                   doctype="Purchase Invoice"))
        self.assertEqual(store.get_plan("pi1").doctype, "Purchase Invoice")

    def test_pre_breadth_db_is_migrated_and_history_reads_as_sales_invoice(self):
        import sqlite3
        from pacioli.store import BrokerStore
        conn = sqlite3.connect(":memory:")
        # A state db exactly as 0.5.0 created it: plans has an op column but NO doctype column.
        conn.executescript("""
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY, target TEXT NOT NULL, docname TEXT NOT NULL,
                doc_version TEXT NOT NULL, posting_date TEXT NOT NULL,
                projected_gl TEXT NOT NULL, risk_flags TEXT NOT NULL, ts TEXT NOT NULL,
                op TEXT NOT NULL DEFAULT 'submit'
            );
            INSERT INTO plans VALUES
                ('old1','prod','SI-1','v1','2026-07-01','[]','[]','2026-07-01','submit');
        """)
        store = BrokerStore(conn, key=b"k" * 32)  # opening migrates
        old = store.get_plan("old1")
        # pre-breadth history WAS all Sales Invoice — honest backfill, same shape as the op column.
        self.assertEqual(old.doctype, "Sales Invoice")

    def test_migration_from_the_very_first_pre_op_pre_doctype_shape(self):
        # Both migrations must chain correctly against a db older than either column.
        import sqlite3
        from pacioli.store import BrokerStore
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY, target TEXT NOT NULL, docname TEXT NOT NULL,
                doc_version TEXT NOT NULL, posting_date TEXT NOT NULL,
                projected_gl TEXT NOT NULL, risk_flags TEXT NOT NULL, ts TEXT NOT NULL
            );
            INSERT INTO plans VALUES ('old0','prod','SI-1','v1','2026-07-01','[]','[]','2026-07-01');
        """)
        store = BrokerStore(conn, key=b"k" * 32)
        old = store.get_plan("old0")
        self.assertEqual(old.op, "submit")
        self.assertEqual(old.doctype, "Sales Invoice")


class TestPlanGraphPersistence(unittest.TestCase):
    """The graph column: the cascade_cancel node list round-trips through the store, mirroring
    TestPlanOpPersistence/TestPlanDoctypePersistence exactly — same shape, one column later."""

    def test_plan_graph_roundtrips(self):
        from pacioli.plan import new_plan
        store = _store()  # existing helper that opens a seeded BrokerStore
        graph = [
            {"doctype": "Payment Entry", "docname": "ACC-PAY-1", "doc_version": "v1",
             "posting_date": "2026-07-03", "company": "X", "coverage": "generic",
             "projected_gl": [["2026-07-03", "Debtors", "", 100.0]]},
            {"doctype": "Sales Invoice", "docname": "ACC-SINV-1", "doc_version": "v2",
             "posting_date": "2026-07-03", "company": "X", "coverage": "modeled",
             "projected_gl": []},
        ]
        p = new_plan(plan_id="p1", target="t", doc_version="v2", posting_date="2026-07-03",
                     docname="ACC-SINV-1", op="cascade_cancel", doctype="Sales Invoice", graph=graph)
        store.record_plan(p)
        got = store.get_plan("p1")
        self.assertEqual(got.op, "cascade_cancel")
        self.assertEqual(got.graph, graph)

    def test_plans_graph_migration_on_old_db(self):
        import sqlite3, json
        from pacioli.store import _migrate_plans_graph
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE plans (plan_id TEXT PRIMARY KEY, target TEXT, docname TEXT,"
                     " doc_version TEXT, posting_date TEXT, projected_gl TEXT, risk_flags TEXT,"
                     " ts TEXT, op TEXT DEFAULT 'submit', doctype TEXT DEFAULT 'Sales Invoice')")
        _migrate_plans_graph(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)")}
        self.assertIn("graph", cols)
        _migrate_plans_graph(conn)  # idempotent — no error on second run


class TestPlanReconcileFieldsPersistence(unittest.TestCase):
    """F-R2: party_type/party/receivable_payable_account/company — the four fields a reconcile
    plan carries so ``_tool_reconcile`` can rebuild the reconcile call from the PINNED plan alone,
    never from execute-time args. Mirrors TestPlanGraphPersistence exactly — same shape, four
    columns later."""

    def test_reconcile_fields_roundtrip(self):
        from pacioli.plan import new_plan
        store = _store()
        graph = [
            {"payment_type": "Payment Entry", "payment_no": "PAY1", "payment_version": "vP1",
             "payment_date": "2026-07-01", "invoice_type": "Sales Invoice",
             "invoice_no": "INV1", "invoice_version": "vI1", "invoice_date": "2026-07-01",
             "allocated_amount": 100.0, "invoice_outstanding": 100.0,
             "payment_unallocated": 500.0, "company": "Example Corp"},
        ]
        p = new_plan(plan_id="r1", target="prod", doc_version="", posting_date="",
                     docname="r1", op="reconcile", doctype="Payment Reconciliation", graph=graph,
                     party_type="Customer", party="Cust A",
                     receivable_payable_account="Debtors - EC", company="Example Corp")
        store.record_plan(p)
        got = store.get_plan("r1")
        self.assertEqual(got.op, "reconcile")
        self.assertEqual(got.party_type, "Customer")
        self.assertEqual(got.party, "Cust A")
        self.assertEqual(got.receivable_payable_account, "Debtors - EC")
        self.assertEqual(got.company, "Example Corp")
        self.assertEqual(got.graph, graph)

    def test_defaults_are_blank_not_none_for_a_non_reconcile_plan(self):
        # A submit/cancel/cascade plan never sets these — must persist+read back as "", never
        # NULL/None (the store's NOT NULL discipline, matching op/doctype's own backfill posture).
        from pacioli.plan import new_plan
        store = _store()
        p = new_plan(plan_id="p1", target="prod", doc_version="v1", posting_date="2026-07-01",
                     docname="SI-1")
        store.record_plan(p)
        got = store.get_plan("p1")
        self.assertEqual(got.party_type, "")
        self.assertEqual(got.party, "")
        self.assertEqual(got.receivable_payable_account, "")
        self.assertEqual(got.company, "")

    def test_plans_reconcile_fields_migration_on_old_db(self):
        import sqlite3
        from pacioli.store import _migrate_plans_reconcile
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE plans (plan_id TEXT PRIMARY KEY, target TEXT, docname TEXT,"
                     " doc_version TEXT, posting_date TEXT, projected_gl TEXT, risk_flags TEXT,"
                     " ts TEXT, op TEXT DEFAULT 'submit', doctype TEXT DEFAULT 'Sales Invoice',"
                     " graph TEXT DEFAULT '[]')")
        _migrate_plans_reconcile(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)")}
        for col in ("party_type", "party", "receivable_payable_account", "company"):
            self.assertIn(col, cols)
        _migrate_plans_reconcile(conn)  # idempotent — no error on second run

    def test_old_db_full_chain_migrates_and_backfills_blank_reconcile_fields(self):
        # A state db exactly as pre-F-R2 created it (has op/doctype/graph, no reconcile fields) —
        # opening the store must migrate it and read old plans back with blank (not NULL) fields.
        import sqlite3
        from pacioli.store import BrokerStore
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY, target TEXT NOT NULL, docname TEXT NOT NULL,
                doc_version TEXT NOT NULL, posting_date TEXT NOT NULL,
                projected_gl TEXT NOT NULL, risk_flags TEXT NOT NULL, ts TEXT NOT NULL,
                op TEXT NOT NULL DEFAULT 'submit', doctype TEXT NOT NULL DEFAULT 'Sales Invoice',
                graph TEXT NOT NULL DEFAULT '[]'
            );
            INSERT INTO plans(plan_id, target, docname, doc_version, posting_date, projected_gl,
                              risk_flags, ts)
                VALUES ('old1','prod','SI-1','v1','2026-07-01','[]','[]','2026-07-01');
        """)
        store = BrokerStore(conn, key=b"k" * 32)  # opening migrates
        old = store.get_plan("old1")
        self.assertEqual(old.party_type, "")
        self.assertEqual(old.party, "")
        self.assertEqual(old.receivable_payable_account, "")
        self.assertEqual(old.company, "")


class TestVerifySnapshot(unittest.TestCase):
    """verify_snapshot returns the exact receipts it verified, from one read — the anchor's
    verify-then-compare must never run against two different snapshots."""

    def _store(self):
        import sqlite3
        from pacioli.store import BrokerStore
        return BrokerStore(sqlite3.connect(":memory:"), key=b"k" * 32)

    def test_returns_ok_and_the_verified_receipts(self):
        s = self._store()
        i = s.record_intent({"tool": "submit", "docname": "SI-1"})
        s.record_outcome(i, "committed", {"docstatus": 1}, final_marker=None)
        ok, reason, receipts = s.verify_snapshot()
        self.assertTrue(ok)
        self.assertIsNone(reason)
        # The returned list IS the verified chain — same length/head the caller must compare against.
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[-1].hmac, s.head())

    def test_empty_chain_snapshot(self):
        ok, reason, receipts = self._store().verify_snapshot()
        self.assertTrue(ok)
        self.assertEqual(receipts, [])

    def test_expected_head_mismatch_is_caught_in_the_same_call(self):
        s = self._store()
        s.record_intent({"tool": "submit", "docname": "SI-1"})
        ok, reason, receipts = s.verify_snapshot(expected_head="0" * 64)
        self.assertFalse(ok)
        self.assertTrue(receipts)  # still returns what it read, for the caller's message

    def test_keyless_store_refuses(self):
        import sqlite3
        from pacioli.store import BrokerStore
        s = BrokerStore(sqlite3.connect(":memory:"), key=None)
        with self.assertRaises(ValueError):
            s.verify_snapshot()
