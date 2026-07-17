# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""``close --respond --envelope`` + auto-seal on CONTAIN (Task 4,
docs/plans/2026-07-14-close-half3-seal-slice.md).

Tasks 1-3 gave the store its fail-closed, evented seal state, gated every governed write behind
it, and gave the operator `pacioli seal`/`unseal`/`seal-status`. This module wires the LAST
door: an operator can escalate a finding class above its response.py floor via
`--envelope CLASS=LEVEL`, and when that escalation reaches `contain` the close itself seals the
store (`source="response"`) — CONTAIN's teeth. `--envelope` parsing is a STRICTER boundary than
response.py's floor semantics (which the parsed dict feeds unmodified): a malformed entry here
refuses the WHOLE close (usage error, exit 2) rather than quietly degrading to a weaker envelope.

Fixture mirrors `TestCloseRespond` (test_close.py) — a real on-disk store under a temp dir.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pacioli.cli import _parse_envelope, build_parser, cmd_close, main
from pacioli.runtime import open_store
from pacioli.store import BrokerStore

_REG = ('[targets.prod]\nbase_url = "https://erp.example.com"\n'
        'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n')


# --- _parse_envelope: the pure CLI-boundary parser, unit-tested directly ---------------------

class TestParseEnvelope(unittest.TestCase):
    def test_no_entries_no_respond_is_empty_dict(self):
        d, err = _parse_envelope(None, False)
        self.assertEqual(d, {})
        self.assertIsNone(err)

    def test_no_entries_with_respond_is_empty_dict(self):
        d, err = _parse_envelope([], True)
        self.assertEqual(d, {})
        self.assertIsNone(err)

    def test_good_entry_parses(self):
        d, err = _parse_envelope(["orphan=contain"], True)
        self.assertIsNone(err)
        self.assertEqual(d, {"orphan": "contain"})

    def test_repeatable_entries_all_present(self):
        d, err = _parse_envelope(["orphan=contain", "ungoverned=alert"], True)
        self.assertIsNone(err)
        self.assertEqual(d, {"orphan": "contain", "ungoverned": "alert"})

    def test_every_finding_class_accepted(self):
        for cls in ("orphan", "unconfirmed", "second_generation", "blind_read",
                    "adverse_posture", "ungoverned"):
            d, err = _parse_envelope([f"{cls}=alert"], True)
            self.assertIsNone(err, cls)
            self.assertEqual(d, {cls: "alert"})

    def test_every_level_accepted(self):
        for level in ("record", "alert", "attestation_gate", "contain"):
            d, err = _parse_envelope([f"orphan={level}"], True)
            self.assertIsNone(err, level)
            self.assertEqual(d, {"orphan": level})

    def test_present_without_respond_is_an_error(self):
        d, err = _parse_envelope(["orphan=contain"], False)
        self.assertIsNone(d)
        self.assertIsNotNone(err)
        self.assertIn("--respond", err)

    def test_bad_class_is_an_error_naming_the_entry(self):
        d, err = _parse_envelope(["nonsense=contain"], True)
        self.assertIsNone(d)
        self.assertIn("nonsense=contain", err)
        self.assertIn("class", err.lower())

    def test_bad_level_is_an_error_naming_the_entry(self):
        d, err = _parse_envelope(["orphan=obliterate"], True)
        self.assertIsNone(d)
        self.assertIn("orphan=obliterate", err)
        self.assertIn("level", err.lower())

    def test_missing_equals_is_an_error(self):
        d, err = _parse_envelope(["orphan"], True)
        self.assertIsNone(d)
        self.assertIn("orphan", err)

    def test_one_bad_entry_among_good_ones_refuses_the_whole_dict(self):
        # deny-biased: an operator who asked for an escalation must never silently get a WEAKER
        # one just because one entry among several was malformed.
        d, err = _parse_envelope(["orphan=contain", "bogus=contain"], True)
        self.assertIsNone(d)
        self.assertIsNotNone(err)

    def test_duplicate_class_weaker_second_is_refused(self):
        # a repeated class letting the LAST value win is a silent weakening (orphan=contain,
        # then quietly downgraded to orphan=alert) — refused outright, like any malformed entry.
        d, err = _parse_envelope(["orphan=contain", "orphan=alert"], True)
        self.assertIsNone(d)
        self.assertIsNotNone(err)
        self.assertIn("orphan", err)

    def test_duplicate_identical_entries_refused(self):
        # even two IDENTICAL entries are refused — the rule is simpler stated once: a class
        # appears once, full stop, not "duplicates are fine as long as they agree".
        d, err = _parse_envelope(["orphan=contain", "orphan=contain"], True)
        self.assertIsNone(d)
        self.assertIsNotNone(err)
        self.assertIn("orphan", err)


# --- argparse wiring ---------------------------------------------------------------------------

class TestEnvelopeParserWiring(unittest.TestCase):
    def test_envelope_flag_defaults_none(self):
        args = build_parser().parse_args(["close"])
        self.assertIsNone(args.envelope)

    def test_envelope_flag_repeatable(self):
        args = build_parser().parse_args(
            ["close", "--respond", "--envelope", "orphan=contain",
             "--envelope", "ungoverned=alert"])
        self.assertEqual(args.envelope, ["orphan=contain", "ungoverned=alert"])

    def test_main_threads_envelope_into_cmd_close(self):
        with mock.patch("pacioli.cli.cmd_close", return_value=0) as m:
            rc = main(["close", "--respond", "--envelope", "orphan=contain"],
                     env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertEqual(rc, 0)
        self.assertEqual(m.call_args.kwargs.get("envelope"), ["orphan=contain"])

    def test_main_passes_envelope_none_by_default(self):
        with mock.patch("pacioli.cli.cmd_close", return_value=0) as m:
            main(["close"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertIsNone(m.call_args.kwargs.get("envelope"))


# --- cmd_close integration: usage errors --------------------------------------------------------

class TestCmdCloseEnvelopeUsageErrors(unittest.TestCase):
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
                           respond=kw.get("respond", False), envelope=kw.get("envelope"))
        return rc, o.getvalue(), e.getvalue()

    def test_envelope_without_respond_is_usage_error_exit_two(self):
        rc, out, err = self._run(envelope=["orphan=contain"], respond=False)
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        self.assertIn("--respond", err)
        self.assertEqual(out, "")  # close did NOT run — nothing rendered

    def test_bad_class_is_usage_error_exit_two_close_does_not_run(self):
        rc, out, err = self._run(envelope=["bogus=contain"], respond=True)
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        self.assertIn("bogus=contain", err)
        self.assertEqual(out, "")

    def test_bad_level_is_usage_error_exit_two_close_does_not_run(self):
        rc, out, err = self._run(envelope=["orphan=explode"], respond=True)
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        self.assertIn("orphan=explode", err)
        self.assertEqual(out, "")

    def test_duplicate_class_is_usage_error_exit_two_close_does_not_run(self):
        rc, out, err = self._run(envelope=["orphan=contain", "orphan=alert"], respond=True)
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        self.assertIn("orphan", err)
        self.assertEqual(out, "")

    def test_usage_error_opens_no_store(self):
        # A bad --envelope refuses BEFORE the store is even opened — no file should appear.
        rc, out, err = self._run(envelope=["bogus=contain"], respond=True)
        self.assertEqual(rc, 2)
        db_files = list(Path(self.dir.name).glob("*.db")) + list(Path(self.dir.name).glob("*.sqlite*"))
        self.assertEqual(db_files, [])

    def test_duplicate_class_usage_error_opens_no_store(self):
        rc, out, err = self._run(envelope=["orphan=contain", "orphan=alert"], respond=True)
        self.assertEqual(rc, 2)
        db_files = list(Path(self.dir.name).glob("*.db")) + list(Path(self.dir.name).glob("*.sqlite*"))
        self.assertEqual(db_files, [])


# --- cmd_close integration: escalation to CONTAIN + auto-seal ----------------------------------

class TestCmdCloseEnvelopeAutoSeal(unittest.TestCase):
    """An orphan-bearing fixture: floor is alert; --envelope orphan=contain escalates it to
    CONTAIN, which must actually seal the store."""

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
                           respond=kw.get("respond", True), envelope=kw.get("envelope"))
        return rc, o.getvalue(), e.getvalue()

    def _make_orphan(self):
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})  # no outcome

    def test_orphan_escalated_to_contain_seals_the_store(self):
        self._make_orphan()
        rc, out, err = self._run(envelope=["orphan=contain"])
        self.assertEqual(rc, 1, err)
        self.assertIn("CONTAIN", out)
        self.assertIn("SEALED", out)
        self.assertIn("sealed by response", out)
        # the seal line names the way back in — no urgency theatre, just the accounting register
        # pointing at the two commands that read/reverse it.
        self.assertIn("pacioli unseal --reason", out)
        self.assertIn("pacioli seal-status", out)

        # reopen the store and confirm it is ACTUALLY sealed, not just rendered as such
        store2 = open_store(self.env, "prod")
        state = store2.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["source"], "response")
        self.assertIn("orphan", state["reason"])
        self.assertIn("CONTAIN", state["reason"])
        self.assertIn("genesis..now", state["reason"])  # the period, named (default window)

    def test_json_mode_seal_block_shape_pinned(self):
        self._make_orphan()
        rc, out, err = self._run(envelope=["orphan=contain"], as_json=True)
        self.assertEqual(rc, 1, err)
        doc = json.loads(out)
        self.assertIn("seal", doc)
        self.assertEqual(set(doc["seal"]), {"sealed", "seq", "action"})
        self.assertTrue(doc["seal"]["sealed"])
        self.assertEqual(doc["seal"]["action"], "sealed by response")
        self.assertIsInstance(doc["seal"]["seq"], int)
        self.assertEqual(doc["response"]["response"], "contain")
        self.assertTrue(doc["response"]["seal_required"])

    def test_same_fixture_without_envelope_is_alert_floor_no_seal_write(self):
        self._make_orphan()
        rc, out, err = self._run(envelope=None)
        self.assertEqual(rc, 1, err)
        self.assertIn("[alert] orphan", out)
        self.assertNotIn("RESULT:      CONTAIN", out)
        self.assertNotIn("SEALED", out)
        self.assertNotIn("SEALED", out)

        store2 = open_store(self.env, "prod")
        state = store2.seal_state()
        self.assertFalse(state["sealed"])
        # only the genesis row -- zero seal_events writes on this path
        events = store2.seal_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "genesis")

    def test_clean_period_with_envelope_present_but_unreached_makes_no_seal_write(self):
        # A clean period: even with an envelope configured, if nothing reaches its floor there is
        # nothing to escalate -- orphan=contain never fires because there is no orphan finding.
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        rc, out, err = self._run(envelope=["orphan=contain"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("RESULT:      CONTAIN", out)  # scope_note always mentions "CONTAIN" in
        self.assertNotIn("SEALED", out)                # the abstract; the VERDICT line must not
        store2 = open_store(self.env, "prod")
        self.assertEqual(len(store2.seal_events()), 1)  # genesis only

    def test_already_sealed_store_still_appends_a_new_seal_event(self):
        store = open_store(self.env, "prod")
        store.seal("operator sealed earlier", source="operator")
        self._make_orphan()
        rc, out, err = self._run(envelope=["orphan=contain"])
        self.assertEqual(rc, 1, err)
        self.assertIn("SEALED", out)
        store2 = open_store(self.env, "prod")
        events = store2.seal_events()
        # genesis, operator seal, response seal = 3
        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1]["source"], "response")
        self.assertEqual(events[-1]["action"], "seal")

    def test_explicit_since_until_named_in_seal_reason(self):
        self._make_orphan()
        rc, out, err = self._run(envelope=["orphan=contain"],
                                 since="2026-07-01", until="2026-07-31")
        self.assertEqual(rc, 1, err)
        store2 = open_store(self.env, "prod")
        reason = store2.seal_state()["reason"]
        self.assertIn("2026-07-01", reason)
        self.assertIn("2026-07-31", reason)

    def test_multiple_contain_classes_all_named_in_reason(self):
        store = open_store(self.env, "prod")
        # an orphan: intent with no outcome at all
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})
        # a genuine `unconfirmed` finding: intent + an `unconfirmed` outcome (no answer — MAY
        # have posted) — mirrors close.py's own recorded_open/unconfirmed fixture (test_close.py
        # test_unconfirmed_act_blocks_a_clean_close_and_confesses) via the store, not a raw Receipt.
        intent2 = store.record_intent({"tool": "reconcile", "target": "prod",
                                       "doctype": "Payment Entry", "docname": "PE-9"})
        store.record_outcome(intent2, "unconfirmed", {"docstatus": None}, None)
        rc, out, err = self._run(envelope=["orphan=contain", "unconfirmed=contain"])
        self.assertEqual(rc, 1, err)
        self.assertIn("[contain] orphan", out)
        self.assertIn("[contain] unconfirmed", out)
        store2 = open_store(self.env, "prod")
        reason = store2.seal_state()["reason"]
        self.assertIn("orphan", reason)
        self.assertIn("unconfirmed", reason)
        # ALSO prove it via seal_events' own recorded reason, not just seal_state's derived read.
        events = store2.seal_events()
        self.assertIn("orphan", events[-1]["reason"])
        self.assertIn("unconfirmed", events[-1]["reason"])

    def test_seal_reason_is_truthful_not_escalated(self):
        # C1 (redteam fix wave): the permanent seal_events reason must never claim "escalated" —
        # true here too (an explicit --envelope IS a genuine operator escalation) but the wording
        # is shared with the chain_broken default-floor path, which is NOT an escalation, so the
        # shared phrasing must hold for both: neutral, truthful, names the class and period.
        self._make_orphan()
        rc, out, err = self._run(envelope=["orphan=contain"])
        self.assertEqual(rc, 1, err)
        store2 = open_store(self.env, "prod")
        reason = store2.seal_state()["reason"]
        self.assertNotIn("escalated", reason)
        self.assertIn("reached CONTAIN", reason)
        self.assertIn("orphan", reason)


# --- chain_broken: the marquee end-to-end path (C4, redteam fix wave) --------------------------

class TestCmdCloseChainBrokenAutoSeal(unittest.TestCase):
    """D1 (John ruled 2026-07-15): chain_broken fires on `verified is False` ONLY, at its
    unconditional default floor contain — reachable on a PLAIN `close --respond` with NO
    --envelope at all. This pins the shipped capability end-to-end against a real tampered
    receipt row on disk, not just a synthetic `build_response` dict (mirrors
    TestCmdCloseEnvelopeAutoSeal.test_orphan_escalated_to_contain_seals_the_store)."""

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
                           respond=kw.get("respond", True), envelope=kw.get("envelope"))
        return rc, o.getvalue(), e.getvalue()

    def test_tampered_receipt_reaches_contain_with_no_envelope_and_really_seals(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        # tamper a REAL receipt row on disk (an API user with store access rewriting history) —
        # mirrors test_store.py's TestVerify.test_tamper_detected.
        store._conn.execute("UPDATE receipts SET body = ? WHERE seq = 0", ('{"amount": 999}',))
        store._conn.commit()

        rc, out, err = self._run(envelope=None)  # NO --envelope at all — the whole point of D1
        self.assertEqual(rc, 1, err)
        self.assertIn("CONTAIN", out)
        self.assertIn("SEALED", out)
        self.assertIn("chain_broken", out)
        self.assertIn("sealed by response", out)

        # reopen and confirm a REAL sealed store, not just a rendered claim.
        store2 = open_store(self.env, "prod")
        state = store2.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["source"], "response")
        self.assertIn("chain_broken", state["reason"])
        self.assertNotIn("escalated", state["reason"])  # ties to C1 — never a lie about how it got here
        self.assertIn("reached CONTAIN", state["reason"])

    def test_json_mode_chain_broken_seal_block_shape(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        store._conn.execute("UPDATE receipts SET body = ? WHERE seq = 0", ('{"amount": 999}',))
        store._conn.commit()

        rc, out, err = self._run(envelope=None, as_json=True)
        self.assertEqual(rc, 1, err)
        doc = json.loads(out)
        self.assertEqual(doc["response"]["response"], "contain")
        self.assertTrue(doc["response"]["seal_required"])
        classes = {f["class"] for f in doc["response"]["findings"]}
        self.assertIn("chain_broken", classes)
        self.assertIn("seal", doc)
        self.assertTrue(doc["seal"]["sealed"])


# --- fail-closed: the seal WRITE itself raises --------------------------------------------------

class TestCmdCloseSealWriteFailure(unittest.TestCase):
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
                           respond=True, envelope=kw.get("envelope"))
        return rc, o.getvalue(), e.getvalue()

    def test_seal_write_raises_loud_stderr_exit_one_never_claims_sealed(self):
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})
        with mock.patch.object(BrokerStore, "seal", side_effect=RuntimeError("disk full")):
            rc, out, err = self._run(envelope=["orphan=contain"])
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertIn("seal", err.lower())
        self.assertIn("FAILED", err)
        self.assertIn("pacioli seal", err)
        # never report contain as handled -- no success seal block in the render
        self.assertNotIn("SEALED (seq", out)
        self.assertNotIn("sealed by response", out)

        # the store itself must NOT actually be sealed by this failed attempt (source="response")
        store2 = open_store(self.env, "prod")
        state = store2.seal_state()
        self.assertFalse(state["sealed"])

    def test_seal_write_raises_json_mode_no_seal_key(self):
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})
        with mock.patch.object(BrokerStore, "seal", side_effect=RuntimeError("disk full")):
            rc, out, err = self._run(envelope=["orphan=contain"], as_json=True)
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        doc = json.loads(out)
        self.assertNotIn("seal", doc)  # no seal block claimed when the write never landed
        self.assertEqual(doc["response"]["response"], "contain")  # the DECISION still renders


# --- regression pins: plain close / close --respond (no --envelope) are unchanged --------------

class TestRegressionNoEnvelope(unittest.TestCase):
    """Content-level pins (the store's real HMAC key and clock make exact byte-for-byte hashes
    non-reproducible across runs) proving `--envelope`'s absence changes NOTHING: no seal text
    anywhere, no `seal` JSON key, same exit codes/content as the pre-Task-4 TestCloseRespond
    assertions in test_close.py."""

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

    def test_plain_close_unaffected(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertIn("balanced", out.lower())
        self.assertNotIn("seal", out.lower())
        self.assertEqual(err, "")

    def test_respond_clean_period_unaffected(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        rc, out, err = self._run(respond=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("mixed_door", out)
        self.assertIn("record", out)
        self.assertNotIn("RESULT:      CONTAIN", out)
        self.assertNotIn("seal:", out)

    def test_respond_orphan_unaffected(self):
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})
        rc, out, err = self._run(respond=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("[alert] orphan", out)
        self.assertNotIn("RESULT:      CONTAIN", out)
        self.assertNotIn("seal:", out)

    def test_respond_json_shape_unaffected(self):
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "target": "prod", "docname": "ORPH-2"})
        rc, out, err = self._run(as_json=True, respond=True)
        self.assertEqual(rc, 1, err)
        doc = json.loads(out)
        self.assertEqual(set(doc), {"statement", "response"})  # no "seal" key leaked in
        self.assertEqual(doc["response"]["response"], "alert")


# --- cmd_close integration: --reconcile + --envelope reaching auto-seal on a RECONCILIATION-side
# class -------------------------------------------------------------------------------------------
#
# Every TestCmdCloseEnvelopeAutoSeal test above escalates a STATEMENT-side class (orphan,
# unconfirmed) -- the ones close.py alone can produce, no bench call needed. The reconciliation-side
# classes (ungoverned, second_generation, blind_read) share the exact same
# build_response -> seal_required -> store.seal() wiring in cmd_close, but were never proven
# end-to-end through the seal path itself (correctness redteam 2026-07-14, "Minors" triage). Fixture
# mirrors TestCloseReconcileCli (test_close.py) -- a fake bench transport, never real HTTP --
# redefined here so this module stays self-contained, matching that module's own stated convention.

_REG_COMPANY = ('[targets.prod]\nbase_url = "https://erp.example.com"\n'
                'api_key = "env:K"\napi_secret = "env:S"\ncompany = "Example Co"\ndefault = true\n')


def _routing_transport(routes, calls=None):
    """A fake ErpnextClient transport that answers by URL substring — the same shape as
    ``test_close``'s own ``_routing_transport``."""
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


class TestCmdCloseReconcileEnvelopeAutoSeal(unittest.TestCase):
    """``close --reconcile --respond --envelope ungoverned=contain`` reaching the auto-seal — the
    reconciliation-side counterpart to ``TestCmdCloseEnvelopeAutoSeal`` above."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(_REG_COMPANY)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, transport, **kw):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_close(self.env, target=None, since=kw.get("since", "2026-07-01"),
                           until=kw.get("until", "2026-07-31"),
                           expected_head=kw.get("expected_head"), as_json=kw.get("as_json", False),
                           reconcile=True, respond=True, envelope=kw.get("envelope"),
                           transport=transport)
        return rc, o.getvalue(), e.getvalue()

    def _ungoverned_routes(self):
        # A governed SI-1 (matches the receipt the caller records) plus an ungoverned SI-9 row —
        # the same shape as test_close.py's own ungoverned fixture.
        routes = dict(READY_RECON_ROUTES)
        routes["/api/resource/GL%20Entry"] = (200, {"data": [
            _gl_row(),  # governed: matches the SI-1 receipt below
            _gl_row(voucher_no="SI-9", account="Sales - EC", debit=0.0, credit=100.0),
        ]})
        return routes

    def test_ungoverned_escalated_to_contain_seals_the_store(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        rc, out, err = self._run(_routing_transport(self._ungoverned_routes()),
                                 envelope=["ungoverned=contain"])
        self.assertEqual(rc, 1, err)
        self.assertIn("[contain] ungoverned", out)
        self.assertIn("RESULT:      CONTAIN", out)
        self.assertIn("SEALED", out)
        self.assertIn("sealed by response", out)

        # reopen the store and confirm it is ACTUALLY sealed, not just rendered as such
        store2 = open_store(self.env, "prod")
        state = store2.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["source"], "response")
        self.assertIsNone(state["cause"])  # a genuine seal, not a fail-closed one
        self.assertIn("ungoverned", state["reason"])

    def test_json_mode_ungoverned_contain_seal_block_shape(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        rc, out, err = self._run(_routing_transport(self._ungoverned_routes()),
                                 envelope=["ungoverned=contain"], as_json=True)
        self.assertEqual(rc, 1, err)
        doc = json.loads(out)
        self.assertIn("seal", doc)
        self.assertEqual(set(doc["seal"]), {"sealed", "seq", "action"})
        self.assertTrue(doc["seal"]["sealed"])
        self.assertEqual(doc["seal"]["action"], "sealed by response")
        self.assertEqual(doc["response"]["response"], "contain")
        self.assertTrue(doc["response"]["seal_required"])

    def test_without_envelope_ungoverned_stays_at_record_floor_no_seal(self):
        # Regression pin: the SAME ungoverned fixture, with no --envelope escalation, must stay at
        # its mixed_door "record" floor (accounting-not-police) and never seal.
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        rc, out, err = self._run(_routing_transport(self._ungoverned_routes()), envelope=None)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("RESULT:      CONTAIN", out)
        self.assertNotIn("SEALED", out)
        store2 = open_store(self.env, "prod")
        state = store2.seal_state()
        self.assertFalse(state["sealed"])


if __name__ == "__main__":
    unittest.main()
