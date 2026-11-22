# Copyright (c) 2026, John Broadway and contributors
# License: AGPL-3.0-or-later
"""The fourth consent act: editing a document that is already submitted (0.18.0).

Reported privately twice: GHSA-wc4v-5w73-74w4 (sandhiyabalaji32-tech, first) and
GHSA-9h6v-f656-p77h (nikhilagrawalsf, with a run reproduction). A principal whose grant has
``require_consent`` needed a human-minted marker to submit, cancel or delete, and nothing to change
the ``allow_on_submit`` fields of a posted document. frappe fires ``before_update_after_submit``
for that save (``model/document.py`` ``run_before_save_methods``) and no handler was registered.

The ride rule: John left the choice to Claude on 2026-11-21 ("your call"), on a recommendation
made after a sweep of every save in erpnext 16
(``docs/plans/2026-11-21-guard-update-after-submit.md``). An edit
ERPNext makes INSIDE a submit or cancel that already established consent rides that act; a direct
edit needs its own marker.
"""
import json
import os
import pathlib
import unittest

# test_enforce stubs the `frappe` module that act.py imports at its top, so it is imported first.
# In the full suite another module had already done it, which hid that this file could not be
# collected on its own.
from pacioli_guard.tests.test_enforce import FakePermissionError  # isort: skip
from pacioli_guard import act  # noqa: E402
from pacioli_guard.scope import CONSENT_ACTS, plan_consent_marker  # noqa: E402
from pacioli_guard.tests.test_act import (  # noqa: E402
    TOKEN, FakeDoc, _save, insert, marker, wire,
)

HANDLER = "before_update_after_submit"


def submitted(doctype="Sales Invoice", name="SI-0001"):
    """A document loaded by name at docstatus 1: not created by any act in progress."""
    return FakeDoc(doctype=doctype, name=name, in_insert=False)


def direct_update(doc):
    """One write frame holding ``doc``: the actor's own save of it."""
    return _save(doc, act.before_update_after_submit, doc, HANDLER)


def update_inside(outer, inner):
    """``inner`` saved while ``outer``'s own write frame is still live."""
    return _save(outer, _save, inner, act.before_update_after_submit, inner, HANDLER)


class TestTheActExists(unittest.TestCase):
    def test_update_is_a_consent_act(self):
        self.assertIn("update", CONSENT_ACTS)
        self.assertEqual(act.UPDATE, "update")

    def test_hooks_registers_the_gate(self):
        from pacioli_guard import hooks
        self.assertEqual(hooks.doc_events["*"][HANDLER],
                         "pacioli_guard.act.before_update_after_submit")

    # The receipt (`api.CONSENT_HANDLERS`) is pinned by `test_api_reports_the_truth.py`, which
    # derives the expected set from hooks.py, so registering the gate without counting it fails
    # there. Not repeated here: importing `api` needs that file's own frappe stub.

    def test_a_marker_can_be_minted_for_it(self):
        ok, reason, row = plan_consent_marker("Sales Invoice", "SI-0001", "update",
                                              "t" * 32, 900, 1_700_000_000)
        self.assertTrue(ok, reason)
        self.assertEqual(row["ref_action"], "update")
        ok, reason, _ = plan_consent_marker("Sales Invoice", "SI-0001", "edit",
                                            "t" * 32, 900, 1_700_000_000)
        self.assertFalse(ok)
        self.assertIn("update", reason)

    def test_the_marker_doctype_offers_the_act(self):
        path = (pathlib.Path(act.__file__).parent / "scoping" / "doctype"
                / "pacioli_consent_marker" / "pacioli_consent_marker.json")
        field = next(f for f in json.loads(path.read_text())["fields"]
                     if f["fieldname"] == "ref_action")
        self.assertEqual([o for o in field["options"].split("\n") if o], list(CONSENT_ACTS))

    def test_the_refusal_names_what_this_gate_does_not_see(self):
        self.assertIn("ignore_validate", act._RESIDUAL[act.UPDATE])


class TestADirectEditNeedsItsOwnMarker(unittest.TestCase):
    def test_no_marker_is_refused(self):
        fake = wire(headers={}, markers=marker(action="update"))
        with self.assertRaises(FakePermissionError):
            direct_update(submitted())
        self.assertIn("consent to update", fake.thrown[0].lower())

    def test_an_oauth_bearer_edit_is_refused_too(self):
        # No api-key header: the transport gate never runs. This one is on the document.
        fake = wire(headers={"Authorization": "Bearer some-oauth-token"},
                    markers=marker(action="update"))
        with self.assertRaises(FakePermissionError):
            direct_update(submitted())
        self.assertIn("consent", fake.thrown[0].lower())

    def test_no_marker_for_this_document_at_all_is_refused(self):
        wire(headers={act.CONSENT_HEADER: TOKEN}, markers={})
        with self.assertRaises(FakePermissionError):
            direct_update(submitted())

    def test_markers_for_the_other_acts_do_not_spend_on_an_edit(self):
        for other in ("submit", "cancel", "delete"):
            with self.subTest(marker=other):
                fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action=other))
                with self.assertRaises(FakePermissionError):
                    direct_update(submitted())
                self.assertIn(f"authorises {other}, not update", fake.thrown[0])

    def test_an_update_marker_does_not_spend_on_the_other_acts(self):
        for handler, name in ((act.before_submit, "submit"), (act.before_cancel, "cancel"),
                              (act.on_trash, "delete")):
            with self.subTest(act=name):
                fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="update"))
                with self.assertRaises(FakePermissionError):
                    handler(submitted(), name)
                self.assertIn(f"authorises update, not {name}", fake.thrown[0])

    def test_an_update_marker_spends_once(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="update"))
        direct_update(submitted())
        self.assertIsNone(fake.thrown)
        row = fake.db.markers[("Sales Invoice", "SI-0001")]
        self.assertEqual(row["burned"], 1)
        with self.assertRaises(FakePermissionError):
            direct_update(submitted())

    def test_a_self_minted_marker_is_refused(self):
        wire(headers={act.CONSENT_HEADER: TOKEN},
             markers=marker(action="update", minted_by="broker@x"))
        with self.assertRaises(FakePermissionError):
            direct_update(submitted())

    def test_a_principal_that_is_not_gated_is_left_alone(self):
        fake = wire(gated=False, headers={}, markers={})
        direct_update(submitted())
        self.assertIsNone(fake.thrown)
        fake = wire(gated=None, headers={}, markers={})
        direct_update(submitted())
        self.assertIsNone(fake.thrown)

    def test_the_handler_takes_one_or_two_arguments(self):
        fake = wire(gated=False, headers={}, markers={})
        act.before_update_after_submit(submitted())
        self.assertIsNone(fake.thrown)


class TestAnEditInsideAGovernedActRides(unittest.TestCase):
    """The invoice-with-advances case. ``Sales Invoice.on_submit`` saves the already-submitted
    Payment Entry named in its advances table (erpnext ``sales_invoice.py`` ->
    ``accounts_controller.update_against_document_in_jv`` -> ``accounts/utils.py``
    ``reconcile_against_document``), with the invoice's own write frame still on the stack."""

    def setUp(self):
        self.fake = wire(headers={}, markers={})
        self.invoice = submitted("Sales Invoice", "SI-0001")
        self.payment = submitted("Payment Entry", "PE-0007")

    def test_under_a_governed_submit_it_rides(self):
        self.invoice.flags[act._CONSENT_ESTABLISHED] = act.SUBMIT
        update_inside(self.invoice, self.payment)
        self.assertIsNone(self.fake.thrown)

    def test_under_a_governed_cancel_it_rides(self):
        # Cancelling a bank-reconciled voucher saves the Bank Transaction that matched it.
        self.invoice.flags[act._CONSENT_ESTABLISHED] = act.CANCEL
        update_inside(self.invoice, submitted("Bank Transaction", "BT-0001"))
        self.assertIsNone(self.fake.thrown)

    def test_a_ridden_edit_leaves_no_stamp_behind(self):
        # A stamp is authority that outlives the frame it was earned in. The Payment Entry was
        # named by the caller and approved by nobody, so it must not carry `submit` or `cancel`
        # once the invoice's act is over (review of the first cut, which stamped it with the act
        # it rode).
        self.invoice.flags[act._CONSENT_ESTABLISHED] = act.CANCEL
        update_inside(self.invoice, self.payment)
        self.assertIsNone(self.fake.thrown)
        self.assertNotIn(act._CONSENT_ESTABLISHED, self.payment.flags)

    def test_what_a_ridden_edit_cascades_into_is_judged_against_the_original_act(self):
        # Custody without a stamp: the walk passes the unstamped Payment Entry frame and finds
        # the invoice's, still live. An edit two levels down rides the governed submit...
        self.invoice.flags[act._CONSENT_ESTABLISHED] = act.SUBMIT
        third = submitted("Journal Entry", "JV-0003")
        _save(self.invoice, _save, self.payment, _save, third,
              act.before_update_after_submit, third, HANDLER)
        self.assertIsNone(self.fake.thrown)
        # ...and a cancel two levels down under a governed SUBMIT is still refused.
        with self.assertRaises(FakePermissionError):
            _save(self.invoice, _save, self.payment, _save, third,
                  act.before_cancel, third, "before_cancel")

    def test_a_document_that_rode_an_edit_licenses_nothing_afterwards(self):
        # The invoice's cancel is over. The same Payment Entry object is written again, by a
        # path that fires no handler of its own, and a cancel of a pre-existing document happens
        # inside it. With a `cancel` stamp left on the Payment Entry that cancel would ride.
        self.invoice.flags[act._CONSENT_ESTABLISHED] = act.CANCEL
        update_inside(self.invoice, self.payment)
        victim = submitted("Journal Entry", "JV-0009")
        with self.assertRaises(FakePermissionError):
            _save(self.payment, _save, victim, act.before_cancel, victim, "before_cancel")

    def test_under_a_write_that_established_nothing_it_is_refused(self):
        # A draft save, or an insert of a non-submittable document, is never gated, so an
        # enclosing frame for one proves nothing (the 0.9.2 fail-open, kept closed here).
        with self.assertRaises(FakePermissionError):
            update_inside(self.invoice, self.payment)

    def test_under_a_consented_edit_it_does_not_ride(self):
        # A marker for editing one document is not a marker for editing the next one.
        self.invoice.flags[act._CONSENT_ESTABLISHED] = act.UPDATE
        with self.assertRaises(FakePermissionError):
            update_inside(self.invoice, self.payment)

    def test_under_a_governed_delete_stamp_it_does_not_ride(self):
        self.invoice.flags[act._CONSENT_ESTABLISHED] = act.DELETE
        with self.assertRaises(FakePermissionError):
            update_inside(self.invoice, self.payment)

    def test_a_stamp_with_no_live_frame_licenses_nothing(self):
        # The invoice finished its governed submit earlier in this process. The Payment Entry is
        # now saved on its own, with no invoice frame on the stack.
        self.invoice.flags[act._CONSENT_ESTABLISHED] = act.SUBMIT
        with self.assertRaises(FakePermissionError):
            direct_update(self.payment)


class TestADocumentSavingItselfInsideItsOwnGovernedAct(unittest.TestCase):
    """ERPNext controllers call ``self.save()`` from their own ``on_submit`` (for one,
    ``pos_invoice_merge_log.py``). That inner save is a post-submit edit of the document whose
    submit is still running, so it is the same act, not a second one."""

    def setUp(self):
        self.fake = wire(headers={}, markers={})

    def test_the_inner_save_rides_its_own_governed_submit(self):
        doc = submitted()
        doc.flags[act._CONSENT_ESTABLISHED] = act.SUBMIT
        update_inside(doc, doc)
        self.assertIsNone(self.fake.thrown)
        # ...and the stamp still says what a human approved, so later cascades are judged by it.
        self.assertEqual(doc.flags[act._CONSENT_ESTABLISHED], act.SUBMIT)

    def test_a_fresh_object_for_the_same_document_rides_too(self):
        # `frappe.get_doc(self.doctype, self.name).save()` builds a second object with no stamp.
        outer = submitted()
        outer.flags[act._CONSENT_ESTABLISHED] = act.CANCEL
        update_inside(outer, submitted())
        self.assertIsNone(self.fake.thrown)

    def test_one_frame_is_the_actors_own_save_however_the_document_is_stamped(self):
        # A stamp left on the object by an act that already finished is not an enclosing act.
        doc = submitted()
        doc.flags[act._CONSENT_ESTABLISHED] = act.SUBMIT
        with self.assertRaises(FakePermissionError):
            direct_update(doc)

    def test_two_frames_with_no_stamp_are_refused(self):
        doc = submitted()
        with self.assertRaises(FakePermissionError):
            update_inside(doc, doc)

    def test_an_outer_edit_does_not_license_an_inner_one(self):
        doc = submitted()
        doc.flags[act._CONSENT_ESTABLISHED] = act.UPDATE
        with self.assertRaises(FakePermissionError):
            update_inside(doc, doc)

    def test_another_documents_frame_between_does_not_break_the_ride(self):
        doc = submitted()
        doc.flags[act._CONSENT_ESTABLISHED] = act.SUBMIT
        other = submitted("GL Entry", "GLE-1")
        _save(doc, _save, other, _save, doc, act.before_update_after_submit, doc, HANDLER)
        self.assertIsNone(self.fake.thrown)


class TestFrappeWiring(unittest.TestCase):
    REFS = os.environ.get("PACIOLI_FRAPPE_REFS", "")

    def setUp(self):
        if not self.REFS:
            self.skipTest("PACIOLI_FRAPPE_REFS is unset — the frappe WIRING claim is UNVERIFIED "
                          "in this run (point it at a frappe checkout to verify)")
        self.src = "".join((pathlib.Path(self.REFS) / "frappe" / "model" / "document.py")
                           .read_text().split())

    def test_frappe_fires_the_event_this_gate_is_registered_on(self):
        self.assertIn('elifself._action=="update_after_submit":'
                      'self.run_method("before_update_after_submit")', self.src)

    def test_the_post_submit_flag_skips_field_validation_not_the_event(self):
        self.assertIn("defvalidate_update_after_submit(self):"
                      "ifself.flags.ignore_validate_update_after_submit:return", self.src)


class TestWhatAConsentedEditMayCauseAndWhatItMayNot(unittest.TestCase):
    """Second review of the edit gate. A spent ``update`` marker stamps the document, and two
    places in act.py ask only whether SOME stamp is on an enclosing frame. Each consequence is
    decided here on purpose, in one direction or the other, so neither is an accident."""

    def setUp(self):
        self.fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="update"))
        self.invoice = submitted()

    def _inside_the_consented_edit(self, fn):
        def edit():
            act.before_update_after_submit(self.invoice, HANDLER)
            return fn()
        return _save(self.invoice, edit)

    def test_a_document_the_edit_creates_may_be_submitted_under_it(self):
        # DECIDED: allowed. `Sales Invoice.on_update_after_submit` calls
        # `repost_accounting_entries` when an account field changed, which inserts and submits a
        # `Repost Accounting Ledger` (erpnext `accounts_controller.py`). No human can mint a
        # marker for a document that does not exist yet, which is the rule every other act here
        # already follows. The cost is stated in SECURITY.md: an `update` marker can cause a
        # ledger repost.
        repost = FakeDoc(doctype="Repost Accounting Ledger", name="RAL-0001", in_insert=False)

        def create_and_submit():
            insert(repost, act.after_insert, repost, "after_insert")
            _save(repost, act.before_submit, repost, "before_submit")
        self._inside_the_consented_edit(create_and_submit)
        self.assertIsNone(self.fake.thrown)

    def test_a_pre_existing_document_may_not_be_submitted_under_it(self):
        draft = FakeDoc(doctype="Journal Entry", name="JV-DRAFT", in_insert=False)
        with self.assertRaises(FakePermissionError):
            self._inside_the_consented_edit(
                lambda: _save(draft, act.before_submit, draft, "before_submit"))

    def test_a_pre_existing_document_may_not_be_cancelled_or_deleted_under_it(self):
        for handler, name in ((act.before_cancel, "before_cancel"), (act.on_trash, "on_trash")):
            with self.subTest(act=name):
                wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="update"))
                self.invoice = submitted()
                other = submitted("Journal Entry", "JV-0002")
                with self.assertRaises(FakePermissionError):
                    self._inside_the_consented_edit(
                        lambda: _save(other, handler, other, name))

    def test_an_edit_stamp_does_not_stand_in_for_a_submit_marker_at_a_preview(self):
        # `_establish_consent_for_preview` returned early on ANY stamp. An `update` stamp is not
        # consent to post, so the preview must still ask for the submit marker.
        self._inside_the_consented_edit(lambda: None)
        self.assertEqual(self.invoice.flags[act._CONSENT_ESTABLISHED], act.UPDATE)
        with self.assertRaises(FakePermissionError):
            act.before_gl_preview(self.invoice, "before_gl_preview")

    def test_a_submit_stamp_still_lets_a_nested_preview_through(self):
        wire(headers={}, markers={})
        doc = FakeDoc()
        doc.flags[act._CONSENT_ESTABLISHED] = act.SUBMIT
        act.before_gl_preview(doc, "before_gl_preview")


class TestTheOwnActRideIsForEditsOnly(unittest.TestCase):
    """A document saving itself inside its own governed act rides an EDIT. Nothing else: a
    review showed that dropping the ``action == UPDATE`` condition left the suite green, and
    with it a submit marker would have licensed the same document's cancel."""

    def test_a_cancel_of_the_same_document_inside_its_own_governed_submit_is_refused(self):
        wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="submit"))
        doc, again = submitted(), submitted()

        def submit_then_cancel():
            act.before_submit(doc, "before_submit")
            _save(again, act.before_cancel, again, "before_cancel")
        with self.assertRaises(FakePermissionError):
            _save(doc, submit_then_cancel)

    def test_a_delete_of_the_same_document_inside_its_own_governed_cancel_is_refused(self):
        wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="cancel"))
        doc, again = submitted(), submitted()

        def cancel_then_delete():
            act.before_cancel(doc, "before_cancel")
            _save(again, act.on_trash, again, "on_trash")
        with self.assertRaises(FakePermissionError):
            _save(doc, cancel_then_delete)


class TestTheLookupAsksForTheAct(unittest.TestCase):
    """With four acts a document can hold several markers at once. The lookup read ONE row per
    document, whichever frappe's default sort returned (the newest: a bare ``creation`` sorts
    descending outside ``db_query_compat``, ``database/query.py``), so a newer marker for another
    act hid an older live one. GHSA-7p4h-gwc5-w282 (sandhiyabalaji32-tech) reported this as
    "the oldest row always wins"; the direction was wrong and the gap was real, and the report's
    point about the test double held: it kept one marker per document and could not show it.
    """

    KEY = ("Sales Invoice", "SI-0001")

    @staticmethod
    def one(action, *, burned=0, name=None, token=TOKEN):
        row = marker(action=action, burned=burned, name=name or f"PCM-{action}-{burned}",
                     token=token)
        return next(iter(row.values()))

    def test_an_older_live_marker_is_found_behind_a_newer_one_for_another_act(self):
        rows = {self.KEY: [self.one("cancel"), self.one("update")]}
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=rows)
        act.before_cancel(submitted(), "before_cancel")
        self.assertIsNone(fake.thrown)
        self.assertEqual([r["burned"] for r in fake.db.markers[self.KEY]], [1, 0])

    def test_a_newer_live_marker_is_found_behind_nothing(self):
        rows = {self.KEY: [self.one("submit", burned=1), self.one("cancel")]}
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=rows)
        act.before_cancel(submitted(), "before_cancel")
        self.assertIsNone(fake.thrown)

    def test_a_spent_marker_for_the_act_does_not_hide_a_live_one(self):
        # Either order: the human re-minted after the first one was used.
        for rows in ([self.one("update", burned=1), self.one("update")],
                     [self.one("update"), self.one("update", burned=1)]):
            with self.subTest(order=[r["burned"] for r in rows]):
                fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers={self.KEY: rows})
                direct_update(submitted())
                self.assertIsNone(fake.thrown)

    def test_when_only_a_spent_marker_exists_the_refusal_still_says_so(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN},
                    markers={self.KEY: [self.one("update", burned=1), self.one("submit")]})
        with self.assertRaises(FakePermissionError):
            direct_update(submitted())
        self.assertIn("already used", fake.thrown[0])

    def test_when_no_marker_for_the_act_exists_the_refusal_names_the_act_that_does(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN},
                    markers={self.KEY: [self.one("submit")]})
        with self.assertRaises(FakePermissionError):
            direct_update(submitted())
        self.assertIn("authorises submit, not update", fake.thrown[0])

    def test_every_read_states_its_sort_order(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers={self.KEY: [self.one("update")]})
        direct_update(submitted())
        self.assertTrue(fake.db.marker_reads)
        for filters, order_by in fake.db.marker_reads:
            self.assertEqual(order_by, "creation desc")

    def test_the_first_read_asks_for_this_act_and_an_unspent_row(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers={self.KEY: [self.one("update")]})
        direct_update(submitted())
        filters, _ = fake.db.marker_reads[0]
        self.assertEqual(filters, {"ref_doctype": "Sales Invoice", "ref_docname": "SI-0001",
                                   "ref_action": "update", "burned": 0})

    def test_the_preview_reads_the_submit_marker(self):
        rows = {self.KEY: [self.one("submit"), self.one("update")]}
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=rows)
        act.before_gl_preview(FakeDoc(in_insert=False), "before_gl_preview")
        self.assertIsNone(fake.thrown)


if __name__ == "__main__":
    unittest.main()
