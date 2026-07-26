# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The act-level consent gate at the DOCUMENT layer (``pacioli_guard.act``).

Why this layer exists, and why the same gate at ``auth_hooks`` was not enough. Consent is a property
of an ACT ON A DOCUMENT. Enforced in the HTTP auth hook it could only ever cover requests that
authenticate with an api-key, and it had to reverse-engineer "is this a submit, and of what" out of a
request shape — which is why the classifier grew ``?cmd=`` dominance, five body-carrying RPC
rewrites, a ``savedocs`` action map, three REST mounts and a disclosed residual on raw ``docstatus``
writes. At ``before_submit`` the same question is ``doc.doctype``, ``doc.name``, and the fact that
submit is happening.

Verified against frappe 16 source before this was written (paths are frappe's own tree):
``_submit()`` sets ``docstatus = 1`` then ``save()`` -> ``_save()`` (``model/document.py:552``) ->
``run_before_save_methods()`` (``:587``) -> ``run_method("before_submit")`` (``:1407``), and
``Document.hook`` (``:1606``) composes every app's ``doc_events`` handler around it. The composed
runner uses ``try/finally``, NOT ``try/except``, so a ``frappe.throw`` from a handler aborts the save.
``before_submit`` has exactly two call sites, ``:479`` (insert) and ``:587`` (save), so every path
that reaches ``docstatus = 1`` through the ORM passes here: REST, ``run_doc_method``,
``frappe.client.submit``, the desk ``savedocs`` endpoint, bulk submit, a raw docstatus field write
followed by save, a background job, a server script, and the bench console.

The tests that matter most are the last two groups: a credential with NO api-key header at all, and
an actor with no request context whatsoever. Those are the paths the transport-layer gate could never
see, and they are exactly the paths this gate exists to cover.

Run: ``python3 -m pytest pacioli_guard/tests/test_act.py -q``. No frappe required.
"""
import sys
import types
import unittest

# `enforce`/`act` do a hard ``import frappe`` at module top (they ARE the frappe glue). Satisfy it
# with an empty stub before importing them, exactly as test_enforce.py does; every test then points
# both modules at its own fake.
sys.modules.setdefault("frappe", types.ModuleType("frappe"))

from pacioli_guard import act  # noqa: E402
from pacioli_guard import enforce  # noqa: E402
from pacioli_guard.scope import consent_token_hash  # noqa: E402
from pacioli_guard.tests.test_enforce import (  # noqa: E402
    FakeFrappe,
    FakePermissionError,
    FakeScopeDoc,
)

TOKEN = "floor-consent-token"


class FakeDoc:
    """A document arriving at ``before_submit``.

    Carries `flags`, because the gate stamps custody there. A real frappe `Document.flags` is a
    per-object `frappe._dict` that is never persisted — a plain dict is the faithful shape. The
    double lacking `flags` is what let the 0.9.2 predicate look correct: with no way to express
    "this enclosing document never established consent", no test could fail on it.
    """

    def __init__(self, doctype="Sales Invoice", name="SI-0001", in_insert=True):
        self.doctype = doctype
        self.name = name
        # `in_insert` mirrors frappe's own flag, set at model/document.py:478 immediately before
        # `run_before_save_methods()` fires `before_submit` and cleared at :482. It defaults TRUE
        # here because the cascade documents these tests model are created by the act they ride
        # (`frappe.get_doc(<dict>)` then submit -> `_save` delegates to `insert()`). A document
        # LOADED BY NAME and submitted has it False, and that is the whole discrimination.
        self.flags = {"in_insert": in_insert}


def marker(*, docname="SI-0001", doctype="Sales Invoice", action="submit", burned=0,
           minted_by="operator@x", token=TOKEN, expires_in=900):
    import time
    return {(doctype, docname): {
        "name": "PCM-0001", "token_hash": consent_token_hash(token),
        "ref_doctype": doctype, "ref_docname": docname, "ref_action": action,
        "expires_at": time.time() + expires_in, "burned": burned, "minted_by": minted_by}}


def wire(*, gated=True, headers=None, markers=None, user="broker@x", request=None,
         legacy=None, legacy_columns=()):
    """Point BOTH glue modules at one fake. ``act`` reuses ``enforce``'s record load, claim and
    denial path deliberately — one implementation of each — so both namespaces need the fake."""
    scopes = {}
    if gated is not None:
        scopes[user] = FakeScopeDoc(method_patterns=["Sales Invoice.submit"],
                                    require_consent=1 if gated else 0)
    fake = FakeFrappe(headers=dict(headers or {}), session_user=user, scopes=scopes,
                      request=request, markers=markers, legacy=legacy or {},
                      legacy_columns=legacy_columns)
    enforce.frappe = fake
    act.frappe = fake
    return fake


class TestTheGateCoversWhatTheTransportGateCouldNot(unittest.TestCase):
    """The coverage win, stated as tests. None of these carry an api-key Authorization header, so
    ``check_scope`` returns without enforcing anything on all of them."""

    def test_a_desk_session_submit_is_refused(self):
        # No Authorization header at all — a cookie/desk session. The transport gate is a no-op
        # here by design (`_scope_for_request` returns None), so before this layer existed a gated
        # seat could submit through the desk UI with no marker.
        fake = wire(headers={act.CONSENT_HEADER: None}, markers=marker())
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")
        self.assertIn("consent", fake.thrown[0].lower())

    def test_a_background_job_with_no_request_context_is_refused(self):
        # A scheduler run or a server script: no request, so no header can exist. A gated principal
        # acting with no way to present consent must be refused, not waved through.
        fake = wire(markers=marker())

        def no_request(key, default=None):
            raise RuntimeError("no request in this context")

        fake.get_request_header = no_request
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")
        self.assertIn("consent", fake.thrown[0].lower())

    def test_an_oauth_bearer_submit_is_refused(self):
        # `api_key_from_auth_header` ignores Bearer, so the transport gate never fires for OAuth.
        fake = wire(headers={"Authorization": "Bearer some-oauth-token"}, markers=marker())
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")
        self.assertIn("consent", fake.thrown[0].lower())

    def test_a_valid_marker_passes_on_a_desk_session_too(self):
        # The gate is about consent, not about transport: present a real marker and it proceeds,
        # whatever door the act came through.
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        act.before_submit(FakeDoc(), "before_submit")
        self.assertIsNone(fake.thrown)


class TestTheConsentDecisionItself(unittest.TestCase):
    """Same properties the pure core already guarantees, now proven through this entry point."""

    def test_no_marker_at_all_is_refused(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers={})
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")

    def test_a_valid_marker_is_spent_on_use(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        act.before_submit(FakeDoc(), "before_submit")
        self.assertTrue(all(row["burned"] for row in fake.db.markers.values()))

    def test_a_replay_is_refused(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        act.before_submit(FakeDoc(), "before_submit")
        fake.thrown = None
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")

    def test_a_submit_marker_does_not_spend_on_a_cancel(self):
        # Consent to post is not consent to reverse — cancel reverses GL entries.
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="submit"))
        with self.assertRaises(FakePermissionError):
            act.before_cancel(FakeDoc(), "before_cancel")

    def test_a_cancel_marker_spends_on_a_cancel(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="cancel"))
        act.before_cancel(FakeDoc(), "before_cancel")
        self.assertIsNone(fake.thrown)

    def test_a_self_minted_marker_is_refused(self):
        # The separation property: the acting principal cannot be the minter.
        fake = wire(headers={act.CONSENT_HEADER: TOKEN},
                    markers=marker(minted_by="broker@x"))
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")

    def test_an_expired_marker_is_refused(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(expires_in=-1))
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")

    def test_a_marker_for_another_document_is_refused(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(docname="SI-9999"))
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(name="SI-0001"), "before_submit")

    def test_a_lost_claim_denies_even_though_the_verdict_passed(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        fake.db.sql_hides_rowcount = True
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")


class TestItLeavesUngatedActorsCompletelyAlone(unittest.TestCase):
    """A `"*"` doc_events hook runs on EVERY submit on the site, including a human clicking Submit
    in the desk UI and ERPNext's own internal submits. If it were not inert for anyone who has not
    opted in, installing this app would break the site."""

    def test_an_actor_with_no_grant_is_untouched(self):
        fake = wire(gated=None, markers=marker())
        act.before_submit(FakeDoc(), "before_submit")
        self.assertIsNone(fake.thrown)

    def test_a_grant_with_require_consent_off_is_untouched(self):
        fake = wire(gated=False, markers=marker())
        act.before_submit(FakeDoc(), "before_submit")
        self.assertIsNone(fake.thrown)

    def test_it_does_not_even_look_for_a_marker_when_ungated(self):
        # Cheap for the overwhelmingly common case: one grant lookup and out, no marker read,
        # no claim. A `"*"` hook that did real work per submit would be a tax on every site.
        fake = wire(gated=False, markers=marker())
        act.before_submit(FakeDoc(), "before_submit")
        self.assertEqual(fake.db.sql_calls, [])

    def test_a_missing_session_user_is_untouched(self):
        fake = wire(gated=None, markers=marker())
        fake.session = types.SimpleNamespace(user=None)
        act.before_submit(FakeDoc(), "before_submit")
        self.assertIsNone(fake.thrown)

    def test_an_unreadable_grant_does_not_break_the_site(self):
        # STATED RESIDUAL, deliberate. The GATING question fails open, because a `"*"` hook that
        # failed closed on a transient DB error would refuse every submit on a site that never
        # opted into this app. Once gating IS established, every consent failure denies.
        fake = wire(markers=marker())

        def boom(doctype, name):
            raise RuntimeError("grant read failed")

        fake.get_doc = boom
        act.before_submit(FakeDoc(), "before_submit")
        self.assertIsNone(fake.thrown)


class TestTheHandlerContract(unittest.TestCase):
    """frappe's ``Document.hook`` composer inspects the handler signature and calls it either as
    ``f(doc, method)`` or ``f(doc)`` depending on whether it accepts a method argument
    (``model/document.py`` compose/runner). Both shapes must work."""

    def test_called_with_the_method_argument(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        act.before_submit(FakeDoc(), "before_submit")
        self.assertIsNone(fake.thrown)

    def test_called_without_the_method_argument(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        act.before_submit(FakeDoc())
        self.assertIsNone(fake.thrown)

    def test_a_legacy_json_grant_is_gated_too(self):
        # Parity with the transport gate, which falls back to the deprecated User.api_scope blob.
        # A site mid-migration must not silently lose its consent gate.
        fake = wire(gated=None, headers={act.CONSENT_HEADER: TOKEN}, markers={},
                    legacy={"broker@x": '{"methods": ["x"], "require_consent": true}'},
                    legacy_columns=("api_scope",))
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")


class TestPropertiesInheritedFromTheTransportGate(unittest.TestCase):
    """Coverage parity. These three properties were proven only by the old ``auth_hooks`` consent
    tests; the gate moved, so they are re-proven here before those tests are deleted. A move must
    not quietly drop a property — that is how the 2026-07-25 bypass got in."""

    def test_a_forged_token_is_refused(self):
        # A real, live, correctly-bound marker exists — the token presented just is not its token.
        fake = wire(headers={act.CONSENT_HEADER: "not-the-real-token"}, markers=marker())
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")
        self.assertFalse(any(row["burned"] for row in fake.db.markers.values()))

    def test_a_marker_with_no_recorded_minter_is_refused(self):
        # Separation cannot be established, so it is not established. An unrecorded minter is not
        # the same as a different minter.
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(minted_by=None))
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")

    def test_a_document_that_cannot_be_identified_is_refused(self):
        # Consent that cannot be bound to a document is not consent. At this layer an unnamed
        # document is the only way that happens (frappe sets the name before before_submit runs).
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(name=None), "before_submit")
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(doctype=None), "before_submit")


class TestBackwardCompatibilityAndRegistration(unittest.TestCase):
    """The last two properties the deleted transport-layer tests proved.

    ``test_gate_is_off_by_default`` mattered because a grant written before ``bench migrate`` added
    the ``require_consent`` column has NO such attribute at all, and absence must read as OFF. A new
    gate that switched itself on during an upgrade would start refusing every write a live broker
    makes — the lesson every gate in this app has already learned once.

    ``test_does_not_gate_a_call_that_changes_no_docstatus`` mattered at the transport layer because
    the gate saw every request and had to decide which ones were docstatus moves. At this layer that
    is structural rather than conditional: only ``before_submit`` and ``before_cancel`` are
    registered, so a read, a draft save, or an update-after-submit never reaches the gate at all.
    The equivalent assertion is therefore on the registration itself.
    """

    def test_a_grant_predating_the_column_reads_as_not_gated(self):
        # A bare FakeScopeDoc genuinely LACKS `require_consent` (see its docstring), which models a
        # doc loaded on a not-yet-migrated site.
        legacy_doc = FakeScopeDoc(method_patterns=["Sales Invoice.submit"])
        self.assertFalse(hasattr(legacy_doc, "require_consent"))
        fake = FakeFrappe(headers={}, session_user="broker@x", scopes={"broker@x": legacy_doc},
                          markers=marker())
        enforce.frappe = fake
        act.frappe = fake
        act.before_submit(FakeDoc(), "before_submit")
        self.assertIsNone(fake.thrown)

    def test_only_the_two_docstatus_events_are_registered(self):
        from pacioli_guard import hooks

        self.assertEqual(set(hooks.doc_events["*"]), {"before_submit", "before_cancel"})
        self.assertEqual(hooks.doc_events["*"]["before_submit"],
                         "pacioli_guard.act.before_submit")
        self.assertEqual(hooks.doc_events["*"]["before_cancel"],
                         "pacioli_guard.act.before_cancel")

    def test_it_is_registered_on_the_wildcard_not_a_doctype_list(self):
        # Keyed on the acting principal's grant, never on a list of doctypes — otherwise a governed
        # seat slips through by touching a doctype nobody thought to enumerate.
        from pacioli_guard import hooks

        self.assertEqual(list(hooks.doc_events), ["*"])

    def test_the_credential_floor_is_still_registered_too(self):
        # The move is a relocation of ONE gate, not a replacement of the app. Credential scoping
        # stays at auth_hooks, which is the only altitude where "which credential" exists.
        from pacioli_guard import hooks

        self.assertEqual(hooks.auth_hooks, ["pacioli_guard.enforce.check_scope"])


# A faithful double of frappe's document-write frames. Built by `exec` into a namespace whose
# `__name__` really is `frappe.model.document`, because the production signal reads THREE things off
# a live frame and a plain module-level def can only supply one:
#
#   1. the code name (`_save` / `insert`),
#   2. the MODULE the frame belongs to, and
#   3. WHICH DOCUMENT that frame is writing (its `self`).
#
# The 2026-07-26 redteam found the gate fail-OPEN precisely because the old double supplied only (1).
# `frappe.client.insert` is a second, unrelated function also named `insert`, and `_save` delegates
# to `insert` for any new document — so a bare name count read two top-level paths as "nested" and
# skipped consent. A double that cannot express "same name, different module" and "same document,
# two frames" cannot fail on either. Ask what dimension a fake lacks; that is where the bug will be.
_FRAPPE_DOCUMENT_NS = {"__name__": "frappe.model.document"}
exec(compile(  # noqa: S102 — building a frame double, not evaluating input
    "def _save(self, fn, *args):\n"
    "    return fn(*args)\n"
    "def insert(self, fn, *args):\n"
    "    return fn(*args)\n",
    "<frappe.model.document double>", "exec"), _FRAPPE_DOCUMENT_NS)
_save = _FRAPPE_DOCUMENT_NS["_save"]
insert = _FRAPPE_DOCUMENT_NS["insert"]

# `frappe.client.insert` — the whitelisted REST entry point (frappe 16 `client.py:208`). Same code
# NAME as `Document.insert`, different module, and it holds no document in `self`.
_FRAPPE_CLIENT_NS = {"__name__": "frappe.client"}
exec(compile(  # noqa: S102
    "def insert(fn, *args):\n"
    "    return fn(*args)\n",
    "<frappe.client double>", "exec"), _FRAPPE_CLIENT_NS)
client_insert = _FRAPPE_CLIENT_NS["insert"]


class TestTheFrameworkCascade(unittest.TestCase):
    """Found by the LIVE run on 2026-07-26, and by nothing in this file — which is the point.

    The governed REST write failed with `require_consent` ON even though a valid marker existed. The
    Sales Invoice's own check PASSED; then ERPNext's own accounting machinery, inside that same
    submit, created and submitted `Payment Ledger Entry` documents, and this `"*"` handler demanded
    consent for those too:

        Refused for Payment Ledger Entry ruq5vig9c2

    A human cannot mint a marker for that document — its name does not exist until the submit is
    already running. Consent binds to the ACT a human authorised, and the framework's consequences
    of that act are part of that act.

    THE SIGNAL IS NESTING DEPTH, and the alternative was rejected on purpose. A per-request stack
    with an explicit close would need a pop point, and the only candidate is `on_submit` — which is
    exactly where ERPNext creates its ledger entries. Hook ordering inside one event would decide
    whether the pop ran before or after that cascade, and a pop that ran too early would let a BULK
    submit's invoices 2..N ride invoice 1's marker. That is fail-OPEN. Depth needs no pairing, keeps
    no state that can leak between requests, does not care about hook order, and if frappe ever
    renames those internals it reads as "not nested" and REFUSES. Fail-closed is the direction a
    security gate is allowed to be wrong in.

    `FakeDoc` was a single document, so nothing here could see any of this before.
    """

    def setUp(self):
        self.fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())

    def test_a_top_level_submit_still_needs_its_marker(self):
        # Depth must not become a way to skip consent: one write frame is the actor's own act.
        d = FakeDoc()
        _save(d, act.before_submit, d, "before_submit")
        self.assertIsNone(self.fake.thrown)

    def test_a_top_level_submit_with_no_marker_is_still_refused(self):
        wire(headers={}, markers=marker())
        d = FakeDoc()
        with self.assertRaises(FakePermissionError):
            _save(d, act.before_submit, d, "before_submit")

    def test_the_cascade_rides_the_consented_act(self):
        # The live failure, as a test: the parent's write frame is still open when the framework
        # submits its own ledger document, and that document has no marker and never could.
        si = FakeDoc()

        def cascade():
            ple = FakeDoc(doctype="Payment Ledger Entry", name="ruq5vig9c2")
            insert(ple, act.before_submit, ple, "before_submit")

        _save(si, lambda: (act.before_submit(si, "before_submit"), cascade()))
        self.assertIsNone(self.fake.thrown)

    def test_the_cascade_needs_no_marker_of_its_own(self):
        self.assertIsNone(self.fake.db.markers.get(("Payment Ledger Entry", "ple-1")))
        si = FakeDoc()

        def cascade():
            ple = FakeDoc(doctype="Payment Ledger Entry", name="ple-1")
            insert(ple, act.before_submit, ple, "before_submit")

        _save(si, lambda: (act.before_submit(si, "before_submit"), cascade()))
        self.assertIsNone(self.fake.thrown)

    def test_bulk_each_top_level_submit_needs_ITS_OWN_marker(self):
        # THE TRAP the rejected design would have fallen into. Two invoices in one request are two
        # top-level acts at the same depth, not a cascade — the second must be refused.
        a = FakeDoc(name="SI-0001")
        _save(a, act.before_submit, a, "before_submit")
        self.assertIsNone(self.fake.thrown)
        b = FakeDoc(name="SI-0002")
        with self.assertRaises(FakePermissionError):
            _save(b, act.before_submit, b, "before_submit")

    def test_the_marker_is_spent_once_for_the_whole_cascade(self):
        si = FakeDoc()

        def cascade():
            ple = FakeDoc(doctype="Payment Ledger Entry", name="ple-1")
            insert(ple, act.before_submit, ple, "before_submit")

        _save(si, lambda: (act.before_submit(si, "before_submit"), cascade()))
        self.assertEqual(len(self.fake.db.sql_calls), 1)  # one claim, not one per cascaded document

    def test_cancel_cascades_the_same_way(self):
        # MODELLED AS FRAPPE PRODUCES IT (corrected 2026-07-26, third redteam pass). This test used
        # to drive the cascaded document through the `insert` frame double with `in_insert`
        # defaulted True — a combination frappe cannot produce, which is why it could not fail on
        # the 0.9.4 regression. `Document._cancel` (`model/document.py:1324-1326`) sets
        # `docstatus = 2` and calls `save()` -> `_save()`; `_save` never sets `in_insert`, and a
        # document being cancelled has a name and is not `__islocal` so `_save` never delegates to
        # `insert()`. A cancelled document therefore reaches this gate through `_save`, always with
        # `in_insert` False. See `TestACancelCascadeIsStructurallyNotAnInsert`.
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="cancel"))
        si = FakeDoc(in_insert=False)

        def cascade():
            ple = FakeDoc(doctype="Payment Ledger Entry", name="ple-9", in_insert=False)
            _save(ple, act.before_cancel, ple, "before_cancel")

        _save(si, lambda: (act.before_cancel(si, "before_cancel"), cascade()))
        self.assertIsNone(fake.thrown)

    def test_an_ungated_actor_is_untouched_at_any_nesting(self):
        fake = wire(gated=None, headers={}, markers=marker())
        d = FakeDoc()
        _save(d, lambda: insert(d, act.before_submit, d, "before_submit"))
        self.assertIsNone(fake.thrown)


class TestTopLevelWritesThatTraverseTwoWriteFrames(unittest.TestCase):
    """The 2026-07-26 redteam finding: counting write frames BY NAME failed OPEN on two live,
    top-level, remotely reachable paths. Both put two write-named frames on the stack for what is
    still ONE actor writing ONE document, so the gate read them as a framework cascade and returned
    without checking consent at all.

    Verified on real frappe 16 bytes before writing these:
      A. `frappe.client.insert` (`client.py:208`, @whitelist POST/PUT) -> `insert_doc` (`:511`)
         -> `frappe.get_doc(doc).insert()` (`:527`) -> `Document.insert` (`model/document.py:431`).
         TWO frames named `insert`. A docstatus-1 body inserts a document already submitted.
      B. `Document._save` (`model/document.py:571-572`) delegates to `self.insert()` for any new
         document, so saving/submitting a NEW doc is `_save` -> `insert` for a single document.

    Vector A is reachable with exactly the grant the README tells an operator to give the broker,
    which makes it the 2025-07-25 bypass reopened one altitude down.

    The corrected signal is not a count. It asks whether an enclosing frappe document-write frame is
    writing a DIFFERENT document, which is what "this act is a consequence of another act" actually
    means. Same document across two frames is one act; a foreign module's same-named function is not
    a document write at all.
    """

    def setUp(self):
        self.fake = wire(headers={}, markers=marker())  # no marker presented: consent MUST refuse

    def test_frappe_client_insert_does_not_disarm_the_gate(self):
        # VECTOR A. Two frames named `insert`, but the outer one is `frappe.client` and holds no
        # document. A name count reads depth 2 and skips consent; this must still refuse.
        d = FakeDoc()
        with self.assertRaises(FakePermissionError):
            client_insert(lambda: insert(d, act.before_submit, d, "before_submit"))

    def test_submitting_a_NEW_document_does_not_disarm_the_gate(self):
        # VECTOR B. `_save` delegating to `insert` is TWO write frames for ONE document.
        d = FakeDoc()
        with self.assertRaises(FakePermissionError):
            _save(d, lambda: insert(d, act.before_submit, d, "before_submit"))

    def test_a_genuine_cascade_under_those_same_two_frames_still_rides(self):
        # The fix must not re-break the thing 0.9.1 was for: a DIFFERENT document nested inside a
        # consented parent still rides, even when the parent's own path used both frames.
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        si = FakeDoc()

        def cascade():
            ple = FakeDoc(doctype="Payment Ledger Entry", name="ple-9")
            insert(ple, act.before_submit, ple, "before_submit")

        _save(si, lambda: insert(si, lambda: (act.before_submit(si, "before_submit"), cascade())))
        self.assertIsNone(fake.thrown)

    def test_a_foreign_frame_named_insert_cannot_disarm_the_gate(self):
        # The walk must not treat any function named `insert` as a document write. Only frappe's
        # own document module writes documents.
        d = FakeDoc()
        with self.assertRaises(FakePermissionError):
            client_insert(lambda: client_insert(
                lambda: insert(d, act.before_submit, d, "before_submit")))

    def test_nesting_is_read_from_real_frames_not_from_state(self):
        # Nothing persists between calls: the same call at the same nesting behaves the same way
        # twice. A design that kept per-request state could leak a consented frame into the next act.
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(name="SI-0009"), "before_submit")
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(name="SI-0009"), "before_submit")


class TestAnUngovernedOuterWriteProvesNothing(unittest.TestCase):
    """Redteam 2026-07-26, second pass — it broke the 0.9.2 fix.

    0.9.2 asked "is any enclosing frame writing a DIFFERENT document" and treated yes as "this is a
    consequence of a governed act". Those are not the same question, because **only `before_submit`
    and `before_cancel` are gated**. A draft `save()`, and an insert of a non-submittable DocType,
    are never gated and never need a marker — yet each puts a `frappe.model.document` write frame
    holding a different document on the stack. Anything submitted underneath one rode for free,
    with NO marker existing anywhere on the site.

    Three real, ledger-moving instances, all verified against ERPNext 16 source:

      * `Subscription` is not submittable. `Document.insert` runs `after_insert` inside its own
        frame (frappe `model/document.py:498`), and `subscription.py:529` submits a Sales Invoice
        per elapsed billing period. A back-dated `start_date` means N invoices, N sets of GL
        entries, zero consent.
      * `Item` is not submittable. `item.py:192` submits a Stock Entry from `after_insert`, at a
        caller-supplied `opening_stock` and `valuation_rate`.
      * A `Stock Reconciliation` DRAFT SAVE submits a Serial and Batch Bundle
        (`stock_reconciliation.py:227`) from `validate` — an ungated action.

    The fix: the enclosing act must have ESTABLISHED consent, not merely be a different document.
    """

    def setUp(self):
        self.fake = wire(headers={}, markers=marker())  # no marker presented: consent MUST refuse

    def test_subscription_style_ungated_insert_does_not_license_a_submit(self):
        sub = FakeDoc(doctype="Subscription", name="SUB-0001")   # never gated: not submittable
        si = FakeDoc(name="SI-NEW-1")
        with self.assertRaises(FakePermissionError):
            insert(sub, lambda: act.before_submit(si, "before_submit"))

    def test_item_style_ungated_insert_does_not_license_a_stock_entry(self):
        item = FakeDoc(doctype="Item", name="HBW-FRAME-56")
        se = FakeDoc(doctype="Stock Entry", name="SE-NEW-1")
        with self.assertRaises(FakePermissionError):
            insert(item, lambda: act.before_submit(se, "before_submit"))

    def test_an_ungated_DRAFT_SAVE_does_not_license_a_submit(self):
        draft = FakeDoc(doctype="Stock Reconciliation", name="SR-0001")
        bundle = FakeDoc(doctype="Serial and Batch Bundle", name="SABB-1")
        with self.assertRaises(FakePermissionError):
            _save(draft, lambda: act.before_submit(bundle, "before_submit"))

    def test_a_GOVERNED_parent_still_licenses_its_cascade(self):
        # The whole reason the skip exists. Must survive the fix.
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        si = FakeDoc()

        def cascade():
            ple = FakeDoc(doctype="Payment Ledger Entry", name="ple-1")
            insert(ple, act.before_submit, ple, "before_submit")

        _save(si, lambda: (act.before_submit(si, "before_submit"), cascade()))
        self.assertIsNone(fake.thrown)

    def test_a_cascade_OF_A_CASCADE_keeps_riding_the_original_act(self):
        # Custody propagates: a GL Entry inside a Payment Ledger Entry inside the consented invoice
        # must not become ungoverned just for being two levels deep.
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        si = FakeDoc()

        def deep():
            ple = FakeDoc(doctype="Payment Ledger Entry", name="ple-1")

            def deeper():
                gl = FakeDoc(doctype="GL Entry", name="gl-1")
                insert(gl, act.before_submit, gl, "before_submit")

            insert(ple, lambda: (act.before_submit(ple, "before_submit"), deeper()))

        _save(si, lambda: (act.before_submit(si, "before_submit"), deep()))
        self.assertIsNone(fake.thrown)

    def test_a_REFUSED_parent_licenses_nothing(self):
        # An act whose marker was never spent must not stamp custody. Nested submits stay gated.
        parent = FakeDoc(name="SI-REFUSED")
        with self.assertRaises(FakePermissionError):
            act.before_submit(parent, "before_submit")
        self.assertFalse(parent.flags.get("pacioli_consent_established"))


class TestDocumentIdentityNotObjectIdentity(unittest.TestCase):
    """Redteam 2026-07-26 — `other is not doc` hung the whole gate on OBJECT identity.

    `frappe.get_doc` returns a fresh instance on every call, and `load_doc_before_save` already
    builds a second object for the same document inside `_save`. A controller doing
    `frappe.get_doc(self.doctype, self.name).submit()` would present a different object for the
    same document, which read as "a different document is being written" and skipped consent.
    Comparing (doctype, name) is strictly more fail-closed and costs nothing.
    """

    def setUp(self):
        self.fake = wire(headers={}, markers=marker())

    def test_a_second_object_for_the_SAME_document_is_not_a_cascade(self):
        outer = FakeDoc(name="SI-0001")
        inner = FakeDoc(name="SI-0001")           # same document, different Python object
        outer.flags["pacioli_consent_established"] = True
        with self.assertRaises(FakePermissionError):
            _save(outer, lambda: act.before_submit(inner, "before_submit"))

    def test_two_unsaved_documents_are_not_treated_as_the_same_document(self):
        # Both names are None. Falling back to object identity is the fail-closed reading: they are
        # different documents, so the inner one needs its own consent unless the outer established it.
        outer = FakeDoc(name=None)
        inner = FakeDoc(name=None)
        with self.assertRaises(FakePermissionError):
            _save(outer, lambda: act.before_submit(inner, "before_submit"))


class TestOneMarkerLicensesOnlyWhAtTheActCREATED(unittest.TestCase):
    """Redteam 2026-07-26 (second pass) — CONSENT AMPLIFICATION.

    Riding on "an enclosing governed act is in progress" licensed more than the human approved. The
    failure that motivated the exception was a `Payment Ledger Entry` that DID NOT EXIST until the
    submit ran — which is precisely why no human could mint a marker for it. But the same predicate
    also licensed PRE-EXISTING drafts the caller NAMED in the request body, which a human could have
    been asked to approve.

    Reachable in the ordinary slice-one flow. Submitting a Sales Invoice with `update_stock` carries
    the item row's `serial_and_batch_bundle` LINK straight out of the request body
    (`stock_controller.py:1069`), and `serial_batch_bundle.py:441-449` then does
    `frappe.get_doc("Serial and Batch Bundle", <that name>)` and submits it. One marker for one
    invoice, and stock-ledger-moving documents the human never saw reach docstatus 1.

    `flags.in_insert` separates them using frappe's own signal: set at `model/document.py:478`
    immediately before `before_submit` fires, cleared at :482. Verified on real bytes that the
    motivating cascade HAS it (`create_payment_ledger_entry` builds the PLE with
    `frappe.get_doc(<dict>)`, so `_save` delegates to `insert()`), and a doc loaded by name does not.
    """

    def setUp(self):
        self.fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())

    def test_a_document_the_act_CREATED_still_rides(self):
        si = FakeDoc()

        def cascade():
            ple = FakeDoc(doctype="Payment Ledger Entry", name="ple-1", in_insert=True)
            insert(ple, act.before_submit, ple, "before_submit")

        _save(si, lambda: (act.before_submit(si, "before_submit"), cascade()))
        self.assertIsNone(self.fake.thrown)

    def test_a_PRE_EXISTING_draft_named_by_the_caller_does_NOT_ride(self):
        si = FakeDoc()

        def amplify():
            # Loaded by name and submitted — the Serial and Batch Bundle shape.
            bundle = FakeDoc(doctype="Serial and Batch Bundle", name="SABB-EXISTING",
                             in_insert=False)
            _save(bundle, act.before_submit, bundle, "before_submit")

        with self.assertRaises(FakePermissionError):
            _save(si, lambda: (act.before_submit(si, "before_submit"), amplify()))

    def test_the_consented_act_itself_is_unaffected(self):
        # A top-level submit of an EXISTING document is the normal case and must still work with
        # its own marker — `in_insert` is only consulted on the RIDE path.
        d = FakeDoc(in_insert=False)
        _save(d, act.before_submit, d, "before_submit")
        self.assertIsNone(self.fake.thrown)

    def test_a_cascade_of_a_cascade_still_rides_when_each_was_created(self):
        si = FakeDoc()

        def deep():
            ple = FakeDoc(doctype="Payment Ledger Entry", name="ple-1", in_insert=True)

            def deeper():
                gl = FakeDoc(doctype="GL Entry", name="gl-1", in_insert=True)
                insert(gl, act.before_submit, gl, "before_submit")

            insert(ple, lambda: (act.before_submit(ple, "before_submit"), deeper()))

        _save(si, lambda: (act.before_submit(si, "before_submit"), deep()))
        self.assertIsNone(self.fake.thrown)


class TestTheDenialMessageDoesNotLie(unittest.TestCase):
    """John, 2026-07-26: **"our code must not lie."**

    The denial text told operators: *"This gate runs on every path that reaches the document
    lifecycle, not only on api-key REST calls."* The second half is true and is the whole reason
    this moved off ``auth_hooks``. **The first half is false**, and this module's own docstring says
    so 180 lines above the sentence that denies it: ``run_before_save_methods`` returns early on
    ``flags.ignore_validate`` (frappe 16 ``model/document.py:1399-1400``) — verified in source — and
    that return happens BEFORE the ``_action == "submit"`` branch at ``:1405`` that fires
    ``before_submit``. ERPNext sets the flag and changes docstatus anyway in at least two places,
    one of which cancels a consolidated Sales Invoice and reverses GL entries.

    So a write can reach the document lifecycle and never reach this gate. An operator reading
    "every path" would size their threat model on a guarantee this software does not provide — in
    a refusal message, which is the one place they are certain to read it.
    """

    def setUp(self):
        self.fake = wire(headers={act.CONSENT_HEADER: None}, markers=None)

    def _refusal(self):
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")
        return self.fake.thrown[0]

    def test_it_does_not_claim_EVERY_path(self):
        self.assertNotIn("every path", self._refusal())

    def test_it_still_states_the_coverage_that_IS_real(self):
        # The true half must survive the correction — it is the reason this gate exists at this
        # altitude, and deleting it would trade one false impression for another.
        text = self._refusal().lower()
        self.assertIn("api-key", text)

    def test_it_names_the_residual_that_walks_around_it(self):
        self.assertIn("ignore_validate", self._refusal())


class TestACancelCascadeIsStructurallyNotAnInsert(unittest.TestCase):
    """Redteam 2026-07-26, THIRD pass — 0.9.4 made the ride unreachable on the CANCEL path.

    ``flags.in_insert`` is written in exactly one place in frappe 16: ``Document.insert``, set at
    ``model/document.py:478`` and cleared at ``:482`` (and again at ``:499``/``:507``).
    ``Document._cancel`` (``:1324-1326``) is ``docstatus = 2`` followed by ``save()`` -> ``_save()``,
    which never sets the flag; and a document being cancelled has a name and is not ``__islocal``, so
    ``_save`` never delegates to ``insert()`` (``:571-572``). **So ``in_insert`` is False at every
    ``before_cancel`` frappe is capable of producing**, and 0.9.4's ride condition could not be
    satisfied there by any input.

    The effect: a gated principal cancelling a Sales Invoice was refused the instant ERPNext cancelled
    the ledger entries underneath it, and the whole cancel aborted. Fail-CLOSED, so never an escape —
    but it removed UNDO, which is a pillar this product claims and the broker's documented undo story.

    **Why nothing caught it.** ``test_cancel_cascades_the_same_way`` drove the cascaded document
    through the ``insert`` frame double with ``in_insert`` defaulted True — a combination frappe
    cannot produce. The double could express the submit cascade faithfully and the cancel cascade
    only unfaithfully, so it was green on a path that was broken. Sixth time in three days that the
    missing dimension of a test double was where the defect lived.
    """

    def setUp(self):
        self.fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="cancel"))

    def test_in_insert_is_never_required_on_the_cancel_path(self):
        # The predicate itself, pinned: no cancelled document can carry the flag, so demanding it
        # would deny unconditionally.
        self.assertTrue(act._may_ride(FakeDoc(in_insert=False), act.CANCEL))

    def test_a_cascaded_cancel_rides_the_consented_cancel(self):
        si = FakeDoc(in_insert=False)

        def cascade():
            ple = FakeDoc(doctype="Payment Ledger Entry", name="ple-9", in_insert=False)
            _save(ple, act.before_cancel, ple, "before_cancel")

        _save(si, lambda: (act.before_cancel(si, "before_cancel"), cascade()))
        self.assertIsNone(self.fake.thrown)

    def test_a_cancel_under_an_UNGOVERNED_outer_write_is_still_refused(self):
        # The 0.9.3 half stays load-bearing on this path: restoring the cancel ride must not restore
        # riding on an outer write that never established consent for itself.
        outer = FakeDoc(doctype="Subscription", name="SUB-1", in_insert=False)

        def cascade():
            si = FakeDoc(name="SI-OTHER", in_insert=False)
            _save(si, act.before_cancel, si, "before_cancel")

        with self.assertRaises(FakePermissionError):
            _save(outer, cascade)

    def test_a_top_level_cancel_still_needs_its_own_marker(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="submit"))
        d = FakeDoc(in_insert=False)
        # The marker on this document authorises SUBMIT, not CANCEL. Riding is not in play at top
        # level, and the act binding must still refuse.
        with self.assertRaises(FakePermissionError):
            _save(d, act.before_cancel, d, "before_cancel")
        self.assertIn("consent", fake.thrown[0].lower())

    def test_the_submit_path_still_requires_in_insert(self):
        # The 0.9.4 amplification fix is untouched: a pre-existing draft NAMED by the caller and
        # submitted inside a governed act still does not ride.
        si = FakeDoc()

        def amplify():
            bundle = FakeDoc(doctype="Serial and Batch Bundle", name="SABB-EXISTING",
                             in_insert=False)
            _save(bundle, act.before_submit, bundle, "before_submit")

        with self.assertRaises(FakePermissionError):
            _save(si, lambda: (act.before_submit(si, "before_submit"), amplify()))
