# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Operator CLI: `pacioli seal` / `pacioli unseal` / `pacioli seal-status` (Task 3,
docs/plans/2026-07-14-close-half3-seal-slice.md; `seal-status --anchor`,
docs/plans/2026-07-15-seal-anchor-slice.md Task 3).

Task 1 gave ``BrokerStore`` the fail-closed, evented seal state; Task 2 wired the dispatch-time
gate that refuses every governed write while sealed. This module is the HUMAN side of that same
state: the operator's own hand to seal/unseal, and a read-only status render that must stay
legible (never crash) even when the underlying history is itself fail-closed (a gap, an
unverifiable latest event) — the confession must stay readable.

``TestSealStatusAnchor`` covers the 2026-07-15 slice's Task 3: ``seal-status --anchor`` threads a
recorded off-box pin's ``seal_head``/``seal_count`` into ``seal_state(...)`` so the everyday status
command can render a tail-rollback the same way `pacioli anchor check` already does — audit-time
DETECTION, gated by the operator's own check cadence, never real-time prevention.

Fixture mirrors ``TestCloseCli`` (test_close.py) — a real on-disk store under a temp dir, no
mocking of the store layer itself; only `--reason`/argparse wiring and the ambiguous-default
registry case are exercised at the parser/wiring level.
"""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pacioli.anchor import make_anchor, render_anchor
from pacioli.cli import build_parser, cmd_anchor_write, cmd_seal, cmd_seal_status, cmd_unseal, main
from pacioli.prove import GENESIS
from pacioli.runtime import open_store, state_db_path

_REG = ('[targets.prod]\nbase_url = "https://erp.example.com"\n'
        'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n')

_AMBIGUOUS_REG = (
    '[targets.a]\nbase_url = "https://a.example.com"\napi_key = "env:K"\napi_secret = "env:S"\n'
    '[targets.b]\nbase_url = "https://b.example.com"\napi_key = "env:K"\napi_secret = "env:S"\n'
)


class TestSealParserWiring(unittest.TestCase):
    """The argparse flags exist and `main` threads them into the right cmd_* — mirrors
    TestCloseReconcileFlagWiring (test_close.py)."""

    def test_seal_requires_reason(self):
        with self.assertRaises(SystemExit) as cm:
            build_parser().parse_args(["seal"])
        self.assertEqual(cm.exception.code, 2)

    def test_unseal_requires_reason(self):
        with self.assertRaises(SystemExit) as cm:
            build_parser().parse_args(["unseal"])
        self.assertEqual(cm.exception.code, 2)

    def test_seal_parses_with_reason_and_target(self):
        args = build_parser().parse_args(["seal", "--reason", "closing early", "--target", "x"])
        self.assertEqual(args.reason, "closing early")
        self.assertEqual(args.target, "x")

    def test_unseal_parses_with_reason(self):
        args = build_parser().parse_args(["unseal", "--reason", "reopening"])
        self.assertEqual(args.reason, "reopening")
        self.assertIsNone(args.target)

    def test_seal_status_defaults(self):
        args = build_parser().parse_args(["seal-status"])
        self.assertIsNone(args.target)
        self.assertFalse(args.as_json)

    def test_seal_status_json_flag(self):
        args = build_parser().parse_args(["seal-status", "--json"])
        self.assertTrue(args.as_json)

    def test_seal_status_anchor_defaults_to_none(self):
        args = build_parser().parse_args(["seal-status"])
        self.assertIsNone(args.anchor)

    def test_seal_status_anchor_flag_parses(self):
        args = build_parser().parse_args(["seal-status", "--anchor", "pin.json"])
        self.assertEqual(args.anchor, "pin.json")

    def test_main_threads_seal(self):
        with mock.patch("pacioli.cli.cmd_seal", return_value=0) as m:
            rc = main(["seal", "--reason", "why"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertEqual(rc, 0)
        m.assert_called_once()
        self.assertEqual(m.call_args.args[1], "why")

    def test_main_threads_unseal(self):
        with mock.patch("pacioli.cli.cmd_unseal", return_value=0) as m:
            rc = main(["unseal", "--reason", "why"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertEqual(rc, 0)
        m.assert_called_once()

    def test_main_threads_seal_status(self):
        with mock.patch("pacioli.cli.cmd_seal_status", return_value=0) as m:
            rc = main(["seal-status"], env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertEqual(rc, 0)
        m.assert_called_once()

    def test_main_threads_seal_status_anchor(self):
        with mock.patch("pacioli.cli.cmd_seal_status", return_value=0) as m:
            rc = main(["seal-status", "--anchor", "pin.json"],
                     env={"PACIOLI_REGISTRY": "/nonexistent"})
        self.assertEqual(rc, 0)
        m.assert_called_once()
        self.assertEqual(m.call_args.args[3], "pin.json")


class TestSealCli(unittest.TestCase):
    """Real on-disk store, mirrors TestCloseCli's fixture shape."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(_REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _seal(self, reason="closing the books early", target=None):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_seal(self.env, reason, target)
        return rc, o.getvalue(), e.getvalue()

    def _unseal(self, reason="books balanced, reopening", target=None):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_unseal(self.env, reason, target)
        return rc, o.getvalue(), e.getvalue()

    def _status(self, target=None, as_json=False, anchor=None):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_seal_status(self.env, target, as_json, anchor)
        return rc, o.getvalue(), e.getvalue()

    # --- happy path -----------------------------------------------------------------------
    def test_fresh_store_is_unsealed_status_exit_zero(self):
        rc, out, err = self._status()
        self.assertEqual(rc, 0, err)
        self.assertIn("sealed: False", out)

    def test_seal_prints_state_and_exits_zero(self):
        rc, out, err = self._seal(reason="closing the books early")
        self.assertEqual(rc, 0, err)
        self.assertIn("sealed: True", out)
        self.assertIn("closing the books early", out)

    def test_seal_then_status_is_sealed_exit_two_reason_shown(self):
        self._seal(reason="closing the books early")
        rc, out, err = self._status()
        self.assertEqual(rc, 2)
        self.assertIn("sealed: True", out)
        self.assertIn("closing the books early", out)

    def test_unseal_after_seal_returns_to_unsealed_exit_zero(self):
        self._seal()
        rc, out, err = self._unseal(reason="books balanced, reopening")
        self.assertEqual(rc, 0, err)
        self.assertIn("sealed: False", out)
        self.assertIn("books balanced, reopening", out)
        rc2, out2, _ = self._status()
        self.assertEqual(rc2, 0)
        self.assertIn("sealed: False", out2)

    # --- F2 (security redteam 2026-07-15): re-pin reminder at the point of action -----------
    def test_seal_prints_repin_reminder(self):
        # "re-pin after every seal/unseal" is the linchpin of the off-box seal anchor -- a seal
        # taken after the last pin is silently reversible to the stale pre-seal pin. Before this
        # fix, that discipline lived only in an abstract README paragraph; an operator acting at
        # the CLI never saw it. `pacioli seal` must now say so, at the point of action.
        rc, out, err = self._seal(reason="closing the books early")
        self.assertEqual(rc, 0, err)
        self.assertIn("anchor write", err)

    def test_unseal_prints_repin_reminder_on_success(self):
        self._seal(reason="x")
        rc, out, err = self._unseal(reason="books balanced, reopening")
        self.assertEqual(rc, 0, err)
        self.assertIn("anchor write", err)

    def test_unseal_prints_repin_reminder_even_when_it_stays_sealed(self):
        # A gapped history means the unseal event was still APPENDED (the pin is stale either
        # way) even though the resulting state reads sealed=True and the command exits 1 -- the
        # re-pin reminder must fire regardless, not only on the "cleared" path.
        self._seal(reason="closing")      # seq 2
        self._unseal(reason="temp")       # seq 3
        self._seal(reason="closing again")  # seq 4
        db = state_db_path(self.env["PACIOLI_STATE_DIR"], "prod")
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM seal_events WHERE seq = 3")
        conn.commit()
        conn.close()
        rc, out, err = self._unseal(reason="clearing now")
        self.assertEqual(rc, 1)
        self.assertIn("anchor write", err)

    def test_seal_recorded_with_source_operator(self):
        self._seal(reason="x")
        store = open_store(self.env, "prod")
        self.assertEqual(store.seal_events()[-1]["source"], "operator")
        self.assertEqual(store.seal_events()[-1]["action"], "seal")

    def test_unseal_recorded_with_source_operator(self):
        self._seal(reason="x")
        self._unseal(reason="y")
        store = open_store(self.env, "prod")
        self.assertEqual(store.seal_events()[-1]["source"], "operator")
        self.assertEqual(store.seal_events()[-1]["action"], "unseal")

    # --- --json shape, pinned ---------------------------------------------------------------
    def test_json_shape_pinned_when_sealed(self):
        self._seal(reason="closing")
        rc, out, err = self._status(as_json=True)
        self.assertEqual(rc, 2, err)
        doc = json.loads(out)
        self.assertEqual(set(doc), {"target", "state", "event_count", "recent_events"})
        self.assertEqual(doc["target"], "prod")
        self.assertEqual(set(doc["state"]), {"sealed", "since", "reason", "source", "seq", "cause"})
        self.assertTrue(doc["state"]["sealed"])
        self.assertEqual(doc["state"]["reason"], "closing")
        self.assertEqual(doc["state"]["source"], "operator")
        self.assertIsNone(doc["state"]["cause"])
        self.assertEqual(doc["event_count"], 2)  # genesis + seal
        self.assertIsInstance(doc["recent_events"], list)
        self.assertEqual(len(doc["recent_events"]), 2)

    def test_json_shape_pinned_when_unsealed_fresh_store(self):
        rc, out, err = self._status(as_json=True)
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertFalse(doc["state"]["sealed"])
        self.assertEqual(doc["event_count"], 1)  # genesis only

    def test_status_shows_at_most_five_most_recent_events_oldest_first(self):
        # genesis(1) + 6 more appends = 7 events total; only the latest 5 are rendered
        for i in range(6):
            if i % 2 == 0:
                self._seal(reason=f"r{i}")
            else:
                self._unseal(reason=f"r{i}")
        rc, out, err = self._status(as_json=True)
        doc = json.loads(out)
        self.assertEqual(doc["event_count"], 7)
        events = doc["recent_events"]
        self.assertEqual(len(events), 5)
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(seqs))  # oldest first
        self.assertEqual(seqs[-1], 7)
        self.assertEqual(seqs[0], 3)  # rows 1,2 dropped, tail is 3..7

    def test_events_carry_verified_flags(self):
        self._seal(reason="x")
        rc, out, err = self._status(as_json=True)
        doc = json.loads(out)
        self.assertTrue(all(e["verified"] is True for e in doc["recent_events"]))

    def test_tampered_row_render_uses_neutral_register_not_a_verdict_word(self):
        # model-fidelity redteam 2026-07-14: the per-event human render used "TAMPERED" (a verdict
        # accusing an actor) for the identical fact seal_state's own `cause` calls the neutral
        # "unverifiable" -- the CLI must match the accounting register, state what not who. Tamper
        # a MIDDLE row (not the latest) so seal_state's own `cause` stays None (the overall state
        # is genuinely sealed/unsealed) while `seal_events`' per-event verified flag still catches
        # it -- isolating this to the per-event render, not the state-level cause string.
        self._seal(reason="a")       # seq 2
        self._unseal(reason="b")     # seq 3
        db = state_db_path(self.env["PACIOLI_STATE_DIR"], "prod")
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE seal_events SET reason = 'forged' WHERE seq = 2")
        conn.commit()
        conn.close()
        rc, out, err = self._status()
        self.assertNotIn("TAMPERED", out)
        self.assertIn("HMAC MISMATCH", out)

    # --- registry error surfaces cleanly, never a raw traceback ------------------------------
    def test_ambiguous_default_target_registry_error_surfaces_cleanly(self):
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(_AMBIGUOUS_REG)
        rc, out, err = self._status(target=None)
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    def test_ambiguous_default_target_registry_error_on_seal(self):
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(_AMBIGUOUS_REG)
        rc, out, err = self._seal(target=None)
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    def test_ambiguous_default_target_registry_error_on_unseal(self):
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(_AMBIGUOUS_REG)
        rc, out, err = self._unseal(target=None)
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    # --- fail-closed states are RENDERED, never raised ---------------------------------------
    def test_seq_gap_status_renders_cause_exit_two(self):
        self._seal(reason="closing")   # seq 2
        self._unseal(reason="reopening")  # seq 3 -- currently unsealed
        db = state_db_path(self.env["PACIOLI_STATE_DIR"], "prod")
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM seal_events WHERE seq = 2")
        conn.commit()
        conn.close()
        rc, out, err = self._status()
        self.assertEqual(rc, 2)  # a gap fail-closes to sealed
        self.assertIn("seal history gap", out)
        self.assertEqual(err, "")  # rendered on stdout, not raised as an error

    def test_unverifiable_latest_status_renders_cause_exit_two(self):
        self._seal(reason="closing")      # seq 2
        self._unseal(reason="reopening")  # seq 3 -- would otherwise read as unsealed
        db = state_db_path(self.env["PACIOLI_STATE_DIR"], "prod")
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE seal_events SET hmac = ? WHERE seq = 3", ("0" * 64,))
        conn.commit()
        conn.close()
        rc, out, err = self._status()
        self.assertEqual(rc, 2)
        self.assertIn("unverifiable", out)
        self.assertEqual(err, "")

    def test_unseal_over_a_preexisting_gap_stays_sealed_exit_one(self):
        # A gap earlier in the history is NOT healed just because a fresh `unseal` appends after
        # it -- seal_state's contiguity check still fails, so the operator must be told the
        # unseal did NOT actually clear the broker, not given a false "sealed: False".
        self._seal(reason="closing")      # seq 2
        self._unseal(reason="temp")       # seq 3
        self._seal(reason="closing again")  # seq 4
        db = state_db_path(self.env["PACIOLI_STATE_DIR"], "prod")
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM seal_events WHERE seq = 3")
        conn.commit()
        conn.close()
        rc, out, err = self._unseal(reason="clearing now")
        self.assertEqual(rc, 1)
        self.assertIn("sealed: True", out)  # the truthful post-append state, still rendered
        self.assertIn("error:", err)  # AND a loud, distinct refusal — never a silent exit 1


class TestSealStatusAnchor(unittest.TestCase):
    """`seal-status --anchor <pinfile>` (2026-07-15 slice, Task 3) — threads a recorded off-box
    pin's `seal_head`/`seal_count` into `seal_state(...)` so a tail-rollback renders SEALED here
    too, not only via `pacioli anchor check`. Fixture mirrors `TestAnchorCli` (test_anchor.py).
    Audit-time DETECTION only — nothing here blocks a write, on-box, ever."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(_REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _seal(self, reason="closing", target=None):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_seal(self.env, reason, target)
        return rc, o.getvalue(), e.getvalue()

    def _unseal(self, reason="reopening", target=None):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_unseal(self.env, reason, target)
        return rc, o.getvalue(), e.getvalue()

    def _status(self, target=None, as_json=False, anchor=None):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_seal_status(self.env, target, as_json, anchor)
        return rc, o.getvalue(), e.getvalue()

    def _write_pin(self):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_anchor_write(self.env, target=None, out=None)
        self.assertEqual(rc, 0, e.getvalue())
        return o.getvalue()

    def _db(self):
        return sqlite3.connect(str(Path(self.dir.name) / "prod.db"))

    def _pin_path(self, text):
        path = Path(self.dir.name) / "pin.json"
        path.write_text(text)
        return str(path)

    # --- no --anchor: byte-identical to 0.20.0 (Global Constraint 4) -------------------------
    def test_no_anchor_flag_matches_plain_status_unsealed(self):
        rc_plain, out_plain, _ = self._status()
        rc_anchor, out_anchor, _ = self._status(anchor=None)
        self.assertEqual((rc_plain, out_plain), (rc_anchor, out_anchor))

    def test_no_anchor_flag_matches_plain_status_sealed(self):
        self._seal(reason="closing early")
        rc_plain, out_plain, _ = self._status()
        rc_anchor, out_anchor, _ = self._status(anchor=None)
        self.assertEqual((rc_plain, out_plain), (rc_anchor, out_anchor))

    # --- v2 pin, agreeing: renders the plain on-box state, unchanged -------------------------
    def test_v2_anchor_agreeing_renders_plain_unsealed_state(self):
        pin = self._write_pin()
        path = self._pin_path(pin)
        rc, out, err = self._status(anchor=path)
        self.assertEqual(rc, 0, err)
        self.assertIn("sealed: False", out)

    def test_v2_anchor_agreeing_after_a_seal_renders_plain_sealed_state(self):
        self._seal(reason="closing the books")
        pin = self._write_pin()
        path = self._pin_path(pin)
        rc, out, err = self._status(anchor=path)
        self.assertEqual(rc, 2, err)
        self.assertIn("sealed: True", out)
        self.assertIn("closing the books", out)

    def test_v2_anchor_after_legitimate_growth_still_matches(self):
        # events appended AFTER the pin are never held against the caller — the pin only
        # vouches for history up to its own count (store.py's seal_state docstring).
        pin = self._write_pin()  # pins count 1 (genesis only)
        path = self._pin_path(pin)
        self._seal(reason="closing")  # legitimate new event since the pin
        rc, out, err = self._status(anchor=path)
        self.assertEqual(rc, 2, err)
        self.assertIn("sealed: True", out)
        self.assertIn("closing", out)  # the genuine current reason, not an anchor-mismatch cause

    # --- the headline: a tail rollback the on-box (unpinned) read alone cannot see -----------
    def test_anchor_detects_seal_tail_truncation_that_plain_status_misses(self):
        # Mirrors test_anchor.py's test_check_detects_seal_tail_truncation_while_receipts_still_
        # verify: seal once, pin (witnessing sealed=True off-box), then a keyless attacker
        # deletes that newest seal_events row -- the surviving genesis row's HMAC still verifies
        # (untouched), so the PLAIN (unpinned) read reverts to sealed=False -- the exact redteam
        # finding this slice closes. Only the anchored read still sees it.
        self._seal(reason="closing")       # seq 2 -- now sealed
        pin = self._write_pin()            # pins seal_head/seal_count at count 2, sealed
        path = self._pin_path(pin)
        with self._db() as conn:
            conn.execute("DELETE FROM seal_events WHERE seq = (SELECT MAX(seq) FROM seal_events)")

        # control: the plain (unpinned) read is blind to the rollback -- proves the gap this
        # closes is real, not a strawman (mirrors the redteam's own reproduction).
        rc_plain, out_plain, _ = self._status()
        self.assertEqual(rc_plain, 0, out_plain)
        self.assertIn("sealed: False", out_plain)

        rc, out, err = self._status(anchor=path)
        self.assertEqual(rc, 2, err)
        self.assertIn("sealed: True", out)
        self.assertIn("off-box anchor", out)

    def test_anchor_detects_seal_divergence(self):
        self._seal(reason="closing")
        pin = self._write_pin()
        path = self._pin_path(pin)
        from pacioli.store import _seal_event_hmac
        with self._db() as conn:
            row = conn.execute(
                "SELECT seq, ts, source FROM seal_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            seq, ts, source = row
            key = (Path(self.dir.name) / "seal.key").read_bytes()
            new_mac = _seal_event_hmac(key, seq, ts, "unseal", "forged reopen", source)
            conn.execute(
                "UPDATE seal_events SET action=?, reason=?, hmac=? WHERE seq=?",
                ("unseal", "forged reopen", new_mac, seq),
            )
        rc, out, err = self._status(anchor=path)
        self.assertEqual(rc, 2, err)
        self.assertIn("diverges", out)

    def test_json_mode_with_anchor_mismatch_carries_cause(self):
        self._seal(reason="closing")
        self._unseal(reason="reopening")
        pin = self._write_pin()
        path = self._pin_path(pin)
        with self._db() as conn:
            conn.execute("DELETE FROM seal_events WHERE seq = (SELECT MAX(seq) FROM seal_events)")
        rc, out, err = self._status(anchor=path, as_json=True)
        self.assertEqual(rc, 2, err)
        doc = json.loads(out)
        self.assertTrue(doc["state"]["sealed"])
        self.assertIn("off-box anchor", doc["state"]["cause"])

    # --- v1 pin: WARN, never falsely claim the seal is covered -------------------------------
    def test_v1_anchor_pin_warns_and_renders_onbox_state(self):
        v1_pin = render_anchor(make_anchor("prod", GENESIS, 0, "t"))
        path = self._pin_path(v1_pin)
        self._seal(reason="closing")
        rc, out, err = self._status(anchor=path)
        self.assertEqual(rc, 2, err)  # renders the genuine on-box sealed state
        self.assertIn("sealed: True", out)
        self.assertIn("predates seal anchoring", err)
        self.assertIn("v1", err)
        self.assertIn("NOT covered", err)

    def test_v1_anchor_pin_does_not_mask_a_real_rollback(self):
        # A v1 pin gives NO seal coverage -- feeding it in must not change the render at all
        # (still the plain on-box state), and must never claim to have checked what it did not.
        # Same rollback setup as the v2 headline test above: seal once, delete that newest row --
        # the surviving genesis row's HMAC still verifies, so the on-box read alone (v1 pin or no
        # pin at all) reverts to sealed=False, the exact gap a v2 pin (above) closes.
        self._seal(reason="closing")
        v1_pin = render_anchor(make_anchor("prod", GENESIS, 0, "t"))
        path = self._pin_path(v1_pin)
        with self._db() as conn:
            conn.execute("DELETE FROM seal_events WHERE seq = (SELECT MAX(seq) FROM seal_events)")
        rc, out, err = self._status(anchor=path)
        self.assertEqual(rc, 0, err)  # NOT covered -- reads exactly as the unpinned control does
        self.assertIn("sealed: False", out)
        self.assertIn("predates seal anchoring", err)

    # --- deny-biased: unreadable/malformed pin -> ERROR, never silently "unanchored" ---------
    def test_malformed_anchor_text_errors_not_silently_unanchored(self):
        path = self._pin_path("not json at all")
        rc, out, err = self._status(anchor=path)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")  # nothing rendered -- never renders as if unanchored
        self.assertIn("error:", err)

    def test_unreadable_anchor_path_errors(self):
        rc, out, err = self._status(anchor=str(Path(self.dir.name) / "nonexistent.json"))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("error:", err)

    def test_malformed_partial_seal_fields_errors(self):
        rec = dict(make_anchor("prod", GENESIS, 0, "t", seal_head="a" * 64, seal_count=1))
        del rec["seal_count"]
        path = self._pin_path(json.dumps(rec))
        rc, out, err = self._status(anchor=path)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("error:", err)

    def test_cross_target_anchor_errors(self):
        other_pin = render_anchor(make_anchor("other-target", GENESIS, 0, "t"))
        path = self._pin_path(other_pin)
        rc, out, err = self._status(anchor=path)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("error:", err)
        self.assertIn("other-target", err)

    def test_check_reads_stdin_dash(self):
        pin = self._write_pin()
        with mock.patch("sys.stdin", io.StringIO(pin)):
            rc, out, err = self._status(anchor="-")
        self.assertEqual(rc, 0, err)
        self.assertIn("sealed: False", out)


if __name__ == "__main__":
    unittest.main()
