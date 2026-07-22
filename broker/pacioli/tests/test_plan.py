# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Bench-free unit tests for the PLAN pure core (pacioli.plan) — plan record, TOCTOU freshness,
and the closed-books check (period-lock) refusal.

Dates are ISO ``YYYY-MM-DD`` strings (lexicographic compare = chronological *only* for validated
ISO), so the core validates the format before comparing. Deny-biased.

Run: `python3 -m unittest pacioli.tests.test_plan` from the broker app root. No frappe required.
"""
import unittest

from pacioli.plan import NO_DATE_FIELD, Plan, check_fresh, check_red_line, new_plan

PLAN = new_plan(
    plan_id="p1", target="acme/Acme Corp", doc_version="2026-06-30 11:00:00",
    posting_date="2026-06-30", projected_gl=[{"account": "Debtors", "debit": 100}],
    risk_flags=[], ts="2026-07-01T00:00:00Z",
)


class TestFreshness(unittest.TestCase):
    def test_unchanged_doc_is_fresh(self):
        self.assertEqual(check_fresh(PLAN, "2026-06-30 11:00:00"), (True, None))

    def test_changed_doc_is_stale(self):
        ok, reason = check_fresh(PLAN, "2026-06-30 12:30:00")
        self.assertFalse(ok)
        self.assertIn("stale", str(reason).lower())

    def test_missing_version_on_either_side_refused(self):
        # None == None must not read as a verified match
        self.assertFalse(check_fresh(PLAN, None)[0])
        self.assertFalse(check_fresh(PLAN, "")[0])
        self.assertFalse(check_fresh(new_plan("p", "t", doc_version=None, posting_date="2026-06-30"), None)[0])


class TestRedLine(unittest.TestCase):
    def test_clear_posting_allowed(self):
        self.assertEqual(check_red_line("2026-06-30", now_date="2026-07-01", locks={}), (True, None))

    def test_missing_posting_date_refused(self):
        self.assertFalse(check_red_line(None, now_date="2026-07-01", locks={})[0])

    def test_malformed_posting_date_refused(self):
        # non-zero-padded dates sort wrong lexicographically; refuse rather than mis-compare
        ok, reason = check_red_line("2026-3-15", now_date="2026-07-01", locks={})
        self.assertFalse(ok)
        self.assertIn("iso", str(reason).lower())

    def test_inside_closed_period_refused(self):
        ok, reason = check_red_line(
            "2026-05-15", now_date="2026-07-01", locks={"closed_period_until": "2026-05-31"}
        )
        self.assertFalse(ok)
        self.assertIn("period", str(reason).lower())

    def test_inside_frozen_till_refused(self):
        self.assertFalse(
            check_red_line("2026-05-15", now_date="2026-07-01", locks={"frozen_until": "2026-05-31"})[0]
        )

    def test_inside_pcv_boundary_refused(self):
        self.assertFalse(
            check_red_line("2026-05-15", now_date="2026-07-01", locks={"pcv_until": "2026-05-31"})[0]
        )

    def test_after_lock_allowed(self):
        self.assertTrue(
            check_red_line("2026-06-15", now_date="2026-07-01", locks={"frozen_until": "2026-05-31"})[0]
        )

    # --- E6: exactly on the boundary — the core inclusivity pin, previously untested (every
    # case above is clearly-before or clearly-after; none pins the tie itself). ``<=`` refuses.
    def test_frozen_until_exact_boundary_refused(self):
        ok, reason = check_red_line(
            "2026-05-31", now_date="2026-07-01", locks={"frozen_until": "2026-05-31"}
        )
        self.assertFalse(ok)
        self.assertIn("frozen_until", reason)

    def test_pcv_until_exact_boundary_refused(self):
        ok, reason = check_red_line(
            "2026-05-31", now_date="2026-07-01", locks={"pcv_until": "2026-05-31"}
        )
        self.assertFalse(ok)
        self.assertIn("pcv_until", reason)

    def test_closed_period_until_exact_boundary_refused(self):
        ok, reason = check_red_line(
            "2026-05-31", now_date="2026-07-01", locks={"closed_period_until": "2026-05-31"}
        )
        self.assertFalse(ok)
        self.assertIn("closed_period_until", reason)

    def test_frozen_until_one_day_after_allowed(self):
        self.assertTrue(
            check_red_line("2026-06-01", now_date="2026-07-01", locks={"frozen_until": "2026-05-31"})[0]
        )

    def test_pcv_until_one_day_after_allowed(self):
        self.assertTrue(
            check_red_line("2026-06-01", now_date="2026-07-01", locks={"pcv_until": "2026-05-31"})[0]
        )

    def test_closed_period_until_one_day_after_allowed(self):
        self.assertTrue(
            check_red_line("2026-06-01", now_date="2026-07-01",
                           locks={"closed_period_until": "2026-05-31"})[0]
        )

    def test_future_dated_with_lock_refused(self):
        ok, reason = check_red_line(
            "2026-08-01", now_date="2026-07-01", locks={"frozen_until": "2026-05-31"}
        )
        self.assertFalse(ok)
        self.assertIn("future", str(reason).lower())

    def test_future_dated_without_lock_allowed(self):
        self.assertTrue(check_red_line("2026-08-01", now_date="2026-07-01", locks={})[0])

    def test_falsy_now_with_live_lock_refused(self):
        # the fail-open: a missing/empty clock must NOT skip the anti-future-dating check when a lock is live
        self.assertFalse(check_red_line("2026-06-15", now_date=None, locks={"frozen_until": "2026-05-31"})[0])
        self.assertFalse(check_red_line("2026-06-15", now_date="", locks={"frozen_until": "2026-05-31"})[0])

    def test_malformed_now_with_live_lock_refused(self):
        self.assertFalse(check_red_line("2026-06-15", now_date="2026-7-1", locks={"frozen_until": "2026-05-31"})[0])

    def test_malformed_boundary_refused(self):
        ok, _ = check_red_line("2026-06-15", now_date="2026-07-01", locks={"frozen_until": "bad-date"})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()


class TestPlanDocnameBinding(unittest.TestCase):
    """A plan is bound to ONE document by name — doc_version equality alone is coincidence-prone
    (two drafts can share a `modified` timestamp), so the glue refuses a plan/doc name mismatch."""

    def test_plan_records_its_docname(self):
        p = new_plan("p1", "prod", "v1", "2026-07-01", docname="SI-1")
        self.assertEqual(p.docname, "SI-1")

    def test_check_docname_match(self):
        from pacioli.plan import check_docname
        p = new_plan("p1", "prod", "v1", "2026-07-01", docname="SI-1")
        self.assertEqual(check_docname(p, "SI-1"), (True, None))
        ok, reason = check_docname(p, "SI-2")
        self.assertFalse(ok)
        self.assertIn("different document", reason)

    def test_check_docname_refuses_blank_on_either_side(self):
        from pacioli.plan import check_docname
        unbound = new_plan("p1", "prod", "v1", "2026-07-01")
        ok, _ = check_docname(unbound, "SI-1")
        self.assertFalse(ok)
        ok, _ = check_docname(new_plan("p1", "prod", "v1", "2026-07-01", docname="SI-1"), "")
        self.assertFalse(ok)


class TestFalsyBoundaryStillRefuses(unittest.TestCase):
    """A present-but-falsy lock boundary must be treated as malformed (refuse), not as 'no lock'
    — matching the module's deny-bias everywhere else. None/absent alone means genuinely no lock."""

    def test_empty_string_boundary_refuses(self):
        ok, _ = check_red_line("2026-05-15", "2026-07-01", {"frozen_until": ""})
        self.assertFalse(ok)

    def test_zero_boundary_refuses(self):
        ok, _ = check_red_line("2026-05-15", "2026-07-01", {"frozen_until": 0})
        self.assertFalse(ok)

    def test_none_boundary_is_genuinely_no_lock(self):
        ok, _ = check_red_line("2026-05-15", "2026-07-01", {"frozen_until": None})
        self.assertTrue(ok)

    def test_absent_key_is_no_lock(self):
        ok, _ = check_red_line("2026-05-15", "2026-07-01", {})
        self.assertTrue(ok)


class TestRedLineDateless(unittest.TestCase):
    """The declared-dateless pass (BOM breadth, 2026-07-21). NO_DATE_FIELD is set ONLY by the
    glue's _posting_date_of, ONLY when SUPPORTED_DOCTYPES declares date_field=None — a
    source-verified pin, never an empty read. See check_red_line's own docstring for the
    three-way source proof that passing it equals ERPNext exactly."""

    def test_sentinel_passes_with_no_locks(self):
        self.assertEqual(check_red_line(NO_DATE_FIELD, "2026-07-01", {}), (True, None))

    def test_sentinel_passes_even_with_live_locks(self):
        # Deliberate, documented: a dateless doctype has no posting date for any boundary to
        # bite on, and ERPNext itself never period-checks it (BOM absent from
        # period_closing_doctypes; check_freezing_date is GL-path-only) — refusing would deny
        # every submit/cancel of the doctype forever while protecting nothing ERPNext protects.
        # In every real flow locks is {} anyway (_locks_for never reads locks for the sentinel);
        # this pins that the answer doesn't secretly depend on that.
        ok, _ = check_red_line(NO_DATE_FIELD, "2026-07-01",
                               {"frozen_until": "2026-12-31", "pcv_until": "2026-12-31",
                                "closed_period_until": "2026-12-31"})
        self.assertTrue(ok)

    def test_sentinel_passes_with_no_now_date(self):
        # The future-dating belt needs a "now" to compare against; with no date on the document
        # there is nothing to compare, so an unreadable clock cannot block a dateless doctype.
        self.assertEqual(check_red_line(NO_DATE_FIELD, "", {}), (True, None))

    def test_empty_date_still_refused_datelessness_is_never_inferred(self):
        # The load-bearing asymmetry: an EMPTY read on a doctype that declares a date field is
        # unverifiable (deny), not dateless (pass) — only the explicit sentinel passes.
        ok, reason = check_red_line("", "2026-07-01", {})
        self.assertFalse(ok)
        self.assertIn("no posting_date", reason)

    def test_sentinel_is_not_a_valid_iso_date(self):
        # Backstop property: if a bug ever leaks the sentinel into a slot that validates ISO
        # shape (get_period_locks' own gate, a lock boundary), it must REFUSE, never parse.
        from pacioli.plan import _is_iso_date
        self.assertFalse(_is_iso_date(NO_DATE_FIELD))

    def test_sentinel_sorts_after_every_iso_date(self):
        # Backstop property two (documented on the constant): an UNBRANCHED call site comparing
        # the sentinel against a date fires loudly ("in the future") rather than silently
        # passing a range check — wrong-but-loud, never wrong-and-quiet. The glue's flag sites
        # branch explicitly; this pins the failure MODE if one ever forgets.
        self.assertGreater(NO_DATE_FIELD, "9999-12-31")

    def test_sentinel_survives_a_store_round_trip_by_equality(self):
        # The Plan's posting_date channel persists through the store's TEXT column — the branch
        # must hold for an equal COPY of the string, never rely on interning/identity.
        rehydrated = "".join(NO_DATE_FIELD)
        self.assertIsNot(rehydrated, NO_DATE_FIELD)
        self.assertEqual(check_red_line(rehydrated, "2026-07-01", {}), (True, None))


class TestCheckOp(unittest.TestCase):
    """The cross-op guard: a plan (and the marker minted against it) authorizes ONE operation."""

    def _plan(self, op="submit"):
        from pacioli.plan import new_plan
        return new_plan(plan_id="p1", target="prod", doc_version="v1",
                        posting_date="2026-07-01", docname="SI-1", op=op)

    def test_matching_op_passes(self):
        from pacioli.plan import check_op
        self.assertEqual(check_op(self._plan("submit"), "submit"), (True, None))
        self.assertEqual(check_op(self._plan("cancel"), "cancel"), (True, None))

    def test_cross_op_refused_both_directions(self):
        from pacioli.plan import check_op
        ok, reason = check_op(self._plan("submit"), "cancel")
        self.assertFalse(ok)
        self.assertIn("does not transfer", reason)
        ok, reason = check_op(self._plan("cancel"), "submit")
        self.assertFalse(ok)

    def test_missing_op_on_either_side_refuses(self):
        from pacioli.plan import check_op
        self.assertFalse(check_op(None, "submit")[0])
        self.assertFalse(check_op(self._plan("submit"), "")[0])
        self.assertFalse(check_op(self._plan(""), "submit")[0])

    def test_default_plan_op_is_submit(self):
        from pacioli.plan import new_plan
        p = new_plan(plan_id="p", target="t", doc_version="v", posting_date="2026-07-01")
        self.assertEqual(p.op, "submit")


class TestCheckDoctype(unittest.TestCase):
    """The security headline: a plan is bound to ONE doctype (mirrors check_op/check_docname) —
    a plan built for Sales Invoice must never authorize a Purchase Invoice submit/cancel."""

    def _plan(self, doctype="Sales Invoice"):
        from pacioli.plan import new_plan
        return new_plan(plan_id="p1", target="prod", doc_version="v1",
                        posting_date="2026-07-01", docname="SI-1", doctype=doctype)

    def test_matching_doctype_passes(self):
        from pacioli.plan import check_doctype
        self.assertEqual(check_doctype(self._plan("Sales Invoice"), "Sales Invoice"), (True, None))
        self.assertEqual(check_doctype(self._plan("Purchase Invoice"), "Purchase Invoice"),
                         (True, None))

    def test_cross_doctype_refused_both_directions(self):
        from pacioli.plan import check_doctype
        ok, reason = check_doctype(self._plan("Sales Invoice"), "Purchase Invoice")
        self.assertFalse(ok)
        self.assertIn("different document type", reason)
        ok, reason = check_doctype(self._plan("Purchase Invoice"), "Sales Invoice")
        self.assertFalse(ok)

    def test_missing_doctype_on_either_side_refuses(self):
        from pacioli.plan import check_doctype
        self.assertFalse(check_doctype(None, "Sales Invoice")[0])
        self.assertFalse(check_doctype(self._plan("Sales Invoice"), "")[0])
        self.assertFalse(check_doctype(self._plan(""), "Sales Invoice")[0])

    def test_default_plan_doctype_is_sales_invoice(self):
        from pacioli.plan import new_plan
        p = new_plan(plan_id="p", target="t", doc_version="v", posting_date="2026-07-01")
        self.assertEqual(p.doctype, "Sales Invoice")
