# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The seal gate — the choke point (Task 2, docs/plans/2026-07-14-close-half3-seal-slice.md).

Task 1 gave ``BrokerStore`` a fail-closed, evented seal state (``seal``/``unseal``/``seal_state``/
``seal_events``). This module proves the CALLER side: a sealed broker refuses every governed write
at the ONE place all of them dispatch through (``PacioliBroker.dispatch``), the handler never runs
(nothing is claimed, nothing is spent), every read-only tool is unaffected — even when the seal
state itself is corrupt or unreadable — and the human mint CLI carries its own defense-in-depth
pre-check ahead of the authoritative keyed gate.

Fixtures mirror ``test_tools.py``'s shape (a fake ERPNext client + a real in-memory
:class:`~pacioli.store.BrokerStore`) but are kept deliberately lean and self-contained — this
module needs only enough doctype fixtures to prove the gate's classification, not the full
per-doctype breadth ``test_tools.py`` already covers.
"""
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pacioli.cli import cmd_mint
from pacioli.registry import load_registry
from pacioli.runtime import open_store
from pacioli.store import BrokerStore
from pacioli.tools import READ_ONLY_TOOLS, PacioliBroker, format_seal_refusal, tool_names

CLOCK = "2026-07-14T00:00:00Z"

REG = ('[targets.prod]\nbase_url = "https://erp.example.com"\ncompany = "Example Corp"\n'
       'api_key = "k"\napi_secret = "env:S"\ndefault = true\n')


class FakeClient:
    """Lean fake: just enough doctype fixtures (one draft per supported doctype) to exercise every
    read tool and one representative governed pair (Sales Invoice plan_submit/submit_sales_invoice)
    — full per-doctype breadth already lives in ``test_tools.py``; this module's job is the GATE,
    not doctype coverage."""

    def __init__(self):
        self.docs = {
            "SI-1": {"name": "SI-1", "docstatus": 0, "company": "Example Corp",
                     "posting_date": "2026-07-01", "modified": "2026-07-01 10:00:00.000001"},
            "PI-1": {"name": "PI-1", "docstatus": 0, "company": "Example Corp",
                     "posting_date": "2026-07-01", "modified": "2026-07-01 10:00:00.000001"},
            "PE-1": {"name": "PE-1", "docstatus": 0, "company": "Example Corp",
                     "posting_date": "2026-07-01", "modified": "2026-07-01 10:00:00.000001"},
            "JE-1": {"name": "JE-1", "docstatus": 0, "company": "Example Corp",
                     "voucher_type": "Journal Entry", "accounts": [],
                     "posting_date": "2026-07-01", "modified": "2026-07-01 10:00:00.000001"},
        }
        self.locks = {}
        self.workflows = []
        self.submitted = []

    def get_document(self, doctype, name):
        from pacioli.erpnext import ErpnextError
        if name not in self.docs:
            raise ErpnextError(f"HTTP 404: {name} not found", status=404, answered=True)
        return dict(self.docs[name])

    def list_documents(self, doctype, filters=None, limit=20, party_field="customer"):
        return [dict(d) for d in self.docs.values()]

    def ledger_preview(self, company, doctype, docname):
        return {"gl_columns": [], "gl_data": [{"account": "Debtors", "debit": 100.0}]}

    def get_period_locks(self, company, doctype, posting_date):
        return dict(self.locks)

    def get_active_workflows(self, doctype):
        return [dict(w) for w in self.workflows]

    def get_workflow_state(self, doctype, name, state_field):
        return None

    def submit_document(self, doctype, name, doc=None):
        self.submitted.append(name)
        return {"name": name, "docstatus": 1, "modified": "2026-07-01 10:05:00.000001"}

    def cancel_document(self, doctype, name):
        return {"name": name, "docstatus": 2, "modified": "2026-07-01 11:00:00.000001"}


class NoCallClient:
    """A client double for the blanket sealed-governed-tool sweep: ANY attribute access resolves to
    a callable that records the call and returns an empty placeholder, never raises. The seal gate
    must short-circuit every governed tool BEFORE its handler ever reaches for the client, so a
    correct gate leaves ``self.calls`` empty no matter which governed tool is dispatched — a broken
    gate would instead accumulate real call names here (a loud, precise failure signal, not a
    crash that could be confused with an unrelated bug)."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append(name)
            return {}
        return _record


class RaisingSealStateStore:
    """A store double whose ``seal_state()`` itself raises — simulates a genuine SQL/connection
    failure, distinct from Task 1's own deny-biased malformed-CONTENT handling (which never
    raises; see ``BrokerStore.seal_state``'s docstring). Every OTHER method raises loudly if
    called — the gate must deny before the handler ever reaches for the store's substantive
    methods (``get_plan``, ``record_plan``, ``claim_marker``, ...)."""

    def seal_state(self):
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    def __getattr__(self, name):
        def _boom(*a, **k):
            raise AssertionError(
                f"store.{name} must not be reached once seal_state() itself raised")
        return _boom


class RaisingSealStateStoreBareSqliteError:
    """Same shape as :class:`RaisingSealStateStore`, but raises the BASE ``sqlite3.Error`` rather
    than the ``OperationalError`` subclass — pins that the gate's deny-bias catches the whole
    ``sqlite3.Error`` hierarchy (review F1, Task 2 review), not merely the one subclass the other
    fixture happens to use, and that the resulting reason stays honest that this is an UNREADABLE
    seal, never a confirmed seal event."""

    def seal_state(self):
        raise sqlite3.Error("database disk image is malformed (simulated)")

    def __getattr__(self, name):
        def _boom(*a, **k):
            raise AssertionError(
                f"store.{name} must not be reached once seal_state() itself raised")
        return _boom


def make_broker(client=None, reg=None, key=b"k" * 32):
    client = client or FakeClient()
    stores = {}

    def store_provider(target_name):
        if target_name not in stores:
            stores[target_name] = BrokerStore(sqlite3.connect(":memory:"), key=key,
                                              now_iso=lambda: CLOCK)
        return stores[target_name]

    broker = PacioliBroker(
        registry=load_registry(toml_text=reg or REG),
        store_provider=store_provider,
        client_provider=lambda target: client,
        now_epoch=lambda: 1_000.0,
        now_date=lambda: "2026-07-01",
    )
    return broker, client, store_provider


# name -> a minimal args dict that lets a READ tool actually succeed (not merely "not refused") —
# used by both the plain-sealed and corrupt-seal-state read survival tests below.
def _read_args(name):
    doc_for = {"get_sales_invoice": "SI-1", "get_purchase_invoice": "PI-1",
               "get_payment_entry": "PE-1", "get_journal_entry": "JE-1"}
    if name in doc_for:
        return {"name": doc_for[name]}
    if name == "workflow_status":
        return {"name": "SI-1"}
    return {}


class TestClassificationCompleteness(unittest.TestCase):
    """Deny-biased classification (the brief's headline invariant): every ``_tool_*`` attribute on
    PacioliBroker must be either read-only or explicitly accounted for by this suite's governed
    list — a new tool nobody classified must fail THIS test, loudly, rather than silently landing
    ungated (or silently landing gated with nobody noticing it was never exercised)."""

    # The 19 governed tools this module's TDD matrix actually exercises (directly, or via the
    # blanket sweep in TestSealGateBlocksEveryGovernedTool). Anything dispatch-resolvable that is
    # NOT in READ_ONLY_TOOLS and NOT named here is uncovered — the test below fails loudly.
    GOVERNED_TOOLS_COVERED_BY_THIS_SUITE = frozenset({
        "submit_sales_invoice", "cancel_sales_invoice", "amend_sales_invoice",
        "submit_purchase_invoice", "cancel_purchase_invoice", "amend_purchase_invoice",
        "submit_payment_entry", "cancel_payment_entry", "amend_payment_entry",
        "submit_journal_entry", "cancel_journal_entry", "amend_journal_entry",
        "plan_submit", "plan_cancel", "plan_cascade_cancel", "cascade_cancel",
        "plan_reconcile", "reconcile", "request_workflow_transition",
    })

    def test_every_tool_is_read_only_or_gate_covered(self):
        all_names = {n[len("_tool_"):] for n in dir(PacioliBroker) if n.startswith("_tool_")}
        uncovered = all_names - READ_ONLY_TOOLS - self.GOVERNED_TOOLS_COVERED_BY_THIS_SUITE
        self.assertEqual(
            uncovered, set(),
            f"tool(s) {sorted(uncovered)} are neither classified READ_ONLY_TOOLS nor covered by "
            "this seal-gate test suite's governed list — a NEW tool is born gated by dispatch() "
            "regardless (anything outside READ_ONLY_TOOLS is seal-gated by construction), but it "
            "must still be consciously classified here: add it to READ_ONLY_TOOLS in tools.py "
            "ONLY if it is truly read-only, otherwise add it to "
            "GOVERNED_TOOLS_COVERED_BY_THIS_SUITE above and extend the seal-gate TDD matrix")

    def test_read_only_and_governed_partition_every_tool_exactly(self):
        # No drift, no double-counting: the two sets exactly partition the real tool surface.
        self.assertEqual(READ_ONLY_TOOLS | self.GOVERNED_TOOLS_COVERED_BY_THIS_SUITE,
                         set(tool_names()))
        self.assertEqual(READ_ONLY_TOOLS & self.GOVERNED_TOOLS_COVERED_BY_THIS_SUITE, set())


class TestSealGateBlocksEveryGovernedTool(unittest.TestCase):
    def test_every_governed_tool_is_refused_while_sealed_handler_never_runs(self):
        client = NoCallClient()
        broker, _, stores = make_broker(client=client)
        store = stores("prod")
        store.seal("incident under investigation", source="operator")

        governed = sorted(set(tool_names()) - READ_ONLY_TOOLS)
        self.assertTrue(governed)  # sanity: there is something for the gate to cover
        for name in governed:
            with self.subTest(tool=name):
                out = broker.dispatch(name, {})
                self.assertFalse(out["ok"], f"{name} must be refused while sealed: {out}")
                self.assertEqual(out["stage"], "seal", f"{name} wrong stage: {out}")
                self.assertIn("SEALED", out["reason"])
                self.assertIn("pacioli unseal --reason", out["reason"])
                self.assertEqual(client.calls, [],
                                 f"{name} reached the client while sealed: {client.calls}")

    def test_refusal_names_since_reason_source(self):
        client = NoCallClient()
        broker, _, stores = make_broker(client=client)
        stores("prod").seal("Q3 reconciliation gap", source="operator")
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "seal")
        self.assertIn(CLOCK, out["reason"])
        self.assertIn("Q3 reconciliation gap", out["reason"])
        self.assertIn("operator", out["reason"])


class TestReadOnlyToolsSurviveSealed(unittest.TestCase):
    def test_all_read_only_tools_succeed_while_sealed(self):
        broker, client, stores = make_broker()
        stores("prod").seal("incident", source="operator")
        for name in sorted(READ_ONLY_TOOLS):
            with self.subTest(tool=name):
                out = broker.dispatch(name, _read_args(name))
                self.assertTrue(out["ok"], f"{name} must still succeed while sealed: {out}")


class TestReadOnlyToolsSurviveCorruptSealState(unittest.TestCase):
    """Global constraint #6 (the plan): reads never sealed — not even when the seal_events history
    itself is corrupt. Read tools skip target/store resolution on the seal-gate path entirely
    (their own handlers route independently), so a gap/zero-row/unverifiable seal state must never
    surface as a read failure."""

    def _corrupt_gap(self, store):
        store.seal("first", source="operator")  # seq=2 (genesis=1, seal=2)
        store._conn.execute("DELETE FROM seal_events WHERE seq=1")  # gap: only seq=2 remains
        state = store.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "seal history gap (rollback?)")

    def test_reads_survive_a_seal_history_gap(self):
        broker, client, stores = make_broker()
        self._corrupt_gap(stores("prod"))
        for name in sorted(READ_ONLY_TOOLS):
            with self.subTest(tool=name):
                out = broker.dispatch(name, _read_args(name))
                self.assertTrue(out["ok"], f"{name} must survive a corrupt seal state: {out}")

    def test_governed_tool_denies_naming_the_gap_cause(self):
        broker, client, stores = make_broker()
        self._corrupt_gap(stores("prod"))
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "seal")
        self.assertIn("gap", out["reason"].lower())


class TestSealStateExceptionDenyBiased(unittest.TestCase):
    def test_seal_state_raising_denies_the_write_stage_seal_handler_never_runs(self):
        client = NoCallClient()
        broker = PacioliBroker(
            registry=load_registry(toml_text=REG),
            store_provider=lambda name: RaisingSealStateStore(),
            client_provider=lambda target: client,
            now_epoch=lambda: 1_000.0,
            now_date=lambda: "2026-07-01",
        )
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "seal")
        self.assertIn("disk I/O error", out["reason"])
        self.assertEqual(client.calls, [])

    def test_seal_state_raising_bare_sqlite3_error_denies_stage_seal_honestly(self):
        # Fix 1 (review F1, Task 2 review): once the store itself has resolved cleanly, a failure
        # reading seal_state() — here the BASE sqlite3.Error, not merely OperationalError — is the
        # one case _seal_gate itself denies, at stage="seal". The reason must carry the raised
        # cause AND say plainly this is not a confirmed seal event (an unreadable seal, denied
        # deny-biased — never mistaken for "probably unsealed" or for a genuine seal record).
        client = NoCallClient()
        broker = PacioliBroker(
            registry=load_registry(toml_text=REG),
            store_provider=lambda name: RaisingSealStateStoreBareSqliteError(),
            client_provider=lambda target: client,
            now_epoch=lambda: 1_000.0,
            now_date=lambda: "2026-07-01",
        )
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "seal")
        self.assertIn("database disk image is malformed", out["reason"])
        self.assertIn("NOT a confirmed seal event", out["reason"])
        self.assertEqual(client.calls, [])

    def test_registry_error_on_a_governed_tool_still_denies_never_crashes(self):
        # An unknown pacioli_target: the seal gate resolves the target itself (the same
        # pacioli_target path _route uses) before it can even look up a store — deny-biased, this
        # too must refuse rather than let a raw RegistryError escape dispatch(). Review F1 (Task 2
        # review): resolution precedes seal knowledge entirely, so this must land at the SAME
        # stage a read-only tool's own _route call would have produced for the identical failure
        # — "request", the pre-Task-2 shape — never "seal" (an unknown target has no store to be
        # sealed).
        client = NoCallClient()
        broker, _, _ = make_broker(client=client)
        out = broker.dispatch("plan_submit", {"name": "SI-1", "pacioli_target": "nonexistent"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")
        self.assertIn("unknown target", out["reason"])
        self.assertEqual(client.calls, [])

    def test_registry_error_on_a_governed_tool_stays_request_stage_even_while_sealed(self):
        # Same unknown target, but this time the broker's OWN configured ("prod") store IS
        # sealed. Must not matter: resolving "nonexistent" fails before any store is even opened,
        # so there is no store for this call to find sealed — stage stays "request", proving the
        # documented order (resolution precedes seal knowledge) rather than merely happening to
        # hold when unsealed.
        client = NoCallClient()
        broker, _, stores = make_broker(client=client)
        stores("prod").seal("incident under investigation", source="operator")
        out = broker.dispatch("plan_submit", {"name": "SI-1", "pacioli_target": "nonexistent"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")
        self.assertIn("unknown target", out["reason"])
        self.assertEqual(client.calls, [])

    def test_store_corrupt_error_on_a_governed_tool_is_stage_store_not_seal(self):
        # The other resolution failure the ruling names: a torn/corrupt store file. Before Task 2
        # this landed as dispatch()'s pre-existing StoreCorruptError clause (stage="store") — the
        # same shape test_tools.py pins for the read-only path
        # (test_torn_store_on_the_server_path_is_a_structured_deny_not_a_raw_error). A governed
        # tool must get the identical stage, never "seal" — seal_state() is never even reached.
        from pacioli.store import StoreCorruptError

        def torn_provider(target_name):
            raise StoreCorruptError(
                "state db is only 1 bytes — smaller than a valid SQLite header")

        client = NoCallClient()
        broker = PacioliBroker(
            registry=load_registry(toml_text=REG),
            store_provider=torn_provider,
            client_provider=lambda target: client,
            now_epoch=lambda: 1_000.0,
            now_date=lambda: "2026-07-01",
        )
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "store")
        self.assertIn("header", out["reason"].lower())
        self.assertEqual(client.calls, [])


class TestUnsealedByteIdenticalBehavior(unittest.TestCase):
    """Pins the pre-seal (0.19.0) shape for one representative success and one representative
    deny — a fresh keyed store's genesis row reads as unsealed (Task 1), and the gate must add
    NOTHING to either response in that state."""

    def test_plan_submit_success_shape_unchanged(self):
        broker, client, stores = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertEqual(set(out), {"ok", "plan_id", "docname", "target", "doctype",
                                     "doc_version", "posting_date", "projected_gl", "risk_flags",
                                     "workflow", "next"})
        self.assertEqual(out["docname"], "SI-1")
        self.assertEqual(out["target"], "prod")

    def test_submit_with_unknown_plan_deny_shape_unchanged(self):
        broker, client, stores = make_broker()
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": "nope", "marker": "t"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")  # NOT "seal" — the ordinary plan-stage refusal
        self.assertEqual(set(out), {"ok", "stage", "reason"})
        self.assertEqual(client.submitted, [])


class TestMarkerSurvivesSealedRefusal(unittest.TestCase):
    """The F-C2 invariant, extended to the seal: consent is spent by COMMITMENT, never by
    refusal. A marker minted before a seal must still be live after a sealed attempt to spend it,
    and must still work — the SAME marker — once the seal clears."""

    def test_mint_seal_refused_marker_intact_unseal_same_marker_commits(self):
        broker, client, stores = make_broker()
        store = stores("prod")

        plan = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(plan["ok"], plan)
        token = "raw-marker-token"
        store.mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        self.assertEqual(store.marker_state(token), "live")

        store.seal("incident under investigation", source="operator")
        self.assertTrue(store.seal_state()["sealed"])

        refused = broker.dispatch(
            "submit_sales_invoice",
            {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["stage"], "seal")
        # nothing spent, nothing landed: the marker is untouched and the bench never saw a submit.
        self.assertEqual(store.marker_state(token), "live")
        self.assertEqual(client.submitted, [])

        state = store.unseal("resolved")
        self.assertFalse(state["sealed"])

        committed = broker.dispatch(
            "submit_sales_invoice",
            {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(committed["ok"], committed)
        self.assertEqual(client.submitted, ["SI-1"])
        self.assertEqual(store.marker_state(token), "consumed")


class TestFormatSealRefusal(unittest.TestCase):
    def test_names_since_reason_source_cause_and_the_unseal_instruction(self):
        text = format_seal_refusal({"sealed": True, "since": "2026-07-14T00:00:00Z",
                                    "reason": "incident", "source": "operator", "seq": 2,
                                    "cause": None})
        for expect in ("2026-07-14T00:00:00Z", "incident", "operator",
                      "pacioli unseal --reason"):
            self.assertIn(expect, text)

    def test_names_the_cause_when_present(self):
        text = format_seal_refusal({"sealed": True, "since": None, "reason": None,
                                    "source": None, "seq": 0, "cause": "no seal history"})
        self.assertIn("no seal history", text)


class TestMintCliSealGate(unittest.TestCase):
    """cli.cmd_mint's keyless, defense-in-depth pre-check — never the authoritative gate (that is
    the keyed dispatch-time _seal_gate above), but a sealed store must still refuse to mint."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(
            '[targets.prod]\nbase_url = "https://erp.example.com"\n'
            'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n')
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _mint(self, plan_id="p1", ttl=900):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cmd_mint(self.env, plan_id=plan_id, target=None, ttl=ttl)
        return rc, out.getvalue() + err.getvalue()

    def _record_plan(self):
        store = open_store(self.env, "prod")  # keyed
        from pacioli.plan import new_plan
        store.record_plan(new_plan("p1", "prod", "v1", "2026-07-01", docname="SI-1"))
        return store

    def test_mint_refused_on_sealed_store_no_marker_minted(self):
        store = self._record_plan()
        store.seal("incident", source="operator")

        rc, out = self._mint()
        self.assertNotEqual(rc, 0)
        self.assertIn("SEALED", out)
        self.assertIn("pacioli unseal --reason", out)

        store2 = open_store(self.env, "prod", with_key=False)
        count = store2._conn.execute("SELECT COUNT(*) FROM markers").fetchone()[0]
        self.assertEqual(count, 0)

    def test_mint_unaffected_when_unsealed(self):
        self._record_plan()
        rc, out = self._mint()
        self.assertEqual(rc, 0)
        self.assertIn("marker:", out)


if __name__ == "__main__":
    unittest.main()
