# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""AMEND core tests — the strip-list is the security surface, so it is pinned field by field:
identity/audit/state must NOT copy, ``amended_from`` is SET (never copied), and the child tables
survive with their data but without their row identity."""
import copy
import unittest

from pacioli.amend import amend_payload


def cancelled_source():
    return {
        "name": "ACC-SINV-2026-00042",
        "doctype": "Sales Invoice",
        "docstatus": 2,
        "owner": "broker@example.com",
        "creation": "2026-06-15 09:00:00.000001",
        "modified": "2026-07-01 11:00:00.000001",
        "modified_by": "broker@example.com",
        "idx": 0,
        "status": "Cancelled",
        "workflow_state": "Cancelled",
        "amended_from": "",
        "naming_series": "ACC-SINV-.YYYY.-",
        "company": "Example Corp",
        "customer": "ACME",
        "posting_date": "2026-06-15",
        "due_date": "2026-07-15",
        "grand_total": 250.0,
        "outstanding_amount": 0.0,
        "_user_tags": ",flagged",
        "_comments": "[]",
        "_assign": "[]",
        "_liked_by": "[]",
        "__onload": {"load_after_mapping": True},
        "items": [
            {"name": "row-aaa", "parent": "ACC-SINV-2026-00042", "parentfield": "items",
             "parenttype": "Sales Invoice", "doctype": "Sales Invoice Item", "idx": 1,
             "docstatus": 2, "owner": "broker@example.com",
             "creation": "2026-06-15 09:00:00.000001",
             "modified": "2026-07-01 11:00:00.000001", "modified_by": "broker@example.com",
             "item_code": "WIDGET", "qty": 5.0, "rate": 50.0, "amount": 250.0,
             "income_account": "Sales - EC"},
        ],
        "taxes": [
            {"name": "row-bbb", "parent": "ACC-SINV-2026-00042", "parentfield": "taxes",
             "parenttype": "Sales Invoice", "docstatus": 2, "idx": 1,
             "charge_type": "On Net Total", "account_head": "VAT - EC", "rate": 0.0},
        ],
        "payment_schedule": [],
    }


class TestAmendedFromAndDraftState(unittest.TestCase):
    def test_amended_from_is_set_to_the_source_and_docstatus_forced_to_draft(self):
        p = amend_payload(cancelled_source())
        self.assertEqual(p["amended_from"], "ACC-SINV-2026-00042")
        self.assertEqual(p["docstatus"], 0)

    def test_a_prior_amended_from_is_overwritten_never_copied(self):
        # A chain SI → SI-1 → SI-2 must always point one hop back, never at the original twice.
        src = cancelled_source()
        src["name"] = "ACC-SINV-2026-00042-1"
        src["amended_from"] = "ACC-SINV-2026-00042"
        p = amend_payload(src)
        self.assertEqual(p["amended_from"], "ACC-SINV-2026-00042-1")


class TestStripList(unittest.TestCase):
    def test_identity_and_audit_fields_do_not_copy(self):
        p = amend_payload(cancelled_source())
        for f in ("name", "doctype", "owner", "creation", "modified", "modified_by", "idx"):
            self.assertNotIn(f, p, f)

    def test_state_and_settlement_residue_do_not_copy(self):
        p = amend_payload(cancelled_source())
        for f in ("status", "workflow_state", "outstanding_amount"):
            self.assertNotIn(f, p, f)

    def test_every_underscore_prefixed_key_is_dropped_by_rule(self):
        p = amend_payload(cancelled_source())
        self.assertEqual([k for k in p if k.startswith("_")], [])

    def test_document_data_survives(self):
        p = amend_payload(cancelled_source())
        self.assertEqual(p["company"], "Example Corp")
        self.assertEqual(p["customer"], "ACME")
        self.assertEqual(p["posting_date"], "2026-06-15")
        self.assertEqual(p["due_date"], "2026-07-15")
        self.assertEqual(p["grand_total"], 250.0)
        self.assertEqual(p["naming_series"], "ACC-SINV-.YYYY.-")


class TestChildTables(unittest.TestCase):
    def test_child_rows_survive_with_data_but_without_row_identity(self):
        p = amend_payload(cancelled_source())
        self.assertEqual(len(p["items"]), 1)
        row = p["items"][0]
        self.assertEqual(row["item_code"], "WIDGET")
        self.assertEqual(row["qty"], 5.0)
        self.assertEqual(row["rate"], 50.0)
        self.assertEqual(row["income_account"], "Sales - EC")
        self.assertEqual(row["idx"], 1)                          # ordering survives
        self.assertEqual(row["doctype"], "Sales Invoice Item")   # child meta survives
        for f in ("name", "parent", "parentfield", "parenttype", "docstatus",
                  "owner", "creation", "modified", "modified_by"):
            self.assertNotIn(f, row, f)

    def test_taxes_survive_too(self):
        p = amend_payload(cancelled_source())
        self.assertEqual(p["taxes"][0]["account_head"], "VAT - EC")
        self.assertNotIn("parent", p["taxes"][0])

    def test_empty_child_table_copies_as_empty(self):
        p = amend_payload(cancelled_source())
        self.assertEqual(p["payment_schedule"], [])


class TestDenyBias(unittest.TestCase):
    def test_uncancelled_sources_are_refused(self):
        for ds in (0, 1, None, "2"):
            src = dict(cancelled_source(), docstatus=ds)
            with self.assertRaises(ValueError):
                amend_payload(src)

    def test_nameless_and_nondict_sources_are_refused(self):
        with self.assertRaises(ValueError):
            amend_payload(dict(cancelled_source(), name=""))
        with self.assertRaises(ValueError):
            amend_payload(dict(cancelled_source(), name=None))
        with self.assertRaises(ValueError):
            amend_payload(["not", "a", "doc"])

    def test_source_is_never_mutated(self):
        src = cancelled_source()
        before = copy.deepcopy(src)
        amend_payload(src)
        self.assertEqual(src, before)


class TestWorkflowSeat(unittest.TestCase):
    """The F1 fix (2026-07-17, found by the first dogfood drive): an amendment born under an
    ACTIVE workflow must be SEATED at the workflow's initial state — the strip correctly drops
    the cancelled state, but an unseated draft is stuck (no legal transition from a null state).
    ``seat`` is (field, state) as computed by ``pacioli.workflow.initial_seat``; None keeps the
    payload byte-identical to the ungoverned case."""

    def test_no_seat_keeps_the_payload_exactly_as_before(self):
        self.assertEqual(amend_payload(cancelled_source()),
                         amend_payload(cancelled_source(), seat=None))
        self.assertNotIn("workflow_state", amend_payload(cancelled_source()))

    def test_seat_sets_the_state_field(self):
        payload = amend_payload(cancelled_source(), seat=("workflow_state", "Draft"))
        self.assertEqual(payload["workflow_state"], "Draft")

    def test_seat_overwrites_a_custom_state_field_carried_from_the_source(self):
        # A CUSTOM workflow_state_field is not in the strip-list (it copies through, carrying the
        # cancelled state) — the seat must overwrite it, never leave the stale value standing.
        src = dict(cancelled_source(), approval_state="Cancelled")
        payload = amend_payload(src, seat=("approval_state", "Draft"))
        self.assertEqual(payload["approval_state"], "Draft")

    def test_a_malformed_seat_is_refused_not_guessed(self):
        for bad in (("", "Draft"), ("workflow_state", ""), ("f",), "workflow_state",
                    (None, "Draft"), ("workflow_state", None)):
            with self.assertRaises(ValueError, msg=f"accepted malformed seat {bad!r}"):
                amend_payload(cancelled_source(), seat=bad)

    def test_a_seat_field_that_reenters_the_strip_surface_is_refused(self):
        # The strip-list is the security surface — a (mis)configured workflow_state_field must
        # never be allowed to overwrite what the strip exists to protect (review finding [0]).
        for field in ("docstatus", "amended_from", "name", "doctype", "owner", "modified",
                      "status", "outstanding_amount"):
            with self.assertRaises(ValueError, msg=f"accepted reserved seat field {field!r}"):
                amend_payload(cancelled_source(), seat=(field, "Draft"))

    def test_an_underscore_seat_field_is_refused(self):
        with self.assertRaises(ValueError):
            amend_payload(cancelled_source(), seat=("_assign", "Draft"))

    def test_a_seat_field_naming_a_child_table_is_refused(self):
        # payload["items"] is a child TABLE — a seat there would replace rows with a string.
        with self.assertRaises(ValueError):
            amend_payload(cancelled_source(), seat=("items", "Draft"))


if __name__ == "__main__":
    unittest.main()
