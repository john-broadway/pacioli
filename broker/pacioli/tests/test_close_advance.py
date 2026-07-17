# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""``close --advance`` — Half 3, Fork A1, Task 2
(docs/plans/2026-07-15-close-half3-close-record.md).

Task 1 shipped the store's close-record + attestation-gate machinery (``record_close``,
``attest``, ``close_cursor``, ``close_gate_state`` — see ``test_store_close_record.py``). This
module wires the CLI door: ``--advance`` writes the close record after the full render, defaults
``--since`` from the verified cursor when the caller omits it, and refuses the WRITE (never the
READ — Global constraint 5) when the PRE-EXISTING gate is already LATCHED: a workflow gap awaiting
attestation, or a close-record integrity failure.

Fixture mirrors ``TestCloseRespond``/``TestCmdCloseEnvelopeUsageErrors`` (test_close.py,
test_close_envelope.py) — a real on-disk store under a temp dir, ``redirect_stdout``/
``redirect_stderr``.
"""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pacioli.cli import main
from pacioli.runtime import open_store, state_db_path
from pacioli.store import BrokerStore, _close_record_hmac

_REG = ('[targets.prod]\nbase_url = "https://erp.example.com"\n'
        'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n')


class _CloseAdvanceFixture(unittest.TestCase):
    """Shared setUp/run/fixture helpers — same shape as TestCloseRespond."""

    def setUp(self):
        from pacioli.cli import cmd_close
        self._cmd_close = cmd_close
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
            rc = self._cmd_close(
                self.env, target=None, since=kw.get("since"), until=kw.get("until"),
                expected_head=kw.get("expected_head"), as_json=kw.get("as_json", False),
                reconcile=kw.get("reconcile", False), respond=kw.get("respond", False),
                envelope=kw.get("envelope"), advance=kw.get("advance", False),
            )
        return rc, o.getvalue(), e.getvalue()

    def _clean_close(self):
        """One committed act — a period that closes balanced with no findings."""
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        return store

    def _orphan_close(self):
        """One orphan (intent, no outcome) — floors at 'alert' unescalated."""
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})  # no outcome
        return store

    def _orphan_close_at(self, ts="2026-01-01T00:00:00Z"):
        """One orphan BACKDATED to ``ts`` — built with an explicit fixed-clock BrokerStore on the
        same db/key the CLI will open, so tests can use PAST ``--until`` bounds that still cover
        the act. Needed since the redteam wave outlawed the old future-``--until`` scaffolding
        (a future until poisons the cursor — the exact finding-1 bug the scaffolding relied on)."""
        from pacioli.runtime import load_or_create_seal_key
        d = Path(self.dir.name)
        key = load_or_create_seal_key(d / "seal.key")
        store = BrokerStore(sqlite3.connect(str(state_db_path(str(d), "prod"))), key,
                            now_iso=lambda: ts)
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})  # no outcome
        return store

    def _inject_legacy_close(self, *, period_until, seq=1, period_since=None, gapped=0):
        """Inject a HISTORICAL close row directly (valid hmac) — the shapes a pre-redteam-wave
        store could have written (an open-ended ``period_until=None``, or a future cursor) that
        the WRITE path now refuses but the read/derive path still tolerates."""
        from pacioli.runtime import load_or_create_seal_key
        d = Path(self.dir.name)
        key = load_or_create_seal_key(d / "seal.key")
        conn = sqlite3.connect(str(state_db_path(str(d), "prod")))
        BrokerStore(conn, key)  # ensure schema
        ts = "2026-01-01T00:00:00Z"
        mac = _close_record_hmac(key, seq, ts, "close", period_since, period_until, "h1",
                                  gapped, "", "close")
        conn.execute(
            "INSERT INTO close_records(seq, ts, action, period_since, period_until,"
            " attested_head, gapped, reason, source, hmac) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (seq, ts, "close", period_since, period_until, "h1", gapped, "", "close", mac),
        )
        conn.commit()


# --- (a) --advance without --respond is a usage error, exit 2 -----------------------------------

class TestAdvanceUsageErrors(_CloseAdvanceFixture):
    def test_advance_without_respond_is_usage_error_exit_two(self):
        self._clean_close()
        rc, out, err = self._run(advance=True, respond=False)
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        self.assertIn("--respond", err)
        self.assertEqual(out, "")  # close did NOT run — nothing rendered

    def test_advance_without_respond_opens_no_store_and_writes_no_row(self):
        store = self._clean_close()
        self._run(advance=True, respond=False)
        _, rows = store.close_records_snapshot()
        self.assertEqual(rows, [])

    def test_main_threads_advance_into_cmd_close(self):
        with mock.patch("pacioli.cli.cmd_close", return_value=0) as m:
            main(["close", "--respond", "--advance"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertTrue(m.call_args.kwargs.get("advance"))

    def test_main_passes_advance_false_by_default(self):
        with mock.patch("pacioli.cli.cmd_close", return_value=0) as m:
            main(["close"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertFalse(m.call_args.kwargs.get("advance"))


# --- (b) --since defaults from the verified cursor -----------------------------------------------

class TestAdvanceSinceDefault(_CloseAdvanceFixture):
    def test_first_ever_advance_defaults_since_to_genesis(self):
        self._clean_close()
        rc, out, err = self._run(respond=True, advance=True, as_json=True)
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertIsNone(doc["statement"]["period"]["since"])

    def test_second_advance_defaults_since_to_first_closes_until(self):
        self._clean_close()
        rc1, _, err1 = self._run(respond=True, advance=True, until="2026-07-01T23:59:59Z")
        self.assertEqual(rc1, 0, err1)
        rc2, out2, err2 = self._run(respond=True, advance=True, as_json=True)
        self.assertEqual(rc2, 0, err2)
        doc2 = json.loads(out2)
        self.assertEqual(doc2["statement"]["period"]["since"], "2026-07-01T23:59:59Z")

    def test_explicit_since_is_never_overridden_by_the_cursor(self):
        self._clean_close()
        self._run(respond=True, advance=True, until="2026-07-01T23:59:59Z")
        rc, out, err = self._run(respond=True, advance=True, as_json=True,
                                 since="2019-01-01T00:00:00Z")
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["statement"]["period"]["since"], "2019-01-01T00:00:00Z")

    def test_historical_open_ended_close_defaults_next_since_to_genesis_too(self):
        # A HISTORICAL open-ended row (period_until=None — writable before the redteam wave;
        # the write path now materializes a concrete until, see TestAdvanceMaterializedUntil) is
        # tolerated, NOT an integrity failure. The design says the NEXT --since must default to
        # None (genesis) on a None cursor — a None cursor from a real close is a different fact
        # from "no close ever happened", but both correctly default --since to None.
        self._clean_close()
        self._inject_legacy_close(period_until=None)

        store = open_store(self.env, "prod")
        gate = store.close_gate_state()
        self.assertIsNotNone(gate["last_close_seq"])  # a close DOES exist
        self.assertIsNone(gate["cursor"])              # ...and it is (historically) open-ended

        rc2, out2, err2 = self._run(respond=True, advance=True, as_json=True)
        self.assertEqual(rc2, 0, err2)
        doc2 = json.loads(out2)
        self.assertIsNone(doc2["statement"]["period"]["since"])


# --- (c) gate LATCHED for a workflow cause (gapped, unattested) ----------------------------------

class TestAdvanceGateLatchWorkflow(_CloseAdvanceFixture):
    def test_gapped_close_still_writes_with_gapped_flag_and_latches_next_advance(self):
        self._orphan_close()
        rc, out, err = self._run(respond=True, advance=True, envelope=["orphan=attestation_gate"])
        self.assertEqual(rc, 1, err)  # the response itself rose above record
        self.assertIn("attestation_gate", out.lower())
        store = open_store(self.env, "prod")
        state, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][6], 1)  # gapped column
        self.assertTrue(state["latched"])
        self.assertEqual(state["cause"], "gapped_awaiting_attestation")

    def test_next_advance_refused_names_the_seq_and_the_attest_command(self):
        self._orphan_close_at("2026-01-01T00:00:00Z")
        rc1, _, err1 = self._run(respond=True, advance=True,
                                 envelope=["orphan=attestation_gate"], until="2026-06-01T00:00:00Z")
        self.assertEqual(rc1, 1, err1)

        rc2, out2, err2 = self._run(respond=True, advance=True)
        self.assertEqual(rc2, 1, err2)
        self.assertIn("REFUSED", out2)
        self.assertIn("seq 1", out2)
        self.assertIn("2026-06-01T00:00:00Z", out2)  # the stuck period named
        self.assertIn("pacioli attest --target prod --reason", out2)

        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)  # the refusal wrote NOTHING

    def test_statement_still_renders_while_refused_reads_are_never_gated(self):
        self._orphan_close()
        self._run(respond=True, advance=True, envelope=["orphan=attestation_gate"])
        rc, out, err = self._run(respond=True, advance=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("period statement", out.lower())     # the statement rendered anyway
        self.assertIn("response for target", out.lower())  # the response rendered anyway too

    def test_attest_clears_the_gate_and_a_clean_next_advance_succeeds(self):
        # A BACKDATED orphan closed with a PAST --until that covers it, so the next advance's
        # cursor-defaulted --since puts the still-unresolved orphan OUTSIDE the next window —
        # proves the clear is real, not just "the same orphan happens not to trip the floor again"
        self._orphan_close_at("2026-01-01T00:00:00Z")
        rc1, _, err1 = self._run(respond=True, advance=True,
                                 envelope=["orphan=attestation_gate"], until="2026-06-01T00:00:00Z")
        self.assertEqual(rc1, 1, err1)

        store = open_store(self.env, "prod")
        store.attest("reviewed the gap, it's a known timing artifact", expected_seq=1)

        rc2, out2, err2 = self._run(respond=True, advance=True)
        self.assertEqual(rc2, 0, err2)
        self.assertIn("advance:     cursor recorded", out2)
        self.assertIn("gate:        OPEN", out2)
        state, rows = store.close_records_snapshot()
        self.assertFalse(state["latched"])
        self.assertEqual(len(rows), 3)  # the gapped close row + the attest row + the new close


# --- (d) gate LATCHED for an integrity cause (gap / unverifiable) -------------------------------

class TestAdvanceGateLatchIntegrity(_CloseAdvanceFixture):
    def test_unverifiable_close_record_history_refuses_advance_never_writes(self):
        self._clean_close()
        rc1, _, err1 = self._run(respond=True, advance=True)
        self.assertEqual(rc1, 0, err1)

        store = open_store(self.env, "prod")
        # a keyless-attacker-shaped edit: rewrite a row's content, leave its stored hmac stale —
        # mirrors test_close_envelope.py's chain_broken fixture on the receipt side.
        store._conn.execute("UPDATE close_records SET reason = ? WHERE seq = 1", ("tampered",))
        store._conn.commit()

        rc2, out2, err2 = self._run(respond=True, advance=True)
        self.assertEqual(rc2, 1, err2)
        self.assertIn("REFUSED", out2)
        self.assertIn("integrity failure", out2.lower())
        self.assertIn("unverifiable", out2.lower())
        _, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)  # no new row appended

    def test_gap_in_close_record_history_refuses_advance_never_writes(self):
        self._clean_close()
        rc1, _, err1 = self._run(respond=True, advance=True, until="2026-01-01T00:00:00Z")
        self.assertEqual(rc1, 0, err1)
        rc2, _, err2 = self._run(respond=True, advance=True, until="2026-02-01T00:00:00Z")
        self.assertEqual(rc2, 0, err2)

        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 2)
        store._conn.execute("DELETE FROM close_records WHERE seq = 1")
        store._conn.commit()

        rc3, out3, err3 = self._run(respond=True, advance=True)
        self.assertEqual(rc3, 1, err3)
        self.assertIn("REFUSED", out3)
        self.assertIn("integrity failure", out3.lower())
        self.assertIn("gap", out3.lower())
        _, rows2 = store.close_records_snapshot()
        self.assertEqual(len(rows2), 1)  # still just the surviving row


# --- (e) happy-path write: correct fields, gapped writes anyway, independent of auto-seal --------

class TestAdvanceHappyPathWrite(_CloseAdvanceFixture):
    def test_clean_close_writes_an_ungapped_record_with_correct_fields(self):
        self._clean_close()
        rc, out, err = self._run(respond=True, advance=True, until="2026-07-01T23:59:59Z")
        self.assertEqual(rc, 0, err)
        store = open_store(self.env, "prod")
        state, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)
        seq, ts, action, since, until, head, gapped, reason, source, mac = rows[0]
        self.assertEqual(action, "close")
        self.assertIsNone(since)                          # genesis — first-ever close
        self.assertEqual(until, "2026-07-01T23:59:59Z")    # the effective period_until
        self.assertIsNotNone(head)                         # the live chain head at close time
        self.assertEqual(gapped, 0)
        self.assertEqual(source, "close")
        self.assertFalse(state["latched"])
        self.assertIn("advance:     cursor recorded", out)
        self.assertIn("gate:        OPEN", out)

    def test_gapped_close_still_writes_gapped_flag_set(self):
        self._orphan_close()
        rc, out, err = self._run(respond=True, advance=True, envelope=["orphan=attestation_gate"])
        self.assertEqual(rc, 1, err)
        store = open_store(self.env, "prod")
        state, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][6], 1)  # gapped=1 — THIS close recorded itself despite the gap
        self.assertIn("gate:        LATCHED", out)  # the write confesses it latches the NEXT one

    def test_auto_seal_and_close_record_write_are_independent_mechanisms(self):
        # --envelope orphan=contain reaches CONTAIN: seal_required=True, auto-seal fires. The
        # close record must STILL land this same run — seal stops the pen, the gate stops the
        # page-turn, and neither mechanism silently suppresses the other.
        self._orphan_close()
        rc, out, err = self._run(respond=True, advance=True, envelope=["orphan=contain"])
        self.assertEqual(rc, 1, err)
        self.assertIn("SEALED", out)
        self.assertIn("advance:     cursor recorded", out)
        store = open_store(self.env, "prod")
        state, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)              # the write landed alongside the auto-seal
        self.assertEqual(rows[0][6], 1)              # gapped — contain implies gate_required too
        self.assertTrue(store.seal_state()["sealed"])


# --- (f) JSON mode: shape + absence-without-the-flag ---------------------------------------------

class TestAdvanceJsonMode(_CloseAdvanceFixture):
    def test_json_written_shape(self):
        self._clean_close()
        rc, out, err = self._run(respond=True, advance=True, as_json=True,
                                 until="2026-07-01T23:59:59Z")
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        adv = doc["advance"]
        self.assertTrue(adv["written"])
        self.assertIsNone(adv["cause"])
        self.assertEqual(adv["period_until"], "2026-07-01T23:59:59Z")
        self.assertIsInstance(adv["seq"], int)
        self.assertEqual(adv["gapped"], False)
        # Redteam finding 6: the gate verdict comes from record_close's RETURNED fresh state,
        # not the input gapped flag -- the json carries it explicitly.
        self.assertEqual(adv["gate_latched"], False)

    def test_json_refused_shape(self):
        self._orphan_close()
        self._run(respond=True, advance=True, envelope=["orphan=attestation_gate"])
        rc, out, err = self._run(respond=True, advance=True, as_json=True)
        self.assertEqual(rc, 1, err)
        doc = json.loads(out)
        adv = doc["advance"]
        self.assertFalse(adv["written"])
        self.assertEqual(adv["cause"], "gapped_awaiting_attestation")
        self.assertIsNone(adv["seq"])

    def test_json_mode_has_no_advance_key_without_the_flag(self):
        self._clean_close()
        rc, out, err = self._run(respond=True, as_json=True)
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertNotIn("advance", doc)


# --- byte-identical without --advance (Global constraint 6) -------------------------------------

class TestAdvanceCompareAndAppend(_CloseAdvanceFixture):
    """The advance path passes its PLANNED ``last_close_seq`` (from the one gate_state read it
    already does) into ``record_close(expected_last_close_seq=...)`` -- compare-and-append -- and
    renders a stale-cursor refusal as exit 1 with a re-run instruction (the period math must be
    redone against the moved cursor)."""

    def test_advance_passes_the_planned_last_close_seq_through(self):
        from pacioli.store import BrokerStore
        self._clean_close()
        real = BrokerStore.record_close
        seen = []

        def spy(self, **kw):
            seen.append(kw.get("expected_last_close_seq", "MISSING"))
            return real(self, **kw)

        # First advance: no close record exists yet -- the planned seq is None.
        with mock.patch.object(BrokerStore, "record_close", spy):
            rc, out, err = self._run(respond=True, advance=True)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [None])

        # Second advance: the close just written is seq 1 -- the planned seq threads through.
        seen.clear()
        with mock.patch.object(BrokerStore, "record_close", spy):
            rc, out, err = self._run(respond=True, advance=True)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [1])

    def test_stale_cursor_refusal_is_exit_one_with_rerun_instruction(self):
        from pacioli.store import BrokerStore, CloseRecordStaleError
        self._clean_close()
        with mock.patch.object(
            BrokerStore, "record_close",
            side_effect=CloseRecordStaleError("the close cursor moved"),
        ):
            rc, out, err = self._run(respond=True, advance=True)
        self.assertEqual(rc, 1)  # refused write -> exit 1, same contract as the latch refusal
        self.assertIn("REFUSED", out)
        self.assertIn("re-run", out)  # the operator's fix: redo the period math and try again

    def test_stale_cursor_refusal_json_shape(self):
        from pacioli.store import BrokerStore, CloseRecordStaleError
        self._clean_close()
        with mock.patch.object(
            BrokerStore, "record_close",
            side_effect=CloseRecordStaleError("the close cursor moved"),
        ):
            rc, out, err = self._run(respond=True, advance=True, as_json=True)
        self.assertEqual(rc, 1)
        doc = json.loads(out)
        self.assertFalse(doc["advance"]["written"])
        self.assertEqual(doc["advance"]["cause"], "stale_cursor")


class TestAdvanceFutureUntil(_CloseAdvanceFixture):
    """Redteam finding 1 (CRITICAL): a future --until permanently poisons the cursor -- every
    later default-since advance silently sees ZERO acts (a real orphan invisible, gapped=False,
    rc=0, forever). The advance path now refuses a future effective until against the STORE's
    own clock; plain close is untouched."""

    def test_future_until_refused_exit_one_no_row(self):
        self._clean_close()
        rc, out, err = self._run(respond=True, advance=True, until="2099-01-01T00:00:00Z")
        self.assertEqual(rc, 1, err)
        self.assertIn("REFUSED", out)
        self.assertIn("has not happened yet", out)
        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        self.assertEqual(rows, [])  # the poison never landed

    def test_future_until_json_cause(self):
        self._clean_close()
        rc, out, err = self._run(respond=True, advance=True, until="2099-01-01T00:00:00Z",
                                 as_json=True)
        self.assertEqual(rc, 1, err)
        doc = json.loads(out)
        self.assertFalse(doc["advance"]["written"])
        self.assertEqual(doc["advance"]["cause"], "future_until")

    def test_plain_close_with_future_until_is_untouched(self):
        # The fix is advance-path ONLY: a stateless close of a window reaching into the future
        # writes nothing and stays byte-compatible (constraint 6).
        self._clean_close()
        rc, out, err = self._run(until="2099-01-01T00:00:00Z")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("REFUSED", out)
        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        self.assertEqual(rows, [])

    def test_legacy_future_cursor_is_refused_loudly_not_silently_empty(self):
        # The un-poisoning: a store already carrying a future cursor (written before this wave)
        # must never again yield the silent zero-act rc=0 close the finding reproduced -- the
        # cursor-defaulted since is ahead of the materialized until, and that now refuses loudly.
        self._orphan_close()  # a REAL act the poisoned window would have hidden
        self._inject_legacy_close(period_until="2099-01-01T00:00:00Z")
        rc, out, err = self._run(respond=True, advance=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("REFUSED", out)
        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)  # only the legacy row -- nothing new written


class TestAdvanceReversedWindow(_CloseAdvanceFixture):
    """Redteam finding 2 (HIGH): since > until was accepted, rendered trivially 'balanced' (the
    window excludes everything), and wrote permanently. Two refusal shapes: caller-supplied
    reversed bounds are a usage error (exit 2, no I/O); a cursor-defaulted since ahead of the
    requested until is a state-dependent refusal (exit 1, rendered)."""

    def test_explicit_reversed_bounds_usage_error_exit_two(self):
        self._clean_close()
        rc, out, err = self._run(respond=True, advance=True,
                                 since="2026-02-01T00:00:00Z", until="2026-01-01T00:00:00Z")
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        self.assertEqual(out, "")  # refused before any close ran
        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        self.assertEqual(rows, [])

    def test_cursor_ahead_of_requested_until_refused_exit_one(self):
        self._clean_close()
        rc1, _, err1 = self._run(respond=True, advance=True, until="2026-06-01T00:00:00Z")
        self.assertEqual(rc1, 0, err1)
        # cursor is now 2026-06-01; ask to close a period ending BEFORE it
        rc2, out2, err2 = self._run(respond=True, advance=True, until="2026-03-01T00:00:00Z")
        self.assertEqual(rc2, 1, err2)
        self.assertIn("REFUSED", out2)
        self.assertIn("already", out2)  # the cursor is already past the requested until
        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)  # nothing new written


class TestAdvanceMaterializedUntil(_CloseAdvanceFixture):
    """Redteam finding 3 (HIGH): two consecutive no-until advances both closed genesis..now --
    full overlap, double-count. Fix by materialization: when --until is absent on --advance, the
    effective until is the STORE's now at close time, recorded concretely (a concrete cursor
    always). The statement window uses the same materialized bound, so the examined window and
    the recorded period are identical."""

    def test_no_until_advance_records_a_concrete_cursor(self):
        self._clean_close()
        rc, out, err = self._run(respond=True, advance=True)
        self.assertEqual(rc, 0, err)
        store = open_store(self.env, "prod")
        state, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)
        recorded_until = rows[0][4]
        self.assertIsNotNone(recorded_until)                    # concrete, never open-ended
        self.assertRegex(recorded_until, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(state["cursor"], recorded_until)

    def test_two_no_until_advances_do_not_overlap(self):
        # The exact double-count reproduction, now closed: the second period STARTS where the
        # first ended -- no shared genesis..now overlap.
        self._clean_close()
        rc1, _, err1 = self._run(respond=True, advance=True)
        self.assertEqual(rc1, 0, err1)
        rc2, _, err2 = self._run(respond=True, advance=True)
        self.assertEqual(rc2, 0, err2)
        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 2)
        first_until, second_since = rows[0][4], rows[1][3]
        self.assertEqual(second_since, first_until)  # contiguous, not overlapping
        self.assertIsNotNone(rows[1][4])

    def test_statement_window_matches_the_recorded_period(self):
        self._clean_close()
        rc, out, err = self._run(respond=True, advance=True, as_json=True)
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertIsNotNone(doc["advance"]["period_until"])
        self.assertEqual(doc["statement"]["period"]["until"], doc["advance"]["period_until"])


class TestAdvanceSealConfession(_CloseAdvanceFixture):
    """Redteam finding 7 (model-fidelity F2): a pre-existing SEAL was not confessed by --advance
    -- the page turned silently while the pen was confiscated. Render-only fix: one confession
    line; no mechanism coupling (the write still lands -- seal stops the pen, not the page-turn)."""

    def test_pre_existing_seal_confessed_on_advance(self):
        store = self._clean_close()
        store.seal("containing for the test")
        rc, out, err = self._run(respond=True, advance=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("SEALED", out)
        self.assertIn("does not clear containment", out)
        _, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)  # the write still landed -- render honesty only

    def test_no_seal_no_confession_line(self):
        self._clean_close()
        rc, out, err = self._run(respond=True, advance=True)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("does not clear containment", out)

    def test_json_carries_broker_sealed(self):
        store = self._clean_close()
        store.seal("containing for the test")
        rc, out, err = self._run(respond=True, advance=True, as_json=True)
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertTrue(doc["advance"]["broker_sealed"])


class TestRenderAdvanceUsesFreshState(unittest.TestCase):
    """Redteam finding 6 (honesty F2): the written-shape gate line was rendered from the INPUT
    gapped flag, not from the fresh state record_close actually returned. ``_render_advance`` is
    pure -- prove the source of truth directly."""

    _BASE = {"written": True, "cause": None, "reason": "", "period_since": None,
             "period_until": "2026-01-31T00:00:00Z", "cursor": "2026-01-31T00:00:00Z",
             "gapped": False, "seq": 1, "broker_sealed": False}

    def test_gate_line_latched_when_fresh_state_latched(self):
        from pacioli.cli import _render_advance
        result = dict(self._BASE, gate_latched=True,
                      gate_reason="close at seq 1 closed over a gap (gate_required) and has "
                                  "not been attested")
        out = _render_advance(result, "prod")
        self.assertIn("LATCHED", out)
        self.assertNotIn("gate:        OPEN", out)

    def test_gate_line_open_when_fresh_state_open(self):
        from pacioli.cli import _render_advance
        result = dict(self._BASE, gate_latched=False, gate_reason="")
        out = _render_advance(result, "prod")
        self.assertIn("gate:        OPEN", out)
        self.assertNotIn("LATCHED", out)


class TestGoldenByteIdentical(_CloseAdvanceFixture):
    """Redteam finding 10: TWO LITERAL golden strings -- full-output equality for plain ``close``
    and ``close --respond`` on a fully fixed store (fixed seal key, fixed clock, one committed
    act), alongside TestByteIdenticalWithoutAdvance's vocabulary sweep. Captured from the
    pre-wave code (commit 5f1752c) -- these paths must never move again without a deliberate,
    reviewed golden update."""

    CLOCK = "2026-07-01T00:00:00Z"
    _PLAIN_GOLDEN = (
        "PACIOLI CLOSE — period statement for target 'prod'\n"
        "  period:      genesis .. now\n"
        "  governed acts: 1  (committed 1, recorded-open 0, orphan 0)\n"
        "  by tool:     submit=1\n"
        "  by doctype:  Sales Invoice=1\n"
        "  chain:       2 receipts, verifies; head "
        "6c537cd083d3f1f50681d7b77995cee06a024c1df8a00885a57c1bd72de109ee\n"
        "  RESULT:      balanced — every governed act in this period is accounted for on a "
        "verified chain.\n"
        "  scope:       This attests ONLY to activity that passed through Pacioli on target "
        "'prod'. It is NOT a statement about the whole ERPNext ledger — movement through any "
        "other door (desk users, other integrations, direct database access) is invisible here "
        "until the Reconciliation (forthcoming). Verify this statement against the off-box "
        "anchor head.\n"
    )
    _RESPOND_GOLDEN = _PLAIN_GOLDEN + (
        "PACIOLI CLOSE — response for target 'prod'\n"
        "  posture:     mixed_door  (ungoverned movement is recorded, not alerted)\n"
        "  findings:    0\n"
        "  RESULT:      record — every finding is accounted for; nothing rises to a reaction.\n"
        "  scope:       This is Pacioli's response layer: it applies the operator's configured "
        "posture and response envelope to the findings from the period Statement and "
        "Reconciliation. It renders NO verdict on another party — 'ungoverned' movement is a "
        "legitimate, normal part of real books and is RECORDED, not accused; it is elevated to "
        "an alert ONLY under a sole-door posture the operator explicitly declared. CONTAIN is "
        "never a default for a working ledger honestly recording a known uncertainty — every "
        "such finding, including Pacioli's own 'orphan'/'unconfirmed', stays at alert or below "
        "unless the operator explicitly escalates it. The sole exception is 'chain_broken': the "
        "attestation apparatus itself is provably broken (the HMAC chain cannot verify itself, "
        "so no record in it can be trusted) — a fundamentally different thing from the apparatus "
        "working and confessing an open item, and the accounting system halts on its own "
        "unprovable books rather than a verdict on a party (invariant 1, revised 2026-07-15). "
        "Otherwise, the response is the autonomy envelope the operator set in advance, not a "
        "per-gap judgment.\n"
    )

    def _fixed_store(self):
        d = Path(self.dir.name)
        keyfile = d / "seal.key"
        keyfile.write_bytes(b"g" * 32)
        keyfile.chmod(0o600)
        store = BrokerStore(sqlite3.connect(str(state_db_path(str(d), "prod"))), b"g" * 32,
                            now_iso=lambda: self.CLOCK)
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        return store

    def test_plain_close_output_is_byte_identical_to_the_golden(self):
        self._fixed_store()
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, self._PLAIN_GOLDEN)

    def test_close_respond_output_is_byte_identical_to_the_golden(self):
        self._fixed_store()
        rc, out, err = self._run(respond=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, self._RESPOND_GOLDEN)


class TestByteIdenticalWithoutAdvance(_CloseAdvanceFixture):
    """Pinned structurally, not by literal diff (there is no pre-Task-2 binary to diff against):
    no close-record/cursor/gate/advance vocabulary ever appears in output that didn't ask for
    --advance, and the store never gains a close_records row. Exit codes for the existing paths
    are unchanged."""

    _ADVANCE_VOCAB = ("advance:", "cursor recorded", "gate:", "LATCHED", "close-record",
                      "close_record")

    def test_plain_close_stays_silent_on_advance_vocabulary_and_writes_no_row(self):
        store = self._clean_close()
        rc, out, err = self._run()  # no --respond, no --advance — the original v1 shape
        self.assertEqual(rc, 0, err)
        self.assertIn("balanced", out.lower())  # still the same rendered statement
        for word in self._ADVANCE_VOCAB:
            self.assertNotIn(word, out)
        _, rows = store.close_records_snapshot()
        self.assertEqual(rows, [])

    def test_close_respond_without_advance_stays_silent_and_writes_no_row(self):
        store = self._orphan_close()
        rc, out, err = self._run(respond=True)  # --respond WITHOUT --advance
        self.assertEqual(rc, 1, err)  # unchanged exit contract (orphan floors at alert)
        self.assertIn("[alert] orphan", out)
        for word in self._ADVANCE_VOCAB:
            self.assertNotIn(word, out)
        _, rows = store.close_records_snapshot()
        self.assertEqual(rows, [])

    def test_close_reconcile_respond_without_advance_still_writes_no_row(self):
        store = self._clean_close()
        rc, out, err = self._run(respond=True)
        self.assertEqual(rc, 0, err)
        for word in self._ADVANCE_VOCAB:
            self.assertNotIn(word, out)
        _, rows = store.close_records_snapshot()
        self.assertEqual(rows, [])

    def test_exit_codes_for_existing_paths_are_unchanged(self):
        self._clean_close()
        rc, _, _ = self._run()
        self.assertEqual(rc, 0)

        store2 = self._orphan_close()  # same target — adds an orphan on top of the clean act
        rc2, out2, _ = self._run()
        self.assertEqual(rc2, 1)
        self.assertIn("NOT balanced", out2)
        _, rows = store2.close_records_snapshot()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
