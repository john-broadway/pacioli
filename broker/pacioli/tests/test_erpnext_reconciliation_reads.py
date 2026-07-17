# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The Close, Half 2 — the Reconciliation: the READ LAYER's period-sweep tests.

Two new ``ErpnextClient`` reads, over the same injected-transport fake every other test in this
package uses (nothing here touches a network; the real shapes are knowledge-pinned, proven only
against a live bench — see ``erpnext.py``'s module docstring, SPEC §7):

* :meth:`ErpnextClient.sweep_gl_entries` (Fork I) — the CREATION-window movement sweep: every GL
  Entry row *written* for a company inside ``[since, until]``, axis ``creation`` (not
  ``posting_date``) — a backdated/out-of-band write can carry any posting_date while its creation
  timestamp pins exactly when it landed on the bench.
* :meth:`ErpnextClient.get_reposts` (Fork II) — the Repost Accounting Ledger read: explains a
  Fork-IV second generation (a repost re-derives a voucher's GL rows in place). Two-step (LIST then
  per-doc GET for the ``vouchers`` child table), the same shape ``get_period_locks``/
  ``get_active_workflows`` already use.

Both mirror the house discipline pinned by ``get_gl_entries``/``get_settling_references``: explicit
``fields``/``filters`` JSON, ``limit_page_length: "0"`` (F-V1 — a gate-feeding LIST read must pin
the full page), structured-deny-on-non-list-body BEFORE any caller loop, per-row validation where a
malformed value raises rather than silently coercing to zero/blank/absent.
"""
import json
import unittest

from pacioli.erpnext import ErpnextClient, ErpnextError

SINCE = "2026-07-01 00:00:00"
UNTIL = "2026-07-10 23:59:59"


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


def gl_row(**overrides):
    row = {
        "voucher_type": "Sales Invoice", "voucher_no": "SI-9", "account": "Debtors",
        "debit": 250.0, "credit": 0.0, "posting_date": "2026-07-05", "creation": SINCE,
        "owner": "agent@example.com", "modified": SINCE, "modified_by": "agent@example.com",
        "is_cancelled": 0, "party_type": "Customer", "party": "CUST-1",
    }
    row.update(overrides)
    return row


class TestSweepGlEntriesRequestShape(unittest.TestCase):
    def test_url_method_and_page_pin(self):
        c, t = client([(200, {"data": []})])
        c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        call = t.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertIn("/api/resource/GL%20Entry", call["url"])
        self.assertEqual(call["params"]["limit_page_length"], "0")

    def test_filters_on_creation_window_not_posting_date(self):
        c, t = client([(200, {"data": []})])
        c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        filters = json.loads(t.calls[0]["params"]["filters"])
        self.assertIn(["company", "=", "Acme Co"], filters)
        self.assertIn(["creation", ">=", SINCE], filters)
        self.assertIn(["creation", "<=", UNTIL], filters)
        self.assertEqual(len(filters), 3)
        # The axis is creation, never posting_date — a posting_date filter here would be the wrong
        # sweep entirely (Fork I is explicitly the creation-window read).
        for f in filters:
            self.assertNotEqual(f[0], "posting_date")

    def test_full_field_list_pinned(self):
        c, t = client([(200, {"data": []})])
        c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        fields = json.loads(t.calls[0]["params"]["fields"])
        self.assertEqual(fields, [
            "voucher_type", "voucher_no", "account", "debit", "credit", "posting_date",
            "creation", "owner", "modified", "modified_by", "is_cancelled", "party_type", "party",
        ])

    def test_since_and_until_travel_verbatim_not_reformatted(self):
        # This method invents/formats no clock — whatever string the caller supplies rides the
        # wire unchanged.
        c, t = client([(200, {"data": []})])
        c.sweep_gl_entries("Acme Co", "2026-07-01 12:34:56.789012", "2026-07-02 00:00:00")
        filters = json.loads(t.calls[0]["params"]["filters"])
        self.assertIn(["creation", ">=", "2026-07-01 12:34:56.789012"], filters)
        self.assertIn(["creation", "<=", "2026-07-02 00:00:00"], filters)


class TestSweepGlEntriesHappyPath(unittest.TestCase):
    def test_clean_multi_row_body_returns_validated(self):
        rows = [gl_row(account="Debtors", debit=250.0, credit=0.0),
                gl_row(account="Sales", debit=0.0, credit=250.0, voucher_no="SI-10")]
        c, t = client([(200, {"data": rows})])
        out = c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertEqual(out, rows)

    def test_zero_debit_and_credit_are_valid_not_missing(self):
        rows = [gl_row(debit=0.0, credit=250.0)]
        c, t = client([(200, {"data": rows})])
        out = c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertEqual(out, rows)

    def test_int_amounts_are_valid(self):
        rows = [gl_row(debit=100, credit=0)]
        c, t = client([(200, {"data": rows})])
        out = c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertEqual(out, rows)

    def test_is_cancelled_one_is_valid(self):
        rows = [gl_row(is_cancelled=1)]
        c, t = client([(200, {"data": rows})])
        out = c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertEqual(out, rows)

    def test_null_disclosure_fields_are_tolerated(self):
        # posting_date/modified/modified_by/party_type/party are disclosure-only — legitimately
        # blank on real rows (a Cash-account row typically carries no party). Must NOT raise.
        rows = [gl_row(posting_date=None, modified=None, modified_by=None,
                       party_type=None, party=None)]
        c, t = client([(200, {"data": rows})])
        out = c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertEqual(out, rows)

    def test_absent_disclosure_fields_are_tolerated(self):
        row = gl_row()
        for f in ("posting_date", "modified", "modified_by", "party_type", "party"):
            del row[f]
        c, t = client([(200, {"data": [row]})])
        out = c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertEqual(out, [row])


class TestSweepGlEntriesDenyBias(unittest.TestCase):
    def test_non_list_body_raises(self):
        c, t = client([(200, {"data": None})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_non_dict_row_raises(self):
        c, t = client([(200, {"data": ["not-a-dict"]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertIn("malformed", str(ctx.exception))

    def test_missing_account_raises(self):
        row = gl_row()
        del row["account"]
        c, t = client([(200, {"data": [row]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertIn("account", str(ctx.exception))

    def test_blank_account_raises(self):
        c, t = client([(200, {"data": [gl_row(account="   ")]})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_nan_debit_raises(self):
        c, t = client([(200, {"data": [gl_row(debit=float("nan"))]})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_infinite_credit_raises(self):
        c, t = client([(200, {"data": [gl_row(credit=float("inf"))]})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_bool_debit_raises(self):
        # bool is an int subclass in Python — must be explicitly excluded.
        c, t = client([(200, {"data": [gl_row(debit=True)]})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_string_credit_raises(self):
        c, t = client([(200, {"data": [gl_row(credit="250.00")]})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_missing_is_cancelled_raises(self):
        row = gl_row()
        del row["is_cancelled"]
        c, t = client([(200, {"data": [row]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertIn("is_cancelled", str(ctx.exception))

    def test_null_is_cancelled_raises(self):
        c, t = client([(200, {"data": [gl_row(is_cancelled=None)]})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_non_int_is_cancelled_raises(self):
        c, t = client([(200, {"data": [gl_row(is_cancelled="0")]})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_out_of_range_is_cancelled_raises(self):
        c, t = client([(200, {"data": [gl_row(is_cancelled=2)]})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_bool_is_cancelled_raises(self):
        # A malformed value must never silently read as 0 (live) — a JSON boolean is not the
        # clean int the downstream governed/cancel classification expects, even though True==1.
        c, t = client([(200, {"data": [gl_row(is_cancelled=True)]})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_blank_voucher_type_raises(self):
        c, t = client([(200, {"data": [gl_row(voucher_type="")]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertIn("voucher_type", str(ctx.exception))

    def test_missing_voucher_no_raises(self):
        row = gl_row()
        del row["voucher_no"]
        c, t = client([(200, {"data": [row]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertIn("voucher_no", str(ctx.exception))

    def test_blank_creation_raises(self):
        c, t = client([(200, {"data": [gl_row(creation="  ")]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertIn("creation", str(ctx.exception))

    def test_missing_owner_raises(self):
        row = gl_row()
        del row["owner"]
        c, t = client([(200, {"data": [row]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)
        self.assertIn("owner", str(ctx.exception))

    def test_non_str_owner_raises(self):
        c, t = client([(200, {"data": [gl_row(owner=12345)]})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_unreadable_403_raises(self):
        c, t = client([(403, {"exc_type": "PermissionError"})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)

    def test_second_row_malformed_still_raises(self):
        rows = [gl_row(), gl_row(account=None, voucher_no="SI-10")]
        c, t = client([(200, {"data": rows})])
        with self.assertRaises(ErpnextError):
            c.sweep_gl_entries("Acme Co", SINCE, UNTIL)


def repost_row(**overrides):
    row = {"name": "RAL-1", "owner": "agent@example.com", "creation": SINCE, "docstatus": 1}
    row.update(overrides)
    return row


class TestGetRepostsRequestShape(unittest.TestCase):
    def test_list_step_url_method_and_page_pin(self):
        c, t = client([(200, {"data": []})])
        c.get_reposts("Acme Co", SINCE, UNTIL)
        call = t.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertIn("/api/resource/Repost%20Accounting%20Ledger", call["url"])
        self.assertEqual(call["params"]["limit_page_length"], "0")

    def test_list_step_filters_and_fields(self):
        c, t = client([(200, {"data": []})])
        c.get_reposts("Acme Co", SINCE, UNTIL)
        call = t.calls[0]
        filters = json.loads(call["params"]["filters"])
        self.assertIn(["company", "=", "Acme Co"], filters)
        self.assertIn(["creation", ">=", SINCE], filters)
        self.assertIn(["creation", "<=", UNTIL], filters)
        fields = json.loads(call["params"]["fields"])
        self.assertEqual(fields, ["name", "owner", "creation", "docstatus"])

    def test_no_reposts_in_window_makes_only_the_list_call(self):
        c, t = client([(200, {"data": []})])
        out = c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertEqual(out, [])
        self.assertEqual(len(t.calls), 1)

    def test_per_doc_get_url_for_each_hit(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1"), repost_row(name="RAL-2")]}),
            (200, {"data": {"name": "RAL-1", "vouchers": []}}),
            (200, {"data": {"name": "RAL-2", "vouchers": []}}),
        ])
        c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertEqual(len(t.calls), 3)
        self.assertEqual(t.calls[1]["method"], "GET")
        self.assertEqual(t.calls[1]["url"],
                         "https://erp.example.com/api/resource/Repost%20Accounting%20Ledger/RAL-1")
        self.assertEqual(t.calls[2]["url"],
                         "https://erp.example.com/api/resource/Repost%20Accounting%20Ledger/RAL-2")


class TestGetRepostsVouchers(unittest.TestCase):
    def test_vouchers_extracted_from_full_doc(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1")]}),
            (200, {"data": {"name": "RAL-1", "vouchers": [
                {"voucher_type": "Sales Invoice", "voucher_no": "SI-9", "idx": 1,
                 "parent": "RAL-1", "parenttype": "Repost Accounting Ledger",
                 "parentfield": "vouchers", "name": "child-row-1"},
            ]}}),
        ])
        out = c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "RAL-1")
        self.assertEqual(out[0]["owner"], "agent@example.com")
        self.assertEqual(out[0]["creation"], SINCE)
        self.assertEqual(out[0]["docstatus"], 1)
        # Only voucher_type/voucher_no survive — frappe's per-row bookkeeping fields do not.
        self.assertEqual(out[0]["vouchers"], [{"voucher_type": "Sales Invoice",
                                                "voucher_no": "SI-9"}])

    def test_multiple_vouchers_on_one_repost(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1")]}),
            (200, {"data": {"name": "RAL-1", "vouchers": [
                {"voucher_type": "Sales Invoice", "voucher_no": "SI-9"},
                {"voucher_type": "Payment Entry", "voucher_no": "PE-4"},
            ]}}),
        ])
        out = c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertEqual(out[0]["vouchers"], [
            {"voucher_type": "Sales Invoice", "voucher_no": "SI-9"},
            {"voucher_type": "Payment Entry", "voucher_no": "PE-4"},
        ])

    def test_missing_vouchers_key_is_empty_list_not_error(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1")]}),
            (200, {"data": {"name": "RAL-1"}}),
        ])
        out = c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertEqual(out[0]["vouchers"], [])

    def test_empty_vouchers_list_is_empty_list_not_error(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1")]}),
            (200, {"data": {"name": "RAL-1", "vouchers": []}}),
        ])
        out = c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertEqual(out[0]["vouchers"], [])

    def test_null_vouchers_key_is_empty_list_not_error(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1")]}),
            (200, {"data": {"name": "RAL-1", "vouchers": None}}),
        ])
        out = c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertEqual(out[0]["vouchers"], [])


class TestGetRepostsDenyBias(unittest.TestCase):
    def test_403_on_the_list_raises(self):
        c, t = client([(403, {"exc_type": "PermissionError"})])
        with self.assertRaises(ErpnextError):
            c.get_reposts("Acme Co", SINCE, UNTIL)

    def test_non_list_body_on_the_list_raises(self):
        c, t = client([(200, {"data": None})])
        with self.assertRaises(ErpnextError):
            c.get_reposts("Acme Co", SINCE, UNTIL)

    def test_non_dict_list_row_raises(self):
        c, t = client([(200, {"data": ["not-a-dict"]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertIn("malformed", str(ctx.exception))

    def test_blank_name_raises(self):
        c, t = client([(200, {"data": [repost_row(name="  ")]})])
        with self.assertRaises(ErpnextError):
            c.get_reposts("Acme Co", SINCE, UNTIL)

    def test_missing_owner_raises(self):
        row = repost_row()
        del row["owner"]
        c, t = client([(200, {"data": [row]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertIn("owner", str(ctx.exception))

    def test_blank_creation_raises(self):
        c, t = client([(200, {"data": [repost_row(creation="")]})])
        with self.assertRaises(ErpnextError):
            c.get_reposts("Acme Co", SINCE, UNTIL)

    def test_missing_docstatus_raises(self):
        row = repost_row()
        del row["docstatus"]
        c, t = client([(200, {"data": [row]})])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertIn("docstatus", str(ctx.exception))

    def test_non_int_docstatus_raises(self):
        c, t = client([(200, {"data": [repost_row(docstatus="1")]})])
        with self.assertRaises(ErpnextError):
            c.get_reposts("Acme Co", SINCE, UNTIL)

    def test_bool_docstatus_raises(self):
        c, t = client([(200, {"data": [repost_row(docstatus=True)]})])
        with self.assertRaises(ErpnextError):
            c.get_reposts("Acme Co", SINCE, UNTIL)

    def test_null_docstatus_raises(self):
        c, t = client([(200, {"data": [repost_row(docstatus=None)]})])
        with self.assertRaises(ErpnextError):
            c.get_reposts("Acme Co", SINCE, UNTIL)

    def test_unreadable_per_doc_get_raises(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1")]}),
            (403, {"exc_type": "PermissionError"}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_reposts("Acme Co", SINCE, UNTIL)

    def test_malformed_full_doc_body_raises(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1")]}),
            (200, {"data": None}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_reposts("Acme Co", SINCE, UNTIL)

    def test_malformed_vouchers_child_table_raises(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1")]}),
            (200, {"data": {"name": "RAL-1", "vouchers": "not-a-list"}}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_reposts("Acme Co", SINCE, UNTIL)

    def test_malformed_voucher_child_row_not_a_dict_raises(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1")]}),
            (200, {"data": {"name": "RAL-1", "vouchers": ["not-a-dict"]}}),
        ])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertIn("malformed", str(ctx.exception))

    def test_blank_voucher_type_in_child_row_raises(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1")]}),
            (200, {"data": {"name": "RAL-1", "vouchers": [
                {"voucher_type": "  ", "voucher_no": "SI-9"}]}}),
        ])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertIn("voucher_type", str(ctx.exception))

    def test_missing_voucher_no_in_child_row_raises(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1")]}),
            (200, {"data": {"name": "RAL-1", "vouchers": [
                {"voucher_type": "Sales Invoice"}]}}),
        ])
        with self.assertRaises(ErpnextError) as ctx:
            c.get_reposts("Acme Co", SINCE, UNTIL)
        self.assertIn("voucher_no", str(ctx.exception))

    def test_second_repost_malformed_still_raises(self):
        c, t = client([
            (200, {"data": [repost_row(name="RAL-1"), repost_row(name="RAL-2", docstatus="bad")]}),
        ])
        with self.assertRaises(ErpnextError):
            c.get_reposts("Acme Co", SINCE, UNTIL)


if __name__ == "__main__":
    unittest.main()
