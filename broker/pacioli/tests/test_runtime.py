# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Runtime/CLI tests — config assembly, seal-key handling, and the human mint path."""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pacioli.runtime import (RuntimeError_, assemble, load_or_create_seal_key, open_store,
                            state_db_path)
from pacioli.cli import cmd_mint, cmd_verify


REG = '[targets.prod]\nbase_url = "https://erp.example.com"\n' \
      'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n'


class TestSealKey(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "seal.key"

    def tearDown(self):
        self.dir.cleanup()

    def test_creates_key_with_0600_and_reloads_same(self):
        k1 = load_or_create_seal_key(self.path)
        self.assertEqual(len(k1), 32)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")
        k2 = load_or_create_seal_key(self.path)
        self.assertEqual(k1, k2)

    def test_group_or_world_readable_key_refused(self):
        load_or_create_seal_key(self.path)
        os.chmod(self.path, 0o644)
        with self.assertRaises(RuntimeError_) as ctx:
            load_or_create_seal_key(self.path)
        self.assertIn("permission", str(ctx.exception).lower())

    def test_short_key_refused(self):
        self.path.write_bytes(b"short")
        os.chmod(self.path, 0o600)
        with self.assertRaises(RuntimeError_):
            load_or_create_seal_key(self.path)


class TestStatePaths(unittest.TestCase):
    def test_db_per_target(self):
        self.assertNotEqual(state_db_path("/s", "prod"), state_db_path("/s", "staging"))

    def test_target_name_is_sanitised_for_the_filesystem(self):
        p = str(state_db_path("/s", "../../etc/passwd"))
        self.assertNotIn("..", p)
        self.assertTrue(p.startswith("/s/"))


class TestAssemble(unittest.TestCase):
    def test_assemble_builds_a_working_broker(self):
        with tempfile.TemporaryDirectory() as d:
            reg = Path(d) / "targets.toml"
            reg.write_text(REG)
            env = {"PACIOLI_REGISTRY": str(reg), "PACIOLI_STATE_DIR": d,
                   "K": "kk", "S": "ss"}
            broker = assemble(env)
            out = broker.dispatch("prove_orphans", {})
            self.assertTrue(out["ok"])

    def test_missing_registry_is_a_clear_error(self):
        with self.assertRaises(RuntimeError_) as ctx:
            assemble({"PACIOLI_REGISTRY": "/nonexistent/targets.toml"})
        self.assertIn("targets.toml", str(ctx.exception))


class TestMintCli(unittest.TestCase):
    """The human's side of CONSENT: keyless store, high-entropy machine-minted token, printed once."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _mint(self, plan_id="p1", ttl=900):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cmd_mint(self.env, plan_id=plan_id, target=None, ttl=ttl)
        return rc, out.getvalue() + err.getvalue()

    def test_mint_prints_a_high_entropy_token_once_and_stores_only_the_hash(self):
        # A plan must exist first — consent binds to a recorded plan, not a free-typed id.
        store = open_store(self.env, "prod")
        from pacioli.plan import new_plan
        store.record_plan(new_plan("p1", "prod", "v1", "2026-07-01", docname="SI-1"))

        rc, out = self._mint()
        self.assertEqual(rc, 0)
        token = [ln for ln in out.splitlines() if ln.startswith("marker: ")][0].split(" ", 1)[1]
        self.assertGreaterEqual(len(token), 32)
        store2 = open_store(self.env, "prod")
        self.assertEqual(store2.marker_state(token), "live")
        db_bytes = (Path(self.dir.name) / "prod.db").read_bytes()
        self.assertNotIn(token.encode(), db_bytes)  # only the hash is at rest

    # ── the GL disclosure, against the row shape the bench actually sends ──────────────────
    # `ledger_preview` returns {"gl_columns": [...], "gl_data": [...]} and the broker keeps only
    # gl_data, whose rows are LISTS, not dicts. Observed live on the bench 2026-07-30, columns in
    # this fixed order: Posting Date, Account, Debit (<ccy>), Credit (<ccy>), Against Account, ...
    # The currency suffix is why the column cannot be matched by an exact "debit" name.
    BENCH_GL = [
        ["2026-07-25", "4110 - Sales - HBW", "", 1450.0, "Two Rivers Bike Co", "", "", "Main - HBW", "", ""],
        ["2026-07-25", "1310 - Debtors - HBW", 1450.0, "", "4110 - Sales - HBW", "Customer",
         "Two Rivers Bike Co", "", "Sales Invoice", "ACC-SINV-2026-00006"],
    ]

    def _plan_with_gl(self, rows, plan_id="pgl"):
        store = open_store(self.env, "prod")
        from pacioli.plan import new_plan
        store.record_plan(new_plan(plan_id, "prod", "v1", "2026-07-01", docname="SI-1",
                                   projected_gl=rows))
        return plan_id

    def test_mint_discloses_the_real_totals_for_bench_shaped_rows(self):
        """A human approving a 1,450.00 posting must not be shown 0.00.

        Fails if _gl_side only understands dict rows: the bench sends lists, so every real
        disclosure summed to zero from 0.33.2 onward.
        """
        pid = self._plan_with_gl(self.BENCH_GL)
        rc, out = self._mint(plan_id=pid)
        self.assertEqual(rc, 0, out)
        self.assertIn("debits 1,450.00 / credits 1,450.00", out)

    def test_mint_raises_the_house_alarm_when_bench_shaped_rows_do_not_balance(self):
        """No debit without a credit. The alarm must be able to FIRE on real rows.

        Fails if the totals are always 0.0, because 0.0 - 0.0 == 0 makes the imbalance branch
        unreachable for every plan the bench ever produces.
        """
        skewed = [list(r) for r in self.BENCH_GL]
        skewed[1][2] = 1000.0  # debit 1000 against credit 1450
        pid = self._plan_with_gl(skewed, plan_id="pskew")
        rc, out = self._mint(plan_id=pid)
        self.assertEqual(rc, 0, out)
        self.assertIn("DOES NOT BALANCE", out)
        # The SIGN is load-bearing: debits 1000 against credits 1450 is "off by -450.00".
        # Asserting a bare "450.00" would pass just as happily with the debit and credit
        # columns swapped, which is the most likely way to get this helper wrong.
        self.assertIn("off by -450.00", out)

    def test_mint_states_totals_unavailable_rather_than_claiming_zero(self):
        """An unrecognised row shape must not be rendered as a balanced 0.00 entry.

        A silent 0.00 is indistinguishable from a genuinely empty projection AND suppresses the
        imbalance alarm, so the disclosure has to say it could not read the rows.
        """
        pid = self._plan_with_gl([["only", "two"], ["also", "short"]], plan_id="pjunk")
        rc, out = self._mint(plan_id=pid)
        self.assertEqual(rc, 0, out)
        self.assertIn("totals unavailable", out)
        self.assertNotIn("debits 0.00 / credits 0.00", out)

    def test_mint_counts_a_non_numeric_money_column_as_unread_not_as_zero(self):
        """A full-length row whose debit column holds junk is unreadable, not zero.

        Distinct from the short-row case: this one reaches float() and raises, so it exercises the
        exception path rather than the length guard. Mutation testing found that path untested.

        Retargeted in 0.34.2: the row is now reported as unread and the other row still totals,
        rather than blanking the whole disclosure. The property under test is unchanged — the junk
        must never be scored 0.0 — but it is now observable as a count instead of an absence.
        """
        junk = [list(r) for r in self.BENCH_GL]
        junk[0][2] = "n/a"  # a debit column that is present, full length, and not a number
        pid = self._plan_with_gl(junk, plan_id="pnan")
        rc, out = self._mint(plan_id=pid)
        self.assertEqual(rc, 0, out)
        self.assertIn("1 of 2", out)
        self.assertIn("could not be read", out)
        # scoring the junk as 0.0 would have made this a complete, balanced-looking read
        self.assertNotIn("debits 0.00 / credits 1,450.00", out)
        self.assertNotIn("DOES NOT BALANCE", out)

    def test_mint_refuses_an_unknown_plan(self):
        # Seed genesis via a keyed open first (same precondition every real target has by the
        # time a human ever mints — the agent's plan_submit call already dispatched keyed): a
        # target NO keyed caller has EVER opened has a genuinely empty seal_events table, which
        # BrokerStore.seal_state (Task 1) fail-closes to sealed=True, cause="no seal history" —
        # correct, but it would make THIS test's mint refusal about the seal, not the unknown
        # plan_id it means to isolate. See pacioli.tools.PacioliBroker._seal_gate / cli.cmd_mint's
        # own keyless pre-check (Task 2) for why an uninitialized store now surfaces here at all.
        open_store(self.env, "prod")
        rc, out = self._mint(plan_id="never-recorded")
        self.assertNotEqual(rc, 0)
        self.assertIn("plan", out.lower())

    def test_mint_on_a_torn_store_prints_a_clean_error_not_a_traceback(self):
        # A zero-byte store file is a torn write, not an empty ledger (StoreCorruptError). The
        # CLI must confess it like every other open_store refusal: "error: ...", exit 2.
        (Path(self.dir.name) / "prod.db").write_bytes(b"")
        rc, out = self._mint()
        self.assertEqual(rc, 2)
        self.assertIn("error:", out)

    def test_verify_cli_reports_ok(self):
        store = open_store(self.env, "prod")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_verify(self.env, target=None, expected_head=None)
        self.assertEqual(rc, 0)
        self.assertIn("ok", buf.getvalue().lower())

    def test_verify_cli_on_a_corrupt_body_fails_cleanly_not_a_traceback(self):
        # Redteam verify pass: a garbled receipt body must give the operator a clean FAILED, not a
        # raw json traceback out of the head/count reads that run after verify().
        store = open_store(self.env, "prod")
        store.record_intent({"doc": "SI-1"})
        store._conn.execute("UPDATE receipts SET body=? WHERE seq=0", ("{bad json",))
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cmd_verify(self.env, target=None, expected_head=None)  # must NOT raise
        self.assertEqual(rc, 1)
        self.assertIn("failed", err.getvalue().lower())


if __name__ == "__main__":
    unittest.main()


class TestMintDisclosesTheActNotJustThePlanId(unittest.TestCase):
    """Redteam 2026-07-26 — the human's only INDEPENDENT view of what they are authorising.

    `pacioli mint` printed the plan id, the docname and the target. Nothing else. So a one-invoice
    submit and a cascade-cancel that reverses five submitted documents rendered identically apart
    from a hex string, and the operator's entire picture of the act was the agent's narration —
    which is exactly what consent is supposed to be independent of.

    The binding machinery was never weak (`check_op`/`check_doctype` are exact and were probed
    clean). The DISCLOSURE was. Every field shown now was already persisted on the plan.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _record(self, plan_id, **kw):
        from pacioli.plan import new_plan
        store = open_store(self.env, "prod")
        store.record_plan(new_plan(plan_id, "prod", "v1", "2026-07-01", **kw))

    def _mint(self, plan_id):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cmd_mint(self.env, plan_id=plan_id, target=None, ttl=900)
        return rc, out.getvalue() + err.getvalue()

    def test_a_submit_and_a_cascade_cancel_do_not_render_identically(self):
        self._record("p-submit", docname="SI-1", op="submit", doctype="Sales Invoice")
        self._record("p-cascade", docname="SI-1", op="cancel", doctype="Sales Invoice",
                     graph=[{"doctype": "Payment Entry", "name": "PE-9"},
                            {"doctype": "Delivery Note", "name": "DN-9"},
                            {"doctype": "Journal Entry", "name": "JE-9"}])
        _, submit_out = self._mint("p-submit")
        _, cascade_out = self._mint("p-cascade")

        # Strip the plan-id line: the point is that the REST of the disclosure differs.
        def body(text):
            return "\n".join(ln for ln in text.splitlines() if not ln.startswith("plan:")
                             and not ln.startswith("marker:"))

        self.assertNotEqual(body(submit_out), body(cascade_out),
                            "two different acts must not look the same to the approving human")

    def test_the_act_names_the_operation_and_the_doctype(self):
        self._record("p1", docname="SI-1", op="cancel", doctype="Sales Invoice")
        _, out = self._mint("p1")
        self.assertIn("CANCEL", out)
        self.assertIn("Sales Invoice", out)
        self.assertIn("SI-1", out)

    def test_a_cascade_names_every_document_it_will_touch(self):
        self._record("p1", docname="SI-1", op="cancel", doctype="Sales Invoice",
                     graph=[{"doctype": "Payment Entry", "name": "PE-9"},
                            {"doctype": "Delivery Note", "name": "DN-9"}])
        _, out = self._mint("p1")
        # A count is a thing a human skims; a list is a thing they check.
        self.assertIn("PE-9", out)
        self.assertIn("DN-9", out)

    def test_risk_flags_reach_the_human(self):
        self._record("p1", docname="SI-1", op="submit", doctype="Sales Invoice",
                     risk_flags=["posting date is in a prior period"])
        _, out = self._mint("p1")
        self.assertIn("prior period", out)

    def test_an_unbalanced_projection_is_called_out_loudly(self):
        # The house law on the consent line. A balanced entry nets to zero, so printing the NET
        # would show "0.00" for every healthy plan and tell the human nothing; the magnitude is
        # the size of the act and the imbalance is the alarm.
        self._record("p1", docname="JE-1", op="submit", doctype="Journal Entry",
                     projected_gl=[{"debit": 100.0, "credit": 0}, {"debit": 0, "credit": 90.0}])
        _, out = self._mint("p1")
        self.assertIn("DOES NOT BALANCE", out)
        self.assertIn("10.00", out)

    def test_a_balanced_projection_shows_its_size_not_a_useless_zero(self):
        self._record("p1", docname="SI-1", op="submit", doctype="Sales Invoice",
                     projected_gl=[{"debit": 1450.0, "credit": 0}, {"debit": 0, "credit": 1450.0}])
        _, out = self._mint("p1")
        self.assertNotIn("DOES NOT BALANCE", out)
        self.assertIn("1,450.00", out)


class TestGlDisclosureDegradesPerRow(unittest.TestCase):
    """0.34.1 made ANY unreadable row blank the whole disclosure and skip the balance check.

    That lost information a human needs: a genuine imbalance 0.34.0 caught went silent, and a
    cascade concatenates every node's rows (tools.py), so one bad row blanked all of them. Total
    what IS readable, say plainly how much was not, and do not assert balance on a partial read —
    a false alarm is the same class of lie as a false all-clear.
    """

    BENCH_ROW_LEN = 10

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _row(self, debit="", credit=""):
        return ["2026-07-25", "1310 - Debtors - HBW", debit, credit,
                "4110 - Sales - HBW", "Customer", "Two Rivers", "", "Sales Invoice", "SI-1"]

    def _mint_with(self, rows, plan_id="pdeg"):
        store = open_store(self.env, "prod")
        from pacioli.plan import new_plan
        store.record_plan(new_plan(plan_id, "prod", "v1", "2026-07-01", docname="SI-1",
                                   projected_gl=rows))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cmd_mint(self.env, plan_id=plan_id, target=None, ttl=900)
        return out.getvalue() + err.getvalue()

    def test_a_readable_majority_still_reports_its_totals(self):
        """The lens's case: 0.34.0 caught this -450.00, 0.34.1 went silent on it."""
        rows = [self._row(debit=1000.0), self._row(credit=1450.0), self._row(debit="n/a")]
        out = self._mint_with(rows)
        self.assertIn("debits 1,000.00 / credits 1,450.00", out)
        self.assertIn("1 of 3", out)

    def test_an_unreadable_row_is_announced_as_RISK_not_as_quiet_metadata(self):
        """The case where we cannot read what we are disclosing is the strongest reason to slow a
        human down, so it may not render at the same weight as an ordinary count."""
        out = self._mint_with([self._row(debit=1000.0), self._row(debit="n/a")])
        risk_lines = [ln for ln in out.splitlines() if ln.strip().startswith("RISK:")]
        self.assertTrue(any("could not be read" in ln for ln in risk_lines), out)

    def test_a_partial_read_does_not_assert_the_entry_is_unbalanced(self):
        """A missing row may be the balancing side. Claiming imbalance from a partial read is a
        false alarm, which is the same class of lie as a false all-clear."""
        out = self._mint_with([self._row(debit=1000.0), self._row(debit="n/a")])
        self.assertNotIn("DOES NOT BALANCE", out)

    def test_an_inserted_column_is_unreadable_rather_than_silently_wrong(self):
        """If ERPNext inserts a numeric column before debit, positional reads shift. Both sides
        shift together, so the entry still nets to zero and the alarm stays quiet while the human
        is shown the wrong magnitude. The row length is pinned so that becomes unreadable."""
        shifted = [self._row(debit=1000.0)[:2] + [999.0] + self._row(debit=1000.0)[2:]]
        self.assertEqual(len(shifted[0]), self.BENCH_ROW_LEN + 1)
        out = self._mint_with(shifted)
        self.assertNotIn("999.00", out)
        self.assertIn("could not be read", out)

    def test_a_non_finite_amount_is_unreadable(self):
        """float('nan') does not raise, so it sails through a bare try/except and prints
        'debits nan'. Every other money reader in this package applies math.isfinite."""
        out = self._mint_with([self._row(debit=float("nan")), self._row(credit=1450.0)])
        self.assertNotIn("nan", out.lower())
        self.assertIn("could not be read", out)

    def test_a_boolean_is_not_a_money_value(self):
        """float(True) is 1.0. get_gl_entries already refuses non-bool numbers at its seam."""
        out = self._mint_with([self._row(debit=True), self._row(credit=1450.0)])
        self.assertIn("could not be read", out)
