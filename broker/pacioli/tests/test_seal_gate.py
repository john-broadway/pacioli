# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The seal gate — the choke point (Task 2, docs/plans/2026-07-14-close-half3-seal-slice.md).

Task 1 gave ``BrokerStore`` a fail-closed, evented seal state (``seal``/``unseal``/``seal_state``/
``seal_events``). This module proves the CALLER side: a sealed broker refuses every governed write
at the ONE place all of them dispatch through (``PacioliBroker.dispatch``), the handler never runs
(nothing is claimed, nothing is spent), every read-only tool is unaffected — even when the seal
state itself is corrupt or unreadable — and the human mint CLI carries its own defense-in-depth
pre-check ahead of the authoritative keyed gate.

Fixtures mirror ``test_tools.py``'s shape (a fake ERPNext client + a real in-memory
:class:`~pacioli.store.BrokerStore`) but are kept deliberately lean and self-contained — this
module needs only enough doctype fixtures to prove the gate's classification, not the full
per-doctype breadth ``test_tools.py`` already covers.
"""
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pacioli.cli import cmd_mint
from pacioli.registry import load_registry
from pacioli.runtime import open_store
from pacioli.store import BrokerStore
from pacioli.tools import READ_ONLY_TOOLS, PacioliBroker, format_seal_refusal, tool_names

CLOCK = "2026-07-14T00:00:00Z"

REG = ('[targets.prod]\nbase_url = "https://erp.example.com"\ncompany = "Example Corp"\n'
       'api_key = "k"\napi_secret = "env:S"\ndefault = true\n')


class FakeClient:
    """Lean fake: just enough doctype fixtures (one draft per supported doctype) to exercise every
    read tool and one representative governed pair (Sales Invoice plan_submit/submit_sales_invoice)
    — full per-doctype breadth already lives in ``test_tools.py``; this module's job is the GATE,
    not doctype coverage."""

    def __init__(self):
        self.docs = {
            "SI-1": {"name": "SI-1", "docstatus": 0, "company": "Example Corp",
                     "posting_date": "2026-07-01", "modified": "2026-07-01 10:00:00.000001"},
            "PI-1": {"name": "PI-1", "docstatus": 0, "company": "Example Corp",
                     "posting_date": "2026-07-01", "modified": "2026-07-01 10:00:00.000001"},
            "PE-1": {"name": "PE-1", "docstatus": 0, "company": "Example Corp",
                     "posting_date": "2026-07-01", "modified": "2026-07-01 10:00:00.000001"},
            "JE-1": {"name": "JE-1", "docstatus": 0, "company": "Example Corp",
                     "voucher_type": "Journal Entry", "accounts": [],
                     "posting_date": "2026-07-01", "modified": "2026-07-01 10:00:00.000001"},
            "SO-1": {"name": "SO-1", "docstatus": 0, "company": "Example Corp",
                     "customer": "Cust A", "transaction_date": "2026-07-01",
                     "modified": "2026-07-01 10:00:00.000001"},
            "PO-1": {"name": "PO-1", "docstatus": 0, "company": "Example Corp",
                     "supplier": "Supp A", "transaction_date": "2026-07-01",
                     "modified": "2026-07-01 10:00:00.000001"},
            "MR-1": {"name": "MR-1", "docstatus": 0, "company": "Example Corp",
                     "material_request_type": "Purchase", "transaction_date": "2026-07-01",
                     "modified": "2026-07-01 10:00:00.000001"},
            "DN-1": {"name": "DN-1", "docstatus": 0, "company": "Example Corp",
                     "customer": "Cust A", "posting_date": "2026-07-01",
                     "modified": "2026-07-01 10:00:00.000001"},
            "PR-1": {"name": "PR-1", "docstatus": 0, "company": "Example Corp",
                     "supplier": "Supp A", "posting_date": "2026-07-01",
                     "modified": "2026-07-01 10:00:00.000001"},
            "SE-1": {"name": "SE-1", "docstatus": 0, "company": "Example Corp",
                     "purpose": "Material Issue", "posting_date": "2026-07-01",
                     "modified": "2026-07-01 10:00:00.000001"},
            "SQ-1": {"name": "SQ-1", "docstatus": 0, "company": "Example Corp",
                     "supplier": "Supp A", "transaction_date": "2026-07-01",
                     "modified": "2026-07-01 10:00:00.000001"},
            "Q-1": {"name": "Q-1", "docstatus": 0, "company": "Example Corp",
                    "quotation_to": "Customer", "party_name": "Cust A",
                    "transaction_date": "2026-07-01",
                    "modified": "2026-07-01 10:00:00.000001"},
            "POSI-1": {"name": "POSI-1", "docstatus": 0, "company": "Example Corp",
                      "customer": "Cust A", "posting_date": "2026-07-01",
                      "modified": "2026-07-01 10:00:00.000001"},
            "DUNN-1": {"name": "DUNN-1", "docstatus": 0, "company": "Example Corp",
                      "customer": "Cust A", "posting_date": "2026-07-01",
                      "modified": "2026-07-01 10:00:00.000001"},
            "SR-1": {"name": "SR-1", "docstatus": 0, "company": "Example Corp",
                    "purpose": "Stock Reconciliation", "posting_date": "2026-07-01",
                    "modified": "2026-07-01 10:00:00.000001"},
            "LCV-1": {"name": "LCV-1", "docstatus": 0, "company": "Example Corp",
                     "distribute_charges_based_on": "Qty", "posting_date": "2026-07-01",
                     "modified": "2026-07-01 10:00:00.000001"},
            "RFQ-1": {"name": "RFQ-1", "docstatus": 0, "company": "Example Corp",
                     "status": "Draft", "transaction_date": "2026-07-01",
                     "modified": "2026-07-01 10:00:00.000001"},
            "BO-1": {"name": "BO-1", "docstatus": 0, "company": "Example Corp",
                    "blanket_order_type": "Selling", "customer": "Cust A",
                    "from_date": "2026-07-01", "to_date": "2026-12-31",
                    "modified": "2026-07-01 10:00:00.000001"},
            "JC-1": {"name": "JC-1", "docstatus": 0, "company": "Example Corp",
                    "status": "Open", "work_order": "WO-1", "operation": "Assembly",
                    "posting_date": "2026-07-01",
                    "modified": "2026-07-01 10:00:00.000001"},
            # BOM (dateless breadth): deliberately NO date key of any kind — models bom.json's
            # real shape (zero Date/Datetime fields), same as test_tools.py's own fixtures.
            "BOM-1": {"name": "BOM-1", "docstatus": 0, "company": "Example Corp",
                     "item": "CHAIR-001", "is_active": 1, "is_default": 1,
                     "total_cost": 125.0, "has_variants": 0,
                     "modified": "2026-07-01 10:00:00.000001"},
            # Work Order (datetime breadth): planned_start_date carries a REAL time part — the
            # actual frappe REST shape for a Datetime field, same as test_tools.py's fixtures.
            "WO-1": {"name": "WO-1", "docstatus": 0, "company": "Example Corp",
                    "status": "Draft", "production_item": "CHAIR-001", "qty": 10.0,
                    "produced_qty": 0.0, "bom_no": "BOM-1",
                    "planned_start_date": "2026-07-01 08:30:00",
                    "modified": "2026-07-01 10:00:00.000001"},
            "ASSET-1": {"name": "ASSET-1", "docstatus": 0, "company": "Example Corp",
                       "asset_name": "Delivery Truck", "asset_category": "Vehicles",
                       "location": "HQ", "status": "Draft",
                       "available_for_use_date": "2026-07-01",
                       "net_purchase_amount": 45000.0,
                       "modified": "2026-07-01 10:00:00.000001"},
            # Packing Slip (dateless + companyless breadth): deliberately NO "company" key AND
            # no date key of any kind — models packing_slip.json's real shape (both confirmed
            # absent), same as test_tools.py's own fixtures.
            "PS-1": {"name": "PS-1", "docstatus": 0, "delivery_note": "DN-1",
                    "from_case_no": 1, "to_case_no": 1,
                    "modified": "2026-07-01 10:00:00.000001"},
            # Cost Center Allocation (dossier-corrected dated breadth): a REAL "valid_from" date
            # key (unlike BOM/Packing Slip's dateless shape) AND a real "company" key (unlike
            # Packing Slip) — models cost_center_allocation.json's real shape, same as
            # test_tools.py's own fixtures.
            "CCA-1": {"name": "CCA-1", "docstatus": 0, "company": "Example Corp",
                     "main_cost_center": "Marketing - EC", "valid_from": "2026-07-01",
                     "modified": "2026-07-01 10:00:00.000001"},
            # Supplier Scorecard Period (Wave 4's first real-party-field breadth): a real
            # "supplier" party key AND a real "start_date" date key, but deliberately NO
            # "company" key (the SECOND companyless doctype after Packing Slip) — models
            # supplier_scorecard_period.json's real shape, same as test_tools.py's own fixtures.
            "SSP-1": {"name": "SSP-1", "docstatus": 0, "supplier": "Supplier A",
                     "total_score": 0.0, "start_date": "2026-07-01", "end_date": "2026-07-31",
                     "modified": "2026-07-01 10:00:00.000001"},
            # Quality Inspection (Wave 4's Dynamic Link breadth): a real "reference_type"/
            # "reference_name" pair (never a static party) AND a real "company" key (unlike
            # Packing Slip/Supplier Scorecard Period) — models quality_inspection.json's real
            # shape, same as test_tools.py's own fixtures.
            "QI-1": {"name": "QI-1", "docstatus": 0, "reference_type": "Purchase Receipt",
                    "reference_name": "PR-1", "item_code": "ITEM-001",
                    "inspection_type": "Incoming", "status": "Accepted", "company": "Example Corp",
                    "report_date": "2026-07-01", "readings": [{"idx": 1, "status": "Accepted"}],
                    "modified": "2026-07-01 10:00:00.000001"},
            # Installation Note (Wave 4's fifth row, second real-party-field breadth): a real
            # "customer" party key AND a real "company" key (unlike Packing Slip/Supplier
            # Scorecard Period) AND a real "inst_date" key — models installation_note.json's real
            # shape, same as test_tools.py's own fixtures.
            "IN-1": {"name": "IN-1", "docstatus": 0, "company": "Example Corp",
                    "customer": "Customer A", "status": "Draft", "inst_date": "2026-07-01",
                    "remarks": "", "modified": "2026-07-01 10:00:00.000001"},
            # Shipment (Wave 4's sixth row, first doctype with two independent
            # dynamic-selector pairs): party_field=None (never a static Customer/Supplier field)
            # AND deliberately NO "company" key (unlike Installation Note/Quality Inspection) —
            # models shipment.json's real shape, same as test_tools.py's own fixtures.
            "SHIP-1": {"name": "SHIP-1", "docstatus": 0, "status": "Draft",
                      "pickup_from_type": "Company", "pickup_company": "Example Co",
                      "pickup": "", "delivery_to_type": "Customer",
                      "delivery_customer": "Customer A", "delivery_to": "",
                      "value_of_goods": 1500.0, "pickup_date": "2026-07-01",
                      "modified": "2026-07-01 10:00:00.000001"},
            # Sales Forecast (Wave 4's seventh row): party_field=None (no party concept at all)
            # AND a real "company" key AND a real "posting_date" key — models
            # sales_forecast.json's real shape, same as test_tools.py's own fixtures. "status" is
            # "Planned" (the conventional desk-entered value; the schema itself has no default).
            "SF-1": {"name": "SF-1", "docstatus": 0, "company": "Example Corp",
                    "status": "Planned", "posting_date": "2026-07-01",
                    "modified": "2026-07-01 10:00:00.000001"},
            # Project Update (Wave 4's eighth row): party_field=None (only project/amended_from
            # are Links, neither a party) AND status/grand_total BOTH confirmed absent AND
            # deliberately NO "company" key (the FOURTH companyless doctype after Packing
            # Slip/Supplier Scorecard Period/Shipment) AND a real "date" key (a genuinely new
            # combination — reqd=0, no schema default, see pacioli/erpnext.py's own module
            # docstring) — models project_update.json's real shape, same as test_tools.py's own
            # fixtures.
            "PU-1": {"name": "PU-1", "docstatus": 0, "project": "PROJ-0001", "sent": 0,
                    "date": "2026-07-01", "modified": "2026-07-01 10:00:00.000001"},
            # Maintenance Visit (Wave 4's ninth row): party_field="customer" (a real, required
            # Link) AND status present (Draft/Cancelled/Submitted) alongside a SEPARATE
            # completion_status Select AND grand_total confirmed absent AND a real "mntc_date"
            # key (reqd, default "Today" — see pacioli/erpnext.py's own module docstring) —
            # models maintenance_visit.json's real shape, same as test_tools.py's own fixtures.
            "MV-1": {"name": "MV-1", "docstatus": 0, "customer": "Cust A",
                    "company": "Example Corp", "status": "Draft",
                    "completion_status": "", "maintenance_type": "Unscheduled",
                    "mntc_date": "2026-07-01", "modified": "2026-07-01 10:00:00.000001"},
            # Maintenance Schedule (Wave 4's tenth and last row): party_field="customer" (the
            # FIRST party field this campaign has spliced that carries no "reqd" key at all) AND
            # status present (Draft/Submitted/Cancelled) AND grand_total confirmed absent AND a
            # real "transaction_date" key (rejoins the standing SO/PO/MR/SQ/Q/RFQ pattern) —
            # models maintenance_schedule.json's real shape, same as test_tools.py's own fixtures.
            "MS-1": {"name": "MS-1", "docstatus": 0, "customer": "Cust A",
                    "company": "Example Corp", "status": "Draft",
                    "transaction_date": "2026-07-01",
                    "modified": "2026-07-01 10:00:00.000001"},
            # Asset Maintenance Log (Wave 5's first row): party_field=None (no party concept at
            # all) AND no "company" key at all (the FIFTH companyless doctype) AND the
            # lifecycle-adjacent field is "maintenance_status", NOT "status" (the first campaign
            # doctype where the two differ) AND "completion_date" is the real date_field (a
            # second real Date field, "due_date", rides as a read-only reference) — models
            # asset_maintenance_log.json's real shape, same as test_tools.py's own fixtures.
            "AML-1": {"name": "AML-1", "docstatus": 0, "task": "AMT-001",
                     "asset_maintenance": "AM-0001", "maintenance_status": "Planned",
                     "due_date": "2026-07-15",
                     "modified": "2026-07-01 10:00:00.000001"},
            # Bank Guarantee (Wave 5's second row): party_field=None — a genuine DUAL CONDITIONAL
            # customer/supplier pair (never "no party concept at all"), no "company" key at all
            # (the SIXTH companyless doctype), no "status" key at all (docstatus only), and
            # "start_date" is the real date_field — models bank_guarantee.json's real shape, same
            # as test_tools.py's own fixtures.
            "BG-1": {"name": "BG-1", "docstatus": 0, "bg_type": "Receiving",
                    "reference_doctype": "Sales Order", "customer": "Cust A",
                    "start_date": "2026-07-01", "amount": 5000.0,
                    "bank_guarantee_number": "BGN-001", "name_of_beneficiary": "Acme Corp",
                    "bank": "Test Bank",
                    "modified": "2026-07-01 10:00:00.000001"},
            # Asset Movement (Wave 5's third row): party_field=None (a Dynamic Link provenance
            # pair, never a GL party), company IS present (unlike BG/AML/etc — NOT companyless),
            # no "status" key at all (docstatus only; purpose is the 4-way router), and
            # "transaction_date" is a real Datetime (default "Now") — the SECOND Datetime-typed
            # date_field this campaign has found — models asset_movement.json's real shape, same
            # as test_tools.py's own fixtures.
            "AM-1": {"name": "AM-1", "docstatus": 0, "company": "Example Corp",
                    "purpose": "Transfer", "transaction_date": "2026-07-01 08:30:00",
                    "assets": [{"asset": "AST-0001", "target_location": "Warehouse A"}],
                    "modified": "2026-07-01 10:00:00.000001"},
            # Delivery Trip (Wave 5's fourth row): party_field=None — company IS present (reqd),
            # NOT companyless. status is a REAL field (unlike Asset Movement) but self-maintained
            # via db_set, never spliced by party mechanics. "departure_time" is a real, required
            # Datetime with NO schema default (unlike Asset Movement's own default "Now") — models
            # delivery_trip.json's real shape, same as test_tools.py's own fixtures.
            "DT-1": {"name": "DT-1", "docstatus": 0, "company": "Example Corp",
                    "driver": "DRV-001", "departure_time": "2026-07-01 08:30:00",
                    "status": "Draft",
                    "delivery_stops": [{"delivery_note": "DN-1", "address": "ADDR-1"}],
                    "modified": "2026-07-01 10:00:00.000001"},
            # Asset Value Adjustment (Wave 5's fifth row): party_field=None — company IS present
            # but NOT reqd (the third such row after Purchase Invoice/Quality Inspection). The
            # campaign's first sibling-document FACTORY — models asset_value_adjustment.json's
            # real shape, same as test_tools.py's own fixtures.
            "AVA-1": {"name": "AVA-1", "docstatus": 0, "company": "Example Corp",
                     "asset": "AST-0001", "date": "2026-07-01",
                     "current_asset_value": 1000.0, "new_asset_value": 1200.0,
                     "modified": "2026-07-01 10:00:00.000001"},
            # Payment Order (Wave 5's sixth row): party_field=None — company IS present and
            # reqd. Models payment_order.json's real shape, same as test_tools.py's own
            # fixtures. "PMO-" (not "PO-") — ERPNext's own naming_series prefix for this
            # doctype, and clear of Purchase Order's already-claimed "PO-*" fixture names in
            # this FLAT, globally-unique-by-name docs dict.
            "PMO-1": {"name": "PMO-1", "docstatus": 0, "company": "Example Corp",
                     "payment_order_type": "Payment Request", "posting_date": "2026-07-01",
                     "references": [{"payment_request": "PR-1", "amount": 100.0}],
                     "modified": "2026-07-01 10:00:00.000001"},
            # Share Transfer (a full-attention landing, not a numbered wave row): party_field=
            # None — company IS present and reqd. Models share_transfer.json's real shape, same
            # as test_tools.py's own fixtures. A Transfer-type draft (both from_shareholder and
            # to_shareholder populated — the genuinely new "both conditions true at once"
            # sub-variant this landing found).
            "ST-1": {"name": "ST-1", "docstatus": 0, "company": "Example Corp",
                    "transfer_type": "Transfer", "from_shareholder": "SH-0001",
                    "to_shareholder": "SH-0002", "from_folio_no": "FN-0001",
                    "to_folio_no": "FN-0002", "date": "2026-07-01",
                    "modified": "2026-07-01 10:00:00.000001"},
            # BOM Creator (dateless breadth, John's ruling 2 — the two-phase PROVE): deliberately
            # NO date key of any kind — models bom_creator.json's real shape (zero Date/Datetime
            # fields), same as test_tools.py's own fixtures. company IS present and reqd.
            "BC-1": {"name": "BC-1", "docstatus": 0, "company": "Example Corp",
                    "item_code": "WIDGET-100", "currency": "USD", "raw_material_cost": 500.0,
                    "status": "Draft", "modified": "2026-07-01 10:00:00.000001"},
            # Budget (a full-attention landing off the pre-verification addendum,
            # budget.verify.md): party_field=None (cost_center/project dual conditional pair);
            # company IS present and reqd. Models budget.json's real shape, same as
            # test_tools.py's own fixtures — a Cost-Center-typed draft with the Purchase Order
            # axis armed (Stop on annual, Warn on monthly-accumulated, the confirmed schema
            # defaults) and both hidden dates already populated (set_fiscal_year_dates() runs on
            # every persisted document).
            "BUD-1": {"name": "BUD-1", "docstatus": 0, "company": "Example Corp",
                     "budget_against": "Cost Center", "cost_center": "Main - EC",
                     "account": "Travel Expenses - EC", "budget_amount": 50000.0,
                     "applicable_on_purchase_order": 1,
                     "action_if_annual_budget_exceeded_on_po": "Stop",
                     "action_if_accumulated_monthly_budget_exceeded_on_po": "Warn",
                     "applicable_on_booking_actual_expenses": 1,
                     "budget_start_date": "2026-01-01", "budget_end_date": "2026-12-31",
                     "modified": "2026-07-01 10:00:00.000001"},
            # Timesheet (a Sonnet landing agent off the pre-verification addendum,
            # timesheet.verify.md): party_field="customer" — a plain, singular Link. company IS
            # present but NOT reqd. Models timesheet.json's real shape, same as test_tools.py's
            # own fixtures — a healthy Draft with one billable, fully-timed row.
            "TS-1": {"name": "TS-1", "docstatus": 0, "company": "Example Corp",
                    "customer": "Example Customer", "status": "Draft", "per_billed": 0.0,
                    "start_date": "2026-01-01", "end_date": "2026-01-01",
                    "time_logs": [{"idx": 1, "from_time": "2026-01-01 09:00:00",
                                  "to_time": "2026-01-01 17:00:00", "hours": 8.0,
                                  "is_billable": 1}],
                    "modified": "2026-07-01 10:00:00.000001"},
            # Contract (a Sonnet landing agent off the pre-verification addendum,
            # contract.verify.md): party_field=None (Dynamic Link pair). NO "company" key at all
            # — the SEVENTH companyless doctype, models the real schema faithfully (never an
            # empty-string stand-in); reads are unaffected by the company pin either way.
            "CONTRACT-1": {"name": "CONTRACT-1", "docstatus": 0, "party_type": "Customer",
                          "party_name": "Example Customer", "status": "Unsigned",
                          "fulfilment_status": "N/A", "is_signed": 1,
                          "start_date": "2026-01-01", "end_date": "2026-12-31",
                          "signed_on": "2026-01-01 09:00:00",
                          "modified": "2026-07-01 10:00:00.000001"},
            # Pick List (a Sonnet landing agent off the pre-verification addendum,
            # pick_list.verify.md): party_field="customer" — a plain, always-real header Link.
            # company IS present and reqd (NOT companyless). date_field=None — the FOURTH
            # NO_DATE_FIELD member. A healthy Draft, one fully-located row, no reservation.
            "PL-1": {"name": "PL-1", "docstatus": 0, "company": "Example Corp",
                    "customer": "Example Customer", "purpose": "Delivery", "status": "Draft",
                    "locations": [{"idx": 1, "item_code": "ITEM-1", "warehouse": "Stores - EC",
                                  "picked_qty": 5.0, "stock_qty": 5.0,
                                  "use_serial_batch_fields": 1}],
                    "modified": "2026-07-01 10:00:00.000001"},
            # Asset Repair (a Sonnet landing agent off the pre-verification addendum,
            # asset_repair.verify.md): party_field=None (asset is a fixed-asset reference).
            # company IS present but NOT reqd (fetch_from asset.company). date_field=
            # "completion_date" (Datetime). A healthy Draft, capitalized and costed.
            "AR-1": {"name": "AR-1", "docstatus": 0, "company": "Example Corp",
                    "asset": "ASSET-001", "repair_status": "Completed",
                    "failure_date": "2026-06-25 08:00:00",
                    "completion_date": "2026-07-01 10:00:00", "capitalize_repair_cost": 1,
                    "total_repair_cost": 800.0, "stock_items": [],
                    "modified": "2026-07-01 10:00:00.000001"},
            # Invoice Discounting (a Sonnet landing agent off the pre-verification addendum,
            # invoice_discounting.verify.md): party_field=None (child-table party). company IS
            # present AND reqd. date_field="posting_date" (plain Date). A healthy Draft, one
            # discounted invoice row.
            "ID-1": {"name": "ID-1", "docstatus": 0, "company": "Example Corp",
                    "posting_date": "2026-07-01", "loan_start_date": "2026-07-01",
                    "loan_period": 30, "status": "Draft", "total_amount": 1000.0,
                    "invoices": [{"sales_invoice": "SI-1", "customer": "Example Customer",
                                 "outstanding_amount": 1000.0}],
                    "modified": "2026-07-01 10:00:00.000001"},
            # Asset Capitalization (a Sonnet landing agent off the pre-verification addendum,
            # asset_capitalization.verify.md — whose own double-count RED FLAG is REFUTED from
            # source). party_field=None. company IS present AND reqd. date_field="posting_date"
            # (paired with a separate posting_time Time field). A healthy Draft, one stock item.
            "AC-1": {"name": "AC-1", "docstatus": 0, "company": "Example Corp",
                    "posting_date": "2026-07-01", "posting_time": "10:00:00",
                    "target_asset": "ASSET-100", "total_value": 500.0,
                    "stock_items": [{"idx": 1, "item_code": "ITEM-1", "stock_qty": 2.0}],
                    "asset_items": [], "service_items": [],
                    "modified": "2026-07-01 10:00:00.000001"},
            # Production Plan (a Sonnet landing agent off the pre-verification addendum,
            # production_plan.verify.md — whose own "auto-submitted: NO" claim for Stock
            # Reservation Entry is REFUTED from source). party_field=None (customer is a
            # conditional UI filter). company IS present AND reqd. date_field="posting_date". A
            # healthy Draft, reserve_stock=0 (the quiet SRE-channel control).
            "PP-1": {"name": "PP-1", "docstatus": 0, "company": "Example Corp",
                    "posting_date": "2026-07-01", "get_items_from": "Sales Order",
                    "customer": "Example Customer", "item_code": "ITEM-1",
                    "reserve_stock": 0, "status": "Draft",
                    "po_items": [{"idx": 1, "item_code": "ITEM-1", "planned_qty": 10.0}],
                    "mr_items": [], "sub_assembly_items": [], "sales_orders": [],
                    "modified": "2026-07-01 10:00:00.000001"},
            # Subcontracting Order (a Sonnet landing agent off the pre-verification addendum,
            # subcontracting_order.verify.md — its own center, THE SEVEN-PATH MUTATOR MAP,
            # re-verified byte-for-byte). party_field="supplier" (Link, reqd:1, label "Job
            # Worker"). company IS present AND reqd. date_field="transaction_date". A healthy
            # Draft, purchase_order set, no warehouse collision, reserve_stock=0 (the quiet
            # SRE-channel control).
            "SCO-1": {"name": "SCO-1", "docstatus": 0, "company": "Example Corp",
                     "supplier": "ACME Supply", "transaction_date": "2026-07-01",
                     "status": "Draft", "purchase_order": "PO-100",
                     "supplier_warehouse": "Supplier - EC", "reserve_stock": 0,
                     "total": 500.0, "per_received": 0.0,
                     "supplied_items": [{"idx": 1, "reserve_warehouse": "Stores - EC"}],
                     "modified": "2026-07-01 10:00:00.000001"},
            # Subcontracting Inward Order (a Sonnet landing agent off the pre-verification
            # addendum, subcontracting_inward_order.verify.md — its own center, THE ELEVEN-ROW
            # MUTATOR MAP plus THE NEW BYPASS CLASS, re-verified byte-for-byte). party_field=
            # "customer" (Link, reqd:1). company IS present AND reqd. date_field=
            # "transaction_date". A healthy Draft, customer_warehouse belonging to customer.
            "SCIO-1": {"name": "SCIO-1", "docstatus": 0, "company": "Example Corp",
                      "customer": "ACME Retail", "customer_name": "ACME Retail",
                      "transaction_date": "2026-07-01", "status": "Draft",
                      "sales_order": "SO-100", "customer_warehouse": "Customer - EC",
                      "per_raw_material_received": 0.0, "per_produced": 0.0,
                      "per_process_loss": 0.0, "per_delivered": 0.0,
                      "per_raw_material_returned": 0.0, "per_returned": 0.0,
                      "items": [], "service_items": [], "received_items": [],
                      "secondary_items": [],
                      "modified": "2026-07-01 10:00:00.000001"},
            # Subcontracting Receipt (THE ROOF ROW — a Sonnet landing agent off the
            # pre-verification addendum, subcontracting_receipt.verify.md — 2 corrections + 5
            # landing risks, plus THE SIXTH LEDGER-PREVIEW SHAPE, a live-crash risk this landing's
            # own finding). party_field="supplier" (Link, reqd:1, label "Job Worker"). company IS
            # present AND reqd. date_field="posting_date". A healthy Draft, no job_card, no
            # return_against, is_return=0.
            "SCR-1": {"name": "SCR-1", "docstatus": 0, "company": "Example Corp",
                     "supplier": "ACME Fabricators", "posting_date": "2026-07-01",
                     "status": "Draft", "is_return": 0, "return_against": None,
                     "total": 750.0, "per_returned": 0.0,
                     "items": [{"idx": 1, "item_code": "ITEM-1", "qty": 10.0,
                                "rejected_qty": 0.0}],
                     "supplied_items": [],
                     "modified": "2026-07-01 10:00:00.000001"},
        }
        self.locks = {}
        self.workflows = []
        self.submitted = []

    def get_document(self, doctype, name):
        from pacioli.erpnext import ErpnextError
        if name not in self.docs:
            raise ErpnextError(f"HTTP 404: {name} not found", status=404, answered=True)
        return dict(self.docs[name])

    def list_documents(self, doctype, filters=None, limit=20, party_field="customer",
                       date_field="posting_date"):
        return [dict(d) for d in self.docs.values()]

    def ledger_preview(self, company, doctype, docname):
        return {"gl_columns": [], "gl_data": [{"account": "Debtors", "debit": 100.0}]}

    def get_period_locks(self, company, doctype, posting_date):
        return dict(self.locks)

    def get_active_workflows(self, doctype):
        return [dict(w) for w in self.workflows]

    def get_workflow_state(self, doctype, name, state_field):
        return None

    def submit_document(self, doctype, name, doc=None):
        self.submitted.append(name)
        return {"name": name, "docstatus": 1, "modified": "2026-07-01 10:05:00.000001"}

    def cancel_document(self, doctype, name):
        return {"name": name, "docstatus": 2, "modified": "2026-07-01 11:00:00.000001"}


class NoCallClient:
    """A client double for the blanket sealed-governed-tool sweep: ANY attribute access resolves to
    a callable that records the call and returns an empty placeholder, never raises. The seal gate
    must short-circuit every governed tool BEFORE its handler ever reaches for the client, so a
    correct gate leaves ``self.calls`` empty no matter which governed tool is dispatched — a broken
    gate would instead accumulate real call names here (a loud, precise failure signal, not a
    crash that could be confused with an unrelated bug)."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append(name)
            return {}
        return _record


class RaisingSealStateStore:
    """A store double whose ``seal_state()`` itself raises — simulates a genuine SQL/connection
    failure, distinct from Task 1's own deny-biased malformed-CONTENT handling (which never
    raises; see ``BrokerStore.seal_state``'s docstring). Every OTHER method raises loudly if
    called — the gate must deny before the handler ever reaches for the store's substantive
    methods (``get_plan``, ``record_plan``, ``claim_marker``, ...)."""

    def seal_state(self):
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    def __getattr__(self, name):
        def _boom(*a, **k):
            raise AssertionError(
                f"store.{name} must not be reached once seal_state() itself raised")
        return _boom


class RaisingSealStateStoreBareSqliteError:
    """Same shape as :class:`RaisingSealStateStore`, but raises the BASE ``sqlite3.Error`` rather
    than the ``OperationalError`` subclass — pins that the gate's deny-bias catches the whole
    ``sqlite3.Error`` hierarchy (review F1, Task 2 review), not merely the one subclass the other
    fixture happens to use, and that the resulting reason stays honest that this is an UNREADABLE
    seal, never a confirmed seal event."""

    def seal_state(self):
        raise sqlite3.Error("database disk image is malformed (simulated)")

    def __getattr__(self, name):
        def _boom(*a, **k):
            raise AssertionError(
                f"store.{name} must not be reached once seal_state() itself raised")
        return _boom


def make_broker(client=None, reg=None, key=b"k" * 32):
    client = client or FakeClient()
    stores = {}

    def store_provider(target_name):
        if target_name not in stores:
            stores[target_name] = BrokerStore(sqlite3.connect(":memory:"), key=key,
                                              now_iso=lambda: CLOCK)
        return stores[target_name]

    broker = PacioliBroker(
        registry=load_registry(toml_text=reg or REG),
        store_provider=store_provider,
        client_provider=lambda target: client,
        now_epoch=lambda: 1_000.0,
        now_date=lambda: "2026-07-01",
    )
    return broker, client, store_provider


# name -> a minimal args dict that lets a READ tool actually succeed (not merely "not refused") —
# used by both the plain-sealed and corrupt-seal-state read survival tests below.
def _read_args(name):
    doc_for = {"get_sales_invoice": "SI-1", "get_purchase_invoice": "PI-1",
               "get_payment_entry": "PE-1", "get_journal_entry": "JE-1",
               "get_sales_order": "SO-1", "get_purchase_order": "PO-1",
               "get_material_request": "MR-1", "get_delivery_note": "DN-1",
               "get_purchase_receipt": "PR-1", "get_stock_entry": "SE-1",
               "get_supplier_quotation": "SQ-1", "get_quotation": "Q-1",
               "get_pos_invoice": "POSI-1", "get_dunning": "DUNN-1",
               "get_stock_reconciliation": "SR-1", "get_landed_cost_voucher": "LCV-1",
               "get_request_for_quotation": "RFQ-1", "get_blanket_order": "BO-1",
               "get_job_card": "JC-1", "get_bom": "BOM-1", "get_work_order": "WO-1",
               "get_asset": "ASSET-1", "get_packing_slip": "PS-1",
               "get_cost_center_allocation": "CCA-1",
               "get_supplier_scorecard_period": "SSP-1",
               "get_quality_inspection": "QI-1",
               "get_installation_note": "IN-1",
               "get_shipment": "SHIP-1",
               "get_sales_forecast": "SF-1",
               "get_project_update": "PU-1",
               "get_maintenance_visit": "MV-1",
               "get_maintenance_schedule": "MS-1",
               "get_asset_maintenance_log": "AML-1",
               "get_bank_guarantee": "BG-1",
               "get_asset_movement": "AM-1",
               "get_delivery_trip": "DT-1",
               "get_asset_value_adjustment": "AVA-1",
               "get_payment_order": "PMO-1",
               "get_share_transfer": "ST-1",
               "get_bom_creator": "BC-1",
               "get_budget": "BUD-1",
               "get_timesheet": "TS-1",
               "get_contract": "CONTRACT-1",
               "get_pick_list": "PL-1",
               "get_asset_repair": "AR-1",
               "get_invoice_discounting": "ID-1",
               "get_asset_capitalization": "AC-1",
               "get_production_plan": "PP-1",
               "get_subcontracting_order": "SCO-1",
               "get_subcontracting_inward_order": "SCIO-1",
               "get_subcontracting_receipt": "SCR-1"}
    if name in doc_for:
        return {"name": doc_for[name]}
    if name == "workflow_status":
        return {"name": "SI-1"}
    return {}


class TestClassificationCompleteness(unittest.TestCase):
    """Deny-biased classification (the brief's headline invariant): every ``_tool_*`` attribute on
    PacioliBroker must be either read-only or explicitly accounted for by this suite's governed
    list — a new tool nobody classified must fail THIS test, loudly, rather than silently landing
    ungated (or silently landing gated with nobody noticing it was never exercised)."""

    # The governed tools this module's TDD matrix actually exercises (directly, or via the
    # blanket sweep in TestSealGateBlocksEveryGovernedTool) — a count deliberately not restated
    # here, it drifts every breadth landing. Anything dispatch-resolvable that is NOT in
    # READ_ONLY_TOOLS and NOT named here is uncovered — the test below fails loudly.
    GOVERNED_TOOLS_COVERED_BY_THIS_SUITE = frozenset({
        "submit_sales_invoice", "cancel_sales_invoice", "amend_sales_invoice",
        "submit_purchase_invoice", "cancel_purchase_invoice", "amend_purchase_invoice",
        "submit_payment_entry", "cancel_payment_entry", "amend_payment_entry",
        "submit_journal_entry", "cancel_journal_entry", "amend_journal_entry",
        "submit_sales_order", "cancel_sales_order", "amend_sales_order",
        "submit_purchase_order", "cancel_purchase_order", "amend_purchase_order",
        "submit_material_request", "cancel_material_request", "amend_material_request",
        "submit_delivery_note", "cancel_delivery_note", "amend_delivery_note",
        "submit_purchase_receipt", "cancel_purchase_receipt", "amend_purchase_receipt",
        "submit_stock_entry", "cancel_stock_entry", "amend_stock_entry",
        "submit_supplier_quotation", "cancel_supplier_quotation", "amend_supplier_quotation",
        "submit_quotation", "cancel_quotation", "amend_quotation",
        "submit_pos_invoice", "cancel_pos_invoice", "amend_pos_invoice",
        "submit_dunning", "cancel_dunning", "amend_dunning",
        "submit_stock_reconciliation", "cancel_stock_reconciliation", "amend_stock_reconciliation",
        "submit_landed_cost_voucher", "cancel_landed_cost_voucher", "amend_landed_cost_voucher",
        "submit_request_for_quotation", "cancel_request_for_quotation",
        "amend_request_for_quotation",
        "submit_blanket_order", "cancel_blanket_order", "amend_blanket_order",
        "submit_job_card", "cancel_job_card", "amend_job_card",
        "submit_bom", "cancel_bom", "amend_bom",
        "submit_work_order", "cancel_work_order", "amend_work_order",
        "submit_asset", "cancel_asset", "amend_asset",
        "submit_packing_slip", "cancel_packing_slip", "amend_packing_slip",
        "submit_cost_center_allocation", "cancel_cost_center_allocation",
        "amend_cost_center_allocation",
        "submit_supplier_scorecard_period", "cancel_supplier_scorecard_period",
        "amend_supplier_scorecard_period",
        "submit_quality_inspection", "cancel_quality_inspection", "amend_quality_inspection",
        "submit_installation_note", "cancel_installation_note", "amend_installation_note",
        "submit_shipment", "cancel_shipment", "amend_shipment",
        "submit_sales_forecast", "cancel_sales_forecast", "amend_sales_forecast",
        "submit_project_update", "cancel_project_update", "amend_project_update",
        "submit_maintenance_visit", "cancel_maintenance_visit", "amend_maintenance_visit",
        "submit_maintenance_schedule", "cancel_maintenance_schedule",
        "amend_maintenance_schedule",
        "submit_asset_maintenance_log", "cancel_asset_maintenance_log",
        "amend_asset_maintenance_log",
        "submit_bank_guarantee", "cancel_bank_guarantee", "amend_bank_guarantee",
        "submit_asset_movement", "cancel_asset_movement", "amend_asset_movement",
        "submit_delivery_trip", "cancel_delivery_trip", "amend_delivery_trip",
        "submit_asset_value_adjustment", "cancel_asset_value_adjustment",
        "amend_asset_value_adjustment",
        "submit_payment_order", "cancel_payment_order", "amend_payment_order",
        "submit_share_transfer", "cancel_share_transfer", "amend_share_transfer",
        "submit_bom_creator", "cancel_bom_creator", "amend_bom_creator",
        "submit_budget", "cancel_budget", "amend_budget",
        "submit_timesheet", "cancel_timesheet", "amend_timesheet",
        "submit_contract", "cancel_contract", "amend_contract",
        "submit_pick_list", "cancel_pick_list", "amend_pick_list",
        "submit_asset_repair", "cancel_asset_repair", "amend_asset_repair",
        "submit_invoice_discounting", "cancel_invoice_discounting", "amend_invoice_discounting",
        "submit_asset_capitalization", "cancel_asset_capitalization", "amend_asset_capitalization",
        "submit_production_plan", "cancel_production_plan", "amend_production_plan",
        "submit_subcontracting_order", "cancel_subcontracting_order",
        "amend_subcontracting_order",
        "submit_subcontracting_inward_order", "cancel_subcontracting_inward_order",
        "amend_subcontracting_inward_order",
        "submit_subcontracting_receipt", "cancel_subcontracting_receipt",
        "amend_subcontracting_receipt",
        "plan_submit", "plan_cancel", "plan_cascade_cancel", "cascade_cancel",
        "plan_reconcile", "reconcile", "request_workflow_transition",
    })

    def test_every_tool_is_read_only_or_gate_covered(self):
        all_names = {n[len("_tool_"):] for n in dir(PacioliBroker) if n.startswith("_tool_")}
        uncovered = all_names - READ_ONLY_TOOLS - self.GOVERNED_TOOLS_COVERED_BY_THIS_SUITE
        self.assertEqual(
            uncovered, set(),
            f"tool(s) {sorted(uncovered)} are neither classified READ_ONLY_TOOLS nor covered by "
            "this seal-gate test suite's governed list — a NEW tool is born gated by dispatch() "
            "regardless (anything outside READ_ONLY_TOOLS is seal-gated by construction), but it "
            "must still be consciously classified here: add it to READ_ONLY_TOOLS in tools.py "
            "ONLY if it is truly read-only, otherwise add it to "
            "GOVERNED_TOOLS_COVERED_BY_THIS_SUITE above and extend the seal-gate TDD matrix")

    def test_read_only_and_governed_partition_every_tool_exactly(self):
        # No drift, no double-counting: the two sets exactly partition the real tool surface.
        self.assertEqual(READ_ONLY_TOOLS | self.GOVERNED_TOOLS_COVERED_BY_THIS_SUITE,
                         set(tool_names()))
        self.assertEqual(READ_ONLY_TOOLS & self.GOVERNED_TOOLS_COVERED_BY_THIS_SUITE, set())


class TestSealGateBlocksEveryGovernedTool(unittest.TestCase):
    def test_every_governed_tool_is_refused_while_sealed_handler_never_runs(self):
        client = NoCallClient()
        broker, _, stores = make_broker(client=client)
        store = stores("prod")
        store.seal("incident under investigation", source="operator")

        governed = sorted(set(tool_names()) - READ_ONLY_TOOLS)
        self.assertTrue(governed)  # sanity: there is something for the gate to cover
        for name in governed:
            with self.subTest(tool=name):
                out = broker.dispatch(name, {})
                self.assertFalse(out["ok"], f"{name} must be refused while sealed: {out}")
                self.assertEqual(out["stage"], "seal", f"{name} wrong stage: {out}")
                self.assertIn("SEALED", out["reason"])
                self.assertIn("pacioli unseal --reason", out["reason"])
                self.assertEqual(client.calls, [],
                                 f"{name} reached the client while sealed: {client.calls}")

    def test_refusal_names_since_reason_source(self):
        client = NoCallClient()
        broker, _, stores = make_broker(client=client)
        stores("prod").seal("Q3 reconciliation gap", source="operator")
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "seal")
        self.assertIn(CLOCK, out["reason"])
        self.assertIn("Q3 reconciliation gap", out["reason"])
        self.assertIn("operator", out["reason"])


class TestReadOnlyToolsSurviveSealed(unittest.TestCase):
    def test_all_read_only_tools_succeed_while_sealed(self):
        broker, client, stores = make_broker()
        stores("prod").seal("incident", source="operator")
        for name in sorted(READ_ONLY_TOOLS):
            with self.subTest(tool=name):
                out = broker.dispatch(name, _read_args(name))
                self.assertTrue(out["ok"], f"{name} must still succeed while sealed: {out}")


class TestReadOnlyToolsSurviveCorruptSealState(unittest.TestCase):
    """Global constraint #6 (the plan): reads never sealed — not even when the seal_events history
    itself is corrupt. Read tools skip target/store resolution on the seal-gate path entirely
    (their own handlers route independently), so a gap/zero-row/unverifiable seal state must never
    surface as a read failure."""

    def _corrupt_gap(self, store):
        store.seal("first", source="operator")  # seq=2 (genesis=1, seal=2)
        store._conn.execute("DELETE FROM seal_events WHERE seq=1")  # gap: only seq=2 remains
        state = store.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "seal history gap (rollback?)")

    def test_reads_survive_a_seal_history_gap(self):
        broker, client, stores = make_broker()
        self._corrupt_gap(stores("prod"))
        for name in sorted(READ_ONLY_TOOLS):
            with self.subTest(tool=name):
                out = broker.dispatch(name, _read_args(name))
                self.assertTrue(out["ok"], f"{name} must survive a corrupt seal state: {out}")

    def test_governed_tool_denies_naming_the_gap_cause(self):
        broker, client, stores = make_broker()
        self._corrupt_gap(stores("prod"))
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "seal")
        self.assertIn("gap", out["reason"].lower())


class TestSealStateExceptionDenyBiased(unittest.TestCase):
    def test_seal_state_raising_denies_the_write_stage_seal_handler_never_runs(self):
        client = NoCallClient()
        broker = PacioliBroker(
            registry=load_registry(toml_text=REG),
            store_provider=lambda name: RaisingSealStateStore(),
            client_provider=lambda target: client,
            now_epoch=lambda: 1_000.0,
            now_date=lambda: "2026-07-01",
        )
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "seal")
        self.assertIn("disk I/O error", out["reason"])
        self.assertEqual(client.calls, [])

    def test_seal_state_raising_bare_sqlite3_error_denies_stage_seal_honestly(self):
        # Fix 1 (review F1, Task 2 review): once the store itself has resolved cleanly, a failure
        # reading seal_state() — here the BASE sqlite3.Error, not merely OperationalError — is the
        # one case _seal_gate itself denies, at stage="seal". The reason must carry the raised
        # cause AND say plainly this is not a confirmed seal event (an unreadable seal, denied
        # deny-biased — never mistaken for "probably unsealed" or for a genuine seal record).
        client = NoCallClient()
        broker = PacioliBroker(
            registry=load_registry(toml_text=REG),
            store_provider=lambda name: RaisingSealStateStoreBareSqliteError(),
            client_provider=lambda target: client,
            now_epoch=lambda: 1_000.0,
            now_date=lambda: "2026-07-01",
        )
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "seal")
        self.assertIn("database disk image is malformed", out["reason"])
        self.assertIn("NOT a confirmed seal event", out["reason"])
        self.assertEqual(client.calls, [])

    def test_registry_error_on_a_governed_tool_still_denies_never_crashes(self):
        # An unknown pacioli_target: the seal gate resolves the target itself (the same
        # pacioli_target path _route uses) before it can even look up a store — deny-biased, this
        # too must refuse rather than let a raw RegistryError escape dispatch(). Review F1 (Task 2
        # review): resolution precedes seal knowledge entirely, so this must land at the SAME
        # stage a read-only tool's own _route call would have produced for the identical failure
        # — "request", the pre-Task-2 shape — never "seal" (an unknown target has no store to be
        # sealed).
        client = NoCallClient()
        broker, _, _ = make_broker(client=client)
        out = broker.dispatch("plan_submit", {"name": "SI-1", "pacioli_target": "nonexistent"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")
        self.assertIn("unknown target", out["reason"])
        self.assertEqual(client.calls, [])

    def test_registry_error_on_a_governed_tool_stays_request_stage_even_while_sealed(self):
        # Same unknown target, but this time the broker's OWN configured ("prod") store IS
        # sealed. Must not matter: resolving "nonexistent" fails before any store is even opened,
        # so there is no store for this call to find sealed — stage stays "request", proving the
        # documented order (resolution precedes seal knowledge) rather than merely happening to
        # hold when unsealed.
        client = NoCallClient()
        broker, _, stores = make_broker(client=client)
        stores("prod").seal("incident under investigation", source="operator")
        out = broker.dispatch("plan_submit", {"name": "SI-1", "pacioli_target": "nonexistent"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")
        self.assertIn("unknown target", out["reason"])
        self.assertEqual(client.calls, [])

    def test_store_corrupt_error_on_a_governed_tool_is_stage_store_not_seal(self):
        # The other resolution failure the ruling names: a torn/corrupt store file. Before Task 2
        # this landed as dispatch()'s pre-existing StoreCorruptError clause (stage="store") — the
        # same shape test_tools.py pins for the read-only path
        # (test_torn_store_on_the_server_path_is_a_structured_deny_not_a_raw_error). A governed
        # tool must get the identical stage, never "seal" — seal_state() is never even reached.
        from pacioli.store import StoreCorruptError

        def torn_provider(target_name):
            raise StoreCorruptError(
                "state db is only 1 bytes — smaller than a valid SQLite header")

        client = NoCallClient()
        broker = PacioliBroker(
            registry=load_registry(toml_text=REG),
            store_provider=torn_provider,
            client_provider=lambda target: client,
            now_epoch=lambda: 1_000.0,
            now_date=lambda: "2026-07-01",
        )
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "store")
        self.assertIn("header", out["reason"].lower())
        self.assertEqual(client.calls, [])


class TestUnsealedByteIdenticalBehavior(unittest.TestCase):
    """Pins the pre-seal (0.19.0) shape for one representative success and one representative
    deny — a fresh keyed store's genesis row reads as unsealed (Task 1), and the gate must add
    NOTHING to either response in that state."""

    def test_plan_submit_success_shape_unchanged(self):
        broker, client, stores = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertEqual(set(out), {"ok", "plan_id", "docname", "target", "doctype",
                                     "doc_version", "posting_date", "projected_gl", "risk_flags",
                                     "workflow", "next"})
        self.assertEqual(out["docname"], "SI-1")
        self.assertEqual(out["target"], "prod")

    def test_submit_with_unknown_plan_deny_shape_unchanged(self):
        broker, client, stores = make_broker()
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": "nope", "marker": "t"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")  # NOT "seal" — the ordinary plan-stage refusal
        self.assertEqual(set(out), {"ok", "stage", "reason"})
        self.assertEqual(client.submitted, [])


class TestMarkerSurvivesSealedRefusal(unittest.TestCase):
    """The F-C2 invariant, extended to the seal: consent is spent by COMMITMENT, never by
    refusal. A marker minted before a seal must still be live after a sealed attempt to spend it,
    and must still work — the SAME marker — once the seal clears."""

    def test_mint_seal_refused_marker_intact_unseal_same_marker_commits(self):
        broker, client, stores = make_broker()
        store = stores("prod")

        plan = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(plan["ok"], plan)
        token = "raw-marker-token"
        store.mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        self.assertEqual(store.marker_state(token), "live")

        store.seal("incident under investigation", source="operator")
        self.assertTrue(store.seal_state()["sealed"])

        refused = broker.dispatch(
            "submit_sales_invoice",
            {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["stage"], "seal")
        # nothing spent, nothing landed: the marker is untouched and the bench never saw a submit.
        self.assertEqual(store.marker_state(token), "live")
        self.assertEqual(client.submitted, [])

        state = store.unseal("resolved")
        self.assertFalse(state["sealed"])

        committed = broker.dispatch(
            "submit_sales_invoice",
            {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(committed["ok"], committed)
        self.assertEqual(client.submitted, ["SI-1"])
        self.assertEqual(store.marker_state(token), "consumed")


class TestFormatSealRefusal(unittest.TestCase):
    def test_names_since_reason_source_cause_and_the_unseal_instruction(self):
        text = format_seal_refusal({"sealed": True, "since": "2026-07-14T00:00:00Z",
                                    "reason": "incident", "source": "operator", "seq": 2,
                                    "cause": None})
        for expect in ("2026-07-14T00:00:00Z", "incident", "operator",
                      "pacioli unseal --reason"):
            self.assertIn(expect, text)

    def test_names_the_cause_when_present(self):
        text = format_seal_refusal({"sealed": True, "since": None, "reason": None,
                                    "source": None, "seq": 0, "cause": "no seal history"})
        self.assertIn("no seal history", text)


class TestMintCliSealGate(unittest.TestCase):
    """cli.cmd_mint's keyless, defense-in-depth pre-check — never the authoritative gate (that is
    the keyed dispatch-time _seal_gate above), but a sealed store must still refuse to mint."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(
            '[targets.prod]\nbase_url = "https://erp.example.com"\n'
            'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n')
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _mint(self, plan_id="p1", ttl=900):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cmd_mint(self.env, plan_id=plan_id, target=None, ttl=ttl)
        return rc, out.getvalue() + err.getvalue()

    def _record_plan(self):
        store = open_store(self.env, "prod")  # keyed
        from pacioli.plan import new_plan
        store.record_plan(new_plan("p1", "prod", "v1", "2026-07-01", docname="SI-1"))
        return store

    def test_mint_refused_on_sealed_store_no_marker_minted(self):
        store = self._record_plan()
        store.seal("incident", source="operator")

        rc, out = self._mint()
        self.assertNotEqual(rc, 0)
        self.assertIn("SEALED", out)
        self.assertIn("pacioli unseal --reason", out)

        store2 = open_store(self.env, "prod", with_key=False)
        count = store2._conn.execute("SELECT COUNT(*) FROM markers").fetchone()[0]
        self.assertEqual(count, 0)

    def test_mint_unaffected_when_unsealed(self):
        self._record_plan()
        rc, out = self._mint()
        self.assertEqual(rc, 0)
        self.assertIn("marker:", out)


if __name__ == "__main__":
    unittest.main()
