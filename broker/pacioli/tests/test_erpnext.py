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

from pacioli.erpnext import (JOURNAL_ENTRY, PAYMENT_ENTRY, PURCHASE_INVOICE, SALES_INVOICE,
                             SUPPORTED_DOCTYPES, ErpnextClient, ErpnextError)

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
                         {"party_field": "customer", "submit_via": "run_method"})
        self.assertEqual(SUPPORTED_DOCTYPES[PURCHASE_INVOICE],
                         {"party_field": "supplier", "submit_via": "run_method"})
        self.assertEqual(SUPPORTED_DOCTYPES[PAYMENT_ENTRY],
                         {"party_field": "party", "submit_via": "run_method"})

    def test_journal_entry_has_no_party_field(self):
        # Journal Entry carries no header-level party at all (only per-line party in its
        # `accounts` child table) — party_field is None, not a missing key or an empty string.
        # submit_via is "client_rpc" — the ONLY doctype not on the URL-path run_method surface,
        # because JournalEntry.submit()/.cancel() override the base Document methods without
        # @frappe.whitelist() (SCOPED-TOKEN-PROOF.md PHASE L), 403ing the run_method vector.
        self.assertEqual(SUPPORTED_DOCTYPES[JOURNAL_ENTRY],
                         {"party_field": None, "submit_via": "client_rpc"})

    def test_exactly_four_doctypes_supported(self):
        self.assertEqual(set(SUPPORTED_DOCTYPES),
                         {SALES_INVOICE, PURCHASE_INVOICE, PAYMENT_ENTRY, JOURNAL_ENTRY})

    def test_only_journal_entry_uses_client_rpc(self):
        for doctype, cfg in SUPPORTED_DOCTYPES.items():
            expected = "client_rpc" if doctype == JOURNAL_ENTRY else "run_method"
            self.assertEqual(cfg["submit_via"], expected, doctype)


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
