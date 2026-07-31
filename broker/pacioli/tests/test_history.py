import unittest
from unittest import mock

from pacioli import history


class FakeClient:
    """``rows`` defaults to ``[]`` when omitted (convenient for the common case), but a value
    passed explicitly — including ``None`` or a non-list — is returned exactly as given.
    Production must handle a garbled read; a fake that quietly launders ``None`` into ``[]``
    could never prove that it does."""
    _UNSET = object()

    def __init__(self, rows=_UNSET, raises=None):
        self.rows = [] if rows is FakeClient._UNSET else rows
        self.raises = raises
        self.calls = 0
        # Every call's arguments. Recording only a COUNT made it impossible for any test in this
        # file to observe WHAT was read: the class below is named "scoped to the doctype it read"
        # and could only ever check the doctype the sentence NAMES. Scoping the read to a
        # different doctype than the sentence claims left both its tests green.
        self.kwargs = []

    def party_amount_history(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        if self.raises:
            raise self.raises
        return self.rows


PI = "Purchase Invoice"
SI = "Sales Invoice"


class TestPartyContext(unittest.TestCase):
    def test_a_read_failure_is_STATED_never_silent(self):
        """The never-silent rail. A plan with nothing added reads as 'normal'."""
        client = FakeClient(raises=RuntimeError("boom"))
        out = history.party_context(client, PI, "C", {"supplier": "ACME",
                                                     "base_grand_total": 10.0})
        self.assertEqual(out["flags"], [])
        self.assertEqual(len(out["context"]), 1)
        self.assertIn("history unavailable", out["context"][0])
        self.assertIn("boom", out["context"][0])

    def test_a_missing_party_is_stated_not_guessed(self):
        client = FakeClient()
        out = history.party_context(client, PI, "C", {"base_grand_total": 10.0})
        self.assertIn("history unavailable", out["context"][0])
        self.assertEqual(client.calls, 0, "no read is attempted without a party")

    def test_an_unconfigured_doctype_is_inert_and_makes_no_claim(self):
        client = FakeClient()
        out = history.party_context(client, "Stock Entry", "C", {"base_grand_total": 1.0})
        self.assertEqual(out, {"context": [], "flags": [], "source": "none", "as_of": None})
        self.assertEqual(client.calls, 0)

    def test_the_candidate_amount_comes_from_the_doctypes_own_field(self):
        """Payment Entry's amount is base_paid_amount. Reading base_grand_total would find None."""
        client = FakeClient(rows=[{"base_paid_amount": 100.0, "posting_date": "2026-01-01"}])
        out = history.party_context(client, "Payment Entry", "C",
                                   {"party": "ACME", "base_paid_amount": 500.0})
        self.assertTrue(any("AMOUNT OUTSIDE RANGE" in f for f in out["flags"]))

    def test_the_read_window_being_hit_is_disclosed(self):
        rows = [{"base_grand_total": 100.0, "posting_date": "2026-01-01"}] * 100
        client = FakeClient(rows=rows)
        out = history.party_context(client, PI, "C",
                                   {"supplier": "ACME", "base_grand_total": 150.0},
                                   window=100)
        self.assertTrue(any("last 100" in line for line in out["context"]))

    def test_last_seen_comes_from_the_newest_row(self):
        client = FakeClient(rows=[
            {"base_grand_total": 100.0, "posting_date": "2026-03-09"},
            {"base_grand_total": 90.0, "posting_date": "2026-01-01"},
        ])
        out = history.party_context(client, PI, "C",
                                   {"supplier": "ACME", "base_grand_total": 100.0})
        self.assertTrue(any("2026-03-09" in line for line in out["context"]))

    def test_the_disclosed_window_reflects_the_caller_supplied_value_not_the_default(self):
        """window=100 equals baseline's own default, so a test that only ever passes 100 could
        never catch a broker that forgets to forward it — the same shape as a bug this project
        shipped before: a test that only passed because two independently-set values happened to
        be equal. This one uses a non-default value and checks BOTH directions."""
        rows = [{"base_grand_total": 100.0, "posting_date": "2026-01-01"}] * 25
        client = FakeClient(rows=rows)
        out = history.party_context(client, PI, "C",
                                   {"supplier": "ACME", "base_grand_total": 150.0},
                                   window=25)
        self.assertTrue(any("last 25" in line for line in out["context"]))
        self.assertFalse(any("last 100" in line for line in out["context"]))

    def test_a_failure_after_the_read_still_returns_a_stated_line_not_a_crash(self):
        """The read itself can succeed and the failure still happen later — in summarizing, in
        the last-seen scan, or inside assess. The plan must never die because of any of it, so
        the WHOLE body downstream of the read is under exception protection, not just the read."""
        client = FakeClient(rows=[{"base_grand_total": 10.0, "posting_date": "2026-01-01"}])
        with mock.patch.object(history.baseline, "assess", side_effect=ValueError("kaboom")):
            out = history.party_context(client, PI, "C",
                                       {"supplier": "ACME", "base_grand_total": 20.0})
        self.assertEqual(out["flags"], [])
        self.assertIn("history unavailable", out["context"][0])
        self.assertIn("kaboom", out["context"][0])

    def test_a_genuinely_empty_history_is_reported_as_novelty(self):
        """A real, successful read that legitimately found zero prior documents. This is the ONE
        case allowed to raise NOVEL COUNTERPARTY — distinguished below from a broken read
        (test_a_none_response.../test_a_non_list_response...) and from unusable rows
        (test_rows_returned_but_none_usable...), neither of which may masquerade as this one."""
        client = FakeClient(rows=[])
        out = history.party_context(client, PI, "C",
                                   {"supplier": "ACME", "base_grand_total": 10.0})
        self.assertTrue(any("NOVEL COUNTERPARTY" in f for f in out["flags"]))

    def test_a_none_response_is_a_failed_read_not_novelty(self):
        """None is not 'zero rows found', it is a broken read. Coalescing it to [] upstream would
        let a garbled response manufacture a false NOVEL COUNTERPARTY alarm on a plan a human is
        about to approve — a fabricated alarm is the same class of lie as a false all-clear."""
        client = FakeClient(rows=None)
        out = history.party_context(client, PI, "C",
                                   {"supplier": "ACME", "base_grand_total": 10.0})
        self.assertEqual(out["flags"], [])
        self.assertIn("history unavailable", out["context"][0])

    def test_a_non_list_response_is_a_failed_read_not_novelty(self):
        client = FakeClient(rows={"unexpected": "shape"})
        out = history.party_context(client, PI, "C",
                                   {"supplier": "ACME", "base_grand_total": 10.0})
        self.assertEqual(out["flags"], [])
        self.assertIn("history unavailable", out["context"][0])

    def test_rows_returned_but_none_usable_is_stated_not_confused_with_novelty(self):
        """Two rows come back, but neither carries a usable amount. There IS prior history here
        (unlike a genuine first document) and nothing was proven about the amount's novelty
        (unlike a real outside-band read) — so this must claim neither."""
        client = FakeClient(rows=[
            {"base_grand_total": None, "posting_date": "2026-01-01"},
            {"base_grand_total": "not-a-number", "posting_date": "2026-01-02"},
        ])
        out = history.party_context(client, PI, "C",
                                   {"supplier": "ACME", "base_grand_total": 10.0})
        self.assertEqual(out["flags"], [])
        self.assertFalse(any("FIRST" in line for line in out["context"]))
        self.assertFalse(any("NOVEL COUNTERPARTY" in line for line in out["context"]))
        self.assertTrue(any("2" in line for line in out["context"]))

    def test_inert_result_lists_are_not_shared_between_calls(self):
        """A shallow copy of a module-level inert dict shares its list objects across every call.
        One caller mutating a returned list would poison every later inert return, process-wide —
        this proves fresh lists are built each time, not copied from shared state."""
        client = FakeClient()
        out1 = history.party_context(client, "Stock Entry", "C", {"base_grand_total": 1.0})
        out1["context"].append("poison")
        out1["flags"].append("poison")
        out2 = history.party_context(client, "Stock Entry", "C", {"base_grand_total": 1.0})
        self.assertEqual(out2["context"], [])
        self.assertEqual(out2["flags"], [])

    def test_sales_invoice_is_live_not_inert_now_that_it_has_an_amount_field(self):
        """Task 6b: a feasibility probe of the live bench found ZERO submitted Purchase Invoices
        and ZERO submitted Payment Entries — the only two doctypes configured with an
        ``amount_field`` at the time — but 37 submitted Sales Invoices across two customers with
        real amount ranges, so the feature's own discrimination could never have been proven
        against real data. Sales Invoice gained ``amount_field`` (base_grand_total) to fix that.
        Before this change Sales Invoice's config resolved with no ``amount_field``, so
        ``_config`` returned ``None`` and every call took the ``_inert()`` branch unconditionally
        — the client was never even called. This proves the opposite now: a real read reaches a
        real assessment (the prior range appears in ``context``, ``source`` is ``"books"``, not
        ``"none"``), and the fake client's read method is actually invoked — none of which the
        inert branch can produce, no matter what the client returns."""
        rows = [
            {"base_grand_total": 200.0, "posting_date": "2026-02-01"},
            {"base_grand_total": 100.0, "posting_date": "2026-01-01"},
        ]
        client = FakeClient(rows=rows)
        out = history.party_context(client, SI, "C",
                                   {"customer": "ACME Corp", "base_grand_total": 150.0})
        self.assertEqual(client.calls, 1, "a live doctype actually performs the read")
        self.assertNotEqual(out, {"context": [], "flags": [], "source": "none", "as_of": None})
        self.assertEqual(out["source"], "books")
        joined = " ".join(out["context"])
        self.assertIn("prior range 100.00 to 200.00", joined)

    def test_a_non_numeric_amount_states_what_is_actually_wrong(self):
        """The field IS present here, just not a number. 'no {field} on this document' would be a
        false description of the actual problem, and Task 6's human reader deserves the true one."""
        client = FakeClient()
        out = history.party_context(client, PI, "C",
                                   {"supplier": "ACME", "base_grand_total": "oops"})
        self.assertNotIn("no base_grand_total on this document", out["context"][0])
        self.assertIn("not a number", out["context"][0])
        self.assertEqual(client.calls, 0)


class TestEnabled(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(history.enabled(env={}))

    def test_on_only_for_exactly_one(self):
        self.assertTrue(history.enabled(env={"PACIOLI_PARTY_HISTORY": "1"}))
        self.assertFalse(history.enabled(env={"PACIOLI_PARTY_HISTORY": "true"}))
        self.assertFalse(history.enabled(env={"PACIOLI_PARTY_HISTORY": "0"}))


class TestContextIsScopedToTheDoctypeItRead(unittest.TestCase):
    """The reader must hand the doctype to the core, or the core cannot qualify its sentences.

    This is the end-to-end guard for the 2026-07-30 defect. The core test proves `assess` uses a
    doctype it is GIVEN; only this one proves the reader actually gives it. Both are needed: the
    bug was in the plumbing, and a core-only test would stay green with the wiring cut.
    """

    def test_novel_context_names_the_doctype_that_was_actually_read(self):
        client = FakeClient(rows=[])
        out = history.party_context(client, "Payment Entry", "C",
                                    {"party": "Ridgeline Cyclery", "base_paid_amount": 500.0})
        joined = " ".join(out["context"])
        self.assertIn("Payment Entry", joined)
        # The read was scoped to Payment Entry; claiming the whole books is a claim it never made.
        self.assertNotIn("FIRST Ridgeline Cyclery document in these books", joined)
        # And the doctype the sentence names must be the doctype actually READ. Without this the
        # class name is a claim it cannot check: scoping the read to something else while still
        # rendering "Payment Entry" was green.
        self.assertEqual(client.kwargs[-1].get("doctype"), "Payment Entry")

    def test_prior_history_context_names_the_doctype_that_was_actually_read(self):
        client = FakeClient(rows=[{"base_grand_total": 100.0, "posting_date": "2026-07-01"},
                                  {"base_grand_total": 200.0, "posting_date": "2026-06-01"},
                                  {"base_grand_total": 150.0, "posting_date": "2026-05-01"}])
        out = history.party_context(client, SI, "C",
                                    {"customer": "Ridgeline Cyclery",
                                     "base_grand_total": 120.0})
        joined = " ".join(out["context"])
        self.assertIn(SI, joined)
        self.assertEqual(client.kwargs[-1].get("doctype"), SI)
