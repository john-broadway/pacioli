# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""A tool description is a PROMPT, so a false one is a capability bug, not a typo.

**The defect these pin, found 2026-08-11 by an independent review.** `_DOCTYPE_PROP`'s
`description` — the text a model reads to decide what it may ask for — enumerated four doctypes:

    "ERPNext DocType to operate on: 'Sales Invoice', 'Purchase Invoice', 'Payment Entry', or
     'Journal Entry' …"

`SUPPORTED_DOCTYPES` held **51**, and `_resolve_doctype` accepted every one. The string shipped in
five tools' `inputSchema` and five times over in the published LobeHub manifest.

⭐⭐ **This is not a stale comment. It is the product's advertised surface.** An agent told the
answer is four invoice types will never attempt the other 47, so the enumeration silently capped
the usable product at 8% of what it governs — and it capped it hardest for the small local driver
the doorway exists to serve, which has no schema list to check the claim against and must take the
description at its word.

The rule, from this workspace's own law: **say the limit negatively and fix the class.** The count
is derived from the dict, the *refusal* carries the names (where a caller who needs them is
already looking), and no schema description enumerates doctypes.
"""
import json
import re
import unittest
from pathlib import Path

from pacioli import tools
from pacioli.erpnext import SUPPORTED_DOCTYPES

REPO_ROOT = Path(__file__).resolve().parents[3]


def _doctype_description():
    return tools._DOCTYPE_PROP["pacioli_doctype"]["description"]


class TestTheDoctypeDescriptionDoesNotUndersellTheSurface(unittest.TestCase):
    def test_it_does_not_enumerate_doctypes(self):
        """The enumeration IS the defect: any hand-listed subset re-caps the surface."""
        described = _doctype_description()
        named = [d for d in SUPPORTED_DOCTYPES if f"'{d}'" in described]
        self.assertEqual(
            named, ["Sales Invoice"],
            f"the doctype description enumerates {named}. Only the DEFAULT may be named — a list "
            f"of {len(named)} out of {len(SUPPORTED_DOCTYPES)} tells a model the surface stops "
            f"there, and it will never ask for the rest.")

    def test_it_states_the_real_count(self):
        self.assertIn(str(len(SUPPORTED_DOCTYPES)), _doctype_description(),
                      "the description must carry the live count, derived from the dict")

    def test_it_tells_the_caller_not_to_assume_the_common_cases(self):
        """Stated NEGATIVELY, because a model's default assumption is the failure mode."""
        self.assertIn("do not assume", _doctype_description().lower())

    def test_it_points_at_where_the_names_actually_are(self):
        self.assertIn("refus", _doctype_description().lower())

    def test_the_refusal_really_does_name_every_accepted_value(self):
        """Guard-the-guard: the description promises the refusal carries the names. If that were
        false, this would be the same defect moved one hop — a promise pointing at nothing."""
        _, denial = tools._resolve_doctype({"pacioli_doctype": "Not A Real Doctype"})
        self.assertIsNotNone(denial, "an unsupported doctype must be refused")
        text = json.dumps(denial)
        missing = [d for d in SUPPORTED_DOCTYPES if d not in text]
        self.assertEqual(missing, [],
                         f"the refusal omits {len(missing)} accepted doctype(s): {missing[:5]}")

    def test_every_supported_doctype_is_actually_accepted(self):
        """The description says the surface is `len(SUPPORTED_DOCTYPES)` wide. That is only honest
        if the resolver agrees."""
        for doctype in SUPPORTED_DOCTYPES:
            with self.subTest(doctype=doctype):
                resolved, denial = tools._resolve_doctype({"pacioli_doctype": doctype})
                self.assertIsNone(denial, f"{doctype} is in the dict but the resolver refused it")
                self.assertEqual(resolved, doctype)


class TestThePublishedManifestAgreesWithTheCode(unittest.TestCase):
    """`lhm.plugin.json` is GENERATED from the tool surface (`scripts/gen_lobehub_manifest.py`,
    re-run by `scripts/release.sh`) — but nothing checked that the committed copy still matches.
    The four-doctype string was live in the manifest for as long as it was live in the source."""

    MANIFEST = REPO_ROOT / "lhm.plugin.json"

    def test_the_manifest_carries_no_stale_doctype_enumeration(self):
        if not self.MANIFEST.exists():
            self.skipTest(f"{self.MANIFEST} not present")
        text = self.MANIFEST.read_text()

        stale = re.findall(r"DocType to operate on: '[^\"]*", text)
        self.assertEqual(
            stale, [],
            f"the published manifest still carries an enumerated doctype description "
            f"({len(stale)} occurrence(s)). Regenerate it: "
            f"cd broker && .venv/bin/python scripts/gen_lobehub_manifest.py")

    def test_the_manifest_doctype_description_matches_the_code(self):
        if not self.MANIFEST.exists():
            self.skipTest(f"{self.MANIFEST} not present")
        text = self.MANIFEST.read_text()
        if "pacioli_doctype" not in text:
            self.skipTest("manifest carries no pacioli_doctype property")

        self.assertIn(
            _doctype_description(), text,
            "the manifest's doctype description differs from the code's. It is generated, so "
            "regenerate and commit it: cd broker && .venv/bin/python "
            "scripts/gen_lobehub_manifest.py")
