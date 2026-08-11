# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Prose that ENUMERATES code is a claim, and this pins the ones that can rot.

**Why this file exists.** `erpnext.py` is 11,511 lines of which **778 are code**: 92.7% prose,
measured 2026-08-11. In a product whose entire pitch is that its claims are checkable, the largest
file is almost entirely claims. The behavioural ones ("submit rides the query string", "doc names
are fully quoted", "never sends adv_adj") were already pinned by `test_erpnext.py`. What had no
test at all was prose that **counts or lists things the code defines** — and that is exactly what
had rotted:

  the module header called SUPPORTED_DOCTYPES the "I've been built and tested for these"
  allowlist **"(Sales Invoice, Purchase Invoice)"**. By 2026-08-11 the dict held **51**.

Nobody lied. The parenthetical was true when written and was never revisited, so the file's own
header understated its subject by an order of magnitude. That is the whole failure mode of prose
as documentation: it is written once, at a moment, and then it is load-bearing forever with
nothing checking it.

**The rule these tests enforce:** a docstring may narrate, cite a source, or explain a decision —
none of which can be mechanically checked and all of which are worth keeping. What it may NOT do
is restate, as an enumeration or a count, something the code already knows. Point at the dict.

Scope note, deliberately narrow: this does not try to verify the ~190 provenance claims in that
file ("confirmed by enumerating all 130 fields in quotation.json"). Those cite a fixed upstream
version and cannot rot into a false statement about THIS code. The claims pinned here are the
self-referential ones.
"""
import ast
import re
import unittest
from pathlib import Path

from pacioli import tools
from pacioli.erpnext import SUPPORTED_DOCTYPES

import pacioli.erpnext as erpnext_module

REPO_ROOT = Path(__file__).resolve().parents[3]


def _module_docstring():
    return ast.get_docstring(ast.parse(Path(erpnext_module.__file__).read_text())) or ""


class TestTheHeaderDoesNotEnumerateTheAllowlist(unittest.TestCase):
    """The specific rot that was found, and the shape of it, so it cannot come back."""

    def test_the_allowlist_sentence_carries_no_parenthetical_list(self):
        doc = _module_docstring()
        sentence = re.search(r"allowlist[^.]*", doc)
        self.assertIsNotNone(sentence, "the header must still describe SUPPORTED_DOCTYPES")

        listed = re.findall(r"\(([^)]*)\)", sentence.group(0))
        for group in listed:
            named = [part.strip() for part in group.split(",")]
            hits = [n for n in named if n in SUPPORTED_DOCTYPES]
            self.assertEqual(
                hits, [],
                f"the allowlist sentence enumerates {hits} — a prose list of a dict that has "
                f"{len(SUPPORTED_DOCTYPES)} entries and grows. Point at SUPPORTED_DOCTYPES "
                f"instead; that is exactly how this header came to say 'two' while the dict "
                f"said 'fifty-one'.",
            )

    def test_the_header_still_names_the_dict_so_a_reader_can_find_the_truth(self):
        self.assertIn("SUPPORTED_DOCTYPES", _module_docstring())


class TestPublicCountsMatchTheCode(unittest.TestCase):
    """The counts the READMEs publish are the first numbers anyone reads. They are checkable, so
    they get checked — the same reasoning as `scripts/tests/test_copy_does_not_overclaim.py`,
    applied to arithmetic instead of absolutes.

    These pass today. They exist for the day doctype 52 is added and someone updates one README
    and not the other, which is the shape `feedback_gh-push-sweep-all-copy` already records.
    """

    PUBLIC_COPY = ("README.md", "broker/README.md")

    def _copy(self):
        for relative in self.PUBLIC_COPY:
            path = REPO_ROOT / relative
            self.assertTrue(path.exists(), f"{relative} is missing")
            yield relative, path.read_text()

    def test_every_published_doctype_count_matches_SUPPORTED_DOCTYPES(self):
        expected = len(SUPPORTED_DOCTYPES)
        found = 0
        for relative, text in self._copy():
            for claimed in re.findall(r"(\d+)\s+governed doctypes", text):
                found += 1
                self.assertEqual(
                    int(claimed), expected,
                    f"{relative} claims {claimed} governed doctypes; the code defines {expected}",
                )
        self.assertGreater(found, 0, "no doctype count found in the public copy — if the claim "
                                     "was removed, remove this test deliberately rather than "
                                     "leaving it passing on nothing")

    def test_every_published_tool_count_matches_the_catalog(self):
        expected = len(tools.TOOLS)
        found = 0
        for relative, text in self._copy():
            for claimed in re.findall(r"(\d+)\s+tools\b", text):
                found += 1
                self.assertEqual(
                    int(claimed), expected,
                    f"{relative} claims {claimed} tools; the catalog holds {expected}",
                )
        self.assertGreater(found, 0, "no tool count found in the public copy")

    def test_a_live_proven_subset_claim_never_exceeds_the_whole(self):
        """`48 of the 51 live-proven` — the numerator is a bench fact this suite cannot verify,
        but the DENOMINATOR is code and the subset can never exceed it."""
        total = len(SUPPORTED_DOCTYPES)
        for relative, text in self._copy():
            for proven, claimed_total in re.findall(r"(\d+)\s+of the\s+(\d+)", text):
                self.assertEqual(
                    int(claimed_total), total,
                    f"{relative} says 'of the {claimed_total}'; the code defines {total}")
                self.assertLessEqual(
                    int(proven), total,
                    f"{relative} claims {proven} live-proven of {total} — a subset cannot exceed "
                    f"the whole")


class TestTheDoctypeNarrationIsNotReadAsAnInventory(unittest.TestCase):
    """The header's per-doctype paragraphs are a build log: each was written when that doctype was
    added and describes it as "the fourth", "the fourteenth", and so on. Those ordinals are
    historical and harmless. This pins that the header SAYS so, because a reader who takes the
    narration for a current inventory makes exactly the mistake the allowlist parenthetical made.
    """

    def test_the_header_warns_that_the_narration_is_a_build_log(self):
        doc = _module_docstring()
        self.assertIn("build log", doc)
        self.assertIn("Read the dict", doc)
