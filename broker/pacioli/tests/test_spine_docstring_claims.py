# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The spine's own docstring, held to the spine's own code.

**Why this file exists, and it is the worst finding of the campaign.** The docstring-claim pass of
2026-08-11 swept eight modules with five regex patterns (counts, superlatives, status words,
versions, enumerations), found one stale enumeration in `erpnext.py`, and reported *"the rest came
back clean"*. An independent review then read the docstrings instead of pattern-matching them and
found **30 stale or false self-referential claims**, 23 of them inside those same eight modules.

The regexes were looking for the wrong shape. The dominant rot in this codebase is not counts. It
is **stale absolutes and stale value-set lists**: "never", "always", "only", ":param x is one of
…", "the four members are …". Those carry no digits and no superlative, so nothing matched them.

⭐⭐ **And the single worst instance was in this module's header, describing CONSENT.** Invariant 4
said *"Failure → the marker is **released** back to `live`"*, flat. The code releases only on an
ANSWERED refusal; on no answer it **spends** the marker, because the write may already be in
motion server-side and releasing would let one human grant initiate a second act. The flip was
deliberate, shipped in 0.10.4, documented in `broker/README.md`, and stated outright by a comment
a hundred lines below the header — which the header still contradicted. **The governance spine's
stated invariant was the opposite of the rule it implements.**

These tests derive their expectations FROM THE CODE, so they cannot be satisfied by editing the
docstring to match a hardcoded list here.
"""
import ast
import inspect
import re
import unittest
from pathlib import Path

from pacioli import spine


def _module_source():
    return Path(spine.__file__).read_text()


def _module_doc():
    return ast.get_docstring(ast.parse(_module_source())) or ""


class TestTheStageListMatchesTheStagesConstructed(unittest.TestCase):
    """`SubmitResult.stage` is a value set a caller switches on. A missing value is a caller
    falling through on an outcome it never heard of — and the one that was missing,
    ``"unconfirmed"``, is the one meaning *the marker is spent and the real state is unknown*."""

    def _documented(self):
        """Read the LIST ONLY, stopping at the ⚠️ note.

        ⚠️ The first version of this read the whole `:param stage:` paragraph — which includes a
        note explaining that ``"unconfirmed"`` had been missing. So deleting the value from the
        list left it present in the note and the test stayed green: **an assertion scoped too wide,
        inside the test written to catch exactly that class.** Third instance this session. Caught
        by mutating the docstring rather than trusting the guard.
        """
        doc = inspect.getdoc(spine.SubmitResult) or ""
        stage_para = doc.partition(":param stage:")[2].partition(":param result:")[0]
        enumeration = stage_para.partition("⚠️")[0]
        return set(re.findall(r'``"(\w+)"``', enumeration))

    def _constructed(self):
        """Every string literal passed in the `stage` position of a SubmitResult(...) call."""
        found = set()
        for node in ast.walk(ast.parse(_module_source())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "SubmitResult"):
                continue
            if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant) \
                    and isinstance(node.args[2].value, str):
                found.add(node.args[2].value)
        return found

    def test_every_constructed_stage_is_documented(self):
        missing = self._constructed() - self._documented()
        self.assertEqual(
            missing, set(),
            f"stage value(s) {sorted(missing)} are constructed by spine.py but absent from "
            f"SubmitResult's :param stage: list. A caller switching on stage falls through on "
            f"them.")

    def test_every_documented_stage_is_actually_constructed(self):
        """The other direction: a documented stage nobody produces is a promise of an outcome
        that never arrives."""
        phantom = self._documented() - self._constructed()
        self.assertEqual(phantom, set(),
                         f"stage value(s) {sorted(phantom)} are documented but never constructed")

    def test_the_guard_is_reading_real_data(self):
        """Guard-the-guard: if either extractor silently returned nothing, both tests above would
        pass vacuously — the exact failure this whole campaign is about."""
        self.assertGreaterEqual(len(self._constructed()), 5)
        self.assertGreaterEqual(len(self._documented()), 5)
        self.assertIn("unconfirmed", self._constructed())


class TestTheEffectsProtocolIsDocumentedInFull(unittest.TestCase):
    """The header documents `effects` as an injected protocol. Anyone implementing it from that
    list gets an AttributeError for any member the list omits — and ``readback`` was omitted, on
    the no-answer path, which is the deny-biased path that matters most."""

    def _called_members(self):
        return {node.attr for node in ast.walk(ast.parse(_module_source()))
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "effects"}

    def _protocol_list(self):
        """The protocol sentence ONLY, stopping before the note that explains what was missing —
        same too-wide-scope trap as the stage list above, and it stayed green the same way."""
        doc = _module_doc()
        after = doc.partition("Side effects are injected via")[2]
        return after.partition("``readback`` was omitted")[0]

    def test_every_effects_member_the_spine_calls_is_documented(self):
        listed = self._protocol_list()
        self.assertTrue(listed, "the protocol sentence must still exist to be checked")
        undocumented = sorted(m for m in self._called_members() if m not in listed)
        self.assertEqual(
            undocumented, [],
            f"spine.py calls effects.{undocumented} but the module docstring's protocol list does "
            f"not name them — an implementer following the docs gets an AttributeError.")

    def test_the_guard_is_reading_real_data(self):
        called = self._called_members()
        self.assertIn("readback", called, "the extractor must see the member that was missing")
        self.assertGreaterEqual(len(called), 5)


class TestTheMarkerDispositionInvariantIsStatedCORRECTLY(unittest.TestCase):
    """Invariant 4 is a governance claim: what happens to a human's consent grant when a write
    fails. It was flatly wrong. These pin the shape of the truth, not one phrasing of it."""

    #: A line that RECORDS the old wrong wording is not a line that MAKES the claim. The header
    #: deliberately quotes what it used to say, and the first version of this test flagged that
    #: quotation — writing about a banned phrasing wrote the banned phrasing into the file, which
    #: is a known trap in this workspace. The exclusion is narrow (past-tense self-identification)
    #: and is itself pinned by `test_the_historical_note_is_still_present` below, so it cannot be
    #: used to smuggle a live claim back in without also removing the record.
    HISTORICAL = ("until 2026-", "used to", "said ")

    def _live_release_claims(self):
        return [line for line in _module_doc().splitlines()
                if "released" in line and "Failure" in line
                and "ANSWERED" not in line
                and not any(marker in line for marker in self.HISTORICAL)]

    def test_the_header_does_not_claim_failure_always_releases(self):
        claims = self._live_release_claims()
        self.assertEqual(
            claims, [],
            f"the header states an unconditional 'Failure -> released': {claims}. Release "
            f"happens ONLY on an answered refusal; a no-answer failure SPENDS the marker.")

    def test_the_historical_note_is_still_present(self):
        """The exclusion above is only safe while the record it excuses still exists. If someone
        deletes the ⚠️ note, the exclusion has nothing left to protect and this reds — so the note
        cannot be quietly dropped to make room for a restored false claim."""
        doc = _module_doc()
        self.assertIn("until 2026-08-11", doc,
                      "the record of what this invariant used to say must survive")
        self.assertTrue(any(m in doc for m in self.HISTORICAL))

    def test_the_header_names_the_no_answer_spend(self):
        doc = _module_doc()
        self.assertIn("SPENT", doc,
                      "the header must say that a no-answer failure spends the marker")
        self.assertIn("answered", doc.lower(),
                      "the header must name the answered/no-answer split that decides it")

    def test_the_code_really_does_split_on_answered(self):
        """Guard-the-guard: pins the behaviour the docstring now describes, so if the CODE ever
        flips back the docstring tests do not quietly become the wrong requirement."""
        source = _module_source()
        self.assertIn('getattr(exc, "answered", False)', source)
        self.assertIn("committed_marker = commit(reserved)", source,
                      "the no-answer path must still commit (spend) the marker")
