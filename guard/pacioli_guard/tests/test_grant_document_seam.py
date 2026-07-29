# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Bench-free tests for the seam between the pure decision core and the grant DOCUMENT.

Run: `python3 -m unittest pacioli_guard.tests.test_grant_document_seam` from the app root.
No frappe required — these read the shipped DocType JSON as data.

WHY THIS FILE EXISTS
--------------------
`test_scope.py` covers the pure core exhaustively and, by design, imports no frappe. Every
existing test of `RESOURCE_DOCTYPE_WILDCARD` builds its `ApiScope` from a **dict**. That is
correct for testing a decision function and it means nothing on this side of the seam was ever
checked: the core can honor a value that the document feeding it cannot store, and no test in
the suite would notice.

That is not hypothetical. It is the state of the shipped wildcard — see
`test_wildcard_is_storable_in_the_grant_document` below.
"""
import json
import pathlib
import unittest

from pacioli_guard.scope import RESOURCE_DOCTYPE_WILDCARD

_DOCTYPE_DIR = pathlib.Path(__file__).resolve().parents[1] / "scoping" / "doctype"


def _load(doctype_dir_name):
    path = _DOCTYPE_DIR / doctype_dir_name / f"{doctype_dir_name}.json"
    return json.loads(path.read_text())


def _field(doc, fieldname):
    for f in doc.get("fields", []):
        if f.get("fieldname") == fieldname:
            return f
    raise AssertionError(f"{doc.get('name')} has no field {fieldname!r}")


class TestGrantDocumentSeam(unittest.TestCase):
    def test_site_wide_access_is_expressible_through_the_grant_document(self):
        """Site-wide resource access must be sayable by an operator, in the document, or not at all.

        THE DEFECT THIS WAS WRITTEN FOR (2026-07-29): `is_permitted` honored
        `RESOURCE_DOCTYPE_WILDCARD` ("*") in `resource_doctypes`, and the only field that feeds it
        -- `API Key Scope DocType.ref_doctype` -- is a validated **Link to DocType**. frappe's
        `Document._validate_links` runs on insert AND save and walks `get_all_children()`, so child
        rows are validated; `get_invalid_links` skips only falsy values and "*" is truthy. No
        DocType is named "*", so a normal `.save()` raises `LinkValidationError`. The core honored
        a value the document could not store, and every wildcard test built its `ApiScope` from a
        DICT through the pure core, so nothing in the suite crossed the boundary and noticed.

        This is deliberately fix-AGNOSTIC: it asserts the capability, not the mechanism. Either
        route closes it --

          * `ref_doctype` becomes an unvalidated field that can hold "*"; or
          * the parent carries an explicit `allow_all_doctypes` Check.

        -- and a future refactor that swaps one for the other keeps this test meaningful instead of
        forcing someone to edit the assertion to match whatever the code now does.
        """
        parent = _load("api_key_scope")
        parent_fields = {f.get("fieldname"): f for f in parent.get("fields", [])}
        checkbox = parent_fields.get("allow_all_doctypes")
        if checkbox is not None:
            self.assertEqual(
                checkbox.get("fieldtype"),
                "Check",
                "`allow_all_doctypes` must be a Check -- a site-wide grant is a yes/no an auditor "
                "reads at a glance, not a value to be parsed.",
            )
            self.assertIn(
                checkbox.get("default"),
                (0, "0", None),
                "`allow_all_doctypes` must default OFF. A site-wide grant that arrives switched "
                "on during an upgrade is exactly the F1 defect (GHSA-hm86-xvfq-hc58) again.",
            )
            return

        child = _load("api_key_scope_doctype")
        ref = _field(child, "ref_doctype")
        if ref.get("fieldtype") != "Link" or ref.get("ignore_link_validation"):
            return  # an unvalidated field can hold the wildcard; the seam is closed that way

        self.fail(
            f"Site-wide resource access is not expressible through the grant document. "
            f"`is_permitted` honors {RESOURCE_DOCTYPE_WILDCARD!r} in `resource_doctypes`, but "
            f"`ref_doctype` is a validated Link to {ref.get('options')!r} with no "
            f"`ignore_link_validation`, so frappe refuses to store it -- and there is no "
            f"`allow_all_doctypes` Check on the parent either. Add one, or make the field "
            f"unvalidated, or remove the wildcard from the core and correct its comment."
        )

    def test_control_plane_doctypes_all_exist_as_shipped_doctypes(self):
        """`_UNGRANTABLE_DOCTYPES` names the guard's own control plane by string.

        A typo, or a DocType renamed without updating the frozenset, silently reopens the exact
        hole floor-audit F2 closed: a credential that can write its own `API Key Scope` is not
        scoped. The names are compared against `tabDocType.name` at runtime, so nothing catches a
        drifted string except this.
        """
        from pacioli_guard.scope import _UNGRANTABLE_DOCTYPES

        shipped = set()
        for d in _DOCTYPE_DIR.iterdir():
            f = d / f"{d.name}.json"
            if f.is_file():
                shipped.add(json.loads(f.read_text())["name"])
        missing = {n for n in _UNGRANTABLE_DOCTYPES if n not in shipped}
        self.assertEqual(
            missing,
            set(),
            f"control-plane DocTypes named in _UNGRANTABLE_DOCTYPES but not shipped by this app: "
            f"{sorted(missing)}. Shipped: {sorted(shipped)}. A name that matches no real DocType "
            f"denies nothing.",
        )

    def test_verb_checkboxes_cover_every_crud_verb_the_core_knows(self):
        """`_scope_from_doctype` builds `resource_verbs` from `verb_<v>` Check fields.

        If the core learns a fifth verb, or a Check field is renamed, the glue silently drops it
        from the narrowing and that verb becomes ungovernable per-credential.
        """
        from pacioli_guard.scope import _CRUD_VERBS

        parent = _load("api_key_scope")
        names = {f.get("fieldname") for f in parent.get("fields", [])}
        missing = {v for v in _CRUD_VERBS if f"verb_{v}" not in names}
        self.assertEqual(
            missing,
            set(),
            f"`_CRUD_VERBS` knows {sorted(_CRUD_VERBS)} but the API Key Scope document has no "
            f"Check field for: {sorted(missing)}. `_scope_from_doctype` reads `verb_<v>` per "
            f"verb, so a missing field means that verb can never be narrowed.",
        )


if __name__ == "__main__":
    unittest.main()
