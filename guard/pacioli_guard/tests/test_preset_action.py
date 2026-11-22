# Copyright (c) 2026, John Broadway and contributors
# License: AGPL-3.0-or-later
"""A document body that presets frappe's `_action` (0.18.0, found by review on the branch).

frappe keeps any non-reserved key of a document dict (`BaseDocument.update`), and
`Document.check_if_latest` keeps an `_action` that is already set. When it is "discard", frappe
skips the docstatus transition check (and with it the submit/cancel permission check), and
`run_before_save_methods` / `run_post_save_methods` run no before/on hook at all, the consent gate
and the ledger posting included. Only `on_change` still fires. frappe's own `discard()` never
calls `save()` (it writes with `db_set`), so a save or insert that carries `_action = "discard"`
was not started by frappe.

The resource routes were closed in 0.18.0 already. These pin the other doors that take a whole
document in the body, and the document-layer belt for a consent-gated seat.
"""
import json
import unittest

from pacioli_guard.tests.test_enforce import AUTH, FakeFrappe, FakePermissionError, FakeScopeDoc  # isort: skip
from pacioli_guard import act, enforce  # noqa: E402
from pacioli_guard.scope import body_presets_save_action  # noqa: E402
from pacioli_guard.tests.test_act import TOKEN, FakeDoc, _save, insert, marker, wire  # noqa: E402
from pacioli_guard.tests.test_resource_body import WRITE_NO_METHODS, _BodyReq  # noqa: E402

DT = "Journal Entry"
EVIL = {"doctype": DT, "name": "JV-1", "docstatus": 1, "_action": "discard"}
FULL = FakeScopeDoc(method_patterns=[f"{DT}.submit", f"{DT}.cancel", f"{DT}.*",
                                     "frappe.client.save", "frappe.client.insert",
                                     "frappe.desk.form.save.savedocs"],
                    **dict(WRITE_NO_METHODS, resource_doctypes=[DT]))


class TestTheBodyScan(unittest.TestCase):
    def test_finds_it_in_every_shape_a_document_arrives_in(self):
        for form in ({"doc": json.dumps(EVIL)}, {"doc": EVIL}, {"docs": json.dumps(EVIL)},
                     {"docs": json.dumps([{"doctype": DT}, EVIL])}, {"data": json.dumps(EVIL)},
                     {"_action": "discard"}, {"doc": " \n" + json.dumps(EVIL)},
                     {"doc": "﻿" + json.dumps(EVIL)}):
            with self.subTest(form=str(form)[:60]):
                self.assertTrue(body_presets_save_action(form))

    def test_a_document_without_it_is_fine(self):
        for form in ({"doc": json.dumps({"doctype": DT, "name": "JV-1"})}, {"doc": "not json"},
                     {}, {"action": "Submit"}, {"doc": json.dumps([1, 2])}, None,
                     {"doc": json.dumps({"doctype": DT, "accounts": [{"_action": "x"}]})}):
            with self.subTest(form=str(form)[:60]):
                self.assertFalse(body_presets_save_action(form))

    def test_text_that_opens_an_object_but_will_not_parse_is_refused(self):
        # As for a resource body: this parser failing is not proof frappe's did.
        self.assertTrue(body_presets_save_action({"doc": '{"_action": "discard", NaN}'}))
        self.assertTrue(body_presets_save_action({"doc": "{" * 100000}))


class TestEveryDoorRefusesIt(unittest.TestCase):
    """The credential holds every grant the door needs, so the ONLY reason left is `_action`."""

    def setUp(self):
        self._real = enforce.frappe

    def tearDown(self):
        enforce.frappe = self._real

    def _denied(self, path, form, *, for_this=True):
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="a@x", scopes={"a@x": FULL},
                                    request=_BodyReq(path, "POST", ""), form_dict=form)
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        if for_this:
            self.assertIn("_action", str(ctx.exception))

    def test_client_submit_insert_save(self):
        for m in ("frappe.client.submit", "frappe.client.insert", "frappe.client.save"):
            with self.subTest(method=m):
                self._denied(f"/api/method/{m}", {"doc": json.dumps(EVIL)})

    def test_savedocs_every_action(self):
        for action in ("Submit", "Update", "Cancel"):
            with self.subTest(action=action):
                self._denied("/api/method/frappe.desk.form.save.savedocs",
                             {"doc": json.dumps(EVIL), "action": action})
        # A draft Save through the bare savedocs name is refused earlier, by deny-unknown (a bare
        # method name is not a resolved per-doctype grant), so the reason is not this one.
        self._denied("/api/method/frappe.desk.form.save.savedocs",
                     {"doc": json.dumps(EVIL), "action": "Save"}, for_this=False)

    def test_run_doc_method(self):
        self._denied("/api/method/run_doc_method", {"docs": json.dumps(EVIL), "method": "submit"})

    def test_insert_many_is_refused(self):
        # Refused earlier, by the multi-document rewrite, which closes on any batch it cannot
        # express as one per-doctype grant. Pinned so a later widening of that rule meets this.
        self._denied("/api/method/frappe.client.insert_many", {"docs": json.dumps([EVIL])},
                     for_this=False)

    def test_the_same_requests_without_it_still_pass(self):
        clean = dict(EVIL)
        del clean["_action"]
        for path, form in (("/api/method/frappe.client.submit", {"doc": json.dumps(clean)}),
                           ("/api/method/frappe.desk.form.save.savedocs",
                            {"doc": json.dumps(clean), "action": "Update"})):
            with self.subTest(path=path):
                enforce.frappe = FakeFrappe(headers=AUTH, session_user="a@x",
                                            scopes={"a@x": FULL},
                                            request=_BodyReq(path, "POST", ""), form_dict=form)
                self.assertIsNone(enforce.check_scope())


class TestTheDocumentLayerBelt(unittest.TestCase):
    """OAuth Bearer, desk sessions, jobs and the console never pass the request check. For a
    consent-gated seat the document refuses a save that carries `_action = "discard"`."""

    HANDLER = "on_change"

    @staticmethod
    def doc(action="discard"):
        d = FakeDoc(in_insert=False)
        d._action = action
        return d

    def test_a_save_carrying_discard_is_refused_for_a_gated_seat(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="submit"))
        d = self.doc()
        with self.assertRaises(FakePermissionError):
            _save(d, act.on_change, d, self.HANDLER)
        self.assertIn("_action", fake.thrown[0])

    def test_the_refusal_rolls_the_write_back_before_it_raises(self):
        # The row is already written when on_change runs, and frappe.client.bulk_update catches
        # every exception per document and commits. So a raise alone left the write in place.
        fake = wire(headers={}, markers={})
        real_throw = fake.throw

        def throw(msg, exc=None):
            fake.db.events.append("throw")
            return real_throw(msg, exc)

        fake.throw = throw
        fake.db._disable_transaction_control = 1      # as frappe sets it around every hook
        d = self.doc()
        with self.assertRaises(FakePermissionError):
            _save(d, act.on_change, d, self.HANDLER)
        self.assertEqual(fake.db.events, ["rollback", "throw"])
        self.assertEqual(fake.db._disable_transaction_control, 1, "the lock is put back")

    def test_a_rollback_that_fails_still_refuses(self):
        fake = wire(headers={}, markers={})
        fake.db.rollback_raises = True
        fake.db._disable_transaction_control = 2
        added = []
        fake.db.before_commit = type("CB", (), {"add": lambda self, f: added.append(f)})()
        fake.PermissionError = FakePermissionError
        d = self.doc()
        with self.assertRaises(FakePermissionError):
            _save(d, act.on_change, d, self.HANDLER)
        self.assertEqual(fake.db._disable_transaction_control, 2)
        # The write is still in the transaction, so its next commit is refused.
        self.assertEqual(len(added), 1)
        with self.assertRaises(FakePermissionError):
            added[0]()

    def test_a_rollback_that_ran_adds_no_commit_refusal(self):
        fake = wire(headers={}, markers={})
        fake.db._disable_transaction_control = 1
        added = []
        fake.db.before_commit = type("CB", (), {"add": lambda self, f: added.append(f)})()
        d = self.doc()
        with self.assertRaises(FakePermissionError):
            _save(d, act.on_change, d, self.HANDLER)
        self.assertEqual(added, [])

    def test_a_frappe_without_the_lock_counter_cannot_commit_after_a_refusal(self):
        fake = wire(headers={}, markers={})
        del fake.db._disable_transaction_control
        added = []
        fake.db.before_commit = type("CB", (), {"add": lambda self, f: added.append(f)})()
        fake.PermissionError = FakePermissionError
        d = self.doc()
        with self.assertRaises(FakePermissionError):
            _save(d, act.on_change, d, self.HANDLER)
        self.assertEqual(len(added), 1)
        with self.assertRaises(FakePermissionError):
            added[0]()

    def test_an_insert_carrying_discard_is_refused_should_frappe_ever_reach_it(self):
        # frappe 16 never gets here: check_if_latest reads previous.modified with no previous
        # document and raises AttributeError first. The belt holds anyway.
        wire(headers={}, markers={})
        d = self.doc()
        with self.assertRaises(FakePermissionError):
            insert(d, act.on_change, d, self.HANDLER)

    def test_frappes_own_discard_passes(self):
        # Document.discard() runs on_change from db_set, with no save frame for the document.
        fake = wire(headers={}, markers={})
        act.on_change(self.doc(), self.HANDLER)
        self.assertIsNone(fake.thrown)
        self.assertEqual(fake.db.events, [])

    def test_an_ordinary_save_passes(self):
        fake = wire(headers={}, markers={})
        for action in ("save", "submit", "cancel", "update_after_submit"):
            d = self.doc(action)
            _save(d, act.on_change, d, self.HANDLER)
        self.assertIsNone(fake.thrown)

    def test_a_document_with_no_action_at_all_passes(self):
        fake = wire(headers={}, markers={})
        d = FakeDoc(in_insert=False)
        _save(d, act.on_change, d, self.HANDLER)
        self.assertIsNone(fake.thrown)

    def test_a_seat_that_is_not_gated_is_left_alone(self):
        fake = wire(gated=False, headers={}, markers={})
        d = self.doc()
        _save(d, act.on_change, d, self.HANDLER)
        self.assertIsNone(fake.thrown)

    def test_another_documents_save_frame_is_not_this_documents(self):
        # A discard of D running inside some other document's save is frappe's own db_set path.
        fake = wire(headers={}, markers={})
        outer, d = FakeDoc(doctype="Sales Invoice", name="SI-9", in_insert=False), self.doc()
        _save(outer, act.on_change, d, self.HANDLER)
        self.assertIsNone(fake.thrown)

    def test_hooks_registers_it_and_the_receipt_counts_it(self):
        from pacioli_guard import hooks
        self.assertEqual(hooks.doc_events["*"][self.HANDLER], "pacioli_guard.act.on_change")


class TestOnlyADraftMayBeDiscarded(unittest.TestCase):
    """`Document.discard()` checks the docstatus of the object it was handed, which
    `run_doc_method` builds from the client's body. `before_discard` judges the stored row."""

    HANDLER = "before_discard"

    @staticmethod
    def doc():
        d = FakeDoc(doctype=DT, name="JV-1", in_insert=False)
        d.docstatus = 0          # what the CLIENT said
        return d

    def test_a_submitted_document_described_as_a_draft_is_refused(self):
        fake = wire(headers={}, markers={})
        fake.db.docstatus[(DT, "JV-1")] = 1
        with self.assertRaises(FakePermissionError) as ctx:
            act.before_discard(self.doc(), self.HANDLER)
        self.assertIn("only a draft may be discarded", str(ctx.exception))
        self.assertEqual(fake.db.docstatus_reads, [(DT, "JV-1", True)])

    def test_it_holds_for_every_principal_not_only_a_gated_seat(self):
        for gated in (True, False, None):
            with self.subTest(gated=gated):
                fake = wire(gated=gated, headers={}, markers={})
                fake.db.docstatus[(DT, "JV-1")] = 2
                with self.assertRaises(FakePermissionError):
                    act.before_discard(self.doc(), self.HANDLER)

    def test_a_stored_draft_is_discarded_as_before(self):
        for stored in (0, "0"):
            with self.subTest(stored=stored):
                fake = wire(headers={}, markers={})
                fake.db.docstatus[(DT, "JV-1")] = stored
                act.before_discard(self.doc(), self.HANDLER)
                self.assertIsNone(fake.thrown)

    def test_no_stored_row_is_left_to_frappe(self):
        fake = wire(headers={}, markers={})
        act.before_discard(self.doc(), self.HANDLER)
        self.assertIsNone(fake.thrown)

    def test_an_unreadable_row_is_refused(self):
        fake = wire(headers={}, markers={})
        fake.db.docstatus[(DT, "JV-1")] = RuntimeError("db down")
        with self.assertRaises(FakePermissionError):
            act.before_discard(self.doc(), self.HANDLER)

    def test_registered(self):
        # That `gate_registered` counts it is pinned by test_api_reports_the_truth.py, which
        # derives CONSENT_HANDLERS's expected set from hooks.py itself.
        from pacioli_guard import hooks
        self.assertEqual(hooks.doc_events["*"][self.HANDLER], "pacioli_guard.act.before_discard")


class TestFreeTextIsNotADocument(unittest.TestCase):
    def test_a_bracket_in_free_text_is_not_refused(self):
        for form in ({"remarks": "[URGENT] pay today"}, {"title": "{draft}"},
                     {"user_remark": "[a, b"}):
            with self.subTest(form=form):
                self.assertFalse(body_presets_save_action(form))

    def test_unparseable_text_under_a_document_key_still_is(self):
        for key in ("doc", "docs", "document", "data"):
            with self.subTest(key=key):
                self.assertTrue(body_presets_save_action({key: '{"_action": "discard", NaN'}))

    def test_set_value_naming_it_as_the_field_is_refused(self):
        self.assertTrue(body_presets_save_action({"doctype": DT, "name": "JV-1",
                                                  "fieldname": "_action", "value": "discard"}))
        self.assertTrue(body_presets_save_action({"fieldname": json.dumps({"_action": "discard"})}))
        self.assertFalse(body_presets_save_action({"fieldname": "user_remark", "value": "x"}))

    def test_parseable_text_carrying_it_under_any_key_still_is(self):
        self.assertTrue(body_presets_save_action({"other": json.dumps(EVIL)}))


if __name__ == "__main__":
    unittest.main()
