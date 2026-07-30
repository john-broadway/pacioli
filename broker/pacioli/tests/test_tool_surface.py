"""Characterization test — pins the EXACT tool surface + dispatch routing so the doctype-descriptor
refactor (docs/plans/2026-07-08-doctype-descriptor-spine.md) is provably behavior-preserving.

Expectations are INDEPENDENT LITERALS, deliberately NOT derived from `TOOLS` — a snapshot that reads
the thing it tests proves nothing. Any drift in the name-set, a tool's schema signature, or which
doctype a mechanical tool dispatches to fails here loudly. Descriptions are asserted non-empty only
(they are templated by the refactor, by design — see the design doc's "one real fork").
"""

import unittest

from pacioli.erpnext import (
    ASSET,
    ASSET_CAPITALIZATION,
    ASSET_MAINTENANCE_LOG,
    ASSET_MOVEMENT,
    ASSET_REPAIR,
    ASSET_VALUE_ADJUSTMENT,
    BANK_GUARANTEE,
    BLANKET_ORDER,
    BOM,
    BOM_CREATOR,
    BUDGET,
    CONTRACT,
    COST_CENTER_ALLOCATION,
    DELIVERY_NOTE,
    DELIVERY_TRIP,
    DUNNING,
    INSTALLATION_NOTE,
    INVOICE_DISCOUNTING,
    JOB_CARD,
    JOURNAL_ENTRY,
    LANDED_COST_VOUCHER,
    MAINTENANCE_SCHEDULE,
    MAINTENANCE_VISIT,
    MATERIAL_REQUEST,
    PACKING_SLIP,
    PAYMENT_ENTRY,
    PAYMENT_ORDER,
    PICK_LIST,
    POS_INVOICE,
    PRODUCTION_PLAN,
    PROJECT_UPDATE,
    PURCHASE_INVOICE,
    PURCHASE_ORDER,
    PURCHASE_RECEIPT,
    QUALITY_INSPECTION,
    QUOTATION,
    REQUEST_FOR_QUOTATION,
    SALES_FORECAST,
    SALES_INVOICE,
    SALES_ORDER,
    SHARE_TRANSFER,
    SHIPMENT,
    STOCK_ENTRY,
    STOCK_RECONCILIATION,
    SUBCONTRACTING_INWARD_ORDER,
    SUBCONTRACTING_ORDER,
    SUBCONTRACTING_RECEIPT,
    SUPPLIER_QUOTATION,
    SUPPLIER_SCORECARD_PERIOD,
    TIMESHEET,
    WORK_ORDER,
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
    "get_sales_order": ("get", SALES_ORDER),
    "list_sales_orders": ("list", SALES_ORDER),
    "submit_sales_order": ("submit", SALES_ORDER),
    "cancel_sales_order": ("cancel", SALES_ORDER),
    "amend_sales_order": ("amend", SALES_ORDER),
    "get_purchase_order": ("get", PURCHASE_ORDER),
    "list_purchase_orders": ("list", PURCHASE_ORDER),
    "submit_purchase_order": ("submit", PURCHASE_ORDER),
    "cancel_purchase_order": ("cancel", PURCHASE_ORDER),
    "amend_purchase_order": ("amend", PURCHASE_ORDER),
    "get_material_request": ("get", MATERIAL_REQUEST),
    "list_material_requests": ("list", MATERIAL_REQUEST),
    "submit_material_request": ("submit", MATERIAL_REQUEST),
    "cancel_material_request": ("cancel", MATERIAL_REQUEST),
    "amend_material_request": ("amend", MATERIAL_REQUEST),
    "get_delivery_note": ("get", DELIVERY_NOTE),
    "list_delivery_notes": ("list", DELIVERY_NOTE),
    "submit_delivery_note": ("submit", DELIVERY_NOTE),
    "cancel_delivery_note": ("cancel", DELIVERY_NOTE),
    "amend_delivery_note": ("amend", DELIVERY_NOTE),
    "get_purchase_receipt": ("get", PURCHASE_RECEIPT),
    "list_purchase_receipts": ("list", PURCHASE_RECEIPT),
    "submit_purchase_receipt": ("submit", PURCHASE_RECEIPT),
    "cancel_purchase_receipt": ("cancel", PURCHASE_RECEIPT),
    "amend_purchase_receipt": ("amend", PURCHASE_RECEIPT),
    "get_stock_entry": ("get", STOCK_ENTRY),
    "list_stock_entries": ("list", STOCK_ENTRY),
    "submit_stock_entry": ("submit", STOCK_ENTRY),
    "cancel_stock_entry": ("cancel", STOCK_ENTRY),
    "amend_stock_entry": ("amend", STOCK_ENTRY),
    "get_supplier_quotation": ("get", SUPPLIER_QUOTATION),
    "list_supplier_quotations": ("list", SUPPLIER_QUOTATION),
    "submit_supplier_quotation": ("submit", SUPPLIER_QUOTATION),
    "cancel_supplier_quotation": ("cancel", SUPPLIER_QUOTATION),
    "amend_supplier_quotation": ("amend", SUPPLIER_QUOTATION),
    "get_quotation": ("get", QUOTATION),
    "list_quotations": ("list", QUOTATION),
    "submit_quotation": ("submit", QUOTATION),
    "cancel_quotation": ("cancel", QUOTATION),
    "amend_quotation": ("amend", QUOTATION),
    "get_pos_invoice": ("get", POS_INVOICE),
    "list_pos_invoices": ("list", POS_INVOICE),
    "submit_pos_invoice": ("submit", POS_INVOICE),
    "cancel_pos_invoice": ("cancel", POS_INVOICE),
    "amend_pos_invoice": ("amend", POS_INVOICE),
    "get_dunning": ("get", DUNNING),
    "list_dunnings": ("list", DUNNING),
    "submit_dunning": ("submit", DUNNING),
    "cancel_dunning": ("cancel", DUNNING),
    "amend_dunning": ("amend", DUNNING),
    "get_stock_reconciliation": ("get", STOCK_RECONCILIATION),
    "list_stock_reconciliations": ("list", STOCK_RECONCILIATION),
    "submit_stock_reconciliation": ("submit", STOCK_RECONCILIATION),
    "cancel_stock_reconciliation": ("cancel", STOCK_RECONCILIATION),
    "amend_stock_reconciliation": ("amend", STOCK_RECONCILIATION),
    "get_landed_cost_voucher": ("get", LANDED_COST_VOUCHER),
    "list_landed_cost_vouchers": ("list", LANDED_COST_VOUCHER),
    "submit_landed_cost_voucher": ("submit", LANDED_COST_VOUCHER),
    "cancel_landed_cost_voucher": ("cancel", LANDED_COST_VOUCHER),
    "amend_landed_cost_voucher": ("amend", LANDED_COST_VOUCHER),
    "get_request_for_quotation": ("get", REQUEST_FOR_QUOTATION),
    "list_request_for_quotations": ("list", REQUEST_FOR_QUOTATION),
    "submit_request_for_quotation": ("submit", REQUEST_FOR_QUOTATION),
    "cancel_request_for_quotation": ("cancel", REQUEST_FOR_QUOTATION),
    "amend_request_for_quotation": ("amend", REQUEST_FOR_QUOTATION),
    "get_blanket_order": ("get", BLANKET_ORDER),
    "list_blanket_orders": ("list", BLANKET_ORDER),
    "submit_blanket_order": ("submit", BLANKET_ORDER),
    "cancel_blanket_order": ("cancel", BLANKET_ORDER),
    "amend_blanket_order": ("amend", BLANKET_ORDER),
    "get_job_card": ("get", JOB_CARD),
    "list_job_cards": ("list", JOB_CARD),
    "submit_job_card": ("submit", JOB_CARD),
    "cancel_job_card": ("cancel", JOB_CARD),
    "amend_job_card": ("amend", JOB_CARD),
    "get_bom": ("get", BOM),
    "list_boms": ("list", BOM),
    "submit_bom": ("submit", BOM),
    "cancel_bom": ("cancel", BOM),
    "amend_bom": ("amend", BOM),
    "get_work_order": ("get", WORK_ORDER),
    "list_work_orders": ("list", WORK_ORDER),
    "submit_work_order": ("submit", WORK_ORDER),
    "cancel_work_order": ("cancel", WORK_ORDER),
    "amend_work_order": ("amend", WORK_ORDER),
    "get_asset": ("get", ASSET),
    "list_assets": ("list", ASSET),
    "submit_asset": ("submit", ASSET),
    "cancel_asset": ("cancel", ASSET),
    "amend_asset": ("amend", ASSET),
    "get_packing_slip": ("get", PACKING_SLIP),
    "list_packing_slips": ("list", PACKING_SLIP),
    "submit_packing_slip": ("submit", PACKING_SLIP),
    "cancel_packing_slip": ("cancel", PACKING_SLIP),
    "amend_packing_slip": ("amend", PACKING_SLIP),
    "get_cost_center_allocation": ("get", COST_CENTER_ALLOCATION),
    "list_cost_center_allocations": ("list", COST_CENTER_ALLOCATION),
    "submit_cost_center_allocation": ("submit", COST_CENTER_ALLOCATION),
    "cancel_cost_center_allocation": ("cancel", COST_CENTER_ALLOCATION),
    "amend_cost_center_allocation": ("amend", COST_CENTER_ALLOCATION),
    "get_supplier_scorecard_period": ("get", SUPPLIER_SCORECARD_PERIOD),
    "list_supplier_scorecard_periods": ("list", SUPPLIER_SCORECARD_PERIOD),
    "submit_supplier_scorecard_period": ("submit", SUPPLIER_SCORECARD_PERIOD),
    "cancel_supplier_scorecard_period": ("cancel", SUPPLIER_SCORECARD_PERIOD),
    "amend_supplier_scorecard_period": ("amend", SUPPLIER_SCORECARD_PERIOD),
    "get_quality_inspection": ("get", QUALITY_INSPECTION),
    "list_quality_inspections": ("list", QUALITY_INSPECTION),
    "submit_quality_inspection": ("submit", QUALITY_INSPECTION),
    "cancel_quality_inspection": ("cancel", QUALITY_INSPECTION),
    "amend_quality_inspection": ("amend", QUALITY_INSPECTION),
    "get_installation_note": ("get", INSTALLATION_NOTE),
    "list_installation_notes": ("list", INSTALLATION_NOTE),
    "submit_installation_note": ("submit", INSTALLATION_NOTE),
    "cancel_installation_note": ("cancel", INSTALLATION_NOTE),
    "amend_installation_note": ("amend", INSTALLATION_NOTE),
    "get_shipment": ("get", SHIPMENT),
    "list_shipments": ("list", SHIPMENT),
    "submit_shipment": ("submit", SHIPMENT),
    "cancel_shipment": ("cancel", SHIPMENT),
    "amend_shipment": ("amend", SHIPMENT),
    "get_sales_forecast": ("get", SALES_FORECAST),
    "list_sales_forecasts": ("list", SALES_FORECAST),
    "submit_sales_forecast": ("submit", SALES_FORECAST),
    "cancel_sales_forecast": ("cancel", SALES_FORECAST),
    "amend_sales_forecast": ("amend", SALES_FORECAST),
    "get_project_update": ("get", PROJECT_UPDATE),
    "list_project_updates": ("list", PROJECT_UPDATE),
    "submit_project_update": ("submit", PROJECT_UPDATE),
    "cancel_project_update": ("cancel", PROJECT_UPDATE),
    "amend_project_update": ("amend", PROJECT_UPDATE),
    "get_maintenance_visit": ("get", MAINTENANCE_VISIT),
    "list_maintenance_visits": ("list", MAINTENANCE_VISIT),
    "submit_maintenance_visit": ("submit", MAINTENANCE_VISIT),
    "cancel_maintenance_visit": ("cancel", MAINTENANCE_VISIT),
    "amend_maintenance_visit": ("amend", MAINTENANCE_VISIT),
    "get_maintenance_schedule": ("get", MAINTENANCE_SCHEDULE),
    "list_maintenance_schedules": ("list", MAINTENANCE_SCHEDULE),
    "submit_maintenance_schedule": ("submit", MAINTENANCE_SCHEDULE),
    "cancel_maintenance_schedule": ("cancel", MAINTENANCE_SCHEDULE),
    "amend_maintenance_schedule": ("amend", MAINTENANCE_SCHEDULE),
    "get_asset_maintenance_log": ("get", ASSET_MAINTENANCE_LOG),
    "list_asset_maintenance_logs": ("list", ASSET_MAINTENANCE_LOG),
    "submit_asset_maintenance_log": ("submit", ASSET_MAINTENANCE_LOG),
    "cancel_asset_maintenance_log": ("cancel", ASSET_MAINTENANCE_LOG),
    "amend_asset_maintenance_log": ("amend", ASSET_MAINTENANCE_LOG),
    "get_bank_guarantee": ("get", BANK_GUARANTEE),
    "list_bank_guarantees": ("list", BANK_GUARANTEE),
    "submit_bank_guarantee": ("submit", BANK_GUARANTEE),
    "cancel_bank_guarantee": ("cancel", BANK_GUARANTEE),
    "amend_bank_guarantee": ("amend", BANK_GUARANTEE),
    "get_asset_movement": ("get", ASSET_MOVEMENT),
    "list_asset_movements": ("list", ASSET_MOVEMENT),
    "submit_asset_movement": ("submit", ASSET_MOVEMENT),
    "cancel_asset_movement": ("cancel", ASSET_MOVEMENT),
    "amend_asset_movement": ("amend", ASSET_MOVEMENT),
    "get_delivery_trip": ("get", DELIVERY_TRIP),
    "list_delivery_trips": ("list", DELIVERY_TRIP),
    "submit_delivery_trip": ("submit", DELIVERY_TRIP),
    "cancel_delivery_trip": ("cancel", DELIVERY_TRIP),
    "amend_delivery_trip": ("amend", DELIVERY_TRIP),
    "get_asset_value_adjustment": ("get", ASSET_VALUE_ADJUSTMENT),
    "list_asset_value_adjustments": ("list", ASSET_VALUE_ADJUSTMENT),
    "submit_asset_value_adjustment": ("submit", ASSET_VALUE_ADJUSTMENT),
    "cancel_asset_value_adjustment": ("cancel", ASSET_VALUE_ADJUSTMENT),
    "amend_asset_value_adjustment": ("amend", ASSET_VALUE_ADJUSTMENT),
    "get_payment_order": ("get", PAYMENT_ORDER),
    "list_payment_orders": ("list", PAYMENT_ORDER),
    "submit_payment_order": ("submit", PAYMENT_ORDER),
    "cancel_payment_order": ("cancel", PAYMENT_ORDER),
    "amend_payment_order": ("amend", PAYMENT_ORDER),
    "get_share_transfer": ("get", SHARE_TRANSFER),
    "list_share_transfers": ("list", SHARE_TRANSFER),
    "submit_share_transfer": ("submit", SHARE_TRANSFER),
    "cancel_share_transfer": ("cancel", SHARE_TRANSFER),
    "amend_share_transfer": ("amend", SHARE_TRANSFER),
    "get_bom_creator": ("get", BOM_CREATOR),
    "list_bom_creators": ("list", BOM_CREATOR),
    "submit_bom_creator": ("submit", BOM_CREATOR),
    "cancel_bom_creator": ("cancel", BOM_CREATOR),
    "amend_bom_creator": ("amend", BOM_CREATOR),
    "get_budget": ("get", BUDGET),
    "list_budgets": ("list", BUDGET),
    "submit_budget": ("submit", BUDGET),
    "cancel_budget": ("cancel", BUDGET),
    "amend_budget": ("amend", BUDGET),
    "get_timesheet": ("get", TIMESHEET),
    "list_timesheets": ("list", TIMESHEET),
    "submit_timesheet": ("submit", TIMESHEET),
    "cancel_timesheet": ("cancel", TIMESHEET),
    "amend_timesheet": ("amend", TIMESHEET),
    "get_contract": ("get", CONTRACT),
    "list_contracts": ("list", CONTRACT),
    "submit_contract": ("submit", CONTRACT),
    "cancel_contract": ("cancel", CONTRACT),
    "amend_contract": ("amend", CONTRACT),
    "get_pick_list": ("get", PICK_LIST),
    "list_pick_lists": ("list", PICK_LIST),
    "submit_pick_list": ("submit", PICK_LIST),
    "cancel_pick_list": ("cancel", PICK_LIST),
    "amend_pick_list": ("amend", PICK_LIST),
    "get_asset_repair": ("get", ASSET_REPAIR),
    "list_asset_repairs": ("list", ASSET_REPAIR),
    "submit_asset_repair": ("submit", ASSET_REPAIR),
    "cancel_asset_repair": ("cancel", ASSET_REPAIR),
    "amend_asset_repair": ("amend", ASSET_REPAIR),
    "get_invoice_discounting": ("get", INVOICE_DISCOUNTING),
    "list_invoice_discountings": ("list", INVOICE_DISCOUNTING),
    "submit_invoice_discounting": ("submit", INVOICE_DISCOUNTING),
    "cancel_invoice_discounting": ("cancel", INVOICE_DISCOUNTING),
    "amend_invoice_discounting": ("amend", INVOICE_DISCOUNTING),
    "get_asset_capitalization": ("get", ASSET_CAPITALIZATION),
    "list_asset_capitalizations": ("list", ASSET_CAPITALIZATION),
    "submit_asset_capitalization": ("submit", ASSET_CAPITALIZATION),
    "cancel_asset_capitalization": ("cancel", ASSET_CAPITALIZATION),
    "amend_asset_capitalization": ("amend", ASSET_CAPITALIZATION),
    "get_production_plan": ("get", PRODUCTION_PLAN),
    "list_production_plans": ("list", PRODUCTION_PLAN),
    "submit_production_plan": ("submit", PRODUCTION_PLAN),
    "cancel_production_plan": ("cancel", PRODUCTION_PLAN),
    "amend_production_plan": ("amend", PRODUCTION_PLAN),
    "get_subcontracting_order": ("get", SUBCONTRACTING_ORDER),
    "list_subcontracting_orders": ("list", SUBCONTRACTING_ORDER),
    "submit_subcontracting_order": ("submit", SUBCONTRACTING_ORDER),
    "cancel_subcontracting_order": ("cancel", SUBCONTRACTING_ORDER),
    "amend_subcontracting_order": ("amend", SUBCONTRACTING_ORDER),
    "get_subcontracting_inward_order": ("get", SUBCONTRACTING_INWARD_ORDER),
    "list_subcontracting_inward_orders": ("list", SUBCONTRACTING_INWARD_ORDER),
    "submit_subcontracting_inward_order": ("submit", SUBCONTRACTING_INWARD_ORDER),
    "cancel_subcontracting_inward_order": ("cancel", SUBCONTRACTING_INWARD_ORDER),
    "amend_subcontracting_inward_order": ("amend", SUBCONTRACTING_INWARD_ORDER),
    "get_subcontracting_receipt": ("get", SUBCONTRACTING_RECEIPT),
    "list_subcontracting_receipts": ("list", SUBCONTRACTING_RECEIPT),
    "submit_subcontracting_receipt": ("submit", SUBCONTRACTING_RECEIPT),
    "cancel_subcontracting_receipt": ("cancel", SUBCONTRACTING_RECEIPT),
    "amend_subcontracting_receipt": ("amend", SUBCONTRACTING_RECEIPT),
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
    # `consent_token` (2026-07-25) is OPTIONAL, never required: it carries the floor-side consent
    # marker for benches running pacioli-guard with `require_consent` on. Required-tuple
    # deliberately unchanged — making it mandatory would break every bench that has not turned the
    # gate on, and the gate is opt-in by design.
    "submit": (["consent_token", "marker", "name", "pacioli_target", "plan_id"],
               ("name", "plan_id", "marker")),
    "cancel": (["consent_token", "marker", "name", "pacioli_target", "plan_id"],
               ("name", "plan_id", "marker")),
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
        self.assertEqual(len(TOOLS), 265)
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
                                                                "human handed you."},
                                      "consent_token": {
                                          "type": "string",
                                          "description": (
                                              "Optional. The floor-side consent marker "
                                              "(`API Key Scope.require_consent`), minted on "
                                              "the books box by a different principal for this "
                                              "exact document and act, and spent on use. Required "
                                              "by benches that enforce consent at the floor; "
                                              "harmless on benches that do not. See the guard's "
                                              "SECURITY.md for supported versions.")}, **target}},
            "cancel": {"type": "object", "required": ["name", "plan_id", "marker"],
                       "properties": {"name": {"type": "string",
                                               "description": "Submitted document name."},
                                      "plan_id": {"type": "string", "description": "From plan_cancel."},
                                      "marker": {"type": "string",
                                                 "description": "The single-use consent token a "
                                                                "human handed you."},
                                      "consent_token": {
                                          "type": "string",
                                          "description": (
                                              "Optional. The floor-side consent marker "
                                              "(`API Key Scope.require_consent`), minted on "
                                              "the books box by a different principal for this "
                                              "exact document and act, and spent on use. Required "
                                              "by benches that enforce consent at the floor; "
                                              "harmless on benches that do not. See the guard's "
                                              "SECURITY.md for supported versions.")}, **target}},
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


class TestDeclaredLimitBoundsAreActuallyEnforced(unittest.TestCase):
    """Redteam 2026-07-26. Every `list_*` publishes `{"minimum": 1, "maximum": 200}` and nothing
    enforced it: the MCP SDK does not validate `inputSchema` and the A2A door passes params through
    as given, so the declaration was a promise with no keeper.

    `limit=0` was the sharp one. It reached frappe as `limit_page_length="0"`, which this very
    client uses deliberately elsewhere to mean ALL ROWS — so a tool whose schema says the minimum
    is 1 became an unbounded dump of the doctype.
    """

    def test_zero_does_not_become_unbounded(self):
        from pacioli.tools import LIMIT_MIN, _bounded_limit
        self.assertEqual(_bounded_limit(0), LIMIT_MIN)

    def test_negative_and_huge_are_clamped_to_the_declared_range(self):
        from pacioli.tools import LIMIT_MAX, LIMIT_MIN, _bounded_limit
        self.assertEqual(_bounded_limit(-5), LIMIT_MIN)
        self.assertEqual(_bounded_limit(10_000_000), LIMIT_MAX)
        self.assertEqual(_bounded_limit(200), LIMIT_MAX)

    def test_a_non_integer_falls_back_instead_of_raising_out_of_dispatch(self):
        # `int(args.get("limit"))` raised ValueError/TypeError straight out of dispatch, which
        # catches neither — a traceback instead of a structured refusal, and no receipt.
        from pacioli.tools import LIMIT_DEFAULT, _bounded_limit
        for bad in ("abc", None, {}, [], object()):
            self.assertEqual(_bounded_limit(bad), LIMIT_DEFAULT, repr(bad))

    def test_an_ordinary_value_is_untouched(self):
        from pacioli.tools import _bounded_limit
        self.assertEqual(_bounded_limit(50), 50)
        self.assertEqual(_bounded_limit("50"), 50)


class TestMalformedArgumentsGetAStructuredRefusalNotATraceback(unittest.TestCase):
    """Redteam 2026-07-26. `dispatch`'s own docstring promises "a governed denial is a structured
    answer (stage + reason), never a traceback", and `a2a.py` claims hostile enumeration "is a
    ledger entry, not an invisible transport error". Neither held for malformed arguments.

    Neither door validates `inputSchema` — the MCP SDK does not, and the A2A door passes `params`
    through as given — so schema-illegal-but-JSON-legal values reach the handlers. `dispatch` caught
    only ErpnextError, RegistryError, sqlite3.OperationalError and StoreCorruptError. Real escapes:

      * a non-string `plan_id` -> `sqlite3.ProgrammingError`, which is NOT an OperationalError
      * a non-string `expected_head` -> `TypeError` from `hmac.compare_digest`

    Both raised out of dispatch as tracebacks, with no receipt behind them.
    """

    def _broker(self):
        from pacioli.tests.test_seal_gate import make_broker
        return make_broker()[0]

    def test_a_non_string_plan_id_is_a_structured_deny(self):
        out = self._broker().dispatch("submit_sales_invoice",
                                      {"name": "SI-1", "plan_id": {"not": "a string"},
                                       "marker": "x"})
        self.assertFalse(out["ok"])
        self.assertIn("stage", out)
        self.assertIsInstance(out.get("reason"), str)

    def test_a_non_string_expected_head_is_a_structured_deny(self):
        out = self._broker().dispatch("prove_verify", {"expected_head": 12345})
        self.assertFalse(out["ok"])
        self.assertIn("stage", out)

    def test_the_reason_names_the_type_and_carries_no_traceback(self):
        out = self._broker().dispatch("prove_verify", {"expected_head": 12345})
        self.assertNotIn("Traceback", out["reason"])
        self.assertNotIn("File \"", out["reason"])

    def test_an_ordinary_refusal_keeps_its_own_precise_stage(self):
        # The backstop must be LAST: a normal governed denial must not be relabelled "request".
        out = self._broker().dispatch("submit_sales_invoice",
                                      {"name": "SI-1", "plan_id": "nope", "marker": "x"})
        self.assertFalse(out["ok"])
        self.assertNotEqual(out.get("stage"), "request")


class PlanSubmitConsentSurface(unittest.TestCase):
    """``plan_submit`` is a state-changing call on a consent-enforcing bench, and its schema and
    description have to say so. ERPNext's native preview posts and rolls back, so guard 0.13.0
    gates the preview on the same marker as the submit — which makes the PLAN tier the first place
    a caller needs a token, and moves minting BEFORE the plan."""

    def _plan_submit(self):
        return next(t for t in TOOLS if t["name"] == "plan_submit")

    def test_plan_submit_accepts_an_optional_consent_token(self):
        schema = self._plan_submit()["inputSchema"]
        self.assertIn("consent_token", schema["properties"])
        self.assertEqual(schema["properties"]["consent_token"]["type"], "string")
        # Optional, exactly like submit/cancel: a bench without the gate must keep working, and
        # requiring it would break every install that has not turned consent on.
        self.assertEqual(tuple(schema.get("required", ())), ("name",))

    def test_the_description_does_not_promise_that_nothing_is_written(self):
        # It used to say "Nothing is posted." full stop. On a perpetual-inventory bench the
        # preview DOES post and then roll back, which is exactly why the floor gates it — an
        # agent-facing description that denies the write is the kind of copy this project treats
        # as a defect, not a nuance.
        desc = self._plan_submit()["description"]
        self.assertNotIn("Nothing is posted.", desc)
        self.assertIn("rolls back", desc)

    def test_the_description_puts_minting_before_the_plan(self):
        # The old text said a human mints the marker AFTER the plan ("A human then mints..."),
        # which is the ceremony guard 0.13.0 inverted for the floor marker. A caller following the
        # old order gets refused at the preview with no way to recover inside the same call.
        desc = self._plan_submit()["description"]
        self.assertNotIn("A human then mints a consent marker", desc)
        self.assertIn("before", desc.lower())
