# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The Close — Half 1, the period Statement (docs/plans/2026-07-09-the-close.md).

Pure-core tests, bench-free: build_statement reads receipts (constructed directly — it never
re-seals, verification is passed IN), pairs each governed act's intent with its outcome, and
attests the period. The confession (an orphan = intent with no committed outcome) can never be
reported as balanced, and no windowed receipt is ever silently dropped."""
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pacioli.cli import build_parser, cmd_close, main
from pacioli.close import build_statement, render_statement
from pacioli.prove import GENESIS, Receipt
from pacioli.runtime import open_store


def _intent(seq, ts, *, tool="submit", target="bench", doctype="Sales Invoice",
            docname=None, transition="0->1", plan_id="p1"):
    body = {"tool": tool, "target": target, "doctype": doctype, "transition": transition,
            "plan_id": plan_id}
    if docname is not None:
        body["docname"] = docname
    return Receipt(seq=seq, prev_hash=GENESIS, kind="intent", body=body, ts=ts, hmac=f"h{seq}")


def _outcome(seq, ts, finalizes, status="committed"):
    return Receipt(seq=seq, prev_hash=GENESIS, kind="outcome",
                   body={"finalizes": finalizes, "status": status}, ts=ts, hmac=f"h{seq}")


class TestBuildStatement(unittest.TestCase):
    def _basic_chain(self):
        # three acts: committed, recorded-open (failed), orphan (no outcome)
        return [
            _intent(0, "2026-07-01T10:00:00Z", docname="SI-1"),
            _outcome(1, "2026-07-01T10:00:01Z", finalizes=0, status="committed"),
            _intent(2, "2026-07-02T10:00:00Z", docname="SI-2", tool="cancel", transition="1->2"),
            _outcome(3, "2026-07-02T10:00:01Z", finalizes=2, status="failed"),
            _intent(4, "2026-07-03T10:00:00Z", docname="SI-3"),  # orphan — no outcome
        ]

    def test_three_classes_split_correctly(self):
        st = build_statement(self._basic_chain(), target="bench")
        by = st["summary"]["by_class"]
        self.assertEqual(by, {"committed": 1, "recorded_open": 1, "orphan": 1})
        classes = {a["docname"]: a["class"] for a in st["acts"]}
        self.assertEqual(classes, {"SI-1": "committed", "SI-2": "recorded_open", "SI-3": "orphan"})

    def test_orphan_means_not_balanced(self):
        st = build_statement(self._basic_chain(), target="bench", verified=(True, None))
        self.assertFalse(st["balanced"])  # an orphan is an unbalanced entry

    def test_all_committed_and_verified_is_balanced(self):
        chain = [_intent(0, "2026-07-01T10:00:00Z"),
                 _outcome(1, "2026-07-01T10:00:01Z", finalizes=0)]
        st = build_statement(chain, target="bench", verified=(True, None))
        self.assertTrue(st["balanced"])

    def test_failed_act_does_not_block_a_clean_close(self):
        # a `failed` outcome = the bench answered and refused, nothing landed = a KNOWN non-event.
        # It is reported (recorded-open) but must NOT make the period confess — governance correctly
        # refusing a write cannot read as an unbalanced period.
        chain = [_intent(0, "2026-07-01T10:00:00Z"),
                 _outcome(1, "2026-07-01T10:00:01Z", finalizes=0, status="committed"),
                 _intent(2, "2026-07-02T10:00:00Z", docname="SI-2"),
                 _outcome(3, "2026-07-02T10:00:01Z", finalizes=2, status="failed")]
        st = build_statement(chain, target="bench", verified=(True, None))
        self.assertTrue(st["balanced"])
        self.assertEqual(st["summary"]["unconfirmed"], 0)

    def test_unconfirmed_act_blocks_a_clean_close_and_confesses(self):
        # an `unconfirmed` outcome = no answer, MAY have posted = a suspense item. It must block a
        # clean close (like an orphan) — the safety-critical case `prove`'s own sweep also refuses to
        # clear — and be surfaced in the confession, not waved through as "balanced".
        chain = [_intent(0, "2026-07-01T10:00:00Z"),
                 _outcome(1, "2026-07-01T10:00:01Z", finalizes=0, status="committed"),
                 _intent(2, "2026-07-02T10:00:00Z", docname="PE-9", tool="reconcile"),
                 _outcome(3, "2026-07-02T10:00:01Z", finalizes=2, status="unconfirmed")]
        st = build_statement(chain, target="bench", verified=(True, None))
        self.assertFalse(st["balanced"])
        self.assertEqual(st["summary"]["unconfirmed"], 1)
        text = render_statement(st)
        self.assertIn("NOT balanced", text)
        self.assertIn("unconfirmed", text)
        self.assertIn("PE-9", text)
        self.assertIn("MAY have posted", text)

    def test_unverified_chain_is_never_balanced_even_with_no_orphans(self):
        chain = [_intent(0, "2026-07-01T10:00:00Z"),
                 _outcome(1, "2026-07-01T10:00:01Z", finalizes=0)]
        st = build_statement(chain, target="bench", verified=(False, "seal mismatch"))
        self.assertFalse(st["balanced"])
        self.assertEqual(st["chain"]["verify_reason"], "seal mismatch")

    def test_outcome_just_past_until_still_finalizes_its_windowed_intent(self):
        # The act was INITIATED in the period; its outcome landing 1s after `until` must not
        # misreport it as an orphan. Intents window by ts; outcomes resolve against the full chain.
        chain = [_intent(0, "2026-07-01T10:00:00Z", docname="SI-1"),
                 _outcome(1, "2026-07-31T23:59:59Z", finalizes=0, status="committed")]
        st = build_statement(chain, target="bench",
                             since="2026-07-01T00:00:00Z", until="2026-07-01T23:59:59Z")
        self.assertEqual(st["summary"]["by_class"], {"committed": 1, "recorded_open": 0, "orphan": 0})
        self.assertEqual(len(st["acts"]), 1)

    def test_window_filters_intents_by_ts_inclusive_bounds(self):
        chain = [_intent(0, "2026-06-30T23:59:59Z", docname="OLD"),
                 _intent(1, "2026-07-01T00:00:00Z", docname="ON-SINCE"),
                 _intent(2, "2026-07-15T00:00:00Z", docname="MID"),
                 _intent(3, "2026-07-31T23:59:59Z", docname="ON-UNTIL"),
                 _intent(4, "2026-08-01T00:00:00Z", docname="AFTER")]
        st = build_statement(chain, target="bench",
                             since="2026-07-01T00:00:00Z", until="2026-07-31T23:59:59Z")
        names = {a["docname"] for a in st["acts"]}
        self.assertEqual(names, {"ON-SINCE", "MID", "ON-UNTIL"})

    def test_default_window_is_the_whole_chain(self):
        st = build_statement(self._basic_chain(), target="bench")
        self.assertEqual(st["summary"]["total_acts"], 3)
        self.assertEqual(st["period"], {"since": None, "until": None})

    def test_missing_ts_receipt_is_included_and_flagged_never_dropped(self):
        chain = [_intent(0, "2026-07-01T10:00:00Z", docname="SI-1"),
                 Receipt(seq=1, prev_hash=GENESIS, kind="intent",
                         body={"tool": "submit", "target": "bench", "doctype": "Sales Invoice",
                               "docname": "NOTS"}, ts="", hmac="h1")]
        st = build_statement(chain, target="bench",
                             since="2026-07-01T00:00:00Z", until="2026-07-01T23:59:59Z")
        names = {a["docname"] for a in st["acts"]}
        self.assertIn("NOTS", names)  # a windowed statement never silently drops a no-ts act
        self.assertTrue(any("no timestamp" in f.lower() for f in st["flags"]))

    def test_summaries_by_tool_target_doctype(self):
        chain = [_intent(0, "2026-07-01T10:00:00Z", tool="submit", doctype="Sales Invoice"),
                 _outcome(1, "2026-07-01T10:00:01Z", finalizes=0),
                 _intent(2, "2026-07-01T11:00:00Z", tool="cancel", doctype="Payment Entry",
                         transition="1->2"),
                 _outcome(3, "2026-07-01T11:00:01Z", finalizes=2)]
        st = build_statement(chain, target="bench")
        self.assertEqual(st["summary"]["by_tool"], {"submit": 1, "cancel": 1})
        self.assertEqual(st["summary"]["by_doctype"], {"Sales Invoice": 1, "Payment Entry": 1})

    def test_absent_tool_or_doctype_absorbed_not_crashed(self):
        chain = [Receipt(seq=0, prev_hash=GENESIS, kind="intent",
                         body={"target": "bench"}, ts="2026-07-01T10:00:00Z", hmac="h0")]
        st = build_statement(chain, target="bench")
        self.assertEqual(st["summary"]["total_acts"], 1)
        # absent fields bucket under a stable "(unknown)" key, never a KeyError
        self.assertIn("(unknown)", st["summary"]["by_tool"])
        self.assertIn("(unknown)", st["summary"]["by_doctype"])

    def test_empty_chain_is_an_honest_empty_statement_not_a_crash(self):
        st = build_statement([], target="bench", verified=(True, None))
        self.assertEqual(st["summary"]["total_acts"], 0)
        self.assertEqual(st["summary"]["by_class"], {"committed": 0, "recorded_open": 0, "orphan": 0})
        self.assertTrue(st["balanced"])  # nothing to be unbalanced about, and the chain verifies
        self.assertIn("scope_note", st)

    def test_scope_note_always_present_and_names_the_target(self):
        st = build_statement([], target="bench")
        self.assertIn("only", st["scope_note"].lower())
        self.assertIn("bench", st["scope_note"])
        self.assertIn("Reconciliation", st["scope_note"])  # points forward, doesn't overclaim

    def test_chain_block_carries_head_count_and_anchor(self):
        st = build_statement(self._basic_chain(), target="bench",
                             head="deadbeef", verified=(True, None),
                             anchor="deadbeef")
        self.assertEqual(st["chain"]["head"], "deadbeef")
        self.assertEqual(st["chain"]["count"], 5)  # total receipts, not acts
        self.assertTrue(st["chain"]["verified"])
        self.assertEqual(st["chain"]["anchor_head"], "deadbeef")
        self.assertTrue(st["chain"]["anchor_matches"])

    def test_anchor_mismatch_is_flagged_and_unbalances(self):
        st = build_statement(self._basic_chain()[:2], target="bench",
                             head="live123", verified=(True, None), anchor="pinnedZZZ")
        self.assertFalse(st["chain"]["anchor_matches"])
        self.assertFalse(st["balanced"])  # head disagrees with the off-box pin — cannot attest


class TestRenderStatement(unittest.TestCase):
    def _st(self, **kw):
        chain = [_intent(0, "2026-07-01T10:00:00Z", docname="SI-1"),
                 _outcome(1, "2026-07-01T10:00:01Z", finalizes=0),
                 _intent(2, "2026-07-03T10:00:00Z", docname="SI-3")]  # orphan
        return build_statement(chain, target="bench", head="abc123",
                               verified=(True, None), **kw)

    def test_render_is_human_legible_and_shows_counts(self):
        text = render_statement(self._st())
        self.assertIn("bench", text)
        self.assertIn("committed", text.lower())
        self.assertIn("orphan", text.lower())

    def test_render_never_omits_the_scope_caveat(self):
        text = render_statement(self._st())
        self.assertIn("only", text.lower())
        self.assertIn("Reconciliation", text)

    def test_render_shows_the_confession_when_unbalanced(self):
        text = render_statement(self._st())
        # an orphan present -> the render must say so unmistakably, not bury it
        self.assertTrue("SI-3" in text)
        self.assertIn("NOT balanced", text)


_REG = ('[targets.prod]\nbase_url = "https://erp.example.com"\n'
        'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n')


class TestCloseCli(unittest.TestCase):
    """The operator's `pacioli close`, against a real on-disk store (mirrors TestAnchorCli)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(_REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, **kw):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_close(self.env, target=None, since=kw.get("since"), until=kw.get("until"),
                           expected_head=kw.get("expected_head"), as_json=kw.get("as_json", False))
        return rc, o.getvalue(), e.getvalue()

    def test_clean_period_closes_balanced_exit_zero(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("balanced", out.lower())
        self.assertIn("only", out.lower())  # scope caveat never dropped

    def test_orphan_makes_the_close_confess_exit_one(self):
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})  # no outcome
        rc, out, _ = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("NOT balanced", out)
        self.assertIn("ORPH-1", out)

    def test_wrong_expected_head_unbalances_exit_one(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {}, None)
        rc, out, _ = self._run(expected_head="not-the-real-head")
        self.assertEqual(rc, 1)
        self.assertIn("DOES NOT MATCH", out)

    def test_json_mode_emits_structured_statement(self):
        import json
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {}, None)
        rc, out, _ = self._run(as_json=True)
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["summary"]["by_class"]["committed"], 1)
        self.assertIn("scope_note", doc)

    def test_empty_ledger_closes_balanced(self):
        open_store(self.env, "prod")  # creates the store, no receipts
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("0", out)


# --- The Close, Half 2 — the Reconciliation, wired onto `close --reconcile` -----------------

_REG_COMPANY = ('[targets.prod]\nbase_url = "https://erp.example.com"\n'
               'api_key = "env:K"\napi_secret = "env:S"\ncompany = "Example Co"\ndefault = true\n')

_REG_SEAT = _REG_COMPANY.replace("default = true\n",
                                 'seat_user = "seat@example.com"\ndefault = true\n')


def _routing_transport(routes, calls=None):
    """A fake ErpnextClient transport that answers by URL substring — the same shape as
    ``pacioli.tests.test_doctor``'s ``_routing_transport``, redefined here so this module stays
    self-contained. Records every ``(method, url, params, body)`` on ``calls`` when given, so a
    test can inspect exactly what was sent (e.g. the frappe-clock since/until conversion)."""
    if calls is None:
        calls = []
    def transport(method, url, headers, params=None, body=None):
        calls.append((method, url, params, body))
        for fragment, response in routes.items():
            if fragment in url:
                return response
        return 404, None
    transport.calls = calls
    return transport


def _gl_row(voucher_type="Sales Invoice", voucher_no="SI-1", **overrides):
    row = {"voucher_type": voucher_type, "voucher_no": voucher_no, "account": "Debtors - EC",
           "debit": 100.0, "credit": 0.0, "posting_date": "2026-07-01",
           "creation": "2026-07-01 10:00:01.000000", "owner": "seat@example.com",
           "modified": "2026-07-01 10:00:01.000000", "modified_by": "seat@example.com",
           "is_cancelled": 0, "party_type": "Customer", "party": "Cust A"}
    row.update(overrides)
    return row


READY_RECON_ROUTES = {
    "/api/resource/GL%20Entry": (200, {"data": [_gl_row()]}),
    "/api/resource/Accounts%20Settings": (200, {"data": {"enable_immutable_ledger": 1,
                                                          "delete_linked_ledger_entries": 0}}),
    "/api/resource/Repost%20Accounting%20Ledger": (200, {"data": []}),
}


class TestCloseReconcileCli(unittest.TestCase):
    """``close --reconcile`` — the glue that joins Half 1 (the Statement, unchanged) to Half 2
    (the Reconciliation, ``pacioli.reconciliation``) against a FAKE bench transport (never real
    HTTP — the same injection seam ``ErpnextClient`` already exposes, mirroring how
    ``pacioli.doctor.run_doctor`` takes a ``transport=`` for its own bench-free probes)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(_REG_COMPANY)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _write_registry(self, text):
        (Path(self.dir.name) / "targets.toml").write_text(text)

    def _run(self, transport, since="2026-07-01", until="2026-07-31", as_json=False,
             target=None, reconcile=True):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_close(self.env, target=target, since=since, until=until,
                           expected_head=None, as_json=as_json, reconcile=reconcile,
                           transport=transport)
        return rc, o.getvalue(), e.getvalue()

    # --- plain close is untouched --------------------------------------------------------
    def test_plain_close_never_touches_the_bench_when_reconcile_is_false(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {}, None)
        calls = []
        transport = _routing_transport({}, calls)
        rc, out, _ = self._run(transport, reconcile=False)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])  # not one bench call made
        self.assertNotIn("reconciliation for target", out)

    # --- the governed / ungoverned / complete paths ---------------------------------------
    def test_governed_period_renders_both_sections_and_exits_zero(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        rc, out, err = self._run(_routing_transport(dict(READY_RECON_ROUTES)))
        self.assertEqual(rc, 0, err)
        self.assertIn("period statement for target", out)  # Half 1, unchanged
        self.assertIn("reconciliation for target", out)  # Half 2
        self.assertIn("governed:    1", out)
        self.assertIn("complete — posture readable", out)

    def test_ungoverned_movement_present_does_not_flip_exit_code(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        routes = dict(READY_RECON_ROUTES)
        routes["/api/resource/GL%20Entry"] = (200, {"data": [
            _gl_row(),  # governed: matches the SI-1 receipt above
            _gl_row(voucher_no="SI-9", account="Sales - EC", debit=0.0, credit=100.0),
        ]})
        rc, out, err = self._run(_routing_transport(routes))
        self.assertEqual(rc, 0, err)  # ungoverned is presented, never a failure
        self.assertIn("ungoverned:  1", out)
        self.assertIn("did not pass through Pacioli", out)

    def test_seat_user_owner_match_stays_governed(self):
        # With seat_user set, a governed voucher whose GL rows carry that owner stays governed.
        self._write_registry(_REG_SEAT)
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        routes = dict(READY_RECON_ROUTES)
        routes["/api/resource/GL%20Entry"] = (200, {"data": [_gl_row(owner="seat@example.com")]})
        rc, out, err = self._run(_routing_transport(routes))
        self.assertEqual(rc, 0, err)
        self.assertIn("governed:    1", out)

    def test_seat_user_owner_mismatch_downgrades_to_second_generation(self):
        # A governed voucher whose GL rows were stamped by a DIFFERENT user than the seat (e.g. a
        # repost that rewrote them under an admin's name) downgrades to second-generation — surfaced,
        # never a false clean. This is the whole point of owner corroboration (Fork III).
        self._write_registry(_REG_SEAT)
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        routes = dict(READY_RECON_ROUTES)
        routes["/api/resource/GL%20Entry"] = (200, {"data": [_gl_row(owner="admin@evil")]})
        rc, out, err = self._run(_routing_transport(routes))
        self.assertEqual(rc, 0, err)  # still complete — presented, not failed (accounting mode)
        self.assertIn("governed:    0", out)
        self.assertIn("2nd-gen:     1", out)

    def test_unbalanced_statement_with_reconcile_still_exits_nonzero(self):
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})  # orphan
        rc, out, err = self._run(_routing_transport(dict(READY_RECON_ROUTES)))
        self.assertNotEqual(rc, 0)
        self.assertIn("NOT balanced", out)

    def test_posture_unreadable_makes_reconciliation_incomplete_and_flips_exit_code(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        routes = dict(READY_RECON_ROUTES)
        # enable_immutable_ledger is simply ABSENT — a readable call, an unreadable field.
        routes["/api/resource/Accounts%20Settings"] = (
            200, {"data": {"delete_linked_ledger_entries": 0}})
        rc, out, err = self._run(_routing_transport(routes))
        self.assertNotEqual(rc, 0)
        self.assertIn("NOT complete", out)  # it still RENDERS — this is not a build refusal
        self.assertIn("enable_immutable_ledger is None", out)

    # --- unreadable AUDIT SOURCE (fatal) vs. unreadable reposts (non-fatal) ---------------
    def test_unreadable_gl_sweep_refuses_nonzero_with_clear_error(self):
        routes = dict(READY_RECON_ROUTES)
        routes["/api/resource/GL%20Entry"] = (403, {"exc_type": "PermissionError"})
        rc, out, err = self._run(_routing_transport(routes))
        self.assertNotEqual(rc, 0)
        self.assertIn("error:", err)
        self.assertIn("unreadable", err.lower())

    def test_unreadable_accounts_settings_refuses_nonzero_with_clear_error(self):
        routes = dict(READY_RECON_ROUTES)
        routes["/api/resource/Accounts%20Settings"] = (403, {"exc_type": "PermissionError"})
        rc, out, err = self._run(_routing_transport(routes))
        self.assertNotEqual(rc, 0)
        self.assertIn("error:", err)
        self.assertIn("unreadable", err.lower())

    def test_non_dict_accounts_settings_refuses_without_crashing(self):
        # A proxy-shaped {"data": null} body for Accounts Settings must refuse gracefully — the glue
        # never raises past its boundary (sec-A: the later settings.get() would AttributeError).
        routes = dict(READY_RECON_ROUTES)
        routes["/api/resource/Accounts%20Settings"] = (200, {"data": None})
        rc, out, err = self._run(_routing_transport(routes))
        self.assertNotEqual(rc, 0)
        self.assertIn("error:", err)
        self.assertIn("unreadable", err.lower())
        self.assertNotIn("Traceback", err)

    def test_unreadable_reposts_is_a_nonfatal_flag_reconciliation_still_renders(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        routes = dict(READY_RECON_ROUTES)
        routes["/api/resource/Repost%20Accounting%20Ledger"] = (
            403, {"exc_type": "PermissionError"})
        rc, out, err = self._run(_routing_transport(routes))
        self.assertEqual(rc, 0, err)  # non-fatal — the reconciliation still completes
        self.assertIn("repost source unreadable", out)
        self.assertIn("second-generation attribution unavailable", out)

    # --- deny-biased refusals: no company pin, no bounded window --------------------------
    def test_missing_company_pin_refuses(self):
        self._write_registry(_REG)  # module-level fixture above — no `company =`
        rc, out, err = self._run(_routing_transport(dict(READY_RECON_ROUTES)))
        self.assertNotEqual(rc, 0)
        self.assertIn("company", err.lower())

    def test_missing_since_refuses(self):
        rc, out, err = self._run(_routing_transport(dict(READY_RECON_ROUTES)),
                                 since=None, until="2026-07-31")
        self.assertNotEqual(rc, 0)
        self.assertIn("--since", err)
        self.assertIn("--until", err)

    def test_missing_until_refuses(self):
        rc, out, err = self._run(_routing_transport(dict(READY_RECON_ROUTES)),
                                 since="2026-07-01", until=None)
        self.assertNotEqual(rc, 0)
        self.assertIn("--since", err)
        self.assertIn("--until", err)

    # --- --json emits the combined object --------------------------------------------------
    def test_json_mode_emits_combined_statement_and_reconciliation(self):
        import json
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        rc, out, err = self._run(_routing_transport(dict(READY_RECON_ROUTES)), as_json=True)
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertIn("statement", doc)
        self.assertIn("reconciliation", doc)
        self.assertTrue(doc["statement"]["balanced"])
        self.assertEqual(doc["reconciliation"]["summary"]["governed"], 1)
        self.assertTrue(doc["reconciliation"]["complete"])

    def test_json_mode_plain_close_is_unchanged_shape(self):
        import json
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {}, None)
        rc, out, err = self._run(_routing_transport({}), as_json=True, reconcile=False)
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertNotIn("reconciliation", doc)  # bare Statement shape, exactly as before
        self.assertIn("scope_note", doc)

    # --- the since/until -> frappe-clock conversion ----------------------------------------
    def test_since_until_converted_to_frappe_clock_for_the_gl_sweep(self):
        import json
        calls = []
        transport = _routing_transport(dict(READY_RECON_ROUTES), calls)
        rc, out, err = self._run(transport, since="2026-07-01T00:00:00Z",
                                 until="2026-07-31T23:59:59Z")
        self.assertEqual(rc, 0, err)
        gl_call = next(c for c in calls if "/api/resource/GL%20Entry" in c[1])
        filters = json.loads(gl_call[2]["filters"])
        since_bound = next(f[2] for f in filters if f[0] == "creation" and f[1] == ">=")
        until_bound = next(f[2] for f in filters if f[0] == "creation" and f[1] == "<=")
        self.assertEqual(since_bound, "2026-07-01 00:00:00")
        self.assertEqual(until_bound, "2026-07-31 23:59:59")

    def test_bare_date_since_is_midnight_until_is_end_of_day(self):
        # A bare-date LOWER bound reads as midnight (correct inclusive start). A bare-date UPPER
        # bound must cover the WHOLE day — MariaDB reads "2026-07-31" as 00:00:00 and would drop
        # every row created during the 31st (F3), so the until bound expands to end-of-day.
        import json
        calls = []
        transport = _routing_transport(dict(READY_RECON_ROUTES), calls)
        rc, out, err = self._run(transport, since="2026-07-01", until="2026-07-31")
        self.assertEqual(rc, 0, err)
        gl_call = next(c for c in calls if "/api/resource/GL%20Entry" in c[1])
        filters = json.loads(gl_call[2]["filters"])
        since_bound = next(f[2] for f in filters if f[0] == "creation" and f[1] == ">=")
        until_bound = next(f[2] for f in filters if f[0] == "creation" and f[1] == "<=")
        self.assertEqual(since_bound, "2026-07-01")
        self.assertEqual(until_bound, "2026-07-31 23:59:59.999999")


class TestCloseReconcileFlagWiring(unittest.TestCase):
    """The argparse flag exists and `main` threads it into `cmd_close` — the seam the controller
    needs confirmed, independent of any bench/store behavior."""

    def test_reconcile_flag_defaults_false(self):
        args = build_parser().parse_args(["close"])
        self.assertFalse(args.reconcile)

    def test_reconcile_flag_can_be_set(self):
        args = build_parser().parse_args(["close", "--reconcile"])
        self.assertTrue(args.reconcile)

    def test_main_threads_reconcile_into_cmd_close(self):
        with mock.patch("pacioli.cli.cmd_close", return_value=0) as m:
            rc = main(["close", "--reconcile"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertEqual(rc, 0)
        self.assertTrue(m.called)
        self.assertTrue(m.call_args.kwargs.get("reconcile"))

    def test_main_passes_reconcile_false_by_default(self):
        with mock.patch("pacioli.cli.cmd_close", return_value=0) as m:
            main(["close"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertFalse(m.call_args.kwargs.get("reconcile"))


class TestCloseRespond(unittest.TestCase):
    """close --respond — Half 3 (response-to-gap) wired into the CLI. Arm-free: over a statement
    alone (no --reconcile) it produces the statement-side response (orphans / unconfirmed) at the
    default mixed_door posture; the reconciliation-side findings join under --reconcile (bench)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(_REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, **kw):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_close(self.env, target=None, since=kw.get("since"), until=kw.get("until"),
                           expected_head=kw.get("expected_head"), as_json=kw.get("as_json", False),
                           respond=kw.get("respond", False))
        return rc, o.getvalue(), e.getvalue()

    def test_respond_clean_period_is_record_exit_zero(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        rc, out, _ = self._run(respond=True)
        self.assertEqual(rc, 0)
        low = out.lower()
        self.assertIn("mixed_door", low)   # default posture named
        self.assertIn("record", low)       # nothing rises to a reaction

    def test_respond_orphan_is_a_finding_exit_one(self):
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})  # no outcome
        rc, out, _ = self._run(respond=True)
        self.assertEqual(rc, 1)
        self.assertIn("ORPH-1", out)
        self.assertIn("[alert] orphan", out)

    def test_respond_json_includes_response_block(self):
        import json
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "target": "prod", "docname": "ORPH-2"})
        rc, out, _ = self._run(as_json=True, respond=True)
        self.assertEqual(rc, 1)
        doc = json.loads(out)
        self.assertIn("response", doc)
        self.assertEqual(doc["response"]["response"], "alert")
        self.assertIn("statement", doc)   # statement kept alongside, never replaced

    def test_respond_still_renders_the_statement(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod", "docname": "SI-9"})
        store.record_outcome(intent, "committed", {}, None)
        rc, out, _ = self._run(respond=True)
        self.assertIn("period statement", out.lower())     # statement view not replaced
        self.assertIn("response for target", out.lower())  # response view appended

    def test_registry_posture_is_threaded_into_the_response(self):
        # Fork C: a per-target `posture = "sole_door"` in the registry is read and applied — the
        # response names it (statement-only has no ungoverned to elevate, so we prove the wiring).
        Path(self.dir.name, "targets.toml").write_text(
            _REG.replace("default = true\n", 'default = true\nposture = "sole_door"\n'))
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {}, None)
        _, out, _ = self._run(respond=True)
        self.assertIn("sole_door", out)
        self.assertNotIn("mixed_door", out)

    def test_main_threads_respond_into_cmd_close(self):
        with mock.patch("pacioli.cli.cmd_close", return_value=0) as m:
            rc = main(["close", "--respond"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertEqual(rc, 0)
        self.assertTrue(m.call_args.kwargs.get("respond"))

    def test_main_passes_respond_false_by_default(self):
        with mock.patch("pacioli.cli.cmd_close", return_value=0) as m:
            main(["close"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertFalse(m.call_args.kwargs.get("respond"))


if __name__ == "__main__":
    unittest.main()
