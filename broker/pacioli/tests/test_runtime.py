# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Runtime/CLI tests — config assembly, seal-key handling, and the human mint path."""
import io
import os
import sqlite3
import tempfile
import unittest
import unittest.mock   # explicit: `import unittest` does not bind the .mock submodule
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pacioli.runtime import (RuntimeError_, assemble, load_or_create_seal_key, open_store,
                            state_db_path)
from pacioli.cli import cmd_mint, cmd_verify


REG = '[targets.prod]\nbase_url = "https://erp.example.com"\n' \
      'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n'


def gl_line(case, out):
    """The single `projected GL:` line.

    Assertions about the TOTALS line must be made against the totals line: the RISK line below it
    carries the same "N of M ... could not be read" substring, so `assertIn(..., out)` over the
    whole output is satisfied by either one.

    MODULE-LEVEL since 2026-08-11. It used to be a method on one test class, and a test in a
    DIFFERENT class 60 lines above therefore asserted `assertIn("could not be read", out)` over
    the whole output — the exact shape this helper's own docstring forbids, in the same file.
    Proven dead by an independent review: blanking the disclosure from the totals line
    (`cli.py:132`) while leaving the RISK line kept that test green.
    """
    lines = [ln for ln in out.splitlines() if "projected GL:" in ln]
    case.assertEqual(len(lines), 1, f"expected exactly one projected-GL line, got: {out}")
    return lines[0]


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

    def _env(self, d):
        reg = Path(d) / "targets.toml"
        reg.write_text(REG)
        return {"PACIOLI_REGISTRY": str(reg), "PACIOLI_STATE_DIR": d, "K": "kk", "S": "ss"}

    def test_assemble_without_via_leaves_operator_tools_refused(self):
        # Deny-biased default: an undeclared broker never reaches the operator three.
        with tempfile.TemporaryDirectory() as d:
            broker = assemble(self._env(d))
            out = broker.dispatch("sweep_gl_entries",
                                  {"since": "2026-06-01 00:00:00",
                                   "until": "2026-06-30 23:59:59"})
            self.assertFalse(out["ok"])
            self.assertIn("operator", out["reason"])

    def test_assemble_hands_the_via_stamp_to_the_broker(self):
        # With the CLI stamp the same call gets PAST the spine gate — proven without any
        # network: the refusal it now earns is the missing-argument one, not the door one.
        from pacioli.operator import CLI_VIA
        with tempfile.TemporaryDirectory() as d:
            broker = assemble(self._env(d), via=CLI_VIA)
            out = broker.dispatch("sweep_gl_entries", {})
            self.assertFalse(out["ok"])
            self.assertNotIn("operator-only", out["reason"])
            self.assertIn("required", out["reason"])

    def test_assemble_threads_the_transport_seam_to_the_client(self):
        # The same testing seam ErpnextClient has always had, now reachable through assemble —
        # what lets the close-path suites drive the governed reroute over fake HTTP routes.
        from pacioli.operator import CLI_VIA

        def transport(method, url, headers, params=None, body=None):
            if "Accounts%20Settings" in url:
                return 200, {"data": {"enable_immutable_ledger": 1}}
            return 404, None

        with tempfile.TemporaryDirectory() as d:
            broker = assemble(self._env(d), via=CLI_VIA, transport=transport)
            out = broker.dispatch("get_accounts_settings",
                                  {"fields": ["enable_immutable_ledger"]})
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["settings"], {"enable_immutable_ledger": 1})


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
        # The count was unpinned: dropping the header line entirely, and hardcoding "1" over a
        # two-node graph, both survived the whole suite. A fabricated count on a consent line is
        # the same class as a fabricated total.
        self.assertIn("CASCADES to 2 more document(s)", out)

    def test_a_reconcile_names_its_allocation_PAIRS_not_question_marks(self):
        """A reconcile graph node is an allocation pair, not a document.

        Its nodes carry payment_type/payment_no and invoice_type/invoice_no and have no `doctype`
        and no `docname`, so the generic render printed every one as `- ? ?` — an uncheckable
        list, labelled "documents", with the real names sitting on the plan unprinted. The block's
        own comment says a list is a thing a human checks; for reconcile there was nothing to
        check.
        """
        self._record("p1", docname="REC-1", op="reconcile", doctype="Payment Reconciliation",
                     graph=[{"payment_type": "Payment Entry", "payment_no": "PAY-1",
                             "invoice_type": "Sales Invoice", "invoice_no": "INV-1",
                             "allocated_amount": 1450.0}])
        _, out = self._mint("p1")
        self.assertNotIn("?", out)
        self.assertIn("Payment Entry PAY-1", out)
        self.assertIn("Sales Invoice INV-1", out)
        self.assertIn("1,450.00", out)
        # And it must not call an allocation pair a document.
        self.assertIn("ALLOCATES across 1 pair(s)", out)

    def test_an_unreadable_allocated_amount_says_so_rather_than_vanishing(self):
        """The same never-silent rail as the GL totals: a money value we cannot read is stated."""
        self._record("p1", docname="REC-1", op="reconcile", doctype="Payment Reconciliation",
                     graph=[{"payment_type": "Payment Entry", "payment_no": "PAY-1",
                             "invoice_type": "Sales Invoice", "invoice_no": "INV-1",
                             "allocated_amount": "n/a"}])
        _, out = self._mint("p1")
        self.assertIn("allocated amount unreadable", out)

    def test_risk_flags_reach_the_human(self):
        self._record("p1", docname="SI-1", op="submit", doctype="Sales Invoice",
                     risk_flags=["posting date is in a prior period"])
        _, out = self._mint("p1")
        self.assertIn("prior period", out)

    def test_context_reaches_the_human_before_the_risk_flags(self):
        """The consent moment (cmd_mint renders a REHYDRATED plan straight from the store) must
        show the party-baseline disclosure — and show it BEFORE the risk flags, so the numbers
        arrive before the alarm. Task 5 fix-round 1: this link had ZERO automated proof; a
        misplaced, mistyped, or mis-ordered context line would have shipped silently green.

        SCOPED HONESTLY (2026-07-31): "before the alarm" holds for `plan.risk_flags`, which is what
        this asserts. It is NOT true of every line beginning `RISK:` — the GL block renders two of
        its own above the context (the imbalance alarm, and 0.34.2's unreadable-row notice), and
        that is deliberate: a GL risk belongs beside the GL summary it qualifies. The unconditional
        version of this sentence appears in the plan doc and was wrong there too."""
        self._record("p1", docname="SI-1", op="submit", doctype="Sales Invoice",
                     context=["14 prior submitted document(s) for ACME."],
                     risk_flags=["posting date is in a prior period"])
        _, out = self._mint("p1")
        self.assertIn("14 prior submitted document(s) for ACME.", out)
        self.assertLess(out.index("14 prior submitted document(s) for ACME."),
                        out.index("prior period"),
                        "context must render before risk flags — the numbers before the alarm")

    def test_the_gl_block_and_the_context_block_compose(self):
        """The two blocks together, which nothing covered.

        Both merges onto this branch (0.34.1 and then 0.34.2) interleave the GL block with the
        context render, and every other test is on one side or the other: the GL tests record no
        context and the context test records no GL.

        CORRECTED CLAIM (2026-07-31): this docstring said a merge nesting the context loop INSIDE
        `if plan.projected_gl:` would pass "with the whole suite green". Not true — that mutant is
        caught by `test_context_reaches_the_human_before_the_risk_flags`, which predates this
        test. The mutant this one uniquely catches is ORDERING: rendering `plan.context` above the
        GL block leaves every other test green and is red only here. The coverage is real; the
        justification written into it was not, which is the same defect as the code it guards.
        """
        def row(debit="", credit=""):
            # The bench shape: ten columns, debit at 2 and credit at 3.
            return ["2026-07-25", "1310 - Debtors - HBW", debit, credit, "4110 - Sales - HBW",
                    "Customer", "Ridgeline Cyclery", "", "Sales Invoice", "SI-1"]

        # One unreadable row, so the partial-read path runs inside the composition too.
        rows = [row(debit=1000.0), row(credit=1450.0), row(debit="n/a")]
        self._record("p1", docname="SI-1", op="submit", doctype="Sales Invoice",
                     projected_gl=rows,
                     context=["26 prior submitted Sales Invoice document(s) for Ridgeline."],
                     risk_flags=["posting date is in a prior period"])
        _, out = self._mint("p1")
        self.assertIn("projected GL:", out)
        # ON THE TOTALS LINE, not merely somewhere in the output: the RISK line underneath carries
        # the same substring, so the whole-output form was satisfied by either and stayed green
        # when the totals-line disclosure was blanked entirely (independent review, 2026-08-11).
        self.assertIn("could not be read", gl_line(self, out))
        self.assertIn("26 prior submitted", out)
        self.assertLess(out.index("projected GL:"), out.index("26 prior submitted"),
                        "the GL summary renders before the party context")
        self.assertLess(out.index("26 prior submitted"), out.index("prior period"),
                        "context must render before the risk flags")

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

    def _gl_line(self, out):
        return gl_line(self, out)

    def test_a_readable_majority_still_reports_its_totals(self):
        """0.34.1 blanked the totals on any unreadable row; the readable subtotal survives now.

        (The docstring here used to say "0.34.0 caught this -450.00". It did not: 0.34.0 scored
        list rows 0.0 across the board, so it caught nothing on this fixture. Corrected 2026-07-31.)
        """
        rows = [self._row(debit=1000.0), self._row(credit=1450.0), self._row(debit="n/a")]
        out = self._mint_with(rows)
        gl_line = self._gl_line(out)
        self.assertIn("debits 1,000.00 / credits 1,450.00", gl_line)
        # The shortfall must ride ON the totals line, not only in the RISK line underneath. With
        # `partial = ""` the human reads "debits 1,000.00 / credits 1,450.00" as a complete total
        # over a partial read — and asserting "1 of 3" against the whole output stayed green,
        # because the RISK line says it too. Two paths, one pass signal, neither one asserted.
        self.assertIn("readable rows only; 1 of 3 could not be read", gl_line)

    def test_an_empty_projection_still_says_something(self):
        """NEVER SILENT at the container level, not only per row.

        `plan_submit` maps a missing `gl_data` key, a null, an empty object and an empty body all
        to `[]`, and `ledger_preview` checks only that a `message` key came back. The render had
        no `else`, so the consent line said NOTHING about GL and "this document posts no GL" was
        indistinguishable from "we could not read the projection". 0.34.2 hardened the degradation
        INSIDE that block while the block itself could be skipped whole.
        """
        out = self._mint_with([])
        self.assertIn("projected GL:", out)
        self.assertIn("none provided", out)
        # And it must not claim the document posts nothing — the one thing an empty body cannot
        # establish. Asserted as the presence of the hedge, because the first version of this
        # assertion was `assertNotIn("posts no GL", out)` against a line that renders "posts
        # none" — it could not have fired on ANY rendering, and printing the forbidden claim
        # outright left it green.
        # PIN THE WHOLE LINE, because the negative could not be written honestly.
        #
        # Three attempts at "the line must not claim the document posts nothing" all failed the
        # same way. v1 `assertNotIn("posts no GL")` could never fire against a line rendering
        # "posts none". v2 added a regex that was case-sensitive and hard-coded one phrasing. v3
        # widened it to two phrasings and its comment called that "the SEMANTIC negation" — and
        # 9 of 13 forbidden renderings still walked past, `the body posts nothing` among them.
        #
        # A regex over prose is a phrase list wearing a semantic costume, and the comment
        # claiming otherwise was itself the lie. So this asserts the exact line instead: any
        # rewording at all, forbidden or innocent, fails here and must be made deliberately.
        gl_lines = [ln for ln in out.splitlines() if "projected GL:" in ln]
        self.assertEqual(len(gl_lines), 1, out)
        self.assertEqual(
            gl_lines[0].strip(),
            "projected GL: none provided (not a claim that this document posts none)")

    def test_a_cancel_with_no_gl_rows_does_not_hedge_over_the_top_of_a_real_finding(self):
        """The hedge is a SUBMIT-path fact and must not travel to the cancel path.

        `plan_cancel` builds `projected_gl` from `get_gl_entries`, which validates every row and
        returns `[]` only when the ledger genuinely HAS none — and `tools.py` already flags that
        ("no live GL rows found for this voucher — nothing visible to unwind"). Rendering "none
        provided (not a claim that this document posts none)" there put two adjacent lines in one
        consent disclosure making OPPOSITE claims about the same fact, and degraded a positive
        finding the broker had actually established.
        """
        store = open_store(self.env, "prod")
        from pacioli.plan import new_plan
        store.record_plan(new_plan("pcan", "prod", "v1", "2026-07-01", docname="SO-1",
                                   op="cancel", doctype="Sales Order", projected_gl=[],
                                   risk_flags=["no live GL rows found for this voucher"]))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cmd_mint(self.env, plan_id="pcan", target=None, ttl=900)
        got = out.getvalue() + err.getvalue()
        # NOT asserting the flag text back: this fixture writes that string itself three lines
        # up, so reading it back proves only that risk_flags render at all — deleting the
        # PRODUCTION flag left it green. The production line is held where it is produced (25
        # tests for the single-op flag, and a dedicated test for the cascade per-node one).
        # What this test uniquely pins is the suppression.
        self.assertNotIn("none provided", got)

    def test_every_op_is_pinned_for_the_hedge_not_just_cancel(self):
        """All four ops, both directions. Scoping this to `op == "submit"` was pinned ONLY against
        `cancel`, so `!= "cancel"`, `in ("submit", "cascade_cancel")` and `in ("submit",
        "reconcile")` all survived the suite — and the first of those re-created the adjacent
        contradiction for `cascade_cancel`, the one path the justification never named.

        HEDGE (the emptiness is ambiguous or was never read): submit, reconcile.
        SILENT (the broker read the ledger and established it, and flags it itself):
        cancel, cascade_cancel.
        """
        cases = {"submit": True, "reconcile": True, "cancel": False, "cascade_cancel": False}
        # PRODUCTION-SHAPED, and that is the whole point of the second shape. The first version of
        # this test built every plan bare — no `graph`, no `risk_flags` — and BOTH hedging ops
        # carry both in production (a real reconcile plan has a graph and four standing flags; a
        # real submit reaching this branch generally carries at least one). So `elif ... and not
        # plan.risk_flags:` and `elif ... and not plan.graph:` each survived all 3297 tests while
        # silencing the hedge on every real plan. The risk_flags one is the worse: it suppresses
        # the GL disclosure PRECISELY on the plans that carry risk, which is the regression this
        # whole branch of the render exists to undo.
        shapes = {
            "bare": {},
            "production-shaped": {
                "graph": [{"doctype": "Payment Entry", "docname": "PAY-1"}],
                "risk_flags": ["reconcile may spawn system Journal Entries this broker does not "
                               "separately govern"],
            },
        }
        from pacioli.plan import new_plan
        for op, expect_hedge in cases.items():
            for shape, extra in shapes.items():
                with self.subTest(op=op, shape=shape):
                    store = open_store(self.env, "prod")
                    pid = f"pop-{op}-{shape}"
                    store.record_plan(new_plan(pid, "prod", "v1", "2026-07-01", docname="D-1",
                                               op=op, doctype="Sales Order", projected_gl=[],
                                               **extra))
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        cmd_mint(self.env, plan_id=pid, target=None, ttl=900)
                    got = out.getvalue() + err.getvalue()
                    if expect_hedge:
                        self.assertIn("none provided", got, f"{op}/{shape} must hedge")
                    else:
                        self.assertNotIn("none provided", got, f"{op}/{shape} must stay silent")

    def test_an_op_this_render_has_never_seen_hedges_by_DEFAULT(self):
        """The stated reason the rule is written as an EXCLUSION, which had zero coverage.

        `elif plan.op not in ("cancel", "cascade_cancel")` and `elif plan.op in ("submit",
        "reconcile")` are behaviourally identical across the four ops that exist today, so no
        test COULD tell them apart — which is exactly why the inclusion form was reported dead
        when it is not. What separates them is a FIFTH op, and `check_op` carries no allowlist,
        so one is a single code change away.

        On a never-silent rail the fail-safe direction is to say something, so an op this render
        has never seen must hedge rather than go quiet.
        """
        store = open_store(self.env, "prod")
        from pacioli.plan import new_plan
        store.record_plan(new_plan("pop-fifth", "prod", "v1", "2026-07-01", docname="D-1",
                                   op="amend", doctype="Sales Order", projected_gl=[]))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cmd_mint(self.env, plan_id="pop-fifth", target=None, ttl=900)
        self.assertIn("none provided", out.getvalue() + err.getvalue())

    def test_a_None_projection_is_empty_not_one_unreadable_row(self):
        """`_gl_rows`'s falsy short-circuit. Without it, `projected_gl=None` becomes ONE row that
        cannot be read, putting a false `RISK: 1 of 1 ... could not be read` on every mint whose
        caller passes no projection at all — which `plan_reconcile` does."""
        out = self._mint_with(None)
        self.assertNotIn("could not be read", out)
        self.assertNotIn("1 line(s)", out)

    def test_a_dict_row_missing_the_side_is_unreadable_not_zero(self):
        """A dict row that does not carry the side being read must be UNREADABLE, not 0.00.

        The list branch pins its shape exactly; the dict branch pinned nothing, on the reasoning
        that dict rows arrive only from `get_gl_entries`, which validates at its own seam. That
        seam guards the CANCEL direction only — `plan_submit` reaches the same helper through
        `preview.get("gl_data")` with no validation at all. `row.get(side)` returned None for an
        absent key and that scored 0.0 AND counted the row readable, so a genuine one-sided
        7,200.00 posting summed to `debits 0.00 / credits 0.00` with no alarm: the 0.33.2 bug
        wearing a different shape.
        """
        rows = [{"account": "1310 - Debtors", "debit_in_account_currency": 7200.0},
                {"account": "4110 - Sales", "credit_in_account_currency": 0.0}]
        out = self._mint_with(rows)
        self.assertNotIn("debits 0.00 / credits 0.00", out)
        self.assertIn("could not be read", out)

    def test_a_dict_row_missing_ONLY_the_credit_is_still_unreadable(self):
        """The discriminating fixture, and the reason the first pair of these tests was weak.

        In those, NEITHER row carried the exact key for EITHER side, so a half-guard that only
        refused a missing `debit` (or only a missing `credit`) still rendered both rows unreadable
        and the assertions passed through a different path than the one they name. A fixture where
        everything is broken cannot tell a half-fix from a fix.

        Here `debit` is present and correct under its exact name and only `credit` is absent, so a
        debit-only guard scores this row readable at 7,200 / 0.00 and reports a genuine one-sided
        posting as balanced-looking with no alarm.
        """
        rows = [{"account": "1310 - Debtors", "debit": 7200.0,
                 "credit_in_account_currency": 0.0}]
        out = self._mint_with(rows)
        self.assertIn("could not be read", out)
        self.assertNotIn("debits 7,200.00", out)

    def test_a_dict_row_missing_ONLY_the_debit_is_still_unreadable(self):
        """The mirror, which a credit-only guard would let through as a false alarm."""
        rows = [{"account": "1310 - Debtors", "debit_in_account_currency": 7200.0,
                 "credit": 0.0},
                {"account": "4110 - Sales", "debit": 0.0, "credit": 7200.0}]
        out = self._mint_with(rows)
        self.assertIn("could not be read", out)
        self.assertNotIn("DOES NOT BALANCE", out)

    def test_a_dict_row_with_an_EXPLICIT_null_amount_is_unreadable(self):
        """`side not in row` is strictly WEAKER than `.get(side) is None`: it catches the absent
        key and scores an explicit null 0.0. A mutation run once reported the two equivalent —
        that was an artifact of no fixture carrying an explicit null. This is that fixture."""
        rows = [{"account": "1310 - Debtors", "debit": None, "credit": None},
                {"account": "4110 - Sales", "debit": None, "credit": None}]
        out = self._mint_with(rows)
        self.assertNotIn("debits 0.00 / credits 0.00", out)
        self.assertIn("could not be read", out)

    def test_a_dict_row_with_an_empty_string_amount_is_unreadable(self):
        """`""` is the LIST shape's unused-side convention; a dict has no such convention, and
        `get_gl_entries` requires a finite number on both sides. Falling through to the shared
        check scored it 0.0 and readable — the same bug, one step narrower, inside the fix."""
        rows = [{"account": "1310 - Debtors", "debit": "", "credit": ""}]
        out = self._mint_with(rows)
        self.assertNotIn("debits 0.00 / credits 0.00", out)
        self.assertIn("could not be read", out)

    def test_a_dict_row_with_ONLY_an_empty_credit_is_unreadable(self):
        """The one-sided `""` fixture, and the third time this exact lesson has been learned.

        The commit that added the `""` guard argued, correctly, that a fixture where BOTH sides
        are broken cannot tell a half-fix from a fix — and then gave the new `""` half a fixture
        with both sides broken, one line inside the fix that argument motivated. A guard reading
        `value == "" and side == "debit"` survived the whole suite.

        Here `debit` is a real number and only `credit` carries ERPNext's unused-side `""`, which
        is exactly what an unvalidated `preview.get("gl_data")` can deliver. A debit-only guard
        renders this as a clean, silent, balanced-looking 7,200.00 with no RISK line at all.
        """
        rows = [{"account": "1310 - Debtors", "debit": 7200.0, "credit": ""},
                {"account": "4110 - Sales", "debit": 0.0, "credit": 7200.0}]
        out = self._mint_with(rows)
        self.assertIn("could not be read", out)
        self.assertNotIn("debits 7,200.00 / credits 7,200.00", out)

    def test_a_dict_row_with_ONLY_an_empty_debit_is_unreadable(self):
        """The mirror, which a credit-only `""` guard passes."""
        rows = [{"account": "1310 - Debtors", "debit": "", "credit": 7200.0}]
        out = self._mint_with(rows)
        self.assertIn("could not be read", out)
        self.assertNotIn("credits 7,200.00", out)

    def test_a_dict_row_with_ONLY_an_explicit_null_credit_is_unreadable(self):
        """Same shape for the null half: the explicit-null fixture also had both sides broken."""
        rows = [{"account": "1310 - Debtors", "debit": 7200.0, "credit": None},
                {"account": "4110 - Sales", "debit": 0.0, "credit": 7200.0}]
        out = self._mint_with(rows)
        self.assertIn("could not be read", out)
        self.assertNotIn("debits 7,200.00 / credits 7,200.00", out)

    def test_a_dict_row_missing_the_side_does_not_raise_a_false_alarm_either(self):
        """The mirror case, and the same class of lie. A balanced entry whose credit sits under an
        unrecognised key scored credits 0.00 and fired DOES NOT BALANCE on books that balance."""
        rows = [{"account": "1310 - Debtors", "debit": 1450.0, "credit": 0.0},
                {"account": "4110 - Sales", "credit_in_account_currency": 1450.0}]
        out = self._mint_with(rows)
        self.assertNotIn("DOES NOT BALANCE", out)
        self.assertIn("could not be read", out)

    def test_a_non_list_projection_does_not_fabricate_a_row_count(self):
        """`list(value or [])` explodes a STRING into one row per CHARACTER, and the consent line
        prints that length as fact. A permission error echoed back as the body rendered as
        `projected GL: 36 line(s)` — 36 being the length of an error message."""
        out = self._mint_with("Insufficient Permission for GL Entry")
        self.assertNotIn("36 line(s)", out)
        self.assertIn("1 line(s)", out)
        self.assertIn("totals unavailable", out)
        self.assertIn("could not be read", out)

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
        'debits nan'. Every other money reader in this package applies math.isfinite.

        **Scoped to the GL line, and that is load-bearing.** Until 2026-08-11 this asserted
        ``assertNotIn("nan", out.lower())`` over the WHOLE mint output — which also carries the
        freshly minted marker token. That token is ``secrets.token_urlsafe(24)``, i.e. 32 base64url
        characters, so it contains the letters ``nan`` on its own now and then and reddened a
        correct build. Seen live as ``marker: FHl9y8io_UetACzDU7DAZWRnanGWZvn4``.

        ⚠️ **Rate corrected 2026-08-11 by an independent review, and the correction is its own
        lesson.** The first note here said *"measured over 300 real mints: 1 hit (0.3%)"*. One hit
        in 300 draws is an OBSERVATION, not a rate — quoting it as a measured frequency turned a
        single sample into a statistic, in a docstring, during a campaign about claims being true.
        The real rate over 2,000,000 draws is **0.092%, about 1 in 1,090** (a single hit in 300 is
        perfectly ordinary at that rate). Rare enough to look like anything but what it was, which
        is exactly why it read as a coverage interaction.

        This is the same lesson ``_gl_line`` above already records in its own docstring, and that
        ``test_an_empty_projection_still_says_something`` records again in its comment about
        ``assertNotIn("posts no GL", out)``: an assertion about what a LINE says must be made
        against that line. A whole-output ``assertNotIn`` is both too weak (some other line can
        satisfy it) and too strong (unrelated random text can break it).
        ``test_the_disclosure_is_not_fooled_by_the_TOKEN`` pins this deterministically.
        """
        out = self._mint_with([self._row(debit=float("nan")), self._row(credit=1450.0)])
        gl_line = self._gl_line(out)
        self.assertNotIn("nan", gl_line.lower())
        self.assertIn("could not be read", gl_line)

    def test_the_disclosure_is_not_fooled_by_the_TOKEN(self):
        """Deterministic proof of the scoping fix above: force a token that contains ``nan``.

        With the token pinned, the old whole-output assertion fails and the line-scoped one
        passes — so this test would have caught the flake on every run instead of 1 in 300.
        """
        poisoned = "AAAAnanAAAA"
        with unittest.mock.patch("pacioli.cli.secrets.token_urlsafe", return_value=poisoned):
            out = self._mint_with([self._row(debit=float("nan")), self._row(credit=1450.0)])

        self.assertIn(f"marker: {poisoned}", out, "the poisoned token must reach the output")

        # the old assertion: reds on a correct build purely because of the token
        self.assertIn("nan", out.lower())

        # the fixed assertion: reads only the line whose rendering is under test
        gl_line = self._gl_line(out)
        self.assertNotIn("nan", gl_line.lower())
        self.assertIn("could not be read", gl_line)

    def test_a_boolean_is_not_a_money_value(self):
        """float(True) is 1.0. get_gl_entries already refuses non-bool numbers at its seam."""
        out = self._mint_with([self._row(debit=True), self._row(credit=1450.0)])
        self.assertIn("could not be read", out)
