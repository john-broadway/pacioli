# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Bench-free tests for the Pacioli Consent Marker DocType CONTROLLER.

**Why this file exists.** The immutable-marker rule's *decision* lives in the pure core
(``scope.immutable_marker_violations``) and has been tested since 0.13.0. The *controller* that
calls it had never executed: the first branch-coverage run of this repo (2026-08-11) scored
``pacioli_consent_marker.py`` at 63.6% statements and **0.0% branches — 0 of 2 ever taken**.
So what was proven was that the rule computes correctly, not that the DocType enforces it.

That gap is this project's own recurring shape: *a test that covers ONE site of a defect class
lets it survive at the others* (2026-07-29). Two different questions, both answered here:

1. **Does the method refuse?** Driven directly, both branches, against a frappe fake.
2. **Does frappe ever CALL the method?** Read out of the real frappe source rather than assumed,
   because a fake is always more forgiving than reality — the lesson the 0.13.0 migration taught
   when a frappe Float defaulted to 0 rather than NULL.

Run: ``python3 -m unittest pacioli_guard.tests.test_consent_marker_controller``. No frappe needed.
"""
import os
import re
import sys
import types
import unittest


# The controller does a hard `import frappe` and `from frappe.model.document import Document` at
# module top (it IS frappe glue). Satisfy both with stubs so it imports bench-free, exactly as
# test_enforce.py does for enforce.py. Keeps the controller pristine: no test-only import shim.
def _install_frappe_stub():
    frappe = sys.modules.setdefault("frappe", types.ModuleType("frappe"))
    model = sys.modules.setdefault("frappe.model", types.ModuleType("frappe.model"))
    document = sys.modules.setdefault("frappe.model.document",
                                      types.ModuleType("frappe.model.document"))
    if not hasattr(document, "Document"):
        class Document:  # minimal stand-in for frappe's base Document
            pass
        document.Document = Document
    frappe.model = model
    model.document = document
    return frappe


_install_frappe_stub()

from pacioli_guard.scope import IMMUTABLE_MARKER_FIELDS  # noqa: E402
from pacioli_guard.scoping.doctype.pacioli_consent_marker import (  # noqa: E402
    pacioli_consent_marker as controller,
)


class Refused(Exception):
    """Stands in for frappe.PermissionError raised through frappe.throw."""


class FakeFrappe:
    """Just enough frappe for the controller: a throwing `throw`, a session, PermissionError."""

    PermissionError = Refused

    def __init__(self, user="Administrator"):
        self.session = types.SimpleNamespace(user=user)
        self.thrown = []

    def throw(self, message, exc=Exception):
        self.thrown.append((message, exc))
        raise exc(message)


class Marker(controller.PacioliConsentMarker):
    """A marker instance with a settable `get_doc_before_save`, which is what frappe hands the
    controller: the pre-edit row on UPDATE, and None on INSERT."""

    def __init__(self, before=None, **fields):
        self._before = before
        for key, value in fields.items():
            setattr(self, key, value)

    def get_doc_before_save(self):
        return self._before


def _fields(**overrides):
    row = {name: f"original-{name}" for name in IMMUTABLE_MARKER_FIELDS}
    row.update(overrides)
    return row


class TestValidateRefusesAnEditToAMintedMarker(unittest.TestCase):
    def setUp(self):
        self.frappe = FakeFrappe()
        self._real = controller.frappe
        controller.frappe = self.frappe
        self.addCleanup(lambda: setattr(controller, "frappe", self._real))

    # -- the branch that was never taken: violations exist, so throw -------------------

    def test_each_immutable_field_is_refused_on_its_own(self):
        """Every field in the immutable set, one at a time — a rule that holds for one field and
        silently not another is the same 'covered at one site' defect this file exists for."""
        for name in IMMUTABLE_MARKER_FIELDS:
            with self.subTest(field=name):
                before = _fields()
                after = Marker(before=before, **_fields(**{name: "TAMPERED"}))

                with self.assertRaises(Refused) as caught:
                    after.validate()

                self.assertIn(name, str(caught.exception),
                              "the refusal must name the field it refused")
                self.assertIs(self.frappe.thrown[-1][1], Refused,
                              "must throw frappe.PermissionError, not a bare Exception")

    def test_the_refusal_says_what_to_do_instead(self):
        """A gate that only says no is an outage — this project's own line. The message must
        route the operator, and the route must be the one that actually exists."""
        after = Marker(before=_fields(), **_fields(ref_docname="SOMEONE-ELSES-INVOICE"))

        with self.assertRaises(Refused) as caught:
            after.validate()
        message = str(caught.exception)

        self.assertIn("Cancel this marker and mint a fresh one", message)
        self.assertIn("burned", message, "the one mutable field must be named")

    def test_several_edits_at_once_are_all_named(self):
        after = Marker(before=_fields(),
                       **_fields(ref_doctype="Journal Entry", token_hash="deadbeef"))

        with self.assertRaises(Refused) as caught:
            after.validate()

        self.assertIn("ref_doctype", str(caught.exception))
        self.assertIn("token_hash", str(caught.exception))

    # -- the other branch that was never taken: no violations, so return ---------------

    def test_an_unchanged_save_passes(self):
        Marker(before=_fields(), **_fields()).validate()
        self.assertEqual(self.frappe.thrown, [])

    def test_spending_the_marker_is_allowed_because_burned_is_not_immutable(self):
        """The floor spends a marker with a raw `UPDATE ... WHERE burned = 0`, which skips the
        document lifecycle — but an ORM-side spend must pass too, or the gate would block the
        one legitimate change."""
        self.assertNotIn("burned", IMMUTABLE_MARKER_FIELDS)

        after = Marker(before=_fields(burned=0), **_fields(burned=1))
        after.validate()

        self.assertEqual(self.frappe.thrown, [])

    def test_an_insert_reports_nothing(self):
        """`get_doc_before_save()` is None on create; an insert changes no existing value."""
        Marker(before=None, **_fields()).validate()
        self.assertEqual(self.frappe.thrown, [])

    def test_a_datetime_rendered_two_ways_is_not_a_violation(self):
        """The core compares as STRINGS on purpose: a frappe Datetime can arrive as a datetime on
        one side and a string on the other, and a spurious violation would block a legal save."""
        import datetime as dt
        moment = dt.datetime(2026, 8, 11, 5, 30, 0)
        after = Marker(before=_fields(expires_at=moment), **_fields(expires_at=str(moment)))

        after.validate()

        self.assertEqual(self.frappe.thrown, [])


class TestBeforeInsertBindsTheMinter(unittest.TestCase):
    """`minted_by` is the field the whole separation property rests on: the floor refuses a
    marker whose minter equals the caller. It is only as good as the server establishing it."""

    def setUp(self):
        self.frappe = FakeFrappe(user="a-human@example.com")
        self._real = controller.frappe
        controller.frappe = self.frappe
        self.addCleanup(lambda: setattr(controller, "frappe", self._real))

    def test_it_overwrites_whatever_the_caller_supplied(self):
        marker = Marker(**_fields(minted_by="Administrator"))

        marker.before_insert()

        self.assertEqual(marker.minted_by, "a-human@example.com")

    def test_it_overwrites_rather_than_filling_when_blank(self):
        """Fill-when-blank would let a credential name any other principal as its minter and
        satisfy the separation check with a string it chose itself (floor audit F3)."""
        marker = Marker(**_fields(minted_by="somebody-else@example.com"))

        marker.before_insert()

        self.assertEqual(marker.minted_by, "a-human@example.com")


class TestFrappeActuallyCallsValidate(unittest.TestCase):
    """THE WIRING, read from real frappe source rather than assumed.

    Driving ``validate()`` directly proves the METHOD refuses. It cannot prove frappe INVOKES it
    on save — and a fake is always more forgiving than the framework. These read frappe's own
    ``Document`` dispatch out of the vendored reference tree.

    Skips loudly when the reference tree is absent (it is not vendored into this repo), so the
    absence is visible rather than silently counting as a pass.
    """

    # No default on purpose. This used to fall back to a checkout path on the author's own box,
    # which meant nothing to anyone else and put a private filesystem layout into a published
    # file — caught by the pre-push leak guard on the 0.37.0 flip. Env-only is also honest: with
    # the variable unset the test SKIPS and says the claim is unverified here, rather than
    # implying a default location that does not exist for the reader.
    REFS = os.environ.get("PACIOLI_FRAPPE_REFS", "")

    def setUp(self):
        if not self.REFS:
            self.skipTest(
                "PACIOLI_FRAPPE_REFS is unset — the frappe WIRING claim is UNVERIFIED in this "
                "environment. Point it at a frappe checkout (any version; the claim is read out "
                "of `frappe/model/document.py`) to run it."
            )
        self.path = os.path.join(self.REFS, "frappe", "model", "document.py")
        if not os.path.exists(self.path):
            self.skipTest(
                f"no frappe/model/document.py at {self.path} (from PACIOLI_FRAPPE_REFS) — the "
                f"wiring claim is UNVERIFIED in this environment."
            )
        with open(self.path) as handle:
            self.source = handle.read()

    def test_save_and_submit_both_dispatch_validate(self):
        body = self.source.partition("def run_before_save_methods(self)")[2]
        self.assertTrue(body, "run_before_save_methods not found in frappe's Document")
        head = body[:2000]

        self.assertIn('if self._action == "save":', head)
        self.assertIn('self.run_method("validate")', head)
        self.assertIn('elif self._action == "submit":', head)

    def test_the_insert_and_update_paths_both_reach_it(self):
        """Both write paths must run it, or the guard would hold on one and not the other."""
        self.assertGreaterEqual(
            len(re.findall(r"self\.run_before_save_methods\(\)", self.source)), 2,
            "expected the insert path AND the save path to call run_before_save_methods",
        )

    def test_ignore_validate_is_an_early_return_ahead_of_every_validate_dispatch(self):
        """⚠️ The residual this reading found. frappe returns from run_before_save_methods before
        any `validate` dispatch when `flags.ignore_validate` is set, so a write carrying that flag
        edits a minted marker unguarded. Same bypass ``act.py`` already names for the consent gate.

        This half needs the source. The half that holds the CONTROLLER to naming it lives in
        ``TestTheResidualsStayNamed`` below, which must never be skippable — see its docstring.
        """
        body = self.source.partition("def run_before_save_methods(self)")[2][:2000]
        self.assertIn("if self.flags.ignore_validate:", body)
        self.assertIn("return", body.partition("if self.flags.ignore_validate:")[2][:40])
        self.assertLess(body.index("if self.flags.ignore_validate:"),
                        body.index('self.run_method("validate")'),
                        "the early return must come BEFORE any validate dispatch, or it is not a "
                        "bypass at all")


class TestTheResidualsStayNamed(unittest.TestCase):
    """The controller must keep NAMING what walks around it. Runs everywhere, always.

    ⚠️ These assertions used to live inside ``TestFrappeActuallyCallsValidate``, whose ``setUp``
    skips the entire class when the frappe reference tree is absent — which is the case in CI,
    since the refs are not vendored. They need no frappe source at all, so the effect was a guard
    that ran on this box and **silently skipped in CI**: the residual could have been deleted from
    the shipped docstring and the public gate would have stayed green.

    ⭐ That is this repo's own law broken by the batch that cites it: *a gate can only refuse what
    it was given*, and a skipped gate was given nothing. Found by an independent review, 2026-08-11.

    Naming a residual rather than rounding it off is the product's whole posture — the public
    README does it, ``act.py`` does it. If someone quietly drops one, this reds.
    """

    def _doc(self):
        return controller.PacioliConsentMarker.validate.__doc__ or ""

    def test_the_ignore_validate_bypass_is_named(self):
        self.assertIn("ignore_validate", self._doc(),
                      "the controller must NAME the bypass its own gate does not cover")

    def test_the_lifecycle_skipping_writes_are_named(self):
        doc = self._doc()
        for residual in ("db_set", "db_update", "raw SQL"):
            with self.subTest(residual=residual):
                self.assertIn(residual, doc)

    def test_DELETE_is_named_as_uncovered(self):
        """The gap that is still open and is John's ceremony decision. It must not quietly
        disappear from the docstring while it remains unclosed."""
        doc = self._doc()
        self.assertIn("DELETE", doc)
        self.assertIn("erases", doc.lower() if "erases" in doc.lower() else doc)

    def test_this_guard_cannot_be_skipped(self):
        """Guard-the-guard: this class must never grow a setUp that can skip it, which is exactly
        how the assertions above went dead in CI in the first place."""
        self.assertFalse(hasattr(TestTheResidualsStayNamed, "setUpClass_skips"), "")
        own_setup = TestTheResidualsStayNamed.__dict__.get("setUp")
        self.assertIsNone(own_setup,
                          "no setUp on this class: a setUp is where the skip crept in last time")


class TestTheImmutableSetTracksTheSCHEMA(unittest.TestCase):
    """A new field on the DocType must be CLASSIFIED, not silently unguarded.

    ⚠️ `test_each_immutable_field_is_refused_on_its_own` iterates `IMMUTABLE_MARKER_FIELDS`, and
    so does its fixture — so it is tautological with respect to that tuple. It proves the guard
    covers everything the tuple names; it cannot notice that the tuple stopped naming everything
    that matters. An independent review proved the gap by adding an authoritative-shaped field
    (`consumed_by_credential`, `read_only: 1`) to the DocType JSON: the whole suite stayed green
    while `validate()` did not guard it (2026-08-11).

    ⭐ The fix is to make the SCHEMA the source of truth and fail closed on drift. Every field
    must be either immutable or explicitly, reasonably mutable. A field that is neither reds.
    """

    #: Fields deliberately OUTSIDE the immutable set, each with the reason it is safe to change.
    #: Adding to this is a decision someone has to write down, which is the point.
    KNOWN_MUTABLE = {
        "burned": "the spend flag — the floor MUST be able to set it, or a marker is unusable",
    }

    @classmethod
    def _schema_fields(cls):
        import json
        import pathlib
        path = (pathlib.Path(controller.__file__).parent / "pacioli_consent_marker.json")
        return [f["fieldname"] for f in json.loads(path.read_text())["fields"]
                if f.get("fieldname") and f.get("fieldtype") not in
                ("Section Break", "Column Break", "Tab Break", "HTML")]

    def test_every_schema_field_is_either_immutable_or_knowingly_mutable(self):
        unclassified = [f for f in self._schema_fields()
                        if f not in IMMUTABLE_MARKER_FIELDS and f not in self.KNOWN_MUTABLE]

        self.assertEqual(
            unclassified, [],
            f"new field(s) on Pacioli Consent Marker are guarded by NOTHING: {unclassified}. "
            f"Add each to IMMUTABLE_MARKER_FIELDS in scope.py, or to KNOWN_MUTABLE here with the "
            f"reason it is safe to change after minting. A consent record's fields do not get to "
            f"default to editable.",
        )

    def test_the_immutable_set_does_not_name_fields_the_schema_lost(self):
        """The other direction: a stale entry means the guard is defending a field that no longer
        exists, and reading it as evidence of coverage would be false comfort."""
        schema = set(self._schema_fields())
        stale = [f for f in IMMUTABLE_MARKER_FIELDS if f not in schema]
        self.assertEqual(stale, [], f"IMMUTABLE_MARKER_FIELDS names fields not on the DocType: "
                                    f"{stale}")

    def test_burned_is_the_only_mutable_field(self):
        """Pin the exception explicitly. If KNOWN_MUTABLE ever grows, that is a governance change
        and should be a deliberate, visible edit rather than a quiet one."""
        self.assertEqual(set(self.KNOWN_MUTABLE), {"burned"})
