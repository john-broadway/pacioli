# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Tool-layer tests — the MCP surface, exercised without the MCP SDK.

A fake ERPNext client + real in-memory stores drive the full governed flow the way an MCP client
would: plan_submit in one call, a human mint out-of-band, submit in another call.

Breadth (Purchase Invoice): FakeClient's methods now match the generalized ErpnextClient (see
pacioli/erpnext.py) — get_document/list_documents/submit_document/cancel_document/etc., each
taking an explicit ``doctype``. ``self.docs`` stays a FLAT dict keyed by name only (not
``(doctype, name)``): the fixture names are already globally unique across the fake's universe
(``SI-*`` for Sales Invoice, ``PI-*`` for Purchase Invoice), so doctype partitioning would add
nothing here — the real client partitions by doctype via the URL path itself (test_erpnext.py
pins that). This is the brief's "(or add a parallel PI doc set)" option.
"""
import sqlite3
import unittest

from pacioli.erpnext import (JOURNAL_ENTRY, PAYMENT_ENTRY, PURCHASE_INVOICE, SALES_INVOICE,
                             SUPPORTED_DOCTYPES)
from pacioli.store import BrokerStore
from pacioli.tools import TOOLS, PacioliBroker, tool_names
from pacioli.registry import load_registry


class FakeClient:
    def __init__(self):
        self.docs = {"SI-1": {"name": "SI-1", "docstatus": 0, "company": "Example Corp",
                              "posting_date": "2026-07-01", "grand_total": 100.0,
                              "modified": "2026-07-01 10:00:00.000001"},
                     "SI-9": {"name": "SI-9", "docstatus": 1, "company": "Example Corp",
                              "posting_date": "2026-06-15", "grand_total": 250.0,
                              "modified": "2026-06-15 09:00:00.000001"},
                     "PI-1": {"name": "PI-1", "docstatus": 0, "company": "Example Corp",
                              "supplier": "ACME Supply", "posting_date": "2026-07-01",
                              "grand_total": 80.0,
                              "modified": "2026-07-01 10:00:00.000001"},
                     "PI-9": {"name": "PI-9", "docstatus": 1, "company": "Example Corp",
                              "supplier": "ACME Supply", "posting_date": "2026-06-15",
                              "grand_total": 150.0,
                              "modified": "2026-06-15 09:00:00.000001"},
                     "PE-1": {"name": "PE-1", "docstatus": 0, "company": "Example Corp",
                              "party": "Cust A", "party_type": "Customer",
                              "posting_date": "2026-07-01", "paid_amount": 250.0,
                              "references": [
                                  {"reference_doctype": "Sales Invoice", "reference_name": "SI-9",
                                   "allocated_amount": 250.0, "outstanding_amount": 250.0,
                                   "exchange_gain_loss": 0.0}],
                              "modified": "2026-07-01 10:00:00.000001"},
                     "PE-9": {"name": "PE-9", "docstatus": 1, "company": "Example Corp",
                              "party": "Cust A", "party_type": "Customer",
                              "posting_date": "2026-06-15", "paid_amount": 250.0,
                              "references": [
                                  {"reference_doctype": "Sales Invoice", "reference_name": "SI-9",
                                   "allocated_amount": 250.0, "outstanding_amount": 250.0,
                                   "exchange_gain_loss": 0.0}],
                              "modified": "2026-06-15 09:00:00.000001"},
                     "JE-1": {"name": "JE-1", "docstatus": 0, "company": "Example Corp",
                              "voucher_type": "Journal Entry", "posting_date": "2026-07-01",
                              "total_debit": 100.0, "total_credit": 100.0,
                              "accounts": [{"account": "Cash", "debit": 100.0, "credit": 0.0},
                                          {"account": "Sales", "debit": 0.0, "credit": 100.0}],
                              "modified": "2026-07-01 10:00:00.000001"},
                     "JE-9": {"name": "JE-9", "docstatus": 1, "company": "Example Corp",
                              "voucher_type": "Journal Entry", "posting_date": "2026-06-15",
                              "total_debit": 250.0, "total_credit": 250.0,
                              "accounts": [{"account": "Cash", "debit": 250.0, "credit": 0.0},
                                          {"account": "Sales", "debit": 0.0, "credit": 250.0}],
                              "modified": "2026-06-15 09:00:00.000001"}}
        self.locks = {}
        self.submitted = []
        self.submit_docs = []
        self.cancelled = []
        self.linked = []
        self.accounts_settings = {"unlink_payment_on_cancellation_of_invoice": 0}
        self.fail_accounts_settings = None
        self.accounts_settings_calls = []
        # F-R1: settling-PE disclosure — docname -> [{"voucher_type", "voucher_no", "amount",
        # "account_currency"}, ...], defaulting to no settling rows for every fixture doc (control).
        self.settling_references = {}
        self.fail_settling_references = None
        self.settling_reference_calls = []
        self.gl_rows = [{"posting_date": "2026-06-15", "account": "Debtors", "debit": 250.0,
                         "credit": 0.0},
                        {"posting_date": "2026-06-15", "account": "Sales", "debit": 0.0,
                         "credit": 250.0}]
        self.fail_submit = None
        self.fail_cancel = None
        self.fail_locks = None
        self.fail_linked = None
        self.fail_amend = None
        # Transport taxonomy: a RAW, unconverted exception (never an ErpnextError at all — the
        # exact shape of the pre-fix residual, e.g. a bare OSError that escaped default_transport
        # unconverted) from the mutating call itself, distinct from fail_submit/fail_cancel (which
        # simulate an ANSWERED bench refusal, `answered=True`, and keep releasing the marker).
        self.raise_raw_submit = None
        self.raise_raw_cancel = None
        # A get_document read that fails ONLY during the post-failure readback — NOT the initial
        # pre-execute fetch `_governed_write` already did for the same name. Gated on
        # `_mutation_attempted` (set the moment submit_document/cancel_document is called, success
        # or fail) so only the SUBSEQUENT get_document (the readback) sees the induced failure —
        # pins that a readback failure degrades to `readback_error` rather than crashing the flow.
        self.readback_fails = None
        self._mutation_attempted = False
        self.amended = []
        self.amend_seats = []                  # the seat passed to each create_amended_draft
        self.drop_seat = False                 # bench silently drops the seated field (finding 8)
        self.workflows = []                    # full workflow docs for get_active_workflows
        self.workflow_states = {}              # docname -> current state value
        self.workflow_state_field = "workflow_state"  # where this fake bench keeps the state
        self.fail_workflows = None
        self.fail_workflow_state = None
        self.fail_apply_workflow = None
        self.workflow_calls = []               # tracks get_active_workflows(doctype) calls
        self.state_field_reads = []            # every state_field passed to get_workflow_state
        self.applied_workflow_transitions = []  # [(name, action)]
        self.list_calls = []                   # tracks list_documents(doctype, party_field) calls
        self.gl_calls = []                     # tracks get_gl_entries(voucher_type, voucher_no)
        self.fail_gl_entries = None             # set to raise, mirroring fail_locks/fail_linked
        self.lock_calls = []                   # tracks get_period_locks(company, doctype, date)

    def get_document(self, doctype, name):
        from pacioli.erpnext import ErpnextError
        if self.readback_fails and name == self.readback_fails and self._mutation_attempted:
            raise ErpnextError("HTTP 500: bench hiccup during readback", status=500, answered=True)
        if name not in self.docs:
            raise ErpnextError(f"HTTP 404: {name} not found", status=404, answered=True)
        return dict(self.docs[name])

    def list_documents(self, doctype, filters=None, limit=20, party_field="customer"):
        self.list_calls.append((doctype, party_field))
        return [dict(d) for d in self.docs.values()]

    def ledger_preview(self, company, doctype, docname):
        return {"gl_columns": [], "gl_data": [{"account": "Debtors", "debit": 100.0}]}

    def submit_document(self, doctype, name, doc=None):
        from pacioli.erpnext import ErpnextError
        self._mutation_attempted = True
        if self.fail_submit:
            raise ErpnextError(self.fail_submit, status=500, answered=True)
        if self.raise_raw_submit:
            raise self.raise_raw_submit
        self.submitted.append(name)
        self.submit_docs.append(doc)
        return {"name": name, "docstatus": 1, "modified": "2026-07-01 10:05:00.000001",
                "customer": "ACME", "items": [{"rate": 1.0}]}

    def cancel_document(self, doctype, name):
        from pacioli.erpnext import ErpnextError
        self._mutation_attempted = True
        if self.fail_cancel:
            raise ErpnextError(self.fail_cancel, status=417, answered=True)
        if self.raise_raw_cancel:
            raise self.raise_raw_cancel
        self.cancelled.append(name)
        return {"name": name, "docstatus": 2, "modified": "2026-07-01 11:00:00.000001"}

    def get_submitted_linked_docs(self, doctype, name):
        from pacioli.erpnext import ErpnextError
        if self.fail_linked:
            raise ErpnextError(self.fail_linked, status=500, answered=True)
        return [dict(d) for d in self.linked]

    def get_gl_entries(self, voucher_type, voucher_no):
        from pacioli.erpnext import ErpnextError
        self.gl_calls.append((voucher_type, voucher_no))
        if self.fail_gl_entries:
            raise ErpnextError(self.fail_gl_entries, status=403, answered=True)
        return [dict(r) for r in self.gl_rows]

    def get_period_locks(self, company, doctype, posting_date):
        # F-S1: the fake stays doctype-blind (self.locks is a fixed dict a test sets directly —
        # the real doctype-aware logic lives in erpnext.py, exercised in test_erpnext.py); what
        # this fake DOES verify is that every call site actually threads all three args through,
        # never silently reverting to a 1-arg call (that would be a TypeError against the real
        # client, but this fake would otherwise mask it).
        from pacioli.erpnext import ErpnextError
        self.lock_calls.append((company, doctype, posting_date))
        if self.fail_locks:
            raise ErpnextError(self.fail_locks, status=403, answered=True)
        return dict(self.locks)

    def get_accounts_settings(self, fields):
        from pacioli.erpnext import ErpnextError
        self.accounts_settings_calls.append(tuple(fields))
        if self.fail_accounts_settings:
            raise ErpnextError(self.fail_accounts_settings, status=403, answered=True)
        return {f: self.accounts_settings.get(f) for f in fields}

    def get_settling_references(self, doctype, name):
        # F-R1: docname-keyed, doctype-blind — self.settling_references is {docname: [row, ...]},
        # defaulting to no rows (the control case: unsettled document, zero flags).
        from pacioli.erpnext import ErpnextError
        self.settling_reference_calls.append((doctype, name))
        if self.fail_settling_references:
            raise ErpnextError(self.fail_settling_references, status=403, answered=True)
        return [dict(r) for r in self.settling_references.get(name, [])]

    def get_doc_for_amend(self, doctype, name):
        return self.get_document(doctype, name)

    def find_amendments(self, doctype, name):
        return [{"name": d["name"], "docstatus": d["docstatus"]}
                for d in self.docs.values() if d.get("amended_from") == name]

    def create_amended_draft(self, doctype, source_doc, seat=None):
        from pacioli.erpnext import ErpnextError
        self.amend_seats.append(seat)
        if self.fail_amend:
            raise ErpnextError(self.fail_amend, status=417, answered=True)
        new_name = f"{source_doc['name']}-1"
        doc = {"name": new_name, "docstatus": 0, "company": source_doc.get("company"),
               "posting_date": source_doc.get("posting_date"),
               "grand_total": source_doc.get("grand_total"),
               "amended_from": source_doc["name"],
               "modified": "2026-07-01 12:00:00.000001"}
        if seat is not None and not self.drop_seat:
            field, state = seat
            doc[field] = state
        self.docs[new_name] = doc
        self.amended.append(source_doc["name"])
        return dict(doc)

    def get_active_workflows(self, doctype):
        from pacioli.erpnext import ErpnextError
        self.workflow_calls.append(doctype)
        if self.fail_workflows:
            raise ErpnextError(self.fail_workflows, status=500, answered=True)
        # Non-dicts pass through RAW: the malformed-config e2e tests feed garbage ([{}], [None],
        # ["some-string"]) straight to the broker to prove the gate denies instead of crashing
        # or silently passing. (The REAL client raises on these — that's its own layer's test.)
        return [dict(w) if isinstance(w, dict) else w for w in self.workflows]

    def get_workflow_state(self, doctype, name, state_field):
        from pacioli.erpnext import ErpnextError
        if self.fail_workflow_state:
            raise ErpnextError(self.fail_workflow_state, status=500, answered=True)
        self.state_field_reads.append(state_field)
        # Honor the configured field: a caller asking for the WRONG field finds nothing there —
        # exactly what a real bench doc would return for a field the workflow doesn't use.
        if state_field != self.workflow_state_field:
            return None
        return self.workflow_states.get(name)

    def apply_workflow(self, doctype, name, action):
        from pacioli.erpnext import ErpnextError
        if self.fail_apply_workflow:
            raise ErpnextError(self.fail_apply_workflow, status=417, answered=True)
        self.applied_workflow_transitions.append((name, action))
        return {"name": name, "modified": "2026-07-03 00:00:00.000001",
                self.workflow_state_field: self.workflow_states.get(name, "")}


def sample_workflow(name="SI Approval", states=None, transitions=None):
    """A representative active Sales Invoice Workflow: Draft -> Pending Approval (non-approving,
    self-approvable) -> Approved (approving, role Sales Manager, self-approval off)."""
    return {
        "name": name,
        "document_type": "Sales Invoice",
        "is_active": 1,
        "workflow_state_field": "workflow_state",
        "states": states if states is not None else [
            {"state": "Draft", "doc_status": "0"},
            {"state": "Pending Approval", "doc_status": "0"},
            {"state": "Approved", "doc_status": "1"},
        ],
        "transitions": transitions if transitions is not None else [
            {"state": "Draft", "action": "Submit for Approval", "next_state": "Pending Approval",
             "allowed": "Sales User", "allow_self_approval": "1"},
            {"state": "Pending Approval", "action": "Approve", "next_state": "Approved",
             "allowed": "Sales Manager", "allow_self_approval": "0"},
        ],
    }


REG = '[targets.prod]\nbase_url = "https://erp.example.com"\ncompany = "Example Corp"\n' \
      'api_key = "k"\napi_secret = "env:S"\ndefault = true\n'

# F-C2: a target with NO company pin — the documented unpinned posture (registry.py's `company`
# is optional; unset means "accept any company's document", not "accept none"). Same target name
# ("prod") and shape as REG, company line omitted only, so make_broker(reg=REG_UNPINNED) is a
# drop-in swap for the wrong-books tests that need an unpinned target.
REG_UNPINNED = '[targets.prod]\nbase_url = "https://erp.example.com"\n' \
              'api_key = "k"\napi_secret = "env:S"\ndefault = true\n'


def make_broker(client=None, reg=None):
    client = client or FakeClient()
    stores = {}

    def store_provider(target_name):
        if target_name not in stores:
            stores[target_name] = BrokerStore(sqlite3.connect(":memory:"), key=b"k" * 32)
        return stores[target_name]

    broker = PacioliBroker(
        registry=load_registry(toml_text=reg or REG),
        store_provider=store_provider,
        client_provider=lambda target: client,
        now_epoch=lambda: 1_000.0,
        now_date=lambda: "2026-07-01",
    )
    return broker, client, store_provider


class TestToolSurface(unittest.TestCase):
    def test_thirty_tools_with_schemas(self):
        # F-R2: +2 (plan_reconcile, reconcile) over the prior 28.
        self.assertEqual(len(TOOLS), 30)
        for t in TOOLS:
            self.assertTrue(t["name"])
            self.assertTrue(t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_five_purchase_invoice_siblings_present(self):
        names = set(tool_names())
        for si_name in ("get_sales_invoice", "list_sales_invoices", "submit_sales_invoice",
                        "cancel_sales_invoice", "amend_sales_invoice"):
            pi_name = si_name.replace("sales_invoice", "purchase_invoice")
            self.assertIn(si_name, names)
            self.assertIn(pi_name, names)

    def test_five_payment_entry_siblings_present(self):
        # Payment Entry's plural is irregular ("entries", not "entrys") so this can't reuse the
        # PI sibling test's naive string-replace pattern — spelled out explicitly instead.
        names = set(tool_names())
        for pe_name in ("get_payment_entry", "list_payment_entries", "submit_payment_entry",
                        "cancel_payment_entry", "amend_payment_entry"):
            self.assertIn(pe_name, names)

    def test_five_journal_entry_siblings_present(self):
        # Same irregular-plural reason as Payment Entry.
        names = set(tool_names())
        for je_name in ("get_journal_entry", "list_journal_entries", "submit_journal_entry",
                        "cancel_journal_entry", "amend_journal_entry"):
            self.assertIn(je_name, names)

    def test_generic_doc_scoped_tools_gained_optional_pacioli_doctype(self):
        for name in ("plan_submit", "plan_cancel", "workflow_status",
                     "request_workflow_transition"):
            (schema,) = [t for t in TOOLS if t["name"] == name]
            self.assertIn("pacioli_doctype", schema["inputSchema"]["properties"])
            self.assertNotIn("pacioli_doctype", schema["inputSchema"].get("required", []))

    def test_minting_is_not_a_tool(self):
        for name in tool_names():
            self.assertNotIn("mint", name)
            self.assertNotIn("marker_create", name)

    def test_unknown_tool_refused(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("delete_everything", {})
        self.assertFalse(out["ok"])

    def test_torn_store_on_the_server_path_is_a_structured_deny_not_a_raw_error(self):
        # Redteam (ledger-integrity lens, Gap #3): a StoreCorruptError from open_store during a
        # tool call must surface through dispatch() as the house structured deny (ok:False,
        # stage:store) — the same envelope every other refusal uses — never escape dispatch to be
        # delivered through the MCP SDK's generic, structuredContent-less error channel.
        from pacioli.store import StoreCorruptError

        def torn_provider(target_name):
            raise StoreCorruptError("state db is only 1 bytes — smaller than a valid SQLite header")

        broker = PacioliBroker(
            registry=load_registry(toml_text=REG),
            store_provider=torn_provider,
            client_provider=lambda target: FakeClient(),
            now_epoch=lambda: 1_000.0,
            now_date=lambda: "2026-07-01",
        )
        out = broker.dispatch("prove_verify", {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "store")
        self.assertIn("header", out["reason"].lower())

    def test_corrupt_receipt_body_on_prove_verify_is_structured_not_a_raw_crash(self):
        # Redteam verify pass: a garbled receipt body (a large store tail-truncated so SQLite opens
        # fine but a body no longer parses) must not crash prove_verify with a raw json error on the
        # agent-facing server path — the same torn-store CLASS the dispatch StoreCorruptError catch
        # closes, via a read-time trigger instead of open-time.
        broker, _, store_provider = make_broker()
        store = store_provider("prod")  # same instance dispatch's default target resolves to
        store.record_intent({"doc": "SI-1"})
        store._conn.execute("UPDATE receipts SET body=? WHERE seq=0", ("{bad json",))
        out = broker.dispatch("prove_verify", {})  # must NOT raise
        self.assertFalse(out["ok"])


class TestReadTier(unittest.TestCase):
    def test_get(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("get_sales_invoice", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["doc"]["name"], "SI-1")

    def test_get_error_is_structured_not_a_traceback(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("get_sales_invoice", {"name": "NOPE"})
        self.assertFalse(out["ok"])
        self.assertIn("404", out["reason"])

    def test_list(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("list_sales_invoices", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["rows"][0]["name"], "SI-1")


class TestUpdateStockDisclosure(unittest.TestCase):
    """Envelope E2 (2026-07-07): a document with ``update_stock`` set moves PHYSICAL stock on
    submit — the stock ledger is written alongside the GL, and with perpetual inventory disabled
    the movement leaves no trace in the projected GL at all. The memorandum must disclose the
    movement itself (from the draft's own items rows — never a new bench read), on plan_submit AND
    plan_cancel (the cancel reverses it). Doctype-agnostic by construction: fires only when the
    doc itself carries a truthy ``update_stock``."""

    def _stock_si(self, client):
        client.docs["SI-1"]["update_stock"] = 1
        client.docs["SI-1"]["items"] = [
            {"item_code": "G10-WIDGET", "qty": 2, "uom": "Nos", "warehouse": "Stores - PT"},
        ]

    def test_plan_submit_discloses_the_stock_movement(self):
        broker, client, _ = make_broker()
        self._stock_si(client)
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        flag = next(f for f in out["risk_flags"] if "stock" in f.lower())
        self.assertIn("G10-WIDGET", flag)
        self.assertIn("Stores - PT", flag)
        self.assertIn("perpetual inventory", flag)  # names why the GL alone can be blind to it

    def test_plan_cancel_discloses_the_reversal(self):
        broker, client, _ = make_broker()
        self._stock_si(client)
        client.docs["SI-1"]["docstatus"] = 1
        out = broker.dispatch("plan_cancel", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        flag = next(f for f in out["risk_flags"] if "stock" in f.lower())
        self.assertIn("revers", flag.lower())
        self.assertIn("G10-WIDGET", flag)

    def test_no_update_stock_no_flag(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any("physical stock" in f.lower() for f in out["risk_flags"]))

    def test_many_rows_are_summarized_not_dumped(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["update_stock"] = 1
        client.docs["SI-1"]["items"] = [
            {"item_code": f"ITEM-{i}", "qty": 1, "uom": "Nos", "warehouse": "Stores - PT"}
            for i in range(12)
        ]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        flag = next(f for f in out["risk_flags"] if "stock" in f.lower())
        self.assertIn("12 item row(s)", flag)
        self.assertIn("more", flag)  # truncated honestly, never silently

    def _negative_qty_si(self, client):
        client.docs["SI-1"]["update_stock"] = 1
        client.docs["SI-1"]["items"] = [
            {"item_code": "G10-WIDGET", "qty": -2, "uom": "Nos", "warehouse": "Stores - PT"},
        ]

    def test_negative_qty_submit_names_the_inbound_direction(self):
        # Envelope E4 (source-confirmed defect): a negative qty row is a return receipt — stock
        # comes IN on submit — the submit disclosure must say so explicitly.
        broker, client, _ = make_broker()
        self._negative_qty_si(client)
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        inbound_flag = next(f for f in out["risk_flags"] if "NEGATIVE" in f)
        self.assertIn("comes IN", inbound_flag)
        self.assertIn("return receipt", inbound_flag.lower())
        self.assertIn("G10-WIDGET", inbound_flag)

    def test_negative_qty_cancel_no_longer_claims_return_to_their_warehouses(self):
        # The BACKWARDS defect: cancelling a return sends stock back OUT, not "return to their
        # warehouses" (that phrasing is only true for a normal positive-qty movement).
        broker, client, _ = make_broker()
        self._negative_qty_si(client)
        client.docs["SI-1"]["docstatus"] = 1
        out = broker.dispatch("plan_cancel", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        flag = next(f for f in out["risk_flags"] if "stock" in f.lower())
        self.assertNotIn("return to their warehouses", flag)
        self.assertIn("back OUT", flag)
        self.assertIn("G10-WIDGET", flag)

    def test_positive_qty_submit_flag_is_byte_identical_to_before_the_sign_aware_fix(self):
        broker, client, _ = make_broker()
        self._stock_si(client)
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        flag = next(f for f in out["risk_flags"] if "stock" in f.lower())
        self.assertEqual(
            flag,
            "this document moves PHYSICAL STOCK on submit (update_stock is set): 1 item row(s) "
            "— 2 Nos of G10-WIDGET @ Stores - PT. The stock ledger is written alongside the GL; "
            "the projected GL shows the valuation rows only when perpetual inventory is enabled, "
            "so the stock movement can be invisible in the GL preview above")

    def test_positive_qty_cancel_flag_is_byte_identical_to_before_the_sign_aware_fix(self):
        broker, client, _ = make_broker()
        self._stock_si(client)
        client.docs["SI-1"]["docstatus"] = 1
        out = broker.dispatch("plan_cancel", {"name": "SI-1"})
        flag = next(f for f in out["risk_flags"] if "stock" in f.lower())
        self.assertEqual(
            flag,
            "cancelling this document REVERSES its physical stock movement (update_stock was "
            "set): 1 item row(s) — 2 Nos of G10-WIDGET @ Stores - PT — return to their "
            "warehouses; the stock ledger entries are cancelled alongside the GL reversal")


class TestReturnDisclosure(unittest.TestCase):
    """Envelope E4: ``is_return`` (credit note) disclosures, doctype-agnostic — read entirely from
    the draft's own fields (``is_return``, ``return_against``, items qty signs), no new bench
    read. Fires on ``plan_submit``, ``plan_cancel``, and per-node in ``plan_cascade_cancel``."""

    def test_plan_submit_names_the_document_as_a_return(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_return"] = 1
        client.docs["SI-1"]["return_against"] = "SI-9"
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("RETURN" in f and "credit note" in f for f in out["risk_flags"]),
                        out["risk_flags"])

    def test_no_is_return_no_flag(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any("RETURN" in f for f in out["risk_flags"]))

    def test_free_standing_credit_note_gets_the_loud_flag(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_return"] = 1  # no return_against set
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        loud = next(f for f in out["risk_flags"] if f.startswith("FREE-STANDING credit note"))
        self.assertIn("FREE-STANDING", loud)
        self.assertIn("over-return", loud.lower())
        self.assertIn("exchange-rate", loud.lower())
        self.assertIn("receivable-account", loud.lower())
        self.assertIn("posting-date", loud.lower())

    def test_return_against_set_suppresses_the_loud_flag(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_return"] = 1
        client.docs["SI-1"]["return_against"] = "SI-9"
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any(f.startswith("FREE-STANDING credit note") for f in out["risk_flags"]))

    def test_update_outstanding_for_self_discloses_the_original_is_not_settled(self):
        # Envelope E4, found live (PHASE Q): the mapper-built return posts against ITSELF by
        # default — the original's outstanding does not move. Consent must know which shape it is.
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_return"] = 1
        client.docs["SI-1"]["return_against"] = "SI-9"
        client.docs["SI-1"]["update_outstanding_for_self"] = 1
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        flag = next(f for f in out["risk_flags"] if "does NOT settle SI-9" in f)
        self.assertIn("update_outstanding_for_self", flag)
        self.assertIn("separate payment reconciliation", flag)

    def test_no_update_outstanding_for_self_discloses_the_original_is_reduced(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_return"] = 1
        client.docs["SI-1"]["return_against"] = "SI-9"
        client.docs["SI-1"]["update_outstanding_for_self"] = 0
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("posts against SI-9" in f and "outstanding is reduced" in f
                            for f in out["risk_flags"]))

    def test_free_standing_credit_note_gets_no_settlement_flag(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_return"] = 1
        client.docs["SI-1"]["update_outstanding_for_self"] = 1
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any("settle" in f or "posts against" in f for f in out["risk_flags"]))

    def test_mixed_sign_rows_get_the_mixed_sign_flag(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_return"] = 1
        client.docs["SI-1"]["return_against"] = "SI-9"
        client.docs["SI-1"]["items"] = [{"item_code": "A", "qty": -1}, {"item_code": "B", "qty": 1}]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("MIXED-SIGN" in f for f in out["risk_flags"]), out["risk_flags"])

    def test_all_negative_rows_no_mixed_sign_flag(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_return"] = 1
        client.docs["SI-1"]["return_against"] = "SI-9"
        client.docs["SI-1"]["items"] = [{"item_code": "A", "qty": -1}, {"item_code": "B", "qty": -2}]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any("MIXED-SIGN" in f for f in out["risk_flags"]), out["risk_flags"])

    def test_plan_cancel_carries_the_same_return_flags(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_return"] = 1  # free-standing, no return_against
        client.docs["SI-1"]["docstatus"] = 1
        out = broker.dispatch("plan_cancel", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("RETURN" in f and "credit note" in f for f in out["risk_flags"]))
        self.assertTrue(any(f.startswith("FREE-STANDING credit note") for f in out["risk_flags"]))

    def test_cascade_per_node_flags_prefixed_with_docname(self):
        cc = CascadeClient({})
        cc.docs["T"]["is_return"] = 1  # free-standing on the target itself
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel",
                               {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(any(f.startswith("T: ") and "RETURN" in f and "credit note" in f
                            for f in plan["risk_flags"]), plan["risk_flags"])
        self.assertTrue(any(f.startswith("T: FREE-STANDING credit note") for f in plan["risk_flags"]),
                        plan["risk_flags"])


class TestPosDisclosure(unittest.TestCase):
    """Envelope E4: ``is_pos`` disclosures, doctype-agnostic — read entirely from the draft's own
    ``payments`` child rows and header fields, no new bench read. Fires on ``plan_submit``,
    ``plan_cancel`` (gains one extra sentence — the inline payment GL legs reverse too), and
    per-node in ``plan_cascade_cancel``."""

    def test_no_is_pos_no_flag(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any("POS document" in f for f in out["risk_flags"]))

    def test_payments_summarized_mode_and_amount(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_pos"] = 1
        client.docs["SI-1"]["grand_total"] = 100.0
        client.docs["SI-1"]["is_created_using_pos"] = 1
        client.docs["SI-1"]["payments"] = [
            {"mode_of_payment": "Cash", "amount": 60.0},
            {"mode_of_payment": "Card", "amount": 40.0},
        ]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        flag = next(f for f in out["risk_flags"] if f.startswith("POS document"))
        self.assertIn("Cash", flag)
        self.assertIn("60.0", flag)
        self.assertIn("Card", flag)
        self.assertIn("40.0", flag)
        self.assertIn("2 payment row(s)", flag)

    def test_many_payments_summarized_not_dumped(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_pos"] = 1
        client.docs["SI-1"]["grand_total"] = 100.0
        client.docs["SI-1"]["is_created_using_pos"] = 1
        client.docs["SI-1"]["payments"] = [
            {"mode_of_payment": f"Mode-{i}", "amount": 1.0} for i in range(7)
        ]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        flag = next(f for f in out["risk_flags"] if f.startswith("POS document"))
        self.assertIn("7 payment row(s)", flag)
        self.assertIn("more", flag)

    def test_empty_payments_positive_total_discloses_the_coming_refusal(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_pos"] = 1
        client.docs["SI-1"]["grand_total"] = 100.0
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        flag = next(f for f in out["risk_flags"] if "POS document" in f)
        self.assertIn("NO payments rows", flag)
        self.assertIn("refuse", flag.lower())

    def test_zero_total_no_payments_names_the_waiver(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_pos"] = 1
        client.docs["SI-1"]["grand_total"] = 0
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        flag = next(f for f in out["risk_flags"] if "POS document" in f)
        self.assertIn("waive", flag.lower())

    def test_partial_payment_flagged_when_not_created_using_pos(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_pos"] = 1
        client.docs["SI-1"]["grand_total"] = 100.0
        client.docs["SI-1"]["payments"] = [{"mode_of_payment": "Cash", "amount": 40.0}]
        # is_created_using_pos deliberately NOT set
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any(f.startswith("PARTIAL-PAYMENT") for f in out["risk_flags"]),
                        out["risk_flags"])

    def test_partial_payment_not_flagged_when_created_using_pos(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_pos"] = 1
        client.docs["SI-1"]["grand_total"] = 100.0
        client.docs["SI-1"]["is_created_using_pos"] = 1
        client.docs["SI-1"]["payments"] = [{"mode_of_payment": "Cash", "amount": 40.0}]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any(f.startswith("PARTIAL-PAYMENT") for f in out["risk_flags"]))

    def test_full_payment_not_flagged(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_pos"] = 1
        client.docs["SI-1"]["grand_total"] = 100.0
        client.docs["SI-1"]["payments"] = [{"mode_of_payment": "Cash", "amount": 100.0}]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any(f.startswith("PARTIAL-PAYMENT") for f in out["risk_flags"]))

    def test_float_dust_shortfall_is_not_flagged_partial(self):
        # An intended-full payment whose float representation lands a hair under grand_total
        # must not read as a shortfall — tolerance 0.005, the JE balance-check precedent.
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_pos"] = 1
        client.docs["SI-1"]["grand_total"] = 108.9
        client.docs["SI-1"]["payments"] = [
            {"mode_of_payment": "Cash", "amount": 36.3},
            {"mode_of_payment": "Card", "amount": 36.3},
            {"mode_of_payment": "Bank", "amount": 36.3},
        ]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any(f.startswith("PARTIAL-PAYMENT") for f in out["risk_flags"]))

    def test_a_real_shortfall_beyond_the_tolerance_is_flagged(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_pos"] = 1
        client.docs["SI-1"]["grand_total"] = 100.0
        client.docs["SI-1"]["payments"] = [{"mode_of_payment": "Cash", "amount": 99.99}]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any(f.startswith("PARTIAL-PAYMENT") for f in out["risk_flags"]))

    def test_plan_cancel_adds_the_inline_payment_gl_legs_sentence(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_pos"] = 1
        client.docs["SI-1"]["grand_total"] = 100.0
        client.docs["SI-1"]["is_created_using_pos"] = 1
        client.docs["SI-1"]["payments"] = [{"mode_of_payment": "Cash", "amount": 100.0}]
        client.docs["SI-1"]["docstatus"] = 1
        out = broker.dispatch("plan_cancel", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("reverses the inline payment GL legs" in f for f in out["risk_flags"]),
                        out["risk_flags"])

    def test_plan_submit_has_no_cancel_only_sentence(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["is_pos"] = 1
        client.docs["SI-1"]["grand_total"] = 100.0
        client.docs["SI-1"]["is_created_using_pos"] = 1
        client.docs["SI-1"]["payments"] = [{"mode_of_payment": "Cash", "amount": 100.0}]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(
            any("reverses the inline payment GL legs" in f for f in out["risk_flags"]))

    def test_cascade_per_node_flags_prefixed_with_docname(self):
        cc = CascadeClient({})
        cc.docs["T"]["is_pos"] = 1
        cc.docs["T"]["grand_total"] = 100.0
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel",
                               {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(any(f.startswith("T: ") and "POS document" in f
                            for f in plan["risk_flags"]), plan["risk_flags"])
        self.assertTrue(any(f.startswith("T: ") and "reverses the inline payment GL legs" in f
                            for f in plan["risk_flags"]), plan["risk_flags"])


class TestPlanSubmit(unittest.TestCase):
    def test_plan_records_and_returns(self):
        broker, _, stores = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["plan_id"])
        self.assertEqual(out["doc_version"], "2026-07-01 10:00:00.000001")
        self.assertEqual(out["projected_gl"][0]["account"], "Debtors")
        stored = stores("prod").get_plan(out["plan_id"])
        self.assertEqual(stored.docname, "SI-1")
        self.assertEqual(stored.target, "prod")

    def test_plan_refuses_non_draft(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["docstatus"] = 1
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertIn("draft", out["reason"].lower())

    def test_plan_refuses_company_off_target_pin(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["company"] = "Other Corp"
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertIn("company", out["reason"].lower())

    def test_future_posting_date_is_flagged(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["posting_date"] = "2026-08-01"
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(any("future" in f for f in out["risk_flags"]))

    # --- Envelope E6: plan-time closed-books disclosure — the memorandum warns BEFORE a marker
    # is minted, instead of the human only discovering the refusal at submit. Disclosure only:
    # the plan still records (ok: True); the execute-time gate (spine.check_red_line) still
    # enforces the actual refusal (TestGovernedSubmit.test_locked_period_refused, below).
    def test_locked_period_flagged_but_plan_still_records(self):
        broker, client, _ = make_broker()
        client.locks = {"frozen_until": "2026-07-15"}
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertTrue(any(
            "closed-books" in f and "frozen_until" in f and "2026-07-15" in f
            for f in out["risk_flags"]))

    def test_unlocked_posting_carries_no_closed_books_flag(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertFalse(any("closed-books" in f for f in out["risk_flags"]))

    def test_unreadable_period_locks_refuses_the_plan(self):
        # get_period_locks is deny-on-unreadable at execute already; wiring it into plan_submit
        # for the disclosure inherits that same deny-bias — an unverifiable lock source refuses
        # the whole plan, never reads as "no lock".
        broker, client, _ = make_broker()
        client.fail_locks = "HTTP 403: PermissionError"
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertFalse(out["ok"])


class TestGovernedSubmit(unittest.TestCase):
    def _plan_and_mint(self, broker, stores, name="SI-1"):
        plan = broker.dispatch("plan_submit", {"name": name})
        token = "raw-token-high-entropy"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        return plan, token

    def test_full_governed_flow(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"]["docstatus"], 1)
        self.assertEqual(client.submitted, ["SI-1"])
        store = stores("prod")
        self.assertEqual(store.marker_state(token), "consumed")
        receipts = store.receipts()
        self.assertEqual([r.kind for r in receipts], ["intent", "outcome"])
        self.assertEqual(receipts[0].body["docname"], "SI-1")
        self.assertEqual(store.orphans(), [])

    def test_outcome_result_is_normalised_minimal(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        broker.dispatch("submit_sales_invoice",
                        {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        outcome = stores("prod").receipts()[-1]
        # "doctype" is recorded on BOTH sides of the submit/cancel receipt: the outcome's result
        # (built in this module's execute() closure, asserted here) and the intent body (added to
        # the spine core as a one-line, doctype-agnostic field — see test_spine.py). Every receipt
        # is self-describing per doctype.
        self.assertEqual(set(outcome.body["result"]),
                         {"name", "docstatus", "modified", "doctype"})
        self.assertEqual(outcome.body["result"]["doctype"], SALES_INVOICE)

    def test_unknown_plan_refused(self):
        broker, _, stores = make_broker()
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": "nope", "marker": "t"})
        self.assertFalse(out["ok"])

    def test_wrong_docname_refused(self):
        broker, client, stores = make_broker()
        client.docs["SI-2"] = dict(client.docs["SI-1"], name="SI-2")
        plan, token = self._plan_and_mint(broker, stores)
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-2", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(client.submitted, [])

    def test_stale_plan_refused(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.docs["SI-1"]["modified"] = "2026-07-01 11:00:00.000001"
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "fresh")
        self.assertEqual(client.submitted, [])

    def test_locked_period_refused(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.locks = {"frozen_until": "2026-12-31"}
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "red_line")
        self.assertEqual(client.submitted, [])

    def test_unreadable_locks_refuse_not_proceed(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.fail_locks = "HTTP 403: PermissionError"
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(client.submitted, [])

    def test_submit_threads_doctype_and_posting_date_into_get_period_locks(self):
        # F5: get_period_locks now REQUIRES doctype+posting_date — pins that _governed_write
        # actually threads BOTH through (using the doc it already fetched, never a fresh read).
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.lock_calls.clear()
        broker.dispatch("submit_sales_invoice",
                        {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertEqual(client.lock_calls, [("Example Corp", SALES_INVOICE, "2026-07-01")])

    def test_plan_and_execute_call_get_period_locks_with_the_same_triple(self):
        # F7 (disclosure parity): plan_submit's disclosure read and submit's execute-time gate
        # must call get_period_locks with the IDENTICAL (company, doctype, posting_date) triple —
        # the "never drifts" property is calling the same function with the same arguments, not
        # merely reusing check_red_line.
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_submit", {"name": "SI-1"})
        plan_call = client.lock_calls[-1]
        token = "tok-parity"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        broker.dispatch("submit_sales_invoice",
                        {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        execute_call = client.lock_calls[-1]
        self.assertEqual(plan_call, execute_call)

    def test_spent_marker_refused_on_second_use(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        args = {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token}
        self.assertTrue(broker.dispatch("submit_sales_invoice", args)["ok"])
        # A second submit of the same doc is stale anyway; reset the doc to isolate the marker.
        client.docs["SI-1"]["modified"] = "2026-07-01 10:05:00.000001"
        plan2 = broker.dispatch("plan_submit", {"name": "SI-1"})
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan2["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(client.submitted, ["SI-1"])

    def test_failed_execute_releases_marker_and_leaves_orphan(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.fail_submit = "HTTP 500: ValidationError"
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        store = stores("prod")
        self.assertEqual(store.marker_state(token), "live")   # grant spared
        self.assertEqual(len(store.orphans()), 1)             # intent awaits reconciliation

    # --- F-C2: the execute-time wrong-books TOCTOU belt (the gap _cascade_books_gate already
    # closed for cascade cancel, tools.py ~1632, mirrored into the single-op spine here). Plan
    # while the doc's company still matches the pinned target (so a plan records and a marker can
    # mint), then the doc's company changes BEFORE execute — `modified` deliberately left
    # untouched, the exact TOCTOU shape a `db_set(update_modified=False)`/raw-SQL company patch
    # produces, which check_fresh's version-equality cannot see.
    def test_execute_refuses_wrong_books_toctou(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.docs["SI-1"]["company"] = "Other Corp"   # modified untouched — the TOCTOU shape
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")   # mirrors the plan-time/cascade wrong-books stage
        self.assertIn("wrong books", out["reason"].lower())
        self.assertIn("Other Corp", out["reason"])
        self.assertEqual(client.submitted, [])   # nothing written — the belt fired before execute
        # A refusal is not a spend: the marker was never reserved/claimed (this belt sits before
        # `store.get_marker`/`governed_submit` run at all), so it stays exactly as minted — a
        # legitimate re-plan against the correct company could still use it.
        self.assertEqual(stores("prod").marker_state(token), "live")

    def test_execute_company_match_proceeds_normally(self):
        # Regression: the ordinary path (doc company == target's pinned company) is unaffected by
        # the new belt — same fixture shape as test_full_governed_flow, asserted explicitly here
        # as the F-C2 companion pin (a same-company write must never be caught in the net).
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(client.submitted, ["SI-1"])
        self.assertEqual(stores("prod").marker_state(token), "consumed")

    def test_execute_unpinned_target_no_wrong_books_refusal(self):
        # The documented unpinned posture (registry.py): a target with no company pin accepts any
        # company's document — no wrong-books refusal, at plan OR execute.
        broker, client, stores = make_broker(reg=REG_UNPINNED)
        plan, token = self._plan_and_mint(broker, stores)
        client.docs["SI-1"]["company"] = "Other Corp"
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(client.submitted, ["SI-1"])

    def test_wrong_books_refusal_precedes_red_line(self):
        # Ordering: when a company mismatch AND a closed-books lock would BOTH independently
        # refuse this write, the wrong-books belt must fire first (stage "plan", not "red_line") —
        # a document posting to the wrong company's books is refused on that fact alone, before any
        # date-range belt gets a chance to characterize it as merely "locked." Placement documented
        # at the belt's own comment in _governed_write.
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.docs["SI-1"]["company"] = "Other Corp"
        client.locks = {"frozen_until": "2026-12-31"}   # would ALSO refuse, via check_red_line
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("wrong books", out["reason"].lower())
        self.assertEqual(client.submitted, [])


class TestGovernedSubmitNoAnswerReadback(unittest.TestCase):
    """Transport taxonomy glue wiring (docs/plans/2026-07-07-transport-taxonomy.md): a RAW,
    unconverted exception from ``submit_document`` itself (never an ``ErpnextError`` at all — the
    exact residual shape a bare connection failure used to leave) is "no answer", not an answered
    refusal. `_governed_write`'s ``readback`` closure must be wired through to
    ``SubmitEffects``/``governed_submit`` end to end: the marker is spent (never released) and the
    outcome resolved by a real ``client.get_document`` readback — mirrors
    ``TestCascadeCancelConfirmation`` (test_tools.py:2782) for the single-op path."""

    def _plan_and_mint(self, broker, stores, name="SI-1"):
        plan = broker.dispatch("plan_submit", {"name": name})
        token = "raw-token-high-entropy"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        return plan, token

    def test_raw_exception_readback_confirms_committed_with_confirmed_via(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.raise_raw_submit = RuntimeError("connection reset by peer")
        # simulate the write having actually landed server-side despite the raw exception
        client.docs["SI-1"]["docstatus"] = 1
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["confirmed_via"], "post_failure_readback")
        self.assertEqual(out["result"]["docstatus"], 1)
        self.assertEqual(stores("prod").marker_state(token), "consumed")
        self.assertEqual(client.submitted, [])   # the fake never recorded a submit — only raised

    def test_raw_exception_readback_mismatch_is_unconfirmed_marker_still_consumed(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.raise_raw_submit = RuntimeError("connection reset by peer")
        # docstatus left at 0 (draft, unchanged) — the readback shows nothing landed either
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "unconfirmed")
        self.assertEqual(stores("prod").marker_state(token), "consumed")   # spent, not released
        self.assertEqual(len(stores("prod").orphans()), 1)

    def test_raw_exception_readback_itself_fails_unconfirmed_with_readback_error(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.raise_raw_submit = RuntimeError("connection reset by peer")
        client.readback_fails = "SI-1"   # the confirmatory get_document ALSO fails
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "unconfirmed")
        self.assertIn("bench hiccup during readback", out["reason"])
        self.assertEqual(stores("prod").marker_state(token), "consumed")   # never release-in-flight
        outcomes = [r for r in stores("prod").receipts() if r.kind == "outcome"]
        self.assertIn("bench hiccup during readback", outcomes[-1].body["result"]["readback_error"])

    def test_answered_error_unaffected_by_the_new_no_answer_branch(self):
        # Regression: the pre-existing fail_submit shape (an ANSWERED bench refusal) is untouched —
        # this is the byte-identical sibling of test_failed_execute_releases_marker_and_leaves_orphan.
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.fail_submit = "HTTP 500: ValidationError"
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(stores("prod").marker_state(token), "live")   # released, not spent


class TestPlanCancel(unittest.TestCase):
    def test_plan_records_the_reversal_and_the_op(self):
        broker, _, stores = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["projected_reversal"][0]["account"], "Debtors")
        stored = stores("prod").get_plan(out["plan_id"])
        self.assertEqual(stored.op, "cancel")
        self.assertEqual(stored.docname, "SI-9")

    def test_plan_refuses_a_draft(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertIn("submitted", out["reason"])

    def test_plan_refuses_when_submitted_docs_link_to_it(self):
        broker, client, _ = make_broker()
        client.linked = [{"doctype": "Payment Entry", "name": "PE-1"},
                         {"doctype": "Delivery Note", "name": "DN-1"}]
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertFalse(out["ok"])
        self.assertIn("Payment Entry PE-1", out["reason"])
        self.assertIn("cascade", out["reason"])

    def test_unreadable_blast_radius_refuses_never_reads_as_empty(self):
        broker, client, _ = make_broker()
        client.fail_linked = "HTTP 500: linked-with exploded"
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertFalse(out["ok"])

    def test_plan_refuses_company_off_target_pin(self):
        broker, client, _ = make_broker()
        client.docs["SI-9"]["company"] = "Other Corp"
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertFalse(out["ok"])
        self.assertIn("company", out["reason"].lower())

    def test_no_gl_rows_is_flagged_not_hidden(self):
        broker, client, _ = make_broker()
        client.gl_rows = []
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertTrue(out["ok"])
        self.assertTrue(any("GL" in f for f in out["risk_flags"]))

    def test_unreadable_gl_entries_refuses_the_plan_never_reads_as_empty(self):
        # The reconciliation-audit residual (21b7f84): an unverifiable projected reversal (the
        # real ErpnextClient now raises on a malformed GL Entry row/body — test_erpnext.py pins
        # that) must refuse the WHOLE plan through dispatch's structured-deny catch, the same
        # deny-bias as get_submitted_linked_docs/get_period_locks/get_settling_references above —
        # it must never be silently swallowed into a "no live GL rows" flag (that flag is for a
        # genuinely EMPTY, verified read, see test_no_gl_rows_is_flagged_not_hidden immediately
        # above; an UNREADABLE read is a different, stronger case: a refusal, not a disclosure).
        broker, client, _ = make_broker()
        client.fail_gl_entries = "HTTP 403: PermissionError"
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")
        self.assertIn("PermissionError", out["reason"])

    # --- Envelope E6: plan-time closed-books disclosure, cancel direction (mirrors TestPlanSubmit).
    def test_locked_period_flagged_but_plan_still_records(self):
        broker, client, _ = make_broker()
        client.locks = {"pcv_until": "2026-06-20"}
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertTrue(out["ok"])
        self.assertTrue(any(
            "closed-books" in f and "pcv_until" in f and "2026-06-20" in f
            for f in out["risk_flags"]))

    def test_unlocked_posting_carries_no_closed_books_flag(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertTrue(out["ok"])
        self.assertFalse(any("closed-books" in f for f in out["risk_flags"]))

    def test_unreadable_period_locks_refuses_the_plan(self):
        broker, client, _ = make_broker()
        client.fail_locks = "HTTP 403: PermissionError"
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertFalse(out["ok"])


class TestSettlingPeDisclosure(unittest.TestCase):
    """F-R1 — the settling-PE disclosure on cancel (pin sheet
    docs/plans/2026-07-07-fr1-settling-pe-disclosure.md). ``plan_cancel`` now reads the settling
    Payment Ledger Entry rows (``get_settling_references``) for EVERY supported doctype — not just
    Journal Entry — plus the Accounts Settings unlink flag (widened beyond the prior JE-only gate,
    memoized once per plan) — and flags each settling voucher in one of two exact voices depending
    on whether ``unlink_payment_on_cancellation_of_invoice`` is ON or OFF. The JE-specific EG-note
    and unlink flag (``_journal_entry_cancel_flags_for_settings``) are unchanged, exercised
    elsewhere (TestJournalEntryPlanCancelAndCancel)."""

    def test_on_voice_names_the_voucher_and_amount(self):
        broker, client, _ = make_broker()
        client.accounts_settings["unlink_payment_on_cancellation_of_invoice"] = 1
        client.settling_references["SI-9"] = [
            {"voucher_type": "Payment Entry", "voucher_no": "PE-9", "amount": 250.0,
             "account_currency": "USD"}]
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertTrue(out["ok"], out)
        flag = next(f for f in out["risk_flags"] if "SILENTLY UNLINK" in f)
        self.assertIn("Payment Entry PE-9", flag)
        self.assertIn("250.0", flag)
        self.assertIn("auto_cancel_exempted_doctypes", flag)

    def test_off_voice_names_the_refusal(self):
        broker, client, _ = make_broker()
        client.settling_references["SI-9"] = [
            {"voucher_type": "Payment Entry", "voucher_no": "PE-9", "amount": 250.0,
             "account_currency": "USD"}]
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertTrue(out["ok"], out)
        flag = next(f for f in out["risk_flags"] if "REFUSE this cancel" in f)
        self.assertIn("LinkExistsError", flag)
        self.assertIn("PE-9", flag)

    def test_control_no_settling_rows_means_no_settling_flags(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any("SILENTLY UNLINK" in f or "REFUSE this cancel" in f
                             for f in out["risk_flags"]))

    def test_unreadable_ple_refuses_the_whole_plan(self):
        broker, client, _ = make_broker()
        client.fail_settling_references = "HTTP 403: PermissionError"
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertFalse(out["ok"])

    def test_unreadable_settings_refuses_for_sales_invoice(self):
        # F-R1: an unreadable Accounts Settings now refuses SI/PI too, not just Journal Entry (the
        # widened gate) — the same deny-bias as every other lock-adjacent read in this codebase.
        broker, client, _ = make_broker()
        client.fail_accounts_settings = "HTTP 403: PermissionError"
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertFalse(out["ok"])

    def test_settings_read_happens_for_purchase_invoice_too(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_cancel",
                              {"name": "PI-9", "pacioli_doctype": "Purchase Invoice"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(client.accounts_settings_calls), 1)

    def test_at_most_one_settings_read_and_one_ple_read_per_plan(self):
        broker, client, _ = make_broker()
        client.settling_references["SI-9"] = [
            {"voucher_type": "Payment Entry", "voucher_no": "PE-9", "amount": 250.0,
             "account_currency": "USD"},
            {"voucher_type": "Payment Entry", "voucher_no": "PE-8", "amount": 10.0,
             "account_currency": "USD"},
        ]
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(client.accounts_settings_calls), 1)
        self.assertEqual(len(client.settling_reference_calls), 1)

    def test_multiple_settling_vouchers_each_get_their_own_flag(self):
        broker, client, _ = make_broker()
        client.accounts_settings["unlink_payment_on_cancellation_of_invoice"] = 1
        client.settling_references["SI-9"] = [
            {"voucher_type": "Payment Entry", "voucher_no": "PE-9", "amount": 250.0,
             "account_currency": "USD"},
            {"voucher_type": "Payment Entry", "voucher_no": "PE-8", "amount": 10.0,
             "account_currency": "USD"},
        ]
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertTrue(out["ok"], out)
        flags = [f for f in out["risk_flags"] if "SILENTLY UNLINK" in f]
        self.assertEqual(len(flags), 2)
        self.assertTrue(any("PE-9" in f and "250.0" in f for f in flags))
        self.assertTrue(any("PE-8" in f and "10.0" in f for f in flags))


class TestGovernedCancel(unittest.TestCase):
    def _plan_and_mint(self, broker, stores, name="SI-9"):
        plan = broker.dispatch("plan_cancel", {"name": name})
        token = "raw-cancel-token"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        return plan, token

    def test_full_governed_cancel(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"]["docstatus"], 2)
        self.assertEqual(client.cancelled, ["SI-9"])
        store = stores("prod")
        self.assertEqual(store.marker_state(token), "consumed")
        receipts = store.receipts()
        self.assertEqual([r.kind for r in receipts], ["intent", "outcome"])
        self.assertEqual(receipts[0].body["tool"], "cancel")
        self.assertEqual(receipts[0].body["transition"], "1->2")
        self.assertEqual(store.orphans(), [])

    def test_a_submit_marker_cannot_cancel(self):
        # The cross-op guard, direction one: consent to POST is not consent to UNWIND.
        broker, client, stores = make_broker()
        splan = broker.dispatch("plan_submit", {"name": "SI-1"})
        stores("prod").mint_marker("submit-token", splan["plan_id"], 2_000.0)
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-1", "plan_id": splan["plan_id"],
                               "marker": "submit-token"})
        self.assertFalse(out["ok"])
        self.assertIn("authorizes 'submit'", out["reason"])
        self.assertEqual(client.cancelled, [])
        self.assertEqual(stores("prod").marker_state("submit-token"), "live")  # grant untouched

    def test_a_cancel_marker_cannot_submit(self):
        # Direction two: consent to UNWIND is not consent to POST.
        broker, client, stores = make_broker()
        cplan, token = self._plan_and_mint(broker, stores)
        client.docs["SI-9"]["docstatus"] = 0  # even if the doc were somehow a draft again
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-9", "plan_id": cplan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertIn("authorizes 'cancel'", out["reason"])
        self.assertEqual(client.submitted, [])

    def test_cancel_into_a_locked_period_refused(self):
        # The posting sits at 2026-06-15; a freeze at month-end must block the unwind too.
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.locks = {"frozen_until": "2026-06-30"}
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "red_line")
        self.assertEqual(client.cancelled, [])

    def test_stale_cancel_plan_refused(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.docs["SI-9"]["modified"] = "2026-06-16 09:00:00.000001"
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "fresh")
        self.assertEqual(client.cancelled, [])

    def test_failed_cancel_releases_marker_and_leaves_orphan(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.fail_cancel = "HTTP 417: LinkExistsError"
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        store = stores("prod")
        self.assertEqual(store.marker_state(token), "live")
        self.assertEqual(len(store.orphans()), 1)

    # --- F-S1 pin F6 (cancel parity): the cancel path runs the identical doctype-aware check ---
    def test_cancel_threads_doctype_and_posting_date_into_get_period_locks(self):
        # get_period_locks now REQUIRES doctype+posting_date (F5) — this pins that the cancel
        # path (_governed_write) actually threads BOTH through, using the doc's own posting_date
        # (never a fresh network read), and never regresses to a doctype-blind call.
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.lock_calls.clear()
        broker.dispatch("cancel_sales_invoice",
                        {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertEqual(client.lock_calls, [("Example Corp", SALES_INVOICE, "2026-06-15")])

    # --- F-C2: the execute-time wrong-books TOCTOU belt, cancel direction (mirrors the submit-side
    # pins above and the cascade-cancel sibling, test_cascade_execute_refuses_wrong_books_
    # dependent_toctou in TestCascadeCancel).
    def test_execute_refuses_wrong_books_toctou(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.docs["SI-9"]["company"] = "Other Corp"   # modified untouched — the TOCTOU shape
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("wrong books", out["reason"].lower())
        self.assertIn("Other Corp", out["reason"])
        self.assertEqual(client.cancelled, [])
        self.assertEqual(stores("prod").marker_state(token), "live")   # grant untouched, not spent

    def test_execute_company_match_proceeds_normally(self):
        # Regression: same fixture shape as test_full_governed_cancel — an ordinary same-company
        # cancel is unaffected by the new belt.
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(client.cancelled, ["SI-9"])
        self.assertEqual(stores("prod").marker_state(token), "consumed")

    def test_execute_unpinned_target_no_wrong_books_refusal(self):
        broker, client, stores = make_broker(reg=REG_UNPINNED)
        plan, token = self._plan_and_mint(broker, stores)
        client.docs["SI-9"]["company"] = "Other Corp"
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(client.cancelled, ["SI-9"])

    def test_wrong_books_refusal_precedes_red_line(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.docs["SI-9"]["company"] = "Other Corp"
        client.locks = {"frozen_until": "2026-06-30"}   # would ALSO refuse, via check_red_line
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("wrong books", out["reason"].lower())
        self.assertEqual(client.cancelled, [])


class TestGovernedCancelNoAnswerReadback(unittest.TestCase):
    """Cancel-direction sibling of ``TestGovernedSubmitNoAnswerReadback`` — same glue wiring
    (``readback`` expects docstatus 2, the ``1->2`` transition end, instead of 1)."""

    def _plan_and_mint(self, broker, stores, name="SI-9"):
        plan = broker.dispatch("plan_cancel", {"name": name})
        token = "raw-cancel-token"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        return plan, token

    def test_raw_exception_readback_confirms_committed_with_confirmed_via(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.raise_raw_cancel = RuntimeError("connection reset by peer")
        client.docs["SI-9"]["docstatus"] = 2   # the cancel actually landed despite the raise
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["confirmed_via"], "post_failure_readback")
        self.assertEqual(out["result"]["docstatus"], 2)
        self.assertEqual(stores("prod").marker_state(token), "consumed")
        self.assertEqual(client.cancelled, [])

    def test_raw_exception_readback_mismatch_is_unconfirmed_marker_still_consumed(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.raise_raw_cancel = RuntimeError("connection reset by peer")
        # docstatus stays 1 (submitted, unchanged) — nothing landed
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "unconfirmed")
        self.assertEqual(stores("prod").marker_state(token), "consumed")

    def test_answered_error_unaffected_by_the_new_no_answer_branch(self):
        # Byte-identical sibling of test_failed_cancel_releases_marker_and_leaves_orphan.
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.fail_cancel = "HTTP 417: LinkExistsError"
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(stores("prod").marker_state(token), "live")


class TestAmendTool(unittest.TestCase):
    """UNDO's second half. No marker anywhere in these flows — the happy path succeeding with
    zero mints IS the proof that amend needs no consent grant; the receipts prove it still
    leaves a trail."""

    def _cancelled(self, client, name="SI-9"):
        client.docs[name]["docstatus"] = 2

    def test_amend_creates_the_draft_with_receipts_and_no_marker(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"]["name"], "SI-9-1")
        self.assertEqual(out["result"]["docstatus"], 0)
        self.assertEqual(out["result"]["amended_from"], "SI-9")
        self.assertIn("plan_submit", out["next"])          # the arc continues at the front door
        self.assertEqual(client.amended, ["SI-9"])
        store = stores("prod")
        receipts = store.receipts()
        self.assertEqual([r.kind for r in receipts], ["intent", "outcome"])
        self.assertEqual(receipts[0].body["tool"], "amend")
        self.assertEqual(receipts[0].body["transition"], "2->0(draft)")
        self.assertEqual(receipts[0].body["docname"], "SI-9")
        self.assertEqual(receipts[1].body["status"], "committed")
        self.assertEqual(store.orphans(), [])
        # No marker was minted, presented, or settled — the whole flow ran without one.

    def test_amend_takes_no_marker_by_schema(self):
        (schema,) = [t for t in TOOLS if t["name"] == "amend_sales_invoice"]
        self.assertEqual(set(schema["inputSchema"]["properties"]), {"name", "pacioli_target"})
        self.assertEqual(schema["inputSchema"]["required"], ["name"])

    def test_amend_refuses_an_uncancelled_source(self):
        broker, client, stores = make_broker()
        for name in ("SI-1", "SI-9"):  # a draft (0) and a submitted (1) document
            out = broker.dispatch("amend_sales_invoice", {"name": name})
            self.assertFalse(out["ok"])
            self.assertIn("cancelled", out["reason"])
        self.assertEqual(client.amended, [])
        self.assertEqual(stores("prod").receipts(), [])    # refused before any intent

    def test_amend_refuses_when_an_amendment_already_exists_and_names_it(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        client.docs["SI-9-1"] = {"name": "SI-9-1", "docstatus": 2, "company": "Example Corp",
                                 "amended_from": "SI-9",
                                 "modified": "2026-07-01 12:00:00.000001"}
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertFalse(out["ok"])
        self.assertIn("SI-9-1", out["reason"])             # names the existing one
        self.assertEqual(client.amended, [])               # even a CANCELLED amendment counts
        self.assertEqual(stores("prod").receipts(), [])

    def test_amend_refuses_wrong_books(self):
        broker, client, _ = make_broker()
        self._cancelled(client)
        client.docs["SI-9"]["company"] = "Other Corp"
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertFalse(out["ok"])
        self.assertIn("company", out["reason"].lower())
        self.assertEqual(client.amended, [])

    def test_failed_insert_leaves_an_orphan_intent(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        client.fail_amend = "HTTP 417: ValidationError"
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "execute")
        store = stores("prod")
        receipts = store.receipts()
        self.assertEqual([r.kind for r in receipts], ["intent", "outcome"])
        self.assertEqual(receipts[1].body["status"], "failed")
        self.assertEqual(len(store.orphans()), 1)          # awaits reconciliation

    def test_the_full_arc_cancel_amend_resubmit_reads_off_one_ledger(self):
        # The story the feature exists for: SI-9 is cancelled under a marker, amended without
        # one, and the corrected draft submitted under a fresh plan + marker — six receipts.
        broker, client, stores = make_broker()
        store = stores("prod")
        cplan = broker.dispatch("plan_cancel", {"name": "SI-9"})
        store.mint_marker("cancel-token", cplan["plan_id"], 2_000.0)
        self.assertTrue(broker.dispatch(
            "cancel_sales_invoice",
            {"name": "SI-9", "plan_id": cplan["plan_id"], "marker": "cancel-token"})["ok"])
        client.docs["SI-9"]["docstatus"] = 2               # the fake's cancel result, landed
        amended = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertTrue(amended["ok"])
        draft = amended["result"]["name"]
        splan = broker.dispatch("plan_submit", {"name": draft})
        store.mint_marker("submit-token", splan["plan_id"], 2_000.0)
        self.assertTrue(broker.dispatch(
            "submit_sales_invoice",
            {"name": draft, "plan_id": splan["plan_id"], "marker": "submit-token"})["ok"])
        tools = [r.body.get("tool") for r in store.receipts() if r.kind == "intent"]
        self.assertEqual(tools, ["cancel", "amend", "submit"])
        self.assertEqual(store.orphans(), [])


class TestMarkerlessRecordFailures(unittest.TestCase):
    """The markerless amend/workflow flows record intent/outcome DIRECTLY (no spine, no marker); a
    store-write failure in any of those three calls must become a structured result, never a raw
    traceback past dispatch(). John's ruling 2026-07-10 for the committed-record case (the act
    LANDED on the bench but its receipt could not be written): ok:False, name the landed doc, tell
    the caller NOT to retry, leave the intent as an orphan for hand-reconciliation — the PROVE chain
    is the source of truth, so the broker never attests a clean success the receipts don't back."""

    def _cancelled(self, client, name="SI-9"):
        client.docs[name]["docstatus"] = 2

    def test_amend_committed_record_failure_confesses_landed_but_unrecorded(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        store = stores("prod")
        orig = store.record_outcome
        def boom(intent, status, result, final_marker):
            if status == "committed":
                raise sqlite3.DatabaseError("disk I/O error")   # not an OperationalError -> uncaught before
            return orig(intent, status, result, final_marker)
        store.record_outcome = boom
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})  # must NOT raise
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "store")
        self.assertEqual(client.amended, ["SI-9"])          # the draft DID land on the bench
        self.assertIn("SI-9-1", out["reason"])              # names the landed doc
        self.assertIn("retry", out["reason"].lower())       # tells the caller not to retry
        self.assertEqual([r.seq for r in store.orphans()], [0])  # a bare orphan intent to reconcile

    def test_amend_intent_record_failure_is_structured_and_nothing_lands(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        store = stores("prod")
        store.record_intent = lambda body: (_ for _ in ()).throw(sqlite3.DatabaseError("io error"))
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})  # must NOT raise
        self.assertFalse(out["ok"])
        self.assertEqual(client.amended, [])                # pre-wire: nothing sent to the bench
        self.assertIn("intent", out["reason"].lower())

    def test_amend_double_fault_bench_and_failure_record_never_crashes(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        client.fail_amend = "HTTP 417: link exists"        # the bench insert itself fails
        store = stores("prod")
        store.record_outcome = lambda *a, **k: (_ for _ in ()).throw(sqlite3.DatabaseError("io"))
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})  # must NOT raise
        self.assertFalse(out["ok"])
        self.assertEqual(client.amended, [])                # nothing landed

    def test_workflow_committed_record_failure_confesses_landed_but_unrecorded(self):
        broker, client, stores = make_broker()
        client.workflows = [sample_workflow()]
        client.workflow_states["SI-1"] = "Draft"
        store = stores("prod")
        orig = store.record_outcome
        def boom(intent, status, result, final_marker):
            if status == "committed":
                raise sqlite3.DatabaseError("disk I/O error")
            return orig(intent, status, result, final_marker)
        store.record_outcome = boom
        out = broker.dispatch("request_workflow_transition",
                              {"name": "SI-1", "action": "Submit for Approval"})  # must NOT raise
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "store")
        self.assertEqual(client.applied_workflow_transitions, [("SI-1", "Submit for Approval")])
        self.assertIn("SI-1", out["reason"])
        self.assertIn("retry", out["reason"].lower())
        self.assertEqual([r.seq for r in store.orphans()], [0])

    def test_workflow_intent_record_failure_is_structured_and_nothing_moves(self):
        broker, client, stores = make_broker()
        client.workflows = [sample_workflow()]
        client.workflow_states["SI-1"] = "Draft"
        store = stores("prod")
        store.record_intent = lambda body: (_ for _ in ()).throw(sqlite3.DatabaseError("io error"))
        out = broker.dispatch("request_workflow_transition",
                              {"name": "SI-1", "action": "Submit for Approval"})  # must NOT raise
        self.assertFalse(out["ok"])
        self.assertEqual(client.applied_workflow_transitions, [])  # pre-wire: no state moved
        self.assertIn("intent", out["reason"].lower())

    def test_workflow_double_fault_apply_and_failure_record_never_crashes(self):
        broker, client, stores = make_broker()
        client.workflows = [sample_workflow()]
        client.workflow_states["SI-1"] = "Draft"
        client.fail_apply_workflow = "HTTP 417: transition rejected"  # the apply itself fails
        store = stores("prod")
        store.record_outcome = lambda *a, **k: (_ for _ in ()).throw(sqlite3.DatabaseError("io"))
        out = broker.dispatch("request_workflow_transition",
                              {"name": "SI-1", "action": "Submit for Approval"})  # must NOT raise
        self.assertFalse(out["ok"])
        self.assertEqual(client.applied_workflow_transitions, [])  # nothing moved


class TestProveTools(unittest.TestCase):
    def test_verify_and_orphans_tools(self):
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_submit", {"name": "SI-1"})
        stores("prod").mint_marker("t", plan["plan_id"], 2_000.0)
        broker.dispatch("submit_sales_invoice",
                        {"name": "SI-1", "plan_id": plan["plan_id"], "marker": "t"})
        v = broker.dispatch("prove_verify", {})
        self.assertTrue(v["ok"])
        self.assertTrue(v["head"])
        self.assertEqual(v["count"], 2)
        o = broker.dispatch("prove_orphans", {})
        self.assertTrue(o["ok"])
        self.assertEqual(o["orphans"], [])


if __name__ == "__main__":
    unittest.main()


class TestLockContentionIsStructuredNotACrash(unittest.TestCase):
    """A SQLite write-lock timeout during record_intent must return a fail-closed deny, never a
    traceback out of the tool call (the marker stays reserved; the human re-mints)."""

    def test_operational_error_becomes_a_deny(self):
        import sqlite3
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_submit", {"name": "SI-1"})
        stores("prod").mint_marker("t", plan["plan_id"], 2_000.0)

        real = BrokerStore.record_intent
        def boom(self, body):
            raise sqlite3.OperationalError("database is locked")
        BrokerStore.record_intent = boom
        try:
            out = broker.dispatch("submit_sales_invoice",
                                  {"name": "SI-1", "plan_id": plan["plan_id"], "marker": "t"})
        finally:
            BrokerStore.record_intent = real
        self.assertFalse(out["ok"])
        self.assertEqual(client.submitted, [])   # nothing posted


class TestWorkflowStatusTool(unittest.TestCase):
    def test_no_workflow_reports_inactive(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("workflow_status", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertFalse(out["workflow_active"])

    def test_active_workflow_reports_state_and_transitions(self):
        broker, client, _ = make_broker()
        client.workflows = [sample_workflow()]
        client.workflow_states["SI-1"] = "Draft"
        out = broker.dispatch("workflow_status", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["workflow_active"])
        self.assertEqual(out["workflow_name"], "SI Approval")
        self.assertEqual(out["current_state"], "Draft")
        self.assertEqual(out["current_state_doc_status"], "0")
        (t,) = out["transitions"]
        self.assertEqual(t["action"], "Submit for Approval")
        self.assertEqual(t["next_state"], "Pending Approval")
        self.assertFalse(t["approving"])
        self.assertEqual(t["allowed_role"], "Sales User")

    def test_sod_false_when_approving_transition_allows_self_approval(self):
        broker, client, _ = make_broker()
        states = [{"state": "Draft", "doc_status": "0"}, {"state": "Approved", "doc_status": "1"}]
        transitions = [{"state": "Draft", "action": "Approve", "next_state": "Approved",
                        "allowed": "Sales Manager", "allow_self_approval": "1"}]
        client.workflows = [sample_workflow(states=states, transitions=transitions)]
        client.workflow_states["SI-1"] = "Draft"
        out = broker.dispatch("workflow_status", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertFalse(out["sod"])
        self.assertIn("self-approval", out["note"])

    def test_missing_allow_self_approval_reads_as_frappe_default_on(self):
        # frappe's allow_self_approval defaults to "1" — a transition dict LACKING the key must
        # report allow_self_approval: true (and flag the SoD risk), never read as safely off.
        broker, client, _ = make_broker()
        states = [{"state": "Draft", "doc_status": "0"}, {"state": "Approved", "doc_status": "1"}]
        transitions = [{"state": "Draft", "action": "Approve", "next_state": "Approved",
                        "allowed": "Sales Manager"}]  # no allow_self_approval key at all
        client.workflows = [sample_workflow(states=states, transitions=transitions)]
        client.workflow_states["SI-1"] = "Draft"
        out = broker.dispatch("workflow_status", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        (t,) = out["transitions"]
        self.assertTrue(t["allow_self_approval"])
        self.assertFalse(out["sod"])
        self.assertIn("self-approval", out["note"])

    def test_unreadable_workflow_list_denies(self):
        broker, client, _ = make_broker()
        client.fail_workflows = "HTTP 403: PermissionError"
        out = broker.dispatch("workflow_status", {"name": "SI-1"})
        self.assertFalse(out["ok"])

    def test_ambiguous_workflows_deny_naming_both(self):
        broker, client, _ = make_broker()
        client.workflows = [sample_workflow(name="A"), sample_workflow(name="B")]
        out = broker.dispatch("workflow_status", {"name": "SI-1"})
        self.assertFalse(out["ok"])
        self.assertIn("A", out["reason"])
        self.assertIn("B", out["reason"])


class TestPlanSubmitWorkflowRiskFlag(unittest.TestCase):
    def test_no_workflow_no_new_risk_flag(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["risk_flags"], [])
        self.assertIsNone(out["workflow"])

    def test_active_workflow_flags_and_names_the_role(self):
        broker, client, _ = make_broker()
        client.workflows = [sample_workflow()]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertTrue(any("workflow-governed" in f and "Sales Manager" in f
                            for f in out["risk_flags"]))
        self.assertEqual(out["workflow"]["workflow_name"], "SI Approval")
        # plan is still recorded even though submit will be workflow-governed — planning is a read.
        self.assertTrue(out["plan_id"])

    def test_self_approvable_approving_transition_adds_its_own_risk_flag(self):
        broker, client, _ = make_broker()
        states = [{"state": "Draft", "doc_status": "0"}, {"state": "Approved", "doc_status": "1"}]
        transitions = [{"state": "Draft", "action": "Approve", "next_state": "Approved",
                        "allowed": "Sales Manager", "allow_self_approval": "1"}]
        client.workflows = [sample_workflow(states=states, transitions=transitions)]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(any("self-approval" in f for f in out["risk_flags"]))
        self.assertFalse(out["workflow"]["sod"])

    def test_ambiguous_workflow_is_flagged_not_refused(self):
        # Planning is a read; only the actual submit is refused for ambiguous config.
        broker, client, _ = make_broker()
        client.workflows = [sample_workflow(name="A"), sample_workflow(name="B")]
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertTrue(any("ambiguous" in f.lower() for f in out["risk_flags"]))


class TestGovernedWriteWorkflowGate(unittest.TestCase):
    def _plan_and_mint(self, broker, stores, name="SI-1"):
        plan = broker.dispatch("plan_submit", {"name": name})
        token = "raw-token-high-entropy"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        return plan, token

    def test_submit_refused_when_workflow_active_even_with_valid_plan_and_marker(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.workflows = [sample_workflow()]  # configured AFTER planning, as the gate re-reads live
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "workflow")
        self.assertIn("SI Approval", out["reason"])
        self.assertIn("Sales Manager", out["reason"])
        self.assertEqual(client.submitted, [])
        # the marker was never touched — refused before the spine even ran
        self.assertEqual(stores("prod").marker_state(token), "live")

    def test_submit_unaffected_when_no_workflow_configured(self):
        # Regression: the default FakeClient has no workflows — every existing governed-submit
        # test already proves this, this test pins the workflow fetch happened and passed.
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"])
        self.assertIn(SALES_INVOICE, client.workflow_calls)

    def test_cancel_refused_when_workflow_configures_a_cancel_state(self):
        broker, client, stores = make_broker()
        cplan = broker.dispatch("plan_cancel", {"name": "SI-9"})
        stores("prod").mint_marker("cancel-token", cplan["plan_id"], expires_at=2_000.0)
        states = [{"state": "Approved", "doc_status": "1"},
                 {"state": "Cancelled", "doc_status": "2"}]
        transitions = [{"state": "Approved", "action": "Cancel", "next_state": "Cancelled",
                        "allowed": "Sales Manager", "allow_self_approval": "0"}]
        client.workflows = [sample_workflow(states=states, transitions=transitions)]
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": cplan["plan_id"], "marker": "cancel-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "workflow")
        self.assertEqual(client.cancelled, [])

    def test_cancel_unaffected_when_workflow_does_not_govern_cancel(self):
        # sample_workflow()'s default states never reach doc_status "2" — cancel stays
        # marker-governed even though a workflow IS active for this doctype (governs submit only).
        broker, client, stores = make_broker()
        cplan = broker.dispatch("plan_cancel", {"name": "SI-9"})
        stores("prod").mint_marker("cancel-token", cplan["plan_id"], expires_at=2_000.0)
        client.workflows = [sample_workflow()]
        out = broker.dispatch("cancel_sales_invoice",
                              {"name": "SI-9", "plan_id": cplan["plan_id"], "marker": "cancel-token"})
        self.assertTrue(out["ok"])
        self.assertEqual(client.cancelled, ["SI-9"])

    def test_workflow_fetch_runs_after_check_op_cheap_checks_first(self):
        broker, client, stores = make_broker()
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": "no-such-plan", "marker": "t"})
        self.assertFalse(out["ok"])
        self.assertEqual(client.workflow_calls, [])  # never reached the network read

    def test_unreadable_workflow_config_denies_never_reads_as_no_workflow(self):
        broker, client, stores = make_broker()
        plan, token = self._plan_and_mint(broker, stores)
        client.fail_workflows = "HTTP 403: PermissionError"
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "SI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(client.submitted, [])


class TestRequestWorkflowTransitionTool(unittest.TestCase):
    def test_takes_no_marker_by_schema(self):
        (schema,) = [t for t in TOOLS if t["name"] == "request_workflow_transition"]
        # pacioli_doctype was added in the breadth increment (optional — default Sales Invoice).
        self.assertEqual(set(schema["inputSchema"]["properties"]),
                         {"name", "action", "pacioli_target", "pacioli_doctype"})
        self.assertEqual(schema["inputSchema"]["required"], ["name", "action"])

    def test_no_active_workflow_denied(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("request_workflow_transition", {"name": "SI-1", "action": "Go"})
        self.assertFalse(out["ok"])

    def test_non_approving_transition_succeeds_with_receipts_and_no_marker(self):
        broker, client, stores = make_broker()
        client.workflows = [sample_workflow()]
        client.workflow_states["SI-1"] = "Draft"
        out = broker.dispatch("request_workflow_transition",
                              {"name": "SI-1", "action": "Submit for Approval"})
        self.assertTrue(out["ok"])
        self.assertEqual(client.applied_workflow_transitions, [("SI-1", "Submit for Approval")])
        store = stores("prod")
        receipts = store.receipts()
        self.assertEqual([r.kind for r in receipts], ["intent", "outcome"])
        self.assertEqual(receipts[0].body["tool"], "workflow_transition")
        self.assertEqual(receipts[0].body["transition"], "state:Draft->Pending Approval")
        self.assertEqual(receipts[1].body["status"], "committed")
        self.assertEqual(store.orphans(), [])
        # no marker minted, presented, or settled anywhere in this flow

    def test_approving_transition_denied_naming_role_no_apply_called(self):
        broker, client, _ = make_broker()
        client.workflows = [sample_workflow()]
        client.workflow_states["SI-1"] = "Pending Approval"
        out = broker.dispatch("request_workflow_transition",
                              {"name": "SI-1", "action": "Approve"})
        self.assertFalse(out["ok"])
        self.assertIn("Sales Manager", out["reason"])
        self.assertEqual(client.applied_workflow_transitions, [])

    def test_unknown_action_denied_naming_legal_actions(self):
        broker, client, _ = make_broker()
        client.workflows = [sample_workflow()]
        client.workflow_states["SI-1"] = "Draft"
        out = broker.dispatch("request_workflow_transition",
                              {"name": "SI-1", "action": "Teleport"})
        self.assertFalse(out["ok"])
        self.assertIn("Submit for Approval", out["reason"])
        self.assertEqual(client.applied_workflow_transitions, [])

    def test_failed_apply_leaves_orphan_intent_and_structured_deny(self):
        broker, client, stores = make_broker()
        client.workflows = [sample_workflow()]
        client.workflow_states["SI-1"] = "Draft"
        client.fail_apply_workflow = "HTTP 417: WorkflowTransitionError"
        out = broker.dispatch("request_workflow_transition",
                              {"name": "SI-1", "action": "Submit for Approval"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "execute")
        store = stores("prod")
        receipts = store.receipts()
        self.assertEqual([r.kind for r in receipts], ["intent", "outcome"])
        self.assertEqual(receipts[1].body["status"], "failed")
        self.assertEqual(len(store.orphans()), 1)

    def test_wrong_books_company_denied(self):
        broker, client, _ = make_broker()
        client.docs["SI-1"]["company"] = "Other Corp"
        client.workflows = [sample_workflow()]
        client.workflow_states["SI-1"] = "Draft"
        out = broker.dispatch("request_workflow_transition",
                              {"name": "SI-1", "action": "Submit for Approval"})
        self.assertFalse(out["ok"])
        self.assertIn("company", out["reason"].lower())
        self.assertEqual(client.applied_workflow_transitions, [])

    def test_ambiguous_workflow_denied(self):
        broker, client, _ = make_broker()
        client.workflows = [sample_workflow(name="A"), sample_workflow(name="B")]
        out = broker.dispatch("request_workflow_transition",
                              {"name": "SI-1", "action": "Submit for Approval"})
        self.assertFalse(out["ok"])
        self.assertIn("A", out["reason"])
        self.assertIn("B", out["reason"])

    def test_missing_current_state_denied(self):
        broker, client, _ = make_broker()
        client.workflows = [sample_workflow()]
        # client.workflow_states left empty — get_workflow_state returns None
        out = broker.dispatch("request_workflow_transition",
                              {"name": "SI-1", "action": "Submit for Approval"})
        self.assertFalse(out["ok"])
        self.assertEqual(client.applied_workflow_transitions, [])


class TestMalformedWorkflowConfigE2E(unittest.TestCase):
    """The redteam-proven bypass: a malformed single workflow body ([{}], [None]) previously
    read as "no workflow" — a submit with a valid plan+marker PROCEEDED. And a truthy non-dict
    (["some-string"]) crashed with an uncaught AttributeError. Every garbage shape must now be a
    structured deny end-to-end through dispatch — never ok:true, never a traceback."""

    GARBAGE = ([{}], [None], ["some-string"])

    def test_submit_denies_on_every_garbage_shape(self):
        for garbage in self.GARBAGE:
            broker, client, stores = make_broker()
            plan = broker.dispatch("plan_submit", {"name": "SI-1"})  # planned while config clean
            stores("prod").mint_marker("t", plan["plan_id"], 2_000.0)
            client.workflows = garbage
            out = broker.dispatch("submit_sales_invoice",
                                  {"name": "SI-1", "plan_id": plan["plan_id"], "marker": "t"})
            self.assertFalse(out["ok"], repr(garbage))
            self.assertIn("reason", out)
            self.assertEqual(client.submitted, [], repr(garbage))
            # the marker was never touched — refused before the spine ran
            self.assertEqual(stores("prod").marker_state("t"), "live", repr(garbage))

    def test_cancel_denies_on_every_garbage_shape(self):
        for garbage in self.GARBAGE:
            broker, client, stores = make_broker()
            plan = broker.dispatch("plan_cancel", {"name": "SI-9"})
            stores("prod").mint_marker("t", plan["plan_id"], 2_000.0)
            client.workflows = garbage
            out = broker.dispatch("cancel_sales_invoice",
                                  {"name": "SI-9", "plan_id": plan["plan_id"], "marker": "t"})
            self.assertFalse(out["ok"], repr(garbage))
            self.assertIn("reason", out)
            self.assertEqual(client.cancelled, [], repr(garbage))

    def test_workflow_status_denies_on_every_garbage_shape(self):
        for garbage in self.GARBAGE:
            broker, client, _ = make_broker()
            client.workflows = garbage
            out = broker.dispatch("workflow_status", {"name": "SI-1"})
            self.assertFalse(out["ok"], repr(garbage))
            self.assertIn("reason", out)

    def test_request_workflow_transition_denies_on_every_garbage_shape(self):
        for garbage in self.GARBAGE:
            broker, client, _ = make_broker()
            client.workflows = garbage
            out = broker.dispatch("request_workflow_transition",
                                  {"name": "SI-1", "action": "Anything"})
            self.assertFalse(out["ok"], repr(garbage))
            self.assertIn("reason", out)
            self.assertEqual(client.applied_workflow_transitions, [], repr(garbage))

    def test_plan_submit_flags_malformed_config_but_still_plans(self):
        # Planning is a read: malformed config is flagged (like ambiguous), never crashes, and
        # the actual write is where the refusal lands (proven above).
        for garbage in self.GARBAGE:
            broker, client, _ = make_broker()
            client.workflows = garbage
            out = broker.dispatch("plan_submit", {"name": "SI-1"})
            self.assertTrue(out["ok"], repr(garbage))
            self.assertTrue(any("malformed" in f.lower() for f in out["risk_flags"]),
                            repr(garbage))


class TestNewToolsRequireTheirArgs(unittest.TestCase):
    """The two NEW tools validate their schema-required args before any network call. (The nine
    pre-existing tools are deliberately untouched.)"""

    def test_workflow_status_requires_name(self):
        for args in ({}, {"name": ""}, {"name": "   "}, {"name": None}):
            broker, client, _ = make_broker()
            out = broker.dispatch("workflow_status", args)
            self.assertFalse(out["ok"], repr(args))
            self.assertEqual(out["stage"], "request")
            self.assertIn("required", out["reason"])
            self.assertEqual(client.workflow_calls, [])  # denied BEFORE any network read

    def test_request_workflow_transition_requires_name_and_action(self):
        for args in ({}, {"name": "SI-1"}, {"action": "Go"},
                     {"name": "", "action": "Go"}, {"name": "SI-1", "action": "  "}):
            broker, client, _ = make_broker()
            client.workflows = [sample_workflow()]
            out = broker.dispatch("request_workflow_transition", args)
            self.assertFalse(out["ok"], repr(args))
            self.assertEqual(out["stage"], "request")
            self.assertIn("required", out["reason"])
            self.assertEqual(client.workflow_calls, [])
            self.assertEqual(client.applied_workflow_transitions, [])


class TestConfiguredStateFieldEndToEnd(unittest.TestCase):
    """A workflow's workflow_state_field is CONFIGURABLE — prove the tools read and return the
    configured field (e.g. "approval_state"), never a hardcoded "workflow_state"."""

    def _custom_field_setup(self, client):
        wf = sample_workflow()
        wf["workflow_state_field"] = "approval_state"
        client.workflows = [wf]
        client.workflow_state_field = "approval_state"
        client.workflow_states["SI-1"] = "Draft"

    def test_workflow_status_reads_the_configured_field(self):
        broker, client, _ = make_broker()
        self._custom_field_setup(client)
        out = broker.dispatch("workflow_status", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["current_state"], "Draft")
        self.assertEqual(client.state_field_reads, ["approval_state"])

    def test_request_workflow_transition_reads_and_returns_the_configured_field(self):
        broker, client, stores = make_broker()
        self._custom_field_setup(client)
        out = broker.dispatch("request_workflow_transition",
                              {"name": "SI-1", "action": "Submit for Approval"})
        self.assertTrue(out["ok"])
        self.assertEqual(client.state_field_reads, ["approval_state"])
        self.assertIn("approval_state", out["result"])       # keyed under the configured field
        self.assertNotIn("workflow_state", out["result"])    # never the hardcoded literal
        self.assertEqual(client.applied_workflow_transitions, [("SI-1", "Submit for Approval")])


# ============================================================================================
# BREADTH (Purchase Invoice) — new coverage. Read/plan/execute/amend happy paths mirroring the
# SI ones above, the SUPPORTED_DOCTYPES allowlist deny, and the security headline: a plan bound
# to one doctype must never authorize a submit/cancel of the other.
# ============================================================================================

class TestPurchaseInvoiceReadTier(unittest.TestCase):
    def test_get(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("get_purchase_invoice", {"name": "PI-1"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["doc"]["name"], "PI-1")
        self.assertEqual(out["doc"]["supplier"], "ACME Supply")

    def test_get_error_is_structured_not_a_traceback(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("get_purchase_invoice", {"name": "NOPE"})
        self.assertFalse(out["ok"])
        self.assertIn("404", out["reason"])

    def test_list_uses_the_supplier_party_field(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("list_purchase_invoices", {})
        self.assertTrue(out["ok"])
        self.assertEqual(client.list_calls, [(PURCHASE_INVOICE, "supplier")])

    def test_sales_invoice_list_still_uses_customer(self):
        # Back-compat pin: the SI list tool's party_field is unaffected by PI's addition.
        broker, client, _ = make_broker()
        broker.dispatch("list_sales_invoices", {})
        self.assertEqual(client.list_calls, [(SALES_INVOICE, "customer")])


class TestPurchaseInvoicePlanSubmitAndSubmit(unittest.TestCase):
    def test_plan_records_the_doctype(self):
        broker, _, stores = make_broker()
        out = broker.dispatch("plan_submit", {"name": "PI-1", "pacioli_doctype": "Purchase Invoice"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["doctype"], "Purchase Invoice")
        stored = stores("prod").get_plan(out["plan_id"])
        self.assertEqual(stored.doctype, "Purchase Invoice")
        self.assertEqual(stored.docname, "PI-1")

    def test_plan_submit_default_doctype_is_still_sales_invoice(self):
        # Omitting pacioli_doctype must behave exactly as before this increment.
        broker, _, stores = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertEqual(out["doctype"], "Sales Invoice")
        self.assertEqual(stores("prod").get_plan(out["plan_id"]).doctype, "Sales Invoice")

    def test_full_governed_flow(self):
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_submit",
                               {"name": "PI-1", "pacioli_doctype": "Purchase Invoice"})
        token = "pi-raw-token"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        out = broker.dispatch("submit_purchase_invoice",
                              {"name": "PI-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["docstatus"], 1)
        self.assertEqual(out["result"]["doctype"], "Purchase Invoice")
        self.assertEqual(client.submitted, ["PI-1"])
        store = stores("prod")
        self.assertEqual(store.marker_state(token), "consumed")
        self.assertEqual(store.orphans(), [])

    def test_plan_refuses_non_draft(self):
        broker, client, _ = make_broker()
        client.docs["PI-1"]["docstatus"] = 1
        out = broker.dispatch("plan_submit",
                              {"name": "PI-1", "pacioli_doctype": "Purchase Invoice"})
        self.assertFalse(out["ok"])
        self.assertIn("draft", out["reason"].lower())


class TestPurchaseInvoicePlanCancelAndCancel(unittest.TestCase):
    def test_plan_records_the_reversal_and_the_doctype(self):
        broker, client, stores = make_broker()
        out = broker.dispatch("plan_cancel",
                              {"name": "PI-9", "pacioli_doctype": "Purchase Invoice"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["doctype"], "Purchase Invoice")
        # get_gl_entries is called with the voucher_type filter threaded through.
        self.assertIn((PURCHASE_INVOICE, "PI-9"), client.gl_calls)
        stored = stores("prod").get_plan(out["plan_id"])
        self.assertEqual(stored.op, "cancel")
        self.assertEqual(stored.doctype, "Purchase Invoice")

    def test_full_governed_cancel(self):
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_cancel",
                               {"name": "PI-9", "pacioli_doctype": "Purchase Invoice"})
        token = "pi-cancel-token"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        out = broker.dispatch("cancel_purchase_invoice",
                              {"name": "PI-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["docstatus"], 2)
        self.assertEqual(client.cancelled, ["PI-9"])
        self.assertEqual(stores("prod").marker_state(token), "consumed")


class TestPurchaseInvoiceAmend(unittest.TestCase):
    def _cancelled(self, client, name="PI-9"):
        client.docs[name]["docstatus"] = 2

    def test_amend_creates_the_draft_with_receipts_and_no_marker(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        out = broker.dispatch("amend_purchase_invoice", {"name": "PI-9"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["name"], "PI-9-1")
        self.assertEqual(out["result"]["docstatus"], 0)
        self.assertEqual(out["result"]["doctype"], "Purchase Invoice")
        self.assertEqual(client.amended, ["PI-9"])
        store = stores("prod")
        receipts = store.receipts()
        self.assertEqual([r.kind for r in receipts], ["intent", "outcome"])
        self.assertEqual(receipts[0].body["tool"], "amend")
        self.assertEqual(receipts[0].body["doctype"], "Purchase Invoice")  # amend is NOT via spine
        self.assertEqual(receipts[1].body["result"]["doctype"], "Purchase Invoice")
        self.assertEqual(store.orphans(), [])

    def test_amend_refuses_an_uncancelled_source(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("amend_purchase_invoice", {"name": "PI-1"})  # a draft
        self.assertFalse(out["ok"])
        self.assertIn("cancelled", out["reason"])


# ============================================================================================
# BREADTH (Payment Entry) — a third doctype, riding the same generic handlers as SI/PI, plus two
# Payment-Entry-specific disclosures (plan_submit risk flags, plan_cancel blast-radius listing)
# that live in tools.py because they read Payment Entry's own `references` child-table shape,
# which erpnext.py stays blind to (see pacioli/erpnext.py + pacioli/tools.py docstrings).
# ============================================================================================

class TestPaymentEntryReadTier(unittest.TestCase):
    def test_get(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("get_payment_entry", {"name": "PE-1"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["doc"]["name"], "PE-1")
        self.assertEqual(out["doc"]["party"], "Cust A")

    def test_get_error_is_structured_not_a_traceback(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("get_payment_entry", {"name": "NOPE"})
        self.assertFalse(out["ok"])
        self.assertIn("404", out["reason"])

    def test_list_uses_the_party_field(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("list_payment_entries", {})
        self.assertTrue(out["ok"])
        self.assertEqual(client.list_calls, [(PAYMENT_ENTRY, "party")])


class TestPaymentEntryPlanSubmitAndSubmit(unittest.TestCase):
    def test_plan_records_the_doctype(self):
        broker, _, stores = make_broker()
        out = broker.dispatch("plan_submit", {"name": "PE-1", "pacioli_doctype": "Payment Entry"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["doctype"], "Payment Entry")
        stored = stores("prod").get_plan(out["plan_id"])
        self.assertEqual(stored.doctype, "Payment Entry")
        self.assertEqual(stored.docname, "PE-1")

    def test_full_governed_flow(self):
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_submit", {"name": "PE-1", "pacioli_doctype": "Payment Entry"})
        token = "pe-raw-token"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        out = broker.dispatch("submit_payment_entry",
                              {"name": "PE-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["docstatus"], 1)
        self.assertEqual(out["result"]["doctype"], "Payment Entry")
        self.assertEqual(client.submitted, ["PE-1"])
        store = stores("prod")
        self.assertEqual(store.marker_state(token), "consumed")
        self.assertEqual(store.orphans(), [])

    def test_plan_refuses_non_draft(self):
        broker, client, _ = make_broker()
        client.docs["PE-1"]["docstatus"] = 1
        out = broker.dispatch("plan_submit", {"name": "PE-1", "pacioli_doctype": "Payment Entry"})
        self.assertFalse(out["ok"])
        self.assertIn("draft", out["reason"].lower())

    def test_plan_does_not_flag_a_clean_reference(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "PE-1", "pacioli_doctype": "Payment Entry"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any("exchange_gain_loss" in f for f in out["risk_flags"]))
        self.assertFalse(any("outstanding" in f for f in out["risk_flags"]))

    def test_plan_flags_nonzero_exchange_gain_loss_reference(self):
        broker, client, _ = make_broker()
        client.docs["PE-1"]["references"][0]["exchange_gain_loss"] = 12.5
        out = broker.dispatch("plan_submit", {"name": "PE-1", "pacioli_doctype": "Payment Entry"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("exchange_gain_loss" in f for f in out["risk_flags"]))
        self.assertTrue(any("SI-9" in f for f in out["risk_flags"]))
        self.assertTrue(any("projection-incomplete" in f for f in out["risk_flags"]))

    def test_plan_flags_reference_already_at_zero_outstanding(self):
        broker, client, _ = make_broker()
        client.docs["PE-1"]["references"][0]["outstanding_amount"] = 0.0
        out = broker.dispatch("plan_submit", {"name": "PE-1", "pacioli_doctype": "Payment Entry"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("zero/negative outstanding" in f for f in out["risk_flags"]))

    def test_plan_flags_reference_already_at_negative_outstanding(self):
        broker, client, _ = make_broker()
        client.docs["PE-1"]["references"][0]["outstanding_amount"] = -10.0
        out = broker.dispatch("plan_submit", {"name": "PE-1", "pacioli_doctype": "Payment Entry"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("zero/negative outstanding" in f for f in out["risk_flags"]))

    def test_sales_invoice_plan_is_unaffected_by_pe_risk_logic(self):
        # SI docs have no "references" child table — the PE-only branch must never fire for them.
        broker, _, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["risk_flags"], [])


class TestPaymentEntryPlanCancelAndCancel(unittest.TestCase):
    def test_plan_records_the_reversal_and_the_doctype(self):
        broker, client, stores = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "PE-9", "pacioli_doctype": "Payment Entry"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["doctype"], "Payment Entry")
        self.assertIn((PAYMENT_ENTRY, "PE-9"), client.gl_calls)
        stored = stores("prod").get_plan(out["plan_id"])
        self.assertEqual(stored.op, "cancel")
        self.assertEqual(stored.doctype, "Payment Entry")

    def test_plan_discloses_the_blast_radius_references(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "PE-9", "pacioli_doctype": "Payment Entry"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["references"],
                         [{"reference_doctype": "Sales Invoice", "reference_name": "SI-9",
                           "allocated_amount": 250.0}])

    def test_non_pe_plan_cancel_has_no_references_disclosure(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertTrue(out["ok"])
        self.assertIsNone(out["references"])

    def test_full_governed_cancel(self):
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_cancel", {"name": "PE-9", "pacioli_doctype": "Payment Entry"})
        token = "pe-cancel-token"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        out = broker.dispatch("cancel_payment_entry",
                              {"name": "PE-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["docstatus"], 2)
        self.assertEqual(client.cancelled, ["PE-9"])
        self.assertEqual(stores("prod").marker_state(token), "consumed")


class TestPaymentEntryAmend(unittest.TestCase):
    def _cancelled(self, client, name="PE-9"):
        client.docs[name]["docstatus"] = 2

    def test_amend_creates_the_draft_with_receipts_and_no_marker(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        out = broker.dispatch("amend_payment_entry", {"name": "PE-9"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["docstatus"], 0)
        self.assertEqual(out["result"]["doctype"], "Payment Entry")
        self.assertEqual(client.amended, ["PE-9"])
        store = stores("prod")
        receipts = store.receipts()
        self.assertEqual([r.kind for r in receipts], ["intent", "outcome"])
        self.assertEqual(receipts[0].body["doctype"], "Payment Entry")
        self.assertEqual(store.orphans(), [])

    def test_amend_refuses_an_uncancelled_source(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("amend_payment_entry", {"name": "PE-1"})  # a draft
        self.assertFalse(out["ok"])
        self.assertIn("cancelled", out["reason"])


# ============================================================================================
# BREADTH (Journal Entry) — a fourth doctype, the first with no header-level party and the first
# with its OWN gate (the reserved voucher_type refusal + the independent balance check), not just
# an advisory disclosure. Read/plan/execute/amend happy paths mirror SI/PI/PE; the new coverage
# here is the JE-specific gates and risk flags (see pacioli/tools.py's module docstring).
# ============================================================================================

class TestJournalEntryReadTier(unittest.TestCase):
    def test_get(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("get_journal_entry", {"name": "JE-1"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["doc"]["name"], "JE-1")

    def test_get_error_is_structured_not_a_traceback(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("get_journal_entry", {"name": "NOPE"})
        self.assertFalse(out["ok"])
        self.assertIn("404", out["reason"])

    def test_list_carries_no_party_field(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("list_journal_entries", {})
        self.assertTrue(out["ok"])
        self.assertEqual(client.list_calls, [(JOURNAL_ENTRY, None)])


class TestJournalEntryPlanSubmitAndSubmit(unittest.TestCase):
    def test_plan_records_the_doctype(self):
        broker, _, stores = make_broker()
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["doctype"], "Journal Entry")
        stored = stores("prod").get_plan(out["plan_id"])
        self.assertEqual(stored.doctype, "Journal Entry")
        self.assertEqual(stored.docname, "JE-1")

    def test_full_governed_flow(self):
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        token = "je-raw-token"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        out = broker.dispatch("submit_journal_entry",
                              {"name": "JE-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["docstatus"], 1)
        self.assertEqual(out["result"]["doctype"], "Journal Entry")
        self.assertEqual(client.submitted, ["JE-1"])
        store = stores("prod")
        self.assertEqual(store.marker_state(token), "consumed")
        self.assertEqual(store.orphans(), [])

    def test_submit_passes_the_same_already_fetched_doc_client_submits_with(self):
        # Journal Entry rides the client_rpc submit path (frappe.client.submit), which needs the
        # FULL doc body — and it must be the SAME snapshot current_doc_version/the closed-books/balance
        # checks already validated against, never a fresh re-fetch (that would reopen a TOCTOU gap
        # between the freshness check and the actual write).
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        token = "je-doc-passthrough-token"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        broker.dispatch("submit_journal_entry",
                        {"name": "JE-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertEqual(len(client.submit_docs), 1)
        self.assertEqual(client.submit_docs[0]["name"], "JE-1")
        self.assertEqual(client.submit_docs[0]["total_debit"], 100.0)
        self.assertEqual(client.submit_docs[0]["total_credit"], 100.0)

    def test_plan_refuses_non_draft(self):
        broker, client, _ = make_broker()
        client.docs["JE-1"]["docstatus"] = 1
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertFalse(out["ok"])
        self.assertIn("draft", out["reason"].lower())

    def test_plan_carries_the_standing_fidelity_gap_note(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("on_submit-only" in f for f in out["risk_flags"]))

    def test_sales_invoice_plan_is_unaffected_by_je_risk_logic(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["risk_flags"], [])

    # --- the reserved voucher_type refusal ------------------------------------------------
    def test_plan_refuses_exchange_gain_or_loss_voucher_type(self):
        broker, client, _ = make_broker()
        client.docs["JE-1"]["voucher_type"] = "Exchange Gain Or Loss"
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("Exchange Gain Or Loss", out["reason"])
        self.assertIn("system-reserved", out["reason"])

    def test_submit_refuses_exchange_gain_or_loss_even_with_a_valid_plan_and_marker(self):
        # TOCTOU belt: plan while the voucher_type is still ordinary, then it changes to the
        # reserved value before submit — the governed-write gate must catch it independently.
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        token = "je-token"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        client.docs["JE-1"]["voucher_type"] = "Exchange Gain Or Loss"
        out = broker.dispatch("submit_journal_entry",
                              {"name": "JE-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")  # the EG belt fired, not a coincidental other gate
        self.assertIn("Exchange Gain Or Loss", out["reason"])
        self.assertEqual(client.submitted, [])
        self.assertEqual(stores("prod").marker_state(token), "live")  # never touched

    def test_refused_before_the_native_preview_is_even_called(self):
        broker, client, _ = make_broker()
        client.docs["JE-1"]["voucher_type"] = "Exchange Gain Or Loss"
        preview_calls = []
        real_preview = client.ledger_preview
        client.ledger_preview = lambda **kw: preview_calls.append(kw) or real_preview(**kw)
        broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertEqual(preview_calls, [])

    # --- the independent balance check -----------------------------------------------------
    def test_plan_refuses_an_unbalanced_draft(self):
        broker, client, _ = make_broker()
        client.docs["JE-1"]["accounts"] = [{"account": "Cash", "debit": 100.0, "credit": 0.0},
                                           {"account": "Sales", "debit": 0.0, "credit": 90.0}]
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("100.0", out["reason"])
        self.assertIn("90.0", out["reason"])

    def test_balance_check_ignores_erpnexts_own_cached_totals(self):
        # The check sums the accounts rows itself — a doctored total_debit/total_credit on the
        # parent doc (ERPNext's own cached fields) must NOT paper over unbalanced rows.
        broker, client, _ = make_broker()
        client.docs["JE-1"]["total_debit"] = 100.0
        client.docs["JE-1"]["total_credit"] = 100.0
        client.docs["JE-1"]["accounts"] = [{"account": "Cash", "debit": 100.0, "credit": 0.0},
                                           {"account": "Sales", "debit": 0.0, "credit": 50.0}]
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")

    def test_plan_allows_a_balanced_multi_row_draft(self):
        broker, client, _ = make_broker()
        client.docs["JE-1"]["accounts"] = [{"account": "Cash", "debit": 60.0, "credit": 0.0},
                                           {"account": "Bank", "debit": 40.0, "credit": 0.0},
                                           {"account": "Sales", "debit": 0.0, "credit": 100.0}]
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)

    def test_submit_refuses_an_unbalanced_draft_even_with_a_valid_plan_and_marker(self):
        # TOCTOU belt: plan while balanced, then a row's amount changes before submit.
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        token = "je-token-2"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        client.docs["JE-1"]["accounts"][1]["credit"] = 50.0
        out = broker.dispatch("submit_journal_entry",
                              {"name": "JE-1", "plan_id": plan["plan_id"], "marker": token})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")          # the balance belt, not a coincidental gate
        self.assertIn("total_debit", out["reason"])
        self.assertIn("total_credit", out["reason"])
        self.assertEqual(client.submitted, [])

    def test_balance_check_refuses_a_nan_amount_instead_of_being_defeated_by_it(self):
        # Rigor gap (same class as WG-2a / get_gl_entries): a NaN debit is a float, passes the old
        # isinstance guard, gets summed -> total_debit is NaN -> abs(nan - credit) > epsilon is
        # False, so the imbalance check is DEFEATED and a garbage JE reads as balanced. Must refuse.
        broker, client, _ = make_broker()
        client.docs["JE-1"]["accounts"] = [
            {"account": "Cash", "debit": float("nan"), "credit": 0.0},
            {"account": "Sales", "debit": 0.0, "credit": 100.0}]
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["stage"], "plan")

    def test_balance_check_refuses_a_non_numeric_amount_not_silently_zero(self):
        # The rigor gap: two string amounts were each silently skipped (treated as 0), so 0 == 0
        # read as "balanced" and the check blessed a draft whose amounts it could not even read.
        broker, client, _ = make_broker()
        client.docs["JE-1"]["accounts"] = [
            {"account": "Cash", "debit": "100", "credit": 0.0},
            {"account": "Sales", "debit": 0.0, "credit": "100"}]
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["stage"], "plan")

    def test_balance_check_refuses_a_total_that_overflows_to_inf_via_summation(self):
        # Redteam residual (same class, reached via the ACCUMULATED total, not a single input): each
        # per-row amount is finite, but summing them overflows float64 to inf on BOTH sides ->
        # abs(inf - inf) is NaN -> NaN > epsilon is False -> the imbalance check is DEFEATED. The
        # per-value isfinite guard can't see this; the totals must be checked after the loop too.
        broker, client, _ = make_broker()
        client.docs["JE-1"]["accounts"] = [
            {"account": "A", "debit": 1.5e308, "credit": 0.0},
            {"account": "B", "debit": 1.5e308, "credit": 0.0},
            {"account": "C", "debit": 0.0, "credit": 1.5e308},
            {"account": "D", "debit": 0.0, "credit": 1.5e308}]
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["stage"], "plan")

    def test_balance_check_still_allows_an_absent_unused_side(self):
        # Regression guard: an ERPNext accounts row carries only ONE side; the other is legitimately
        # absent/None (not garbage). That must stay balanced and pass — refuse only a PRESENT,
        # unreadable value, never a missing optional field.
        broker, client, _ = make_broker()
        client.docs["JE-1"]["accounts"] = [
            {"account": "Cash", "debit": 100.0},   # no credit key at all
            {"account": "Sales", "credit": 100.0}]  # no debit key at all
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)

    # --- the >100-row queue disclosure (envelope E1) ------------------------------------------
    def test_over_100_rows_flags_the_background_queue(self):
        broker, client, _ = make_broker()
        rows = client.docs["JE-1"]["accounts"]
        half = 51
        client.docs["JE-1"]["accounts"] = (
            [dict(rows[0], debit=1.0, credit=0.0) for _ in range(half)]
            + [dict(rows[1], debit=0.0, credit=1.0) for _ in range(half)]
        )
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("background worker" in f and "unconfirmed" in f
                            for f in out["risk_flags"]))

    def test_100_rows_or_fewer_not_flagged_for_queue(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any("background worker" in f for f in out["risk_flags"]))

    # --- the Bank/Cash cheque risk flag ------------------------------------------------------
    def test_bank_entry_missing_cheque_info_is_flagged(self):
        broker, client, _ = make_broker()
        client.docs["JE-1"]["voucher_type"] = "Bank Entry"
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("Bank Entry" in f and "cheque" in f for f in out["risk_flags"]))

    def test_bank_entry_with_cheque_info_is_not_flagged_for_it(self):
        broker, client, _ = make_broker()
        client.docs["JE-1"]["voucher_type"] = "Bank Entry"
        client.docs["JE-1"]["cheque_no"] = "1234"
        client.docs["JE-1"]["cheque_date"] = "2026-07-01"
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any("missing cheque_no" in f for f in out["risk_flags"]))

    def test_cash_entry_missing_cheque_info_is_flagged_as_broker_precaution(self):
        broker, client, _ = make_broker()
        client.docs["JE-1"]["voucher_type"] = "Cash Entry"
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        flag = next(f for f in out["risk_flags"] if "Cash Entry" in f and "cheque" in f)
        self.assertIn("does not enforce", flag)  # honest — not an ERPNext-enforced check

    def test_plain_journal_entry_voucher_type_is_not_flagged_for_cheque_info(self):
        # The STANDING fidelity-gap note mentions "cheque" generically (it names every on_submit-
        # only check invisible to the preview) — this pins that the CONDITIONAL missing-cheque
        # flag itself does not fire for a plain "Journal Entry" voucher_type.
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any("missing cheque_no" in f for f in out["risk_flags"]))


class TestJournalEntryPlanCancelAndCancel(unittest.TestCase):
    def test_plan_records_the_reversal_and_the_doctype(self):
        broker, client, stores = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "JE-9", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["doctype"], "Journal Entry")
        self.assertIn((JOURNAL_ENTRY, "JE-9"), client.gl_calls)
        stored = stores("prod").get_plan(out["plan_id"])
        self.assertEqual(stored.op, "cancel")
        self.assertEqual(stored.doctype, "Journal Entry")

    def test_plan_always_flags_the_exchange_gain_loss_auto_cancel_note(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "JE-9", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("Exchange Gain Or Loss" in f and "auto-cancel" in f
                            for f in out["risk_flags"]))

    def test_plan_flags_the_unlink_setting_when_on(self):
        broker, client, _ = make_broker()
        client.accounts_settings["unlink_payment_on_cancellation_of_invoice"] = 1
        out = broker.dispatch("plan_cancel", {"name": "JE-9", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("unlink_payment_on_cancellation_of_invoice" in f and "ON" in f
                            for f in out["risk_flags"]))

    def test_plan_does_not_flag_the_unlink_setting_when_off(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "JE-9", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertFalse(any("unlink_payment_on_cancellation_of_invoice" in f
                             for f in out["risk_flags"]))

    def test_unreadable_accounts_settings_refuses_the_whole_plan(self):
        broker, client, _ = make_broker()
        client.fail_accounts_settings = "HTTP 403: PermissionError"
        out = broker.dispatch("plan_cancel", {"name": "JE-9", "pacioli_doctype": "Journal Entry"})
        self.assertFalse(out["ok"])

    def test_plan_cancel_reads_accounts_settings_for_every_doctype(self):
        # NAMED CASUALTY (F-R1, flips deliberately): this test used to be
        # test_non_je_plan_cancel_never_reads_accounts_settings, pinning that a non-JE plan_cancel
        # NEVER read Accounts Settings (the unlink flag was JE-only). F-R1 widens the read beyond
        # the JE-only gate — EVERY doctype's plan_cancel now reads
        # unlink_payment_on_cancellation_of_invoice ONCE, to feed the new settling-PE disclosure
        # (_settling_reference_risk_flags) that applies to any supported doctype, not just Journal
        # Entry. The old assertion (zero reads for SI) is the opposite of the new, correct
        # behaviour — flipped on purpose, not a regression.
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "SI-9"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(client.accounts_settings_calls), 1)

    def test_full_governed_cancel(self):
        broker, client, stores = make_broker()
        plan = broker.dispatch("plan_cancel", {"name": "JE-9", "pacioli_doctype": "Journal Entry"})
        token = "je-cancel-token"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        out = broker.dispatch("cancel_journal_entry",
                              {"name": "JE-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["docstatus"], 2)
        self.assertEqual(client.cancelled, ["JE-9"])
        self.assertEqual(stores("prod").marker_state(token), "consumed")

    def test_cancel_of_an_exchange_gain_or_loss_je_is_not_refused(self):
        # Cancel is deliberately NOT gated on the reserved voucher_type — ERPNext's own machinery
        # auto-cancels these as a side effect of cancelling whatever they reference.
        broker, client, stores = make_broker()
        client.docs["JE-9"]["voucher_type"] = "Exchange Gain Or Loss"
        plan = broker.dispatch("plan_cancel", {"name": "JE-9", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(plan["ok"], plan)
        token = "je-cancel-egl-token"
        stores("prod").mint_marker(token, plan["plan_id"], expires_at=2_000.0)
        out = broker.dispatch("cancel_journal_entry",
                              {"name": "JE-9", "plan_id": plan["plan_id"], "marker": token})
        self.assertTrue(out["ok"], out)
        self.assertEqual(client.cancelled, ["JE-9"])


class TestJournalEntryAmend(unittest.TestCase):
    def _cancelled(self, client, name="JE-9"):
        client.docs[name]["docstatus"] = 2

    def test_amend_creates_the_draft_with_receipts_and_no_marker(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        out = broker.dispatch("amend_journal_entry", {"name": "JE-9"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["docstatus"], 0)
        self.assertEqual(out["result"]["doctype"], "Journal Entry")
        self.assertEqual(client.amended, ["JE-9"])
        store = stores("prod")
        receipts = store.receipts()
        self.assertEqual([r.kind for r in receipts], ["intent", "outcome"])
        self.assertEqual(receipts[0].body["doctype"], "Journal Entry")
        self.assertEqual(store.orphans(), [])

    def test_amend_refuses_an_uncancelled_source(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("amend_journal_entry", {"name": "JE-1"})  # a draft
        self.assertFalse(out["ok"])
        self.assertIn("cancelled", out["reason"])


class TestUnsupportedDoctypeDenied(unittest.TestCase):
    """Design §B — the broker's OWN 'I've been built and tested for these' allowlist. Distinct
    from (and belt-and-suspenders alongside) pacioli_guard's per-credential resource_doctypes
    grant: this refuses BEFORE any network call, regardless of what the credential could do.

    Delivery Note is the exemplar unsupported doctype (repointed off Payment Entry/Journal Entry
    now that Payment Entry breadth landed — those two are no longer safe placeholders for "not
    supported")."""

    def test_plan_submit_refuses_unsupported_doctype(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "DN-1", "pacioli_doctype": "Delivery Note"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")
        self.assertIn("Delivery Note", out["reason"])
        self.assertIn("Sales Invoice", out["reason"])
        self.assertIn("Purchase Invoice", out["reason"])
        self.assertIn("Payment Entry", out["reason"])
        self.assertIn("Journal Entry", out["reason"])

    def test_plan_cancel_refuses_unsupported_doctype(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "DN-1", "pacioli_doctype": "Delivery Note"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")

    def test_workflow_status_refuses_unsupported_doctype(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("workflow_status",
                              {"name": "DN-1", "pacioli_doctype": "Delivery Note"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")

    def test_request_workflow_transition_refuses_unsupported_doctype(self):
        broker, client, _ = make_broker()
        out = broker.dispatch("request_workflow_transition",
                              {"name": "DN-1", "action": "Go", "pacioli_doctype": "Delivery Note"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")

    def test_non_string_pacioli_doctype_is_denied_not_silently_defaulted(self):
        # A present-but-non-string pacioli_doctype (malformed/hostile MCP arg) must be a named
        # deny, not a quiet Sales Invoice default the caller never asked for — deny-biased, matching
        # plan.check_doctype's treatment of a non-string request-side doctype.
        broker, client, _ = make_broker()
        for bad in ([ "Purchase Invoice" ], 12345, {"x": 1}, True):
            out = broker.dispatch("plan_submit", {"name": "SI-1", "pacioli_doctype": bad})
            self.assertFalse(out["ok"], bad)
            self.assertEqual(out["stage"], "request", bad)
            self.assertIn("must be a string", out["reason"], bad)

    def test_refused_before_any_network_call(self):
        broker, client, _ = make_broker()
        broker.dispatch("plan_submit", {"name": "DN-1", "pacioli_doctype": "Delivery Note"})
        self.assertEqual(client.list_calls, [])
        self.assertEqual(client.workflow_calls, [])

    def test_blank_pacioli_doctype_defaults_to_sales_invoice_not_a_denial(self):
        broker, _, _ = make_broker()
        out = broker.dispatch("plan_submit", {"name": "SI-1", "pacioli_doctype": ""})
        self.assertTrue(out["ok"])
        self.assertEqual(out["doctype"], "Sales Invoice")


class TestCrossDoctypePlanBindingSecurity(unittest.TestCase):
    """THE SECURITY HEADLINE (design §C): a plan is bound to ONE doctype, mirroring the existing
    cross-op guard exactly. A Sales Invoice plan must NEVER authorize a Purchase Invoice
    submit/cancel, and vice versa — even with a fully valid, unexpired, correctly-bound-by-docname
    marker in hand."""

    def test_an_si_plan_cannot_submit_a_purchase_invoice(self):
        broker, client, stores = make_broker()
        # Plan built for Sales Invoice SI-1 (default doctype).
        splan = broker.dispatch("plan_submit", {"name": "SI-1"})
        stores("prod").mint_marker("si-token", splan["plan_id"], 2_000.0)
        # Replayed against submit_purchase_invoice with the SAME name is impossible (SI-1 isn't a
        # PI doc) — the realistic attack is a docname that plausibly exists under both doctypes,
        # or simply presenting the SI plan_id/marker to the PI submit tool. Either way the gate
        # must fire on doctype alone, before any document lookup even matters.
        # plan_submit itself already made one workflow read (its own risk-flag check) — snapshot
        # so the assertion below isolates whether the SUBMIT call made a second one.
        calls_before_submit = list(client.workflow_calls)
        out = broker.dispatch("submit_purchase_invoice",
                              {"name": "SI-1", "plan_id": splan["plan_id"], "marker": "si-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("different document type", out["reason"])
        self.assertEqual(client.submitted, [])
        # The marker was never touched — refused before the spine (and even the workflow read) ran.
        self.assertEqual(stores("prod").marker_state("si-token"), "live")
        self.assertEqual(client.workflow_calls, calls_before_submit)  # no NEW read from submit

    def test_a_pi_plan_cannot_submit_a_sales_invoice(self):
        # The mirror direction — consent to post a Purchase Invoice does not transfer to a Sales
        # Invoice, even against the same docname value.
        broker, client, stores = make_broker()
        pplan = broker.dispatch("plan_submit",
                                {"name": "PI-1", "pacioli_doctype": "Purchase Invoice"})
        stores("prod").mint_marker("pi-token", pplan["plan_id"], 2_000.0)
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "PI-1", "plan_id": pplan["plan_id"], "marker": "pi-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("different document type", out["reason"])
        self.assertEqual(client.submitted, [])
        self.assertEqual(stores("prod").marker_state("pi-token"), "live")

    def test_a_je_plan_cannot_submit_a_sales_invoice(self):
        # The new doctype gets the same proof — check_doctype is fully generic and was never
        # touched for Journal Entry breadth, but the gate deserves its own pin here regardless.
        broker, client, stores = make_broker()
        jplan = broker.dispatch("plan_submit", {"name": "JE-1", "pacioli_doctype": "Journal Entry"})
        stores("prod").mint_marker("je-token", jplan["plan_id"], 2_000.0)
        out = broker.dispatch("submit_sales_invoice",
                              {"name": "JE-1", "plan_id": jplan["plan_id"], "marker": "je-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("different document type", out["reason"])
        self.assertEqual(client.submitted, [])
        self.assertEqual(stores("prod").marker_state("je-token"), "live")

    def test_an_si_cancel_plan_cannot_cancel_a_purchase_invoice(self):
        broker, client, stores = make_broker()
        cplan = broker.dispatch("plan_cancel", {"name": "SI-9"})
        stores("prod").mint_marker("si-cancel-token", cplan["plan_id"], 2_000.0)
        out = broker.dispatch("cancel_purchase_invoice",
                              {"name": "SI-9", "plan_id": cplan["plan_id"],
                               "marker": "si-cancel-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("different document type", out["reason"])
        self.assertEqual(client.cancelled, [])

    def test_doctype_check_runs_before_the_workflow_network_read(self):
        # Cheap local checks first: docname, op, THEN doctype — all before any client call from
        # the SUBMIT tool itself (plan_submit's own risk-flag read is a separate, earlier call).
        broker, client, stores = make_broker()
        splan = broker.dispatch("plan_submit", {"name": "SI-1"})
        stores("prod").mint_marker("t", splan["plan_id"], 2_000.0)
        calls_before_submit = list(client.workflow_calls)
        broker.dispatch("submit_purchase_invoice",
                        {"name": "SI-1", "plan_id": splan["plan_id"], "marker": "t"})
        self.assertEqual(client.workflow_calls, calls_before_submit)


class CascadeClient(FakeClient):
    """FakeClient whose linked-docs are per (doctype, docname), for cascade graph tests."""
    def __init__(self, linked_by):
        super().__init__()
        # SI target 'T' + its Payment Entry dependent 'P', both submitted, same company as REG.
        self.docs.update({
            "T": {"name": "T", "docstatus": 1, "company": "Example Corp",
                  "posting_date": "2026-07-01", "modified": "vT"},
            "P": {"name": "P", "docstatus": 1, "company": "Example Corp",
                  "posting_date": "2026-07-01", "modified": "vP"},
        })
        self.linked_by = linked_by  # {(doctype, docname): [{"doctype","docname"}, ...]}
        # Gap A (envelope E3): docname -> docstatus the cancel RESPONSE reports, or None to omit
        # the key entirely (the run_method/client_rpc response-shape gap the audit named).
        # Absent from this dict = the ordinary confirmed shape (docstatus 2).
        self.cancel_docstatus_override = {}
        self.get_document_calls = []  # tracks every read, incl. the Gap-A governed readback

    def get_document(self, doctype, name):
        self.get_document_calls.append((doctype, name))
        return super().get_document(doctype, name)

    def cancel_document(self, doctype, name):
        from pacioli.erpnext import ErpnextError
        self._mutation_attempted = True
        if self.fail_cancel:
            raise ErpnextError(self.fail_cancel, status=417, answered=True)
        if self.raise_raw_cancel:
            raise self.raise_raw_cancel
        self.cancelled.append(name)
        if name in self.cancel_docstatus_override:
            override = self.cancel_docstatus_override[name]
            resp = {"name": name}
            if override is not None:
                resp["docstatus"] = override
            return resp
        return {"name": name, "docstatus": 2}

    def get_submitted_linked_docs(self, doctype, name):
        from pacioli.erpnext import ErpnextError
        if self.fail_linked:
            raise ErpnextError(self.fail_linked, status=500, answered=True)
        return [dict(d) for d in self.linked_by.get((doctype, name), [])]

    def get_active_workflows(self, doctype):
        # Unlike the base FakeClient (which returns self.workflows unfiltered — harmless for the
        # single-doctype submit/cancel tests), one cascade graph spans MULTIPLE doctypes through a
        # single client instance, so this filters by document_type — mirroring the real ERPNext
        # client, which queries Workflow filtered server-side by document_type (workflow.py's own
        # docstring assumes this: "the fetched list of active workflows for one doctype"). Without
        # this filter, an SI-governing Workflow would also appear to govern an unrelated Payment
        # Entry node in the same graph. Non-dict entries still pass through RAW (same
        # malformed-config semantics as the base FakeClient).
        from pacioli.erpnext import ErpnextError
        self.workflow_calls.append(doctype)
        if self.fail_workflows:
            raise ErpnextError(self.fail_workflows, status=500, answered=True)
        return [dict(w) if isinstance(w, dict) else w for w in self.workflows
                if not isinstance(w, dict) or w.get("document_type") == doctype]


class TestCascadeCancel(unittest.TestCase):
    def test_cascade_cancel_end_to_end(self):
        # Delivery Note is the unmodeled-dependent exemplar here (repointed off Payment Entry,
        # which is now SUPPORTED_DOCTYPES and would flip to "modeled" — see TestUnsupportedDoctypeDenied).
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Delivery Note", "docname": "P"}],
                            ("Delivery Note", "P"): []})
        broker, client, store_provider = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertEqual([n["docname"] for n in plan["graph"]], ["P", "T"])   # dependents first, target last
        self.assertEqual(plan["graph"][0]["coverage"], "generic")            # Delivery Note not modeled
        self.assertEqual(plan["graph"][1]["coverage"], "modeled")            # Sales Invoice modeled
        store_provider("prod").mint_marker("casc-token", plan["plan_id"], expires_at=2_000.0)
        out = broker.dispatch("cascade_cancel",
                              {"name": "T", "plan_id": plan["plan_id"], "marker": "casc-token"})
        self.assertTrue(out["ok"], out)
        self.assertEqual([n["docname"] for n in out["cancelled"]], ["P", "T"])
        self.assertEqual(len(client.cancelled), 2)  # both cancelled at the bench

    def test_cascade_labels_a_journal_entry_dependent_as_modeled(self):
        # Journal Entry breadth: it's now in SUPPORTED_DOCTYPES, so a JE dependent in a cascade
        # graph flips to "modeled" — zero code change in cascade.py itself (confirmed by
        # scout-seams.md: build_cascade's label is driven entirely by the supported_doctypes set
        # tools.py passes in).
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Journal Entry", "docname": "P"}],
                            ("Journal Entry", "P"): []})
        broker, client, store_provider = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(plan["graph"][0]["coverage"], "modeled")

    def test_cascade_fetch_linked_normalizes_frappe_name_shape(self):
        # REGRESSION (bench 2026-07-06, Gate/PHASE-J): ERPNext's real
        # get_submitted_linked_docs returns each dependent in frappe's native shape —
        # {"doctype", "name", "docstatus"} — NOT the {"doctype", "docname"} the pure-core
        # fakes fed. build_cascade keys every node on "docname", so the first real dependent
        # blew up with KeyError: 'docname'. _cascade_fetch_linked now maps name -> docname at
        # the seam. This fake returns the REAL wire shape (name, no docname) to pin it.
        class FrappeShapeCascadeClient(CascadeClient):
            def get_submitted_linked_docs(self, doctype, name):
                raw = {("Sales Invoice", "T"): [{"doctype": "Journal Entry", "name": "P",
                                                 "docstatus": 1}],
                       ("Journal Entry", "P"): []}
                return [dict(d) for d in raw.get((doctype, name), [])]
        cc = FrappeShapeCascadeClient({})
        broker, client, store_provider = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertEqual([n["docname"] for n in plan["graph"]], ["P", "T"])   # name -> docname worked
        self.assertEqual(plan["graph"][0]["coverage"], "modeled")            # JE dependent

    def test_cascade_cancel_refuses_when_graph_over_cap(self):
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        broker, client, store_provider = make_broker(client=cc)
        broker._cascade_max = 1  # graph is T + P = 2 > cap 1
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertFalse(plan["ok"])
        self.assertIn("cap", plan["reason"].lower())

    def test_cascade_refused_when_a_node_workflow_governs_cancel(self):
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        # active Workflow on Sales Invoice that governs cancel (a state -> doc_status "2") — the
        # exact fixture shape from test_cancel_refused_when_workflow_configures_a_cancel_state.
        states = [{"state": "Approved", "doc_status": "1"},
                 {"state": "Cancelled", "doc_status": "2"}]
        transitions = [{"state": "Approved", "action": "Cancel", "next_state": "Cancelled",
                        "allowed": "Sales Manager", "allow_self_approval": "0"}]
        cc.workflows = [sample_workflow(states=states, transitions=transitions)]
        broker, client, store_provider = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertFalse(plan["ok"])
        self.assertEqual(plan.get("stage"), "workflow")
        self.assertIn("Sales Invoice", plan["reason"])

    def test_cascade_cancel_execute_refused_when_a_node_workflow_governs_cancel(self):
        # TOCTOU: plan while UNGOVERNED (so a plan is recorded and a marker can be minted), then a
        # governing-cancel Workflow on Sales Invoice appears BEFORE execute. The execute-time gate
        # (the TOCTOU re-check in _tool_cascade_cancel) must catch it independently of the plan-time
        # gate proven above — nothing should be cancelled.
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        broker, client, store_provider = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        store_provider("prod").mint_marker("casc-token", plan["plan_id"], expires_at=2_000.0)
        # ...then a governing-cancel Workflow on Sales Invoice appears BEFORE execute (TOCTOU) —
        # the exact fixture shape from test_cascade_refused_when_a_node_workflow_governs_cancel.
        states = [{"state": "Approved", "doc_status": "1"},
                 {"state": "Cancelled", "doc_status": "2"}]
        transitions = [{"state": "Approved", "action": "Cancel", "next_state": "Cancelled",
                        "allowed": "Sales Manager", "allow_self_approval": "0"}]
        cc.workflows = [sample_workflow(states=states, transitions=transitions)]
        out = broker.dispatch("cascade_cancel",
                              {"name": "T", "plan_id": plan["plan_id"], "marker": "casc-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out.get("stage"), "workflow")
        self.assertEqual(client.cancelled, [])   # nothing cancelled — gate fired before any cancel

    def test_cascade_plan_refuses_wrong_books_dependent(self):
        # The target 'T' is company-pinned (Example Corp, per REG). Its dependent 'P' belongs to
        # a different company — a cross-company launder if the single-op wrong-books pin isn't
        # applied to every node, not just the target (C1, final-review fix).
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        cc.docs["P"]["company"] = "Other Corp"
        broker, client, store_provider = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertFalse(plan["ok"])
        self.assertEqual(plan.get("stage"), "plan")
        self.assertIn("P", plan["reason"])
        self.assertIn("wrong books", plan["reason"].lower())

    def test_cascade_execute_refuses_wrong_books_dependent_toctou(self):
        # TOCTOU belt: plan while all-same-company (so a plan records + a marker mints), then the
        # dependent's company changes BEFORE execute. The execute-time re-check must catch this
        # independently of the plan-time gate proven above — nothing should be cancelled.
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        broker, client, store_provider = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        store_provider("prod").mint_marker("casc-token", plan["plan_id"], expires_at=2_000.0)
        cc.docs["P"]["company"] = "Other Corp"
        out = broker.dispatch("cascade_cancel",
                              {"name": "T", "plan_id": plan["plan_id"], "marker": "casc-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")            # the wrong-books belt specifically, not
        self.assertIn("wrong books", out["reason"].lower())  # some coincidental gate firing first
        self.assertIn("Other Corp", out["reason"])        # and it names the drifted company
        self.assertEqual(client.cancelled, [])   # nothing cancelled — belt fired before any cancel

    def test_cascade_plan_refuses_deny_biased_on_unreadable_disclosure_reads(self):
        # F-R1 deny-bias for the CASCADE plan path (README: "an unreadable settling-reference or
        # unlink-setting read refuses the WHOLE plan, deny-biased"). Only the single-op path pinned
        # this; cascade's per-node disclosure loop was unexercised for the read-failure case, where
        # an unhandled/swallowed exception is plausible. Each unreadable read must refuse cleanly.
        # fail_gl_entries added under the reconciliation-audit residual (21b7f84): the cascade
        # path's own get_gl_entries call (_cascade_node_meta, per node) had no deny-bias coverage
        # here either — same house pattern, same loop.
        for attr in ("fail_settling_references", "fail_accounts_settings", "fail_gl_entries"):
            cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                                ("Payment Entry", "P"): []})
            setattr(cc, attr, "HTTP 403: PermissionError")
            broker, _, _ = make_broker(client=cc)
            out = broker.dispatch("plan_cascade_cancel",
                                  {"name": "T", "pacioli_doctype": "Sales Invoice"})
            self.assertFalse(out["ok"], f"{attr}: an unreadable disclosure read must refuse the plan")
            self.assertIn("reason", out)  # a structured deny, never a raised/escaped exception


class TestCascadePlanRiskFlags(unittest.TestCase):
    """Gap B (envelope E3): a single-op ``plan_cancel`` on a Journal Entry always discloses the EG
    auto-cancel note + the unlink-on-cancel flag (``_journal_entry_cancel_flags_for_settings``) and
    the stock-reversal flag for an ``update_stock`` doc (``_update_stock_risk_flags``) — but
    ``plan_cascade_cancel``'s per-node flags only ever covered future-posting-date/no-live-GL.
    These pin that every node in the cascade graph gets the SAME doctype-appropriate disclosures,
    prefixed with that node's own docname."""

    def _cascade_client(self, extra_linked=None, je_docname="J"):
        linked = {("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"},
                                           {"doctype": "Journal Entry", "docname": je_docname}],
                 ("Payment Entry", "P"): [], ("Journal Entry", je_docname): []}
        if extra_linked:
            linked.update(extra_linked)
        cc = CascadeClient(linked)
        cc.docs[je_docname] = {"name": je_docname, "docstatus": 1, "company": "Example Corp",
                              "posting_date": "2026-07-01", "modified": f"v{je_docname}",
                              "voucher_type": "Journal Entry"}
        # the target T also moves physical stock on cancel — no extra fixture doc needed to pin
        # the stock disclosure, it rides the node already in the graph.
        cc.docs["T"]["update_stock"] = 1
        cc.docs["T"]["items"] = [{"qty": 5, "uom": "Nos", "item_code": "WIDGET", "warehouse": "WH1"}]
        return cc

    def test_je_node_gets_the_auto_cancel_and_unlink_flags_prefixed_with_its_docname(self):
        cc = self._cascade_client()
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(any(f.startswith("J: ") and "Exchange Gain Or Loss" in f
                            for f in plan["risk_flags"]), plan["risk_flags"])

    def test_je_node_unlink_flag_present_when_setting_is_on(self):
        cc = self._cascade_client()
        cc.accounts_settings["unlink_payment_on_cancellation_of_invoice"] = 1
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(any(f.startswith("J: ") and "unlink_payment_on_cancellation_of_invoice" in f
                            for f in plan["risk_flags"]), plan["risk_flags"])

    def test_update_stock_node_gets_the_stock_disclosure_prefixed_with_its_docname(self):
        cc = self._cascade_client()
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(any(f.startswith("T: ") and "REVERSES its physical stock movement" in f
                            for f in plan["risk_flags"]), plan["risk_flags"])

    def test_nodes_without_je_or_update_stock_get_neither_flag(self):
        cc = self._cascade_client()
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertFalse(any(f.startswith("P: ") and
                            ("Exchange Gain Or Loss" in f or "REVERSES its physical stock" in f)
                            for f in plan["risk_flags"]), plan["risk_flags"])

    def test_je_unlink_setting_is_read_once_for_the_whole_cascade_not_once_per_je_node(self):
        cc = self._cascade_client(
            extra_linked={("Payment Entry", "P"): [{"doctype": "Journal Entry", "docname": "J2"}],
                         ("Journal Entry", "J2"): []})
        cc.docs["J2"] = {"name": "J2", "docstatus": 1, "company": "Example Corp",
                        "posting_date": "2026-07-01", "modified": "vJ2",
                        "voucher_type": "Journal Entry"}
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(any(f.startswith("J: ") and "Exchange Gain Or Loss" in f
                            for f in plan["risk_flags"]))
        self.assertTrue(any(f.startswith("J2: ") and "Exchange Gain Or Loss" in f
                            for f in plan["risk_flags"]))
        self.assertEqual(len(cc.accounts_settings_calls), 1)  # ONE read for the whole graph

    def test_single_op_plan_cancel_behavior_unchanged_by_the_split_helper(self):
        # Regression: _tool_plan_cancel (single-op) must keep reading the setting exactly once and
        # returning the exact same flags it always has — the cascade reuse must not change it.
        broker, client, _ = make_broker()
        out = broker.dispatch("plan_cancel", {"name": "JE-9", "pacioli_doctype": "Journal Entry"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("Exchange Gain Or Loss" in f for f in out["risk_flags"]))
        self.assertEqual(len(client.accounts_settings_calls), 1)

    def test_pe_node_gets_its_settled_reference_flags_prefixed_with_its_docname(self):
        # REDTEAM (medium): the single-op PE plan_cancel discloses which invoices the payment
        # settles (_payment_entry_cancel_references → the top-level `references` key); the
        # cascade memorandum said nothing. Every PE node's settled references must appear as
        # prefixed per-node flags — same act, same-informed consent.
        cc = self._cascade_client()
        cc.docs["P"]["references"] = [
            {"reference_doctype": "Sales Invoice", "reference_name": "SI-9",
             "allocated_amount": 250.0},
            {"reference_doctype": "Purchase Invoice", "reference_name": "PI-9",
             "allocated_amount": 80.0},
        ]
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(any(f.startswith("P: ") and "Sales Invoice SI-9" in f and "250.0" in f
                            for f in plan["risk_flags"]), plan["risk_flags"])
        self.assertTrue(any(f.startswith("P: ") and "Purchase Invoice PI-9" in f and "80.0" in f
                            for f in plan["risk_flags"]), plan["risk_flags"])

    def test_pe_node_without_references_gets_no_reference_flag(self):
        cc = self._cascade_client()  # docs["P"] carries no references field
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertFalse(any("settle" in f or "outstanding" in f for f in plan["risk_flags"]),
                         plan["risk_flags"])

    def test_non_pe_nodes_never_get_reference_flags_even_with_a_stray_references_field(self):
        # The disclosure is doctype-gated (Payment Entry only) — a references-shaped field on a
        # non-PE doc (e.g. some custom field) must not leak into its flags.
        cc = self._cascade_client()
        cc.docs["J"]["references"] = [{"reference_doctype": "Sales Invoice",
                                       "reference_name": "SI-9", "allocated_amount": 1.0}]
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertFalse(any(f.startswith("J: ") and "SI-9" in f for f in plan["risk_flags"]),
                         plan["risk_flags"])


class TestCascadeSettlingPeDisclosure(unittest.TestCase):
    """F-R1 cascade parity: every node in a plan_cascade_cancel graph gets the SAME settling-PE
    disclosure a single-op plan_cancel would give it (_settling_reference_risk_flags), prefixed
    with that node's own docname — mirroring TestCascadePlanRiskFlags' JE-unlink-flag conventions.
    The Payment Ledger Entry read happens PER NODE (each node's own settlement blast radius), but
    the Accounts Settings unlink read stays memoized ONCE for the whole graph (generalizing the
    prior je_settings-only memo variable, TestCascadePlanRiskFlags.
    test_je_unlink_setting_is_read_once_for_the_whole_cascade_not_once_per_je_node)."""

    def _cascade_client(self, extra_linked=None, je_docname="J"):
        linked = {("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"},
                                           {"doctype": "Journal Entry", "docname": je_docname}],
                 ("Payment Entry", "P"): [], ("Journal Entry", je_docname): []}
        if extra_linked:
            linked.update(extra_linked)
        cc = CascadeClient(linked)
        cc.docs[je_docname] = {"name": je_docname, "docstatus": 1, "company": "Example Corp",
                              "posting_date": "2026-07-01", "modified": f"v{je_docname}",
                              "voucher_type": "Journal Entry"}
        return cc

    def test_on_voice_flag_prefixed_with_the_settled_nodes_docname(self):
        cc = self._cascade_client()
        cc.accounts_settings["unlink_payment_on_cancellation_of_invoice"] = 1
        cc.settling_references["T"] = [
            {"voucher_type": "Payment Entry", "voucher_no": "PE-9", "amount": 250.0,
             "account_currency": "USD"}]
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        flag = next(f for f in plan["risk_flags"]
                    if f.startswith("T: ") and "SILENTLY UNLINK" in f)
        self.assertIn("Payment Entry PE-9", flag)
        self.assertIn("250.0", flag)

    def test_off_voice_flag_prefixed_with_the_settled_nodes_docname(self):
        cc = self._cascade_client()
        cc.settling_references["T"] = [
            {"voucher_type": "Payment Entry", "voucher_no": "PE-9", "amount": 250.0,
             "account_currency": "USD"}]
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        flag = next(f for f in plan["risk_flags"]
                    if f.startswith("T: ") and "REFUSE this cancel" in f)
        self.assertIn("PE-9", flag)

    def test_nodes_without_settling_rows_get_no_settling_flags(self):
        cc = self._cascade_client()  # no node carries settling_references
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertFalse(any("SILENTLY UNLINK" in f or "REFUSE this cancel" in f
                             for f in plan["risk_flags"]), plan["risk_flags"])

    def test_ple_read_happens_once_per_node(self):
        cc = self._cascade_client()
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(len(plan["graph"]), 3)  # T, P, J
        self.assertEqual(len(cc.settling_reference_calls), 3)

    def test_unlink_settings_read_once_for_the_whole_graph_not_once_per_node(self):
        cc = self._cascade_client()
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(len(cc.accounts_settings_calls), 1)

    def test_multiple_nodes_each_get_their_own_settling_flags(self):
        cc = self._cascade_client()
        cc.accounts_settings["unlink_payment_on_cancellation_of_invoice"] = 1
        cc.settling_references["T"] = [
            {"voucher_type": "Payment Entry", "voucher_no": "PE-9", "amount": 250.0,
             "account_currency": "USD"}]
        cc.settling_references["P"] = [
            {"voucher_type": "Journal Entry", "voucher_no": "J2", "amount": 10.0,
             "account_currency": "USD"}]
        broker, client, _ = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(any(f.startswith("T: ") and "PE-9" in f for f in plan["risk_flags"]))
        self.assertTrue(any(f.startswith("P: ") and "J2" in f for f in plan["risk_flags"]))


class TestCascadeCancelConfirmation(unittest.TestCase):
    """Gap A glue (envelope E3): the ``_Effects.cancel`` seam in ``_tool_cascade_cancel`` must
    confirm a node actually reached docstatus 2, doing a governed readback (``client.get_document``
    — the same read path the rest of this module already uses, never a new one) when the cancel
    response doesn't carry a usable docstatus at all."""

    def _plan_and_mint(self, cc):
        broker, client, store_provider = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        store_provider("prod").mint_marker("casc-token", plan["plan_id"], expires_at=2_000.0)
        return broker, client, store_provider, plan

    # Every cascade_cancel dispatch does exactly 4 get_document reads before any cancel is even
    # attempted (2 nodes x [rebuild's node_meta, preflight's current_version], both in cancel
    # order P-then-T) — pinned here so the assertions below can name the ONE extra call the Gap-A
    # readback adds, rather than a fragile "some number more than before".
    _PRE_EXECUTE_READS = [("Payment Entry", "P"), ("Sales Invoice", "T"),
                         ("Payment Entry", "P"), ("Sales Invoice", "T")]

    def test_response_missing_docstatus_confirmed_via_readback(self):
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        cc.cancel_docstatus_override["P"] = None  # response omits docstatus entirely
        broker, client, store_provider, plan = self._plan_and_mint(cc)
        # the bench-side cancel actually landed even though the response didn't carry docstatus —
        # simulate that truth for the readback to discover.
        cc.docs["P"]["docstatus"] = 2
        cc.get_document_calls.clear()
        out = broker.dispatch("cascade_cancel",
                              {"name": "T", "plan_id": plan["plan_id"], "marker": "casc-token"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(cc.get_document_calls,
                        self._PRE_EXECUTE_READS + [("Payment Entry", "P")])  # the governed readback
        self.assertEqual([n["docname"] for n in out["cancelled"]], ["P", "T"])

    def test_response_missing_docstatus_and_readback_still_unconfirmed(self):
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        cc.cancel_docstatus_override["P"] = None  # response omits docstatus...
        broker, client, store_provider, plan = self._plan_and_mint(cc)
        # ...and cc.docs["P"]["docstatus"] is left at 1 (submitted) — the readback confirms
        # nothing happened either (still queued).
        cc.get_document_calls.clear()
        out = broker.dispatch("cascade_cancel",
                              {"name": "T", "plan_id": plan["plan_id"], "marker": "casc-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "execute")
        self.assertIn("unconfirmed", out["stopped_at"]["reason"].lower())
        self.assertEqual(out["cancelled"], [])
        self.assertEqual(client.cancelled, ["P"])          # attempted, T never touched
        self.assertEqual(cc.get_document_calls,
                        self._PRE_EXECUTE_READS + [("Payment Entry", "P")])  # readback still fired
        self.assertEqual(store_provider("prod").get_marker("casc-token").state, "consumed")

    def test_response_carries_stale_docstatus_reported_unconfirmed_without_readback(self):
        # The response DOES carry docstatus (just the pre-transition value, e.g. a queued cancel)
        # — this is a usable-but-wrong docstatus, not a missing one, so no readback is needed; the
        # existing response value alone is enough to call it unconfirmed.
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        cc.cancel_docstatus_override["P"] = 1  # unchanged from submitted
        broker, client, store_provider, plan = self._plan_and_mint(cc)
        cc.get_document_calls.clear()
        out = broker.dispatch("cascade_cancel",
                              {"name": "T", "plan_id": plan["plan_id"], "marker": "casc-token"})
        self.assertFalse(out["ok"])
        self.assertIn("unconfirmed", out["stopped_at"]["reason"].lower())
        self.assertEqual(cc.get_document_calls, self._PRE_EXECUTE_READS)  # no extra readback
        self.assertEqual(client.cancelled, ["P"])

    def test_readback_throwing_after_successful_cancel_is_unconfirmed_never_failed(self):
        # REDTEAM (critical, empirically reproduced): cancel_document SUCCEEDS with no docstatus
        # in the response, then the confirmatory readback itself THROWS (timeout/transient 5xx).
        # If that exception propagates, run_cascade's generic failure path fires — which on a
        # FIRST node (cancelled == []) RELEASES the marker and records "failed". But the mutating
        # call already went through: a released grant for an act in flight is the exact inversion
        # the unconfirmed rule exists to prevent. A failed readback must degrade to the
        # unconfirmed branch: marker SPENT, outcome "unconfirmed", fail-stop, error disclosed.
        class ReadbackFailsClient(CascadeClient):
            def get_document(self, doctype, name):
                if name in self.cancelled:  # only the post-cancel readback fails, never the
                    from pacioli.erpnext import ErpnextError  # pre-execute reads
                    raise ErpnextError("HTTP 500: bench hiccup during readback", status=500)
                return super().get_document(doctype, name)
        cc = ReadbackFailsClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry",
                                                            "docname": "P"}],
                                  ("Payment Entry", "P"): []})
        cc.cancel_docstatus_override["P"] = None  # response omits docstatus → readback needed
        broker, client, store_provider, plan = self._plan_and_mint(cc)
        out = broker.dispatch("cascade_cancel",
                              {"name": "T", "plan_id": plan["plan_id"], "marker": "casc-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "execute")
        self.assertIn("unconfirmed", out["stopped_at"]["reason"].lower())
        self.assertIn("readback", out["stopped_at"]["reason"].lower())      # names the real cause
        self.assertIn("bench hiccup during readback", out["stopped_at"]["reason"])
        self.assertEqual(client.cancelled, ["P"])                            # T never touched
        self.assertEqual(out["cancelled"], [])
        # THE critical assertion: the marker is SPENT (consumed), never released back live.
        self.assertEqual(store_provider("prod").get_marker("casc-token").state, "consumed")
        # The outcome receipt is "unconfirmed" (not "failed") and its result carries the
        # readback error so a human reconciler sees the readback itself failed, not a queue.
        outcomes = [r for r in store_provider("prod").receipts() if r.kind == "outcome"]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].body["status"], "unconfirmed")
        self.assertIn("bench hiccup during readback", outcomes[0].body["result"]["readback_error"])

    def test_happy_path_end_to_end_unaffected_by_confirmation_check(self):
        # Regression: the ordinary confirmed shape (docstatus 2 straight off the response) still
        # cascades cleanly end to end — pins TestCascadeCancel.test_cascade_cancel_end_to_end's
        # shape still holds with the new confirm step in the glue.
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        broker, client, store_provider, plan = self._plan_and_mint(cc)
        out = broker.dispatch("cascade_cancel",
                              {"name": "T", "plan_id": plan["plan_id"], "marker": "casc-token"})
        self.assertTrue(out["ok"], out)
        self.assertEqual([n["docname"] for n in out["cancelled"]], ["P", "T"])
        self.assertEqual(len(client.cancelled), 2)


class TestCascadeCancelNoAnswerReadback(unittest.TestCase):
    """Transport taxonomy glue wiring (docs/plans/2026-07-07-transport-taxonomy.md), cascade
    direction: a RAW, unconverted exception from ``cancel_document`` itself (never an
    ``ErpnextError`` at all) is "no answer" — the cascade's own ``_Effects.readback`` must be
    wired through end to end: the marker is ALWAYS spent (never released, even on the first node —
    THE FLIP) and the outcome resolved by a real ``client.get_document`` readback."""

    def _plan_and_mint(self, cc):
        broker, client, store_provider = make_broker(client=cc)
        plan = broker.dispatch("plan_cascade_cancel", {"name": "T", "pacioli_doctype": "Sales Invoice"})
        self.assertTrue(plan["ok"], plan)
        store_provider("prod").mint_marker("casc-token", plan["plan_id"], expires_at=2_000.0)
        return broker, client, store_provider, plan

    def test_raw_exception_on_first_node_now_commits_not_released(self):
        # THE FLIP, cascade direction: a raw exception on the very FIRST node used to release the
        # marker (0 progress). It no longer does — the cancel may already be in motion server-side.
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        cc.raise_raw_cancel = RuntimeError("connection reset by peer")
        broker, client, store_provider, plan = self._plan_and_mint(cc)
        cc.docs["P"]["docstatus"] = 2   # simulate the cancel having actually landed
        out = broker.dispatch("cascade_cancel",
                              {"name": "T", "plan_id": plan["plan_id"], "marker": "casc-token"})
        self.assertFalse(out["ok"])          # still fail-stops
        self.assertEqual(out["stage"], "execute")
        self.assertEqual([n["docname"] for n in out["cancelled"]], ["P"])  # readback confirmed it
        self.assertEqual(client.cancelled, [])   # the fake never reached its own success append
        self.assertEqual(store_provider("prod").get_marker("casc-token").state, "consumed")

    def test_raw_exception_readback_mismatch_is_unconfirmed_marker_still_consumed(self):
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        cc.raise_raw_cancel = RuntimeError("connection reset by peer")
        broker, client, store_provider, plan = self._plan_and_mint(cc)
        # cc.docs["P"]["docstatus"] left at 1 (submitted) — the readback confirms nothing happened
        out = broker.dispatch("cascade_cancel",
                              {"name": "T", "plan_id": plan["plan_id"], "marker": "casc-token"})
        self.assertFalse(out["ok"])
        self.assertIn("unconfirmed", out["stopped_at"]["reason"].lower())
        self.assertEqual(out["cancelled"], [])
        self.assertEqual(store_provider("prod").get_marker("casc-token").state, "consumed")

    def test_answered_error_on_first_node_unaffected_still_releases(self):
        # Regression: the pre-existing fail_cancel shape (an ANSWERED bench refusal) is untouched.
        cc = CascadeClient({("Sales Invoice", "T"): [{"doctype": "Payment Entry", "docname": "P"}],
                            ("Payment Entry", "P"): []})
        cc.fail_cancel = "HTTP 417: LinkExistsError"
        broker, client, store_provider, plan = self._plan_and_mint(cc)
        out = broker.dispatch("cascade_cancel",
                              {"name": "T", "plan_id": plan["plan_id"], "marker": "casc-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(store_provider("prod").get_marker("casc-token").state, "live")


class ReconcileClient(FakeClient):
    """FakeClient extended with invoice/payment fixtures carrying outstanding_amount/
    unallocated_amount (F-R2) and a fake ``reconcile`` transport that mutates the invoice's
    outstanding_amount by the allocated amount — simulating the bench's own write, so a readback
    (``get_document`` again) sees the post-write state, exactly like the real bench would."""

    def __init__(self):
        super().__init__()
        self.docs.update({
            "INV1": {"name": "INV1", "docstatus": 1, "company": "Example Corp",
                     "posting_date": "2026-07-01", "outstanding_amount": 100.0,
                     "modified": "vINV1", "customer": "Cust A"},
            "INV2": {"name": "INV2", "docstatus": 1, "company": "Example Corp",
                     "posting_date": "2026-07-01", "outstanding_amount": 200.0,
                     "modified": "vINV2", "customer": "Cust A"},
            "PAY1": {"name": "PAY1", "docstatus": 1, "company": "Example Corp",
                     "posting_date": "2026-07-01", "unallocated_amount": 500.0,
                     "modified": "vPAY1", "party": "Cust A"},
            "PAY2": {"name": "PAY2", "docstatus": 1, "company": "Example Corp",
                     "posting_date": "2026-07-01", "unallocated_amount": 500.0,
                     "modified": "vPAY2", "party": "Cust A"},
        })
        self.reconcile_calls = []
        self.fail_reconcile = None
        self.raise_raw_reconcile = None

    def reconcile(self, company, party_type, party, receivable_payable_account, allocations):
        from pacioli.erpnext import ErpnextError
        self._mutation_attempted = True
        self.reconcile_calls.append({
            "company": company, "party_type": party_type, "party": party,
            "receivable_payable_account": receivable_payable_account,
            "allocations": [dict(r) for r in allocations],
        })
        if self.fail_reconcile:
            raise ErpnextError(self.fail_reconcile, status=417, answered=True)
        if self.raise_raw_reconcile:
            raise self.raise_raw_reconcile
        for row in allocations:
            inv = self.docs.get(row["invoice_no"])
            if inv is not None:
                inv["outstanding_amount"] = inv.get("outstanding_amount", 0.0) - row["allocated_amount"]
        return {"name": "new-payment-reconciliation"}


def _allocation_row(payment_no="PAY1", invoice_no="INV1", amount=40.0,
                    payment_type="Payment Entry", invoice_type="Sales Invoice"):
    return {"payment_type": payment_type, "payment_no": payment_no,
           "invoice_type": invoice_type, "invoice_no": invoice_no,
           "allocated_amount": amount}


def _reconcile_args(allocations=None, party_type="Customer", party="Cust A",
                    company="Example Corp", receivable_payable_account="Debtors - EC"):
    return {"party_type": party_type, "party": party, "company": company,
           "receivable_payable_account": receivable_payable_account,
           "allocations": allocations if allocations is not None else [_allocation_row()]}


class TestPlanReconcile(unittest.TestCase):
    def test_happy_path_builds_graph_with_fresh_read_fields(self):
        rc = ReconcileClient()
        broker, client, store_provider = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args())
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["total"], 1)
        plan = store_provider("prod").get_plan(out["plan_id"])
        self.assertEqual(plan.op, "reconcile")
        self.assertEqual(plan.doctype, "Payment Reconciliation")
        self.assertEqual(plan.party_type, "Customer")
        self.assertEqual(plan.party, "Cust A")
        self.assertEqual(plan.receivable_payable_account, "Debtors - EC")
        self.assertEqual(plan.company, "Example Corp")
        self.assertEqual(len(plan.graph), 1)
        node = plan.graph[0]
        self.assertEqual(node["payment_type"], "Payment Entry")
        self.assertEqual(node["payment_no"], "PAY1")
        self.assertEqual(node["payment_version"], "vPAY1")
        self.assertEqual(node["payment_date"], "2026-07-01")
        self.assertEqual(node["invoice_type"], "Sales Invoice")
        self.assertEqual(node["invoice_no"], "INV1")
        self.assertEqual(node["invoice_version"], "vINV1")
        self.assertEqual(node["invoice_date"], "2026-07-01")
        self.assertEqual(node["allocated_amount"], 40.0)
        self.assertEqual(node["invoice_outstanding"], 100.0)
        self.assertEqual(node["payment_unallocated"], 500.0)
        self.assertEqual(node["company"], "Example Corp")
        # memorandum + the standing hazard notes
        self.assertTrue(any("PAY1" in m and "INV1" in m for m in out["memorandum"]))
        self.assertTrue(any("freeze belt" in f.lower() for f in out["risk_flags"]))
        self.assertTrue(any("journal entr" in f.lower() and "gain" in f.lower()
                            for f in out["risk_flags"]))
        self.assertNotIn("marker", out)  # planning is a read — no marker minted or required

    def test_freeze_belt_disclosure_is_honest_about_per_account_scope(self):
        # FIX 2 (honesty): the broker independently enforces the COMPANY/PERIOD-freeze refusal
        # (closed Accounting Period / PCV / company frozen-till) -- keep that claim, it is
        # source-verified true. But it does NOT read Account.freeze_account, so the PER-ACCOUNT
        # frozen-account check ERPNext bypasses (adv_adj=1) must be disclosed as NOT yet
        # independently enforced -- never claimed as covered by "enforces ... itself here".
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args())
        self.assertTrue(out["ok"], out)
        flags = [f.lower() for f in out["risk_flags"]]
        # The company/period-freeze enforcement claim stays -- it's true.
        self.assertTrue(any("period" in f and "enforce" in f for f in flags),
                        out["risk_flags"])
        # The per-account claim must be honestly scoped: disclosed, but NOT (yet) enforced.
        per_account_flags = [f for f in flags if "per-account" in f or "freeze_account" in f]
        self.assertTrue(per_account_flags, out["risk_flags"])
        self.assertTrue(any("not" in f and ("yet" in f or "independently enforced" in f)
                            for f in per_account_flags), out["risk_flags"])
        # Must NOT claim the broker enforces the per-account check itself.
        self.assertFalse(any("per-account" in f and "enforces" in f and "not" not in f
                             for f in flags), out["risk_flags"])

    def test_memorandum_shows_cumulative_post_outstanding_per_invoice(self):
        # FIX 1 (plan-time honesty): two rows settling the SAME invoice must show the CUMULATIVE
        # post-outstanding in the memorandum, not each row's own outstanding-minus-its-own-amount
        # (which would misleadingly show "100.0 -> 60.0" for BOTH rows instead of the real
        # aggregate effect: 100 -> 60 after row 1, then 60 -> 20 after row 2).
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        rows = [_allocation_row(payment_no="PAY1", invoice_no="INV1", amount=40.0),
               _allocation_row(payment_no="PAY2", invoice_no="INV1", amount=40.0)]
        out = broker.dispatch("plan_reconcile", _reconcile_args(allocations=rows))
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(out["memorandum"]), 2)
        self.assertIn("100.0 -> 60.0", out["memorandum"][0])
        self.assertIn("60.0 -> 20.0", out["memorandum"][1])

    def test_refuses_wrong_books(self):
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args(company="Other Corp"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("wrong books", out["reason"].lower())

    def test_refuses_on_unreadable_invoice(self):
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile",
                              _reconcile_args(allocations=[_allocation_row(invoice_no="NOPE")]))
        self.assertFalse(out["ok"])
        self.assertIn("not found", out["reason"].lower())

    def test_refuses_on_unreadable_payment(self):
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile",
                              _reconcile_args(allocations=[_allocation_row(payment_no="NOPE")]))
        self.assertFalse(out["ok"])
        self.assertIn("not found", out["reason"].lower())

    def test_refuses_cross_company_invoice(self):
        rc = ReconcileClient()
        rc.docs["INV1"]["company"] = "Other Corp"
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("cross-company", out["reason"].lower())

    def test_refuses_cross_company_payment(self):
        rc = ReconcileClient()
        rc.docs["PAY1"]["company"] = "Other Corp"
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("cross-company", out["reason"].lower())

    def test_refuses_mismatched_invoice_party(self):
        # FIX 4: the declared party must match the invoice's OWN customer/supplier field — a
        # wrong party name must never land in the permanent receipt chain.
        rc = ReconcileClient()
        rc.docs["INV1"]["customer"] = "Someone Else"
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("does not match the declared party", out["reason"])

    def test_refuses_missing_invoice_party_field(self):
        # Deny-biased: a missing party field on the invoice refuses, same as any other
        # unverifiable gate source.
        rc = ReconcileClient()
        del rc.docs["INV1"]["customer"]
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("does not match the declared party", out["reason"])

    def test_refuses_mismatched_payment_party(self):
        rc = ReconcileClient()
        rc.docs["PAY1"]["party"] = "Someone Else"
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("does not match the declared party", out["reason"])

    def test_refuses_missing_payment_party_field(self):
        rc = ReconcileClient()
        del rc.docs["PAY1"]["party"]
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")
        self.assertIn("does not match the declared party", out["reason"])

    def test_refuses_missing_invoice_outstanding_field(self):
        rc = ReconcileClient()
        del rc.docs["INV1"]["outstanding_amount"]
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")

    def test_refuses_missing_payment_unallocated_field(self):
        # FIX 5 — the payment-side mirror of test_refuses_missing_invoice_outstanding_field.
        rc = ReconcileClient()
        del rc.docs["PAY1"]["unallocated_amount"]
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")

    def test_refuses_missing_payment_modified_field(self):
        rc = ReconcileClient()
        del rc.docs["PAY1"]["modified"]
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args())
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")

    def test_refuses_empty_allocations(self):
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args(allocations=[]))
        self.assertFalse(out["ok"])

    def test_refuses_blank_required_string_field(self):
        # FIX 5 — a blank (whitespace-only counts too, via .strip()) required string field must
        # be refused at request, before any bench read.
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        bad = _allocation_row(payment_no="")
        out = broker.dispatch("plan_reconcile", _reconcile_args(allocations=[bad]))
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")
        self.assertIn("payment_no", out["reason"])

    def test_refuses_allocations_wrong_type_string(self):
        # FIX 5 — `allocations` must be a list; a bare string is not an empty-list false-y hit,
        # it must be explicitly refused as the wrong type.
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args(allocations="not-a-list"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")

    def test_refuses_allocations_wrong_type_dict(self):
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", _reconcile_args(allocations={"not": "a list"}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "request")

    def test_refuses_non_numeric_allocated_amount(self):
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        bad = _allocation_row()
        bad["allocated_amount"] = "forty"
        out = broker.dispatch("plan_reconcile", _reconcile_args(allocations=[bad]))
        self.assertFalse(out["ok"])

    def test_missing_required_args_refused(self):
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        out = broker.dispatch("plan_reconcile", {})
        self.assertFalse(out["ok"])

    def test_over_allocation_disclosed_not_refused_at_plan_time(self):
        # PLAN is disclosure-only: an amount over the fresh outstanding still plans (ok: True),
        # flagged, with the authoritative refusal deferred to execute.
        rc = ReconcileClient()
        broker, client, _ = make_broker(client=rc)
        row = _allocation_row(amount=500.0)  # over INV1's 100.0 outstanding
        out = broker.dispatch("plan_reconcile", _reconcile_args(allocations=[row]))
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("outstanding" in f.lower() and "refused at execute" in f.lower()
                            for f in out["risk_flags"]))

    def test_journal_entry_payment_is_refused_deferred(self):
        # First-slice allowlist: a JE payment is refused up front (its available amount is not a
        # simple unallocated_amount field) — never reaches a bench read or the reconcile call.
        rc = ReconcileClient()
        broker, _, _ = make_broker(client=rc)
        row = _allocation_row(payment_type="Journal Entry")
        out = broker.dispatch("plan_reconcile", _reconcile_args(allocations=[row]))
        self.assertFalse(out["ok"], out)
        self.assertIn("deferred extension", out["reason"].lower())
        self.assertFalse(rc.reconcile_calls)

    def test_unsupported_invoice_type_is_refused(self):
        rc = ReconcileClient()
        broker, _, _ = make_broker(client=rc)
        row = _allocation_row(invoice_type="Journal Entry")
        out = broker.dispatch("plan_reconcile", _reconcile_args(allocations=[row]))
        self.assertFalse(out["ok"], out)
        self.assertIn("not reconcilable", out["reason"].lower())


class TestReconcileExecute(unittest.TestCase):
    def _plan_and_mint(self, rc=None, allocations=None, **kwargs):
        rc = rc or ReconcileClient()
        broker, client, store_provider = make_broker(client=rc)
        plan = broker.dispatch("plan_reconcile", _reconcile_args(allocations=allocations, **kwargs))
        self.assertTrue(plan["ok"], plan)
        store_provider("prod").mint_marker("recon-token", plan["plan_id"], expires_at=2_000.0)
        return broker, client, store_provider, plan

    def test_happy_path_committed(self):
        broker, client, store_provider, plan = self._plan_and_mint()
        out = broker.dispatch("reconcile", {"plan_id": plan["plan_id"], "marker": "recon-token"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["stage"], "done")
        self.assertEqual(len(out["confirmed"]), 1)
        self.assertEqual(out["confirmed"][0]["outstanding"], 60.0)  # 100 - 40
        self.assertEqual(len(client.reconcile_calls), 1)
        self.assertEqual(store_provider("prod").get_marker("recon-token").state, "consumed")

    def test_reconcile_uses_pinned_graph_never_args(self):
        # THE safety control: even if the caller stuffs an "allocations" key into the execute-time
        # args, it must be completely ignored — the reconcile call is built from the PINNED plan
        # graph alone.
        broker, client, store_provider, plan = self._plan_and_mint()
        evil_args = {"plan_id": plan["plan_id"], "marker": "recon-token",
                    "allocations": [_allocation_row(payment_no="EVIL", invoice_no="EVIL",
                                                    amount=999999.0)]}
        out = broker.dispatch("reconcile", evil_args)
        self.assertTrue(out["ok"], out)
        sent = client.reconcile_calls[0]["allocations"]
        # P7 semantic keys: payment_unallocated/invoice_outstanding ride along too — still from
        # the PINNED graph (INV1's plan-time outstanding=100.0, PAY1's plan-time
        # unallocated=500.0), never from the evil "EVIL"/999999.0 row stuffed into the
        # execute-time args above. (Wire translation to ERPNext's amount/unreconciled_amount
        # field names happens in erpnext.py's reconcile(), not here.)
        self.assertEqual(sent, [{"payment_type": "Payment Entry", "payment_no": "PAY1",
                                 "invoice_type": "Sales Invoice", "invoice_no": "INV1",
                                 "allocated_amount": 40.0, "payment_unallocated": 500.0,
                                 "invoice_outstanding": 100.0}])

    def test_wrong_op_plan_refused(self):
        rc = ReconcileClient()
        broker, client, store_provider = make_broker(client=rc)
        # a plan_submit plan_id presented to reconcile — cross-op laundering must be refused.
        submit_plan = broker.dispatch("plan_submit", {"name": "SI-1"})
        self.assertTrue(submit_plan["ok"], submit_plan)
        store_provider("prod").mint_marker("recon-token", submit_plan["plan_id"], expires_at=2_000.0)
        out = broker.dispatch("reconcile",
                              {"plan_id": submit_plan["plan_id"], "marker": "recon-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "plan")

    def test_stale_invoice_refuses_toctou(self):
        broker, client, store_provider, plan = self._plan_and_mint()
        client.docs["INV1"]["modified"] = "CHANGED"
        out = broker.dispatch("reconcile", {"plan_id": plan["plan_id"], "marker": "recon-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "fresh")
        self.assertEqual(client.reconcile_calls, [])
        self.assertEqual(store_provider("prod").get_marker("recon-token").state, "live")

    def test_over_allocation_vs_live_read_refuses_at_execute(self):
        # The ceiling comes from a FRESH read at execute, never the plan-time disclosed amount —
        # simulate the outstanding having dropped between plan and execute.
        broker, client, store_provider, plan = self._plan_and_mint()
        client.docs["INV1"]["outstanding_amount"] = 10.0  # was 100.0 at plan time; alloc is 40.0
        out = broker.dispatch("reconcile", {"plan_id": plan["plan_id"], "marker": "recon-token"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "allocation")
        self.assertEqual(client.reconcile_calls, [])
        self.assertEqual(store_provider("prod").get_marker("recon-token").state, "live")

    def test_multi_row_all_confirmed(self):
        rows = [_allocation_row(payment_no="PAY1", invoice_no="INV1", amount=40.0),
               _allocation_row(payment_no="PAY2", invoice_no="INV2", amount=60.0)]
        broker, client, store_provider, plan = self._plan_and_mint(allocations=rows)
        out = broker.dispatch("reconcile", {"plan_id": plan["plan_id"], "marker": "recon-token"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(out["confirmed"]), 2)


class TestAmendWorkflowSeat(unittest.TestCase):
    """The F1 fix at the tool layer (2026-07-17, found by the first dogfood drive): an amendment
    created under an ACTIVE workflow was born with no workflow state — stuck, since
    request_workflow_transition (correctly) refuses a null state. Now the amend seats the draft
    at the workflow's initial state (frappe's own states[0] convention), discloses the seat in
    the result and the intent receipt, and — deny-biased — refuses to create the draft at all
    when the workflow config is ambiguous, malformed, unreadable, or unseatable. No workflow
    configured = byte-identical to before (no seat, no disclosure)."""

    def _cancelled(self, client, name="SI-9"):
        client.docs[name]["docstatus"] = 2

    def test_amend_under_an_active_workflow_seats_the_initial_state(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        client.workflows = [sample_workflow()]
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(client.amend_seats, [("workflow_state", "Draft")])
        self.assertEqual(client.docs["SI-9-1"]["workflow_state"], "Draft")
        self.assertEqual(out["result"]["workflow_seat"],
                         {"field": "workflow_state", "state": "Draft", "confirmed": True})
        receipts = stores("prod").receipts()
        self.assertEqual([r.kind for r in receipts], ["intent", "outcome"])
        self.assertEqual(receipts[0].body["workflow_seat"],
                         {"field": "workflow_state", "state": "Draft"})

    def test_amend_with_no_workflow_is_byte_identical_to_before(self):
        broker, client, _ = make_broker()
        self._cancelled(client)
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(client.amend_seats, [None])
        self.assertNotIn("workflow_seat", out["result"])
        self.assertNotIn("workflow_state", client.docs["SI-9-1"])

    def test_amend_refuses_ambiguous_workflow_config(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        client.workflows = [sample_workflow(), sample_workflow(name="SI Approval Two")]
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "workflow")
        self.assertEqual(client.amended, [])
        self.assertEqual(stores("prod").receipts(), [])  # refused BEFORE intent — no orphan

    def test_amend_refuses_malformed_workflow_config(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        client.workflows = [{}]
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "workflow")
        self.assertEqual(client.amended, [])
        self.assertEqual(stores("prod").receipts(), [])

    def test_amend_refuses_an_unreadable_workflow_list(self):
        broker, client, stores = make_broker()
        self._cancelled(client)
        client.fail_workflows = "HTTP 403: PermissionError"
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertFalse(out["ok"])
        self.assertEqual(client.amended, [])
        self.assertEqual(stores("prod").receipts(), [])

    def test_amend_refuses_an_unseatable_workflow_naming_why(self):
        # First state maps to doc_status "1" — a draft must never wear a submitted state's name,
        # and creating the draft UNSEATED would silently recreate the stuck F1 shape.
        broker, client, stores = make_broker()
        self._cancelled(client)
        client.workflows = [sample_workflow(
            states=[{"state": "Posted", "doc_status": "1"}], transitions=[])]
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "workflow")
        self.assertIn("SI Approval", out["reason"])
        self.assertEqual(client.amended, [])
        self.assertEqual(stores("prod").receipts(), [])

    def test_amend_refuses_a_seat_field_that_reenters_the_strip_surface(self):
        # Review finding [0]: workflow_state_field = "amended_from" (misconfigured or malicious)
        # must refuse BEFORE the intent — never overwrite the amendment linkage.
        broker, client, stores = make_broker()
        self._cancelled(client)
        wf = sample_workflow()
        wf["workflow_state_field"] = "amended_from"
        client.workflows = [wf]
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "workflow")
        self.assertIn("amended_from", out["reason"])
        self.assertEqual(client.amended, [])
        self.assertEqual(stores("prod").receipts(), [])  # refused BEFORE intent — no orphan

    def test_amend_seats_through_padded_workflow_config(self):
        # Review findings [1][2][5]: padded field/state names in the workflow config must
        # normalise to the same strings every reader uses — never seat at "Draft ".
        broker, client, _ = make_broker()
        self._cancelled(client)
        wf = sample_workflow(states=[{"state": " Draft ", "doc_status": "0"},
                                     {"state": "Approved", "doc_status": "1"}])
        wf["workflow_state_field"] = " workflow_state "
        client.workflows = [wf]
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(client.amend_seats, [("workflow_state", "Draft")])

    def test_amend_seat_is_confirmed_against_the_created_document(self):
        broker, client, _ = make_broker()
        self._cancelled(client)
        client.workflows = [sample_workflow()]
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["result"]["workflow_seat"]["confirmed"])

    def test_a_seat_the_bench_silently_dropped_is_disclosed_unconfirmed(self):
        # Review finding [8] (the E1 discipline): the disclosure must come from the bench's
        # ANSWER, not the request — a dropped seat must never ride a committed receipt as fact.
        broker, client, stores = make_broker()
        self._cancelled(client)
        client.workflows = [sample_workflow()]
        client.drop_seat = True
        out = broker.dispatch("amend_sales_invoice", {"name": "SI-9"})
        self.assertTrue(out["ok"], out)   # the draft itself WAS created
        self.assertFalse(out["result"]["workflow_seat"]["confirmed"])
        receipts = stores("prod").receipts()
        self.assertFalse(receipts[1].body["result"]["workflow_seat"]["confirmed"])

    def test_workflow_status_reads_a_padded_state_field_config(self):
        broker, client, _ = make_broker()
        wf = sample_workflow()
        wf["workflow_state_field"] = " workflow_state "
        client.workflows = [wf]
        client.workflow_states["SI-1"] = "Draft"
        out = broker.dispatch("workflow_status", {"name": "SI-1"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["current_state"], "Draft")


if __name__ == "__main__":
    unittest.main()
