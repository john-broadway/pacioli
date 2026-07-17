# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Bench-free unit tests for the RECONCILE pure core (pacioli.reconcile) — governed payment
reconciliation: preflight-all -> consent -> one reconcile call -> readback-driven outcome.

Mirrors test_cascade.py's fake-effects pattern. No frappe required.

Run: `.venv/bin/python -m pytest pacioli/tests/test_reconcile.py -q` from the broker app root.
"""
import unittest

from pacioli.consent import new_marker
from pacioli.plan import new_plan
from pacioli.reconcile import check_allocation, run_reconcile


def _node(invoice_no="INV1", payment_no="PAY1", invoice_version="vI1", payment_version="vP1",
          invoice_date="2026-04-30", payment_date="2026-05-01", allocated_amount=100.0,
          invoice_outstanding=100.0, payment_unallocated=500.0, company="X",
          invoice_type="Sales Invoice", payment_type="Payment Entry"):
    return {
        "payment_type": payment_type, "payment_no": payment_no, "payment_version": payment_version,
        "payment_date": payment_date, "invoice_type": invoice_type, "invoice_no": invoice_no,
        "invoice_version": invoice_version, "invoice_date": invoice_date,
        "allocated_amount": allocated_amount, "invoice_outstanding": invoice_outstanding,
        "payment_unallocated": payment_unallocated, "company": company,
    }


def _rplan(graph, plan_id="r1", target="acme/Example Co"):
    return new_plan(plan_id=plan_id, target=target, doc_version="v", posting_date="2026-05-01",
                    docname=plan_id, op="reconcile", doctype="Payment Reconciliation", graph=graph)


def _marker(plan_id="r1"):
    tok = "m" * 32
    return new_marker(token=tok, plan_id=plan_id, expires_at=10_000.0), tok


class AnsweredError(Exception):
    """A stand-in for an answered ``ErpnextError`` — carries ``answered=True`` exactly like the
    real transport-taxonomy exception, duck-typed via ``getattr`` so this pure-core test module
    never imports ``pacioli.erpnext``."""

    def __init__(self, message):
        super().__init__(message)
        self.answered = True


class FakeEffects:
    def __init__(self, versions, outstanding=None, unallocated=None, locks=None,
                 reconcile_exc=None, readback=None, readback_fail_on=None, claim_ok=True,
                 companies=None, default_company="X",
                 intent_raises=None, outcome_raise_times=0, outcome_raises=None):
        self.versions = versions            # {name: modified}
        self.companies = companies or {}    # {name: LIVE company}; absent -> default_company
        self.default_company = default_company  # "X" matches _node()'s default (no-drift tests)
        self.outstanding = outstanding or {}  # {invoice_no: live outstanding at PREFLIGHT time}
        self.unallocated = unallocated or {}  # {payment_no: live unallocated at PREFLIGHT time}
        self.locks = locks or {}
        self.reconcile_exc = reconcile_exc  # exception instance effects.reconcile() raises, or None
        self.readback = readback or {}      # {invoice_no: outstanding AFTER the write}
        self.readback_fail_on = readback_fail_on  # invoice_no whose readback raises
        self.claim_ok = claim_ok
        self.claimed = False
        self.reconcile_calls = []           # rows passed to effects.reconcile, each call
        self.readback_calls = []
        self.lock_calls = []                # (company, doctype, posting_date) per locks_for call
        self.receipts = []                  # (kind, body/status)
        self.outcome_results = []           # result dicts as durably recorded
        self.final_marker = "unset"
        # WG-2b (reconcile-path follow-up): inject post-claim record failures.
        self.intent_raises = intent_raises          # exception record_intent raises, or None
        self.outcome_raise_times = outcome_raise_times  # how many leading record_outcome calls raise
        self.outcome_raises = outcome_raises or RuntimeError("outcome write refused")
        self._outcome_calls = 0

    def claim_marker(self, reserved):
        self.claimed = True
        return self.claim_ok

    def current_version(self, doctype, name):
        return self.versions[name]

    def live_company(self, doctype, name):
        return self.companies.get(name, self.default_company)

    def locks_for(self, company, doctype, posting_date):
        self.lock_calls.append((company, doctype, posting_date))
        return self.locks

    def live_outstanding(self, invoice_type, invoice_no):
        return self.outstanding[invoice_no]

    def live_unallocated(self, payment_type, payment_no):
        return self.unallocated[payment_no]

    def record_intent(self, body):
        if self.intent_raises is not None:
            raise self.intent_raises
        self.receipts.append(("intent", body))
        return {"seq": len(self.receipts)}

    def reconcile(self, rows):
        self.reconcile_calls.append(rows)
        if self.reconcile_exc is not None:
            raise self.reconcile_exc
        return {"ok": True}

    def readback_outstanding(self, invoice_type, invoice_no):
        self.readback_calls.append(invoice_no)
        if invoice_no == self.readback_fail_on:
            raise RuntimeError("readback also failed")
        return self.readback[invoice_no]

    def record_outcome(self, intent, status, result, final_marker):
        self._outcome_calls += 1
        if self._outcome_calls <= self.outcome_raise_times:
            raise self.outcome_raises
        self.receipts.append(("outcome", status))
        self.outcome_results.append(result)
        if final_marker != "unset" and final_marker is not None:
            self.final_marker = getattr(final_marker, "state", final_marker)


class CheckAllocationTest(unittest.TestCase):
    def test_positive_within_limits_ok(self):
        self.assertEqual(check_allocation(100.0, 100.0, 500.0), (True, None))

    def test_zero_refused(self):
        ok, reason = check_allocation(0, 100.0, 500.0)
        self.assertFalse(ok)
        self.assertIn("positive", reason.lower())

    def test_negative_refused(self):
        ok, reason = check_allocation(-10, 100.0, 500.0)
        self.assertFalse(ok)
        self.assertIn("positive", reason.lower())

    def test_nan_amount_refused_not_silently_passed(self):
        # NaN defeats EVERY comparison (nan <= 0, nan > ceiling are all False) — without the
        # isfinite guard check_allocation returns (True, None) and the ceiling is bypassed.
        ok, reason = check_allocation(float("nan"), 100.0, 500.0)
        self.assertFalse(ok)
        self.assertIn("finite", reason.lower())

    def test_infinite_amount_refused(self):
        ok, _ = check_allocation(float("inf"), 100.0, 500.0)
        self.assertFalse(ok)

    def test_nan_live_ceilings_refuse_as_unverifiable(self):
        self.assertFalse(check_allocation(10.0, float("nan"), 500.0)[0])
        self.assertFalse(check_allocation(10.0, 100.0, float("nan"))[0])

    def test_none_amount_refused(self):
        self.assertFalse(check_allocation(None, 100.0, 500.0)[0])

    def test_non_numeric_amount_refused(self):
        self.assertFalse(check_allocation("50", 100.0, 500.0)[0])

    def test_bool_amount_refused(self):
        # isinstance(True, int) is True in Python -- must not silently pass as amount=1.
        self.assertFalse(check_allocation(True, 100.0, 500.0)[0])

    def test_over_outstanding_refused(self):
        ok, reason = check_allocation(150.0, 100.0, 500.0)
        self.assertFalse(ok)
        self.assertIn("outstanding", reason.lower())

    def test_over_unallocated_refused(self):
        ok, reason = check_allocation(50.0, 100.0, 30.0)
        self.assertFalse(ok)
        self.assertIn("unallocated", reason.lower())

    def test_none_outstanding_refused(self):
        ok, reason = check_allocation(50.0, None, 500.0)
        self.assertFalse(ok)
        self.assertIn("outstanding", reason.lower())

    def test_none_unallocated_refused(self):
        ok, reason = check_allocation(50.0, 100.0, None)
        self.assertFalse(ok)
        self.assertIn("unallocated", reason.lower())

    def test_within_eps_of_outstanding_allowed(self):
        self.assertTrue(check_allocation(100.005, 100.0, 500.0, eps=0.005)[0])

    def test_just_over_eps_of_outstanding_refused(self):
        self.assertFalse(check_allocation(100.006, 100.0, 500.0, eps=0.005)[0])

    def test_within_eps_of_unallocated_allowed(self):
        self.assertTrue(check_allocation(100.0, 500.0, 100.005, eps=0.005)[0])

    def test_just_over_eps_of_unallocated_refused(self):
        self.assertFalse(check_allocation(100.01, 500.0, 100.004, eps=0.005)[0])


class RunReconcileHappyPathTest(unittest.TestCase):
    def test_single_allocation_committed_marker_consumed_readback_at_expected(self):
        node = _node(allocated_amount=100.0, invoice_outstanding=100.0)
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                          readback={"INV1": 0.0})
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["stage"], "done")
        self.assertEqual(r["total"], 1)
        self.assertEqual(len(r["confirmed"]), 1)
        self.assertEqual(r["confirmed"][0]["outstanding"], 0.0)
        self.assertEqual(r["unconfirmed"], [])
        self.assertEqual(eff.final_marker, "consumed")
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["committed"])

    def test_two_safe_rows_same_invoice_sum_at_ceiling_both_committed_and_confirmed(self):
        # FIX 1: the false-negative this closes -- 400 + 400 against a 1000 outstanding invoice is
        # SAFE (sums to 800, under the ceiling) and must commit with BOTH rows confirmed. Before
        # the fix, each row's own plan-time invoice_outstanding (1000 for both) minus its own
        # allocated_amount (400) gave an `expected` of 600 for both rows, but the real post-write
        # value is 200 (1000 - 400 - 400) -- a per-row-independent readback compare wrongly marked
        # every row unconfirmed even though the settlement landed exactly as intended.
        n1 = _node(invoice_no="INV1", payment_no="PAY1", allocated_amount=400.0,
                   invoice_outstanding=1000.0)
        n2 = _node(invoice_no="INV1", payment_no="PAY2", payment_version="vP2",
                   allocated_amount=400.0, invoice_outstanding=1000.0)
        eff = FakeEffects(
            versions={"INV1": "vI1", "PAY1": "vP1", "PAY2": "vP2"},
            outstanding={"INV1": 1000.0}, unallocated={"PAY1": 2000.0, "PAY2": 2000.0},
            readback={"INV1": 200.0},
        )
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([n1, n2]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["stage"], "done")
        self.assertEqual(len(r["confirmed"]), 2)
        self.assertEqual(r["unconfirmed"], [])
        self.assertEqual([c["outstanding"] for c in r["confirmed"]], [200.0, 200.0])
        self.assertEqual(eff.final_marker, "consumed")
        # Only ONE readback call for the shared invoice, not one per row.
        self.assertEqual(eff.readback_calls, ["INV1"])

    def test_shared_invoice_readback_failure_marks_every_row_against_it_unconfirmed(self):
        n1 = _node(invoice_no="INV1", payment_no="PAY1", allocated_amount=400.0,
                   invoice_outstanding=1000.0)
        n2 = _node(invoice_no="INV1", payment_no="PAY2", payment_version="vP2",
                   allocated_amount=400.0, invoice_outstanding=1000.0)
        eff = FakeEffects(
            versions={"INV1": "vI1", "PAY1": "vP1", "PAY2": "vP2"},
            outstanding={"INV1": 1000.0}, unallocated={"PAY1": 2000.0, "PAY2": 2000.0},
            readback_fail_on="INV1",
        )
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([n1, n2]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["confirmed"], [])
        self.assertEqual(len(r["unconfirmed"]), 2)
        self.assertTrue(all("readback also failed" in row["readback_error"]
                            for row in r["unconfirmed"]))
        # Only ONE readback attempt for the shared invoice, not one per row.
        self.assertEqual(eff.readback_calls, ["INV1"])

    def test_multi_row_all_confirmed_committed(self):
        n1 = _node(invoice_no="INV1", payment_no="PAY1", allocated_amount=40.0,
                   invoice_outstanding=100.0)
        n2 = _node(invoice_no="INV2", payment_no="PAY2", invoice_version="vI2",
                   payment_version="vP2", allocated_amount=60.0, invoice_outstanding=200.0)
        eff = FakeEffects(
            versions={"INV1": "vI1", "PAY1": "vP1", "INV2": "vI2", "PAY2": "vP2"},
            outstanding={"INV1": 100.0, "INV2": 200.0},
            unallocated={"PAY1": 500.0, "PAY2": 500.0},
            readback={"INV1": 60.0, "INV2": 140.0},
        )
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([n1, n2]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["confirmed"]), 2)
        self.assertEqual(r["unconfirmed"], [])
        self.assertEqual(eff.final_marker, "consumed")


class RunReconcilePreflightTest(unittest.TestCase):
    def _clean_effects(self, **overrides):
        base = dict(versions={"INV1": "vI1", "PAY1": "vP1"},
                    outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                    readback={"INV1": 0.0})
        base.update(overrides)
        return FakeEffects(**base)

    def _assert_clean_no_op(self, eff):
        self.assertEqual(eff.receipts, [])
        self.assertFalse(eff.claimed)
        self.assertEqual(eff.final_marker, "unset")

    def test_stale_invoice_refuses_before_any_write(self):
        node = _node()
        eff = self._clean_effects(versions={"INV1": "CHANGED", "PAY1": "vP1"})
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "fresh")
        self.assertIn("stale", r["reason"].lower())
        self.assertEqual(r["stopped_at"]["invoice_no"], "INV1")
        self._assert_clean_no_op(eff)

    def test_stale_payment_refuses_before_any_write(self):
        node = _node()
        eff = self._clean_effects(versions={"INV1": "vI1", "PAY1": "CHANGED"})
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "fresh")
        self.assertEqual(r["stopped_at"]["payment_no"], "PAY1")
        self._assert_clean_no_op(eff)

    def test_closed_books_on_invoice_date_refuses(self):
        # boundary after invoice_date, before payment_date -- isolates the INVOICE-side check.
        node = _node(invoice_date="2026-04-15", payment_date="2026-05-01")
        eff = self._clean_effects(locks={"closed_period_until": "2026-04-20"})
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "red_line")
        self.assertIn("period", r["reason"].lower())
        self._assert_clean_no_op(eff)

    def test_closed_books_on_payment_date_refuses(self):
        # invoice_date clears the lock; payment_date sits inside it -- isolates the PAYMENT-side
        # check (and proves both dates get checked, not just the invoice's).
        node = _node(invoice_date="2026-04-30", payment_date="2026-04-10")
        eff = self._clean_effects(locks={"closed_period_until": "2026-04-20"})
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "red_line")
        self.assertIn("period", r["reason"].lower())
        self._assert_clean_no_op(eff)

    def test_over_allocation_vs_fresh_outstanding_refuses_even_if_disclosed_field_looks_fine(self):
        # THE invariant: the ceiling comes from effects.live_outstanding (the FRESH read), never
        # the node's disclosure-only invoice_outstanding. Here the disclosed field says 100.0 (an
        # agent could declare anything) but the FRESH read says 50.0 -- the broker must refuse
        # against the fresh number, catching what the disclosure would have let through.
        node = _node(allocated_amount=80.0, invoice_outstanding=100.0)
        eff = self._clean_effects(outstanding={"INV1": 50.0})
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "allocation")
        self.assertIn("outstanding", r["reason"].lower())
        self._assert_clean_no_op(eff)

    def test_company_swapped_under_plan_refuses_wrong_books(self):
        # WG-1 TOCTOU: a company swapped via db_set(update_modified=False) leaves `modified`
        # untouched, so freshness passes; only an execute-time LIVE company re-read catches it.
        # Without the belt the closed-books lock is checked against the WRONG (planned) company.
        node = _node(company="X")
        eff = self._clean_effects(companies={"INV1": "Other Corp"})
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "plan")
        self.assertIn("wrong books", r["reason"].lower())
        self.assertIn("Other Corp", r["reason"])
        self._assert_clean_no_op(eff)

    def test_payment_company_swap_and_missing_company_both_refuse(self):
        # the belt re-reads BOTH the invoice's and the payment's live company; a missing company
        # (None != planned) refuses too, deny-biased.
        for companies in ({"PAY1": "Other Corp"}, {"INV1": None}):
            eff = self._clean_effects(companies=companies)
            m, tok = _marker()
            r = run_reconcile(plan=_rplan([_node(company="X")]), marker=m, token=tok,
                              now_epoch=1.0, now_date="2026-05-01", effects=eff)
            self.assertFalse(r["ok"])
            self.assertEqual(r["stage"], "plan")
            self._assert_clean_no_op(eff)

    def test_nan_allocated_amount_refused_ceiling_not_defeated(self):
        # WG-2a: a NaN `allocated_amount` (reachable via a "nan" plan_reconcile arg) must be
        # refused at preflight, never slip past the ceiling — and never reach consent/write.
        node = _node(allocated_amount=float("nan"))
        eff = self._clean_effects()
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "allocation")
        self.assertIn("finite", r["reason"].lower())
        self._assert_clean_no_op(eff)

    def test_over_allocation_vs_fresh_unallocated_refuses(self):
        node = _node(allocated_amount=80.0, payment_unallocated=500.0)
        eff = self._clean_effects(unallocated={"PAY1": 30.0})
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "allocation")
        self.assertIn("unallocated", r["reason"].lower())
        self._assert_clean_no_op(eff)

    def test_cumulative_over_allocation_same_invoice_refuses_even_though_each_row_alone_is_fine(self):
        # FIX 1 (CRITICAL): two rows against the SAME invoice, each individually within the
        # invoice's live outstanding, but their SUM exceeds it -- every row's fresh read sees the
        # same pre-write state, so a per-row-independent check lets 1800 land against 1000.
        n1 = _node(invoice_no="INV1", payment_no="PAY1", allocated_amount=900.0,
                   invoice_outstanding=1000.0)
        n2 = _node(invoice_no="INV1", payment_no="PAY2", payment_version="vP2",
                   allocated_amount=900.0, invoice_outstanding=1000.0)
        eff = FakeEffects(
            versions={"INV1": "vI1", "PAY1": "vP1", "PAY2": "vP2"},
            outstanding={"INV1": 1000.0}, unallocated={"PAY1": 2000.0, "PAY2": 2000.0},
        )
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([n1, n2]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "allocation")
        self.assertIn("outstanding", r["reason"].lower())
        self.assertIn("cumulative", r["reason"].lower())
        self._assert_clean_no_op(eff)

    def test_cumulative_over_allocation_same_payment_refuses_even_though_each_row_alone_is_fine(self):
        # Mirror of the above on the PAYMENT side: two rows against the same payment, each row's
        # own amount within the payment's live unallocated, but the sum overshoots it.
        n1 = _node(invoice_no="INV1", payment_no="PAY1", allocated_amount=900.0,
                   invoice_outstanding=2000.0)
        n2 = _node(invoice_no="INV2", payment_no="PAY1", invoice_version="vI2",
                   allocated_amount=900.0, invoice_outstanding=2000.0)
        eff = FakeEffects(
            versions={"INV1": "vI1", "INV2": "vI2", "PAY1": "vP1"},
            outstanding={"INV1": 2000.0, "INV2": 2000.0}, unallocated={"PAY1": 1000.0},
        )
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([n1, n2]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "allocation")
        self.assertIn("unallocated", r["reason"].lower())
        self.assertIn("cumulative", r["reason"].lower())
        self._assert_clean_no_op(eff)

    def test_cross_company_graph_refuses(self):
        n1 = _node(invoice_no="INV1", payment_no="PAY1", company="X")
        n2 = _node(invoice_no="INV2", payment_no="PAY2", invoice_version="vI2",
                   payment_version="vP2", company="Y")
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1", "INV2": "vI2", "PAY2": "vP2"})
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([n1, n2]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "plan")
        self.assertIn("compan", r["reason"].lower())
        self._assert_clean_no_op(eff)

    def test_empty_graph_refuses(self):
        plan = new_plan(plan_id="r1", target="t", doc_version="", posting_date="2026-05-01",
                        docname="r1", op="reconcile", doctype="Payment Reconciliation", graph=[])
        eff = FakeEffects(versions={})
        m, tok = _marker()
        r = run_reconcile(plan=plan, marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "plan")
        self.assertEqual(r["total"], 0)
        self.assertIn("no allocations", r["reason"].lower())
        self._assert_clean_no_op(eff)


class RunReconcileConsentTest(unittest.TestCase):
    def test_concurrent_marker_cas_loss_refuses(self):
        node = _node()
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                          readback={"INV1": 0.0}, claim_ok=False)
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "consent")
        self.assertIn("concurrent", r["reason"].lower())
        self.assertTrue(eff.claimed)
        # No intent/outcome recorded past a lost CAS -- the marker was never actually claimed for
        # this call, so nothing irreversible was attempted.
        self.assertEqual(eff.receipts, [])
        self.assertEqual(eff.final_marker, "unset")


class RunReconcilePostClaimExceptionRobustnessTest(unittest.TestCase):
    """WG-2b, reconcile-path follow-up: a post-claim exception in the ledger writes
    (``record_intent`` pre-wire, ``record_outcome`` post-wire) must become a structured result,
    never a raw traceback crashing past ``dispatch()``'s narrow catch — the same residual WG-2b
    closed in spine.py/cascade.py. reconcile.py was the ORIGINAL locus of the concrete WG-2a NaN
    crash (``check_allocation`` closed only that specific trigger; this closes the general one)."""

    def test_intent_recording_failure_is_structured_not_a_crash(self):
        node = _node(allocated_amount=100.0, invoice_outstanding=100.0)
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                          readback={"INV1": 0.0},
                          intent_raises=ValueError("intent body not JSON-native"))
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)  # must NOT raise
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "execute")
        self.assertIn("intent", r["reason"].lower())
        # Pre-wire: reconcile() was never called — nothing was sent to the bench.
        self.assertEqual(eff.reconcile_calls, [])

    def test_intent_recording_failure_leaves_marker_unspendable(self):
        node = _node(allocated_amount=100.0, invoice_outstanding=100.0)
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                          readback={"INV1": 0.0},
                          intent_raises=ValueError("intent body not JSON-native"))
        m, tok = _marker()
        run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                      now_date="2026-05-01", effects=eff)
        self.assertTrue(eff.claimed)              # the marker WAS claimed (dead)
        self.assertEqual(eff.final_marker, "unset")  # never released or committed -> not spendable

    def test_outcome_recording_failure_after_commit_degrades_to_unconfirmed(self):
        # A clean, would-be-committed reconcile whose OUTCOME write refuses (post-wire): the act
        # landed on the bench, so this must NOT read as a clean success and must NOT downgrade to
        # "failed" (which wouldn't block a close) — it degrades to "unconfirmed".
        node = _node(allocated_amount=100.0, invoice_outstanding=100.0)
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                          readback={"INV1": 0.0}, outcome_raise_times=1)
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)  # must NOT raise
        self.assertFalse(r["ok"])
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["unconfirmed"])  # the degraded retry recorded unconfirmed
        self.assertEqual(eff.final_marker, "consumed")  # a landed act keeps the grant spent

    def test_outcome_recording_double_failure_never_crashes(self):
        node = _node(allocated_amount=100.0, invoice_outstanding=100.0)
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                          readback={"INV1": 0.0}, outcome_raise_times=2)
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)  # must NOT raise
        self.assertFalse(r["ok"])  # unrecoverable ledger write is never a clean success

    def test_answered_refusal_outcome_failure_preserves_failed_status(self):
        # An answered refusal with zero readback movement is pre-wire-truth "failed" (nothing
        # landed). If its outcome write refuses once, the degraded retry must keep "failed", not
        # inflate a known-clean refusal to "unconfirmed".
        node = _node(allocated_amount=100.0, invoice_outstanding=100.0)
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                          readback={"INV1": 100.0},  # unchanged -> nothing landed
                          reconcile_exc=AnsweredError("HTTP 417: over-allocated"),
                          outcome_raise_times=1)
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["failed"])
        self.assertEqual(eff.final_marker, "live")  # release() frees a proven-clean refusal's grant

    def test_answered_refusal_total_outcome_failure_is_reported_not_a_silent_clean_refusal(self):
        # Redteam verify pass: an answered refusal (status "failed") whose outcome write fails on
        # BOTH the original and the sanitized retry records NO outcome at all — the reason must say
        # so (mirroring spine's not-recorded path), not read like a normal clean refusal. `ok` stays
        # False and the marker's release rolled back with the failed write (stays reserved/dead).
        node = _node(allocated_amount=100.0, invoice_outstanding=100.0)
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                          readback={"INV1": 100.0},  # unchanged -> nothing landed
                          reconcile_exc=AnsweredError("HTTP 417: over-allocated"),
                          outcome_raise_times=2)  # both attempts fail
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)  # must NOT raise
        self.assertFalse(r["ok"])
        self.assertEqual([b for k, b in eff.receipts if k == "outcome"], [])  # nothing recorded
        self.assertIn("could not be durably recorded", r["reason"].lower())
        self.assertIn("unspendable", r["reason"].lower())


class RunReconcileExecuteTest(unittest.TestCase):
    def test_outstanding_dropped_since_plan_still_confirms_against_fresh_read(self):
        # WG-3: outstanding legitimately dropped 100 -> 55 between plan and execute. The planned 40
        # allocation is within the fresh 55 ceiling and lands (55 -> 15). The success baseline must
        # judge against the FRESH read (55 - 40 = 15), not the stale plan-time snapshot 100 (which
        # would expect 60 and falsely mark a correctly-bounded settle "unconfirmed").
        node = _node(allocated_amount=40.0, invoice_outstanding=100.0)  # plan-time snapshot = 100
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 55.0}, unallocated={"PAY1": 500.0},
                          readback={"INV1": 15.0})  # fresh pre-write 55, post-write 15
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["confirmed"]), 1)
        self.assertEqual(r["unconfirmed"], [])
        self.assertEqual(eff.final_marker, "consumed")

    def test_answered_refusal_zero_readback_movement_released_failed(self):
        node = _node(allocated_amount=100.0, invoice_outstanding=100.0)
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                          reconcile_exc=AnsweredError("HTTP 417: over-allocated"),
                          readback={"INV1": 100.0})  # unchanged -- nothing landed
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["confirmed"], [])
        self.assertEqual(eff.final_marker, "live")
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["failed"])

    def test_partial_apply_row0_landed_row1_not_committed_unconfirmed(self):
        n1 = _node(invoice_no="INV1", payment_no="PAY1", allocated_amount=40.0,
                   invoice_outstanding=100.0)
        n2 = _node(invoice_no="INV2", payment_no="PAY2", invoice_version="vI2",
                   payment_version="vP2", allocated_amount=60.0, invoice_outstanding=200.0)
        eff = FakeEffects(
            versions={"INV1": "vI1", "PAY1": "vP1", "INV2": "vI2", "PAY2": "vP2"},
            outstanding={"INV1": 100.0, "INV2": 200.0},
            unallocated={"PAY1": 500.0, "PAY2": 500.0},
            # row0 landed (100-40=60); row1 did NOT (still at pre-write 200, expected 140)
            readback={"INV1": 60.0, "INV2": 200.0},
        )
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([n1, n2]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "execute")
        self.assertEqual([c["invoice_no"] for c in r["confirmed"]], ["INV1"])
        self.assertEqual([c["invoice_no"] for c in r["unconfirmed"]], ["INV2"])
        self.assertEqual(eff.final_marker, "consumed")
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["unconfirmed"])

    def test_no_answer_exception_full_readback_confirm_still_unconfirmed_not_committed(self):
        # JUDGMENT CALL (flagged in the final report): the settle pseudocode in the spec is
        # literal -- `elif confirmed and not unconfirmed and exc is None` -- so a "committed"
        # STATUS requires the reconcile() call to have raised NOTHING at all. A no-answer
        # exception (no `.answered`) is always resolved via the `else` branch regardless of how
        # completely the readback confirms the rows: the MARKER still always ends up spent
        # (`commit()`), matching the constraint "committed (spent) in every ... no-answer case",
        # but the recorded outcome `status` stays "unconfirmed", never "committed" -- the call's
        # own ambiguity is never silently promoted to a clean success.
        node = _node(allocated_amount=100.0, invoice_outstanding=100.0)
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                          reconcile_exc=RuntimeError("connection reset"),  # no .answered attr
                          readback={"INV1": 0.0})  # fully landed per the readback
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["confirmed"]), 1)
        self.assertEqual(r["unconfirmed"], [])
        self.assertEqual(eff.final_marker, "consumed")  # spent -- every no-answer case
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["unconfirmed"])  # never promoted to "committed"

    def test_readback_itself_raises_unconfirmed_marker_spent_readback_error_carried(self):
        node = _node(allocated_amount=100.0, invoice_outstanding=100.0)
        eff = FakeEffects(versions={"INV1": "vI1", "PAY1": "vP1"},
                          outstanding={"INV1": 100.0}, unallocated={"PAY1": 500.0},
                          readback_fail_on="INV1")
        m, tok = _marker()
        r = run_reconcile(plan=_rplan([node]), marker=m, token=tok, now_epoch=1.0,
                          now_date="2026-05-01", effects=eff)
        self.assertFalse(r["ok"])
        self.assertEqual(r["confirmed"], [])
        self.assertEqual(len(r["unconfirmed"]), 1)
        self.assertIn("readback also failed", r["unconfirmed"][0]["readback_error"])
        self.assertEqual(eff.final_marker, "consumed")
        statuses = [body for kind, body in eff.receipts if kind == "outcome"]
        self.assertEqual(statuses, ["unconfirmed"])

    def test_reconcile_rows_built_from_pinned_graph_never_separate_input(self):
        n1 = _node(invoice_no="INV1", payment_no="PAY1", allocated_amount=40.0,
                   invoice_outstanding=100.0)
        n2 = _node(invoice_no="INV2", payment_no="PAY2", invoice_version="vI2",
                   payment_version="vP2", allocated_amount=60.0, invoice_outstanding=200.0,
                   invoice_type="Purchase Invoice", payment_type="Journal Entry")
        eff = FakeEffects(
            versions={"INV1": "vI1", "PAY1": "vP1", "INV2": "vI2", "PAY2": "vP2"},
            outstanding={"INV1": 100.0, "INV2": 200.0},
            unallocated={"PAY1": 500.0, "PAY2": 500.0},
            readback={"INV1": 60.0, "INV2": 140.0},
        )
        m, tok = _marker()
        run_reconcile(plan=_rplan([n1, n2]), marker=m, token=tok, now_epoch=1.0,
                      now_date="2026-05-01", effects=eff)
        self.assertEqual(len(eff.reconcile_calls), 1)
        # P7 (bench-proven 2026-07-09, sealed-lab v16): rows carry SEMANTIC keys —
        # payment_unallocated (the payment's plan-time available) and invoice_outstanding (the
        # invoice's plan-time outstanding) — still sourced ONLY from the pinned graph node fields,
        # never from any other argument this function accepts. The GLUE (erpnext.py reconcile())
        # owns the translation to ERPNext's wire field names; the old wire-ish keys
        # (`amount`/`unreconciled_amount`) had their semantics SWAPPED against the live bench
        # (unreconciled_amount is the PAYMENT's unallocated, not the invoice's outstanding —
        # check_if_advance_entry_modified compares it to the PE's live unallocated_amount).
        self.assertEqual(eff.reconcile_calls[0], [
            {"payment_type": "Payment Entry", "payment_no": "PAY1",
             "invoice_type": "Sales Invoice", "invoice_no": "INV1", "allocated_amount": 40.0,
             "payment_unallocated": 500.0, "invoice_outstanding": 100.0},
            {"payment_type": "Journal Entry", "payment_no": "PAY2",
             "invoice_type": "Purchase Invoice", "invoice_no": "INV2", "allocated_amount": 60.0,
             "payment_unallocated": 500.0, "invoice_outstanding": 200.0},
        ])


if __name__ == "__main__":
    unittest.main()
