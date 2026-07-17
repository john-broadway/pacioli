"""Characterization test — pins the EXACT tool surface + dispatch routing so the doctype-descriptor
refactor (docs/plans/2026-07-08-doctype-descriptor-spine.md) is provably behavior-preserving.

Expectations are INDEPENDENT LITERALS, deliberately NOT derived from `TOOLS` — a snapshot that reads
the thing it tests proves nothing. Any drift in the name-set, a tool's schema signature, or which
doctype a mechanical tool dispatches to fails here loudly. Descriptions are asserted non-empty only
(they are templated by the refactor, by design — see the design doc's "one real fork").
"""

import unittest

from pacioli.erpnext import (
    JOURNAL_ENTRY,
    PAYMENT_ENTRY,
    PURCHASE_INVOICE,
    SALES_INVOICE,
)
from pacioli.tools import TOOLS, tool_names

from pacioli.tests.test_tools import make_broker


# --- the surface, pinned as literals ---------------------------------------------------

_MECHANICAL = {
    # tool name -> (verb, doctype)  — the 20 that fan out per doctype
    "get_sales_invoice": ("get", SALES_INVOICE),
    "get_purchase_invoice": ("get", PURCHASE_INVOICE),
    "get_payment_entry": ("get", PAYMENT_ENTRY),
    "get_journal_entry": ("get", JOURNAL_ENTRY),
    "list_sales_invoices": ("list", SALES_INVOICE),
    "list_purchase_invoices": ("list", PURCHASE_INVOICE),
    "list_payment_entries": ("list", PAYMENT_ENTRY),
    "list_journal_entries": ("list", JOURNAL_ENTRY),
    "submit_sales_invoice": ("submit", SALES_INVOICE),
    "submit_purchase_invoice": ("submit", PURCHASE_INVOICE),
    "submit_payment_entry": ("submit", PAYMENT_ENTRY),
    "submit_journal_entry": ("submit", JOURNAL_ENTRY),
    "cancel_sales_invoice": ("cancel", SALES_INVOICE),
    "cancel_purchase_invoice": ("cancel", PURCHASE_INVOICE),
    "cancel_payment_entry": ("cancel", PAYMENT_ENTRY),
    "cancel_journal_entry": ("cancel", JOURNAL_ENTRY),
    "amend_sales_invoice": ("amend", SALES_INVOICE),
    "amend_purchase_invoice": ("amend", PURCHASE_INVOICE),
    "amend_payment_entry": ("amend", PAYMENT_ENTRY),
    "amend_journal_entry": ("amend", JOURNAL_ENTRY),
}

_GENERIC = {
    "workflow_status",
    "plan_submit",
    "plan_cancel",
    "plan_cascade_cancel",
    "cascade_cancel",
    "plan_reconcile",
    "reconcile",
    "request_workflow_transition",
    "prove_verify",
    "prove_orphans",
}

# verb -> (sorted property keys, required tuple) — uniform across doctypes (verified 2026-07-08)
_VERB_SCHEMA = {
    "get": (["name", "pacioli_target"], ("name",)),
    "list": (["filters", "limit", "pacioli_target"], ()),
    "submit": (["marker", "name", "pacioli_target", "plan_id"], ("name", "plan_id", "marker")),
    "cancel": (["marker", "name", "pacioli_target", "plan_id"], ("name", "plan_id", "marker")),
    "amend": (["name", "pacioli_target"], ("name",)),
}

# verb -> the doctype-generic helper the wrapper must call (names are NOT uniform: list is plural)
_VERB_HELPER = {
    "get": "_get_document",
    "list": "_list_documents",
    "submit": "_submit_document",
    "cancel": "_cancel_document",
    "amend": "_amend_document",
}


class ToolSurfaceCharacterization(unittest.TestCase):
    def test_exact_name_set(self):
        self.assertEqual(set(tool_names()), set(_MECHANICAL) | _GENERIC)
        self.assertEqual(len(TOOLS), 30)
        self.assertEqual(len(tool_names()), len(set(tool_names())), "duplicate tool name")

    def test_every_tool_has_nonempty_description_and_object_schema(self):
        for t in TOOLS:
            self.assertTrue(t["description"], f"{t['name']} has empty description")
            self.assertEqual(t["inputSchema"]["type"], "object", t["name"])

    def test_mechanical_schema_signatures(self):
        by_name = {t["name"]: t["inputSchema"] for t in TOOLS}
        for name, (verb, _doctype) in _MECHANICAL.items():
            schema = by_name[name]
            want_props, want_required = _VERB_SCHEMA[verb]
            self.assertEqual(sorted(schema["properties"]), want_props, f"{name} props")
            self.assertEqual(tuple(schema.get("required", ())), want_required, f"{name} required")
            # mechanical tools route by NAME, never carry a runtime pacioli_doctype selector
            self.assertNotIn("pacioli_doctype", schema["properties"], name)

    def test_mechanical_full_schema_fidelity(self):
        # Pin the FULL inputSchema (incl. sub-property description text) per verb — an agent-facing
        # surface. Guards against a generator silently rewording hints (submit wants a DRAFT +
        # plan_submit id; cancel wants a SUBMITTED doc + plan_cancel id — that distinction is load-
        # bearing for tool selection and must survive any future generation change).
        target = {"pacioli_target": {"type": "string",
                                     "description": "Registry target to route to (omit to use the "
                                                    "default target)."}}
        expect = {
            "get": {"type": "object", "required": ["name"],
                    "properties": {"name": {"type": "string", "description": "Document name."},
                                   **target}},
            "list": {"type": "object",
                     "properties": {"filters": {"type": "array", "items": {"type": "array"},
                                                "description": "Frappe filter triples."},
                                    "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                                              "default": 20}, **target}},
            "submit": {"type": "object", "required": ["name", "plan_id", "marker"],
                       "properties": {"name": {"type": "string", "description": "Draft document name."},
                                      "plan_id": {"type": "string", "description": "From plan_submit."},
                                      "marker": {"type": "string",
                                                 "description": "The single-use consent token a "
                                                                "human handed you."}, **target}},
            "cancel": {"type": "object", "required": ["name", "plan_id", "marker"],
                       "properties": {"name": {"type": "string",
                                               "description": "Submitted document name."},
                                      "plan_id": {"type": "string", "description": "From plan_cancel."},
                                      "marker": {"type": "string",
                                                 "description": "The single-use consent token a "
                                                                "human handed you."}, **target}},
            "amend": {"type": "object", "required": ["name"],
                      "properties": {"name": {"type": "string",
                                              "description": "Cancelled document name."}, **target}},
        }
        by_name = {t["name"]: t["inputSchema"] for t in TOOLS}
        for name, (verb, _doctype) in _MECHANICAL.items():
            self.assertEqual(by_name[name], expect[verb], f"{name} full schema drift")

    def test_no_schema_subdict_is_shared_between_tools(self):
        # aliasing guard: two generated tools must not share one mutable properties dict
        seen = {}
        for t in TOOLS:
            pid = id(t["inputSchema"]["properties"])
            self.assertNotIn(pid, seen,
                             f"{t['name']} shares a properties dict with {seen.get(pid)}")
            seen[pid] = t["name"]

    def test_mechanical_tools_route_to_their_own_doctype(self):
        # The late-binding-closure guard: each wrapper must reach its OWN doctype, not the last one
        # a generation loop happened to bind. Spy on the five generic helpers; dispatch every
        # mechanical tool; assert the (verb, doctype) actually invoked.
        broker, _client, _ = make_broker()
        recorded = {}
        _current = {"name": None}

        def make_spy(verb):
            def _spy(doctype, args):
                recorded[_current["name"]] = (verb, doctype)
                return {"ok": True, "_spied": True}
            return _spy

        for verb, helper in _VERB_HELPER.items():
            setattr(broker, helper, make_spy(verb))
        for name, expected in _MECHANICAL.items():
            _current["name"] = name
            out = broker.dispatch(name, {})
            self.assertTrue(out.get("_spied"), f"{name} did not route through its verb helper")
            self.assertEqual(recorded[name], expected,
                             f"{name} routed to {recorded[name]}, expected {expected}")

    def test_generic_tools_present_and_unrouted_by_name(self):
        names = set(tool_names())
        for g in _GENERIC:
            self.assertIn(g, names)

    def test_descriptor_and_runtime_doctype_tables_agree(self):
        # Belt against a two-table divergence: DESCRIPTORS (drives tool registration, tools.py) and
        # SUPPORTED_DOCTYPES (drives runtime per-doctype config, erpnext.py) must enumerate the SAME
        # doctypes. Add one to either without the other and a generic tool (plan_submit, ...) would
        # accept a doctype with no mechanical submit/cancel tool to consume its plan — an orphaned
        # plan. This test makes that mistake fail loudly at the source.
        from pacioli.erpnext import SUPPORTED_DOCTYPES
        from pacioli.tools import DESCRIPTORS
        self.assertEqual({d.doctype for d in DESCRIPTORS}, set(SUPPORTED_DOCTYPES))


if __name__ == "__main__":
    unittest.main()
