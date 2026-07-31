import unittest

from pacioli import baseline


class TestSummarize(unittest.TestCase):
    def test_empty_history_summarizes_to_n_zero_and_no_statistics(self):
        s = baseline.summarize([])
        self.assertEqual(s["n"], 0)
        self.assertIsNone(s["min"])
        self.assertIsNone(s["max"])
        self.assertIsNone(s["p50"])

    def test_p50_is_withheld_below_three_points(self):
        """A median of one or two numbers invites more confidence than it earns."""
        self.assertIsNone(baseline.summarize([100.0])["p50"])
        self.assertIsNone(baseline.summarize([100.0, 200.0])["p50"])
        self.assertIsNotNone(baseline.summarize([100.0, 200.0, 300.0])["p50"])

    def test_p50_of_odd_and_even_counts(self):
        self.assertEqual(baseline.summarize([10.0, 20.0, 30.0])["p50"], 20.0)
        self.assertEqual(baseline.summarize([10.0, 20.0, 30.0, 40.0])["p50"], 25.0)

    def test_non_numeric_and_none_amounts_are_skipped_not_coerced(self):
        """A missing base_ amount must not read as zero and drag the range down."""
        s = baseline.summarize([100.0, None, "x", 300.0, True])
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["min"], 100.0)
        self.assertEqual(s["max"], 300.0)

    def test_summarize_sorts_before_taking_min_max_and_median(self):
        """Task 4 feeds this rows ordered by DATE descending, not by amount. Every other case in
        this file happens to already be nondecreasing, so this is the one input that actually
        exercises the sort: without it, clean[0]/clean[-1] become newest/oldest instead of
        min/max, and the stated range (and the band threshold computed from it) is wrong."""
        s = baseline.summarize([300.0, 100.0, 200.0])
        self.assertEqual(s["min"], 100.0)
        self.assertEqual(s["max"], 300.0)
        self.assertEqual(s["p50"], 200.0)


class TestAssess(unittest.TestCase):
    def test_no_history_raises_NOVEL_COUNTERPARTY(self):
        out = baseline.assess(doctype="Sales Invoice", party="NEWCO SUPPLIES LTD.", amount=48000.0,
                              summary=baseline.summarize([]))
        self.assertEqual(len(out["flags"]), 1)
        self.assertIn("NOVEL COUNTERPARTY", out["flags"][0])
        self.assertTrue(any("NEWCO SUPPLIES LTD." in line for line in out["context"]))

    def test_no_history_claims_no_comparison(self):
        out = baseline.assess(doctype="Sales Invoice", party="NEWCO", amount=1.0, summary=baseline.summarize([]))
        joined = " ".join(out["context"] + out["flags"])
        self.assertNotIn("range", joined.lower())

    def test_within_band_states_context_and_raises_NO_flag(self):
        """Not just 'context is non-empty': the actual numbers a human weighs must be present.
        Deleting the range, the median, or the candidate-amount line must fail this test."""
        out = baseline.assess(doctype="Sales Invoice", party="ACME LTD.", amount=3900.0,
                              summary=baseline.summarize([200.0, 1850.0, 4100.0]))
        self.assertEqual(out["flags"], [])
        joined = " ".join(out["context"]).replace(",", "")
        self.assertIn("200.00", joined)     # prior min, part of the stated range
        self.assertIn("4100.00", joined)    # prior max, part of the stated range
        self.assertIn("1850.00", joined)    # prior median (n>=3)
        self.assertIn("3900.00", joined)    # the candidate amount itself

    def test_a_single_prior_document_is_enough_to_band(self):
        """No n>=3 floor: the band rule is well-defined from one prior document."""
        out = baseline.assess(doctype="Sales Invoice", party="ACME", amount=500.0, summary=baseline.summarize([100.0]))
        self.assertEqual(len(out["flags"]), 1)
        self.assertIn("AMOUNT OUTSIDE RANGE", out["flags"][0])

    def test_outside_band_names_the_multiple_the_max_and_K(self):
        out = baseline.assess(doctype="Sales Invoice", party="ACME", amount=48000.0,
                              summary=baseline.summarize([200.0, 1850.0, 4100.0]))
        flag = out["flags"][0]
        self.assertIn("AMOUNT OUTSIDE RANGE", flag)
        self.assertIn("11.7", flag)      # 48000 / 4100
        self.assertIn("4100", flag.replace(",", ""))
        self.assertIn("2.0", flag)       # K is legible in the message

    def test_exactly_at_the_threshold_does_not_fire(self):
        """Strictly greater than max*K. A boundary that fires is a boundary nobody can predict."""
        out = baseline.assess(doctype="Sales Invoice", party="ACME", amount=200.0, summary=baseline.summarize([100.0]))
        self.assertEqual(out["flags"], [])

    def test_only_the_two_declared_flag_kinds_can_ever_be_emitted(self):
        """Guard against a third flag being added without a decision. Matches on the declared
        prefixes rather than slicing words off the string, so the assertion survives rewording.
        Each case also asserts its exact flag COUNT, and the total across all cases is asserted
        too: the inner loop over flags never executes when nothing fires, so without the count
        assertions this test would pass just as happily against an implementation that emits
        nothing at all."""
        prefixes = ("NOVEL COUNTERPARTY:", "AMOUNT OUTSIDE RANGE:")
        cases = (
            ([], 1.0, 1),                    # no history -> novel
            ([100.0], 500.0, 1),             # 500 > 100*2 -> outside band
            ([100.0], 50.0, 0),              # 50 <= 100*2 -> within band
            ([1.0, 2.0, 3.0], 999.0, 1),      # 999 > 3*2 -> outside band
            ([5.0, 5.0], 5.0, 0),            # 5 <= 5*2 -> within band
        )
        total_flags = 0
        for amounts, amount, expected_count in cases:
            flags = baseline.assess(doctype="Sales Invoice", party="P", amount=amount,
                                    summary=baseline.summarize(amounts))["flags"]
            self.assertEqual(len(flags), expected_count,
                            f"amounts={amounts!r} amount={amount!r}: {flags!r}")
            total_flags += len(flags)
            for flag in flags:
                self.assertTrue(flag.startswith(prefixes),
                                f"undeclared flag kind: {flag!r}")
        self.assertEqual(total_flags, 3, "expected exactly 3 flags across all 5 cases")

    def test_window_hit_is_disclosed_not_hidden(self):
        """A range presented as complete when it covers a window is the same class of lie as a
        cached read presented as live."""
        out = baseline.assess(doctype="Sales Invoice", party="ACME", amount=1.0,
                              summary=baseline.summarize([1.0, 2.0, 3.0]), window_hit=True)
        self.assertTrue(any("last 100" in line or "window" in line.lower()
                            for line in out["context"]))

    def test_last_seen_is_stated_when_known_and_never_flagged(self):
        out = baseline.assess(doctype="Sales Invoice", party="ACME", amount=1.0,
                              summary=baseline.summarize([1.0, 2.0, 3.0]),
                              last_seen="2026-01-04")
        self.assertTrue(any("2026-01-04" in line for line in out["context"]))
        self.assertEqual(out["flags"], [])

    def test_negative_prior_max_does_not_fire_and_states_the_limit(self):
        """ERPNext credit notes and returns carry a NEGATIVE base_grand_total, real data this will
        meet. A flag computed against a negative prior max produces a multiple whose sign
        contradicts its own message (e.g. '0.5x the largest prior amount' while still firing).
        John's ruling: when prior_max <= 0, compute no multiple and fire no band flag; state the
        range and admit the limit instead of asserting something that contradicts itself."""
        out = baseline.assess(doctype="Sales Invoice", party="ACME", amount=-50.0, summary=baseline.summarize([-100.0]))
        self.assertEqual(out["flags"], [])
        self.assertTrue(any("not positive" in line.lower() for line in out["context"]))

    def test_zero_prior_max_does_not_fire_and_states_the_limit(self):
        """prior_max == 0 previously produced an 'infx' multiple. Same ruling as the negative
        case: no band computed, the limit is stated in plain language."""
        out = baseline.assess(doctype="Sales Invoice", party="ACME", amount=5.0, summary=baseline.summarize([0.0]))
        self.assertEqual(out["flags"], [])
        self.assertTrue(any("not positive" in line.lower() for line in out["context"]))

    def test_emitted_strings_carry_no_em_dashes(self):
        """One call shape only would leave the novel-counterparty string, the prior-median line,
        and unavailable() unchecked; a stray em dash in any of those would survive. Loop over all
        four shapes an emitted line can take."""
        shapes = {
            "novel": baseline.assess(doctype="Sales Invoice", party="NEWCO", amount=1.0, summary=baseline.summarize([])),
            "within_band": baseline.assess(doctype="Sales Invoice",
                party="ACME", amount=100.0, summary=baseline.summarize([50.0, 60.0, 70.0])),
            "outside_band": baseline.assess(doctype="Sales Invoice",
                party="ACME", amount=48000.0, summary=baseline.summarize([100.0]),
                last_seen="2026-01-04", window_hit=True),
            "unavailable": baseline.unavailable("read timed out"),
        }
        for name, out in shapes.items():
            for line in out["context"] + out["flags"]:
                with self.subTest(shape=name, line=line):
                    self.assertNotIn("—", line)


class TestVantageFields(unittest.TestCase):
    """Honesty rail 2. These fields exist from day one even though increment 1 has only one possible
    value, because increment 2 introduces a second and a field added later has consumers that
    already assumed it was always live."""

    def test_assess_reports_its_source_and_as_of(self):
        out = baseline.assess(doctype="Sales Invoice", party="ACME", amount=1.0, summary=baseline.summarize([1.0]),
                              as_of="2026-07-30T05:00:00Z")
        self.assertEqual(out["source"], "books")
        self.assertEqual(out["as_of"], "2026-07-30T05:00:00Z")

    def test_source_round_trips_when_passed_non_default_on_the_banded_path(self):
        """No test ever passed a non-default source, so a hardcoded 'source': 'books' literal in
        the return statement would pass every other test in this file. Only a non-default value,
        actually asserted back, proves the field is threaded through rather than hardcoded --
        exactly the increment-2 failure (a cached read claiming it came from the books)."""
        out = baseline.assess(doctype="Sales Invoice", party="ACME", amount=1.0, summary=baseline.summarize([1.0]),
                              source="cache-2026-07-29")
        self.assertEqual(out["source"], "cache-2026-07-29")

    def test_source_round_trips_when_passed_non_default_on_the_novel_path(self):
        """The n==0 branch is a separate return statement; a hardcode there survives independently
        of the banded path above."""
        out = baseline.assess(doctype="Sales Invoice", party="NEWCO", amount=1.0, summary=baseline.summarize([]),
                              source="cache-2026-07-29")
        self.assertEqual(out["source"], "cache-2026-07-29")

    def test_unavailable_reports_source_none_not_books(self):
        """An unavailable read must not claim it came from the books."""
        out = baseline.unavailable("timeout")
        self.assertEqual(out["source"], "none")
        self.assertIsNone(out["as_of"])


class TestUnavailable(unittest.TestCase):
    def test_unavailable_states_a_line_and_never_returns_silence(self):
        out = baseline.unavailable("read timed out")
        self.assertEqual(out["flags"], [])
        self.assertEqual(len(out["context"]), 1)
        self.assertIn("history unavailable", out["context"][0])
        self.assertIn("read timed out", out["context"][0])


class TestContextNamesTheDoctype(unittest.TestCase):
    """The read is doctype-scoped, so the sentence may not claim more than the read supported.

    Live 2026-07-30, party-baselines Task 7 step 5: a Payment Entry plan for Ridgeline Cyclery
    rendered "FIRST Ridgeline Cyclery document in these books. No prior history to compare." while
    Ridgeline had 26 submitted Sales Invoices. The verdict was right for the doctype; the sentence
    was false about the books.
    """

    def test_novel_context_names_the_doctype_rather_than_claiming_the_whole_books(self):
        out = baseline.assess(party="Ridgeline Cyclery", amount=500.0, doctype="Payment Entry",
                              summary=baseline.summarize([]))
        joined = " ".join(out["context"])
        self.assertIn("Payment Entry", joined)
        self.assertNotIn("FIRST Ridgeline Cyclery document in these books", joined)

    def test_prior_history_context_names_the_doctype_too(self):
        out = baseline.assess(party="Ridgeline Cyclery", amount=1500.0, doctype="Sales Invoice",
                              summary=baseline.summarize([1450.0, 2400.0, 1450.0]))
        joined = " ".join(out["context"])
        self.assertIn("Sales Invoice", joined)


class TestBandDetailReachesTheHuman(unittest.TestCase):
    """`_bare_baseline_flag` bars the parameterised text out of `risk_flags` because that list is
    echoed to the calling agent and the full text is a tuning oracle. Its docstring says "the human
    loses nothing: the full text is still in context" — which was FALSE, because the multiple, the
    prior max and K only ever existed in the FLAG. Barring the flag deleted them outright, and the
    human at `pacioli mint` was left with "AMOUNT OUTSIDE RANGE" and no idea by how much.
    """

    def _outside(self):
        return baseline.assess(
            doctype="Sales Invoice", party="Ridgeline Cyclery", amount=7200.0,
            summary=baseline.summarize([1450.0, 2400.0, 1450.0]))

    def test_context_states_the_multiple_the_prior_max_and_K_when_the_band_fires(self):
        joined = " ".join(self._outside()["context"])
        self.assertIn("3.0x", joined)          # 7200 / 2400
        self.assertIn("2,400.00", joined)      # the prior max the band was drawn from
        self.assertIn("K=2.0", joined)         # the rule that fired
        self.assertIn("n=3", joined)

    def test_a_quiet_within_band_amount_adds_no_band_line(self):
        """The line is the explanation of a flag, so it may not appear when nothing fired."""
        out = baseline.assess(doctype="Sales Invoice", party="Two Rivers Bike Co", amount=1450.0,
                              summary=baseline.summarize([1450.0, 2400.0, 1450.0]))
        self.assertEqual(out["flags"], [])
        self.assertNotIn("K=", " ".join(out["context"]))


class TestAudienceSplitLosesNothingToTheHuman(unittest.TestCase):
    """Guard the claim `_bare_baseline_flag`'s docstring actually makes.

    The split is a security decision: the parameterised text is a closed-form tuning oracle, so it
    is barred out of `risk_flags` (agent-visible). That is only acceptable while the same facts
    survive on the human side. This test asserts BOTH halves at once, which is the property the
    docstring asserts and which nothing checked before.
    """

    def test_every_number_barred_from_the_flag_still_appears_in_the_context(self):
        from pacioli.tools import _bare_baseline_flag
        out = baseline.assess(doctype="Sales Invoice", party="Ridgeline Cyclery", amount=7200.0,
                              summary=baseline.summarize([1450.0, 2400.0, 1450.0]))
        barred = _bare_baseline_flag(out["flags"][0], "Ridgeline Cyclery")
        context = " ".join(out["context"])
        for number in ("3.0x", "2,400.00", "K=2.0", "n=3"):
            self.assertNotIn(number, barred, f"{number} leaked to the agent-visible flag")
            self.assertIn(number, context, f"{number} was barred from the flag and lost entirely")


class TestTheseSentencesAreComputedNotRecited(unittest.TestCase):
    """Every test that touched these two lines used ONE fixture, so both were provable by a
    hardcoded literal. Verified 2026-07-30: replacing the band f-string with a fixed string, and
    hardcoding {doctype} to "Sales Invoice", each left the whole 3268-test suite green. A test that
    cannot tell a computation from a recitation is not proof of the computation.
    """

    def test_band_line_is_derived_from_THIS_summary_not_a_remembered_one(self):
        out = baseline.assess(doctype="Purchase Invoice", party="Cobb & Sons", amount=10000.0,
                              summary=baseline.summarize([100.0, 200.0, 300.0, 400.0]))
        joined = " ".join(out["context"])
        self.assertIn("25.0x", joined)       # 10000 / 400, not the 3.0x every other test uses
        self.assertIn("400.00", joined)      # this summary's max
        self.assertIn("n=4", joined)         # this summary's n
        self.assertNotIn("2,400.00", joined)

    def test_band_line_reports_the_K_it_was_actually_given(self):
        """K is a parameter. A recited "K=2.0" would survive every other test in this file."""
        out = baseline.assess(doctype="Sales Invoice", party="Cobb & Sons", amount=10000.0,
                              summary=baseline.summarize([100.0, 200.0, 300.0]), k=3.0)
        joined = " ".join(out["context"])
        self.assertIn("K=3.0", joined)
        self.assertNotIn("K=2.0", joined)

    def test_K_decides_the_band_it_is_not_only_a_label_printed_beside_it(self):
        """K must be the number the band actually FIRED on, not a value rendered next to it.

        The sibling above pins only the printed K, and cannot do more: its fixture (10,000 against
        a prior max of 300) clears the threshold at K=2.0 AND at K=3.0, so routing the comparison
        through the default while still rendering the argument is invisible to it. Mutation-proven
        against exactly that edit — `threshold = prior_max * K_DEFAULT` leaves the sibling green
        and turns this red.

        The failure it permits is a consent line reading `threshold K=3.0` while the band fired at
        2.0: a false sentence at the moment a human is deciding, which is the class this whole
        file exists to close.
        """
        summary = baseline.summarize([100.0, 200.0, 300.0])  # prior max 300 -> 600 at K=2, 900 at K=3
        quiet = baseline.assess(doctype="Sales Invoice", party="Cobb & Sons", amount=700.0,
                                summary=summary, k=3.0)
        loud = baseline.assess(doctype="Sales Invoice", party="Cobb & Sons", amount=700.0,
                               summary=summary, k=2.0)
        self.assertEqual(quiet["flags"], [], "700 is under the 900 threshold K=3.0 sets")
        self.assertNotIn("x the largest prior amount", " ".join(quiet["context"]))
        self.assertTrue(loud["flags"], "700 is over the 600 threshold K=2.0 sets")
        self.assertIn("x the largest prior amount", " ".join(loud["context"]))

    def test_prior_history_line_names_the_doctype_it_was_given_not_sales_invoice(self):
        """The n>0 branch was only ever exercised with Sales Invoice, so hardcoding it passed.
        The n==0 branch's Payment Entry tests cannot catch this: different branch."""
        out = baseline.assess(doctype="Payment Entry", party="Ridgeline Cyclery", amount=120.0,
                              summary=baseline.summarize([100.0, 200.0, 150.0]))
        line = out["context"][0]
        self.assertIn("Payment Entry", line)
        self.assertNotIn("Sales Invoice", line)


class TestTheNovelBranchDoesNotOverclaimEither(unittest.TestCase):
    """The n==0 branch says "FIRST ... in these books" without the "submitted" its own sibling line
    nine lines below carries. The read is `docstatus = 1` ONLY, so a party with forty DRAFT Payment
    Entries gets told this is the first. Same overclaim as the doctype one, on the docstatus axis,
    in the same function, left behind when that one was fixed.
    """

    def _novel(self, doctype="Payment Entry"):
        return baseline.assess(doctype=doctype, party="Ridgeline Cyclery", amount=500.0,
                               summary=baseline.summarize([]))

    def test_novel_context_says_SUBMITTED_because_drafts_were_never_read(self):
        # Assert the FIRST claim is qualified, not that the word appears somewhere in the block.
        # The joined-text version of this passed with the qualifier stripped from exactly the
        # sentence that makes the claim, because the second sentence still carried the word.
        first = self._novel()["context"][0]
        self.assertIn("FIRST submitted", first)
        self.assertNotIn("FIRST Ridgeline Cyclery", first)

    def test_novel_FLAG_names_the_doctype_and_does_not_claim_the_whole_books(self):
        """`flags` is part of assess()'s public contract. It is masked today only because
        _bare_baseline_flag rewrites it before anyone sees it, and nothing tested it."""
        flag = self._novel()["flags"][0]
        self.assertIn("Payment Entry", flag)
        self.assertNotIn("this book's history", flag)

    def test_novel_flag_still_carries_its_kind_label_for_the_audience_split(self):
        """_bare_baseline_flag classifies on this prefix; rewording must not break the split."""
        self.assertTrue(self._novel()["flags"][0].startswith("NOVEL COUNTERPARTY"))
