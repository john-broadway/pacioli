# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""`pacioli attest` / `pacioli close-status` (Half 3, Fork A1, Task 3,
docs/plans/2026-07-15-close-half3-close-record.md).

Task 1 gave `BrokerStore` the close-record + attestation-gate machinery (`record_close`,
`attest`, `close_gate_state`, `close_records_snapshot`). Task 2 wired `close --advance`, the write
side. This module is the remaining two operator-facing doors:

  * `pacioli attest --target NAME --reason <why>` — the ceremony that clears a gapped close,
    mirroring `pacioli unseal`'s shape exactly (required `--reason`, `source="operator"` always,
    append-only). Refuses (exit 2) when there is nothing gapped currently pending — including the
    case where the gate is latched for an INTEGRITY reason (a seq gap, an unverifiable hmac):
    attesting cannot repair a corrupt history, so the render must say so and never suggest it can.
  * `pacioli close-status [--target NAME] [--json]` — read-only: renders the gate/cursor state and
    a history tail. Global constraint 5 (reads never gated) means this must work, and render
    honestly, in EVERY state — including a keyless open, which `close_gate_state()` itself already
    reports as `latched=True, cause="keyless"` rather than crashing.

Fixture mirrors `test_close_advance.py`'s `_CloseAdvanceFixture` (real on-disk store, temp dir,
`redirect_stdout`/`redirect_stderr`) since attest/close-status need to interoperate with real
`close --advance` runs to manufacture a gapped gate.
"""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pacioli.cli import build_parser, cmd_attest, cmd_close, cmd_close_status, main
from pacioli.runtime import open_store
from pacioli.store import BrokerStore

_REG = ('[targets.prod]\nbase_url = "https://erp.example.com"\n'
        'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n')


class TestAttestParserWiring(unittest.TestCase):
    def test_attest_requires_reason(self):
        with self.assertRaises(SystemExit) as cm:
            build_parser().parse_args(["attest"])
        self.assertEqual(cm.exception.code, 2)

    def test_attest_parses_with_reason_and_target(self):
        args = build_parser().parse_args(["attest", "--reason", "reviewed", "--target", "x"])
        self.assertEqual(args.reason, "reviewed")
        self.assertEqual(args.target, "x")

    def test_attest_target_defaults_to_none(self):
        args = build_parser().parse_args(["attest", "--reason", "reviewed"])
        self.assertIsNone(args.target)

    def test_main_threads_attest(self):
        with mock.patch("pacioli.cli.cmd_attest", return_value=0) as m:
            rc = main(["attest", "--reason", "why"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertEqual(rc, 0)
        m.assert_called_once()
        self.assertEqual(m.call_args.args[1], "why")


class TestCloseStatusParserWiring(unittest.TestCase):
    def test_close_status_defaults(self):
        args = build_parser().parse_args(["close-status"])
        self.assertIsNone(args.target)
        self.assertFalse(args.as_json)

    def test_close_status_json_flag(self):
        args = build_parser().parse_args(["close-status", "--json"])
        self.assertTrue(args.as_json)

    def test_main_threads_close_status(self):
        with mock.patch("pacioli.cli.cmd_close_status", return_value=0) as m:
            rc = main(["close-status"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertEqual(rc, 0)
        m.assert_called_once()


class _Fixture(unittest.TestCase):
    """Shared setUp/teardown + thin wrappers around cmd_close/cmd_attest/cmd_close_status,
    mirroring `_CloseAdvanceFixture` (test_close_advance.py)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(_REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _close(self, **kw):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_close(
                self.env, target=None, since=kw.get("since"), until=kw.get("until"),
                expected_head=kw.get("expected_head"), as_json=kw.get("as_json", False),
                reconcile=kw.get("reconcile", False), respond=kw.get("respond", False),
                envelope=kw.get("envelope"), advance=kw.get("advance", False),
            )
        return rc, o.getvalue(), e.getvalue()

    def _attest(self, reason="reviewed the gap, it's a known timing artifact", target=None):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_attest(self.env, reason, target)
        return rc, o.getvalue(), e.getvalue()

    def _status(self, target=None, as_json=False):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_close_status(self.env, target, as_json)
        return rc, o.getvalue(), e.getvalue()

    def _clean_close(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        return store

    def _orphan_close(self):
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})  # no outcome
        return store

    def _orphan_close_at(self, ts="2026-01-01T00:00:00Z"):
        """One orphan BACKDATED to ``ts`` — a fixed-clock BrokerStore on the same db/key the CLI
        opens, so a PAST ``--until`` can still cover the act (the redteam wave outlawed the old
        future-``--until`` scaffolding: a future until poisons the cursor, finding 1)."""
        from pacioli.runtime import load_or_create_seal_key, state_db_path
        d = Path(self.dir.name)
        key = load_or_create_seal_key(d / "seal.key")
        store = BrokerStore(sqlite3.connect(str(state_db_path(str(d), "prod"))), key,
                            now_iso=lambda: ts)
        store.record_intent({"tool": "submit", "target": "prod",
                             "doctype": "Sales Invoice", "docname": "ORPH-1"})  # no outcome
        return store

    def _gapped(self, until="2026-06-01T00:00:00Z"):
        """First-ever advance, gapped by an unresolved (backdated) orphan — the shared setup
        nearly every attest test needs. Mirrors test_close_advance.py's own gapped-close recipe."""
        self._orphan_close_at("2026-01-01T00:00:00Z")
        rc, out, err = self._close(respond=True, advance=True,
                                   envelope=["orphan=attestation_gate"], until=until)
        self.assertEqual(rc, 1, err)
        return rc, out, err

    def _inject_legacy_open_ended_close(self):
        """A HISTORICAL open-ended close row (period_until=None, valid hmac) — writable before
        the redteam wave (the write path now materializes a concrete until); tolerated history."""
        from pacioli.runtime import load_or_create_seal_key, state_db_path
        from pacioli.store import _close_record_hmac
        d = Path(self.dir.name)
        key = load_or_create_seal_key(d / "seal.key")
        conn = sqlite3.connect(str(state_db_path(str(d), "prod")))
        BrokerStore(conn, key)  # ensure schema
        ts = "2026-01-01T00:00:00Z"
        mac = _close_record_hmac(key, 1, ts, "close", None, None, "h1", 0, "", "close")
        conn.execute(
            "INSERT INTO close_records(seq, ts, action, period_since, period_until,"
            " attested_head, gapped, reason, source, hmac) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (1, ts, "close", None, None, "h1", 0, "", "close", mac),
        )
        conn.commit()


# --- attest: happy path -----------------------------------------------------------------------

class TestAttestHappyPath(_Fixture):
    def test_attest_clears_gate_names_seq_and_period_exit_zero(self):
        self._gapped(until="2026-06-01T00:00:00Z")
        rc, out, err = self._attest(reason="reviewed the gap, it's a known timing artifact")
        self.assertEqual(rc, 0, err)
        self.assertIn("seq 1", out)
        self.assertIn("2026-06-01T00:00:00Z", out)
        self.assertIn("OPEN", out)

    def test_attest_actually_opens_the_gate(self):
        self._gapped()
        self._attest()
        store = open_store(self.env, "prod")
        self.assertFalse(store.close_gate_state()["latched"])

    def test_attest_recorded_with_source_operator_and_reason(self):
        self._gapped()
        self._attest(reason="known timing artifact, reviewed")
        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        last = rows[-1]
        self.assertEqual(last[2], "attest")
        self.assertEqual(last[7], "known timing artifact, reviewed")  # reason column
        self.assertEqual(last[8], "operator")                          # source column

    def test_attest_reason_appears_in_render(self):
        self._gapped()
        rc, out, err = self._attest(reason="known timing artifact, reviewed")
        self.assertEqual(rc, 0, err)
        self.assertIn("known timing artifact, reviewed", out)


# --- attest: nothing pending refuses ----------------------------------------------------------

class TestAttestNothingPendingRefuses(_Fixture):
    def test_attest_on_fresh_store_refuses_exit_two(self):
        rc, out, err = self._attest()
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        self.assertEqual(rows, [])  # nothing appended

    def test_attest_after_clean_close_refuses_exit_two(self):
        self._clean_close()
        rc1, _, err1 = self._close(respond=True, advance=True)
        self.assertEqual(rc1, 0, err1)
        rc, out, err = self._attest()
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)  # only the close row — attest wrote nothing

    def test_attest_twice_in_a_row_refuses_the_second_time(self):
        self._gapped()
        rc1, _, err1 = self._attest()
        self.assertEqual(rc1, 0, err1)
        rc2, out2, err2 = self._attest(reason="nothing left")
        self.assertEqual(rc2, 2)
        self.assertIn("error:", err2)


# --- attest: cannot fix an integrity failure --------------------------------------------------

class TestAttestCannotFixIntegrity(_Fixture):
    def test_attest_refuses_on_seq_gap_names_it_no_repair_claim(self):
        self._clean_close()
        rc1, _, err1 = self._close(respond=True, advance=True, until="2026-01-01T00:00:00Z")
        self.assertEqual(rc1, 0, err1)
        rc2, _, err2 = self._close(respond=True, advance=True, until="2026-02-01T00:00:00Z")
        self.assertEqual(rc2, 0, err2)

        store = open_store(self.env, "prod")
        store._conn.execute("DELETE FROM close_records WHERE seq = 1")
        store._conn.commit()

        rc, out, err = self._attest()
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        self.assertIn("gap", err.lower())
        self.assertNotIn("clears the gap", err)  # never claims attest can repair this
        _, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)  # nothing appended

    def test_attest_refuses_on_unverifiable_hmac_names_it_no_repair_claim(self):
        self._clean_close()
        rc1, _, err1 = self._close(respond=True, advance=True)
        self.assertEqual(rc1, 0, err1)

        store = open_store(self.env, "prod")
        store._conn.execute("UPDATE close_records SET reason = ? WHERE seq = 1", ("tampered",))
        store._conn.commit()

        rc, out, err = self._attest()
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        self.assertIn("unverifiable", err.lower())
        self.assertIn("cannot repair", err.lower())
        _, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)


# --- close-status: every state renders honestly, never crashes ---------------------------------

class TestCloseStatusGenesis(_Fixture):
    def test_genesis_status_open_exit_zero(self):
        rc, out, err = self._status()
        self.assertEqual(rc, 0, err)
        self.assertIn("OPEN", out)
        self.assertIn("no period ever closed", out.lower())
        self.assertIn("history: 0", out.lower())


class TestCloseStatusOpenWithCursor(_Fixture):
    def test_open_with_real_cursor_exit_zero(self):
        self._clean_close()
        rc1, _, err1 = self._close(respond=True, advance=True, until="2026-06-30T23:59:59Z")
        self.assertEqual(rc1, 0, err1)
        rc, out, err = self._status()
        self.assertEqual(rc, 0, err)
        self.assertIn("OPEN", out)
        self.assertIn("2026-06-30T23:59:59Z", out)
        self.assertIn("history: 1", out.lower())


class TestCloseStatusOpenEnded(_Fixture):
    def test_historical_open_ended_close_names_it_explicitly(self):
        # Open-ended rows are no longer WRITABLE (the redteam wave materializes a concrete until
        # on --advance); the "open-ended" wording remains for HISTORICAL rows only.
        self._clean_close()
        self._inject_legacy_open_ended_close()
        rc, out, err = self._status()
        self.assertEqual(rc, 0, err)
        self.assertIn("open-ended", out.lower())


class TestCloseStatusWorkflowLatched(_Fixture):
    def test_workflow_latched_exit_two_names_attest_command(self):
        self._gapped(until="2026-06-01T00:00:00Z")
        rc, out, err = self._status()
        self.assertEqual(rc, 2, err)
        self.assertIn("LATCHED", out)
        self.assertIn("gapped_awaiting_attestation", out)
        self.assertIn("2026-06-01T00:00:00Z", out)
        self.assertIn("pacioli attest --target prod --reason", out)

    def test_workflow_latched_status_never_crashes_and_history_shows(self):
        self._gapped()
        rc, out, err = self._status()
        self.assertEqual(rc, 2, err)
        self.assertIn("history: 1", out.lower())


class TestCloseStatusIntegrityLatched(_Fixture):
    def test_gap_latched_exit_two_no_attest_suggestion(self):
        self._clean_close()
        self._close(respond=True, advance=True, until="2026-01-01T00:00:00Z")
        self._close(respond=True, advance=True, until="2026-02-01T00:00:00Z")
        store = open_store(self.env, "prod")
        store._conn.execute("DELETE FROM close_records WHERE seq = 1")
        store._conn.commit()

        rc, out, err = self._status()
        self.assertEqual(rc, 2, err)
        self.assertIn("LATCHED", out)
        self.assertIn("gap", out.lower())
        self.assertNotIn("pacioli attest", out)  # never suggests attest can fix an integrity gap

    def test_unverifiable_latched_exit_two_no_attest_suggestion(self):
        self._clean_close()
        self._close(respond=True, advance=True)
        store = open_store(self.env, "prod")
        store._conn.execute("UPDATE close_records SET reason = ? WHERE seq = 1", ("tampered",))
        store._conn.commit()

        rc, out, err = self._status()
        self.assertEqual(rc, 2, err)
        self.assertIn("unverifiable", out.lower())
        self.assertNotIn("pacioli attest", out)


class TestCloseStatusKeyless(_Fixture):
    def test_keyless_status_renders_honestly_exit_two(self):
        keyless_store = BrokerStore(sqlite3.connect(":memory:"), None)
        with mock.patch("pacioli.cli.open_store", return_value=keyless_store):
            rc, out, err = self._status()
        self.assertEqual(rc, 2, err)
        self.assertIn("LATCHED", out)
        self.assertIn("keyless", out.lower())


class TestCloseStatusJsonShape(_Fixture):
    def test_json_shape_open(self):
        self._clean_close()
        self._close(respond=True, advance=True, until="2026-06-30T23:59:59Z")
        rc, out, err = self._status(as_json=True)
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertEqual(set(doc), {"target", "state", "history_count", "recent_history"})
        self.assertEqual(doc["target"], "prod")
        self.assertEqual(
            set(doc["state"]), {"latched", "reason", "cause", "cursor", "last_close_seq"}
        )
        self.assertFalse(doc["state"]["latched"])
        self.assertEqual(doc["history_count"], 1)
        self.assertEqual(len(doc["recent_history"]), 1)
        row = doc["recent_history"][0]
        self.assertEqual(
            set(row),
            {"seq", "ts", "action", "period_since", "period_until", "gapped", "reason", "source"},
        )
        self.assertEqual(row["action"], "close")
        self.assertFalse(row["gapped"])

    def test_json_shape_latched(self):
        self._gapped()
        rc, out, err = self._status(as_json=True)
        self.assertEqual(rc, 2, err)
        doc = json.loads(out)
        self.assertTrue(doc["state"]["latched"])
        self.assertEqual(doc["state"]["cause"], "gapped_awaiting_attestation")
        self.assertTrue(doc["recent_history"][-1]["gapped"])

    def test_history_tail_caps_at_five_most_recent_oldest_first(self):
        self._clean_close()
        for i in range(6):
            until = f"2026-0{i + 1}-01T00:00:00Z"
            self._close(respond=True, advance=True, until=until)
        rc, out, err = self._status(as_json=True)
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["history_count"], 6)
        tail = doc["recent_history"]
        self.assertEqual(len(tail), 5)
        seqs = [r["seq"] for r in tail]
        self.assertEqual(seqs, sorted(seqs))  # oldest first
        self.assertEqual(seqs[0], 2)  # row 1 dropped, tail is 2..6
        self.assertEqual(seqs[-1], 6)


# --- registry error surfaces cleanly ------------------------------------------------------------

class TestErrorSurfaces(_Fixture):
    _AMBIGUOUS_REG = (
        '[targets.a]\nbase_url = "https://a.example.com"\napi_key = "env:K"\n'
        'api_secret = "env:S"\n'
        '[targets.b]\nbase_url = "https://b.example.com"\napi_key = "env:K"\n'
        'api_secret = "env:S"\n'
    )

    def test_attest_ambiguous_default_target_errors_cleanly(self):
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(self._AMBIGUOUS_REG)
        rc, out, err = self._attest(target=None)
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    def test_close_status_ambiguous_default_target_errors_cleanly(self):
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(self._AMBIGUOUS_REG)
        rc, out, err = self._status(target=None)
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)


# --- full lifecycle: advance clean -> advance gapped -> latched status -> attest -> open status ->
# --- advance again (cursor chained) --------------------------------------------------------------

class TestFullLifecycle(_Fixture):
    def _clean_close_at(self, ts):
        """One committed act BACKDATED to ``ts`` — same fixed-clock recipe as _orphan_close_at
        (the redteam wave outlawed the future-``--until`` scaffolding this test used to lean on;
        past windows over backdated acts are the honest replacement)."""
        from pacioli.runtime import load_or_create_seal_key, state_db_path
        d = Path(self.dir.name)
        key = load_or_create_seal_key(d / "seal.key")
        store = BrokerStore(sqlite3.connect(str(state_db_path(str(d), "prod"))), key,
                            now_iso=lambda: ts)
        intent = store.record_intent({"tool": "submit", "target": "prod",
                                      "doctype": "Sales Invoice", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {"docstatus": 1}, None)
        return store

    def test_full_lifecycle_cursor_chains_through_attest(self):
        # 1. advance clean — a backdated act closed with a PAST window that covers it
        self._clean_close_at("2026-01-15T00:00:00Z")
        rc1, _, err1 = self._close(respond=True, advance=True, until="2026-01-31T23:59:59Z")
        self.assertEqual(rc1, 0, err1)

        # 2. advance gapped (explicit --since so the backdated orphan is captured regardless of
        # where the cursor from step 1 landed)
        self._orphan_close_at("2026-02-10T00:00:00Z")
        rc2, _, err2 = self._close(
            respond=True, advance=True, envelope=["orphan=attestation_gate"],
            since="2000-01-01T00:00:00Z", until="2026-02-28T23:59:59Z",
        )
        self.assertEqual(rc2, 1, err2)

        # 3. latched status
        rc3, out3, err3 = self._status()
        self.assertEqual(rc3, 2, err3)
        self.assertIn("LATCHED", out3)
        self.assertIn("2026-02-28T23:59:59Z", out3)

        # 4. attest
        rc4, out4, err4 = self._attest(reason="reviewed, a known timing artifact")
        self.assertEqual(rc4, 0, err4)
        self.assertIn("seq 2", out4)

        # 5. open status
        rc5, out5, err5 = self._status()
        self.assertEqual(rc5, 0, err5)
        self.assertIn("OPEN", out5)
        self.assertIn("2026-02-28T23:59:59Z", out5)

        # 6. advance again — cursor chained: --since auto-defaults to step 2's cursor, and the
        # still-unresolved orphan (timestamped 2026-02-10, before this window) falls outside it,
        # so this advance is clean — proving the clear was real, not a lucky repeat non-trip.
        rc6, out6, err6 = self._close(
            respond=True, advance=True, as_json=True, until="2026-03-31T23:59:59Z"
        )
        self.assertEqual(rc6, 0, err6)
        doc6 = json.loads(out6)
        self.assertEqual(doc6["statement"]["period"]["since"], "2026-02-28T23:59:59Z")
        self.assertEqual(doc6["advance"]["seq"], 4)  # rows: 1=close,2=close(gapped),3=attest,4=close

        store = open_store(self.env, "prod")
        self.assertFalse(store.close_gate_state()["latched"])


class TestAttestSeqFlag(_Fixture):
    """Redteam finding 4 (attest staleness, CLI half): ``pacioli attest`` passes the pending
    gapped close's seq into ``store.attest(expected_seq=...)`` (compare-and-append), and the NEW
    optional ``--seq N`` lets the operator name the exact gap they reviewed — a mismatch refuses
    (exit 1) with a render naming the ACTUAL pending gap."""

    def test_seq_flag_parses(self):
        args = build_parser().parse_args(["attest", "--reason", "r", "--seq", "3"])
        self.assertEqual(args.seq, 3)

    def test_seq_flag_defaults_to_none(self):
        args = build_parser().parse_args(["attest", "--reason", "r"])
        self.assertIsNone(args.seq)

    def _attest_seq(self, seq, reason="reviewed"):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_attest(self.env, reason, None, seq=seq)
        return rc, o.getvalue(), e.getvalue()

    def test_matching_seq_attests_normally(self):
        self._gapped()
        rc, out, err = self._attest_seq(1)
        self.assertEqual(rc, 0, err)
        self.assertIn("seq 1", out)

    def test_mismatched_seq_refuses_exit_one_names_the_actual_pending_gap(self):
        self._gapped()  # the pending gap is seq 1
        rc, out, err = self._attest_seq(7)
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertIn("seq 1", err)   # the ACTUAL pending gap, named
        self.assertIn("not seq 7", err)
        self.assertIn("review", err.lower())
        store = open_store(self.env, "prod")
        _, rows = store.close_records_snapshot()
        self.assertEqual(len(rows), 1)  # nothing appended
        self.assertTrue(store.close_gate_state()["latched"])  # still honestly pending

    def test_attest_passes_the_pending_seq_through_without_the_flag(self):
        # The compare-and-append half without --seq: the CLI reads the gate state it already
        # reads and threads the pending seq into store.attest(expected_seq=...).
        self._gapped()
        real = BrokerStore.attest
        seen = []

        def spy(self, reason, **kw):
            seen.append(kw.get("expected_seq", "MISSING"))
            return real(self, reason, **kw)

        with mock.patch.object(BrokerStore, "attest", spy):
            rc, out, err = self._attest()
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen, [1])

    def test_stale_attest_race_refuses_exit_one(self):
        from pacioli.store import AttestStaleError
        self._gapped()
        with mock.patch.object(BrokerStore, "attest",
                               side_effect=AttestStaleError("the pending gap changed")):
            rc, out, err = self._attest()
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)


class TestAttestGateLineHonest(_Fixture):
    """Redteam finding 6 (honesty F2): cmd_attest printed "gate: OPEN" HARDCODED while
    store.attest already returned the freshly-derived state — the render must come FROM that
    state, and say so honestly if it is somehow still latched."""

    def test_gate_line_reads_the_returned_state_not_a_hardcoded_string(self):
        latched_state = {"latched": True, "reason": "close at seq 3 closed over a gap "
                         "(gate_required) and has not been attested",
                         "cause": "gapped_awaiting_attestation",
                         "cursor": "2026-02-28T00:00:00Z", "last_close_seq": 3}
        self._gapped()
        with mock.patch.object(BrokerStore, "attest", return_value=latched_state):
            rc, out, err = self._attest()
        self.assertEqual(rc, 0, err)
        self.assertIn("LATCHED", out)
        self.assertNotIn("gate:     OPEN", out)

    def test_gate_line_open_when_state_open_unchanged_wording(self):
        self._gapped()
        rc, out, err = self._attest()
        self.assertEqual(rc, 0, err)
        self.assertIn("gate:     OPEN — the next `close --advance` is not blocked", out)


class TestCloseStatusShowsReasons(_Fixture):
    """Redteam finding 8: the close-status history tail must print each row's reason — the
    attest rows carry the ceremony's whole point; an empty reason stays honestly empty."""

    def test_attest_reason_appears_in_history_tail(self):
        self._gapped()
        self._attest(reason="reviewed the JAN gap, known timing artifact")
        rc, out, err = self._status()
        self.assertEqual(rc, 0, err)
        self.assertIn("reviewed the JAN gap, known timing artifact", out)

    def test_close_row_empty_reason_stays_empty(self):
        self._clean_close()
        self._close(respond=True, advance=True, until="2026-06-01T00:00:00Z")
        rc, out, err = self._status()
        self.assertEqual(rc, 0, err)
        self.assertIn("reason=''", out)


if __name__ == "__main__":
    unittest.main()
