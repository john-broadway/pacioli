# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Consent verification at the floor.

Why this gate exists: on 2026-07-25 the live bench proved that scoping the broker's own
credential to exactly the calls it makes does NOT stop whoever holds that credential from
making those calls directly. The broker must be allowed to submit invoices, so its key is
allowed to submit invoices, so a stolen key submits invoices — no plan, no marker, no receipt.
Verified end to end: the ledger moved.

Credential scope cannot close that, by construction. The floor has to check consent itself.

Two properties added after the first pass (second-lens review, same day). Both are the same
principle the document binding already encodes, carried one step further:

* **Consent is to an ACT, not just a document.** Cancel reverses GL entries. A human who
  consented to posting an invoice did not consent to reversing it, and both are docstatus
  moves on the same document — so a submit marker must not spend on a cancel.
* **A credential cannot mint its own consent.** If the key that acts is the key that minted
  the marker, the gate authorises itself and the whole thing is theatre. The marker records
  who minted it; the floor compares.
"""
import unittest

from pacioli_guard.scope import (
    consent_token_hash,
    consent_verdict,
    docstatus_action,
    docstatus_target_docname,
)

GOOD = "fixture-consent-token-not-a-real-secret"
HUMAN = "operator@example.com"
AGENT = "pacioli-broker@example.com"


def record(token=GOOD, doctype="Sales Invoice", docname="ACC-SINV-2026-00004",
           expires_at=2000, burned=0, action="submit", minted_by=HUMAN):
    return {"token_hash": consent_token_hash(token), "doctype": doctype,
            "docname": docname, "expires_at": expires_at, "burned": burned,
            "action": action, "minted_by": minted_by}


def verdict(**over):
    """Every call states the act and the acting principal — there is no implicit default."""
    kw = {"presented": GOOD, "doctype": "Sales Invoice", "docname": "ACC-SINV-2026-00004",
          "action": "submit", "record": record(), "now": 1000, "principal": AGENT}
    kw.update(over)
    return consent_verdict(**kw)


class TestTheRefusalTextDoesNotLie(unittest.TestCase):
    """John, 2026-07-26: **"our code must not lie."**

    A refusal is the one message an operator is guaranteed to read, at the exact moment they are
    blocked and looking for the remedy. Every sentence in it is a claim this software makes about
    itself, and a false one there costs more than a false one in a README — it sends someone down a
    road that does not exist while their write is failing.

    These tests assert the refusal text is TRUE, not that it is well written.
    """

    def test_a_cancel_refusal_does_not_say_the_document_must_be_SUBMITTED(self):
        # The reason hardcoded "before it can be submitted" for every act. Observed live on the
        # bench in this exact shape: "requires human consent to cancel a document, and no live
        # consent marker ... before it can be submitted". The act is a parameter; the text ignored
        # it and named the wrong one.
        allowed, reason = verdict(action="cancel", record=None)
        self.assertFalse(allowed)
        self.assertNotIn("submitted", reason)

    def test_a_submit_refusal_still_reads_correctly(self):
        allowed, reason = verdict(action="submit", record=None)
        self.assertFalse(allowed)
        self.assertIn("submit", reason)

    def test_the_remedy_does_not_promise_a_CLI_this_package_does_not_ship(self):
        """`pacioli mint` lives in the SEPARATE broker distribution (`pip install pacioli`).

        `pacioli-guard` ships no console script at all — its documented deployment is a bare
        `bench install-app` on a customer bench, with no broker anywhere near it. Telling that
        operator to run `pacioli mint` names a command their shell does not have.

        **Strengthened 2026-07-29 from "name the package" to "do not name it at all", because the
        command cannot do the job even where it IS installed.** `pacioli mint` takes a `plan_id`
        positionally and writes a plan-bound marker into the BROKER's own store
        (`store.mint_marker(token, plan_id, ...)`). It never connects to the books and never
        creates the `Pacioli Consent Marker` row that this gate reads. Naming it sends an operator
        to build the wrong object in the wrong place — which the old conditional assertion allowed,
        since it only checked that the package was attributed.
        """
        allowed, reason = verdict(action="submit", record=None)
        self.assertFalse(allowed)
        self.assertNotIn("pacioli mint", reason)

    def test_the_remedy_names_the_object_to_create_and_not_the_desk_form(self):
        """It must name what has to EXIST, and not a form that refuses the operator.

        The first fix for the `pacioli mint` lie said "in the desk UI" instead — and that is a
        second lie for a mechanical reason: `token_hash` is `reqd: 1` AND `read_only: 1` on the
        DocType with no default, and `PacioliConsentMarker.before_insert` sets only `minted_by`.
        So the desk form cannot supply a mandatory field and the save cannot succeed. There is
        also nowhere in that form to see the raw token, which the human must generate and keep out
        of the credential's reach. Naming the desk UI would send an operator to a form that
        refuses them, which is the same defect wearing a friendlier hat.
        """
        allowed, reason = verdict(action="submit", record=None)
        self.assertFalse(allowed)
        self.assertIn("Pacioli Consent Marker", reason)
        self.assertNotIn("desk", reason.lower())
        # The token is the human's to generate — say so, because the marker stores only its hash.
        self.assertIn("token", reason.lower())

    def test_the_remedy_names_the_route_THIS_package_actually_ships(self):
        """Now that one exists, the refusal has to point at it.

        The route was struck from every refusal because there genuinely was none — the only way to
        mint was an ad-hoc script in a container. `pacioli_guard.mint.mint_consent_marker` (0.13.0)
        is that route, it lives in THIS package, and it is reachable with `bench execute` on the
        books box. A refusal that describes the object but not the way to make it leaves the
        operator exactly as stuck as before, just more accurately.
        """
        allowed, reason = verdict(action="submit", record=None)
        self.assertFalse(allowed)
        self.assertIn("pacioli_guard.mint.mint_consent_marker", reason)
        self.assertIn("bench", reason.lower())
        # Still must not name the broker CLI, which cannot make this object.
        self.assertNotIn("pacioli mint", reason)


class TestConsentVerdict(unittest.TestCase):
    def test_refuses_when_no_marker_is_presented(self):
        """The bypass, exactly: the right credential making the right call with no consent."""
        allowed, reason = verdict(presented=None, record=None)
        self.assertFalse(allowed)
        self.assertIn("no consent marker", reason.lower())

    def test_allows_a_live_marker_bound_to_this_document_and_act(self):
        allowed, reason = verdict()
        self.assertTrue(allowed, reason)

    def test_refuses_when_no_marker_was_ever_minted_for_this_document(self):
        allowed, reason = verdict(record=None)
        self.assertFalse(allowed)
        self.assertIn("no live consent marker", reason.lower())

    def test_refuses_an_expired_marker(self):
        allowed, reason = verdict(record=record(expires_at=999))
        self.assertFalse(allowed)
        self.assertIn("expired", reason.lower())

    def test_refuses_a_marker_already_burned(self):
        """Single use. Replay is the whole reason the marker is burned on consumption."""
        allowed, reason = verdict(record=record(burned=1))
        self.assertFalse(allowed)
        self.assertIn("already used", reason.lower())

    def test_refuses_a_marker_minted_for_a_different_document(self):
        """Consent for one invoice is not consent for the next one."""
        allowed, reason = verdict(docname="ACC-SINV-2026-00099")
        self.assertFalse(allowed)
        self.assertIn("different document", reason.lower())

    def test_refuses_a_marker_minted_for_a_different_doctype(self):
        allowed, reason = verdict(doctype="Journal Entry")
        self.assertFalse(allowed)
        self.assertIn("different document", reason.lower())

    def test_refuses_a_forged_token_against_a_real_record(self):
        """The record is live and correctly bound; only the token is wrong."""
        allowed, reason = verdict(presented="not-the-real-token-not-the-real-tok")
        self.assertFalse(allowed)
        self.assertIn("does not match", reason.lower())

    def test_expiry_is_checked_before_the_token_matches(self):
        """An expired marker refuses even when the presented token is correct."""
        allowed, _ = verdict(record=record(expires_at=1000))
        self.assertFalse(allowed)


class TestConsentIsBoundToTheAct(unittest.TestCase):
    """A marker names one act. Cancel is not submit, and reversing a posting is its own decision."""

    def test_a_submit_marker_does_not_authorise_a_cancel(self):
        allowed, reason = verdict(action="cancel")
        self.assertFalse(allowed)
        self.assertIn("different act", reason.lower())

    def test_a_cancel_marker_authorises_a_cancel(self):
        allowed, reason = verdict(action="cancel", record=record(action="cancel"))
        self.assertTrue(allowed, reason)

    def test_refuses_when_the_act_cannot_be_determined(self):
        """An unreadable docstatus move is not spendable — deny-biased, like every other branch."""
        allowed, reason = verdict(action=None)
        self.assertFalse(allowed)
        self.assertIn("act", reason.lower())

    def test_refuses_a_marker_that_names_no_act(self):
        allowed, reason = verdict(record=record(action=None))
        self.assertFalse(allowed)
        self.assertIn("act", reason.lower())

    def test_act_binding_is_checked_before_the_token_matches(self):
        allowed, _ = verdict(action="cancel", presented="not-the-real-token-not-the-real-t")
        self.assertFalse(allowed)


class TestACredentialCannotMintItsOwnConsent(unittest.TestCase):
    """The separation the whole gate rests on. Documented as law 2026-07-25; enforced here.

    Without this, a stolen key mints a marker and spends it in the same breath, and the floor
    dutifully records that consent was given. The marker must come from another hand.
    """

    def test_refuses_when_the_acting_credential_minted_the_marker(self):
        allowed, reason = verdict(record=record(minted_by=AGENT))
        self.assertFalse(allowed)
        self.assertIn("own consent", reason.lower())

    def test_case_and_whitespace_do_not_defeat_the_comparison(self):
        allowed, reason = verdict(record=record(minted_by="  PACIOLI-Broker@Example.com "))
        self.assertFalse(allowed)
        self.assertIn("own consent", reason.lower())

    def test_refuses_a_marker_with_no_recorded_minter(self):
        """Unprovable separation is not separation."""
        allowed, reason = verdict(record=record(minted_by=None))
        self.assertFalse(allowed)
        self.assertIn("minted", reason.lower())

    def test_refuses_when_the_acting_principal_is_unknown(self):
        allowed, reason = verdict(principal=None)
        self.assertFalse(allowed)
        self.assertIn("principal", reason.lower())

    def test_allows_when_a_different_hand_minted_it(self):
        allowed, reason = verdict(record=record(minted_by=HUMAN))
        self.assertTrue(allowed, reason)


class TestDocstatusAction(unittest.TestCase):
    """Which act is this call attempting? Pure, and deny-biased on anything it cannot read."""

    def test_a_document_submit_method(self):
        self.assertEqual(docstatus_action("method", "Sales Invoice.submit", "POST", {}), "submit")

    def test_a_document_cancel_method(self):
        self.assertEqual(docstatus_action("method", "Sales Invoice.cancel", "POST", {}), "cancel")

    def test_the_desk_save_endpoint_submitting(self):
        self.assertEqual(
            docstatus_action("method", "frappe.desk.form.save.savedocs", "POST",
                             {"action": "Submit"}), "submit")

    def test_the_desk_save_endpoint_cancelling(self):
        self.assertEqual(
            docstatus_action("method", "frappe.desk.form.save.savedocs", "POST",
                             {"action": "Cancel"}), "cancel")

    def test_the_desk_save_endpoint_with_an_unknown_action_is_unreadable(self):
        self.assertIsNone(
            docstatus_action("method", "frappe.desk.form.save.savedocs", "POST",
                             {"action": "Update"}))

    def test_a_raw_update_setting_docstatus_one(self):
        self.assertEqual(docstatus_action("resource", ("Sales Invoice", "X"), "PUT",
                                          {"docstatus": 1}), "submit")

    def test_a_raw_update_setting_docstatus_two(self):
        self.assertEqual(docstatus_action("resource", ("Sales Invoice", "X"), "PUT",
                                          {"docstatus": "2"}), "cancel")

    def test_a_create_inserted_as_submitted(self):
        self.assertEqual(docstatus_action("resource", ("Sales Invoice",), "POST",
                                          {"docstatus": 1}), "submit")

    def test_an_unreadable_docstatus_value(self):
        self.assertIsNone(docstatus_action("resource", ("Sales Invoice", "X"), "PUT",
                                           {"docstatus": "banana"}))

    def test_a_plain_read_names_no_act(self):
        self.assertIsNone(docstatus_action("resource", ("Sales Invoice", "X"), "GET", {}))


class TestDocstatusTargetDocname(unittest.TestCase):
    """Consent is bound to one document, so the gate must know WHICH document is being moved.

    Deny-biased on ambiguity, mirroring _run_doc_method_doctype: a legitimate client names one
    document in one place. Only a spoof names two, and an unresolved name must refuse rather
    than let a marker minted for one invoice authorize another.
    """

    def test_reads_dn_from_a_run_doc_method_form(self):
        self.assertEqual(
            docstatus_target_docname("/api/method/run_doc_method",
                                     {"dt": "Sales Invoice", "dn": "ACC-SINV-2026-00004"}),
            "ACC-SINV-2026-00004")

    def test_reads_name_from_a_document_body(self):
        self.assertEqual(
            docstatus_target_docname(
                "/api/method/frappe.client.submit",
                {"doc": {"doctype": "Sales Invoice", "name": "ACC-SINV-2026-00004",
                         "docstatus": 1}}),
            "ACC-SINV-2026-00004")

    def test_reads_the_document_from_a_resource_path(self):
        self.assertEqual(
            docstatus_target_docname("/api/resource/Sales Invoice/ACC-SINV-2026-00004", {}),
            "ACC-SINV-2026-00004")

    def test_percent_encoded_path_segment_is_decoded(self):
        self.assertEqual(
            docstatus_target_docname("/api/resource/Sales%20Invoice/ACC-SINV-2026-00004", {}),
            "ACC-SINV-2026-00004")

    def test_refuses_when_nothing_names_a_document(self):
        self.assertIsNone(docstatus_target_docname("/api/method/run_doc_method",
                                                   {"dt": "Sales Invoice"}))

    def test_refuses_when_two_sources_name_different_documents(self):
        """The same spoof shape the doctype resolver already defends against."""
        self.assertIsNone(docstatus_target_docname(
            "/api/method/run_doc_method",
            {"dn": "ACC-SINV-2026-00004",
             "doc": {"doctype": "Sales Invoice", "name": "ACC-SINV-2026-00099"}}))

    def test_resolves_when_two_sources_agree(self):
        self.assertEqual(
            docstatus_target_docname(
                "/api/method/run_doc_method",
                {"dn": "ACC-SINV-2026-00004",
                 "doc": {"doctype": "Sales Invoice", "name": "ACC-SINV-2026-00004"}}),
            "ACC-SINV-2026-00004")

    def test_a_doctype_only_resource_path_names_no_document(self):
        """POST /api/resource/Sales Invoice creates; there is no document to consent to yet."""
        self.assertIsNone(docstatus_target_docname("/api/resource/Sales Invoice", {}))


class TestConsentTokenHash(unittest.TestCase):
    def test_hash_is_stable_for_the_same_token(self):
        self.assertEqual(consent_token_hash(GOOD), consent_token_hash(GOOD))

    def test_hash_differs_for_different_tokens(self):
        self.assertNotEqual(consent_token_hash(GOOD), consent_token_hash(GOOD + "x"))

    def test_hash_does_not_contain_the_token(self):
        """A stored marker must not be reversible into the token a human handed the agent."""
        self.assertNotIn(GOOD, consent_token_hash(GOOD))


if __name__ == "__main__":
    unittest.main()


class TestConsentStatusIsGrantableAsABareMethod(unittest.TestCase):
    """The doctor probe must be reachable, and reachable ONLY on the safe-method terms.

    `is_permitted` deliberately refuses a bare (doctype-unresolved) method grant unless the name
    is on the small curated SAFE_METHODS list — a classifier cannot enumerate every dangerous
    generic RPC, so the default is denied-until-reviewed. The diagnostic endpoint was granted on
    the live bench and still refused for exactly that reason (2026-07-25), which is the design
    working. It qualifies on the same terms as the others: no arguments, no writes, reports only
    the calling session's own user.
    """

    def test_it_is_on_the_curated_safe_list(self):
        from pacioli_guard.scope import SAFE_METHODS
        self.assertIn("pacioli_guard.api.consent_status", SAFE_METHODS)

    def test_a_granted_credential_may_call_it(self):
        from pacioli_guard.scope import ApiScope, is_permitted
        scope = ApiScope.from_grant(1, ["pacioli_guard.api.consent_status"], [])
        self.assertTrue(is_permitted(scope, "method", "pacioli_guard.api.consent_status"))

    def test_an_ungranted_credential_may_not(self):
        """Being safe does not mean being free — it still has to be in the allowlist."""
        from pacioli_guard.scope import ApiScope, is_permitted
        scope = ApiScope.from_grant(1, ["frappe.auth.get_logged_user"], [])
        self.assertFalse(is_permitted(scope, "method", "pacioli_guard.api.consent_status"))


# ---- F3: minted_by is established by the server, not asserted by the caller --------------------
# The controller does `import frappe` + `from frappe.model.document import Document`, so the stub
# needs that module chain. Same posture as test_enforce.py: satisfy the hard imports bench-free,
# then drive the REAL controller code rather than a reimplementation of it.
import sys
import types

_frappe = sys.modules.setdefault("frappe", types.ModuleType("frappe"))
_frappe_model = sys.modules.setdefault("frappe.model", types.ModuleType("frappe.model"))
_frappe_document = sys.modules.setdefault(
    "frappe.model.document", types.ModuleType("frappe.model.document"))
if not hasattr(_frappe_document, "Document"):
    class _StubDocument:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    _frappe_document.Document = _StubDocument
_frappe.model = _frappe_model
_frappe_model.document = _frappe_document
# `api.py` evaluates `@frappe.whitelist()` at import time, so the stub needs a passthrough.
if not hasattr(_frappe, "whitelist"):
    _frappe.whitelist = lambda *a, **k: (lambda fn: fn)

from pacioli_guard.scoping.doctype.pacioli_consent_marker.pacioli_consent_marker import (  # noqa: E402
    PacioliConsentMarker,
)


class TestMintedByIsServerBound(unittest.TestCase):
    """Floor audit F3 (2026-07-26). ``consent_verdict`` refuses a self-minted marker, and that is
    the property that closed the 2026-07-25 bypass — but the field it reads was never established
    by anything. ``minted_by`` is ``read_only`` on the DocType, which is a form property and not a
    server-side wall, and the controller was ``pass``, so the stored value was whatever the creator
    supplied. These lock the binding, and specifically lock that it OVERWRITES."""

    def setUp(self):
        self._saved = getattr(_frappe, "session", None)
        _frappe.session = types.SimpleNamespace(user="operator@example.com")

    def tearDown(self):
        if self._saved is None:
            if hasattr(_frappe, "session"):
                del _frappe.session
        else:
            _frappe.session = self._saved

    def test_minted_by_comes_from_the_session(self):
        doc = PacioliConsentMarker()
        doc.before_insert()
        self.assertEqual(doc.minted_by, "operator@example.com")

    def test_a_caller_supplied_minted_by_is_overwritten(self):
        # THE property. Fill-when-blank would let a credential name any other principal as its
        # minter and pass the separation check with a string of its own choosing.
        doc = PacioliConsentMarker(minted_by="Administrator")
        doc.before_insert()
        self.assertEqual(doc.minted_by, "operator@example.com")

    def test_the_acting_credential_cannot_launder_itself_through_the_field(self):
        # A credential inserting its own marker gets its OWN name recorded, which is exactly what
        # consent_verdict's separation branch then refuses. Bound field -> real mechanism.
        _frappe.session = types.SimpleNamespace(user="agent@example.com")
        doc = PacioliConsentMarker(minted_by="operator@example.com")
        doc.before_insert()
        self.assertEqual(doc.minted_by, "agent@example.com")
        allowed, reason = consent_verdict(
            "tok", "Sales Invoice", "SI-001", "submit",
            {"name": "m1", "token_hash": consent_token_hash("tok"), "doctype": "Sales Invoice",
             "docname": "SI-001", "action": "submit", "expires_at": 9e18, "burned": 0,
             "minted_by": doc.minted_by},
            0.0, "agent@example.com")
        self.assertFalse(allowed)
        self.assertIn("mint", reason.lower())


class TestConsentStatusSelfReport(unittest.TestCase):
    """The floor's self-report (``pacioli_guard.api.consent_status``). It had no behaviour tests at
    all, which is a gap in a security-relevant endpoint: it is the one thing on SAFE_METHODS that
    an operator's `doctor` run believes. Its contract is POSTURE, never contents — it must never
    return DocType names, and anything it cannot establish must read as not-established."""

    class _Row:
        def __init__(self, ref_doctype):
            self.ref_doctype = ref_doctype

    class _Doc:
        def __init__(self, allow_resource=0, rows=(), require_consent=0):
            self.allow_resource = allow_resource
            self.resource_doctypes = list(rows)
            self.require_consent = require_consent

    class _FakeDB:
        def __init__(self, doc):
            self._doc = doc

        def get_value(self, doctype, filters, fieldname=None, as_dict=False):
            if self._doc is None:
                return None
            if isinstance(filters, dict):      # name discovery
                return "AKS::seat"
            return getattr(self._doc, fieldname, None)

    def _api(self, doc):
        from pacioli_guard import api
        fake = types.ModuleType("frappe")
        fake.session = types.SimpleNamespace(user="seat@example.com")
        fake.db = self._FakeDB(doc)
        fake.get_doc = lambda dt, name: doc
        api.frappe = fake
        return api

    def test_unscoped_is_the_loudest_state(self):
        api = self._api(None)
        out = api.consent_status()
        self.assertFalse(out["scoped"])
        self.assertFalse(out["require_consent"])
        self.assertEqual(out["resource_posture"], "unknown")

    def test_narrow_grant_reports_narrow(self):
        api = self._api(self._Doc(allow_resource=1, rows=[self._Row("Sales Invoice")],
                                  require_consent=1))
        out = api.consent_status()
        self.assertTrue(out["scoped"])
        self.assertTrue(out["require_consent"])
        self.assertEqual(out["resource_posture"], "narrow")

    def test_wildcard_row_reports_all_doctypes(self):
        api = self._api(self._Doc(allow_resource=1, rows=[self._Row("*")]))
        self.assertEqual(api.consent_status()["resource_posture"], "all_doctypes")

    def test_empty_allowlist_reports_denies_all(self):
        # guard 0.8.0: this used to mean "every DocType". It now denies, and that is worth saying —
        # it usually means a half-finished grant.
        api = self._api(self._Doc(allow_resource=1, rows=[]))
        self.assertEqual(api.consent_status()["resource_posture"], "denies_all")

    def test_blank_rows_are_not_mistaken_for_a_grant(self):
        api = self._api(self._Doc(allow_resource=1, rows=[self._Row("  "), self._Row(None)]))
        self.assertEqual(api.consent_status()["resource_posture"], "denies_all")

    def test_master_check_off_reports_off(self):
        api = self._api(self._Doc(allow_resource=0, rows=[self._Row("Sales Invoice")]))
        self.assertEqual(api.consent_status()["resource_posture"], "off")

    def test_it_never_returns_doctype_names(self):
        # The contract: posture, not contents. A leak here would hand one seat's allowlist to
        # whoever holds another seat's key.
        api = self._api(self._Doc(allow_resource=1, rows=[self._Row("Sales Invoice")],
                                  require_consent=1))
        self.assertNotIn("Sales Invoice", repr(api.consent_status()))

    def test_a_broken_posture_read_cannot_break_the_consent_report(self):
        doc = self._Doc(allow_resource=1, rows=[self._Row("Sales Invoice")], require_consent=1)
        api = self._api(doc)

        def boom(dt, name):
            raise RuntimeError("doc read failed")

        api.frappe.get_doc = boom
        out = api.consent_status()
        self.assertTrue(out["require_consent"])          # still established
        self.assertEqual(out["resource_posture"], "unknown")   # never reported as fine


# ---- The gate can be REQUESTED and not LOADED, and only a probe can tell -----------------------
# 2026-07-29, public bench: `require_consent` was 1, the installed hooks.py was byte-identical to
# source and declared all three doc_events, and `get_hooks("doc_events")["*"]` returned None for
# every one of them (stale cache from a site created when guard shipped auth_hooks only). Scope
# rode auth_hooks and worked, so the floor looked present. A submit with no marker returned 200
# and moved the ledger. 471 tests were green throughout, because they test the code and the bug
# was in the registry -- so the probe that closes it gets tests that fail on the real broken shape.
class TestGateRegisteredProbe(unittest.TestCase):
    def _probe(self, hooks):
        # Patch the module object `api` actually holds, not sys.modules["frappe"] by name:
        # sibling tests in this file rebind the stub, so patching by name passed alone and
        # failed in suite order. Patch what the code under test dereferences.
        from pacioli_guard import api
        api.frappe.get_hooks = (hooks if callable(hooks) else (lambda name: hooks))
        return api._gate_registered()

    def test_both_handlers_loaded_is_registered(self):
        self.assertTrue(self._probe({"*": {
            "before_submit": ["pacioli_guard.act.before_submit"],
            "before_cancel": ["pacioli_guard.act.before_cancel"]}}))

    def test_the_2026_07_29_bench_state_is_NOT_registered(self):
        """The exact observed shape: keys present, values None."""
        self.assertFalse(self._probe({"*": {"before_submit": None, "before_cancel": None}}))

    def test_empty_registry_is_not_registered(self):
        self.assertFalse(self._probe({}))

    def test_a_half_loaded_gate_is_not_registered(self):
        """submit gated and cancel not is not 'mostly fine' -- consent to post would be spendable
        on a reversal, which is the very thing before_cancel exists to refuse."""
        self.assertFalse(self._probe({"*": {
            "before_submit": ["pacioli_guard.act.before_submit"]}}))

    def test_another_apps_handler_does_not_count(self):
        """Presence of SOME before_submit is not presence of OURS."""
        self.assertFalse(self._probe({"*": {
            "before_submit": ["someone_else.before_submit"],
            "before_cancel": ["someone_else.before_cancel"]}}))

    def test_a_bare_string_entry_still_counts(self):
        """frappe hands back a str rather than a list for a single handler."""
        self.assertTrue(self._probe({"*": {
            "before_submit": "pacioli_guard.act.before_submit",
            "before_cancel": "pacioli_guard.act.before_cancel"}}))

    def test_it_is_deny_biased_when_it_cannot_look(self):
        """Cannot establish -> NOT established. Never 'probably fine'."""
        def boom(_name):
            raise RuntimeError("no site context")
        self.assertFalse(self._probe(boom))


class TestConsentStatusReportsEnforcementNotIntention(unittest.TestCase):
    """`require_consent` alone actively misled on 2026-07-29. The conjunction is the answer."""

    def _status(self, require_consent, registered):
        from pacioli_guard import api
        api.frappe.session = types.SimpleNamespace(user="broker@example.com")
        api.frappe.db = types.SimpleNamespace(
            get_value=lambda dt, a, b=None, **k: ("SCOPE-1" if b == "name" else require_consent))
        api.frappe.get_hooks = lambda name: ({"*": {
            "before_submit": ["pacioli_guard.act.before_submit"],
            "before_cancel": ["pacioli_guard.act.before_cancel"]}} if registered else {})
        real_posture = api._resource_posture
        api._resource_posture = lambda user: "narrow"
        self.addCleanup(setattr, api, "_resource_posture", real_posture)
        return api.consent_status()

    def test_requested_but_not_loaded_reports_NOT_enforced(self):
        out = self._status(require_consent=1, registered=False)
        self.assertTrue(out["require_consent"])       # the grant asks for it
        self.assertFalse(out["gate_registered"])      # the machinery is absent
        self.assertFalse(out["consent_enforced"])     # so the honest answer is NO

    def test_requested_and_loaded_reports_enforced(self):
        out = self._status(require_consent=1, registered=True)
        self.assertTrue(out["consent_enforced"])

    def test_loaded_but_not_requested_is_not_enforced_for_this_credential(self):
        out = self._status(require_consent=0, registered=True)
        self.assertTrue(out["gate_registered"])
        self.assertFalse(out["consent_enforced"])


class TestPlanConsentMarker(unittest.TestCase):
    """The pure half of the human mint route.

    Until now the only way to create a floor marker was an ad-hoc script running as Administrator
    inside the container (`docs/plans/2026-07-26-consent-ceremony-decision.md` records this as
    Option B's outstanding cost). That is why every refusal had to be rewritten to promise no route:
    there wasn't one. Michelle's books have consent enforced and zero markers ever minted, so the
    first governed write attempted there needs a route a human can actually take.

    Randomness and the clock stay OUT of this function — the caller passes the token and `now` —
    so the row it computes is fully determined and testable, exactly like `consent_verdict`.
    """

    def plan(self, **over):
        from pacioli_guard.scope import plan_consent_marker
        kw = {"ref_doctype": "Sales Invoice", "ref_docname": "ACC-SINV-2026-00004",
              "ref_action": "submit", "token": GOOD, "ttl_seconds": 900, "now": 1000}
        kw.update(over)
        return plan_consent_marker(**kw)

    def test_it_stores_the_hash_and_never_the_token(self):
        ok, reason, row = self.plan()
        self.assertTrue(ok, reason)
        self.assertEqual(row["token_hash"], consent_token_hash(GOOD))
        # The whole point of hash-only storage: reading every row must not yield a spendable token.
        self.assertNotIn(GOOD, repr(row))

    def test_it_binds_the_document_and_the_act(self):
        ok, _, row = self.plan()
        self.assertTrue(ok)
        self.assertEqual(row["ref_doctype"], "Sales Invoice")
        self.assertEqual(row["ref_docname"], "ACC-SINV-2026-00004")
        self.assertEqual(row["ref_action"], "submit")

    def test_the_expiry_is_now_plus_the_ttl(self):
        ok, _, row = self.plan(now=1000, ttl_seconds=900)
        self.assertTrue(ok)
        self.assertEqual(row["expires_at"], 1900)

    def test_it_never_pre_burns_a_marker(self):
        ok, _, row = self.plan()
        self.assertTrue(ok)
        self.assertEqual(row["burned"], 0)

    def test_it_does_not_set_minted_by(self):
        """`before_insert` binds `minted_by` from the session and OVERWRITES what the caller sends.
        Emitting it here would imply this function establishes separation. It does not; the server
        does, and floor audit F3 exists because a caller-supplied value is worth nothing."""
        ok, _, row = self.plan()
        self.assertTrue(ok)
        self.assertNotIn("minted_by", row)

    def test_an_unknown_act_is_refused(self):
        # The gate only ever asks about submit/cancel, and the DocType's Select allows only those.
        # A marker for "delete" would be a row nothing can spend, sitting in the books looking like
        # consent.
        for bad in ("delete", "amend", "SUBMIT", "", None):
            ok, reason, row = self.plan(ref_action=bad)
            self.assertFalse(ok, f"{bad!r} must be refused")
            self.assertIsNone(row)
            self.assertIn("act", reason.lower())

    def test_a_blank_document_is_refused(self):
        for field in ("ref_doctype", "ref_docname"):
            for bad in ("", "   ", None):
                ok, reason, row = self.plan(**{field: bad})
                self.assertFalse(ok, f"{field}={bad!r} must be refused")
                self.assertIsNone(row)

    def test_a_ttl_outside_the_short_lived_range_is_refused(self):
        # Mirrors the broker CLI's own 1..86400 range: a consent grant is meant to be short-lived,
        # and an unbounded TTL is a standing permission wearing a marker's clothes.
        for bad in (0, -1, 86_401, None, "900"):
            ok, reason, row = self.plan(ttl_seconds=bad)
            self.assertFalse(ok, f"ttl={bad!r} must be refused")
            self.assertIsNone(row)
            self.assertIn("ttl", reason.lower())

    def test_a_weak_or_missing_token_is_refused(self):
        """The token is the whole secret. A short one is guessable, and a blank one hashes to a
        constant every other blank-token marker shares."""
        for bad in ("", None, "short", 12345):
            ok, reason, row = self.plan(token=bad)
            self.assertFalse(ok, f"token={bad!r} must be refused")
            self.assertIsNone(row)
            self.assertIn("token", reason.lower())

    def test_a_nonfinite_clock_is_refused(self):
        # `consent_verdict` already refuses a NaN `now` because the expiry compare goes silently
        # false. Minting under the same clock must not produce a row that verify would then reject.
        for bad in (float("nan"), float("inf"), None, "1000"):
            ok, reason, row = self.plan(now=bad)
            self.assertFalse(ok, f"now={bad!r} must be refused")
            self.assertIsNone(row)


class TestTheExpiryReaderRefusesAnUnusableNumber(unittest.TestCase):
    """`_epoch`'s stated contract is "unreadable = None = treated as expired" — deny-biased. An
    independent review (2026-07-29) found the read side missing the very `isfinite` guard the write
    side (`plan_consent_marker`) got in this same change.

    `_epoch(float("inf"))` returned `inf`, and `consent_verdict` compares `now >= expires_at`.
    `now >= inf` is False under IEEE-754, so such a marker NEVER expires — permanently valid,
    the exact opposite of the deny-biased contract. Same for NaN, where every comparison is False.

    Not reachable through the current writer (a `DATETIME(6)` column rejects "inf"), so this is a
    defect-in-waiting rather than a live bypass — but the write side was hardened against precisely
    this and the reader it was modelled on was not.
    """

    def _epoch(self, value):
        import sys
        import types
        sys.modules.setdefault("frappe", types.ModuleType("frappe"))
        from pacioli_guard import enforce
        return enforce._epoch(value)

    def test_a_non_finite_expiry_reads_as_unreadable(self):
        for bad in (float("inf"), float("-inf"), float("nan")):
            self.assertIsNone(self._epoch(bad), f"{bad!r} must be unreadable, never an instant")

    def test_a_real_epoch_number_still_reads(self):
        # The guard must not break the legitimate numeric path.
        self.assertEqual(self._epoch(1785380000), 1785380000.0)
        self.assertEqual(self._epoch(1785380000.5), 1785380000.5)

    def test_an_unreadable_expiry_makes_consent_verdict_refuse(self):
        """The property that actually matters: unreadable must mean REFUSED, end to end."""
        allowed, reason = verdict(record=record(expires_at=float("inf")))
        self.assertFalse(allowed, "a marker with a non-finite expiry must not authorise anything")
        self.assertIn("expired", reason.lower())

    def test_a_non_finite_CLOCK_also_refuses(self):
        """The other side of the same comparison. `nan >= expires_at` is False too, so an unreadable
        clock would have made every live marker pass the expiry check. The broker's pure consent core
        has guarded this since it was written; this one did not until now."""
        for bad in (float("nan"), float("inf"), None, "1000", True):
            allowed, reason = verdict(now=bad)
            self.assertFalse(allowed, f"now={bad!r} must refuse, not authorise")
            self.assertIn("expired", reason.lower())


class TestAMarkerIsImmutableAfterMinting(unittest.TestCase):
    """The clamp a review asked for (2026-07-29), as a pure decision.

    `before_insert` binds `minted_by`, but it fires ONLY on create. So every authoritative field on a
    minted marker was editable afterwards by anything with doctype write permission: the F3 binding
    did not survive an UPDATE, and `expires_at_epoch` — introduced as *the* authoritative expiry —
    had no protection at all. `read_only` is a form property and walls off no API write; no
    `permlevel` is declared. Reaching it needs `System Manager` and the doctype is in
    `_UNGRANTABLE_DOCTYPES`, so a scoped api-key credential is hard-denied — but `is_permitted`
    returns True for a credential with NO grant row, and OAuth `Bearer` never reaches `check_scope`.

    ⭐ **`burned` must stay mutable**, or spending a marker becomes impossible. It is spent by a raw
    `UPDATE ... WHERE burned = 0` (`enforce._claim_consent`) which skips the document lifecycle
    entirely, so this clamp never sees the spend — but a field-level allowance is the honest way to
    say so rather than relying on that.
    """

    def _violations(self, before, after):
        from pacioli_guard.scope import immutable_marker_violations
        return immutable_marker_violations(before, after)

    def _row(self, **over):
        row = {"ref_doctype": "Sales Invoice", "ref_docname": "ACC-SINV-2026-00004",
               "ref_action": "submit", "token_hash": consent_token_hash(GOOD),
               "expires_at": "2026-07-29 22:00:00", "expires_at_epoch": 1785380000.0,
               "burned": 0, "minted_by": HUMAN}
        row.update(over)
        return row

    def test_an_unchanged_marker_is_fine(self):
        self.assertEqual(self._violations(self._row(), self._row()), [])

    def test_burning_a_marker_is_allowed(self):
        # The one field that must move. Spending happens outside the document layer anyway, but a
        # clamp that blocked it would be a gate that stops the gate.
        self.assertEqual(self._violations(self._row(), self._row(burned=1)), [])

    def test_extending_the_authoritative_expiry_is_refused(self):
        """The reason this exists: a bigger epoch makes a marker effectively immortal."""
        self.assertEqual(self._violations(self._row(), self._row(expires_at_epoch=9e9)),
                         ["expires_at_epoch"])

    def test_rewriting_the_readable_expiry_is_refused(self):
        self.assertIn("expires_at",
                      self._violations(self._row(), self._row(expires_at="2030-01-01 00:00:00")))

    def test_rebinding_the_minter_is_refused(self):
        """Floor audit F3's property, now surviving an UPDATE. If a credential can re-stamp
        `minted_by` after the fact it can name any other principal as its minter and satisfy the
        separation check with a string it chose itself."""
        self.assertEqual(self._violations(self._row(), self._row(minted_by=AGENT)),
                         ["minted_by"])

    def test_swapping_the_token_is_refused(self):
        self.assertIn("token_hash",
                      self._violations(self._row(), self._row(token_hash=consent_token_hash("other"))))

    def test_repointing_the_marker_at_another_document_is_refused(self):
        """Consent is bound to ONE document and ONE act. Editing that binding after minting is the
        same escalation as forging the marker, with fewer steps."""
        for field, value in (("ref_docname", "ACC-SINV-2026-99999"),
                             ("ref_doctype", "Journal Entry"),
                             ("ref_action", "cancel")):
            with self.subTest(field):
                self.assertEqual(self._violations(self._row(), self._row(**{field: value})), [field])

    def test_every_changed_field_is_named_at_once(self):
        # An operator fixing a refusal should see the whole problem, not one field per attempt.
        got = self._violations(self._row(), self._row(minted_by=AGENT, expires_at_epoch=9e9))
        self.assertEqual(sorted(got), ["expires_at_epoch", "minted_by"])

    def test_a_missing_previous_version_reports_nothing(self):
        """On INSERT there is no prior document, and nothing is being changed. The glue passes
        `get_doc_before_save()`, which frappe returns as None on create."""
        self.assertEqual(self._violations(None, self._row()), [])
