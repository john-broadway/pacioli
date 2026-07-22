# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""ERPNext client tests — request shaping + response parsing over an injected transport.

The transport is a fake that records every call; nothing here touches a network. The real calls
are proven only against a live bench (SPEC §7) — these tests pin the *shape* the client sends.

Breadth (Purchase Invoice): the read/plan/execute/amend methods were generalized from
``*_sales_invoice`` names to ``doctype``-parameterized generic names (``get_document``,
``list_documents``, ``submit_document``, ``cancel_document``, ``get_doc_for_amend``,
``find_amendments``, ``create_amended_draft``, ``get_gl_entries``); every Sales Invoice test below
now calls the generic method with ``SALES_INVOICE`` explicit, and a parallel set of Purchase
Invoice tests pins the doctype-specific differences (the ``supplier`` party field, the
``voucher_type`` GL filter, the ``Purchase Invoice`` resource path). The Purchase Invoice request
shapes are knowledge-pinned from ERPNext's documented REST conventions (the same doc-method /
resource-CRUD surface Sales Invoice already rides) — NOT live-verified against a bench; live
falsification is a future bench gate.
"""
import json
import unittest
import urllib.error

from pacioli.erpnext import (ASSET_CAPITALIZATION, ASSET_MAINTENANCE_LOG, ASSET_MOVEMENT,
                             ASSET_REPAIR,
                             ASSET_VALUE_ADJUSTMENT, BANK_GUARANTEE,
                             BLANKET_ORDER, BOM, BOM_CREATOR, BUDGET, CONTRACT,
                             COST_CENTER_ALLOCATION, DELIVERY_NOTE, DELIVERY_TRIP, DUNNING,
                             ENQUEUE_ON_SUBMIT_CHANNELS, ENQUEUE_ON_SUBMIT_DOCTYPES,
                             INSTALLATION_NOTE, INVOICE_DISCOUNTING, JOB_CARD, JOURNAL_ENTRY,
                             LANDED_COST_VOUCHER,
                             MAINTENANCE_SCHEDULE, MAINTENANCE_VISIT, MATERIAL_REQUEST,
                             PACKING_SLIP, PAYMENT_ENTRY, PAYMENT_ORDER,
                             PICK_LIST,
                             POS_INVOICE, PRODUCTION_PLAN, PROJECT_UPDATE, PURCHASE_INVOICE,
                             PURCHASE_ORDER,
                             PURCHASE_RECEIPT, QUALITY_INSPECTION, QUOTATION,
                             REQUEST_FOR_QUOTATION, SALES_FORECAST, SALES_INVOICE, SALES_ORDER,
                             SHARE_TRANSFER, SHIPMENT, STOCK_ENTRY, STOCK_RECONCILIATION,
                             SUBCONTRACTING_INWARD_ORDER, SUBCONTRACTING_ORDER,
                             SUBCONTRACTING_RECEIPT,
                             SUPPLIER_QUOTATION,
                             SUPPLIER_SCORECARD_PERIOD, SUPPORTED_DOCTYPES, TIMESHEET,
                             WORK_ORDER, ASSET,
                             ErpnextClient, ErpnextError)

PREVIEW_METHOD = "erpnext.controllers.stock_controller.show_accounting_ledger_preview"


class FakeTransport:
    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [])

    def __call__(self, method, url, headers, params=None, body=None):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "params": params, "body": body})
        if self._responses:
            return self._responses.pop(0)
        return (200, {"data": {}})


def client(responses=None):
    t = FakeTransport(responses)
    c = ErpnextClient(base_url="https://erp.example.com", api_key="KEY",
                      api_secret="SECRET", transport=t)
    return c, t


class TestSupportedDoctypesConfig(unittest.TestCase):
    """The broker's own per-doctype config (design §B) — belt-and-suspenders alongside, but
    distinct from, pacioli_guard's per-credential resource_doctypes grant."""

    def test_all_configured_doctypes_have_their_party_field(self):
        self.assertEqual(SUPPORTED_DOCTYPES[SALES_INVOICE],
                         {"party_field": "customer", "submit_via": "run_method",
                          "date_field": "posting_date"})
        self.assertEqual(SUPPORTED_DOCTYPES[PURCHASE_INVOICE],
                         {"party_field": "supplier", "submit_via": "run_method",
                          "date_field": "posting_date"})
        self.assertEqual(SUPPORTED_DOCTYPES[PAYMENT_ENTRY],
                         {"party_field": "party", "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_journal_entry_has_no_party_field(self):
        # Journal Entry carries no header-level party at all (only per-line party in its
        # `accounts` child table) — party_field is None, not a missing key or an empty string.
        # submit_via is "client_rpc" — the ONLY doctype not on the URL-path run_method surface,
        # because JournalEntry.submit()/.cancel() override the base Document methods without
        # @frappe.whitelist() (SCOPED-TOKEN-PROOF.md PHASE L), 403ing the run_method vector.
        self.assertEqual(SUPPORTED_DOCTYPES[JOURNAL_ENTRY],
                         {"party_field": None, "submit_via": "client_rpc",
                          "date_field": "posting_date"})

    def test_sales_order_uses_transaction_date_not_posting_date(self):
        # Sales Order breadth (2026-07-20, campaign Wave 1) — the FIRST supported doctype
        # confirmed to carry no `posting_date` field at all (sales_order.json, version-16, 170
        # fields enumerated, none named posting_date); its own date field is `transaction_date`.
        # Otherwise mechanically identical to Sales Invoice: party_field="customer",
        # submit_via="run_method" (Sales Order overrides neither submit() nor cancel()).
        self.assertEqual(SUPPORTED_DOCTYPES[SALES_ORDER],
                         {"party_field": "customer", "submit_via": "run_method",
                          "date_field": "transaction_date"})

    def test_purchase_order_is_sales_order_shape_with_supplier_swapped_in(self):
        # Purchase Order breadth (2026-07-20, campaign Wave 1) — the sixth supported doctype, and
        # (with Sales Order) the second confirmed to carry no `posting_date` field at all
        # (purchase_order.json, version-16, 157 fields enumerated, none named posting_date); its
        # own date field is `transaction_date`, identically to Sales Order. It forces NO new
        # mechanical branch: submit_via="run_method" (Purchase Order overrides neither submit()
        # nor cancel(), confirmed from purchase_order.py). The ONLY axis that differs from Sales
        # Order is party_field ("supplier" vs "customer") — asserted directly here, the identity
        # claim itself, rather than a second hand-copied literal dict.
        self.assertEqual(SUPPORTED_DOCTYPES[PURCHASE_ORDER],
                         {**SUPPORTED_DOCTYPES[SALES_ORDER], "party_field": "supplier"})
        self.assertEqual(SUPPORTED_DOCTYPES[PURCHASE_ORDER],
                         {"party_field": "supplier", "submit_via": "run_method",
                          "date_field": "transaction_date"})

    def test_material_request_has_no_party_field_and_uses_transaction_date(self):
        # Material Request breadth (2026-07-20, campaign Wave 1) — the seventh supported doctype.
        # party_field=None: the header-level `customer` field is present (material_request.json)
        # but type-gated (`depends_on: eval:doc.material_request_type=="Customer Provided"`) and
        # forcibly cleared by validate_material_request_type() for the other 5 of 6 types — never
        # a stable counterparty, Journal Entry's shape (no party column to splice), not Payment
        # Entry's (a real, if sometimes-blank, header field). date_field="transaction_date" —
        # confirmed absent `posting_date` (material_request.json, 40 fields enumerated), the same
        # finding shape as Sales Order and Purchase Order.
        self.assertEqual(SUPPORTED_DOCTYPES[MATERIAL_REQUEST],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "transaction_date"})

    def test_delivery_note_uses_customer_and_keeps_the_default_posting_date(self):
        # Delivery Note breadth (2026-07-21, campaign Wave 1) — the eighth supported doctype, and
        # the FIRST stock-primary one (real Stock Ledger + conditional GL of its own — see
        # erpnext.py's SUPPORTED_DOCTYPES comment block). party_field="customer"
        # (delivery_note.json lines 189-201, reqd). Unlike Sales/Purchase Order and Material
        # Request, Delivery Note carries a REAL `posting_date` field (lines 246-258, reqd,
        # default "Today") — it rides the plain default date_field path, not `transaction_date`.
        # submit_via="run_method" — Delivery Note overrides neither submit() nor cancel()
        # (confirmed from delivery_note.py, version-16).
        self.assertEqual(SUPPORTED_DOCTYPES[DELIVERY_NOTE],
                         {"party_field": "customer", "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_purchase_receipt_uses_supplier_and_keeps_the_default_posting_date(self):
        # Purchase Receipt breadth (2026-07-21, campaign Wave 1) — the ninth supported doctype, and
        # the SECOND stock-primary one (with Delivery Note — see erpnext.py's SUPPORTED_DOCTYPES
        # comment block). party_field="supplier" (purchase_receipt.json lines 186-200, reqd).
        # Purchase Receipt carries a REAL `posting_date` field (lines 223-236, reqd, default
        # "Today") — it rides the plain default date_field path, the same shape Delivery Note
        # already rides, not `transaction_date`. submit_via="run_method" — Purchase Receipt
        # overrides neither submit() nor cancel() (confirmed from purchase_receipt.py, version-16).
        self.assertEqual(SUPPORTED_DOCTYPES[PURCHASE_RECEIPT],
                         {"party_field": "supplier", "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_stock_entry_has_no_party_field_and_keeps_the_default_posting_date(self):
        # Stock Entry breadth (2026-07-21, campaign Wave 1) — the tenth supported doctype and the
        # LAST of Wave 1. party_field=None: `supplier` is present in schema (Link -> Supplier,
        # stock_entry.json lines 460-468) but carries no `reqd` and its `depends_on` is a
        # CLIENT-SIDE JS eval only (stock_entry.js:1657-1659) — never read or cleared anywhere in
        # stock_entry.py (confirmed by grep: zero `.supplier` hits) — a WEAKER basis for None than
        # Material Request's own server-enforced clearing. No `customer` field exists at all.
        # date_field="posting_date" — a real field (confirmed present, default "Today"), the same
        # default path Delivery Note/Purchase Receipt already ride, not `transaction_date`.
        # submit_via="run_method" — Stock Entry overrides neither submit() nor cancel() (confirmed
        # from all 4875 lines of stock_entry.py, version-16).
        self.assertEqual(SUPPORTED_DOCTYPES[STOCK_ENTRY],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_supplier_quotation_is_purchase_order_shape_exactly(self):
        # Supplier Quotation breadth (2026-07-21, campaign Wave 2, first row) — the eleventh
        # supported doctype, and the FIRST whose entry is byte-for-byte IDENTICAL to an
        # already-landed doctype's (Purchase Order's), not merely shape-matched with one field
        # swapped. party_field="supplier" (supplier_quotation.json lines 150-162, reqd) — the
        # same fieldname Purchase Order already carries. date_field="transaction_date" —
        # confirmed absent `posting_date` from supplier_quotation.json (the fourth doctype on
        # this pattern, with Sales Order/Purchase Order/Material Request). submit_via="run_method"
        # — Supplier Quotation overrides neither submit() nor cancel() (confirmed from
        # supplier_quotation.py, version-16). Every prior "mechanically identical" pairing (e.g.
        # Purchase Order vs. Sales Order) still differed on at least one axis (there,
        # party_field) — this is the first pairing with a genuinely EMPTY diff, asserted directly
        # here rather than as a shape claim with an override.
        self.assertEqual(SUPPORTED_DOCTYPES[SUPPLIER_QUOTATION],
                         SUPPORTED_DOCTYPES[PURCHASE_ORDER])
        self.assertEqual(SUPPORTED_DOCTYPES[SUPPLIER_QUOTATION],
                         {"party_field": "supplier", "submit_via": "run_method",
                          "date_field": "transaction_date"})

    def test_quotation_has_no_static_party_field_and_uses_transaction_date(self):
        # Quotation breadth (2026-07-21, campaign Wave 2, second row) — the twelfth supported
        # doctype, and the FIRST DYNAMIC-PARTY doctype landed (the judgment call of Wave 2).
        # party_field=None: Quotation carries NO static header-level "customer" field at all —
        # confirmed absent across all 130 fields enumerated in quotation.json — only the dynamic
        # pair quotation_to (Link -> DocType, reqd) + party_name (Dynamic Link, options=
        # "quotation_to"). A Dynamic Link does not fit the broker's single-static-fieldname
        # party_field shape, so this follows Material Request's None-precedent (a doctype-specific
        # pair of list-tier context columns instead — quotation_to/party_name, see
        # test_quotation_list_has_no_single_party_field_but_carries_quotation_to_and_party_name
        # below) rather than forcing the dynamic field into party_field. date_field=
        # "transaction_date" — confirmed absent posting_date from quotation.json (the fifth
        # doctype on this pattern, with Sales/Purchase Order/Material Request/Supplier Quotation).
        # submit_via="run_method" — Quotation overrides neither submit() nor cancel() (confirmed
        # from quotation.py, version-16).
        self.assertEqual(SUPPORTED_DOCTYPES[QUOTATION],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "transaction_date"})

    def test_pos_invoice_is_sales_invoice_shape_exactly(self):
        # POS Invoice breadth (2026-07-21, campaign Wave 2, third row) — the thirteenth supported
        # doctype, and byte-for-byte IDENTICAL to Sales Invoice's own entry (party_field="customer"
        # — pos_invoice.json line 217; date_field stays the default "posting_date" — a real field,
        # line 293, reqd, default "Today"; submit_via="run_method" — confirmed by reading all 1119
        # lines of pos_invoice.py, no submit()/cancel() override). The SHAPE is a repeat of
        # Delivery Note's/Purchase Receipt's own "identical to the sibling invoice doctype" finding
        # — the CONTENT is not: POS Invoice's own submit posts NEITHER a GL Entry NOR a Stock
        # Ledger Entry at all (POSInvoice.on_submit fully overrides SalesInvoice.on_submit without
        # calling it — a correction to the pinned dossier, see erpnext.py's module docstring for
        # the full source-cited finding). That divergence lives entirely in tools.py's risk-flag
        # layer, never in this config dict.
        self.assertEqual(SUPPORTED_DOCTYPES[POS_INVOICE], SUPPORTED_DOCTYPES[SALES_INVOICE])
        self.assertEqual(SUPPORTED_DOCTYPES[POS_INVOICE],
                         {"party_field": "customer", "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_dunning_is_sales_invoice_shape_exactly(self):
        # Dunning breadth (2026-07-21, campaign Wave 2, fourth row) — the fourteenth supported
        # doctype, and byte-for-byte IDENTICAL to Sales Invoice's own entry on THIS config dict
        # (party_field="customer" — dunning.json lines 219-225, reqd: 1, the dossier's citation is
        # CORRECT here; date_field stays the default "posting_date" — a real field, lines 92-98,
        # reqd, default "Today"; submit_via="run_method" — confirmed by reading all 276 lines of
        # dunning.py, no submit()/cancel()/on_submit() override at all). The config-dict SHAPE
        # match is a repeat of POS Invoice's own finding — the CONTENT is a genuinely different
        # divergence: Dunning has no make_gl_entries method anywhere in its MRO (it inherits
        # AccountsController, never StockController), so the native ledger_preview RPC is not just
        # non-posting but UNCALLABLE — see erpnext.py's module docstring for the full source-cited
        # finding. That divergence lives entirely in tools.py's plan_submit branch + risk-flag
        # layer, never in this config dict.
        self.assertEqual(SUPPORTED_DOCTYPES[DUNNING], SUPPORTED_DOCTYPES[SALES_INVOICE])
        self.assertEqual(SUPPORTED_DOCTYPES[DUNNING],
                         {"party_field": "customer", "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_stock_reconciliation_has_no_party_field_and_rides_client_rpc(self):
        # Stock Reconciliation breadth (2026-07-21, campaign Wave 2, fifth row) — the fifteenth
        # supported doctype. party_field=None (no customer/supplier/party field anywhere in the
        # 17-field schema — the JE/MR/SE/Q shape); date_field stays the default "posting_date" (a
        # real field, lines 66-73, reqd, default "Today"). submit_via="client_rpc" — NOT the
        # run_method surface every non-JE doctype so far has used: StockReconciliation overrides
        # submit()/cancel() themselves (stock_reconciliation.py:1107-1127), neither decorated
        # @frappe.whitelist() (confirmed: the file's only three @frappe.whitelist() occurrences all
        # sit after cancel() ends) — the identical Journal Entry mechanism. This is a config-dict
        # divergence from every doctype landed since Journal Entry, not a repeat of any of them.
        self.assertEqual(SUPPORTED_DOCTYPES[STOCK_RECONCILIATION],
                         {"party_field": None, "submit_via": "client_rpc",
                          "date_field": "posting_date"})

    def test_landed_cost_voucher_is_stock_entry_shape_exactly(self):
        # Landed Cost Voucher breadth (2026-07-21, campaign Wave 2, sixth row and LAST) — the
        # sixteenth supported doctype, and the SECOND whose entry is byte-for-byte IDENTICAL to an
        # already-landed doctype's (Stock Entry's), after Supplier Quotation/Purchase Order.
        # party_field=None (no customer/supplier/party field anywhere in the 15-field schema —
        # costs allocate onto already-submitted receipts, supplier identity lives on those
        # receipts, not here). date_field stays the default "posting_date" (a real field, reqd,
        # default "Today", landed_cost_voucher.json lines 134-139), never "transaction_date".
        # submit_via="run_method" — confirmed by reading all 522 lines of landed_cost_voucher.py:
        # no submit()/cancel() override, only on_submit/on_cancel hooks. The config-dict SHAPE
        # match is a repeat of Supplier Quotation's own finding — the CONTENT is not: Landed Cost
        # Voucher's own submit/cancel rewrites OTHER submitted documents' ledger rows entirely,
        # never its own (see erpnext.py's module docstring for the full source-cited finding).
        # That divergence lives entirely in tools.py's plan_submit/plan_cancel branches + risk-flag
        # layer, never in this config dict.
        self.assertEqual(SUPPORTED_DOCTYPES[LANDED_COST_VOUCHER], SUPPORTED_DOCTYPES[STOCK_ENTRY])
        self.assertEqual(SUPPORTED_DOCTYPES[LANDED_COST_VOUCHER],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_request_for_quotation_has_no_party_field_and_uses_transaction_date(self):
        # Request for Quotation breadth (2026-07-21, campaign Wave 3, first row) — the seventeenth
        # supported doctype. party_field=None: RFQ dispatches to MULTIPLE suppliers via a required
        # child table (suppliers, Table -> Request for Quotation Supplier,
        # request_for_quotation.json lines 114-121) — the one supplier-shaped header field
        # ("vendor") is hidden/read-only/optional, set only for a single-supplier PDF print, mostly
        # blank on a real document. date_field="transaction_date" — confirmed NO posting_date field
        # anywhere in the 384 enumerated fields (the sixth doctype on this pattern).
        # submit_via="run_method" — confirmed by reading all 675 lines of
        # request_for_quotation.py: on_submit/on_cancel are bare, undecorated hooks; no
        # def submit/def cancel override anywhere. **The ledger-preview category needed direct
        # verification, not a repeat of the dossier's own hedge**: RequestforQuotation(
        # BuyingController) shares BuyingController -> SubcontractingController -> StockController
        # with Purchase Order/Material Request/Supplier Quotation, so it genuinely inherits a real,
        # callable make_gl_entries — the SO/PO/MR/SQ/Q "honest-empty" category, NOT Dunning/Landed
        # Cost Voucher's "uncallable" one (neither has StockController in its MRO). That finding
        # lives entirely in erpnext.py's module docstring and tools.py's own recap — it does not
        # change this config dict, and RFQ is deliberately NOT added to tools.py's ledger_preview
        # skip tuple.
        self.assertEqual(SUPPORTED_DOCTYPES[REQUEST_FOR_QUOTATION],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "transaction_date"})

    def test_blanket_order_has_no_party_field_and_uses_from_date(self):
        # Blanket Order breadth (2026-07-21, campaign Wave 3, second row) — the eighteenth
        # supported doctype. party_field=None: BlanketOrder carries BOTH customer and supplier as
        # real header Link fields (blanket_order.json lines 44-50/59-65), client-side gated on
        # blanket_order_type via depends_on, but NEITHER carries reqd:1 and NEITHER is
        # server-enforced (set_party_item_code() dispatches on type but never validates either is
        # populated) — a weaker guarantee than Material Request's own explicit clearing, but still
        # no single static fieldname is ever unconditionally the party.
        # date_field="from_date" — confirmed NO posting_date field AND NO transaction_date field
        # anywhere in the 18 enumerated fields (blanket_order.json) — the first doctype on neither
        # established date-field pattern. submit_via="run_method" — confirmed by reading all 201
        # lines of blanket_order.py: no def submit/def cancel override, and (a genuine rarity this
        # campaign) no on_submit/on_cancel method defined at all.
        self.assertEqual(SUPPORTED_DOCTYPES[BLANKET_ORDER],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "from_date"})

    def test_job_card_has_no_party_field_and_uses_posting_date(self):
        # Job Card breadth (2026-07-21, campaign Wave 3, third row) — the nineteenth supported
        # doctype. party_field=None: a shop-floor operation record, zero customer/supplier/party
        # fields anywhere across all 95 enumerated fields (job_card.json) — no party concept to
        # gate at all, unlike Blanket Order's own two gated fields. status confirmed present (8
        # options), grand_total confirmed absent (only total_completed_qty/total_time_in_mins,
        # both non-monetary Float counters; no accounts child table).
        # date_field="posting_date" — confirmed present (Date, default "Today"), KEEPS the
        # default; the plainest date-field landing since Dunning's. submit_via="run_method" —
        # confirmed by reading all 1875 lines of job_card.py: no def submit/def cancel override,
        # only on_submit (776)/on_cancel (783) hooks (plus 14 separate @frappe.whitelist()
        # callables outside the submit/cancel surface entirely — see erpnext.py's own module
        # docstring for the side-surface caveat).
        self.assertEqual(SUPPORTED_DOCTYPES[JOB_CARD],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_job_card_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as every other party_field=None doctype's own test above — a deliberate
        # value, never an accident of a missing key or a blank-string placeholder that could
        # silently splice a literal "" into the requested field list.
        cfg = SUPPORTED_DOCTYPES[JOB_CARD]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_bom_is_dateless_partyless_and_rides_run_method(self):
        # BOM breadth (2026-07-21, campaign Wave 3, fourth row) — the twentieth supported doctype
        # and THE FIRST DATELESS ONE. party_field=None: a product recipe/structure, not a
        # transaction — zero customer/supplier/party fields across all 94 enumerated fields
        # (bom.json; the dossier's own "157-field scan" was a copy-slip from purchase_order.json,
        # corrected by re-enumeration). date_field=None — NOT a fieldname: the explicit
        # source-verified pin that BOM carries no date field at all (zero Date/Datetime-typed
        # fields among the 94; frappe's creation metadata deliberately NOT a stand-in).
        # submit_via="run_method" — BOM(WebsiteGenerator) overrides neither submit() nor cancel()
        # (bom.py:104; only on_submit/on_cancel hooks at 397/401, plus 10 separate
        # @frappe.whitelist() callables outside the submit/cancel surface entirely — see
        # erpnext.py's own module docstring for the update_cost submitted-mutation caveat).
        self.assertEqual(SUPPORTED_DOCTYPES[BOM],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": None})

    def test_bom_date_field_is_none_not_a_missing_key_or_blank_string(self):
        # The dateless pin must be a DELIBERATE None, never an accident: a missing key would fall
        # back to _date_field_for's "posting_date" default (a field bom.json does not carry —
        # every governed BOM write would then hard-deny on the non-ISO empty read), and a blank
        # string would be fed to doc.get()/get_period_locks as a bogus fieldname. Same discipline
        # as every party_field=None test above, applied to the date axis for the first time.
        cfg = SUPPORTED_DOCTYPES[BOM]
        self.assertIn("date_field", cfg)
        self.assertIsNone(cfg["date_field"])

    def test_work_order_is_partyless_datetime_dated_and_rides_run_method(self):
        # Work Order breadth (2026-07-21, campaign Wave 3, fifth row) — the twenty-first
        # supported doctype and THE FIRST DATETIME-DATED ONE. party_field=None: an internal
        # manufacturing order, zero party fields across all 86 enumerated fields
        # (work_order.json). date_field="planned_start_date" — a DATETIME (reqd, default "now",
        # allow_on_submit), NOT posting_date/transaction_date (both confirmed absent): the raw
        # read carries a time part, projected to its date part by tools.py's _posting_date_of
        # (the dossier's own §3 missed the fieldtype and recommended the plain fieldname swap,
        # which would have hard-denied every Work Order write on the non-ISO shape).
        # submit_via="run_method" — WorkOrder(Document) overrides neither submit() nor cancel()
        # (work_order.py:70; hooks only, plus 19 whitelist callables incl. TWO submitted-state
        # status mutators — see erpnext.py's own module docstring).
        self.assertEqual(SUPPORTED_DOCTYPES[WORK_ORDER],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "planned_start_date"})

    def test_asset_is_partyless_gl_posting_dated_and_rides_run_method(self):
        # Asset breadth (2026-07-21, campaign Wave 3, sixth and final row) — the twenty-second
        # supported doctype. party_field=None: the asset_owner/supplier/customer trio is
        # ownership METADATA, never a GL party (the submit GL debits fixed-asset, credits CWIP —
        # no party account anywhere; the dossier's own §1 honest assessment concurs).
        # date_field="available_for_use_date" — THE GL POSTING DATE (make_gl_entries stamps it
        # on both rows, asset.py:942/959; the deferred-GL daily scheduler keys on the same
        # field), chosen over the also-real purchase_date. Asset IS in period_closing_doctypes —
        # the first Wave-3 row whose closed-books check is natively EQUAL to ERPNext.
        # submit_via="run_method" — Asset(AccountsController) overrides neither submit() nor
        # cancel() (asset.py:41; hooks only, plus 15 whitelist document factories — see
        # erpnext.py's own module docstring).
        self.assertEqual(SUPPORTED_DOCTYPES[ASSET],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "available_for_use_date"})

    def test_packing_slip_is_dateless_companyless_partyless_and_rides_run_method(self):
        # Packing Slip breadth (2026-07-21, campaign Wave 4, first row) — the twenty-third
        # supported doctype and THE SECOND DATELESS ONE (reusing BOM's own NO_DATE_FIELD
        # machinery unchanged, not new machinery). party_field=None: a shipment-packing record —
        # the only Link fields across all 22 enumerated fields (packing_slip.json) are
        # delivery_note (AT a Delivery Note, required) and letter_head (cosmetic) — no party
        # concept at all. date_field=None — NOT a fieldname: the explicit source-verified pin
        # that Packing Slip carries no date field at all (zero Date/Datetime-typed fields among
        # the 22). submit_via="run_method" — PackingSlip(StatusUpdater) overrides neither
        # submit() nor cancel() (packing_slip.py:13; only on_submit/on_cancel hooks at 73/76,
        # both one-line calls to update_prevdoc_status(), plus ONE separate
        # @frappe.whitelist() callable outside the submit/cancel surface entirely — see
        # erpnext.py's own module docstring). A genuinely NEW finding this config does not
        # encode (there is no "company_field" key in this broker's design): Packing Slip also
        # carries no "company" field at all — see TestPackingSlipCompanylessBooks in
        # test_tools.py for the load-bearing behavioral proof.
        self.assertEqual(SUPPORTED_DOCTYPES[PACKING_SLIP],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": None})

    def test_packing_slip_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as every other party_field=None doctype's own test above.
        cfg = SUPPORTED_DOCTYPES[PACKING_SLIP]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_packing_slip_date_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as test_bom_date_field_is_none_not_a_missing_key_or_blank_string,
        # applied to the SECOND dateless doctype: a missing key would fall back to
        # _date_field_for's "posting_date" default (a field packing_slip.json does not carry —
        # every governed Packing Slip write would then hard-deny on the non-ISO empty read).
        cfg = SUPPORTED_DOCTYPES[PACKING_SLIP]
        self.assertIn("date_field", cfg)
        self.assertIsNone(cfg["date_field"])

    def test_cost_center_allocation_is_partyless_dated_and_rides_run_method(self):
        # Cost Center Allocation breadth (2026-07-21, campaign Wave 4, second row) — the
        # twenty-fourth supported doctype, and A DOSSIER CORRECTION settled from source: the
        # dossier's own §3/§12 claimed BOTH a real, required valid_from Date field AND "DATELESS
        # in the broker sense" — an internally contradictory pair. Verified from
        # cost_center_allocation.json:27-34: valid_from IS a real Date field (reqd, default
        # "Today") — the declared-dateless pin is reserved for a doctype with ZERO Date/Datetime
        # fields (BOM/Packing Slip's own proof), not this one. party_field=None: a
        # recurring-allocation rule, not a transaction with a counterparty — main_cost_center
        # (routing destination) and company (fetched) are the only Link fields across all 7
        # enumerated fields. date_field="valid_from" — the SIXTH distinct date-fieldname pattern
        # (posting_date/transaction_date/from_date/planned_start_date/available_for_use_date/
        # valid_from), riding the existing generic date machinery unchanged (same shape Blanket
        # Order's from_date and Asset's available_for_use_date already proved generalizes) — the
        # closed-books check runs the NORMAL, equal-or-stricter path, never the dateless
        # sentinel. company IS present (unlike Packing Slip) — fetch_from
        # main_cost_center.company, reqd — the standing "wrong books" belt applies in its
        # ordinary form. submit_via="run_method" — confirmed by reading all 160 lines of
        # cost_center_allocation.py: class CostCenterAllocation(Document) overrides neither
        # submit() nor cancel(), and defines NO on_submit/on_cancel hook of any kind (only
        # __init__/validate/clear_cache exist) — the simplest submit/cancel lifecycle this
        # campaign has found.
        self.assertEqual(SUPPORTED_DOCTYPES[COST_CENTER_ALLOCATION],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "valid_from"})

    def test_cost_center_allocation_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as every other party_field=None doctype's own test above.
        cfg = SUPPORTED_DOCTYPES[COST_CENTER_ALLOCATION]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_cost_center_allocation_date_field_is_valid_from_not_none(self):
        # The inverse discipline of the dateless pin tests above: this landing's whole point is
        # that date_field must NOT be None here, despite the dossier's own contradictory claim —
        # a real, source-verified fieldname, never the NO_DATE_FIELD sentinel (which would have
        # been the wrong call: valid_from IS present and required).
        cfg = SUPPORTED_DOCTYPES[COST_CENTER_ALLOCATION]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "valid_from")
        self.assertIsNotNone(cfg["date_field"])

    def test_supplier_scorecard_period_is_dated_partial_and_rides_run_method(self):
        # Supplier Scorecard Period breadth (2026-07-21, campaign Wave 4, third row) — the
        # twenty-fifth supported doctype, and Wave 4's FIRST ROW WITH A REAL PARTY FIELD (Packing
        # Slip/Cost Center Allocation both carried party_field=None). party_field="supplier" — a
        # real, required, header-level Link (supplier_scorecard_period.json lines 23-30), the same
        # shape Purchase Order/Purchase Receipt/Supplier Quotation already established. A dossier
        # framing correction: the dossier's own §1 calls this a "GL party" — misleading, since
        # this doctype posts no GL at all (see the ledger-preview test below); the pin is decided
        # on the field's own reality, not that framing. date_field="start_date" — a real, required
        # Date field (no default), chosen over the doctype's OTHER real Date field (end_date) as
        # the period's own anchor — the same "window start, not its close" convention Blanket
        # Order's from_date established — the SEVENTH distinct date-fieldname pattern. "company"
        # is confirmed ABSENT (a dossier omission, the SECOND companyless doctype after Packing
        # Slip — see TestSupplierScorecardPeriodCompanylessBooks in test_tools.py).
        # submit_via="run_method" — confirmed by reading all 161 lines of
        # supplier_scorecard_period.py: class SupplierScorecardPeriod(Document) overrides neither
        # submit() nor cancel(), and defines NO on_submit/on_cancel hook of any kind (only
        # validate + its own six helpers exist) — the same simplest submit/cancel lifecycle Cost
        # Center Allocation's own landing established.
        self.assertEqual(SUPPORTED_DOCTYPES[SUPPLIER_SCORECARD_PERIOD],
                         {"party_field": "supplier", "submit_via": "run_method",
                          "date_field": "start_date"})

    def test_supplier_scorecard_period_party_field_is_supplier_not_none(self):
        # The inverse discipline of every party_field=None doctype's own test above: this
        # landing's whole point is that party_field must NOT be None here — Wave 4's first row
        # with a genuinely real, source-verified party fieldname.
        cfg = SUPPORTED_DOCTYPES[SUPPLIER_SCORECARD_PERIOD]
        self.assertIn("party_field", cfg)
        self.assertEqual(cfg["party_field"], "supplier")
        self.assertIsNotNone(cfg["party_field"])

    def test_supplier_scorecard_period_date_field_is_start_date_not_none(self):
        cfg = SUPPORTED_DOCTYPES[SUPPLIER_SCORECARD_PERIOD]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "start_date")
        self.assertIsNotNone(cfg["date_field"])

    def test_quality_inspection_is_partyless_dated_and_rides_run_method(self):
        # Quality Inspection breadth (2026-07-21, campaign Wave 4, fourth row) — the twenty-sixth
        # supported doctype, and the FIRST DOCTYPE ON A DYNAMIC LINK PAIR SINCE QUOTATION.
        # party_field=None — reference_type/reference_name (both reqd), never a static Customer/
        # Supplier field, the same shape Quotation's own quotation_to/party_name pair already
        # established. date_field="report_date" — a real, required Date field (default "Today"),
        # the EIGHTH distinct date-fieldname pattern. "company" IS present on this schema (unlike
        # Packing Slip/Supplier Scorecard Period) though not reqd — set programmatically from the
        # reference document (set_company()); a dossier "GL party fixture" framing corrected (this
        # doctype posts no GL at all — see the ledger-preview test below).
        # submit_via="run_method" — confirmed by reading all 524 lines of quality_inspection.py:
        # class QualityInspection(Document) overrides neither submit() nor cancel(), only
        # on_discard/on_update/on_submit/on_cancel/on_trash/before_submit hooks are defined.
        self.assertEqual(SUPPORTED_DOCTYPES[QUALITY_INSPECTION],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "report_date"})

    def test_quality_inspection_party_field_is_none_and_uses_dynamic_reference_pair(self):
        # Same discipline as Quotation's own test: party_field=None is a deliberate value here
        # too (Quotation's own shape, not RFQ's multiple-supplier-child-table one) — never an
        # accident of a missing dict key or a blank-string placeholder that could silently splice
        # a literal "" into the requested field list.
        cfg = SUPPORTED_DOCTYPES[QUALITY_INSPECTION]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_quality_inspection_date_field_is_report_date_not_none(self):
        cfg = SUPPORTED_DOCTYPES[QUALITY_INSPECTION]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "report_date")
        self.assertIsNotNone(cfg["date_field"])

    def test_installation_note_is_partied_dated_and_rides_run_method(self):
        # Installation Note breadth (2026-07-21, campaign Wave 4, fifth row) — the
        # twenty-seventh supported doctype, and Wave 4's SECOND ROW WITH A REAL PARTY FIELD
        # (after Supplier Scorecard Period). party_field="customer" — a real, required,
        # header-level Link (installation_note.json lines 57-69), never a Dynamic Link pair.
        # date_field="inst_date" — a real, required Date field (default none), the NINTH
        # distinct date-fieldname pattern. "company" IS present and required (unlike Packing
        # Slip/Supplier Scorecard Period) — the standing "wrong books" belt applies in its
        # ordinary form. submit_via="run_method" — confirmed by reading all 133 lines of
        # installation_note.py: class InstallationNote(TransactionBase) overrides neither
        # submit() nor cancel() anywhere.
        self.assertEqual(SUPPORTED_DOCTYPES[INSTALLATION_NOTE],
                         {"party_field": "customer", "submit_via": "run_method",
                          "date_field": "inst_date"})

    def test_installation_note_party_field_is_the_real_customer_link(self):
        cfg = SUPPORTED_DOCTYPES[INSTALLATION_NOTE]
        self.assertIn("party_field", cfg)
        self.assertEqual(cfg["party_field"], "customer")

    def test_installation_note_date_field_is_inst_date_not_none(self):
        cfg = SUPPORTED_DOCTYPES[INSTALLATION_NOTE]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "inst_date")
        self.assertIsNotNone(cfg["date_field"])

    def test_shipment_is_partyless_dated_and_rides_run_method(self):
        # Shipment breadth (2026-07-21, campaign Wave 4, sixth row) — the twenty-eighth supported
        # doctype, and the FIRST DOCTYPE WITH TWO INDEPENDENT DYNAMIC-SELECTOR PAIRS.
        # party_field=None — pickup_from_type/delivery_to_type each gate a trio of mutually
        # exclusive Company/Customer/Supplier Links (shipment.json:72-78/150-156), never
        # server-enforced (confirmed by reading all 148 lines of shipment.py) — a THIRD distinct
        # reason for party_field=None, after Quotation's Dynamic Link and Blanket Order's
        # two-gated-Links shape. date_field="pickup_date" — a real, required Date field
        # (allow_on_submit=1), the TENTH distinct date-fieldname pattern. "company" is CONFIRMED
        # ABSENT (unlike Installation Note/Quality Inspection) — the third companyless doctype
        # after Packing Slip/Supplier Scorecard Period. submit_via="run_method" — confirmed by
        # reading all 148 lines of shipment.py: class Shipment(Document) overrides neither
        # submit() nor cancel() anywhere.
        self.assertEqual(SUPPORTED_DOCTYPES[SHIPMENT],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "pickup_date"})

    def test_shipment_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as Quotation's/RFQ's/Blanket Order's own tests: party_field=None is a
        # deliberate value here too (TWO independent gated trios, never a single fieldname) —
        # never an accident of a missing dict key or a blank-string placeholder that could
        # silently splice a literal "" into the requested field list.
        cfg = SUPPORTED_DOCTYPES[SHIPMENT]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_shipment_date_field_is_pickup_date_not_none(self):
        cfg = SUPPORTED_DOCTYPES[SHIPMENT]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "pickup_date")
        self.assertIsNotNone(cfg["date_field"])

    def test_sales_forecast_has_no_party_field_and_uses_posting_date(self):
        # Sales Forecast breadth (2026-07-21, campaign Wave 4, seventh row) — the twenty-ninth
        # supported doctype. party_field=None: a demand-planning fixture, zero customer/supplier/
        # party fields anywhere across all 20 enumerated fields (sales_forecast.json) — company is
        # metadata only. status confirmed present (Planned/MPS Generated/Cancelled, read_only,
        # in_list_view) but carries NO schema default and NO code path ever writes "Planned" (a
        # narrowing of the dossier's own §11 wording — see erpnext.py's own module docstring).
        # grand_total confirmed absent with no substitute of any kind. date_field="posting_date" —
        # chosen over the ALSO-real from_date (reqd but not in_list_view). submit_via="run_method"
        # — confirmed by reading all 92 lines of sales_forecast.py: no def submit/def cancel
        # override, and neither on_submit nor on_cancel is defined at all (only on_discard, a
        # draft-only hook this broker's own cancel path never reaches).
        self.assertEqual(SUPPORTED_DOCTYPES[SALES_FORECAST],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_sales_forecast_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as every other party_field=None doctype's own test above — a deliberate
        # value, never an accident of a missing key or a blank-string placeholder that could
        # silently splice a literal "" into the requested field list.
        cfg = SUPPORTED_DOCTYPES[SALES_FORECAST]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_sales_forecast_date_field_is_posting_date_not_none(self):
        cfg = SUPPORTED_DOCTYPES[SALES_FORECAST]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "posting_date")
        self.assertIsNotNone(cfg["date_field"])

    def test_project_update_has_no_party_field_and_uses_date_field(self):
        # Project Update breadth (2026-07-21, campaign Wave 4, eighth row) — the thirtieth
        # supported doctype, the NARROWEST schema this campaign has found (9 fields total).
        # party_field=None: only "project" and "amended_from" are Link fields, neither a party.
        # status AND grand_total both confirmed absent — the ONLY in_list_view field anywhere is
        # "project" itself. company also confirmed absent (the FOURTH companyless doctype after
        # Packing Slip/Supplier Scorecard Period/Shipment). date_field="date" — a REAL declared
        # field (never the date_field=None dateless pin) that carries reqd=0 AND no schema
        # default, a genuinely new combination (see erpnext.py's own module docstring for the
        # full "blank date on an API-authored draft" governability argument). submit_via=
        # "run_method" — confirmed by reading all 29 lines of project_update.py: class
        # ProjectUpdate(Document) carries a bare "pass" body, no submit/cancel override, no
        # validate(), no hook of any kind.
        self.assertEqual(SUPPORTED_DOCTYPES[PROJECT_UPDATE],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "date"})

    def test_project_update_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as every other party_field=None doctype's own test above — a deliberate
        # value, never an accident of a missing key or a blank-string placeholder that could
        # silently splice a literal "" into the requested field list.
        cfg = SUPPORTED_DOCTYPES[PROJECT_UPDATE]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_project_update_date_field_is_date_not_none(self):
        cfg = SUPPORTED_DOCTYPES[PROJECT_UPDATE]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "date")
        self.assertIsNotNone(cfg["date_field"])

    def test_maintenance_visit_has_a_real_party_field_and_uses_mntc_date(self):
        # Maintenance Visit breadth (2026-07-21, campaign Wave 4, ninth row) — the thirty-first
        # supported doctype. party_field="customer" — a real, required, header-level Link
        # (maintenance_visit.json lines 65-74), the same static-party shape Installation Note/
        # Supplier Scorecard Period already established. status confirmed present (Draft/
        # Cancelled/Submitted, read_only) alongside a SEPARATE completion_status Select
        # (Partially/Fully Completed) — orthogonal fields, neither one the other; grand_total
        # confirmed absent (32 fields enumerated, no Currency/Float/Percent anywhere).
        # date_field="mntc_date" — Date, reqd=1, default="Today" — the twelfth date-fieldname
        # pattern, and the first to carry BOTH reqd AND a schema default together. submit_via=
        # "run_method" — confirmed by reading all 210 lines of maintenance_visit.py: class
        # MaintenanceVisit(TransactionBase) overrides neither submit() nor cancel() anywhere.
        self.assertEqual(SUPPORTED_DOCTYPES[MAINTENANCE_VISIT],
                         {"party_field": "customer", "submit_via": "run_method",
                          "date_field": "mntc_date"})

    def test_maintenance_visit_party_field_is_the_real_customer_link(self):
        cfg = SUPPORTED_DOCTYPES[MAINTENANCE_VISIT]
        self.assertIn("party_field", cfg)
        self.assertEqual(cfg["party_field"], "customer")

    def test_maintenance_visit_date_field_is_mntc_date_not_none(self):
        cfg = SUPPORTED_DOCTYPES[MAINTENANCE_VISIT]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "mntc_date")
        self.assertIsNotNone(cfg["date_field"])

    def test_maintenance_schedule_has_an_optional_party_field_and_uses_transaction_date(self):
        # Maintenance Schedule breadth (2026-07-21, campaign Wave 4, tenth and last row) — the
        # thirty-second supported doctype. party_field="customer" — a real, header-level Link,
        # but the FIRST party_field row this whole campaign has spliced where the field itself
        # carries NO "reqd" key at all (maintenance_schedule.json — confirmed absent, not 0),
        # unlike Installation Note's/Maintenance Visit's own required customer. Decided on the
        # field's reality (real, static, singular header Link), never its reqd-ness. status
        # confirmed present (Draft/Submitted/Cancelled, read_only, default "Draft"); grand_total
        # confirmed absent (24 fields enumerated, no Currency/Float/Percent anywhere).
        # date_field="transaction_date" — REJOINS the standing SO/PO/MR/SQ/Q/RFQ set as its
        # seventh member, zero new date-fieldname plumbing. submit_via="run_method" — confirmed
        # by reading all 495 lines of maintenance_schedule.py: class
        # MaintenanceSchedule(TransactionBase) overrides neither submit() nor cancel() anywhere.
        self.assertEqual(SUPPORTED_DOCTYPES[MAINTENANCE_SCHEDULE],
                         {"party_field": "customer", "submit_via": "run_method",
                          "date_field": "transaction_date"})

    def test_maintenance_schedule_party_field_is_the_real_but_optional_customer_link(self):
        cfg = SUPPORTED_DOCTYPES[MAINTENANCE_SCHEDULE]
        self.assertIn("party_field", cfg)
        self.assertEqual(cfg["party_field"], "customer")

    def test_maintenance_schedule_date_field_is_transaction_date_not_none(self):
        cfg = SUPPORTED_DOCTYPES[MAINTENANCE_SCHEDULE]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "transaction_date")
        self.assertIsNotNone(cfg["date_field"])

    def test_asset_maintenance_log_is_partyless_companyless_and_uses_completion_date(self):
        # Asset Maintenance Log breadth (2026-07-21, campaign Wave 5, first row) — the
        # thirty-third supported doctype. party_field=None — asset_maintenance/task are
        # operational routing Links, never a GL party (confirmed absent across all 23 fields).
        # maintenance_status confirmed present (Planned/Completed/Cancelled/Overdue, reqd, NO
        # read_only/default at all — the FIRST campaign doctype whose lifecycle Select is
        # writable rather than hook-stamped); grand_total confirmed absent. TWO real Date fields
        # exist (due_date: read-only, fetch_from task.next_due_date; completion_date: writable) —
        # date_field="completion_date" is the operational one, the THIRTEENTH distinct
        # date-fieldname pattern. company confirmed absent — the FIFTH companyless doctype after
        # Packing Slip/Supplier Scorecard Period/Shipment/Project Update. submit_via="run_method"
        # — confirmed by reading all 97 lines of asset_maintenance_log.py: class
        # AssetMaintenanceLog(Document) overrides neither submit() nor cancel() anywhere.
        self.assertEqual(SUPPORTED_DOCTYPES[ASSET_MAINTENANCE_LOG],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "completion_date"})

    def test_asset_maintenance_log_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as Quotation's/RFQ's/Blanket Order's/Shipment's own tests: party_field
        # is a deliberate None here (no party concept at all), never an accident of a missing
        # dict key or a blank-string placeholder that could silently splice a literal "" into the
        # requested field list.
        cfg = SUPPORTED_DOCTYPES[ASSET_MAINTENANCE_LOG]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_asset_maintenance_log_date_field_is_completion_date_not_due_date(self):
        cfg = SUPPORTED_DOCTYPES[ASSET_MAINTENANCE_LOG]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "completion_date")
        self.assertIsNotNone(cfg["date_field"])
        self.assertNotEqual(cfg["date_field"], "due_date")  # the read-only reference, never this

    def test_bank_guarantee_has_dual_conditional_party_and_is_companyless(self):
        # Bank Guarantee breadth (2026-07-21, campaign Wave 5, second row) — the thirty-fourth
        # supported doctype. party_field=None — but a genuine DUAL CONDITIONAL pair, not "no party
        # concept at all": customer (depends_on doc.reference_doctype=="Sales Order") and supplier
        # (depends_on doc.reference_doctype=="Purchase Order") are both real, static header Link
        # fields, populated per bg_type (Receiving/Providing) via a client-side handler only
        # (bank_guarantee.js:36-42) — the Blanket Order shape. status confirmed absent (docstatus
        # only); grand_total confirmed absent (amount, the sole in_list_view Currency field, is
        # the aggregate substitute). company confirmed absent — the SIXTH companyless doctype
        # after Packing Slip/Supplier Scorecard Period/Shipment/Project Update/Asset Maintenance
        # Log. submit_via="run_method" — a DOSSIER CORRECTION: the dossier claims client_rpc
        # citing "an on_submit override", but on_submit is a HOOK method (bank_guarantee.py:49),
        # never a def submit(self)/def cancel(self) override — confirmed absent by a full 74-line
        # read of bank_guarantee.py. SUBMIT_VIA_CLIENT_RPC stays pinned to exactly Journal
        # Entry/Stock Reconciliation (see test_only_journal_entry_and_stock_reconciliation_use_
        # client_rpc below, unchanged by this landing).
        self.assertEqual(SUPPORTED_DOCTYPES[BANK_GUARANTEE],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "start_date"})

    def test_bank_guarantee_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as Quotation's/RFQ's/Blanket Order's/Shipment's/Asset Maintenance Log's
        # own tests: party_field is a deliberate None here (a dual conditional pair, never a
        # single static fieldname), never an accident of a missing dict key or a blank-string
        # placeholder that could silently splice a literal "" into the requested field list.
        cfg = SUPPORTED_DOCTYPES[BANK_GUARANTEE]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_bank_guarantee_date_field_is_start_date_not_end_date(self):
        # start_date REJOINS Supplier Scorecard Period's own SEVENTH date-fieldname pattern (see
        # test_supplier_scorecard_period_and_bank_guarantee_are_the_only_start_date_users below) —
        # the second doctype on it, not a new one. end_date is a genuine dossier gap: it is
        # read-only and described as "calculated from start_date + validity", but the calculation
        # is CLIENT-SIDE JS ONLY (bank_guarantee.js:65-72) — confirmed absent from
        # bank_guarantee.py entirely, so it is never this doctype's own governed date_field.
        cfg = SUPPORTED_DOCTYPES[BANK_GUARANTEE]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "start_date")
        self.assertIsNotNone(cfg["date_field"])
        self.assertNotEqual(cfg["date_field"], "end_date")  # client-JS-derived, never governed

    def test_asset_movement_is_partyless_datetime_dated_and_rides_run_method(self):
        # Asset Movement breadth (2026-07-21, campaign Wave 5, third row) — the thirty-fifth
        # supported doctype, and the SECOND Datetime-dated doctype in this campaign (after Work
        # Order). party_field=None: reference_doctype/reference_name (Dynamic Link, provenance
        # only — the seeding Purchase Receipt/Purchase Invoice) and the child table's own
        # from_employee/to_employee are never a GL party. date_field="transaction_date" — a real,
        # required Datetime (default "Now"), the SAME literal fieldname seven OTHER supported
        # doctypes already use for a Date-typed field — the collision is purely nominal (see
        # test_only_so_po_mr_sq_q_rfq_maintenance_schedule_and_asset_movement_use_transaction_date
        # below); the actual Datetime behavior is proven in test_tools.py's own
        # TestAssetMovementDatetimeDateProjection. submit_via="run_method" —
        # AssetMovement(Document) overrides neither submit() nor cancel() (asset_movement.py:13;
        # 13 methods total, only on_submit/on_cancel hooks at lines 116/119).
        self.assertEqual(SUPPORTED_DOCTYPES[ASSET_MOVEMENT],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "transaction_date"})

    def test_asset_movement_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as every other party_field=None doctype's own test above.
        cfg = SUPPORTED_DOCTYPES[ASSET_MOVEMENT]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_asset_movement_date_field_is_transaction_date_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[ASSET_MOVEMENT]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "transaction_date")
        self.assertIsNotNone(cfg["date_field"])

    def test_delivery_trip_is_partyless_datetime_dated_and_rides_run_method(self):
        # Delivery Trip breadth (2026-07-21) — the thirty-sixth supported doctype, and the THIRD
        # Datetime-dated doctype in this campaign (after Work Order and Asset Movement).
        # party_field=None — company IS present (reqd), NOT companyless; driver/employee are
        # metadata only, never a GL party. date_field="departure_time" — a real, required
        # Datetime with NO schema default (unlike Asset Movement's own default "Now") — a
        # brand-new fourteenth date-fieldname pattern, no collision with any prior member (see
        # test_delivery_trip_is_the_only_departure_time_user below). submit_via="run_method" —
        # DeliveryTrip(Document) overrides neither submit() nor cancel() (delivery_trip.py:14; 15
        # methods total, only on_submit/on_cancel hooks at lines 71-72/77-79).
        self.assertEqual(SUPPORTED_DOCTYPES[DELIVERY_TRIP],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "departure_time"})

    def test_delivery_trip_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as every other party_field=None doctype's own test above.
        cfg = SUPPORTED_DOCTYPES[DELIVERY_TRIP]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_delivery_trip_date_field_is_departure_time_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[DELIVERY_TRIP]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "departure_time")
        self.assertIsNotNone(cfg["date_field"])

    def test_asset_value_adjustment_is_partyless_dated_and_rides_run_method(self):
        # Asset Value Adjustment breadth (2026-07-21) — the thirty-seventh supported doctype, and
        # the campaign's FIRST sibling-document FACTORY row. party_field=None — company IS
        # present (asset_value_adjustment.json line 28) but carries no reqd key (the THIRD such
        # row after Purchase Invoice/Quality Inspection, never before consequential: a blank
        # company here dooms the synchronously-built sibling Journal Entry's own mandatory
        # company field). date_field="date" — a real, required Date field, REJOINS Project
        # Update's own eleventh date pattern (see
        # test_project_update_and_asset_value_adjustment_are_the_only_date_users below).
        # submit_via="run_method" — AssetValueAdjustment(Document) overrides neither submit() nor
        # cancel() (asset_value_adjustment.py:21; 11 methods total, only on_submit/on_cancel
        # hooks at lines 66/76).
        self.assertEqual(SUPPORTED_DOCTYPES[ASSET_VALUE_ADJUSTMENT],
                         {"party_field": None, "submit_via": "run_method", "date_field": "date"})

    def test_asset_value_adjustment_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as every other party_field=None doctype's own test above.
        cfg = SUPPORTED_DOCTYPES[ASSET_VALUE_ADJUSTMENT]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_asset_value_adjustment_date_field_is_date_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[ASSET_VALUE_ADJUSTMENT]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "date")
        self.assertIsNotNone(cfg["date_field"])

    def test_payment_order_is_partyless_posting_dated_and_rides_run_method(self):
        # Payment Order breadth (2026-07-21) — the thirty-eighth supported doctype.
        # party_field=None — a CORRECTION from the dossier's own "party_field=party" claim: the
        # party field carries a real depends_on (payment_order_type=='Payment Request') and is
        # never read or written anywhere in payment_order.py's own code — the "no single static
        # fieldname is ever unconditionally the party" rule, here for a SINGLE (not dual)
        # conditional field for the first time. date_field="posting_date" — REJOINS the largest
        # pattern in this table (its 14th member), never a new one. submit_via="run_method" —
        # PaymentOrder(Document) defines exactly two methods on the whole class (on_submit/
        # on_cancel, both hooks; no validate(), no def submit/def cancel anywhere).
        self.assertEqual(SUPPORTED_DOCTYPES[PAYMENT_ORDER],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_payment_order_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as every other party_field=None doctype's own test above.
        cfg = SUPPORTED_DOCTYPES[PAYMENT_ORDER]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_payment_order_date_field_is_posting_date_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[PAYMENT_ORDER]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "posting_date")
        self.assertIsNotNone(cfg["date_field"])

    def test_share_transfer_is_partyless_dated_and_rides_run_method(self):
        # Share Transfer breadth (2026-07-21) — the thirty-ninth supported doctype, a
        # full-attention landing off the pre-verification addendum
        # (docs/plans/dossiers/share_transfer.verify.md). party_field=None — from_shareholder/
        # to_shareholder is a conditional pair (the addendum's correction #1: the ORIGINAL
        # dossier had the two depends_on directions SWAPPED; the real shape is from_shareholder
        # hidden only on Issue, to_shareholder hidden only on Purchase — confirmed via
        # share_transfer.json AND basic_validations()'s own blanking, share_transfer.py:171/179).
        # company IS present and reqd (share_transfer.json, "reqd": 1) — NOT companyless.
        # date_field="date" — a real, required Date field, REJOINS the standing date_field="date"
        # pattern (Project Update, Asset Value Adjustment) as its THIRD member.
        # submit_via="run_method" — ShareTransfer(Document) defines 11 methods, none shadowing
        # submit()/cancel(); on_submit/on_cancel are plain hooks (share_transfer.py:45/97).
        self.assertEqual(SUPPORTED_DOCTYPES[SHARE_TRANSFER],
                         {"party_field": None, "submit_via": "run_method", "date_field": "date"})

    def test_share_transfer_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as every other party_field=None doctype's own test above.
        cfg = SUPPORTED_DOCTYPES[SHARE_TRANSFER]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_share_transfer_date_field_is_date_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[SHARE_TRANSFER]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "date")
        self.assertIsNotNone(cfg["date_field"])

    def test_bom_creator_is_dateless_partyless_and_run_method(self):
        # BOM Creator breadth (2026-07-21) — the fortieth supported doctype, John's ruling 2 (the
        # two-phase PROVE). party_field=None (no Customer/Supplier/Party field anywhere across all
        # 40 enumerated fields, bom_creator.json) — company IS present and reqd, NOT companyless.
        # date_field=None — THE THIRD DATELESS doctype (after BOM/Packing Slip), a direct
        # field-type enumeration finding zero Date/Datetime fields. submit_via="run_method" — no
        # def submit()/def cancel() override anywhere in bom_creator.py (only on_submit/on_cancel/
        # before_submit HOOKS).
        self.assertEqual(SUPPORTED_DOCTYPES[BOM_CREATOR],
                         {"party_field": None, "submit_via": "run_method", "date_field": None})

    def test_bom_creator_party_field_is_none_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[BOM_CREATOR]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_bom_creator_date_field_is_the_declared_dateless_none_not_a_missing_key(self):
        cfg = SUPPORTED_DOCTYPES[BOM_CREATOR]
        self.assertIn("date_field", cfg)
        self.assertIsNone(cfg["date_field"])

    def test_enqueue_on_submit_doctypes_contains_exactly_bom_creator(self):
        # Membership is source-receipted (like SELF_UNLINKING_DOCTYPES) — see
        # pacioli.erpnext.ENQUEUE_ON_SUBMIT_DOCTYPES's own docstring for the on_submit receipt.
        self.assertEqual(ENQUEUE_ON_SUBMIT_DOCTYPES, (BOM_CREATOR,))
        self.assertEqual(ENQUEUE_ON_SUBMIT_CHANNELS[BOM_CREATOR],
                         ("bom_creator.create_boms", "short"))

    def test_exactly_fifty_one_doctypes_supported(self):
        # THE ROOF ROW — Subcontracting Receipt lands as the 51st and FINAL GOVERN doctype.
        self.assertEqual(set(SUPPORTED_DOCTYPES),
                         {SALES_INVOICE, PURCHASE_INVOICE, PAYMENT_ENTRY, JOURNAL_ENTRY,
                          SALES_ORDER, PURCHASE_ORDER, MATERIAL_REQUEST, DELIVERY_NOTE,
                          PURCHASE_RECEIPT, STOCK_ENTRY, SUPPLIER_QUOTATION, QUOTATION,
                          POS_INVOICE, DUNNING, STOCK_RECONCILIATION, LANDED_COST_VOUCHER,
                          REQUEST_FOR_QUOTATION, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER,
                          ASSET, PACKING_SLIP, COST_CENTER_ALLOCATION,
                          SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
                          SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT,
                          MAINTENANCE_SCHEDULE, ASSET_MAINTENANCE_LOG, BANK_GUARANTEE,
                          ASSET_MOVEMENT, DELIVERY_TRIP, ASSET_VALUE_ADJUSTMENT, PAYMENT_ORDER,
                          SHARE_TRANSFER, BOM_CREATOR, BUDGET, TIMESHEET, CONTRACT, PICK_LIST,
                          ASSET_REPAIR, INVOICE_DISCOUNTING, ASSET_CAPITALIZATION,
                          PRODUCTION_PLAN, SUBCONTRACTING_ORDER, SUBCONTRACTING_INWARD_ORDER,
                          SUBCONTRACTING_RECEIPT})

    def test_subcontracting_receipt_is_supplier_companied_reqd_and_rides_run_method(self):
        # Subcontracting Receipt breadth (2026-07-22) — THE ROOF ROW, the fifty-first and FINAL
        # supported doctype, off the pre-verification addendum
        # (docs/plans/dossiers/subcontracting_receipt.verify.md — 2 corrections + 5 landing
        # risks, all re-verified byte-for-byte, plus THE SIXTH LEDGER-PREVIEW SHAPE, this
        # landing's own finding beyond both dossier and addendum). party_field="supplier" (Link,
        # reqd:1, label "Job Worker") — a plain GL party, no Dynamic Link, no dual-conditional
        # pair. submit_via="run_method" — confirmed by reading the full MRO (leaf class + 3
        # ancestors): zero submit()/cancel() overrides anywhere. date_field="posting_date" — the
        # DN/PR/SE/AC default path, zero Datetime fields on the doctype at all.
        self.assertEqual(SUPPORTED_DOCTYPES[SUBCONTRACTING_RECEIPT],
                         {"party_field": "supplier", "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_subcontracting_receipt_party_field_is_supplier_not_customer(self):
        cfg = SUPPORTED_DOCTYPES[SUBCONTRACTING_RECEIPT]
        self.assertEqual(cfg["party_field"], "supplier")

    def test_subcontracting_receipt_date_field_is_posting_date_not_transaction_date(self):
        cfg = SUPPORTED_DOCTYPES[SUBCONTRACTING_RECEIPT]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "posting_date")

    def test_subcontracting_inward_order_is_customer_companied_reqd_and_rides_run_method(self):
        # Subcontracting Inward Order breadth (2026-07-22) — the fiftieth supported doctype, off
        # the pre-verification addendum
        # (docs/plans/dossiers/subcontracting_inward_order.verify.md — its own central finding,
        # THE ELEVEN-ROW MUTATOR MAP plus THE NEW BYPASS CLASS, re-verified byte-for-byte).
        # party_field="customer" (Link, reqd:1) — a plain GL party, no Dynamic Link, no
        # dual-conditional pair. submit_via="run_method" — confirmed by reading the full
        # 567-line .py: zero submit()/cancel() overrides anywhere. date_field="transaction_date"
        # — rejoins the existing set, no new pattern.
        self.assertEqual(SUPPORTED_DOCTYPES[SUBCONTRACTING_INWARD_ORDER],
                         {"party_field": "customer", "submit_via": "run_method",
                          "date_field": "transaction_date"})

    def test_subcontracting_inward_order_party_field_is_customer_not_supplier(self):
        cfg = SUPPORTED_DOCTYPES[SUBCONTRACTING_INWARD_ORDER]
        self.assertEqual(cfg["party_field"], "customer")

    def test_subcontracting_inward_order_date_field_is_transaction_date_not_posting_date(self):
        cfg = SUPPORTED_DOCTYPES[SUBCONTRACTING_INWARD_ORDER]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "transaction_date")

    def test_subcontracting_order_is_supplier_companied_reqd_and_rides_run_method(self):
        # Subcontracting Order breadth (2026-07-21) — the forty-ninth supported doctype, off the
        # pre-verification addendum (docs/plans/dossiers/subcontracting_order.verify.md — its own
        # central finding, THE SEVEN-PATH MUTATOR MAP, re-verified byte-for-byte).
        # party_field="supplier" (Link, reqd:1, label "Job Worker") — a plain GL party, no
        # Dynamic Link, no dual-conditional pair. submit_via="run_method" — confirmed by reading
        # every class in the MRO (SubcontractingOrder/SubcontractingController/StockController/
        # AccountsController/TransactionBase), zero submit()/cancel() overrides anywhere.
        # date_field="transaction_date" — rejoins the existing set, no new pattern.
        self.assertEqual(SUPPORTED_DOCTYPES[SUBCONTRACTING_ORDER],
                         {"party_field": "supplier", "submit_via": "run_method",
                          "date_field": "transaction_date"})

    def test_subcontracting_order_party_field_is_supplier_not_customer(self):
        cfg = SUPPORTED_DOCTYPES[SUBCONTRACTING_ORDER]
        self.assertEqual(cfg["party_field"], "supplier")

    def test_subcontracting_order_date_field_is_transaction_date_not_posting_date(self):
        cfg = SUPPORTED_DOCTYPES[SUBCONTRACTING_ORDER]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "transaction_date")

    def test_production_plan_is_partyless_companied_reqd_and_rides_run_method(self):
        # Production Plan breadth (2026-07-21) — the forty-eighth supported doctype, off the
        # pre-verification addendum (docs/plans/dossiers/production_plan.verify.md — unusually
        # strong, re-verified every dossier line cite, but whose own "26 branches" tally was
        # stale). party_field=None — the only party-shaped field, customer, is a conditional UI
        # filter (depends_on get_items_from=="Sales Order"), never a GL party.
        # submit_via="run_method" — confirmed by reading all 2261 lines of production_plan.py,
        # zero submit()/cancel() overrides. date_field="posting_date" — REJOINS the large
        # existing set, a plain Date among 5, no Datetime.
        self.assertEqual(SUPPORTED_DOCTYPES[PRODUCTION_PLAN],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_production_plan_party_field_is_none_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[PRODUCTION_PLAN]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_production_plan_date_field_is_posting_date(self):
        cfg = SUPPORTED_DOCTYPES[PRODUCTION_PLAN]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "posting_date")

    def test_asset_capitalization_is_partyless_companied_reqd_and_rides_run_method(self):
        # Asset Capitalization breadth (2026-07-21) — the forty-seventh supported doctype, off the
        # pre-verification addendum (docs/plans/dossiers/asset_capitalization.verify.md — whose own
        # headline RED FLAG, a target-asset cost double-count on re-submit, is ITSELF REFUTED from
        # source, and whose own period_closing_doctypes ordinal ("13th") is corrected to the 12th).
        # party_field=None — no Customer/Supplier/Party Link anywhere in the 39-field enumeration.
        # submit_via="run_method" — confirmed by reading all 882 lines of asset_capitalization.py,
        # zero submit()/cancel() overrides. date_field="posting_date" — REJOINS the large existing
        # set (paired with a separate posting_time Time field, the Stock Entry/Stock Reconciliation
        # precedent, not a new pattern).
        self.assertEqual(SUPPORTED_DOCTYPES[ASSET_CAPITALIZATION],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_asset_capitalization_party_field_is_none_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[ASSET_CAPITALIZATION]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_asset_capitalization_date_field_is_posting_date(self):
        cfg = SUPPORTED_DOCTYPES[ASSET_CAPITALIZATION]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "posting_date")

    def test_pick_list_is_customer_partied_companied_and_rides_run_method(self):
        # Pick List breadth (2026-07-21) — the forty-fourth supported doctype, off the
        # pre-verification addendum (docs/plans/dossiers/pick_list.verify.md). party_field=
        # "customer" — a plain, always-real header Link (depends_on is a pure Desk-form display
        # directive, never a data-model condition — customer is present on every draft
        # regardless of purpose). submit_via="run_method" — confirmed by a full-file grep of
        # pick_list.py (1893 lines) finding zero def submit()/def cancel() overrides.
        # date_field=None — the FOURTH NO_DATE_FIELD member (after BOM/Packing Slip/BOM Creator;
        # the addendum's own "third" was stale, written before BOM Creator's same-morning
        # landing).
        self.assertEqual(SUPPORTED_DOCTYPES[PICK_LIST],
                         {"party_field": "customer", "submit_via": "run_method",
                          "date_field": None})

    def test_pick_list_party_field_is_customer(self):
        cfg = SUPPORTED_DOCTYPES[PICK_LIST]
        self.assertEqual(cfg["party_field"], "customer")

    def test_pick_list_date_field_is_the_declared_dateless_none_not_a_missing_key(self):
        cfg = SUPPORTED_DOCTYPES[PICK_LIST]
        self.assertIn("date_field", cfg)
        self.assertIsNone(cfg["date_field"])

    def test_asset_repair_is_partyless_companied_optional_and_rides_run_method(self):
        # Asset Repair breadth (2026-07-21) — the forty-fifth supported doctype, off the
        # pre-verification addendum (docs/plans/dossiers/asset_repair.verify.md). party_field=
        # None — asset is a fixed-asset reference, never a GL party. submit_via="run_method" —
        # confirmed by a full 28-def class-body grep of asset_repair.py with zero submit()/
        # cancel() overrides. date_field="completion_date" — the GL-posting-date field (GL
        # entries stamp posting_date: self.completion_date at three sites), NOT the
        # unconditionally-reqd failure_date — the pin the addendum itself made and this landing
        # verified.
        self.assertEqual(SUPPORTED_DOCTYPES[ASSET_REPAIR],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "completion_date"})

    def test_asset_repair_party_field_is_none_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[ASSET_REPAIR]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_asset_repair_date_field_is_completion_date_not_failure_date(self):
        # failure_date is unconditionally reqd=1 (real on every draft) but GL entries never post
        # under it; completion_date is what get_gl_entries_for_repair_cost/_for_consumed_items
        # actually stamp as posting_date (asset_repair.py:347/364/403) — picking failure_date
        # would misalign period-lock checks with the date GL actually posts under.
        cfg = SUPPORTED_DOCTYPES[ASSET_REPAIR]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "completion_date")
        self.assertNotEqual(cfg["date_field"], "failure_date")

    def test_invoice_discounting_is_partyless_companied_reqd_and_rides_run_method(self):
        # Invoice Discounting breadth (2026-07-21) — the forty-sixth supported doctype, off the
        # pre-verification addendum (docs/plans/dossiers/invoice_discounting.verify.md — its own
        # Correction 1 wrongly called reference_name "the free Data field"; it is a real Dynamic
        # Link, caught by the supervisor before dispatch). party_field=None — a CHILD-TABLE party
        # shape (Discounted Invoice.customer per row), never a header column. submit_via=
        # "run_method" — confirmed by reading all 380 lines of invoice_discounting.py, zero
        # submit()/cancel() overrides. date_field="posting_date" — REJOINS the large existing set,
        # no new pattern (all three Date fields are genuinely fieldtype "Date", no Datetime).
        self.assertEqual(SUPPORTED_DOCTYPES[INVOICE_DISCOUNTING],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "posting_date"})

    def test_invoice_discounting_party_field_is_none_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[INVOICE_DISCOUNTING]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_invoice_discounting_date_field_is_posting_date(self):
        cfg = SUPPORTED_DOCTYPES[INVOICE_DISCOUNTING]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "posting_date")

    def test_timesheet_is_customer_partied_companied_optional_and_rides_run_method(self):
        # Timesheet breadth (2026-07-21) — the forty-second supported doctype, off the
        # pre-verification addendum (docs/plans/dossiers/timesheet.verify.md). party_field=
        # "customer" — a plain, singular, header-level Link, the simple shape for once.
        # submit_via="run_method" — the dossier's own "client_rpc" conclusion is WRONG per the
        # standing law (SUBMIT_VIA_CLIENT_RPC needs a genuine submit()/cancel() override;
        # confirmed by a 25-def class-body grep of timesheet.py with zero overrides).
        # date_field="start_date" — joins the existing start_date exclusivity set as its THIRD
        # member (Supplier Scorecard Period/Bank Guarantee).
        self.assertEqual(SUPPORTED_DOCTYPES[TIMESHEET],
                         {"party_field": "customer", "submit_via": "run_method",
                          "date_field": "start_date"})

    def test_timesheet_party_field_is_customer(self):
        cfg = SUPPORTED_DOCTYPES[TIMESHEET]
        self.assertEqual(cfg["party_field"], "customer")

    def test_timesheet_date_field_is_start_date_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[TIMESHEET]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "start_date")
        self.assertIsNotNone(cfg["date_field"])

    def test_contract_is_dynamic_link_partied_companyless_and_rides_run_method(self):
        # Contract breadth (2026-07-21) — the forty-third supported doctype, off the
        # pre-verification addendum (docs/plans/dossiers/contract.verify.md). party_field=None —
        # party_type (Select Customer/Supplier/Employee, reqd)/party_name (Dynamic Link, reqd) is
        # a Dynamic Link pair, the Quotation/Quality Inspection shape. submit_via="run_method" —
        # no submit()/cancel() override anywhere in the 9-method class body. date_field=
        # "signed_on" — the pin THIS landing made (the dossier left it open): a Datetime,
        # allow_on_submit=1, genuinely blank-capable field (never set by any code anywhere).
        # company CONFIRMED ABSENT — the SEVENTH companyless doctype.
        self.assertEqual(SUPPORTED_DOCTYPES[CONTRACT],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "signed_on"})

    def test_contract_party_field_is_none_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[CONTRACT]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_contract_date_field_is_signed_on_not_start_date_or_end_date(self):
        # start_date/end_date describe the AGREEMENT's own validity window (a different concept
        # from when it was actually signed); fulfilment_deadline feeds only the fulfilment_status
        # "Lapsed" branch. signed_on is the one field representing WHEN the transaction (the
        # signature event) happened — the pin, made and documented in erpnext.py's own module
        # docstring "Breadth (Contract)" section.
        cfg = SUPPORTED_DOCTYPES[CONTRACT]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "signed_on")
        self.assertIsNotNone(cfg["date_field"])

    def test_budget_is_partyless_companied_and_rides_run_method(self):
        # Budget breadth (2026-07-21) — the forty-first supported doctype, off the
        # pre-verification addendum (docs/plans/dossiers/budget.verify.md). party_field=None —
        # cost_center/project is a dual conditional pair gated on budget_against, never a single
        # static fieldname. submit_via="run_method" — confirmed by a full method-list grep of
        # budget.py's Budget class: zero submit/cancel/on_submit/on_cancel/before_submit/on_trash
        # anywhere. date_field="budget_start_date" — hidden:1, schema-optional, but
        # validate()-FORCED non-blank via the reqd from_fiscal_year Link on every persisted
        # document (see erpnext.py's module docstring "fifth wrinkle").
        self.assertEqual(SUPPORTED_DOCTYPES[BUDGET],
                         {"party_field": None, "submit_via": "run_method",
                          "date_field": "budget_start_date"})

    def test_budget_party_field_is_none_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[BUDGET]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_budget_date_field_is_budget_start_date_not_a_missing_key_or_blank_string(self):
        cfg = SUPPORTED_DOCTYPES[BUDGET]
        self.assertIn("date_field", cfg)
        self.assertEqual(cfg["date_field"], "budget_start_date")
        self.assertIsNotNone(cfg["date_field"])

    def test_only_journal_entry_and_stock_reconciliation_use_client_rpc(self):
        # Stock Reconciliation breadth (2026-07-21): the SECOND client_rpc doctype ever, proving
        # the transport this codebase built for Journal Entry alone genuinely generalizes — zero
        # new transport code was needed in ErpnextClient.submit_document/.cancel_document or
        # tools.py's _governed_write to land it (both were already doctype-generic, never gated on
        # doctype == JOURNAL_ENTRY specifically). Landed Cost Voucher, Request for Quotation,
        # Blanket Order, and Job Card (all landed after Stock Reconciliation) stay on run_method —
        # confirmed each overrides neither submit() nor cancel() — so this set does NOT grow a
        # third member; the loop below still proves it.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            expected = ("client_rpc" if doctype in (JOURNAL_ENTRY, STOCK_RECONCILIATION)
                       else "run_method")
            self.assertEqual(cfg["submit_via"], expected, doctype)

    def test_only_so_po_mr_sq_q_rfq_maintenance_schedule_asset_movement_sco_and_scio_use_transaction_date(
            self):
        # Subcontracting Order (the forty-ninth doctype) REJOINS this set as its NINTH member — a
        # real, required transaction_date field (default "Today", fetch_from purchase_order.
        # transaction_date), zero new plumbing needed — the same shape Maintenance Schedule's own
        # rejoin already established. Subcontracting Inward Order (the fiftieth doctype) REJOINS
        # this set as its TENTH member — a real, required transaction_date field (default
        # "Today", fetch_from sales_order.transaction_date), the same shape again.
        # Delivery Note, Purchase Receipt, and Stock Entry (breadth 2026-07-21, Wave 1)
        # deliberately do NOT join this set — all three carry a real posting_date field, unlike
        # the six transaction_date-only doctypes below (Request for Quotation, Wave 3's first row,
        # joins Sales/Purchase Order/Material Request/Supplier Quotation/Quotation on this
        # pattern). Blanket Order (Wave 3's second row) does NOT join this set either — it carries
        # NEITHER posting_date NOR transaction_date, its own "from_date" pattern instead (see
        # test_blanket_order_date_field_is_the_only_from_date_user below). Job Card (Wave 3's
        # third row) keeps the plain "posting_date" default — a real field, confirmed present.
        # BOM (Wave 3's fourth row) is on NO date pattern at all — date_field=None, the
        # declared-dateless pin (see test_bom_and_packing_slip_are_the_only_dateless_doctypes
        # below). Work Order (Wave 3's fifth row) is on its own FOURTH pattern —
        # planned_start_date, the first Datetime-typed date_field (see
        # test_work_order_is_the_only_planned_start_date_user). Packing Slip (Wave 4's first
        # row) joins BOM on the declared-dateless pin — the SECOND doctype, not a new pattern.
        # Cost Center Allocation (Wave 4's second row) is on its own SIXTH pattern —
        # valid_from, a real Date field the dossier wrongly called "dateless" (see
        # test_cost_center_allocation_is_the_only_valid_from_user below). Supplier Scorecard
        # Period (Wave 4's third row) is on its own SEVENTH pattern — start_date, chosen over its
        # own sibling end_date as the period's anchor (REJOINED by Bank Guarantee, Wave 5's second
        # row — see
        # test_supplier_scorecard_period_and_bank_guarantee_are_the_only_start_date_users below).
        # Quality Inspection
        # (Wave 4's fourth row) is on its own EIGHTH pattern — report_date. Installation Note
        # (Wave 4's fifth row) is on its own NINTH pattern — inst_date (see
        # test_installation_note_is_the_only_inst_date_user below). Shipment (Wave 4's sixth row)
        # is on its own TENTH pattern — pickup_date (see
        # test_shipment_is_the_only_pickup_date_user below). Project Update (Wave 4's eighth row)
        # is on its own ELEVENTH pattern — bare "date" (see
        # test_project_update_and_asset_value_adjustment_are_the_only_date_users above). REJOINED
        # by Asset Value Adjustment (Wave 5's fifth row) as its own SECOND member — a real,
        # required Date field, a purely nominal collision (see
        # test_project_update_and_asset_value_adjustment_are_the_only_date_users above; Delivery
        # Trip, Wave 5's fourth row, is on its own brand-new FOURTEENTH pattern instead —
        # departure_time, no collision — see
        # test_delivery_trip_is_the_only_departure_time_user below). Maintenance Visit (Wave 4's
        # ninth row) is on its own TWELFTH pattern — mntc_date (see
        # test_maintenance_visit_is_the_only_mntc_date_user below). Maintenance Schedule (Wave 4's
        # tenth and last row) REJOINS this set as its SEVENTH member — a real, required
        # transaction_date field with no schema default, zero new plumbing needed. Asset
        # Maintenance Log (Wave 5's first row) is on its own THIRTEENTH pattern — completion_date
        # (REJOINED by Asset Repair, the forty-fifth doctype, as a nominal-only collision — see
        # test_asset_maintenance_log_and_asset_repair_are_the_only_completion_date_users below).
        # Bank Guarantee
        # (Wave 5's second row) REJOINS Supplier Scorecard Period's own SEVENTH date pattern —
        # start_date, a second doctype on it, not a new one (see
        # test_supplier_scorecard_period_and_bank_guarantee_are_the_only_start_date_users below).
        # Asset Movement (Wave 5's third row) REJOINS this set as its EIGHTH member BY FIELDNAME
        # ONLY — a real, required Datetime field (default "Now"), the SECOND Datetime-typed
        # date_field this campaign has found (after Work Order's own planned_start_date). This
        # loop pins the STRING match alone (the exact same mechanism every prior member of this
        # set is pinned by); the type distinction — and the _posting_date_of datetime->date
        # projection it requires, reused unchanged from Work Order's own landing — is proven
        # behaviorally in test_tools.py's own TestAssetMovementDatetimeDateProjection, using
        # realistic Datetime-shaped fixture values. A fieldname collision between a Date-typed
        # group and a Datetime-typed member is safe by construction (the projection keys on the
        # VALUE's own shape at read time, never on which fieldname string produced it) — but each
        # doctype's own type must still be pinned from its own source, never inherited by
        # name-match alone. Share Transfer (a full-attention landing, not a numbered wave row) is
        # on bare "date" (see
        # test_project_update_asset_value_adjustment_and_share_transfer_are_the_only_date_users
        # above) — its THIRD member, not a new pattern. Budget (the forty-first doctype) is on
        # its own FIFTEENTH pattern — budget_start_date (see
        # test_budget_is_the_only_budget_start_date_user below). Contract (the forty-third
        # doctype) is on its own SIXTEENTH pattern — signed_on, a Datetime field like Work
        # Order's/Asset Movement's own, but the first ALSO genuinely blank-capable at submit (see
        # test_contract_is_the_only_signed_on_user below). Asset Repair (the forty-fifth doctype)
        # REJOINS Asset Maintenance Log's own THIRTEENTH pattern — completion_date, a nominal
        # fieldname collision only (AML's copy is a plain Date; this one is Datetime and
        # conditionally mandatory — see
        # test_asset_maintenance_log_and_asset_repair_are_the_only_completion_date_users below).
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            expected = ("transaction_date"
                       if doctype in (SALES_ORDER, PURCHASE_ORDER, MATERIAL_REQUEST,
                                      SUPPLIER_QUOTATION, QUOTATION, REQUEST_FOR_QUOTATION,
                                      MAINTENANCE_SCHEDULE, ASSET_MOVEMENT,
                                      SUBCONTRACTING_ORDER, SUBCONTRACTING_INWARD_ORDER)
                       else "posting_date")
            if doctype in (BLANKET_ORDER, BOM, WORK_ORDER, ASSET, PACKING_SLIP,
                           COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
                           QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT, PROJECT_UPDATE,
                           MAINTENANCE_VISIT, ASSET_MAINTENANCE_LOG, BANK_GUARANTEE,
                           DELIVERY_TRIP, ASSET_VALUE_ADJUSTMENT, SHARE_TRANSFER, BOM_CREATOR,
                           BUDGET, TIMESHEET, CONTRACT, PICK_LIST, ASSET_REPAIR):
                continue  # from_date / dateless / planned_start_date / available_for_use_date /
                          # valid_from / start_date / report_date / inst_date / pickup_date / bare
                          # "date" / mntc_date / completion_date / start_date (again) /
                          # departure_time / bare "date" (again, Asset Value Adjustment and Share
                          # Transfer) / dateless (again, BOM Creator) / budget_start_date (Budget)
                          # / dateless (again, Pick List)
                          # / start_date (again, Timesheet) / signed_on (Contract) /
                          # completion_date (again, Asset Repair) — each pinned by its own
                          # exclusivity test
            self.assertEqual(cfg["date_field"], expected, doctype)

    def test_contract_is_the_only_signed_on_user(self):
        # The exclusivity pin for the SIXTEENTH date pattern: "signed_on" is Contract's own real,
        # schema-optional Datetime field (allow_on_submit=1, no reqd, no default). A future
        # landing reaching for "signed_on" must prove its own doctype's shape from source, not
        # inherit this one's by copy-paste.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype == CONTRACT:
                self.assertEqual(cfg["date_field"], "signed_on", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "signed_on", doctype)

    def test_delivery_trip_is_the_only_departure_time_user(self):
        # The exclusivity pin for the FOURTEENTH date pattern: "departure_time" is Delivery
        # Trip's own real, required Datetime field with NO schema default — a genuinely NEW
        # fieldname this campaign has never used before (unlike Asset Movement's own nominal
        # collision with the transaction_date set). A future landing reaching for
        # "departure_time" must prove its own doctype's shape from source, not inherit this
        # one's by copy-paste.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype == DELIVERY_TRIP:
                self.assertEqual(cfg["date_field"], "departure_time", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "departure_time", doctype)

    def test_work_order_is_the_only_planned_start_date_user(self):
        # The Blanket-Order-style exclusivity pin for the FOURTH date pattern: a Datetime-typed
        # date_field is exceptional (it forces the _posting_date_of projection) — a future
        # landing reaching for planned_start_date or any other Datetime field must prove its own
        # doctype's shape, not inherit Work Order's by copy-paste.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype == WORK_ORDER:
                self.assertEqual(cfg["date_field"], "planned_start_date", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "planned_start_date", doctype)

    def test_asset_is_the_only_available_for_use_date_user(self):
        # The exclusivity pin for the FIFTH date pattern: available_for_use_date is Asset's GL
        # posting date, verified from make_gl_entries' own body — a future landing reaching for
        # it must prove its own doctype's posting-date semantics, not inherit Asset's.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype == ASSET:
                self.assertEqual(cfg["date_field"], "available_for_use_date", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "available_for_use_date", doctype)

    def test_bom_packing_slip_bom_creator_and_pick_list_are_the_only_dateless_doctypes(self):
        # Widened from the three-member "BOM/Packing Slip/BOM Creator" pin to a FOUR-member set
        # now that Pick List (2026-07-21, off the pre-verification addendum) independently proves
        # its own datelessness from source (a Date/Datetime-typed scan of ALL 35 parent
        # (pick_list.json) AND ALL 37 child (pick_list_item.json) fields returns empty — a
        # parent-only scan would have been a real gap for a "full enumeration" charge; confirmed
        # absent from period_closing_doctypes too) — NOT inherited from BOM/Packing Slip/BOM
        # Creator by copy-paste, a wholly separate doctype (and the first with a CHILD TABLE also
        # independently scanned) with its own justification (see its own config tests above and
        # erpnext.py's module docstring). The addendum's own "third" framing was already stale by
        # landing time (BOM Creator took third earlier the same morning) — Pick List is the live
        # FOURTH. The load-bearing shape is unchanged: date_field=None remains an exceptional,
        # per-doctype, source-verified pin — a FIFTH future landing reaching for it must prove its
        # own doctype's datelessness too, rather than inherit membership by the set growing.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype in (BOM, PACKING_SLIP, BOM_CREATOR, PICK_LIST):
                self.assertIsNone(cfg["date_field"], doctype)
            else:
                self.assertIsNotNone(cfg["date_field"], doctype)

    def test_blanket_order_date_field_is_the_only_from_date_user(self):
        # Blanket Order is the FIRST and ONLY supported doctype on neither the posting_date nor the
        # transaction_date pattern — confirmed absent from blanket_order.json's 18 enumerated
        # fields (no posting_date, no transaction_date; only from_date/to_date). This is the
        # load-bearing assertion that the date-field mechanism genuinely swaps to a THIRD literal
        # value, not merely that a "date_field" key exists somewhere in config.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype == BLANKET_ORDER:
                self.assertEqual(cfg["date_field"], "from_date", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "from_date", doctype)

    def test_cost_center_allocation_is_the_only_valid_from_user(self):
        # The exclusivity pin for the SIXTH date pattern: valid_from is Cost Center Allocation's
        # own real, required Date field (the dossier correction the module docstring documents
        # in full — the dossier wrongly called this doctype "dateless"). A future landing
        # reaching for valid_from (or inventing a NEW dateless claim for a doctype that actually
        # carries a real Date field) must prove its own doctype's shape from source, not inherit
        # this one's by copy-paste.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype == COST_CENTER_ALLOCATION:
                self.assertEqual(cfg["date_field"], "valid_from", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "valid_from", doctype)

    def test_supplier_scorecard_period_bank_guarantee_and_timesheet_are_the_only_start_date_users(
            self):
        # Widened from the two-member "Supplier Scorecard Period and Bank Guarantee" pin to a
        # THREE-member set now that Timesheet (2026-07-21, off the pre-verification addendum)
        # independently proves its own "start_date" fieldname from source (a direct re-read of
        # timesheet.json confirms a real Date field of the exact same literal name, read_only:1,
        # no reqd, recomputed by set_dates() on every validate() — not inherited from either prior
        # member by copy-paste, a wholly separate doctype with its own 37-field enumeration). All
        # three chose start_date over their own sibling end_date as the anchor (the same "window
        # start, not its close" convention Blanket Order's from_date established) — Timesheet's
        # own end_date is likewise read_only and recomputed alongside start_date, never the
        # date_field. The load-bearing shape is unchanged: a FOURTH future landing reaching for
        # start_date must prove its own doctype's shape too, rather than inherit membership by the
        # set growing.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype in (SUPPLIER_SCORECARD_PERIOD, BANK_GUARANTEE, TIMESHEET):
                self.assertEqual(cfg["date_field"], "start_date", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "start_date", doctype)

    def test_quality_inspection_is_the_only_report_date_user(self):
        # The exclusivity pin for the EIGHTH date pattern: report_date is Quality Inspection's own
        # real, required Date field (default "Today") — a literal fieldname no prior branch has
        # used even though it echoes "posting" semantics. A future landing reaching for
        # report_date must prove its own doctype's shape from source, not inherit this one's by
        # copy-paste.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype == QUALITY_INSPECTION:
                self.assertEqual(cfg["date_field"], "report_date", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "report_date", doctype)

    def test_installation_note_is_the_only_inst_date_user(self):
        # The exclusivity pin for the NINTH date pattern: inst_date is Installation Note's own
        # real, required Date field (no default) — the schema's only OTHER Date/Datetime field,
        # inst_time, is a Time (clock-time-only, never a calendar date) and is confirmed read
        # nowhere for any period-lock purpose. A future landing reaching for inst_date must prove
        # its own doctype's shape from source, not inherit this one's by copy-paste.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype == INSTALLATION_NOTE:
                self.assertEqual(cfg["date_field"], "inst_date", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "inst_date", doctype)

    def test_shipment_is_the_only_pickup_date_user(self):
        # The exclusivity pin for the TENTH date pattern: pickup_date is Shipment's own real,
        # required Date field (allow_on_submit=1) — the schema's only OTHER Date/Datetime-shaped
        # fields are pickup_from/pickup_to, both Time (clock-time-only, never a calendar date). A
        # future landing reaching for pickup_date must prove its own doctype's shape from source,
        # not inherit this one's by copy-paste.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype == SHIPMENT:
                self.assertEqual(cfg["date_field"], "pickup_date", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "pickup_date", doctype)

    def test_project_update_asset_value_adjustment_and_share_transfer_are_the_only_date_users(
            self):
        # Widened from the single-member "Project Update is the only date user" pin (that
        # doctype's own landing), through a TWO-member set (Asset Value Adjustment), to a
        # THREE-member set now that Share Transfer (a full-attention landing, not a numbered wave
        # row) independently proves its own "date" fieldname from source (a direct re-read of
        # share_transfer.json confirms a real, reqd=1, no-default Date field of the exact same
        # literal name) — NOT inherited from either prior member by copy-paste, a wholly separate
        # doctype with its own 26-field/17-data-field enumeration and its own justification.
        # Share Transfer's own "date" is reqd=1 like Asset Value Adjustment's copy, unlike Project
        # Update's reqd=0 copy — a purely nominal fieldname collision, the same shape Asset
        # Movement's own transaction_date rode against the Date-typed set. The load-bearing shape
        # is unchanged: a FOURTH future landing reaching for "date" must prove its own doctype's
        # shape too, rather than inherit membership by the set growing.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype in (PROJECT_UPDATE, ASSET_VALUE_ADJUSTMENT, SHARE_TRANSFER):
                self.assertEqual(cfg["date_field"], "date", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "date", doctype)

    def test_maintenance_visit_is_the_only_mntc_date_user(self):
        # The exclusivity pin for the TWELFTH date pattern: "mntc_date" is Maintenance Visit's
        # own real, required Date field with a schema default of "Today" — the first pattern in
        # this campaign to carry BOTH reqd AND a default together (Installation Note's inst_date
        # is reqd with no default; Project Update's date is neither). The schema's only other
        # Date/Datetime-shaped field, "mntc_time", is a Time, clock-time-only, read in exactly one
        # place (check_if_last_visit's own same-day tie-break) and is likewise excluded. A future
        # landing reaching for "mntc_date" must prove its own doctype's shape from source, not
        # inherit this one's by copy-paste.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype == MAINTENANCE_VISIT:
                self.assertEqual(cfg["date_field"], "mntc_date", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "mntc_date", doctype)

    def test_budget_is_the_only_budget_start_date_user(self):
        # The exclusivity pin for the FIFTEENTH date pattern, and the FIRST HIDDEN one:
        # "budget_start_date" is Budget's own real Date field — hidden:1, schema-optional (no
        # reqd key), but validate()-FORCED non-blank via the reqd from_fiscal_year Link on every
        # persisted document (set_fiscal_year_dates, budget.py:99-110). The schema's only other
        # Date field, "budget_end_date", is likewise hidden and is the window's CLOSE, never the
        # date_field (the same "window start, not its close" convention Blanket Order's
        # from_date/Supplier Scorecard Period's start_date already established). A future landing
        # reaching for "budget_start_date" must prove its own doctype's shape from source, not
        # inherit this one's by copy-paste.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype == BUDGET:
                self.assertEqual(cfg["date_field"], "budget_start_date", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "budget_start_date", doctype)

    def test_asset_maintenance_log_and_asset_repair_are_the_only_completion_date_users(self):
        # The exclusivity pin for the THIRTEENTH date pattern, widened from a single-member pin
        # (Asset Maintenance Log's own landing) to a TWO-member set now that Asset Repair
        # (2026-07-21, off its own pre-verification addendum) independently proves its own
        # "completion_date" fieldname from source — NOT inherited by copy-paste, a wholly
        # separate doctype with its own 37-field enumeration and its own justification.
        # Asset Maintenance Log's own "completion_date" is a writable, operational Date field
        # (reqd absent, no schema default) — the schema's only OTHER Date field, "due_date", is a
        # read-only reference fetched from the linked task and deliberately excluded. Asset
        # Repair's own copy is a DIFFERENT shape entirely: Datetime (not Date), with
        # "mandatory_depends_on": "eval:doc.repair_status==\"Completed\"" (asset_repair.json) — a
        # purely NOMINAL fieldname collision, the same shape Asset Movement's own transaction_date
        # rode against the Date-typed set (the type distinction — and the _posting_date_of
        # datetime->date projection it requires — is proven behaviorally in test_tools.py's own
        # TestAssetRepairDatetimeDateProjection). The load-bearing shape is unchanged: a THIRD
        # future landing reaching for "completion_date" must prove its own doctype's shape too,
        # rather than inherit membership by the set growing.
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            if doctype in (ASSET_MAINTENANCE_LOG, ASSET_REPAIR):
                self.assertEqual(cfg["date_field"], "completion_date", doctype)
            else:
                self.assertNotEqual(cfg["date_field"], "completion_date", doctype)

    def test_only_quotation_has_dynamic_party_and_needs_two_context_columns(self):
        # The decision itself, asserted directly: party_field=None is a deliberate value for
        # Quotation too (Material Request's shape, not an accident of a missing dict key or an
        # empty-string placeholder that could silently splice a literal "" into the field list).
        cfg = SUPPORTED_DOCTYPES[QUOTATION]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_request_for_quotation_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline as Quotation's own test above, for RFQ's genuinely different reason
        # (a multiple-supplier child table, not a Dynamic Link pair) — party_field=None is a
        # deliberate value, never an accident of a missing key or a blank-string placeholder that
        # could silently splice a literal "" into the requested field list.
        cfg = SUPPORTED_DOCTYPES[REQUEST_FOR_QUOTATION]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])

    def test_blanket_order_party_field_is_none_not_a_missing_key_or_blank_string(self):
        # Same discipline again, for Blanket Order's own genuinely different reason (TWO real,
        # scalar header Link fields — customer AND supplier — that are client-side gated but
        # neither server-enforced nor server-cleared, unlike Material Request's explicit clearing
        # or Quotation's Dynamic Link pair or RFQ's child table) — party_field=None is a deliberate
        # value here too, never an accident.
        cfg = SUPPORTED_DOCTYPES[BLANKET_ORDER]
        self.assertIn("party_field", cfg)
        self.assertIsNone(cfg["party_field"])


class TestAuthAndShape(unittest.TestCase):
    def test_token_auth_header_on_every_call(self):
        c, t = client([(200, {"data": {"name": "SI-1"}})])
        c.get_document(SALES_INVOICE, "SI-1")
        self.assertEqual(t.calls[0]["headers"]["Authorization"], "token KEY:SECRET")

    def test_secret_never_in_url_or_params(self):
        c, t = client([(200, {"data": {"name": "SI-1"}})])
        c.get_document(SALES_INVOICE, "SI-1")
        call = t.calls[0]
        self.assertNotIn("SECRET", call["url"])
        self.assertNotIn("SECRET", json.dumps(call["params"] or {}))


class TestGet(unittest.TestCase):
    def test_get_url_and_data(self):
        c, t = client([(200, {"data": {"name": "SI-1", "modified": "2026-07-01 10:00:00.000001"}})])
        doc = c.get_document(SALES_INVOICE, "SI-1")
        self.assertEqual(t.calls[0]["method"], "GET")
        self.assertEqual(t.calls[0]["url"],
                         "https://erp.example.com/api/resource/Sales%20Invoice/SI-1")
        self.assertEqual(doc["modified"], "2026-07-01 10:00:00.000001")

    def test_slash_bearing_doc_name_is_fully_quoted(self):
        c, t = client([(200, {"data": {"name": "ACC/2026/00001"}})])
        c.get_document(SALES_INVOICE, "ACC/2026/00001")
        self.assertIn("/api/resource/Sales%20Invoice/ACC%2F2026%2F00001", t.calls[0]["url"])

    def test_empty_name_refused_without_a_request(self):
        c, t = client()
        with self.assertRaises(ErpnextError):
            c.get_document(SALES_INVOICE, "")
        self.assertEqual(t.calls, [])

    def test_purchase_invoice_url(self):
        c, t = client([(200, {"data": {"name": "PINV-1"}})])
        c.get_document(PURCHASE_INVOICE, "PINV-1")
        self.assertEqual(t.calls[0]["url"],
                         "https://erp.example.com/api/resource/Purchase%20Invoice/PINV-1")

    def test_journal_entry_url(self):
        c, t = client([(200, {"data": {"name": "JE-1"}})])
        c.get_document(JOURNAL_ENTRY, "JE-1")
        self.assertEqual(t.calls[0]["url"],
                         "https://erp.example.com/api/resource/Journal%20Entry/JE-1")


class TestList(unittest.TestCase):
    def test_list_params(self):
        c, t = client([(200, {"data": [{"name": "SI-1"}]})])
        rows = c.list_documents(SALES_INVOICE, filters=[["status", "=", "Draft"]], limit=5)
        p = t.calls[0]["params"]
        self.assertEqual(json.loads(p["filters"]), [["status", "=", "Draft"]])
        self.assertEqual(p["limit_page_length"], "5")
        self.assertEqual(rows, [{"name": "SI-1"}])

    def test_list_default_fields_include_status_and_dates(self):
        c, t = client([(200, {"data": []})])
        c.list_documents(SALES_INVOICE)
        fields = json.loads(t.calls[0]["params"]["fields"])
        for f in ("name", "status", "posting_date", "grand_total", "docstatus"):
            self.assertIn(f, fields)

    def test_default_party_field_is_customer(self):
        c, t = client([(200, {"data": []})])
        c.list_documents(SALES_INVOICE)
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("customer", fields)

    def test_purchase_invoice_uses_the_supplier_party_field(self):
        c, t = client([(200, {"data": []})])
        c.list_documents(PURCHASE_INVOICE,
                         party_field=SUPPORTED_DOCTYPES[PURCHASE_INVOICE]["party_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("supplier", fields)
        self.assertNotIn("customer", fields)
        self.assertIn("/api/resource/Purchase%20Invoice", t.calls[0]["url"])

    def test_journal_entry_list_has_no_party_status_or_grand_total(self):
        # Journal Entry's own branch (erpnext.py's _list_fields): confirmed absent from
        # journal_entry.json — no header-level party, no `status`, no `grand_total`.
        c, t = client([(200, {"data": []})])
        c.list_documents(JOURNAL_ENTRY,
                         party_field=SUPPORTED_DOCTYPES[JOURNAL_ENTRY]["party_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("total_debit", fields)
        self.assertIn("total_credit", fields)
        self.assertIn("voucher_type", fields)
        self.assertIn("/api/resource/Journal%20Entry", t.calls[0]["url"])

    def test_journal_entry_list_url(self):
        c, t = client([(200, {"data": []})])
        c.list_documents(JOURNAL_ENTRY, party_field=None)
        self.assertEqual(t.calls[0]["url"], "https://erp.example.com/api/resource/Journal%20Entry")

    def test_sales_order_list_uses_transaction_date_not_posting_date(self):
        # Sales Order breadth: confirmed absent from sales_order.json — no `posting_date` field at
        # all. Asking the bench's list endpoint for a column the doctype's schema doesn't carry is
        # the same unknown-column failure class get_period_locks' own docstring documents for a
        # stale `filters` column — this is the load-bearing assertion that the fix actually swaps
        # the requested column, not merely that a "date_field" key exists somewhere in config.
        c, t = client([(200, {"data": []})])
        c.list_documents(SALES_ORDER,
                         party_field=SUPPORTED_DOCTYPES[SALES_ORDER]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[SALES_ORDER]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("transaction_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertIn("customer", fields)
        self.assertIn("status", fields)
        self.assertIn("grand_total", fields)
        self.assertIn("/api/resource/Sales%20Order", t.calls[0]["url"])

    def test_sales_order_list_default_date_field_is_posting_date(self):
        # The client's own default (unchanged) — a caller that omits date_field entirely still
        # gets the pre-existing behavior; it is tools.py's job to always supply the resolved
        # per-doctype value, never this client's.
        c, t = client([(200, {"data": []})])
        c.list_documents(SALES_ORDER, party_field="customer")
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("posting_date", fields)

    def test_purchase_order_list_uses_transaction_date_not_posting_date(self):
        # Purchase Order breadth: mechanically identical to Sales Order's date_field handling
        # (confirmed absent from purchase_order.json — no `posting_date` field at all) — the
        # client-level default-fallback behavior is already pinned above
        # (test_sales_order_list_default_date_field_is_posting_date; that assertion is doctype-
        # agnostic, so it is not re-derived here). This is the one load-bearing, doctype-specific
        # assertion: the requested column set actually swaps for Purchase Order too, on its own
        # party field ("supplier", not "customer") and its own resource path.
        c, t = client([(200, {"data": []})])
        c.list_documents(PURCHASE_ORDER,
                         party_field=SUPPORTED_DOCTYPES[PURCHASE_ORDER]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[PURCHASE_ORDER]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("transaction_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertIn("supplier", fields)
        self.assertIn("status", fields)
        self.assertIn("grand_total", fields)
        self.assertIn("/api/resource/Purchase%20Order", t.calls[0]["url"])

    def test_material_request_list_has_no_party_or_grand_total_and_uses_transaction_date(self):
        # Material Request breadth: the THIRD _list_fields branch (erpnext.py) — status IS
        # present (unlike Journal Entry) but grand_total is confirmed ABSENT (unlike Sales/
        # Purchase Order), and party_field=None means no party column is spliced at all (unlike
        # every other non-JE doctype). material_request_type/per_ordered/per_received stand in
        # for the party/grand_total slots — this is the load-bearing assertion that the new
        # branch actually fires, not merely that a party_field=None key exists in config.
        c, t = client([(200, {"data": []})])
        c.list_documents(MATERIAL_REQUEST,
                         party_field=SUPPORTED_DOCTYPES[MATERIAL_REQUEST]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[MATERIAL_REQUEST]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertIn("docstatus", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("material_request_type", fields)
        self.assertIn("per_ordered", fields)
        self.assertIn("per_received", fields)
        self.assertIn("transaction_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertIn("/api/resource/Material%20Request", t.calls[0]["url"])

    def test_delivery_note_list_uses_customer_and_keeps_posting_date(self):
        # Delivery Note breadth: rides the GENERIC _list_fields branch (status + grand_total both
        # present, party_field="customer") — no new branch, unlike Journal Entry/Material Request.
        # The load-bearing assertion is that it asks for "posting_date" (its OWN real field,
        # confirmed present in delivery_note.json), never "transaction_date" (the SO/PO/MR shape) —
        # this is the doctype that proves the date_field mechanism's DEFAULT path still fires
        # correctly for a doctype explicitly configured with "posting_date" rather than omitting
        # the key.
        c, t = client([(200, {"data": []})])
        c.list_documents(DELIVERY_NOTE,
                         party_field=SUPPORTED_DOCTYPES[DELIVERY_NOTE]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[DELIVERY_NOTE]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("customer", fields)
        self.assertIn("status", fields)
        self.assertIn("grand_total", fields)
        self.assertIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Delivery%20Note", t.calls[0]["url"])

    def test_purchase_receipt_list_uses_supplier_and_keeps_posting_date(self):
        # Purchase Receipt breadth: rides the GENERIC _list_fields branch (status + grand_total
        # both present, party_field="supplier") — no new branch, the same shape Delivery Note's
        # landing already rode. Load-bearing: "posting_date" (its OWN real field, confirmed present
        # in purchase_receipt.json), never "transaction_date" (the SO/PO/MR shape).
        c, t = client([(200, {"data": []})])
        c.list_documents(PURCHASE_RECEIPT,
                         party_field=SUPPORTED_DOCTYPES[PURCHASE_RECEIPT]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[PURCHASE_RECEIPT]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("supplier", fields)
        self.assertIn("status", fields)
        self.assertIn("grand_total", fields)
        self.assertIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Purchase%20Receipt", t.calls[0]["url"])

    def test_stock_entry_list_has_no_party_status_or_grand_total_and_uses_posting_date(self):
        # Stock Entry breadth: the FOURTH _list_fields branch (erpnext.py) — status AND
        # grand_total are BOTH confirmed ABSENT (unlike Material Request, which keeps status), AND
        # party_field=None means no party column is spliced (the same JE/MR shape) — a combination
        # no prior doctype exercised. purpose/total_incoming_value/total_outgoing_value/
        # value_difference stand in. date_field stays "posting_date" (a real field, unlike SO/PO/
        # MR's transaction_date) — this is the load-bearing assertion that the new branch actually
        # fires, not merely that a party_field=None key exists in config.
        c, t = client([(200, {"data": []})])
        c.list_documents(STOCK_ENTRY,
                         party_field=SUPPORTED_DOCTYPES[STOCK_ENTRY]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[STOCK_ENTRY]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("purpose", fields)
        self.assertIn("total_incoming_value", fields)
        self.assertIn("total_outgoing_value", fields)
        self.assertIn("value_difference", fields)
        self.assertIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Stock%20Entry", t.calls[0]["url"])

    def test_supplier_quotation_list_uses_transaction_date_not_posting_date(self):
        # Supplier Quotation breadth (Wave 2, first row): rides the GENERIC _list_fields branch
        # (status + grand_total both present, party_field="supplier") — no new branch, same shape
        # Purchase Order/Delivery Note/Purchase Receipt already ride. Load-bearing: the requested
        # column set swaps to "transaction_date" (confirmed absent posting_date from
        # supplier_quotation.json — the same finding shape Sales/Purchase Order/Material Request
        # already proved), on its own party field ("supplier") and its own resource path.
        c, t = client([(200, {"data": []})])
        c.list_documents(SUPPLIER_QUOTATION,
                         party_field=SUPPORTED_DOCTYPES[SUPPLIER_QUOTATION]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[SUPPLIER_QUOTATION]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("transaction_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertIn("supplier", fields)
        self.assertIn("status", fields)
        self.assertIn("grand_total", fields)
        self.assertIn("/api/resource/Supplier%20Quotation", t.calls[0]["url"])

    def test_quotation_list_has_no_single_party_field_but_carries_the_dynamic_pair(self):
        # Quotation breadth (Wave 2, second row): the FIFTH _list_fields branch (erpnext.py) —
        # status AND grand_total are BOTH confirmed present (unlike Journal Entry/Material
        # Request/Stock Entry), but party_field=None means no SINGLE party column is spliced —
        # Quotation's own party is a Dynamic Link PAIR (quotation_to + party_name), not a static
        # fieldname the generic branch's party_field slot could name. This is the load-bearing
        # assertion that the new branch actually fires and discloses BOTH context columns
        # together, never party_name alone (which would disclose a bare record name with no
        # doctype to interpret it by).
        c, t = client([(200, {"data": []})])
        c.list_documents(QUOTATION,
                         party_field=SUPPORTED_DOCTYPES[QUOTATION]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[QUOTATION]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertIn("grand_total", fields)
        self.assertIn("quotation_to", fields)
        self.assertIn("party_name", fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("transaction_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertIn("/api/resource/Quotation", t.calls[0]["url"])

    def test_pos_invoice_list_uses_customer_and_keeps_posting_date(self):
        # POS Invoice breadth: rides the GENERIC _list_fields branch (status + grand_total both
        # present, party_field="customer") — no new branch, byte-identical to Sales Invoice's own
        # shape. Load-bearing: "posting_date" (its OWN real field, confirmed present in
        # pos_invoice.json), never "transaction_date".
        c, t = client([(200, {"data": []})])
        c.list_documents(POS_INVOICE,
                         party_field=SUPPORTED_DOCTYPES[POS_INVOICE]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[POS_INVOICE]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("customer", fields)
        self.assertIn("status", fields)
        self.assertIn("grand_total", fields)
        self.assertIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/POS%20Invoice", t.calls[0]["url"])

    def test_dunning_list_uses_customer_and_keeps_posting_date(self):
        # Dunning breadth: rides the GENERIC _list_fields branch (status + grand_total both
        # present, party_field="customer") — no new branch, byte-identical to Sales Invoice's own
        # shape. Load-bearing: "posting_date" (its OWN real field, confirmed present in
        # dunning.json), never "transaction_date".
        c, t = client([(200, {"data": []})])
        c.list_documents(DUNNING,
                         party_field=SUPPORTED_DOCTYPES[DUNNING]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[DUNNING]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("customer", fields)
        self.assertIn("status", fields)
        self.assertIn("grand_total", fields)
        self.assertIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Dunning", t.calls[0]["url"])

    def test_stock_reconciliation_list_has_no_party_status_or_grand_total_and_uses_posting_date(
            self):
        # Stock Reconciliation breadth: a SIXTH _list_fields branch (erpnext.py) — status AND
        # grand_total are BOTH confirmed ABSENT (the Stock Entry shape), AND party_field=None means
        # no party column is spliced (the JE/MR/SE/Q shape) — but the SUBSTITUTE fields differ from
        # Stock Entry's own (purpose + difference_amount, not purpose + three value fields), so
        # this is its own branch, not a reuse. date_field stays "posting_date" (a real field,
        # confirmed present), never "transaction_date". List transport itself (GET
        # /api/resource/<doctype>) is unaffected by submit_via — this doctype's own client_rpc
        # transport only touches submit/cancel, proven separately below.
        c, t = client([(200, {"data": []})])
        c.list_documents(STOCK_RECONCILIATION,
                         party_field=SUPPORTED_DOCTYPES[STOCK_RECONCILIATION]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[STOCK_RECONCILIATION]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("purpose", fields)
        self.assertIn("difference_amount", fields)
        self.assertIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Stock%20Reconciliation", t.calls[0]["url"])

    def test_landed_cost_voucher_list_has_no_party_status_or_grand_total_and_uses_posting_date(
            self):
        # Landed Cost Voucher breadth: a SEVENTH _list_fields branch (erpnext.py) — the SAME
        # absence shape as Stock Reconciliation (party_field=None, status AND grand_total both
        # confirmed ABSENT) but, like Stock Reconciliation-vs-Stock-Entry before it, NOT a reuse:
        # the substitute fields differ (distribute_charges_based_on + total_taxes_and_charges, not
        # purpose + difference_amount). date_field stays "posting_date" (a real field, confirmed
        # present), never "transaction_date".
        c, t = client([(200, {"data": []})])
        c.list_documents(LANDED_COST_VOUCHER,
                         party_field=SUPPORTED_DOCTYPES[LANDED_COST_VOUCHER]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[LANDED_COST_VOUCHER]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("distribute_charges_based_on", fields)
        self.assertIn("total_taxes_and_charges", fields)
        self.assertIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Landed%20Cost%20Voucher", t.calls[0]["url"])

    def test_request_for_quotation_list_has_no_party_or_grand_total_and_uses_transaction_date(
            self):
        # Request for Quotation breadth: an EIGHTH _list_fields branch (erpnext.py) — the SAME
        # absence shape as Material Request (party_field=None, status present, grand_total
        # absent), but NOT a reuse: Material Request's own substitute fields
        # (material_request_type/per_ordered/per_received) don't exist on this doctype's schema,
        # and RFQ has no natural analog to any of them — so this branch is the first genuinely
        # bare one (no substitute/context column at all). date_field="transaction_date" (confirmed
        # no posting_date field anywhere in the 384 enumerated fields).
        c, t = client([(200, {"data": []})])
        c.list_documents(REQUEST_FOR_QUOTATION,
                         party_field=SUPPORTED_DOCTYPES[REQUEST_FOR_QUOTATION]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[REQUEST_FOR_QUOTATION]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertIn("docstatus", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertNotIn("vendor", fields)
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("transaction_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertIn("/api/resource/Request%20for%20Quotation", t.calls[0]["url"])

    def test_blanket_order_list_has_no_status_or_grand_total_but_carries_type_and_party_context(
            self):
        # Blanket Order breadth: a NINTH _list_fields branch (erpnext.py) — the SAME absence shape
        # as Stock Entry/Stock Reconciliation/Landed Cost Voucher (party_field=None, status AND
        # grand_total both confirmed ABSENT), but NOT a reuse: the substitute fields differ
        # (blanket_order_type + to_date, not purpose/difference_amount/
        # distribute_charges_based_on). UNIQUELY among those four branches, this one ALSO splices
        # real party context — customer AND supplier both (never just one, which would be wrong for
        # half of all Blanket Orders) — because unlike Stock Entry/SR/LCV (no party concept at all)
        # or RFQ (party lives only in a child table), Blanket Order's party genuinely lives on two
        # scalar header Link fields. date_field="from_date" — confirmed no posting_date AND no
        # transaction_date field anywhere in the 18 enumerated fields.
        c, t = client([(200, {"data": []})])
        c.list_documents(BLANKET_ORDER,
                         party_field=SUPPORTED_DOCTYPES[BLANKET_ORDER]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[BLANKET_ORDER]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("blanket_order_type", fields)
        self.assertIn("customer", fields)
        self.assertIn("supplier", fields)
        self.assertIn("from_date", fields)
        self.assertIn("to_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Blanket%20Order", t.calls[0]["url"])

    def test_job_card_list_has_status_but_no_grand_total_and_carries_operation_context(self):
        # Job Card breadth: an ELEVENTH _list_fields branch (erpnext.py) — the SAME shape as
        # Material Request/Request for Quotation (party_field=None, status present, grand_total
        # confirmed ABSENT), but NOT a reuse: the substitute fields differ (work_order/operation/
        # for_quantity, none of which exist on material_request.json or
        # request_for_quotation.json). date_field="posting_date" — confirmed present, the default.
        c, t = client([(200, {"data": []})])
        c.list_documents(JOB_CARD,
                         party_field=SUPPORTED_DOCTYPES[JOB_CARD]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[JOB_CARD]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("docstatus", fields)
        self.assertIn("work_order", fields)
        self.assertIn("operation", fields)
        self.assertIn("for_quantity", fields)
        self.assertIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Job%20Card", t.calls[0]["url"])

    def test_bom_list_carries_no_date_column_at_all_and_its_own_recipe_context(self):
        # BOM breadth: a TWELFTH _list_fields branch (erpnext.py) — same absence shape as Stock
        # Entry/SR/LCV/Blanket Order (party_field=None, status absent, grand_total absent), NOT a
        # reuse (substitutes differ: item/is_active/is_default/total_cost/has_variants, BOM's own
        # five real in_list_view fields), and genuinely new on the date axis: date_field=None, so
        # the requested column list carries NO date column of any kind — asking the bench for
        # posting_date/transaction_date/from_date on this schema would be the same unknown-column
        # failure class every prior "not a reuse" branch avoids. The None date_field passed
        # through here must also never be spliced in literally (same hazard as party_field=None).
        c, t = client([(200, {"data": []})])
        c.list_documents(BOM,
                         party_field=SUPPORTED_DOCTYPES[BOM]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[BOM]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn(None, fields)  # neither party_field=None nor date_field=None spliced in
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("docstatus", fields)
        self.assertIn("item", fields)
        self.assertIn("is_active", fields)
        self.assertIn("is_default", fields)
        self.assertIn("total_cost", fields)
        self.assertIn("has_variants", fields)
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("from_date", fields)
        self.assertNotIn("creation", fields)  # the rejected fallback stays rejected
        self.assertIn("/api/resource/BOM", t.calls[0]["url"])

    def test_work_order_list_carries_progress_context_and_the_datetime_date_column(self):
        # Work Order breadth: a THIRTEENTH _list_fields branch — the MR/RFQ/Job Card absence
        # shape (party_field=None, status present, grand_total absent), NOT a reuse: substitutes
        # are production_item/qty/produced_qty/bom_no (produced_qty rides as the progress column
        # by meaning, like MR's per_ordered — a dossier correction: it is NOT list-view-flagged
        # in the schema). The date column is planned_start_date, spliced via the parameter as
        # usual — the bench returns the raw Datetime for a LIST read, disclosed as-is (only the
        # closed-books chain needs the date-part projection, which lives in tools.py, never
        # here).
        c, t = client([(200, {"data": []})])
        c.list_documents(WORK_ORDER,
                         party_field=SUPPORTED_DOCTYPES[WORK_ORDER]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[WORK_ORDER]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn(None, fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("docstatus", fields)
        self.assertIn("production_item", fields)
        self.assertIn("qty", fields)
        self.assertIn("produced_qty", fields)
        self.assertIn("bom_no", fields)
        self.assertIn("planned_start_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Work%20Order", t.calls[0]["url"])

    def test_asset_list_carries_identity_value_and_gl_date_columns(self):
        # Asset breadth: a FOURTEENTH _list_fields branch — status present (the widest, 13
        # options), grand_total absent, no party column; substitutes are the five real
        # in_list_view fields (asset_name/asset_category/location + company/status) plus the two
        # value columns (net_purchase_amount, the input cost; total_asset_cost, read-only,
        # populated post-submit — pre-submit rows honestly show it empty, matching the bench's
        # own form). Date column is available_for_use_date, a plain Date.
        c, t = client([(200, {"data": []})])
        c.list_documents(ASSET,
                         party_field=SUPPORTED_DOCTYPES[ASSET]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[ASSET]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn(None, fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("docstatus", fields)
        self.assertIn("asset_name", fields)
        self.assertIn("asset_category", fields)
        self.assertIn("location", fields)
        self.assertIn("net_purchase_amount", fields)
        self.assertIn("total_asset_cost", fields)
        self.assertIn("available_for_use_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("purchase_date", fields)  # the GL date won, deliberately
        self.assertIn("/api/resource/Asset", t.calls[0]["url"])

    def test_packing_slip_list_carries_no_company_or_date_column_at_all(self):
        # Packing Slip breadth: a FIFTEENTH _list_fields branch — the FIRST to omit "company"
        # from the requested columns entirely (packing_slip.json carries no company field at
        # all, on top of the now-familiar dateless omission — the SECOND dateless doctype).
        # Neither status nor grand_total exist either; the three real in_list_view columns are
        # delivery_note/from_case_no/to_case_no, none of which exist on any prior branch's
        # schema (not a reuse). Both party_field=None and date_field=None must never be spliced
        # in literally, and "company" must never appear (asking the bench for a column that
        # doesn't exist on this doctype would be the same unknown-column failure class every
        # prior "not a reuse" branch avoids).
        c, t = client([(200, {"data": []})])
        c.list_documents(PACKING_SLIP,
                         party_field=SUPPORTED_DOCTYPES[PACKING_SLIP]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[PACKING_SLIP]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn(None, fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertNotIn("company", fields)  # the FIRST branch to omit it — confirmed absent
        self.assertIn("docstatus", fields)
        self.assertIn("delivery_note", fields)
        self.assertIn("from_case_no", fields)
        self.assertIn("to_case_no", fields)
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("from_date", fields)
        self.assertNotIn("planned_start_date", fields)
        self.assertNotIn("available_for_use_date", fields)
        self.assertIn("/api/resource/Packing%20Slip", t.calls[0]["url"])

    def test_cost_center_allocation_list_carries_no_status_or_grand_total_substitute_at_all(self):
        # Cost Center Allocation breadth: a SIXTEENTH _list_fields branch — the same absence
        # shape as Stock Entry/Stock Reconciliation/LCV/Blanket Order/BOM (party_field=None,
        # status absent, grand_total absent — confirmed from cost_center_allocation.json's
        # complete 7-field enumeration, the smallest schema this campaign has found), but NOT a
        # reuse: this branch carries no substitute/context column at all beyond its own two real
        # in_list_view fields (main_cost_center, valid_from) — the "no natural analog" shape RFQ's
        # own branch established, here even barer. Unlike BOM/Packing Slip, "company" DOES exist
        # and IS spliced in literally (confirmed present, reqd). date_field="valid_from" is a
        # REAL Date column, spliced via the parameter as usual — never the dateless slot.
        c, t = client([(200, {"data": []})])
        c.list_documents(COST_CENTER_ALLOCATION,
                         party_field=SUPPORTED_DOCTYPES[COST_CENTER_ALLOCATION]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[COST_CENTER_ALLOCATION]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("docstatus", fields)
        self.assertIn("main_cost_center", fields)
        self.assertIn("company", fields)  # UNLIKE Packing Slip, this doctype DOES carry company
        self.assertIn("valid_from", fields)
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("from_date", fields)
        self.assertNotIn("planned_start_date", fields)
        self.assertNotIn("available_for_use_date", fields)
        self.assertIn("/api/resource/Cost%20Center%20Allocation", t.calls[0]["url"])

    def test_supplier_scorecard_period_list_splices_the_real_party_field_and_score_substitute(self):
        # Supplier Scorecard Period breadth: a SEVENTEENTH _list_fields branch — Wave 4's FIRST
        # branch to splice a REAL party_field ("supplier", unlike every party_field=None branch
        # above). status and grand_total both confirmed absent (supplier_scorecard_period.json's
        # complete 12-field enumeration); total_score (a Percent SCORE, never a monetary
        # substitute) stands in for grand_total. "company" is confirmed absent entirely (the
        # SECOND such absence after Packing Slip) and must never be spliced.
        # date_field="start_date" is a REAL Date column, spliced via the parameter as usual —
        # never the dateless slot.
        c, t = client([(200, {"data": []})])
        c.list_documents(
            SUPPLIER_SCORECARD_PERIOD,
            party_field=SUPPORTED_DOCTYPES[SUPPLIER_SCORECARD_PERIOD]["party_field"],
            date_field=SUPPORTED_DOCTYPES[SUPPLIER_SCORECARD_PERIOD]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn("company", fields)  # confirmed absent from the real schema entirely
        self.assertIn("docstatus", fields)
        self.assertIn("supplier", fields)  # the real, spliced party_field — unlike every prior
                                            # party_field=None branch
        self.assertIn("total_score", fields)
        self.assertIn("start_date", fields)
        self.assertNotIn("end_date", fields)  # the sibling date NOT chosen as date_field
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("from_date", fields)
        self.assertNotIn("planned_start_date", fields)
        self.assertNotIn("available_for_use_date", fields)
        self.assertNotIn("valid_from", fields)
        self.assertIn("/api/resource/Supplier%20Scorecard%20Period", t.calls[0]["url"])

    def test_quality_inspection_list_splices_the_reference_pair_and_item_context(self):
        # Quality Inspection breadth: an EIGHTEENTH _list_fields branch — the FIRST Dynamic Link
        # pair since Quotation's own fifth branch. reference_type/reference_name both ride the
        # list tier together (the Quotation precedent: a Dynamic Link's type-half is meaningless
        # without its name-half, so both travel regardless of the schema's own per-field
        # in_list_view flag — only reference_name carries it). item_code/inspection_type are both
        # confirmed in_list_view=1 in the real schema. status confirmed present, grand_total
        # confirmed absent. "company" DOES exist on this schema (unlike Packing Slip/Supplier
        # Scorecard Period) and is spliced in literally — never omitted.
        c, t = client([(200, {"data": []})])
        c.list_documents(
            QUALITY_INSPECTION,
            party_field=SUPPORTED_DOCTYPES[QUALITY_INSPECTION]["party_field"],
            date_field=SUPPORTED_DOCTYPES[QUALITY_INSPECTION]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertIn("docstatus", fields)
        self.assertIn("reference_type", fields)  # rides WITH reference_name, never alone
        self.assertIn("reference_name", fields)
        self.assertIn("item_code", fields)
        self.assertIn("inspection_type", fields)
        self.assertIn("company", fields)  # confirmed present, unlike the two companyless rows
        self.assertIn("report_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("from_date", fields)
        self.assertNotIn("planned_start_date", fields)
        self.assertNotIn("available_for_use_date", fields)
        self.assertNotIn("valid_from", fields)
        self.assertNotIn("start_date", fields)
        self.assertIn("/api/resource/Quality%20Inspection", t.calls[0]["url"])

    def test_installation_note_list_splices_the_real_customer_party_and_remarks(self):
        # Installation Note breadth: a NINETEENTH _list_fields branch — the FIRST to combine a
        # REAL spliced party_field ("customer") with status present, company present, a real
        # date_field, AND a genuinely absent grand_total with no aggregate/type-fork substitute of
        # any kind. remarks is the ONLY other in_list_view-flagged column on this 23-field schema.
        c, t = client([(200, {"data": []})])
        c.list_documents(
            INSTALLATION_NOTE,
            party_field=SUPPORTED_DOCTYPES[INSTALLATION_NOTE]["party_field"],
            date_field=SUPPORTED_DOCTYPES[INSTALLATION_NOTE]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertIn("docstatus", fields)
        self.assertIn("customer", fields)  # the real, spliced party_field
        self.assertIn("remarks", fields)
        self.assertIn("company", fields)  # confirmed present and required, unlike PS/SSP
        self.assertIn("inst_date", fields)
        self.assertNotIn("inst_time", fields)  # a Time field, never the chosen date_field
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("from_date", fields)
        self.assertNotIn("planned_start_date", fields)
        self.assertNotIn("available_for_use_date", fields)
        self.assertNotIn("valid_from", fields)
        self.assertNotIn("start_date", fields)
        self.assertNotIn("report_date", fields)
        self.assertIn("/api/resource/Installation%20Note", t.calls[0]["url"])

    def test_shipment_list_splices_both_dynamic_pairs_and_value_of_goods(self):
        # Shipment breadth: a TWENTIETH _list_fields branch — the FIRST with TWO independent
        # dynamic-selector pairs. party_field=None is never spliced (the branch hardcodes both
        # pairs literally, the same shape Quotation's/Quality Inspection's own branches already
        # established): pickup_from_type rides WITH pickup (never alone), delivery_to_type rides
        # WITH delivery_to (never alone) — doubling the Quotation/QI "type and resolved value
        # ride together" precedent. value_of_goods stands in for the missing grand_total. company
        # is confirmed absent from the real schema entirely (the third companyless doctype).
        c, t = client([(200, {"data": []})])
        c.list_documents(
            SHIPMENT,
            party_field=SUPPORTED_DOCTYPES[SHIPMENT]["party_field"],
            date_field=SUPPORTED_DOCTYPES[SHIPMENT]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertIn("docstatus", fields)
        self.assertIn("pickup_from_type", fields)  # rides WITH pickup, never alone
        self.assertIn("pickup", fields)
        self.assertIn("delivery_to_type", fields)  # rides WITH delivery_to, never alone
        self.assertIn("delivery_to", fields)
        self.assertIn("value_of_goods", fields)
        self.assertNotIn("company", fields)  # confirmed absent from the real schema entirely
        self.assertIn("pickup_date", fields)
        self.assertNotIn("pickup_from", fields)  # a Time field, never the chosen date_field
        self.assertNotIn("pickup_to", fields)  # a Time field, never the chosen date_field
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("from_date", fields)
        self.assertNotIn("planned_start_date", fields)
        self.assertNotIn("available_for_use_date", fields)
        self.assertNotIn("valid_from", fields)
        self.assertNotIn("start_date", fields)
        self.assertNotIn("report_date", fields)
        self.assertNotIn("inst_date", fields)
        self.assertIn("/api/resource/Shipment", t.calls[0]["url"])

    def test_sales_forecast_list_has_status_but_no_grand_total_or_from_date(self):
        # Sales Forecast breadth: a TWENTY-FIRST _list_fields branch — the Material Request/RFQ/
        # Job Card absence shape (party_field=None, status present, grand_total confirmed absent
        # with no substitute of any kind), converging byte-for-byte with RFQ's own bare EIGHTH
        # branch output — a genuine convergence, never a reuse (this doctype still forces its own
        # explicit conditional). from_date is a REAL, required field on this schema but carries no
        # in_list_view flag, so it is deliberately NOT spliced (unlike Blanket Order's own to_date,
        # which rides as context because it IS in_list_view).
        c, t = client([(200, {"data": []})])
        c.list_documents(SALES_FORECAST,
                         party_field=SUPPORTED_DOCTYPES[SALES_FORECAST]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[SALES_FORECAST]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("docstatus", fields)
        self.assertIn("company", fields)
        self.assertIn("posting_date", fields)
        self.assertNotIn("from_date", fields)  # real and required, but NOT in_list_view
        self.assertNotIn("frequency", fields)
        self.assertNotIn("demand_number", fields)
        self.assertNotIn("parent_warehouse", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Sales%20Forecast", t.calls[0]["url"])

    def test_project_update_list_has_only_project_no_status_grand_total_or_company(self):
        # Project Update breadth: a TWENTY-SECOND _list_fields branch — the NARROWEST this
        # campaign has found. status and grand_total are both confirmed absent (the ONLY
        # in_list_view field anywhere in the 9-field schema is "project" itself); company is also
        # confirmed absent (the fourth companyless doctype), so it is never spliced. The real
        # "sent" Check field is deliberately NOT spliced in either — not in_list_view-flagged, and
        # it tracks an unrelated reminder-email side channel, never governance-relevant state.
        c, t = client([(200, {"data": []})])
        c.list_documents(PROJECT_UPDATE,
                         party_field=SUPPORTED_DOCTYPES[PROJECT_UPDATE]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[PROJECT_UPDATE]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertNotIn("company", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn("sent", fields)  # real but not in_list_view; not a manufactured substitute
        self.assertIn("docstatus", fields)
        self.assertIn("project", fields)  # the ONE real in_list_view field
        self.assertIn("date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("from_date", fields)
        self.assertNotIn("pickup_date", fields)
        self.assertIn("/api/resource/Project%20Update", t.calls[0]["url"])

    def test_maintenance_visit_list_has_completion_status_and_maintenance_type_no_grand_total(self):
        # Maintenance Visit breadth: a TWENTY-THIRD _list_fields branch — the SAME categorical
        # shape Installation Note established (real party + status + company, grand_total
        # absent), but NOT a reuse — TWO named substitutes here (completion_status/
        # maintenance_type), not Installation Note's one (remarks). grand_total is confirmed
        # ABSENT (32 fields enumerated, no Currency/Float/Percent anywhere).
        c, t = client([(200, {"data": []})])
        c.list_documents(MAINTENANCE_VISIT,
                         party_field=SUPPORTED_DOCTYPES[MAINTENANCE_VISIT]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[MAINTENANCE_VISIT]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertIn("customer", fields)  # the real, required party_field
        self.assertNotIn("supplier", fields)
        self.assertIn("company", fields)
        self.assertIn("completion_status", fields)
        self.assertIn("maintenance_type", fields)
        self.assertIn("docstatus", fields)
        self.assertIn("mntc_date", fields)
        self.assertNotIn("mntc_time", fields)  # the separate Time field, never the date_field
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("date", fields)  # Project Update's own bare fieldname, not this one
        self.assertIn("/api/resource/Maintenance%20Visit", t.calls[0]["url"])

    def test_maintenance_schedule_list_has_customer_name_no_grand_total(self):
        # Maintenance Schedule breadth: a TWENTY-FOURTH _list_fields branch — the SAME
        # categorical shape Installation Note established (real party + status + company,
        # grand_total absent), but NOT a reuse — customer_name is its own single substitute, not
        # Installation Note's (remarks) or Maintenance Visit's own two (completion_status/
        # maintenance_type). grand_total is confirmed ABSENT (24 fields enumerated, no
        # Currency/Float/Percent anywhere). party_field="customer" splices in even though the
        # field itself is not reqd on this schema.
        c, t = client([(200, {"data": []})])
        c.list_documents(MAINTENANCE_SCHEDULE,
                         party_field=SUPPORTED_DOCTYPES[MAINTENANCE_SCHEDULE]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[MAINTENANCE_SCHEDULE]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertIn("customer", fields)  # the real, but genuinely optional, party_field
        self.assertNotIn("supplier", fields)
        self.assertIn("company", fields)
        self.assertIn("customer_name", fields)
        self.assertNotIn("completion_status", fields)  # Maintenance Visit's own substitute
        self.assertNotIn("maintenance_type", fields)  # Maintenance Visit's own substitute
        self.assertNotIn("remarks", fields)  # Installation Note's own substitute
        self.assertIn("docstatus", fields)
        self.assertIn("transaction_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("mntc_date", fields)
        self.assertNotIn("date", fields)  # Project Update's own bare fieldname, not this one
        self.assertIn("/api/resource/Maintenance%20Schedule", t.calls[0]["url"])

    def test_asset_maintenance_log_list_has_maintenance_status_and_due_date_no_grand_total(self):
        # Asset Maintenance Log breadth: a TWENTY-FIFTH _list_fields branch — the FIRST whose
        # lifecycle-adjacent Select is not literally named "status" (the real fieldname is
        # "maintenance_status"; "status" does not exist on this schema at all, so splicing the
        # literal string "status" would ask the bench for a nonexistent column). party_field=None
        # (never spliced — no party concept at all); grand_total confirmed ABSENT (23 fields
        # enumerated, no Currency/Float/Percent anywhere); company confirmed absent (the fifth
        # companyless doctype). due_date (the read-only scheduling reference) rides alongside
        # completion_date (the date_field) literally, the Blanket Order from_date/to_date
        # precedent.
        c, t = client([(200, {"data": []})])
        c.list_documents(ASSET_MAINTENANCE_LOG,
                         party_field=SUPPORTED_DOCTYPES[ASSET_MAINTENANCE_LOG]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[ASSET_MAINTENANCE_LOG]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("maintenance_status", fields)
        self.assertNotIn("status", fields)  # the literal fieldname does not exist on this schema
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertNotIn("company", fields)  # confirmed absent from the real schema entirely
        self.assertIn("docstatus", fields)
        self.assertIn("completion_date", fields)  # the date_field
        self.assertIn("due_date", fields)  # the read-only reference, rides alongside literally
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Asset%20Maintenance%20Log", t.calls[0]["url"])

    def test_bank_guarantee_list_has_bg_type_and_both_party_fields_no_status_or_grand_total(self):
        # Bank Guarantee breadth: a TWENTY-SIXTH _list_fields branch — the Blanket Order
        # party-splice mechanism (a genuine DUAL CONDITIONAL customer/supplier pair, both spliced
        # together, never one alone) forced together with a companyless, docstatus-only absence
        # shape no prior branch combines. party_field=None (never spliced literally — the pair
        # rides via the hardcoded fieldnames instead, the same not-a-single-fieldname shape
        # Blanket Order/Quotation/Quality Inspection/Shipment already established); status
        # confirmed ABSENT (docstatus only); grand_total confirmed ABSENT (amount is the sole
        # in_list_view Currency field, the aggregate substitute); company confirmed absent (the
        # SIXTH companyless doctype). bg_type rides alongside customer/supplier as the selector
        # context, the Blanket Order blanket_order_type precedent.
        c, t = client([(200, {"data": []})])
        c.list_documents(BANK_GUARANTEE,
                         party_field=SUPPORTED_DOCTYPES[BANK_GUARANTEE]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[BANK_GUARANTEE]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("bg_type", fields)
        self.assertIn("customer", fields)
        self.assertIn("supplier", fields)
        self.assertNotIn("company", fields)  # confirmed absent from the real schema entirely
        self.assertIn("start_date", fields)  # the date_field
        self.assertIn("amount", fields)  # the sole in_list_view Currency field
        self.assertNotIn("end_date", fields)  # client-JS-derived only, never spliced
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Bank%20Guarantee", t.calls[0]["url"])

    def test_asset_movement_list_has_purpose_and_company_no_status_or_grand_total(self):
        # Asset Movement breadth: a TWENTY-SEVENTH _list_fields branch — the MINIMAL shape this
        # campaign has found: no party, no status, no aggregate of any kind, not even a stand-in
        # substitute (unlike Stock Reconciliation's difference_amount or Bank Guarantee's own
        # amount). party_field=None (never spliced — a Dynamic Link provenance pair, never a GL
        # party); status confirmed ABSENT (docstatus only; purpose is a 4-way Select router, the
        # Stock Entry precedent, never a state transition); grand_total confirmed absent with NO
        # substitute at all — the complete 7-real-field enumeration carries no other real field
        # left to splice. date_field="transaction_date" rides via the parameter as usual — the list
        # read keeps the raw Datetime (a display value); only the closed-books chain projects it.
        c, t = client([(200, {"data": []})])
        c.list_documents(ASSET_MOVEMENT,
                         party_field=SUPPORTED_DOCTYPES[ASSET_MOVEMENT]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[ASSET_MOVEMENT]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("purpose", fields)
        self.assertIn("company", fields)  # a real, required field on this schema (unlike BG)
        self.assertIn("transaction_date", fields)  # the date_field
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("/api/resource/Asset%20Movement", t.calls[0]["url"])

    def test_delivery_trip_list_has_a_real_status_and_driver_name_no_grand_total(self):
        # Delivery Trip breadth: a TWENTY-EIGHTH _list_fields branch — a REAL status field (unlike
        # Asset Movement) with NO aggregate of any kind (like Asset Movement). party_field=None
        # (never spliced — driver/employee are metadata, never a GL party); status is included
        # even though the SCHEMA never flags it in_list_view — delivery_trip_list.js (ERPNext's
        # own list-view controller) declares add_fields: ["status"] plus a color-coded
        # get_indicator mapping keyed on it. grand_total confirmed absent with no substitute of
        # any kind. date_field="departure_time" rides via the parameter as usual — the list read
        # keeps the raw Datetime; only the closed-books chain projects it.
        c, t = client([(200, {"data": []})])
        c.list_documents(DELIVERY_TRIP,
                         party_field=SUPPORTED_DOCTYPES[DELIVERY_TRIP]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[DELIVERY_TRIP]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("status", fields)  # a REAL field on this schema, unlike Asset Movement
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("driver_name", fields)  # the real in_list_view field
        self.assertIn("company", fields)
        self.assertIn("departure_time", fields)  # the date_field
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("/api/resource/Delivery%20Trip", t.calls[0]["url"])

    def test_asset_value_adjustment_list_has_three_value_columns_no_status_or_grand_total(self):
        # Asset Value Adjustment breadth: a TWENTY-NINTH _list_fields branch — the SAME
        # party_field=None/status-absent/grand_total-absent shape as Stock Entry/Stock
        # Reconciliation/LCV/Blanket Order/BOM/Cost Center Allocation, but NOT a reuse: THREE
        # real value/reference columns ride here (finance_book/current_asset_value/
        # new_asset_value), none of them a single aggregate. "asset" itself — the field naming
        # WHICH asset this concerns — carries no in_list_view flag on this schema and is honestly
        # NOT spliced. company DOES exist (though not reqd) and is spliced literally, its reality
        # deciding the splice rather than its reqd flag. date_field="date" rides via the
        # parameter as usual.
        c, t = client([(200, {"data": []})])
        c.list_documents(ASSET_VALUE_ADJUSTMENT,
                         party_field=SUPPORTED_DOCTYPES[ASSET_VALUE_ADJUSTMENT]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[ASSET_VALUE_ADJUSTMENT]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("finance_book", fields)  # a real in_list_view field
        self.assertIn("current_asset_value", fields)  # a real in_list_view field
        self.assertIn("new_asset_value", fields)  # a real in_list_view field
        self.assertNotIn("asset", fields)  # NOT in_list_view on this schema — honestly omitted
        self.assertNotIn("difference_amount", fields)  # a real field, but never in_list_view
        self.assertIn("company", fields)  # present (though not reqd), spliced like any branch
        self.assertIn("date", fields)  # the date_field
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("/api/resource/Asset%20Value%20Adjustment", t.calls[0]["url"])

    def test_payment_order_list_has_conditional_party_and_type_context_no_status_or_grand_total(
            self):
        # Payment Order breadth: a THIRTIETH _list_fields branch — the Asset Movement "nothing
        # left to splice" absence shape (status/grand_total both confirmed absent, no substitute
        # of any kind) combined with a genuinely conditional party field spliced literally by
        # name for the FIRST time on a SINGLE (not dual) fieldname. party_field=None (a dossier
        # correction — see test_payment_order_is_partyless_posting_dated_and_rides_run_method
        # above), yet "party" itself rides as a real, in_list_view-flagged context column
        # (alongside payment_order_type, the router explaining why it's blank on Payment-Entry-
        # typed rows — neither blanket_order_type nor bg_type is in_list_view-flagged either, so
        # this is not a new exception). date_field="posting_date" rides via the parameter as
        # usual — the DEFAULT branch, never a distinctive pattern.
        c, t = client([(200, {"data": []})])
        c.list_documents(PAYMENT_ORDER,
                         party_field=SUPPORTED_DOCTYPES[PAYMENT_ORDER]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[PAYMENT_ORDER]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("payment_order_type", fields)  # the router context column
        self.assertIn("party", fields)  # real, conditional, in_list_view — spliced by name
        self.assertIn("company", fields)  # present + reqd, spliced like any ordinary branch
        self.assertIn("posting_date", fields)  # the date_field, the default branch
        self.assertIn("company_bank", fields)  # real in_list_view field, the context substitute
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("/api/resource/Payment%20Order", t.calls[0]["url"])

    def test_share_transfer_list_splices_the_conditional_pair_whole_no_status_or_grand_total(
            self):
        # Share Transfer breadth: a THIRTY-FIRST _list_fields branch — the Asset Movement
        # "nothing left to splice" absence shape (status/grand_total both confirmed absent) with
        # amount as the sole substitute, combined with a dual conditional party PAIR spliced
        # WHOLE for the first time on a THREE-state (not two-state) router: transfer_type has
        # three values (Issue/Purchase/Transfer), and unlike Blanket Order's/Bank Guarantee's own
        # mutually-exclusive pairs, Share Transfer's from_shareholder/to_shareholder can BOTH be
        # populated simultaneously (Transfer type). Neither is in_list_view-flagged on this
        # schema — splicing a conditional pair has never depended on that flag in this table.
        # company IS present + reqd, spliced like any ordinary branch.
        c, t = client([(200, {"data": []})])
        c.list_documents(SHARE_TRANSFER,
                         party_field=SUPPORTED_DOCTYPES[SHARE_TRANSFER]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[SHARE_TRANSFER]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn("status", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn("grand_total", fields)  # confirmed absent from the real schema entirely
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("transfer_type", fields)  # the router context column
        self.assertIn("from_shareholder", fields)  # real, conditional — spliced WHOLE
        self.assertIn("to_shareholder", fields)  # real, conditional — spliced WHOLE
        self.assertIn("company", fields)  # present + reqd, spliced like any ordinary branch
        self.assertIn("date", fields)  # the date_field
        self.assertIn("amount", fields)  # the grand_total substitute
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("customer", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("/api/resource/Share%20Transfer", t.calls[0]["url"])

    def test_bom_creator_list_splices_in_list_view_fields_only_no_party_or_date(self):
        # BOM Creator breadth (2026-07-21) — a THIRTY-SECOND _list_fields branch, the same "both
        # slots blank" shape BOM's/Packing Slip's own branches established (party_field AND
        # date_field both None and unused), but company IS present and spliced literally — unlike
        # Packing Slip, which omits it. The three in_list_view fields (item_code/currency/
        # raw_material_cost) are the confirmed set from bom_creator.json's own 40-field
        # enumeration.
        c, t = client([(200, {"data": []})])
        c.list_documents(BOM_CREATOR,
                         party_field=SUPPORTED_DOCTYPES[BOM_CREATOR]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[BOM_CREATOR]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertIn("status", fields)
        self.assertIn("item_code", fields)
        self.assertIn("currency", fields)
        self.assertIn("raw_material_cost", fields)
        self.assertIn("company", fields)  # present + reqd, spliced like any ordinary branch
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent — raw_material_cost stands in
        self.assertIn("/api/resource/BOM%20Creator", t.calls[0]["url"])

    def test_budget_list_splices_the_conditional_pair_whole_and_no_date_at_all(self):
        # Budget breadth (2026-07-21) — a THIRTY-THIRD _list_fields branch, the barest shape this
        # campaign has found alongside a real dual conditional pair: no status, no grand_total,
        # no substitute for either (budget_amount/budget_distribution_total carry no
        # in_list_view flag, unlike Bank Guarantee's amount/Stock Reconciliation's
        # difference_amount), AND no date spliced at all despite a real, present date_field —
        # because BOTH budget_start_date/budget_end_date are hidden:1 on the schema (see
        # erpnext.py's module docstring "fifth wrinkle"). cost_center/project are spliced WHOLE
        # regardless of the in_list_view flag (neither carries it) — the same "splice a
        # conditional pair together" discipline Blanket Order's/Bank Guarantee's/Share Transfer's
        # own pairs already established.
        c, t = client([(200, {"data": []})])
        c.list_documents(BUDGET,
                         party_field=SUPPORTED_DOCTYPES[BUDGET]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[BUDGET]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("docstatus", fields)
        self.assertNotIn("status", fields)  # confirmed absent from the real schema entirely
        self.assertIn("budget_against", fields)  # the router
        self.assertIn("cost_center", fields)  # conditional pair — spliced WHOLE
        self.assertIn("project", fields)  # conditional pair — spliced WHOLE
        self.assertIn("company", fields)  # present + reqd, spliced like any ordinary branch
        self.assertIn("account", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent, no substitute
        self.assertNotIn("budget_amount", fields)  # not in_list_view-flagged — never spliced
        self.assertNotIn("budget_start_date", fields)  # hidden — no date at all in the list tier
        self.assertNotIn("budget_end_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Budget", t.calls[0]["url"])

    def test_timesheet_list_splices_party_status_per_billed_and_start_date(self):
        # Timesheet breadth (2026-07-21) — a NEW _list_fields branch: party PRESENT (customer,
        # unlike Budget) + status PRESENT + grand_total ABSENT with no substitute
        # (total_billable_amount/total_billed_amount carry no in_list_view flag) + per_billed
        # (the schema's own additional in_list_view-flagged column) + a real, spliced date_field
        # (start_date, itself in_list_view-flagged — unlike Budget's hidden pair).
        c, t = client([(200, {"data": []})])
        c.list_documents(TIMESHEET,
                         party_field=SUPPORTED_DOCTYPES[TIMESHEET]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[TIMESHEET]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("docstatus", fields)
        self.assertIn("status", fields)  # confirmed present on the real schema
        self.assertIn("customer", fields)  # the party field, spliced literally
        self.assertIn("per_billed", fields)  # the only other in_list_view-flagged field
        self.assertIn("company", fields)  # present-but-optional, still spliced like any branch
        self.assertIn("start_date", fields)  # the date_field, itself in_list_view-flagged
        self.assertNotIn("grand_total", fields)  # confirmed absent, no substitute
        self.assertNotIn("total_billable_amount", fields)  # not in_list_view-flagged — never
        self.assertNotIn("total_billed_amount", fields)    # spliced as a grand_total stand-in
        self.assertNotIn("end_date", fields)  # the window's close, never the anchor
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Timesheet", t.calls[0]["url"])

    def test_contract_list_splices_the_dynamic_party_pair_doubled_status_and_signed_on(self):
        # Contract breadth (2026-07-21) — a NEW _list_fields branch (the 33rd special branch by
        # direct count): party_field=None so the Dynamic Link pair (party_type/party_name) is
        # spliced WHOLE (never party_field itself, which is None); TWO status-type columns
        # (status + fulfilment_status) spliced simultaneously — no prior branch carries both;
        # grand_total confirmed absent with no substitute; no company (companyless — omitted,
        # never spliced as an empty string); document_name rides as reference context;
        # contract_terms (a Text Editor, though in_list_view on the real schema) is deliberately
        # NOT spliced — no branch in this campaign has ever spliced a Text Editor field.
        c, t = client([(200, {"data": []})])
        c.list_documents(CONTRACT,
                         party_field=SUPPORTED_DOCTYPES[CONTRACT]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[CONTRACT]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("docstatus", fields)
        self.assertIn("status", fields)
        self.assertIn("fulfilment_status", fields)
        self.assertIn("party_type", fields)
        self.assertIn("party_name", fields)
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertIn("signed_on", fields)  # the date_field
        self.assertIn("document_name", fields)  # reference context, the QI reference_name role
        self.assertNotIn("company", fields)  # companyless — omitted, never an empty splice
        self.assertNotIn("grand_total", fields)  # confirmed absent, no substitute
        self.assertNotIn("contract_terms", fields)  # a Text Editor — deliberately not spliced
        self.assertNotIn("start_date", fields)  # the window, never the date_field pin
        self.assertNotIn("end_date", fields)
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Contract", t.calls[0]["url"])

    def test_pick_list_list_splices_party_status_purpose_router_no_date_or_grand_total(self):
        # Pick List breadth (2026-07-21) — a NEW _list_fields branch (the 34th special branch by
        # direct count): party PRESENT (customer, spliced via the parameter as usual — a plain,
        # always-real header Link, never gated on its own depends_on UI directive). status
        # PRESENT and spliced despite being BOTH hidden:1 AND un-flagged in_list_view (confirmed
        # real and list-tier-worthy by pick_list_list.js's own get_indicator reading doc.status
        # directly). grand_total confirmed absent with no substitute (in_list_view is exactly
        # {company, customer}). purpose rides as the router/context column even though the schema
        # never flags it in_list_view — the Payment Order/Blanket Order precedent. date_field is
        # dropped entirely (the declared-dateless None, the FOURTH such member) — no column to
        # splice.
        c, t = client([(200, {"data": []})])
        c.list_documents(PICK_LIST,
                         party_field=SUPPORTED_DOCTYPES[PICK_LIST]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[PICK_LIST]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("docstatus", fields)
        self.assertIn("status", fields)  # confirmed real, despite hidden:1 + un-flagged
        self.assertIn("purpose", fields)  # the router context column
        self.assertIn("customer", fields)  # the party field, spliced literally
        self.assertIn("company", fields)  # reqd, spliced like any ordinary branch
        self.assertNotIn(None, fields)  # date_field=None must never be spliced in literally
        self.assertNotIn("grand_total", fields)  # confirmed absent, no substitute
        self.assertNotIn("delivery_status", fields)  # real but never in_list_view — not spliced
        self.assertNotIn("per_delivered", fields)     # same
        self.assertNotIn("posting_date", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertNotIn("supplier", fields)
        self.assertIn("/api/resource/Pick%20List", t.calls[0]["url"])

    def test_asset_capitalization_list_splices_total_value_no_party_no_status(self):
        # Asset Capitalization breadth (2026-07-21) — a NEW _list_fields branch (the 37th special
        # branch by direct count, re-establishing the request-shape test the two prior landings
        # (Asset Repair/Invoice Discounting) skipped, not retroactively added to either): no
        # status field at all (confirmed absent, unlike Invoice Discounting's own real-but-
        # unflagged status), no party column (party_field=None, nothing to splice by name),
        # total_value rides as the grand_total substitute despite carrying no in_list_view flag of
        # its own (in_list_view is confirmed EXACTLY {posting_date}) — the same judgment call
        # Invoice Discounting's own total_amount made.
        c, t = client([(200, {"data": []})])
        c.list_documents(ASSET_CAPITALIZATION,
                         party_field=SUPPORTED_DOCTYPES[ASSET_CAPITALIZATION]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[ASSET_CAPITALIZATION]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("docstatus", fields)
        self.assertIn("posting_date", fields)
        self.assertIn("company", fields)  # reqd, spliced like any ordinary branch
        self.assertIn("total_value", fields)  # the grand_total substitute
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertNotIn("status", fields)  # confirmed absent, no such field on this doctype
        self.assertNotIn("grand_total", fields)  # confirmed absent
        self.assertNotIn("target_asset", fields)  # real but never in_list_view — not spliced
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Asset%20Capitalization", t.calls[0]["url"])

    def test_production_plan_list_splices_the_five_real_columns_no_grand_total(self):
        # Production Plan breadth (2026-07-21) — a NEW _list_fields branch (the 38th special
        # branch by direct count): party_field=None (customer is a conditional UI filter, not a
        # GL party) yet customer STILL rides literally because it carries in_list_view:1 in its
        # own right (json.load-confirmed real order: company, get_items_from, posting_date,
        # item_code, customer — item_code precedes customer, the dossier's own order was
        # reversed). status is PRESENT (8 options) but spliced per the Pick List precedent
        # (included even though not itself in_list_view-flagged). grand_total confirmed ABSENT
        # with NO substitute (unlike Invoice Discounting's/Asset Capitalization's own aggregate
        # stand-ins) — the Float counters (total_planned_qty/total_produced_qty) correctly stay
        # unspliced.
        c, t = client([(200, {"data": []})])
        c.list_documents(PRODUCTION_PLAN,
                         party_field=SUPPORTED_DOCTYPES[PRODUCTION_PLAN]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[PRODUCTION_PLAN]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("docstatus", fields)
        self.assertIn("status", fields)  # present, spliced per the Pick List precedent
        self.assertIn("company", fields)
        self.assertIn("get_items_from", fields)  # the Select-typed filter column
        self.assertIn("posting_date", fields)
        self.assertIn("item_code", fields)
        self.assertIn("customer", fields)  # real, in_list_view:1, spliced literally
        self.assertNotIn(None, fields)  # party_field=None must never be spliced in literally
        self.assertNotIn("grand_total", fields)  # confirmed absent, no substitute
        self.assertNotIn("total_planned_qty", fields)  # a Float counter, never the aggregate slot
        self.assertNotIn("total_produced_qty", fields)
        self.assertNotIn("transaction_date", fields)
        self.assertIn("/api/resource/Production%20Plan", t.calls[0]["url"])

    def test_subcontracting_order_list_splices_total_not_grand_total_plus_per_received(self):
        # Subcontracting Order breadth (2026-07-21) — the THIRTY-NINTH special _list_fields
        # branch by direct count: party_field="supplier" splices literally (a plain, always-real
        # GL party). The genuine divergence forcing a new branch: the grand-total-equivalent
        # field is named "total", not "grand_total" (json.load-confirmed, depends_on
        # purchase_order) — the generic branch's literal "grand_total" would ask a real bench for
        # a column that doesn't exist on this doctype. per_received (in_list_view:1 alongside
        # transaction_date, json.load-confirmed) rides along too, the Material Request
        # per_ordered/per_received precedent for a completion-tracking column.
        c, t = client([(200, {"data": []})])
        c.list_documents(SUBCONTRACTING_ORDER,
                         party_field=SUPPORTED_DOCTYPES[SUBCONTRACTING_ORDER]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[SUBCONTRACTING_ORDER]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("docstatus", fields)
        self.assertIn("status", fields)
        self.assertIn("company", fields)
        self.assertIn("supplier", fields)  # party_field spliced literally
        self.assertIn("transaction_date", fields)  # date_field
        self.assertIn("total", fields)  # the real stand-in fieldname
        self.assertIn("per_received", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent under that literal name
        self.assertNotIn(None, fields)
        self.assertIn("/api/resource/Subcontracting%20Order", t.calls[0]["url"])

    def test_subcontracting_inward_order_list_splices_six_per_star_percent_columns_no_total(self):
        # Subcontracting Inward Order breadth (2026-07-22) — the FORTIETH special _list_fields
        # branch by direct count: party_field="customer" splices literally (a plain, always-real
        # GL party). No grand_total/total substitute exists on this doctype AT ALL — it tracks
        # operational progress only, via six in_list_view:1 Percent columns (json.load-confirmed,
        # the full 7-field in_list_view set alongside transaction_date). The widest
        # operational-tracking branch this campaign has built.
        c, t = client([(200, {"data": []})])
        c.list_documents(SUBCONTRACTING_INWARD_ORDER,
                         party_field=SUPPORTED_DOCTYPES[SUBCONTRACTING_INWARD_ORDER]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[SUBCONTRACTING_INWARD_ORDER]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("docstatus", fields)
        self.assertIn("status", fields)
        self.assertIn("company", fields)
        self.assertIn("customer", fields)  # party_field spliced literally
        self.assertIn("transaction_date", fields)  # date_field
        self.assertIn("per_raw_material_received", fields)
        self.assertIn("per_produced", fields)
        self.assertIn("per_process_loss", fields)
        self.assertIn("per_delivered", fields)
        self.assertIn("per_raw_material_returned", fields)
        self.assertIn("per_returned", fields)
        self.assertNotIn("grand_total", fields)
        self.assertNotIn("total", fields)
        self.assertNotIn(None, fields)
        self.assertIn("/api/resource/Subcontracting%20Inward%20Order", t.calls[0]["url"])

    def test_subcontracting_receipt_list_splices_total_not_grand_total_plus_per_returned(self):
        # Subcontracting Receipt breadth (2026-07-22) — THE ROOF ROW: the FORTY-FIRST special
        # _list_fields branch by direct count. party_field="supplier" splices literally (a plain,
        # always-real GL party). The ONE genuine divergence, the SAME shape Subcontracting
        # Order's own branch already established: the grand-total-equivalent field is named
        # "total", not "grand_total" (json.load-confirmed, Currency, read_only). per_returned
        # (in_list_view:1 alongside posting_date, json.load-confirmed — the doctype's own EXACT
        # in_list_view set, no third field) rides along too.
        c, t = client([(200, {"data": []})])
        c.list_documents(SUBCONTRACTING_RECEIPT,
                         party_field=SUPPORTED_DOCTYPES[SUBCONTRACTING_RECEIPT]["party_field"],
                         date_field=SUPPORTED_DOCTYPES[SUBCONTRACTING_RECEIPT]["date_field"])
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("docstatus", fields)
        self.assertIn("status", fields)
        self.assertIn("company", fields)
        self.assertIn("supplier", fields)  # party_field spliced literally
        self.assertIn("posting_date", fields)  # date_field
        self.assertIn("total", fields)  # the real stand-in fieldname
        self.assertIn("per_returned", fields)
        self.assertNotIn("grand_total", fields)  # confirmed absent under that literal name
        self.assertNotIn(None, fields)
        self.assertIn("/api/resource/Subcontracting%20Receipt", t.calls[0]["url"])


class TestPreview(unittest.TestCase):
    def test_preview_posts_dotted_method_with_args(self):
        c, t = client([(200, {"message": {"gl_columns": [], "gl_data": [{"account": "Debtors"}]}})])
        out = c.ledger_preview(company="Example Corp", doctype="Sales Invoice", docname="SI-1")
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"https://erp.example.com/api/method/{PREVIEW_METHOD}")
        self.assertEqual(call["body"], {"company": "Example Corp",
                                        "doctype": "Sales Invoice", "docname": "SI-1"})
        self.assertEqual(out["gl_data"][0]["account"], "Debtors")

    def test_preview_for_purchase_invoice(self):
        # ledger_preview was already doctype-generic before this increment (design confirmed from
        # source) — this pins that Purchase Invoice rides the exact same call shape.
        c, t = client([(200, {"message": {"gl_columns": [], "gl_data": []}})])
        c.ledger_preview(company="Example Corp", doctype="Purchase Invoice", docname="PINV-1")
        self.assertEqual(t.calls[0]["body"], {"company": "Example Corp",
                                              "doctype": "Purchase Invoice", "docname": "PINV-1"})

    def test_preview_for_journal_entry(self):
        # scout-je.md §3: show_accounting_ledger_preview dispatches to doc.make_gl_entries()
        # polymorphically — JournalEntry.make_gl_entries matches the no-arg call shape exactly.
        c, t = client([(200, {"message": {"gl_columns": [], "gl_data": []}})])
        c.ledger_preview(company="Example Corp", doctype="Journal Entry", docname="JE-1")
        self.assertEqual(t.calls[0]["body"], {"company": "Example Corp",
                                              "doctype": "Journal Entry", "docname": "JE-1"})


class TestSubmit(unittest.TestCase):
    def test_submit_rides_the_scopeable_doc_method_surface(self):
        c, t = client([(200, {"data": {"name": "SI-1", "docstatus": 1}})])
        out = c.submit_document(SALES_INVOICE, "SI-1")
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        # run_method travels in the QUERY STRING so guard's classifier (form_dict) always sees it.
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Sales%20Invoice/SI-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_submit_never_sends_adv_adj_or_posting_date(self):
        c, t = client([(200, {"data": {}})])
        c.submit_document(SALES_INVOICE, "SI-1")
        sent = json.dumps([t.calls[0]["params"], t.calls[0]["body"]])
        self.assertNotIn("adv_adj", sent)
        self.assertNotIn("posting_date", sent)

    def test_purchase_invoice_submit_shape(self):
        c, t = client([(200, {"data": {"name": "PINV-1", "docstatus": 1}})])
        out = c.submit_document(PURCHASE_INVOICE, "PINV-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Purchase%20Invoice/PINV-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_journal_entry_submit_rides_frappe_client_submit_with_the_fetched_doc(self):
        # JournalEntry overrides submit()/cancel() WITHOUT @frappe.whitelist() (PHASE L) — the
        # run_method vector 403s — so JE alone rides frappe.client.submit, which calls
        # frappe.get_doc(doc); doc.submit() server-side and needs the FULL doc body, not just a
        # name. This is the override-doctype submit path pacioli_guard's body-doctype scoping
        # (scope.body_scoped_target) now makes safe to enforce per-doctype.
        fetched_doc = {"doctype": "Journal Entry", "name": "JE-1", "docstatus": 0,
                       "total_debit": 100.0, "total_credit": 100.0}
        c, t = client([(200, {"message": {"name": "JE-1", "docstatus": 1}})])
        out = c.submit_document(JOURNAL_ENTRY, "JE-1", doc=fetched_doc)
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://erp.example.com/api/method/frappe.client.submit")
        self.assertEqual(call["body"], {"doc": fetched_doc})
        self.assertIsNone(call["params"])
        self.assertEqual(out["docstatus"], 1)

    def test_journal_entry_submit_without_a_doc_raises(self):
        # Fails closed rather than silently falling back to the 403ing run_method shape or
        # sending a bodyless frappe.client.submit that frappe would reject anyway.
        c, t = client()
        with self.assertRaises(ErpnextError):
            c.submit_document(JOURNAL_ENTRY, "JE-1")
        self.assertEqual(t.calls, [])

    def test_journal_entry_submit_never_sends_adv_adj_or_posting_date(self):
        fetched_doc = {"doctype": "Journal Entry", "name": "JE-1"}
        c, t = client([(200, {"message": {}})])
        c.submit_document(JOURNAL_ENTRY, "JE-1", doc=fetched_doc)
        sent = json.dumps(t.calls[0]["body"])
        self.assertNotIn("adv_adj", sent)
        self.assertNotIn("posting_date", sent)

    def test_journal_entry_submit_missing_message_envelope_is_an_error(self):
        c, t = client([(200, {"unexpected": True})])
        with self.assertRaises(ErpnextError):
            c.submit_document(JOURNAL_ENTRY, "JE-1", doc={"doctype": "Journal Entry"})

    def test_sales_order_submit_shape(self):
        # Mechanically identical to SI/PI/PE — Sales Order overrides neither submit() nor
        # cancel() (confirmed from sales_order.py, version-16), so it rides the same run_method
        # doc-method surface, never the client_rpc path Journal Entry alone needs.
        c, t = client([(200, {"data": {"name": "SO-1", "docstatus": 1}})])
        out = c.submit_document(SALES_ORDER, "SO-1")
        call = t.calls[0]
        self.assertEqual(call["url"], "https://erp.example.com/api/resource/Sales%20Order/SO-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_purchase_order_submit_shape(self):
        # Mechanically identical to Sales Order (and SI/PI/PE) — Purchase Order overrides
        # neither submit() nor cancel() (confirmed from purchase_order.py, version-16), so it
        # rides the same run_method doc-method surface too.
        c, t = client([(200, {"data": {"name": "PO-1", "docstatus": 1}})])
        out = c.submit_document(PURCHASE_ORDER, "PO-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Purchase%20Order/PO-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_material_request_submit_shape(self):
        # Mechanically identical to Sales/Purchase Order on this axis — Material Request
        # overrides neither submit() nor cancel() (confirmed from material_request.py,
        # version-16), so it rides the same run_method doc-method surface too.
        c, t = client([(200, {"data": {"name": "MR-1", "docstatus": 1}})])
        out = c.submit_document(MATERIAL_REQUEST, "MR-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Material%20Request/MR-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_delivery_note_submit_shape(self):
        # Mechanically identical to SI/PI/PE/SO/PO/MR on this axis — Delivery Note overrides
        # neither submit() nor cancel() (confirmed from delivery_note.py, version-16), so it rides
        # the same run_method doc-method surface too, despite being the first stock-primary
        # doctype (that difference lives entirely in on_submit/on_cancel's own SIDE EFFECTS —
        # SLE + conditional GL — never in the submit/cancel TRANSPORT shape this test pins).
        c, t = client([(200, {"data": {"name": "DN-1", "docstatus": 1}})])
        out = c.submit_document(DELIVERY_NOTE, "DN-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Delivery%20Note/DN-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_purchase_receipt_submit_shape(self):
        # Mechanically identical to every prior doctype on this axis — Purchase Receipt overrides
        # neither submit() nor cancel() (confirmed from purchase_receipt.py, version-16), so it
        # rides the same run_method doc-method surface too, despite being the second stock-primary
        # doctype (that difference lives entirely in on_submit/on_cancel's own SIDE EFFECTS — SLE
        # + conditional GL — never in the submit/cancel TRANSPORT shape this test pins).
        c, t = client([(200, {"data": {"name": "PR-1", "docstatus": 1}})])
        out = c.submit_document(PURCHASE_RECEIPT, "PR-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Purchase%20Receipt/PR-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_stock_entry_submit_shape(self):
        # Mechanically identical to every prior doctype on this axis — Stock Entry overrides
        # neither submit() nor cancel() (confirmed from all 4875 lines of stock_entry.py,
        # version-16), so it rides the same run_method doc-method surface too, despite being the
        # third stock-primary doctype AND the first genuinely polymorphic one (that difference
        # lives entirely in on_submit/on_cancel's own SIDE EFFECTS — SLE + conditional GL + the
        # purpose-driven cascade map — never in the submit/cancel TRANSPORT shape this test pins).
        c, t = client([(200, {"data": {"name": "SE-1", "docstatus": 1}})])
        out = c.submit_document(STOCK_ENTRY, "SE-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Stock%20Entry/SE-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_supplier_quotation_submit_shape(self):
        # Mechanically identical to Purchase Order (and every other doctype landed so far) on
        # this axis — Supplier Quotation overrides neither submit() nor cancel() (confirmed from
        # all 362 lines of supplier_quotation.py, version-16), so it rides the same run_method
        # doc-method surface too.
        c, t = client([(200, {"data": {"name": "SQ-1", "docstatus": 1}})])
        out = c.submit_document(SUPPLIER_QUOTATION, "SQ-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Supplier%20Quotation/SQ-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_quotation_submit_shape(self):
        # Mechanically identical to every prior doctype on this axis, despite the dynamic-party
        # DECISION being unique — Quotation overrides neither submit() nor cancel() (confirmed
        # from all of quotation.py, version-16), so it rides the same run_method doc-method
        # surface too; the dynamic-party finding lives entirely in the party_field/_list_fields
        # config, never in the submit/cancel TRANSPORT shape this test pins.
        c, t = client([(200, {"data": {"name": "Q-1", "docstatus": 1}})])
        out = c.submit_document(QUOTATION, "Q-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Quotation/Q-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_pos_invoice_submit_shape(self):
        # Mechanically identical to every prior doctype on this axis — POS Invoice overrides
        # neither submit() nor cancel() (confirmed from all 1119 lines of pos_invoice.py,
        # version-16), so it rides the same run_method doc-method surface too, despite its own
        # on_submit/on_cancel overriding SalesInvoice's WITHOUT calling super() (the finding that
        # means a real submit posts no GL/SL of its own at all — see erpnext.py's module
        # docstring). That divergence lives entirely in what on_submit DOES once frappe's own
        # submit dispatch reaches it, never in the TRANSPORT shape this test pins.
        c, t = client([(200, {"data": {"name": "POSI-1", "docstatus": 1}})])
        out = c.submit_document(POS_INVOICE, "POSI-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/POS%20Invoice/POSI-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_dunning_submit_shape(self):
        # Mechanically identical to every prior doctype on this axis — Dunning overrides neither
        # submit() nor cancel() nor even on_submit() (confirmed from all 276 lines of dunning.py,
        # version-16), so it rides the same run_method doc-method surface too. This test pins the
        # TRANSPORT shape only; the genuinely new finding (the native ledger_preview RPC being
        # uncallable for this doctype) lives entirely in tools.py's plan_submit branch, never here.
        c, t = client([(200, {"data": {"name": "DUNN-1", "docstatus": 1}})])
        out = c.submit_document(DUNNING, "DUNN-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Dunning/DUNN-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_landed_cost_voucher_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype on this axis — Landed Cost
        # Voucher overrides neither submit() nor cancel() (confirmed from all 522 lines of
        # landed_cost_voucher.py, version-16), so it rides the same run_method doc-method surface
        # too. This test pins the TRANSPORT shape only; the genuinely new finding (the native
        # ledger_preview RPC being uncallable, AND even if callable describing the wrong document)
        # lives entirely in tools.py's plan_submit/plan_cancel branches, never here.
        c, t = client([(200, {"data": {"name": "LCV-1", "docstatus": 1}})])
        out = c.submit_document(LANDED_COST_VOUCHER, "LCV-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Landed%20Cost%20Voucher/LCV-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_stock_reconciliation_submit_rides_frappe_client_submit_with_the_fetched_doc(self):
        # StockReconciliation overrides submit()/cancel() themselves (not just on_submit/
        # on_cancel), neither decorated @frappe.whitelist() (PHASE L's own JE mechanism) — the
        # run_method vector 403s, so Stock Reconciliation alone joins Journal Entry on
        # frappe.client.submit, which calls frappe.get_doc(doc); doc.submit() server-side and
        # needs the FULL doc body, not just a name.
        fetched_doc = {"doctype": "Stock Reconciliation", "name": "SR-1", "docstatus": 0,
                       "purpose": "Stock Reconciliation", "difference_amount": 50.0}
        c, t = client([(200, {"message": {"name": "SR-1", "docstatus": 1}})])
        out = c.submit_document(STOCK_RECONCILIATION, "SR-1", doc=fetched_doc)
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://erp.example.com/api/method/frappe.client.submit")
        self.assertEqual(call["body"], {"doc": fetched_doc})
        self.assertIsNone(call["params"])
        self.assertEqual(out["docstatus"], 1)

    def test_stock_reconciliation_submit_without_a_doc_raises(self):
        # Fails closed rather than silently falling back to the 403ing run_method shape or
        # sending a bodyless frappe.client.submit that frappe would reject anyway — the same
        # belt Journal Entry's own landing built, proven generic here.
        c, t = client()
        with self.assertRaises(ErpnextError):
            c.submit_document(STOCK_RECONCILIATION, "SR-1")
        self.assertEqual(t.calls, [])

    def test_request_for_quotation_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype on this axis — RFQ overrides
        # neither submit() nor cancel() (confirmed from all 675 lines of
        # request_for_quotation.py, version-16), so it rides the same run_method doc-method
        # surface too. This test pins the TRANSPORT shape only; the genuinely new finding
        # (on_submit's supplier email dispatch) lives entirely in erpnext.py's/tools.py's module
        # docstrings as a plan-tier disclosure note, never in a transport-level test.
        c, t = client([(200, {"data": {"name": "RFQ-1", "docstatus": 1}})])
        out = c.submit_document(REQUEST_FOR_QUOTATION, "RFQ-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Request%20for%20Quotation/RFQ-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_blanket_order_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype on this axis — BlanketOrder
        # overrides neither submit() nor cancel() (confirmed from all 201 lines of
        # blanket_order.py, version-16 — a genuine rarity this campaign: it defines no
        # on_submit/on_cancel hook AT ALL either), so it rides the same run_method doc-method
        # surface too. This test pins the TRANSPORT shape only; the genuinely new finding (the
        # native ledger_preview RPC being uncallable) lives entirely in tools.py's plan_submit
        # branch, never in a transport-level test.
        c, t = client([(200, {"data": {"name": "BO-1", "docstatus": 1}})])
        out = c.submit_document(BLANKET_ORDER, "BO-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Blanket%20Order/BO-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_job_card_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype on this axis — JobCard overrides
        # neither submit() nor cancel() (confirmed from all 1875 lines of job_card.py, version-16:
        # only on_submit/on_cancel hooks, plus 14 separate @frappe.whitelist() callables that are
        # NOT submit/cancel overrides — see erpnext.py's own module docstring), so it rides the
        # same run_method doc-method surface too. This test pins the TRANSPORT shape only; the
        # genuinely new finding (the native ledger_preview RPC being uncallable) lives entirely in
        # tools.py's plan_submit branch, never in a transport-level test.
        c, t = client([(200, {"data": {"name": "JC-1", "docstatus": 1}})])
        out = c.submit_document(JOB_CARD, "JC-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Job%20Card/JC-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_bom_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype on this axis — BOM overrides
        # neither submit() nor cancel() (bom.py:104, BOM(WebsiteGenerator): only on_submit/
        # on_cancel hooks at 397/401 — see erpnext.py's own module docstring), so it rides the
        # same run_method doc-method surface. This pins the TRANSPORT shape only; everything
        # genuinely new about BOM (the declared-dateless closed-books branch, the uncallable
        # preview) lives in tools.py/plan.py, never at the transport level.
        c, t = client([(200, {"data": {"name": "BOM-CHAIR-001", "docstatus": 1}})])
        out = c.submit_document(BOM, "BOM-CHAIR-001")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/BOM/BOM-CHAIR-001")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_work_order_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype — WorkOrder overrides neither
        # submit() nor cancel() (work_order.py:70; on_submit/on_cancel hooks only), so it rides
        # run_method. Transport shape only; the genuinely new pieces (the datetime->date
        # projection, the uncallable preview, the two ungated status mutators) live in
        # tools.py/erpnext.py's docstrings, never at the transport level.
        c, t = client([(200, {"data": {"name": "MFG-WO-00001", "docstatus": 1}})])
        out = c.submit_document(WORK_ORDER, "MFG-WO-00001")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Work%20Order/MFG-WO-00001")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_asset_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype — Asset(AccountsController)
        # overrides neither submit() nor cancel() (asset.py:41; hooks only). Transport shape
        # only; everything genuinely new (the two async GL channels, the callable preview's
        # conditional emptiness, the structural cancel path) lives in tools.py's disclosure
        # layer, never at the transport level.
        c, t = client([(200, {"data": {"name": "ACC-ASS-2026-00001", "docstatus": 1}})])
        out = c.submit_document(ASSET, "ACC-ASS-2026-00001")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Asset/ACC-ASS-2026-00001")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_packing_slip_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype — PackingSlip(StatusUpdater)
        # overrides neither submit() nor cancel() (packing_slip.py:13; on_submit/on_cancel hooks
        # only at 73/76), so it rides run_method. Transport shape only; everything genuinely new
        # (the declared-dateless closed-books branch reused from BOM, the uncallable preview,
        # the companyless wrong-books behavior) lives in tools.py/erpnext.py's docstrings, never
        # at the transport level.
        c, t = client([(200, {"data": {"name": "MAT-PAC-2026-00001", "docstatus": 1}})])
        out = c.submit_document(PACKING_SLIP, "MAT-PAC-2026-00001")
        call = t.calls[0]
        self.assertEqual(
            call["url"],
            "https://erp.example.com/api/resource/Packing%20Slip/MAT-PAC-2026-00001")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_cost_center_allocation_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype —
        # CostCenterAllocation(Document) overrides neither submit() nor cancel()
        # (cost_center_allocation.py:30; no on_submit/on_cancel hook of any kind at all), so it
        # rides run_method. Transport shape only; everything genuinely new (the dossier's dated-
        # vs-dateless contradiction settled as a REAL date field, the uncallable preview, the
        # normal company posture) lives in tools.py/erpnext.py's docstrings, never at the
        # transport level.
        c, t = client([(200, {"data": {"name": "CC-ALLOC-00001", "docstatus": 1}})])
        out = c.submit_document(COST_CENTER_ALLOCATION, "CC-ALLOC-00001")
        call = t.calls[0]
        self.assertEqual(
            call["url"],
            "https://erp.example.com/api/resource/Cost%20Center%20Allocation/CC-ALLOC-00001")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_supplier_scorecard_period_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype —
        # SupplierScorecardPeriod(Document) overrides neither submit() nor cancel()
        # (supplier_scorecard_period.py:16; no on_submit/on_cancel hook of any kind at all), so it
        # rides run_method. Transport shape only; everything genuinely new (the real party field,
        # the start_date-vs-end_date pick, the uncallable preview, the companyless wrong-books
        # behavior) lives in tools.py/erpnext.py's docstrings, never at the transport level.
        c, t = client([(200, {"data": {"name": "SSP-2026-00001", "docstatus": 1}})])
        out = c.submit_document(SUPPLIER_SCORECARD_PERIOD, "SSP-2026-00001")
        call = t.calls[0]
        self.assertEqual(
            call["url"],
            "https://erp.example.com/api/resource/Supplier%20Scorecard%20Period/SSP-2026-00001")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_quality_inspection_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype — QualityInspection(Document)
        # overrides neither submit() nor cancel() (quality_inspection.py:20; only on_discard/
        # on_update/on_submit/on_cancel/on_trash/before_submit hooks), so it rides run_method.
        # Transport shape only; everything genuinely new (the Dynamic Link pair, the
        # before_submit readings-status gate, the uncallable preview, the update_qc_reference
        # cross-document write) lives in tools.py/erpnext.py's docstrings, never at the
        # transport level.
        c, t = client([(200, {"data": {"name": "QI-2026-00001", "docstatus": 1}})])
        out = c.submit_document(QUALITY_INSPECTION, "QI-2026-00001")
        call = t.calls[0]
        self.assertEqual(
            call["url"],
            "https://erp.example.com/api/resource/Quality%20Inspection/QI-2026-00001")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_installation_note_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype — InstallationNote(TransactionBase)
        # overrides neither submit() nor cancel() (installation_note.py:13; only validate/
        # on_update/on_submit/on_cancel hooks + five private helpers), so it rides run_method.
        # Transport shape only; everything genuinely new (the real customer party field, the
        # deeper uncallable-preview MRO, the shared update_qty/validate_qty StatusUpdater
        # mechanism) lives in tools.py/erpnext.py's docstrings, never at the transport level.
        c, t = client([(200, {"data": {"name": "IN-2026-00001", "docstatus": 1}})])
        out = c.submit_document(INSTALLATION_NOTE, "IN-2026-00001")
        call = t.calls[0]
        self.assertEqual(
            call["url"],
            "https://erp.example.com/api/resource/Installation%20Note/IN-2026-00001")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_shipment_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype — Shipment(Document) overrides
        # neither submit() nor cancel() (shipment.py:14; only on_discard/validate/on_submit/
        # on_cancel hooks + five private helpers), so it rides run_method. Transport shape only;
        # everything genuinely new (the two dynamic-selector pairs, the simplest-MRO uncallable
        # preview, the companyless posture) lives in tools.py/erpnext.py's docstrings, never at
        # the transport level.
        c, t = client([(200, {"data": {"name": "SHIP-2026-00001", "docstatus": 1}})])
        out = c.submit_document(SHIPMENT, "SHIP-2026-00001")
        call = t.calls[0]
        self.assertEqual(
            call["url"],
            "https://erp.example.com/api/resource/Shipment/SHIP-2026-00001")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_sales_forecast_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype — SalesForecast(Document)
        # overrides neither submit() nor cancel() (sales_forecast.py:11; only on_discard is
        # defined at all — neither on_submit nor on_cancel exists). Transport shape only;
        # everything genuinely new (the blank-status finding, the cleanest-yet uncallable preview,
        # the non-submittable MPS cascade edge) lives in tools.py/erpnext.py's docstrings, never at
        # the transport level.
        c, t = client([(200, {"data": {"name": "SF-2026-00001", "docstatus": 1}})])
        out = c.submit_document(SALES_FORECAST, "SF-2026-00001")
        call = t.calls[0]
        self.assertEqual(
            call["url"],
            "https://erp.example.com/api/resource/Sales%20Forecast/SF-2026-00001")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_project_update_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype — ProjectUpdate(Document)
        # overrides neither submit() nor cancel() (project_update.py:9; the class body is a bare
        # "pass" — no on_submit/on_cancel of any kind either). Transport shape only; everything
        # genuinely new (the narrowest list-tier branch, the fourth companyless doctype, the
        # blank-date-on-a-real-field governability finding) lives in tools.py/erpnext.py's
        # docstrings, never at the transport level.
        c, t = client([(200, {"data": {"name": "PU-1", "docstatus": 1}})])
        out = c.submit_document(PROJECT_UPDATE, "PU-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Project%20Update/PU-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_maintenance_visit_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype — MaintenanceVisit(TransactionBase)
        # overrides neither submit() nor cancel() (maintenance_visit.py:12; only on_submit/
        # on_cancel/on_update hooks + private helpers are defined). Transport shape only; the
        # genuinely new findings (the Warranty Claim db_update bypass, the temporal-ordering
        # cancel gate) live in tools.py/erpnext.py's docstrings, never at the transport level.
        c, t = client([(200, {"data": {"name": "MV-1", "docstatus": 1}})])
        out = c.submit_document(MAINTENANCE_VISIT, "MV-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Maintenance%20Visit/MV-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_maintenance_schedule_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype —
        # MaintenanceSchedule(TransactionBase) overrides neither submit() nor cancel()
        # (maintenance_schedule.py:13; only validate/on_submit/on_cancel/on_update/on_trash hooks
        # + private helpers are defined). Transport shape only; the genuinely new findings (the
        # Event auto-creation, the Serial No .save() mutation, the stricter-than-ERPNext blast
        # radius) live in tools.py/erpnext.py's docstrings, never at the transport level.
        c, t = client([(200, {"data": {"name": "MS-1", "docstatus": 1}})])
        out = c.submit_document(MAINTENANCE_SCHEDULE, "MS-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Maintenance%20Schedule/MS-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_asset_maintenance_log_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype —
        # AssetMaintenanceLog(Document) overrides neither submit() nor cancel()
        # (asset_maintenance_log.py:14; only validate/on_submit + one private helper are
        # defined). Transport shape only; the genuinely new findings (the doomed-submit gate, the
        # Task/parent-Asset-Maintenance .save() cascade, the two status-rewrite bypasses) live in
        # tools.py/erpnext.py's docstrings, never at the transport level.
        c, t = client([(200, {"data": {"name": "AML-1", "docstatus": 1}})])
        out = c.submit_document(ASSET_MAINTENANCE_LOG, "AML-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Asset%20Maintenance%20Log/AML-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)

    def test_bank_guarantee_submit_shape(self):
        # Mechanically identical to every non-JE/non-SR doctype — BankGuarantee(Document)
        # overrides neither submit() nor cancel() (bank_guarantee.py:10; only validate() and
        # on_submit() — a hook, not an override — are defined). Transport shape only; the
        # genuinely new finding (the on_submit doomed-submit gate, and the dossier's own
        # submit_via error) lives in tools.py/erpnext.py's docstrings, never at the transport
        # level.
        c, t = client([(200, {"data": {"name": "BG-1", "docstatus": 1}})])
        out = c.submit_document(BANK_GUARANTEE, "BG-1")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Bank%20Guarantee/BG-1")
        self.assertEqual(call["params"], {"run_method": "submit"})
        self.assertEqual(out["docstatus"], 1)


class TestErrors(unittest.TestCase):
    def test_http_error_carries_status_and_server_reason_not_secret(self):
        server_body = {"exc_type": "PermissionError",
                       "_server_messages": json.dumps([json.dumps({"message": "Not permitted"})])}
        c, t = client([(403, server_body)])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_document(SALES_INVOICE, "SI-1")
        self.assertEqual(ctx.exception.status, 403)
        msg = str(ctx.exception)
        self.assertIn("PermissionError", msg)
        self.assertIn("Not permitted", msg)
        self.assertNotIn("SECRET", msg)
        # Transport taxonomy: an int status WITH a parsed frappe JSON body is an ANSWERED
        # refusal — the bench definitely saw and processed the call (release-eligible upstream).
        self.assertTrue(ctx.exception.answered)

    def test_non_json_response_is_an_error(self):
        c, t = client([(200, None)])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_document(SALES_INVOICE, "SI-1")
        # A non-JSON body is proxy-shaped ambiguity, even at a 2xx status — never treated as an
        # answered refusal (there is nothing here that could BE a refusal).
        self.assertFalse(ctx.exception.answered)

    def test_missing_data_key_is_an_error_not_a_none(self):
        c, t = client([(200, {"unexpected": True})])
        with self.assertRaises(ErpnextError):
            c.get_document(SALES_INVOICE, "SI-1")


class TestTransportTaxonomy(unittest.TestCase):
    """The refusal-vs-no-answer taxonomy (docs/plans/2026-07-07-transport-taxonomy.md):
    ``ErpnextError.answered`` is truthy ONLY when an int HTTP status arrived together with a
    parsed JSON body carrying FRAPPE's own error-envelope evidence (``exc_type`` /
    ``_server_messages``), or when the status is one of the pre-processing rejections (429/413)
    regardless of body — everything else (a non-JSON "proxy-shaped" body, a JSON body WITHOUT
    frappe's envelope keys, a connection-level failure) defaults ``answered=False``, so unknowns
    are never mistaken for a bench that actually saw and refused the call."""

    def test_answered_defaults_false(self):
        self.assertFalse(ErpnextError("plain").answered)

    def test_non_2xx_with_json_body_is_answered(self):
        c, t = client([(500, {"exc_type": "ValidationError"})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_document(SALES_INVOICE, "SI-1")
        self.assertEqual(ctx.exception.status, 500)
        self.assertTrue(ctx.exception.answered)

    def test_non_2xx_non_json_body_is_ambiguous_not_answered(self):
        # A proxy-shaped 502/503/504 (HTML/text body, no frappe envelope) — the bench itself may
        # never have seen the request. Status is still recorded (for the message), but answered
        # must stay False: this is exactly the ambiguous class the taxonomy exists to catch.
        c, t = client([(502, None)])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_document(SALES_INVOICE, "SI-1")
        self.assertEqual(ctx.exception.status, 502)
        self.assertFalse(ctx.exception.answered)

    def test_429_is_answered_even_without_a_json_body(self):
        # 429 is always pre-handler (the rate limiter runs before dispatch) — guaranteed no
        # progress, safe to treat as answered wherever emitted, body or not.
        c, t = client([(429, None)])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_document(SALES_INVOICE, "SI-1")
        self.assertEqual(ctx.exception.status, 429)
        self.assertTrue(ctx.exception.answered)

    def test_413_is_answered_even_without_a_json_body(self):
        # 413 trips during body parsing in init_request — the handler never ran either.
        c, t = client([(413, None)])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_document(SALES_INVOICE, "SI-1")
        self.assertEqual(ctx.exception.status, 413)
        self.assertTrue(ctx.exception.answered)

    def test_429_with_a_json_body_is_still_answered(self):
        # Belt-and-suspenders: a 429 that DOES carry a frappe envelope is answered via either rule.
        c, t = client([(429, {"exc_type": "RateLimitExceededError"})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_document(SALES_INVOICE, "SI-1")
        self.assertTrue(ctx.exception.answered)

    def test_json_proxy_error_body_is_NOT_answered(self):
        # THE REDTEAM CATCH: a JSON-speaking proxy (Traefik/ALB/nginx error_page) answers a 502
        # with {"error": "Bad Gateway"} — a dict, but not frappe's. Progress is unknown; treating
        # it as answered would release a consent marker for an act that may have landed. The
        # envelope check (exc_type/_server_messages) is what stands between those two worlds.
        c, t = client([(502, {"error": "Bad Gateway", "message": "upstream connect error"})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_document(SALES_INVOICE, "SI-1")
        self.assertEqual(ctx.exception.status, 502)
        self.assertFalse(ctx.exception.answered)

    def test_json_proxy_503_with_message_key_is_NOT_answered(self):
        # "message" is exactly the key generic proxies use — it must NOT count as frappe evidence.
        c, t = client([(503, {"message": "Service Unavailable"})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_document(SALES_INVOICE, "SI-1")
        self.assertFalse(ctx.exception.answered)

    def test_server_messages_envelope_key_is_answered(self):
        # frappe's other envelope key — _server_messages — is equally positive proof of an answer.
        c, t = client([(417, {"_server_messages": "[\"refused\"]"})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_document(SALES_INVOICE, "SI-1")
        self.assertTrue(ctx.exception.answered)


class TestDefaultTransportConnectionFailures(unittest.TestCase):
    """``default_transport`` is the one place that talks real urllib — these tests monkeypatch
    ``urllib.request.urlopen`` directly (no network) to pin the broadened except clause: any
    ``OSError`` (which subsumes ``urllib.error.URLError`` and the builtin ``TimeoutError``, plus
    raw connection-level failures like ``ConnectionResetError`` that used to escape unconverted)
    becomes ``ErpnextError(status=None)`` — ``answered`` stays at its default, ``False``. An
    ``HTTPError`` (itself an OSError/URLError subclass) must still be handled as an ANSWERED HTTP
    response, never swallowed by the broadened connection-failure catch — order matters."""

    def _client(self):
        from pacioli.erpnext import ErpnextClient
        return ErpnextClient(base_url="https://erp.example.com", api_key="KEY", api_secret="SECRET")

    def test_connection_reset_becomes_no_answer_erpnext_error(self):
        import unittest.mock as mock
        c = self._client()
        with mock.patch("urllib.request.urlopen",
                        side_effect=ConnectionResetError("connection reset by peer")):
            with self.assertRaises(ErpnextError) as ctx:
                c.get_document(SALES_INVOICE, "SI-1")
        self.assertIsNone(ctx.exception.status)
        self.assertFalse(ctx.exception.answered)

    def test_url_error_still_becomes_no_answer_erpnext_error(self):
        # Regression: URLError/TimeoutError, the two classes the except clause already named,
        # must still convert exactly as before now that the clause is broadened to OSError.
        import unittest.mock as mock
        c = self._client()
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("nodename nor servname provided")):
            with self.assertRaises(ErpnextError) as ctx:
                c.get_document(SALES_INVOICE, "SI-1")
        self.assertIsNone(ctx.exception.status)
        self.assertFalse(ctx.exception.answered)

    def test_timeout_error_still_becomes_no_answer_erpnext_error(self):
        import unittest.mock as mock
        c = self._client()
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(ErpnextError) as ctx:
                c.get_document(SALES_INVOICE, "SI-1")
        self.assertIsNone(ctx.exception.status)
        self.assertFalse(ctx.exception.answered)

    def test_http_error_is_not_swallowed_by_the_broadened_catch(self):
        # HTTPError IS an OSError subclass (via URLError) — order matters: it must still be
        # converted to an answered (status, payload) pair by the HTTPError branch, never fall
        # through to the broadened OSError catch as a no-answer connection failure.
        import io
        import unittest.mock as mock
        body = json.dumps({"exc_type": "ValidationError"}).encode("utf-8")
        http_error = urllib.error.HTTPError(
            url="https://erp.example.com/api/resource/Sales%20Invoice/SI-1", code=500,
            msg="Internal Server Error", hdrs=None, fp=io.BytesIO(body))
        c = self._client()
        with mock.patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(ErpnextError) as ctx:
                c.get_document(SALES_INVOICE, "SI-1")
        self.assertEqual(ctx.exception.status, 500)
        self.assertTrue(ctx.exception.answered)  # a parsed JSON body arrived WITH the status


class TestPeriodLocks(unittest.TestCase):
    """Breadth (v16 spine fix): frozen_until reads Company.accounts_frozen_till_date (the v16
    source ERPNext's own check_freezing_date enforces against) FIRST, then Accounts Settings'
    legacy acc_frozen_upto (v15) — the later date wins if both carry a value. PCV is unchanged.

    F-S1 (this increment): get_period_locks now REQUIRES doctype + posting_date (no default —
    F5), and the Accounting Period check is doctype- and date-range-aware: LIST (company, a range
    containing posting_date), then a full-document item GET per hit to read disabled +
    closed_documents (the list endpoint never expands child tables). Call order is unchanged for
    frozen/PCV (Company, Accounts Settings, PCV) — the Accounting Period LIST is 4th, and any
    item GETs follow, one per hit, in list order.

    F-C1 (v15 compatibility): the LIST no longer filters ``disabled`` (v16-only column, absent on
    a v15 bench, and frappe's filter builder has no meta-validation — filtering on it there is an
    unknown-column failure, not "no match"). ``disabled`` is read off the full-document item GET
    instead; absent (the v15 shape) is treated as enabled — see
    ``TestPeriodLocksAccountingPeriodF1NewlyAllowed``/``...V15Compat`` below."""

    DOCTYPE = "Sales Invoice"
    DATE = "2026-07-01"

    def test_locks_read_company_frozen_date_pcv_and_accounting_period(self):
        c, t = client([
            (200, {"data": {"accounts_frozen_till_date": "2026-04-15"}}),
            (200, {"data": {"acc_frozen_upto": ""}}),
            (200, {"data": [{"period_end_date": "2026-06-30"}]}),
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "FY2026-Q3", "closed_documents": [
                {"document_type": "Sales Invoice", "closed": 1}]}}),
        ])
        locks = c.get_period_locks(company="Example Corp", doctype=self.DOCTYPE,
                                   posting_date=self.DATE)
        self.assertEqual(locks, {"frozen_until": "2026-04-15",
                                 "pcv_until": "2026-06-30",
                                 "closed_period_until": "2026-09-30"})

    def test_reads_the_company_doc_for_the_named_company(self):
        c, t = client([
            (200, {"data": {}}), (200, {"data": {}}),
            (200, {"data": []}), (200, {"data": []}),
        ])
        c.get_period_locks(company="Example Corp", doctype=self.DOCTYPE, posting_date=self.DATE)
        self.assertEqual(t.calls[0]["url"],
                         "https://erp.example.com/api/resource/Company/Example%20Corp")

    def test_legacy_acc_frozen_upto_honored_on_an_unmigrated_v15_bench(self):
        # Company has no accounts_frozen_till_date (a v15 bench, or the field simply unset) —
        # the legacy Accounts Settings field is still honored, not silently dropped.
        c, t = client([
            (200, {"data": {}}),
            (200, {"data": {"acc_frozen_upto": "2026-03-31"}}),
            (200, {"data": []}),
            (200, {"data": []}),
        ])
        self.assertEqual(
            c.get_period_locks(company="Example Corp", doctype=self.DOCTYPE,
                              posting_date=self.DATE),
            {"frozen_until": "2026-03-31"})

    def test_when_both_present_the_later_company_date_wins(self):
        c, t = client([
            (200, {"data": {"accounts_frozen_till_date": "2026-05-01"}}),
            (200, {"data": {"acc_frozen_upto": "2026-03-31"}}),
            (200, {"data": []}),
            (200, {"data": []}),
        ])
        locks = c.get_period_locks(company="Example Corp", doctype=self.DOCTYPE,
                                   posting_date=self.DATE)
        self.assertEqual(locks["frozen_until"], "2026-05-01")

    def test_when_both_present_the_later_legacy_date_wins(self):
        c, t = client([
            (200, {"data": {"accounts_frozen_till_date": "2026-02-01"}}),
            (200, {"data": {"acc_frozen_upto": "2026-06-30"}}),
            (200, {"data": []}),
            (200, {"data": []}),
        ])
        locks = c.get_period_locks(company="Example Corp", doctype=self.DOCTYPE,
                                   posting_date=self.DATE)
        self.assertEqual(locks["frozen_until"], "2026-06-30")

    def test_unreadable_company_raises_rather_than_read_as_open(self):
        c, t = client([(403, {"exc_type": "PermissionError"})])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype=self.DOCTYPE,
                               posting_date=self.DATE)

    def test_unreadable_accounts_settings_raises_even_if_company_is_readable(self):
        c, t = client([
            (200, {"data": {"accounts_frozen_till_date": "2026-04-15"}}),
            (403, {"exc_type": "PermissionError"}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype=self.DOCTYPE,
                               posting_date=self.DATE)

    def test_absent_locks_are_absent_not_empty_strings(self):
        c, t = client([
            (200, {"data": {}}),
            (200, {"data": {"acc_frozen_upto": ""}}),
            (200, {"data": []}),
            (200, {"data": []}),
        ])
        self.assertEqual(
            c.get_period_locks(company="Example Corp", doctype=self.DOCTYPE,
                              posting_date=self.DATE),
            {})


# The 4 "unrelated" responses (Company, Accounts Settings, PCV) every Accounting-Period-focused
# test below needs first, before the Accounting Period LIST — kept absent/empty so each test's
# own queue stays focused on what it's actually pinning.
_NO_FROZEN_NO_PCV = [(200, {"data": {}}), (200, {"data": {}}), (200, {"data": []})]


class TestPeriodLocksAccountingPeriodF1NewlyAllowed(unittest.TestCase):
    """F-S1 pin F1: the exact PHASE S P3 shape (and its siblings) that ERPNext itself would ALLOW
    but the pre-F-S1 broker over-refused — each newly-allowed class gets its own test.

    F-C1 update: the "out of date range" and "wrong doctype" cases are still excluded the way
    they always were (the first by the LIST filter itself, the second by the item-GET
    closed_documents read). The disabled case changed shape — F-C1 dropped ``disabled`` from the
    LIST filter (v15 compatibility, see erpnext.py), so a disabled period IS now a real LIST hit;
    it is excluded by the item-GET's own ``disabled`` read instead (see
    ``test_disabled_period_is_now_allowed`` below, which doubles as the F-C1 PHASE-T-preservation
    pin)."""

    def test_posting_dated_before_a_containing_periods_start_is_now_allowed(self):
        # The LIST filter's start_date<=posting_date excludes a period starting AFTER the
        # posting — a real bench returns no rows here, exactly like an ordinary "no period yet".
        c, t = client(_NO_FROZEN_NO_PCV + [(200, {"data": []})])
        locks = c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                                   posting_date="2026-01-15")
        self.assertNotIn("closed_period_until", locks)

    def test_doctype_the_period_does_not_close_is_now_allowed(self):
        # The period DOES contain posting_date and IS enabled (a real LIST hit) but its
        # closed_documents rows close a different doctype only — no match, no lock. `disabled: 0`
        # is explicit here (v16 realism — the field is present and clean on a v16 bench).
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "FY2026-Q3", "disabled": 0, "closed_documents": [
                {"document_type": "Journal Entry", "closed": 1},
                {"document_type": "Sales Invoice", "closed": 0}]}}),
        ])
        locks = c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                                   posting_date="2026-07-15")
        self.assertNotIn("closed_period_until", locks)

    def test_disabled_period_is_now_allowed(self):
        # F-C1: `disabled` is no longer in the LIST filter, so a disabled period IS a real LIST
        # hit now — the item GET must be the thing that excludes it. This is also the PHASE-T
        # preservation pin (F-S1's original "disabled period is allowed" behavior, now proven at
        # the item-GET layer): the full doc's closed_documents WOULD close Sales Invoice for this
        # exact date if the period were enabled, so a lock here would mean the disabled skip
        # never fired — the test only passes if the skip actually ran before closed_documents was
        # ever consulted.
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "FY2026-Q3", "disabled": 1, "closed_documents": [
                {"document_type": "Sales Invoice", "closed": 1}]}}),
        ])
        locks = c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                                   posting_date="2026-07-15")
        self.assertNotIn("closed_period_until", locks)


class TestPeriodLocksAccountingPeriodF2StillRefused(unittest.TestCase):
    """F-S1 pin F2: a posting inside an enabled period that DOES close the doctype is still
    refused — exact-boundary on BOTH ends (== start_date and == end_date), one day outside
    either end allows. `disabled: 0` is explicit in the item-GET fixture below (v16 realism,
    F-C1) — this is also the "v16 enabled period still refuses" pin: an explicit, clean 0 must
    behave identically to the pre-F-C1 shape (which never carried the key at all)."""

    def _closed_period(self, start, end):
        return [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": start, "end_date": end}]}),
            (200, {"data": {"name": "FY2026-Q3", "disabled": 0, "closed_documents": [
                {"document_type": "Sales Invoice", "closed": 1}]}}),
        ]

    def test_refused_exactly_at_period_start_date(self):
        c, t = client(_NO_FROZEN_NO_PCV + self._closed_period("2026-07-01", "2026-09-30"))
        locks = c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                                   posting_date="2026-07-01")
        self.assertEqual(locks["closed_period_until"], "2026-09-30")

    def test_refused_exactly_at_period_end_date(self):
        c, t = client(_NO_FROZEN_NO_PCV + self._closed_period("2026-07-01", "2026-09-30"))
        locks = c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                                   posting_date="2026-09-30")
        self.assertEqual(locks["closed_period_until"], "2026-09-30")

    def test_one_day_before_period_start_allowed(self):
        # Outside the LIST filter's range — a real bench excludes it, same as F1.
        c, t = client(_NO_FROZEN_NO_PCV + [(200, {"data": []})])
        locks = c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                                   posting_date="2026-06-30")
        self.assertNotIn("closed_period_until", locks)

    def test_one_day_after_period_end_allowed(self):
        c, t = client(_NO_FROZEN_NO_PCV + [(200, {"data": []})])
        locks = c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                                   posting_date="2026-10-01")
        self.assertNotIn("closed_period_until", locks)


class TestPeriodLocksAccountingPeriodF3MultiPeriod(unittest.TestCase):
    """F-S1 pin F3: multiple containing periods (same-company overlap is a data-hygiene edge, not
    the normal shape — validate_overlap forbids it on a real bench) — ANY match refuses. And the
    LIST call itself is what keeps a different company's period from ever coming back (F-C1:
    company + date range only now — see the dedicated filter-shape test below, which also guards
    against a `disabled` filter regressing back in)."""

    def test_second_of_two_periods_matching_still_refuses(self):
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "A", "start_date": "2026-07-01", "end_date": "2026-07-31"},
                            {"name": "B", "start_date": "2026-07-01", "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "A", "closed_documents": [
                {"document_type": "Journal Entry", "closed": 1}]}}),   # A: not our doctype
            (200, {"data": {"name": "B", "closed_documents": [
                {"document_type": "Sales Invoice", "closed": 1}]}}),   # B: matches
        ])
        locks = c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                                   posting_date="2026-07-15")
        self.assertEqual(locks["closed_period_until"], "2026-09-30")

    def test_disabled_period_among_multiple_is_skipped_enabled_still_locks(self):
        # F-C1: with `disabled` dropped from the LIST filter, BOTH periods below are real LIST
        # hits (previously the disabled one would never have reached this client at all). Period
        # A closes Sales Invoice but is disabled — must be skipped. Period B is enabled and also
        # closes Sales Invoice — must still lock. Proves the two are independent per-hit, not a
        # single disabled-anywhere-skips-everything shortcut.
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "A", "start_date": "2026-07-01", "end_date": "2026-07-31"},
                            {"name": "B", "start_date": "2026-07-01", "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "A", "disabled": 1, "closed_documents": [
                {"document_type": "Sales Invoice", "closed": 1}]}}),   # A: disabled, skipped
            (200, {"data": {"name": "B", "disabled": 0, "closed_documents": [
                {"document_type": "Sales Invoice", "closed": 1}]}}),   # B: enabled, matches
        ])
        locks = c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                                   posting_date="2026-07-15")
        self.assertEqual(locks["closed_period_until"], "2026-09-30")

    def test_accounting_period_list_filters_by_company_and_range_never_disabled(self):
        # F-C1: `disabled` must NOT be in the LIST filter — that column is v16-only, and
        # filtering on it breaks a v15 bench outright (unknown-column failure). Pin the exact
        # filter shape sent so a regression re-adding it fails here rather than only being
        # noticed against a live v15 bench.
        c, t = client(_NO_FROZEN_NO_PCV + [(200, {"data": []})])
        c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                           posting_date="2026-07-15")
        period_call = t.calls[3]
        self.assertEqual(period_call["url"],
                         "https://erp.example.com/api/resource/Accounting%20Period")
        filters = json.loads(period_call["params"]["filters"])
        self.assertIn(["company", "=", "Example Corp"], filters)
        self.assertIn(["start_date", "<=", "2026-07-15"], filters)
        self.assertIn(["end_date", ">=", "2026-07-15"], filters)
        self.assertEqual(len(filters), 3, f"unexpected extra filter(s): {filters!r}")
        self.assertNotIn(["disabled", "=", 0], filters)
        self.assertFalse(any(f[0] == "disabled" for f in filters),
                         "the LIST must never filter on `disabled` — it is v16-only and breaks "
                         "a v15 bench (F-C1); disabled is read from the item GET instead")

    def test_accounting_period_list_pins_unbounded_limit_page_length(self):
        # F-V1: the AP LIST is a gate-feeding read (it decides closed-books allow/deny) and sent
        # no `limit_page_length` at all — frappe's v1 REST defaults an omitted limit to 20 rows
        # with no truncation signal, so a company with more than 20 matching periods could have
        # an enabled closing period past row 20 silently missed, allowing a posting that should
        # have been refused. Every sibling gate-feeding read already pins a limit explicitly
        # (find_amendments/get_active_workflows pin "0" = unbounded; the PCV read deliberately
        # pins "1") — this LIST must pin "0" too, the same as the other unbounded reads.
        c, t = client(_NO_FROZEN_NO_PCV + [(200, {"data": []})])
        c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                           posting_date="2026-07-15")
        period_call = t.calls[3]
        self.assertEqual(period_call["url"],
                         "https://erp.example.com/api/resource/Accounting%20Period")
        self.assertEqual(period_call["params"]["limit_page_length"], "0")


class TestPeriodLocksAccountingPeriodFC1V15Compat(unittest.TestCase):
    """F-C1: the arm-free v15 proof. A v15 bench's ``Accounting Period`` full document simply has
    NO ``disabled`` key at all (the column doesn't exist on that schema) — this must not raise,
    and the period's ``closed_documents`` must still be evaluated normally (absent ``disabled`` ==
    enabled, the correct v15 reading, not a "give up and allow" shortcut)."""

    def test_v15_shape_no_disabled_key_still_locks_when_it_closes_the_doctype(self):
        # The full-document response below is the exact v15 shape: no `disabled` key anywhere.
        # It closes Sales Invoice for this date — the lock must still fire, proving the read
        # doesn't quietly stop evaluating closed_documents just because `disabled` is missing.
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "FY2026-Q3", "closed_documents": [
                {"document_type": "Sales Invoice", "closed": 1}]}}),   # no "disabled" key at all
        ])
        locks = c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                                   posting_date="2026-07-15")
        self.assertEqual(locks["closed_period_until"], "2026-09-30")

    def test_v15_shape_no_disabled_key_allows_when_it_does_not_close_the_doctype(self):
        # Same v15 shape (no `disabled` key), but this period doesn't close Sales Invoice — no
        # lock, no raise. Confirms "absent == enabled" doesn't also mean "absent == locked"; the
        # two axes (enabled vs. closes-this-doctype) stay independent.
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "FY2026-Q3", "closed_documents": [
                {"document_type": "Journal Entry", "closed": 1}]}}),   # no "disabled" key
        ])
        locks = c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                                   posting_date="2026-07-15")
        self.assertNotIn("closed_period_until", locks)


class TestPeriodLocksAccountingPeriodF4DenyBias(unittest.TestCase):
    """F-S1 pin F4: an unreadable item GET denies the act; malformed period/child data denies;
    non-ISO dates deny. An unverifiable lock must refuse — never skip-and-allow."""

    def test_null_list_body_denies_not_typeerror(self):
        # {"data": null} is valid JSON the transport layer accepts — the lock read must turn it
        # into the structured deny (ErpnextError), never a bare TypeError out of the period loop.
        c, t = client(_NO_FROZEN_NO_PCV[:2] + [(200, {"data": None})])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                               posting_date="2026-07-15")

    def test_unreadable_item_get_denies(self):
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "2026-09-30"}]}),
            (403, {"exc_type": "PermissionError"}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                               posting_date="2026-07-15")

    def test_malformed_child_row_missing_document_type_denies(self):
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "FY2026-Q3", "closed_documents": [{"closed": 1}]}}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                               posting_date="2026-07-15")

    def test_unparseable_closed_value_denies(self):
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "FY2026-Q3", "closed_documents": [
                {"document_type": "Sales Invoice", "closed": "yes"}]}}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                               posting_date="2026-07-15")

    def test_unparseable_disabled_value_denies(self):
        # F-C1: `disabled` present but not a clean 0/1/bool/None — the judgment call flagged in
        # erpnext.py's docstring: raise rather than coerce either direction, never let a garbage
        # value silently read as "enabled" (which would then let closed_documents fire as normal
        # on an assumption) or "disabled" (which would silently unlock a period that closes this
        # doctype). Never reached before F-C1 (disabled wasn't read from the item GET at all).
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "FY2026-Q3", "disabled": "nope", "closed_documents": [
                {"document_type": "Sales Invoice", "closed": 1}]}}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                               posting_date="2026-07-15")

    def test_missing_closed_key_denies(self):
        # A full-doc GET should always carry the closed field (a Check), but treat its absence
        # any other unparseable value — deny, never assume "not closed".
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "FY2026-Q3", "closed_documents": [
                {"document_type": "Sales Invoice"}]}}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                               posting_date="2026-07-15")

    def test_closed_documents_not_a_list_denies(self):
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "FY2026-Q3", "closed_documents": "not-a-list"}}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                               posting_date="2026-07-15")

    def test_non_iso_start_date_on_a_hit_denies(self):
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-7-1",
                            "end_date": "2026-09-30"}]}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                               posting_date="2026-07-15")

    def test_non_iso_end_date_on_a_hit_denies(self):
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "FY2026-Q3", "start_date": "2026-07-01",
                            "end_date": "not-a-date"}]}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                               posting_date="2026-07-15")

    def test_malformed_posting_date_denies_before_any_network_call(self):
        c, t = client([])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                               posting_date="07/15/2026")
        self.assertEqual(t.calls, [])  # refused before spending a single round-trip

    def test_list_row_unreadable_item_get_leaves_no_partial_lock(self):
        # A malformed row further down the list must still deny even if an earlier row already
        # matched — validation never short-circuits on "we already found a match".
        c, t = client(_NO_FROZEN_NO_PCV + [
            (200, {"data": [{"name": "A", "start_date": "2026-07-01", "end_date": "2026-09-30"},
                            {"name": "B", "start_date": "2026-07-01", "end_date": "2026-09-30"}]}),
            (200, {"data": {"name": "A", "closed_documents": [
                {"document_type": "Sales Invoice", "closed": 1}]}}),
            (200, {"data": {"name": "B", "closed_documents": [{"closed": 1}]}}),  # malformed
        ])
        with self.assertRaises(ErpnextError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice",
                               posting_date="2026-07-15")


class TestPeriodLocksRequiresDoctypeAndPostingDate(unittest.TestCase):
    """F-S1 pin F5: no silent doctype-blind call is possible — both new params are REQUIRED
    (no default), so an old-shape call is a TypeError at build time, never a doctype-blind read
    at run time."""

    def test_missing_doctype_and_posting_date_raises_typeerror(self):
        c, t = client([])
        with self.assertRaises(TypeError):
            c.get_period_locks(company="Example Corp")

    def test_missing_posting_date_only_raises_typeerror(self):
        c, t = client([])
        with self.assertRaises(TypeError):
            c.get_period_locks(company="Example Corp", doctype="Sales Invoice")


if __name__ == "__main__":
    unittest.main()


class TestCancelShape(unittest.TestCase):
    def test_cancel_rides_the_item_doc_method_surface(self):
        # Same guard-scopeable shape as submit: POST to the ITEM url with run_method=cancel in the
        # QUERY STRING (classifies as "Sales Invoice.cancel"); no body, no adv_adj, no posting_date.
        c, t = client([(200, {"data": {"name": "SI-9", "docstatus": 2}})])
        out = c.cancel_document(SALES_INVOICE, "SI-9")
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://erp.example.com/api/resource/Sales%20Invoice/SI-9")
        self.assertEqual(call["params"], {"run_method": "cancel"})
        self.assertIsNone(call["body"])
        self.assertEqual(out["docstatus"], 2)

    def test_cancel_never_sends_the_lock_levers(self):
        c, t = client([(200, {"data": {}})])
        c.cancel_document(SALES_INVOICE, "SI-9")
        flat = json.dumps(t.calls[0]["params"]) + json.dumps(t.calls[0]["body"])
        self.assertNotIn("adv_adj", flat)
        self.assertNotIn("posting_date", flat)

    def test_purchase_invoice_cancel_shape(self):
        c, t = client([(200, {"data": {"name": "PINV-9", "docstatus": 2}})])
        out = c.cancel_document(PURCHASE_INVOICE, "PINV-9")
        call = t.calls[0]
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Purchase%20Invoice/PINV-9")
        self.assertEqual(call["params"], {"run_method": "cancel"})
        self.assertEqual(out["docstatus"], 2)

    def test_journal_entry_cancel_rides_frappe_client_cancel(self):
        # cancel's doctype is a PLAIN SIBLING param (frappe.client.cancel(doctype, name) loads the
        # doc fresh from the DB itself) — unlike submit, no doc body is needed at all.
        c, t = client([(200, {"message": {"name": "JE-9", "docstatus": 2}})])
        out = c.cancel_document(JOURNAL_ENTRY, "JE-9")
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://erp.example.com/api/method/frappe.client.cancel")
        self.assertEqual(call["body"], {"doctype": "Journal Entry", "name": "JE-9"})
        self.assertIsNone(call["params"])
        self.assertEqual(out["docstatus"], 2)

    def test_journal_entry_cancel_missing_message_envelope_is_an_error(self):
        c, t = client([(200, {"unexpected": True})])
        with self.assertRaises(ErpnextError):
            c.cancel_document(JOURNAL_ENTRY, "JE-9")

    def test_stock_reconciliation_cancel_rides_frappe_client_cancel(self):
        # The SECOND doctype ever to need this transport (Stock Reconciliation breadth,
        # 2026-07-21) — proving it generalizes beyond the one doctype it was built for. cancel's
        # doctype is a PLAIN SIBLING param (frappe.client.cancel(doctype, name) loads the doc
        # fresh from the DB itself) — unlike submit, no doc body is needed at all.
        c, t = client([(200, {"message": {"name": "SR-9", "docstatus": 2}})])
        out = c.cancel_document(STOCK_RECONCILIATION, "SR-9")
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://erp.example.com/api/method/frappe.client.cancel")
        self.assertEqual(call["body"], {"doctype": "Stock Reconciliation", "name": "SR-9"})
        self.assertIsNone(call["params"])
        self.assertEqual(out["docstatus"], 2)


class TestLinkedDocsShape(unittest.TestCase):
    def test_linked_docs_call_and_parse(self):
        c, t = client([(200, {"message": {"count": 1,
                                          "docs": [{"doctype": "Payment Entry", "name": "PE-1"}]}})])
        docs = c.get_submitted_linked_docs("Sales Invoice", "SI-9")
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("frappe.desk.form.linked_with.get_submitted_linked_docs", call["url"])
        self.assertEqual(call["body"], {"doctype": "Sales Invoice", "name": "SI-9"})
        self.assertEqual(docs, [{"doctype": "Payment Entry", "name": "PE-1"}])

    def test_empty_and_null_graphs_parse_as_empty(self):
        c, _ = client([(200, {"message": {"count": 0, "docs": []}})])
        self.assertEqual(c.get_submitted_linked_docs("Sales Invoice", "SI-9"), [])
        c2, _ = client([(200, {"message": None})])
        self.assertEqual(c2.get_submitted_linked_docs("Sales Invoice", "SI-9"), [])

    def test_missing_envelope_raises_never_reads_as_empty(self):
        c, _ = client([(200, {"unexpected": True})])
        with self.assertRaises(ErpnextError):
            c.get_submitted_linked_docs("Sales Invoice", "SI-9")

    def test_malformed_dict_message_raises_never_reads_as_empty(self):
        # A dict message whose `docs` is null, absent (even while `count` says there ARE links),
        # or not a list, is an UNREADABLE graph — not a leaf. It must refuse, never silently read
        # as an empty blast radius (the docstring's deny-bias promise; a leaf-read here would let a
        # non-leaf cancel through as if it had no dependents).
        for body in ({"message": {"docs": None}},
                     {"message": {"count": 3}},
                     {"message": {"docs": "notalist"}}):
            c, _ = client([(200, body)])
            with self.assertRaises(ErpnextError):
                c.get_submitted_linked_docs("Sales Invoice", "SI-9")

    def test_purchase_invoice_linked_docs(self):
        c, t = client([(200, {"message": {"count": 0, "docs": []}})])
        c.get_submitted_linked_docs(PURCHASE_INVOICE, "PINV-9")
        self.assertEqual(t.calls[0]["body"], {"doctype": "Purchase Invoice", "name": "PINV-9"})

    def test_journal_entry_linked_docs(self):
        c, t = client([(200, {"message": {"count": 0, "docs": []}})])
        c.get_submitted_linked_docs(JOURNAL_ENTRY, "JE-9")
        self.assertEqual(t.calls[0]["body"], {"doctype": "Journal Entry", "name": "JE-9"})


class TestAmendShape(unittest.TestCase):
    SOURCE = {
        "name": "SI-9", "doctype": "Sales Invoice", "docstatus": 2, "status": "Cancelled",
        "owner": "x", "creation": "c", "modified": "m", "modified_by": "x",
        "company": "Example Corp", "customer": "ACME", "grand_total": 250.0,
        "items": [{"name": "row1", "parent": "SI-9", "parentfield": "items",
                   "parenttype": "Sales Invoice", "docstatus": 2, "idx": 1,
                   "item_code": "WIDGET", "qty": 5.0, "rate": 50.0}],
    }

    PI_SOURCE = {
        "name": "PINV-9", "doctype": "Purchase Invoice", "docstatus": 2, "status": "Cancelled",
        "owner": "x", "creation": "c", "modified": "m", "modified_by": "x",
        "company": "Example Corp", "supplier": "ACME Supply", "grand_total": 500.0,
        "items": [{"name": "row1", "parent": "PINV-9", "parentfield": "items",
                   "parenttype": "Purchase Invoice", "docstatus": 2, "idx": 1,
                   "item_code": "WIDGET", "qty": 5.0, "rate": 100.0}],
    }

    def test_create_rides_the_collection_create_with_the_pure_payload(self):
        from pacioli.amend import amend_payload
        c, t = client([(200, {"data": {"name": "SI-9-1", "docstatus": 0,
                                       "amended_from": "SI-9"}})])
        out = c.create_amended_draft(SALES_INVOICE, self.SOURCE)
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        # The COLLECTION url (resource CREATE) — not an item url, no run_method.
        self.assertEqual(call["url"], "https://erp.example.com/api/resource/Sales%20Invoice")
        self.assertIsNone(call["params"])
        # The body is EXACTLY what the pure core builds — never the raw source document.
        self.assertEqual(call["body"], amend_payload(self.SOURCE))
        self.assertEqual(call["body"]["amended_from"], "SI-9")
        self.assertEqual(call["body"]["docstatus"], 0)
        self.assertNotIn("name", call["body"])
        self.assertNotIn("parent", call["body"]["items"][0])
        self.assertEqual(out["docstatus"], 0)

    def test_an_uncancelled_source_is_refused_before_any_request(self):
        c, t = client()
        with self.assertRaises(ErpnextError):
            c.create_amended_draft(SALES_INVOICE, dict(self.SOURCE, docstatus=1))
        self.assertEqual(t.calls, [])

    def test_amendment_search_covers_any_docstatus(self):
        c, t = client([(200, {"data": [{"name": "SI-9-1", "docstatus": 0}]})])
        rows = c.find_amendments(SALES_INVOICE, "SI-9")
        call = t.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertIn("/api/resource/Sales%20Invoice", call["url"])
        filters = json.loads(call["params"]["filters"])
        self.assertEqual(filters, [["amended_from", "=", "SI-9"]])  # deliberately NO docstatus
        self.assertEqual(call["params"]["limit_page_length"], "0")
        self.assertEqual(rows, [{"name": "SI-9-1", "docstatus": 0}])

    def test_amendment_search_null_data_raises_never_reads_as_no_amendments(self):
        # A null/non-list `data` is an unreadable search, not proof of zero amendments — it must
        # refuse, never read as "no amendments" (which would let a second amend draft be created).
        for body in ({"data": None}, {"data": {"name": "SI-9-1"}}):
            c, _ = client([(200, body)])
            with self.assertRaises(ErpnextError):
                c.find_amendments(SALES_INVOICE, "SI-9")

    def test_get_doc_for_amend_is_the_permission_scoped_item_get(self):
        c, t = client([(200, {"data": {"name": "SI-9", "docstatus": 2}})])
        doc = c.get_doc_for_amend(SALES_INVOICE, "SI-9")
        self.assertEqual(t.calls[0]["method"], "GET")
        self.assertEqual(t.calls[0]["url"],
                         "https://erp.example.com/api/resource/Sales%20Invoice/SI-9")
        self.assertEqual(doc["docstatus"], 2)

    def test_purchase_invoice_amend_rides_the_pi_collection_create(self):
        from pacioli.amend import amend_payload
        c, t = client([(200, {"data": {"name": "PINV-9-1", "docstatus": 0,
                                       "amended_from": "PINV-9"}})])
        out = c.create_amended_draft(PURCHASE_INVOICE, self.PI_SOURCE)
        call = t.calls[0]
        self.assertEqual(call["url"], "https://erp.example.com/api/resource/Purchase%20Invoice")
        self.assertEqual(call["body"], amend_payload(self.PI_SOURCE))
        self.assertEqual(call["body"]["amended_from"], "PINV-9")
        self.assertEqual(out["docstatus"], 0)

    def test_purchase_invoice_find_amendments(self):
        c, t = client([(200, {"data": []})])
        c.find_amendments(PURCHASE_INVOICE, "PINV-9")
        self.assertIn("/api/resource/Purchase%20Invoice", t.calls[0]["url"])

    def test_purchase_invoice_get_doc_for_amend(self):
        c, t = client([(200, {"data": {"name": "PINV-9", "docstatus": 2}})])
        c.get_doc_for_amend(PURCHASE_INVOICE, "PINV-9")
        self.assertEqual(t.calls[0]["url"],
                         "https://erp.example.com/api/resource/Purchase%20Invoice/PINV-9")

    JE_SOURCE = {
        "name": "JE-9", "doctype": "Journal Entry", "docstatus": 2, "voucher_type": "Journal Entry",
        "owner": "x", "creation": "c", "modified": "m", "modified_by": "x",
        "company": "Example Corp", "total_debit": 100.0, "total_credit": 100.0,
        "accounts": [{"name": "row1", "parent": "JE-9", "parentfield": "accounts",
                     "parenttype": "Journal Entry", "docstatus": 2, "idx": 1,
                     "account": "Cash", "debit": 100.0, "credit": 0.0},
                    {"name": "row2", "parent": "JE-9", "parentfield": "accounts",
                     "parenttype": "Journal Entry", "docstatus": 2, "idx": 2,
                     "account": "Sales", "debit": 0.0, "credit": 100.0}],
    }

    def test_journal_entry_amend_rides_the_je_collection_create(self):
        from pacioli.amend import amend_payload
        c, t = client([(200, {"data": {"name": "JE-9-1", "docstatus": 0,
                                       "amended_from": "JE-9"}})])
        out = c.create_amended_draft(JOURNAL_ENTRY, self.JE_SOURCE)
        call = t.calls[0]
        self.assertEqual(call["url"], "https://erp.example.com/api/resource/Journal%20Entry")
        self.assertEqual(call["body"], amend_payload(self.JE_SOURCE))
        self.assertEqual(call["body"]["amended_from"], "JE-9")
        self.assertEqual(out["docstatus"], 0)

    def test_journal_entry_find_amendments(self):
        c, t = client([(200, {"data": []})])
        c.find_amendments(JOURNAL_ENTRY, "JE-9")
        self.assertIn("/api/resource/Journal%20Entry", t.calls[0]["url"])

    def test_journal_entry_get_doc_for_amend(self):
        c, t = client([(200, {"data": {"name": "JE-9", "docstatus": 2}})])
        c.get_doc_for_amend(JOURNAL_ENTRY, "JE-9")
        self.assertEqual(t.calls[0]["url"],
                         "https://erp.example.com/api/resource/Journal%20Entry/JE-9")


class TestGlEntriesShape(unittest.TestCase):
    def test_reads_only_uncancelled_rows_for_the_voucher(self):
        rows = [{"account": "Debtors", "debit": 250.0, "credit": 0.0}]
        c, t = client([(200, {"data": rows})])
        out = c.get_gl_entries(SALES_INVOICE, "SI-9")
        call = t.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertIn("/api/resource/GL%20Entry", call["url"])
        filters = json.loads(call["params"]["filters"])
        self.assertIn(["voucher_no", "=", "SI-9"], filters)
        self.assertIn(["is_cancelled", "=", 0], filters)
        self.assertEqual(out, rows)

    def test_filters_on_voucher_type_too(self):
        # The latent cross-doctype gap this increment closes: once Sales Invoice AND Purchase
        # Invoice share a GL Entry table, filtering on voucher_no alone could surface another
        # doctype's rows if names ever collided. voucher_type pins it to the right doctype.
        c, t = client([(200, {"data": []})])
        c.get_gl_entries(PURCHASE_INVOICE, "PINV-9")
        filters = json.loads(t.calls[0]["params"]["filters"])
        self.assertIn(["voucher_type", "=", "Purchase Invoice"], filters)
        self.assertIn(["voucher_no", "=", "PINV-9"], filters)

    def test_journal_entry_voucher_type_filter(self):
        c, t = client([(200, {"data": []})])
        c.get_gl_entries(JOURNAL_ENTRY, "JE-9")
        filters = json.loads(t.calls[0]["params"]["filters"])
        self.assertIn(["voucher_type", "=", "Journal Entry"], filters)
        self.assertIn(["voucher_no", "=", "JE-9"], filters)

    def test_field_list_includes_against_voucher_for_legibility(self):
        # Payment Entry breadth (scout-pe.md §4): a cancel's projected reversal must show which
        # invoice each GL row is against — a single Payment Entry cancel can touch N invoices at
        # once, unlike SI/PI's single-document blast radius. A plain field-list addition, applies
        # to every doctype's read (SI/PI included), not a doctype-conditional branch.
        c, t = client([(200, {"data": []})])
        c.get_gl_entries(SALES_INVOICE, "SI-9")
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertIn("against_voucher_type", fields)
        self.assertIn("against_voucher", fields)

    def test_full_nine_field_list_pinned(self):
        # The reconciliation-audit residual (21b7f84, "get_gl_entries 2-of-9 field pinning"): only
        # against_voucher_type/against_voucher were pinned above — a regression dropping any of
        # the other 7 requested fields (posting_date/account/debit/credit/against/party_type/
        # party) had NO test coverage. Pin the exact, complete field list, closing that gap.
        c, t = client([(200, {"data": []})])
        c.get_gl_entries(SALES_INVOICE, "SI-9")
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertEqual(fields, ["posting_date", "account", "debit", "credit",
                                  "against", "party_type", "party",
                                  "against_voucher_type", "against_voucher"])

    def test_non_list_body_raises_the_structured_deny(self):
        # House pattern (get_period_locks' Accounting Period LIST guard, get_settling_references'
        # Payment Ledger Entry LIST guard): a "data": null body is valid JSON the transport layer
        # accepts, but is as unverifiable as an unreadable response — must raise, never hand a
        # non-list through to the caller's projected-reversal disclosure.
        c, t = client([(200, {"data": None})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_malformed_row_raises_the_structured_deny_not_attribute_error(self):
        # A list whose row is not an object would otherwise reach a caller's row.get(...)
        # disclosure loop and crash with a raw AttributeError, outside dispatch's structured-deny
        # catch. Same per-row guard get_period_locks/get_settling_references already apply.
        c, t = client([(200, {"data": ["not-a-dict"]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_gl_entries(SALES_INVOICE, "SI-9")
        self.assertIn("malformed", str(ctx.exception))

    # --- load-bearing field validation: account/debit/credit are the row's actual accounting
    # content (WHICH account, HOW MUCH debited/credited) — the projected reversal a human consents
    # to and a cascade accumulates into plan.projected_gl. A malformed value here must refuse,
    # never silently reach the disclosure (or a future summing consumer) as if it were zero/blank.
    def test_missing_account_raises(self):
        rows = [{"debit": 100.0, "credit": 0.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_gl_entries(SALES_INVOICE, "SI-9")
        self.assertIn("account", str(ctx.exception))

    def test_null_account_raises(self):
        rows = [{"account": None, "debit": 100.0, "credit": 0.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_blank_account_raises(self):
        rows = [{"account": "   ", "debit": 100.0, "credit": 0.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_wrong_type_account_raises(self):
        rows = [{"account": 12345, "debit": 100.0, "credit": 0.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_missing_debit_raises(self):
        rows = [{"account": "Debtors", "credit": 0.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_gl_entries(SALES_INVOICE, "SI-9")
        self.assertIn("debit", str(ctx.exception))

    def test_null_debit_raises(self):
        rows = [{"account": "Debtors", "debit": None, "credit": 0.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_string_debit_raises(self):
        # A wrong-typed amount ("100.00" instead of 100.0) must never be silently summed/compared
        # downstream as if it were numeric — refuse it here, at the seam, instead.
        rows = [{"account": "Debtors", "debit": "100.00", "credit": 0.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_nan_debit_raises(self):
        # math.isfinite NaN-defense, the same class check_allocation/consent/prove already apply:
        # a NaN slips past naive comparisons silently, so it must be caught explicitly here.
        rows = [{"account": "Debtors", "debit": float("nan"), "credit": 0.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_infinite_debit_raises(self):
        rows = [{"account": "Debtors", "debit": float("inf"), "credit": 0.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_bool_debit_raises(self):
        # bool is an int subclass in Python — must be explicitly excluded, the same guard
        # check_allocation/consent already apply to their own numeric fields.
        rows = [{"account": "Debtors", "debit": True, "credit": 0.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_missing_credit_raises(self):
        rows = [{"account": "Debtors", "debit": 100.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_gl_entries(SALES_INVOICE, "SI-9")
        self.assertIn("credit", str(ctx.exception))

    def test_null_credit_raises(self):
        rows = [{"account": "Debtors", "debit": 100.0, "credit": None}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_string_credit_raises(self):
        rows = [{"account": "Debtors", "debit": 0.0, "credit": "100.00"}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_nan_credit_raises(self):
        rows = [{"account": "Debtors", "debit": 0.0, "credit": float("nan")}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_zero_debit_and_credit_are_valid_not_missing(self):
        # 0.0 is the ordinary value for the unused side of a row (a pure-credit row has debit=0.0)
        # — falsy but perfectly well-formed. Must NOT be treated as "missing".
        rows = [{"account": "Debtors", "debit": 0.0, "credit": 250.0}]
        c, _ = client([(200, {"data": rows})])
        out = c.get_gl_entries(SALES_INVOICE, "SI-9")
        self.assertEqual(out, rows)

    def test_int_debit_and_credit_are_valid(self):
        # An int amount (not a float) is a legitimate finite number, not a "wrong type".
        rows = [{"account": "Debtors", "debit": 100, "credit": 0}]
        c, _ = client([(200, {"data": rows})])
        out = c.get_gl_entries(SALES_INVOICE, "SI-9")
        self.assertEqual(out, rows)

    def test_multiple_rows_second_row_malformed_still_raises(self):
        # The whole read refuses even when only ONE row (not the first) is malformed — never a
        # partial pass-through of "the rows I could verify".
        rows = [{"account": "Debtors", "debit": 250.0, "credit": 0.0},
                {"account": None, "debit": 0.0, "credit": 250.0}]
        c, _ = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.get_gl_entries(SALES_INVOICE, "SI-9")

    def test_optional_disclosure_fields_tolerate_missing_or_null(self):
        # posting_date/against/party_type/party/against_voucher_type/against_voucher are legitimate
        # blanks on many real GL Entry rows (a Cash-account row typically carries no party; only a
        # row settling another voucher carries against_voucher/against_voucher_type at all). These
        # are disclosure-only metadata, never validated as load-bearing — an absent/null value here
        # is a real, common, VALID shape, pinned as intentional rather than an invented refusal.
        rows = [{"account": "Debtors", "debit": 250.0, "credit": 0.0,
                "posting_date": None, "against": None, "party_type": None, "party": None,
                "against_voucher_type": None, "against_voucher": None}]
        c, _ = client([(200, {"data": rows})])
        out = c.get_gl_entries(SALES_INVOICE, "SI-9")
        self.assertEqual(out, rows)


class TestActiveWorkflowsShape(unittest.TestCase):
    def test_lists_then_fetches_each_full_workflow_doc(self):
        c, t = client([
            (200, {"data": [{"name": "SI Approval"}]}),
            (200, {"data": {"name": "SI Approval", "document_type": "Sales Invoice",
                            "is_active": 1, "workflow_state_field": "workflow_state",
                            "states": [], "transitions": []}}),
        ])
        out = c.get_active_workflows("Sales Invoice")
        list_call, doc_call = t.calls
        self.assertEqual(list_call["method"], "GET")
        self.assertIn("/api/resource/Workflow", list_call["url"])
        filters = json.loads(list_call["params"]["filters"])
        self.assertEqual(filters, [["document_type", "=", "Sales Invoice"], ["is_active", "=", 1]])
        self.assertEqual(list_call["params"]["limit_page_length"], "0")
        self.assertEqual(doc_call["method"], "GET")
        self.assertEqual(doc_call["url"], "https://erp.example.com/api/resource/Workflow/SI%20Approval")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["states"], [])

    def test_no_active_workflows_is_an_empty_list_no_extra_calls(self):
        c, t = client([(200, {"data": []})])
        out = c.get_active_workflows("Sales Invoice")
        self.assertEqual(out, [])
        self.assertEqual(len(t.calls), 1)

    def test_multiple_active_workflows_each_fetched_in_full(self):
        c, t = client([
            (200, {"data": [{"name": "A"}, {"name": "B"}]}),
            (200, {"data": {"name": "A", "states": [], "transitions": []}}),
            (200, {"data": {"name": "B", "states": [], "transitions": []}}),
        ])
        out = c.get_active_workflows("Sales Invoice")
        self.assertEqual([w["name"] for w in out], ["A", "B"])
        self.assertEqual(len(t.calls), 3)

    def test_unreadable_workflow_list_raises_not_reads_as_no_workflow(self):
        c, t = client([(403, {"exc_type": "PermissionError"})])
        with self.assertRaises(ErpnextError):
            c.get_active_workflows("Sales Invoice")

    def test_unreadable_workflow_doc_raises(self):
        c, t = client([
            (200, {"data": [{"name": "SI Approval"}]}),
            (403, {"exc_type": "PermissionError"}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_active_workflows("Sales Invoice")

    def test_malformed_full_doc_body_raises_never_flows_through(self):
        # A null/empty/nameless full-doc body must raise (deny) — passing it downstream would
        # let find_active's single-element branch read garbage as "no workflow" and silently
        # disable the gate.
        for bad_body in ({"data": None}, {"data": {}}, {"data": {"is_active": 1}},
                         {"data": {"name": "   "}}, {"data": "some-string"}):
            c, t = client([
                (200, {"data": [{"name": "SI Approval"}]}),
                (200, bad_body),
            ])
            with self.assertRaises(ErpnextError, msg=repr(bad_body)):
                c.get_active_workflows("Sales Invoice")

    def test_purchase_invoice_active_workflows_shape(self):
        # get_active_workflows was already doctype-generic — pins Purchase Invoice rides it too.
        c, t = client([(200, {"data": []})])
        c.get_active_workflows(PURCHASE_INVOICE)
        filters = json.loads(t.calls[0]["params"]["filters"])
        self.assertEqual(filters, [["document_type", "=", "Purchase Invoice"], ["is_active", "=", 1]])

    def test_journal_entry_active_workflows_shape(self):
        c, t = client([(200, {"data": []})])
        c.get_active_workflows(JOURNAL_ENTRY)
        filters = json.loads(t.calls[0]["params"]["filters"])
        self.assertEqual(filters, [["document_type", "=", "Journal Entry"], ["is_active", "=", 1]])


class TestWorkflowStateShape(unittest.TestCase):
    def test_reads_the_configured_state_field_not_hardcoded(self):
        c, t = client([(200, {"data": {"name": "SI-1", "custom_state": "Pending Approval"}})])
        state = c.get_workflow_state("Sales Invoice", "SI-1", "custom_state")
        call = t.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://erp.example.com/api/resource/Sales%20Invoice/SI-1")
        self.assertEqual(state, "Pending Approval")

    def test_empty_state_value_is_returned_as_is_not_raised(self):
        c, t = client([(200, {"data": {"name": "SI-1", "workflow_state": ""}})])
        state = c.get_workflow_state("Sales Invoice", "SI-1", "workflow_state")
        self.assertEqual(state, "")

    def test_missing_state_field_is_none_not_raised(self):
        c, t = client([(200, {"data": {"name": "SI-1"}})])
        state = c.get_workflow_state("Sales Invoice", "SI-1", "workflow_state")
        self.assertIsNone(state)

    def test_unreadable_doc_raises(self):
        c, t = client([(403, {"exc_type": "PermissionError"})])
        with self.assertRaises(ErpnextError):
            c.get_workflow_state("Sales Invoice", "SI-1", "workflow_state")


class TestApplyWorkflowShape(unittest.TestCase):
    def test_posts_the_whitelisted_rpc_with_doc_and_action_only(self):
        c, t = client([(200, {"message": {"name": "SI-1", "workflow_state": "Pending Approval",
                                          "modified": "2026-07-03 00:00:00.000001"}})])
        out = c.apply_workflow("Sales Invoice", "SI-1", "Submit for Approval")
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/method/frappe.model.workflow.apply_workflow")
        self.assertEqual(call["body"], {"doc": {"doctype": "Sales Invoice", "name": "SI-1"},
                                        "action": "Submit for Approval"})
        self.assertIsNone(call["params"])
        self.assertEqual(out["workflow_state"], "Pending Approval")

    def test_missing_message_envelope_raises(self):
        c, t = client([(200, {"unexpected": True})])
        with self.assertRaises(ErpnextError):
            c.apply_workflow("Sales Invoice", "SI-1", "Submit for Approval")

    def test_workflow_error_maps_417_to_erpnext_error(self):
        server_body = {"exc_type": "WorkflowTransitionError",
                       "_server_messages": json.dumps([json.dumps(
                           {"message": "Self approval is not allowed"})])}
        c, t = client([(417, server_body)])
        with self.assertRaises(ErpnextError) as ctx:
            c.apply_workflow("Sales Invoice", "SI-1", "Approve")
        self.assertEqual(ctx.exception.status, 417)
        self.assertIn("Self approval is not allowed", str(ctx.exception))

    def test_never_sends_extra_fields_beyond_doctype_and_name(self):
        # apply_workflow's server contract (frappe.model.workflow) only reads doctype+name off
        # the doc payload and reloads from db — sending more would be misleading, not functional.
        c, t = client([(200, {"message": {}})])
        c.apply_workflow("Sales Invoice", "SI-1", "Approve")
        self.assertEqual(set(t.calls[0]["body"]["doc"]), {"doctype", "name"})

    def test_purchase_invoice_apply_workflow_shape(self):
        c, t = client([(200, {"message": {}})])
        c.apply_workflow(PURCHASE_INVOICE, "PINV-1", "Approve")
        self.assertEqual(t.calls[0]["body"]["doc"],
                         {"doctype": "Purchase Invoice", "name": "PINV-1"})


class TestAccountsSettingsRead(unittest.TestCase):
    """Journal Entry breadth: a small, doctype-blind read of the site's single Accounts Settings
    doctype for whichever fields the caller names — added for plan_cancel(Journal Entry)'s
    unlink_payment_on_cancellation_of_invoice disclosure (tools.py), but reusable for any future
    Accounts Settings field."""

    def test_reads_named_fields_from_the_singleton(self):
        c, t = client([(200, {"data": {"unlink_payment_on_cancellation_of_invoice": 1}})])
        out = c.get_accounts_settings(["unlink_payment_on_cancellation_of_invoice"])
        call = t.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"],
                         "https://erp.example.com/api/resource/Accounts%20Settings/"
                         "Accounts%20Settings")
        self.assertEqual(json.loads(call["params"]["fields"]),
                         ["unlink_payment_on_cancellation_of_invoice"])
        self.assertEqual(out, {"unlink_payment_on_cancellation_of_invoice": 1})

    def test_unreadable_settings_raises(self):
        c, t = client([(403, {"exc_type": "PermissionError"})])
        with self.assertRaises(ErpnextError):
            c.get_accounts_settings(["unlink_payment_on_cancellation_of_invoice"])


class TestSettlingReferencesShape(unittest.TestCase):
    """F-R1: the settling-PE disclosure read — Payment Ledger Entry rows that settle a target
    document (whatever voucher type — PE most commonly, but the read is doctype-blind against the
    exempt list, scout-verified). GL-entries-shaped: explicit fields/filters, limit_page_length
    "0" (F-V1 law). Request-shape pins only — the live read is proven against a bench separately
    (pin sheet R1-R5)."""

    def test_request_shape_url_method_and_limit(self):
        c, t = client([(200, {"data": []})])
        c.get_settling_references(SALES_INVOICE, "SI-9")
        call = t.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertIn("/api/resource/Payment%20Ledger%20Entry", call["url"])
        self.assertEqual(call["params"]["limit_page_length"], "0")

    def test_field_list(self):
        c, t = client([(200, {"data": []})])
        c.get_settling_references(SALES_INVOICE, "SI-9")
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertEqual(fields, ["voucher_type", "voucher_no", "amount", "account_currency"])

    def test_all_four_filters(self):
        c, t = client([(200, {"data": []})])
        c.get_settling_references(SALES_INVOICE, "SI-9")
        filters = json.loads(t.calls[0]["params"]["filters"])
        self.assertIn(["against_voucher_type", "=", "Sales Invoice"], filters)
        self.assertIn(["against_voucher_no", "=", "SI-9"], filters)
        self.assertIn(["delinked", "=", 0], filters)
        self.assertIn(["voucher_no", "!=", "SI-9"], filters)
        self.assertEqual(len(filters), 4)

    def test_generalizes_to_any_supported_doctype(self):
        c, t = client([(200, {"data": []})])
        c.get_settling_references(JOURNAL_ENTRY, "JE-9")
        filters = json.loads(t.calls[0]["params"]["filters"])
        self.assertIn(["against_voucher_type", "=", "Journal Entry"], filters)
        self.assertIn(["against_voucher_no", "=", "JE-9"], filters)

    def test_returns_the_rows(self):
        rows = [{"voucher_type": "Payment Entry", "voucher_no": "PE-9", "amount": 250.0,
                "account_currency": "USD"}]
        c, t = client([(200, {"data": rows})])
        out = c.get_settling_references(SALES_INVOICE, "SI-9")
        self.assertEqual(out, rows)

    def test_unreadable_raises(self):
        c, t = client([(403, {"exc_type": "PermissionError"})])
        with self.assertRaises(ErpnextError):
            c.get_settling_references(SALES_INVOICE, "SI-9")

    def test_non_list_body_raises_the_structured_deny(self):
        # The house pattern (get_period_locks's own Accounting Period LIST guard): a "data": null
        # body is valid JSON the transport layer accepts, but is as unverifiable as an unreadable
        # response — must raise, never flow a non-list through to the caller's per-row loop.
        c, t = client([(200, {"data": None})])
        with self.assertRaises(ErpnextError):
            c.get_settling_references(SALES_INVOICE, "SI-9")

    def test_malformed_row_raises_the_structured_deny_not_attribute_error(self):
        # Redteam catch: a list whose ROW is not an object would otherwise reach the caller's
        # `row.get(...)` disclosure loop and crash with a raw AttributeError — outside dispatch's
        # structured-deny catch. Same per-row guard get_period_locks already applies.
        c, t = client([(200, {"data": ["not-a-dict"]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_settling_references(SALES_INVOICE, "SI-9")
        self.assertIn("malformed", str(ctx.exception))


class TestReconcileShape(unittest.TestCase):
    """F-R2: the governed reconcile transport — the ONE call the broker makes to settle a pinned
    allocation set. Wire shape LIVE-VERIFIED against a real Frappe v16 bench (P7, 2026-07-09,
    the sealed-lab bench): the ``invoices[]`` pool is REQUIRED (``validate_allocation`` builds its per-invoice
    outstanding map from it — absent, ``invoice_outstanding`` is None and the ceiling check
    TypeErrors, HTTP 500), and the allocation row's ``amount`` AND ``unreconciled_amount`` are BOTH
    the PAYMENT's unallocated (``check_if_advance_entry_modified`` compares
    ``unreconciled_amount`` to the PE's live ``unallocated_amount``; the 0.13.0 shape sent the
    invoice's outstanding there and was refused live: "Payment Entry has been modified").

    ``allocations`` here is the caller-supplied row shape (matching pacioli.reconcile's node/`rows`
    SEMANTIC keys: payment_type/payment_no/invoice_type/invoice_no/allocated_amount/
    payment_unallocated/invoice_outstanding) — the CLIENT method itself does not care whether the
    caller sourced it from a pinned plan graph or not; that discipline lives one layer up, in
    tools.py's ``_tool_reconcile``. The semantic->wire field translation happens ONLY here."""

    def _allocations(self):
        return [{"payment_type": "Payment Entry", "payment_no": "PAY1",
                 "invoice_type": "Sales Invoice", "invoice_no": "INV1",
                 "allocated_amount": 100.0, "payment_unallocated": 500.0,
                 "invoice_outstanding": 100.0}]

    def test_posts_run_doc_method_with_docs_and_method(self):
        c, t = client([(200, {"message": {}})])
        c.reconcile(company="Example Corp", party_type="Customer", party="Cust A",
                   receivable_payable_account="Debtors - EC", allocations=self._allocations())
        call = t.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://erp.example.com/api/method/run_doc_method")
        self.assertEqual(call["body"]["method"], "reconcile")

    def test_docs_carries_the_header_fields(self):
        c, t = client([(200, {"message": {}})])
        c.reconcile(company="Example Corp", party_type="Customer", party="Cust A",
                   receivable_payable_account="Debtors - EC", allocations=self._allocations())
        docs = json.loads(t.calls[0]["body"]["docs"])
        self.assertEqual(docs["doctype"], "Payment Reconciliation")
        self.assertEqual(docs["company"], "Example Corp")
        self.assertEqual(docs["party_type"], "Customer")
        self.assertEqual(docs["party"], "Cust A")
        self.assertEqual(docs["receivable_payable_account"], "Debtors - EC")

    def test_allocation_rows_use_the_source_verified_child_field_names(self):
        # payment_reconciliation_allocation.json's reqd fields: invoice_type/invoice_number/
        # reference_type/reference_name/allocated_amount — NOT the caller-facing
        # invoice_no/payment_type/payment_no names (see reconcile()'s docstring).
        c, t = client([(200, {"message": {}})])
        c.reconcile(company="Example Corp", party_type="Customer", party="Cust A",
                   receivable_payable_account="Debtors - EC", allocations=self._allocations())
        docs = json.loads(t.calls[0]["body"]["docs"])
        self.assertEqual(docs["allocation"], [
            {"invoice_type": "Sales Invoice", "invoice_number": "INV1",
             "reference_type": "Payment Entry", "reference_name": "PAY1",
             "allocated_amount": 100.0, "amount": 500.0, "unreconciled_amount": 500.0},
        ])

    def test_multi_row_allocation_all_present_in_order(self):
        rows = self._allocations() + [
            {"payment_type": "Journal Entry", "payment_no": "JE1",
             "invoice_type": "Purchase Invoice", "invoice_no": "PINV1",
             "allocated_amount": 40.0, "payment_unallocated": 200.0,
             "invoice_outstanding": 80.0},
        ]
        c, t = client([(200, {"message": {}})])
        c.reconcile(company="Example Corp", party_type="Supplier", party="ACME Supply",
                   receivable_payable_account="Creditors - EC", allocations=rows)
        docs = json.loads(t.calls[0]["body"]["docs"])
        self.assertEqual(len(docs["allocation"]), 2)
        self.assertEqual(docs["allocation"][1],
                         {"invoice_type": "Purchase Invoice", "invoice_number": "PINV1",
                          "reference_type": "Journal Entry", "reference_name": "JE1",
                          "allocated_amount": 40.0, "amount": 200.0,
                          "unreconciled_amount": 200.0})

    def test_invoices_pool_present_with_unique_invoice_rows(self):
        # P7 (live-verified): validate_allocation builds unreconciled_invoices from
        # self.get("invoices") — with the pool absent, invoice_outstanding is None and
        # `flt(row.allocated_amount) - invoice_outstanding` TypeErrors (HTTP 500, reproduced
        # 2026-07-09). One pool row per UNIQUE invoice, carrying the plan-time outstanding.
        rows = self._allocations() + [
            {"payment_type": "Payment Entry", "payment_no": "PAY2",
             "invoice_type": "Sales Invoice", "invoice_no": "INV1",
             "allocated_amount": 50.0, "payment_unallocated": 300.0,
             "invoice_outstanding": 100.0},
            {"payment_type": "Payment Entry", "payment_no": "PAY2",
             "invoice_type": "Sales Invoice", "invoice_no": "INV9",
             "allocated_amount": 10.0, "payment_unallocated": 300.0,
             "invoice_outstanding": 40.0},
        ]
        c, t = client([(200, {"message": {}})])
        c.reconcile(company="Example Corp", party_type="Customer", party="Cust A",
                   receivable_payable_account="Debtors - EC", allocations=rows)
        docs = json.loads(t.calls[0]["body"]["docs"])
        self.assertEqual(docs["invoices"], [
            {"invoice_type": "Sales Invoice", "invoice_number": "INV1",
             "outstanding_amount": 100.0},
            {"invoice_type": "Sales Invoice", "invoice_number": "INV9",
             "outstanding_amount": 40.0},
        ])

    def test_payments_pool_not_sent(self):
        # P7 (live-verified): the reconcile write path reads only invoices[] + allocation[];
        # a payments[] pool is NOT required and the broker sends only what the bench proved
        # necessary — nothing untested rides the wire.
        c, t = client([(200, {"message": {}})])
        c.reconcile(company="Example Corp", party_type="Customer", party="Cust A",
                   receivable_payable_account="Debtors - EC", allocations=self._allocations())
        docs = json.loads(t.calls[0]["body"]["docs"])
        self.assertNotIn("payments", docs)

    def test_never_forwards_the_caller_facing_row_shape_directly(self):
        # Redteam-relevant: the wire body must use ERPNext's own child-table field names, never
        # leak the caller-facing semantic keys verbatim into `allocation`.
        c, t = client([(200, {"message": {}})])
        c.reconcile(company="Example Corp", party_type="Customer", party="Cust A",
                   receivable_payable_account="Debtors - EC", allocations=self._allocations())
        docs = json.loads(t.calls[0]["body"]["docs"])
        row = docs["allocation"][0]
        self.assertNotIn("invoice_no", row)
        self.assertNotIn("payment_no", row)
        self.assertNotIn("payment_type", row)
        self.assertNotIn("payment_unallocated", row)
        self.assertNotIn("invoice_outstanding", row)

    def test_amount_and_unreconciled_amount_are_both_the_payments_unallocated(self):
        # P7 (live-verified 2026-07-09): ERPNext's validate_allocation reads row.amount (the
        # payment's available; unset -> 0 -> throws on row 1) AND
        # check_if_advance_entry_modified compares row.unreconciled_amount to the PE's LIVE
        # unallocated_amount (utils.py:645-647, the no-voucher_detail_no branch) — BOTH wire
        # fields are the payment's unallocated. The 0.13.0 shape sent the invoice's outstanding
        # as unreconciled_amount and the live bench refused it ("Payment Entry has been modified
        # after you pulled it"). Entries are processed grouped per voucher with every
        # check BEFORE the group's single save (utils.py reconcile_against_document), so every
        # row carries the plain pre-write value — no running decrement.
        c, t = client([(200, {"message": {}})])
        c.reconcile(company="Example Corp", party_type="Customer", party="Cust A",
                   receivable_payable_account="Debtors - EC", allocations=self._allocations())
        docs = json.loads(t.calls[0]["body"]["docs"])
        row = docs["allocation"][0]
        self.assertEqual(row["amount"], 500.0)
        self.assertEqual(row["unreconciled_amount"], 500.0)

    def test_returns_the_message_envelope_when_present(self):
        c, t = client([(200, {"message": {"name": "new-pr-1"}})])
        out = c.reconcile(company="Example Corp", party_type="Customer", party="Cust A",
                          receivable_payable_account="Debtors - EC",
                          allocations=self._allocations())
        self.assertEqual(out, {"name": "new-pr-1"})

    def test_duck_typed_return_when_no_message_envelope(self):
        # Unlike apply_workflow (which RAISES on a missing "message" key), reconcile's response
        # shape from run_doc_method is BENCH-PENDING (see docstring) — this stays duck-typed
        # rather than asserting an envelope shape that has not been live-verified.
        c, t = client([(200, {"ok": True})])
        out = c.reconcile(company="Example Corp", party_type="Customer", party="Cust A",
                          receivable_payable_account="Debtors - EC",
                          allocations=self._allocations())
        self.assertEqual(out, {"ok": True})

    def test_answered_refusal_raises_erpnext_error_with_answered_true(self):
        server_body = {"exc_type": "ValidationError",
                       "_server_messages": json.dumps([json.dumps(
                           {"message": "Payment already fully allocated"})])}
        c, t = client([(417, server_body)])
        with self.assertRaises(ErpnextError) as ctx:
            c.reconcile(company="Example Corp", party_type="Customer", party="Cust A",
                       receivable_payable_account="Debtors - EC",
                       allocations=self._allocations())
        self.assertTrue(ctx.exception.answered)
        self.assertIn("Payment already fully allocated", str(ctx.exception))
