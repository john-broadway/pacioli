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
           minted_by="operator@x", token=TOKEN, expires_in=900, name=None):
    # `name` is the marker's OWN identity, and it must differ per marker. It is what
    # `_claim_consent` burns, so two fixture markers sharing a name make spending one read as
    # spending both — which looked exactly like a single-use violation when the multi-marker tests
    # were first written, and was the double's fault, not the code's. Defaults per document.
    import time
    return {(doctype, docname): {
        "name": name or f"PCM-{doctype.replace(' ', '')}-{docname}",
        "token_hash": consent_token_hash(token),
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

    def test_the_registered_wildcard_handlers_are_exactly_these(self):
        # This test caught the 0.13.0 addition, which is what it is for. Read before widening.
        #
        # Two docstatus transitions gate a POSTING. Two preview events gate a REHEARSAL of one:
        # ERPNext previews by performing the posting and rolling back, so with consent enforced the
        # preview's own cascade was refused and PLAN could not complete. They REFUSE, so they are
        # gates three and four, not recorders — but they refuse only a preview and they do not spend
        # the marker. `after_insert` remains the one non-gate: it reads no grant and refuses nothing.
        from pacioli_guard import hooks

        self.assertEqual(
            set(hooks.doc_events["*"]),
            {"before_submit", "before_cancel", "after_insert",
             "before_gl_preview", "before_sl_preview"})
        for event in ("before_submit", "before_cancel", "after_insert",
                      "before_gl_preview", "before_sl_preview"):
            self.assertEqual(hooks.doc_events["*"][event], f"pacioli_guard.act.{event}")

    def test_the_docstatus_gates_are_still_exactly_two(self):
        """The preview handlers must never become docstatus gates by accident."""
        from pacioli_guard import hooks

        docstatus_events = {e for e in hooks.doc_events["*"] if e.startswith("before_")
                            and "preview" not in e}
        self.assertEqual(docstatus_events, {"before_submit", "before_cancel"})

    def test_the_recording_hook_refuses_nothing_and_reads_no_grant(self):
        # Registered on `"*"`, so it runs on every insert on every site including one that never
        # opted in. It must never throw and must never touch the grant, or installing this app
        # becomes indistinguishable from breaking the site.
        fake = wire(headers={}, markers=None)
        fake.scopes = {}
        act.after_insert(FakeDoc(in_insert=False), "after_insert")
        self.assertIsNone(fake.thrown)

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

    ``Document._cancel`` (``model/document.py:1324-1326``) is ``docstatus = 2`` followed by
    ``save()`` -> ``_save()``, which never sets ``flags.in_insert``; and a document being cancelled
    has a name and is not ``__islocal``, so ``_save`` never delegates to ``insert()``
    (``:571-572``). **So ``in_insert`` is False at every ``before_cancel`` frappe is capable of
    producing**, and 0.9.4's ride condition could not be satisfied there by any input.

    CORRECTED 2026-07-28. This paragraph used to open "``flags.in_insert`` is written in exactly one
    place in frappe 16: ``Document.insert``". That is FALSE — ``frappe/core/doctype/user/user.py:204``
    (``User.before_insert``) writes it too. The conclusion above survives, for reasons that never
    depended on the count: ``User`` is not submittable, ``before_insert`` runs at ``:473`` before
    frappe's own set at ``:478``, and ``insert`` clears the flag at ``:507`` regardless. The claim
    was written without the sweep behind it, which is the same failure as the
    ``stock_reconciliation.py:227`` citation corrected in ``act.py``. In a module whose authority is
    that its citations are checkable, sweep before writing "exactly".

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
        # would deny unconditionally. Since 0.10.0 the cancel branch discriminates on the ENCLOSING
        # act instead — an undo may cascade into undos — so the enclosing act is passed here. What
        # this test guards is unchanged: `in_insert` False must not be what refuses a cascaded
        # cancel.
        self.assertTrue(act._may_ride(FakeDoc(in_insert=False), act.CANCEL, act.CANCEL))

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


class TestActCrossingAndTwoStepCreation(unittest.TestCase):
    """The 2026-07-28 second lens, which walked frappe 16.28.0 and erpnext v16 on disk rather than
    this app's own docstrings. It found the ride wrong in BOTH directions, and both were written
    down here as things that were not known.

    1. **A SUBMIT marker licensed a CANCEL of a caller-named pre-existing document.** `_may_ride`
       returned True for every cancel, and its docstring stated the residual and then said "No such
       lever is known". The lever is shipped in ERPNext: `Sales Invoice.on_submit` (`:507`) ->
       `process_asset_depreciation` -> `depreciate_asset_on_sale` (`:1508-1516`) iterates the item
       rows and does `frappe.get_doc("Asset", d.asset)` on a BARE Link the caller supplies in the
       request body (no `read_only`, no `fetch_from`) -> `depreciation.py:481` ->
       `asset_depreciation_schedule.py:215-217` `current_schedule.cancel()` on a docstatus-1
       document, through the lifecycle. That crosses the exact act binding `consent_verdict`
       enforces and `before_cancel` claims is enforced, because the ride path never calls it.

    2. **`in_insert` is "created by THIS WRITE FRAME", not "created by the act".** frappe clears it
       at `model/document.py:482`/`:507` before a later `submit()` re-enters `_save`, so ERPNext's
       ordinary `save(); submit()` idiom reaches `before_submit` with the flag False on a document
       the act itself created. Shipped instances: `serial_batch_bundle.py:1166`/`:1172` (the
       auto-created Serial and Batch Bundle, `ignore_validate` explicitly cleared before the submit)
       and `depreciation.py:245-249` (the depreciation Journal Entry). Those were REFUSED, aborting
       the whole governed act — the 0.9.4 shape again, fail-closed but product-breaking.

    Neither was expressible before. `FakeDoc` took `in_insert` as a free constructor argument, so
    the suite's only model of "the act created this" WAS the flag, and "created but flag False"
    could not be named. Seventh time the missing dimension of a double is where the defect lived.
    """

    def setUp(self):
        self.fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())

    def test_a_SUBMIT_marker_does_not_license_a_nested_CANCEL_of_a_pre_existing_document(self):
        # The lever, as a test. One marker for `submit Sales Invoice SI-0001`; ERPNext cancels a
        # submitted Asset Depreciation Schedule the caller named through the item row's `asset`
        # link. A human COULD have been asked to approve that cancel — the document exists and has
        # a name — which is exactly the test the submit branch already applies.
        si = FakeDoc(in_insert=False)

        def cascade():
            ads = FakeDoc(doctype="Asset Depreciation Schedule", name="ADS-PRE-EXISTING",
                          in_insert=False)
            _save(ads, act.before_cancel, ads, "before_cancel")

        with self.assertRaises(FakePermissionError):
            _save(si, lambda: (act.before_submit(si, "before_submit"), cascade()))
        # Refused for the SCHEDULE, not the invoice, and for CANCEL, not submit. Asserting only
        # "a refusal happened" would pass just as well if the invoice's own marker had failed,
        # which is a different bug entirely.
        self.assertIn("ADS-PRE-EXISTING", self.fake.thrown[0])
        self.assertIn("consent to cancel", self.fake.thrown[0])

    def test_a_cascaded_cancel_under_a_governed_CANCEL_still_rides(self):
        # The 0.9.4 fix must survive the fix above. An ordinary invoice cancel reverses itself
        # through real lifecycle cancels of second documents — `accounts_controller.py:2001-2005`
        # cancels system-generated credit/debit notes, the exchange gain/loss journal and the
        # common-party journal — and a human cannot mint markers for those.
        self.fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="cancel"))
        si = FakeDoc(in_insert=False)

        def cascade():
            je = FakeDoc(doctype="Journal Entry", name="ACC-JV-EXISTING", in_insert=False)
            _save(je, act.before_cancel, je, "before_cancel")

        _save(si, lambda: (act.before_cancel(si, "before_cancel"), cascade()))
        self.assertIsNone(self.fake.thrown)

    def test_a_document_the_act_CREATED_IN_TWO_STEPS_still_rides(self):
        # `doc.save()` then `doc.submit()`. The document did not exist when the human minted the
        # marker, so it must ride — but `in_insert` is False by the time the submit reaches the
        # gate, which is why the flag alone was the wrong signal.
        si = FakeDoc(in_insert=False)

        def cascade():
            sbb = FakeDoc(doctype="Serial and Batch Bundle", name="SBB-NEW", in_insert=False)
            # `doc.save()` -> Document.insert -> run_method("after_insert") at document.py:498,
            # inside insert's own frame, name set, `in_insert` False (cleared at :482, set again
            # only after this returns).
            insert(sbb, act.after_insert, sbb, "after_insert")
            # `doc.submit()` -> _submit -> save() -> _save. A named, non-local document, so :571-572
            # does NOT delegate back to insert() and the flag stays False.
            _save(sbb, act.before_submit, sbb, "before_submit")

        _save(si, lambda: (act.before_submit(si, "before_submit"), cascade()))
        self.assertIsNone(self.fake.thrown)

    def test_a_two_step_creation_OUTSIDE_a_governed_act_licenses_nothing(self):
        # The custody record is only made when an enclosing act has already established consent.
        # An insert with no governed act on the stack must leave no stamp behind, or the record
        # itself becomes the bypass.
        sbb = FakeDoc(doctype="Serial and Batch Bundle", name="SBB-LOOSE", in_insert=False)
        insert(sbb, act.after_insert, sbb, "after_insert")
        with self.assertRaises(FakePermissionError):
            _save(sbb, act.before_submit, sbb, "before_submit")

    def test_a_CANCEL_marker_licenses_a_submit_of_a_document_the_act_CREATED(self):
        # Pinned as INTENDED, not tolerated. A cancel creates reversing documents
        # (`depreciation.py:544-559` builds a reversal Journal Entry and submits it), and no human
        # could mint a marker for a name that does not exist yet. The act-crossing that matters is
        # the other direction, where the target is pre-existing and nameable.
        self.fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="cancel"))
        si = FakeDoc(in_insert=False)

        def cascade():
            je = FakeDoc(doctype="Journal Entry", name="REV-JE-1", in_insert=True)
            insert(je, act.before_submit, je, "before_submit")

        _save(si, lambda: (act.before_cancel(si, "before_cancel"), cascade()))
        self.assertIsNone(self.fake.thrown)

    def test_may_ride_is_pinned_on_every_producible_state(self):
        # `_may_ride` had exactly ONE direct assertion before this. The (CANCEL, in_insert=True)
        # cell is deliberately absent: frappe cannot produce it, and asserting it would pin the
        # double rather than the code.
        self.assertFalse(act._may_ride(FakeDoc(in_insert=False), act.SUBMIT, act.SUBMIT))
        self.assertTrue(act._may_ride(FakeDoc(in_insert=True), act.SUBMIT, act.SUBMIT))
        self.assertTrue(act._may_ride(FakeDoc(in_insert=True), act.SUBMIT, act.CANCEL))
        self.assertTrue(act._may_ride(FakeDoc(in_insert=False), act.CANCEL, act.CANCEL))
        self.assertFalse(act._may_ride(FakeDoc(in_insert=False), act.CANCEL, act.SUBMIT))


class TestOneRequestCanCarryTheMarkersItNeeds(unittest.TestCase):
    """Second lens on the 0.10.0 fix itself, 2026-07-28, and it caught the fix half-done.

    Closing the act-crossing bypass means a caller-steered cascaded cancel now falls through to the
    marker check. The 0.10.0 changelog and `_may_ride`'s docstring both said the cost was that the
    act "needs a second marker". **That remedy did not exist.** `_presented_consent` read ONE header
    value and `consent_verdict` compares it against the record for the document in hand, so a
    request could satisfy at most one marker — and `pacioli mint` generates a fresh random token per
    marker. A second marker minted for the schedule cancel carried a different token, could never
    match, and the whole submit aborted with no ordering that worked.

    So the fix did not make an asset-sale invoice COSTLIER under a governed seat, it made it
    IMPOSSIBLE, while the refusal message pointed the operator at a command that could not produce a
    usable marker. Consent is per act by design; the transport just could not carry more than one.
    It can now.
    """

    def setUp(self):
        self.si_token = "token-for-the-invoice"
        self.ads_token = "token-for-the-schedule"
        markers = marker(token=self.si_token)
        markers.update(marker(doctype="Asset Depreciation Schedule", docname="ADS-PRE-EXISTING",
                              action="cancel", token=self.ads_token))
        self.markers = markers

    def test_two_markers_in_one_request_satisfy_both_acts(self):
        # The documented remedy, as a test. The governed submit spends its own marker; the cascaded
        # cancel the caller steered falls through to the marker check and spends the SECOND one.
        fake = wire(headers={act.CONSENT_HEADER: f"{self.si_token} {self.ads_token}"},
                    markers=self.markers)
        si = FakeDoc(in_insert=False)

        def cascade():
            ads = FakeDoc(doctype="Asset Depreciation Schedule", name="ADS-PRE-EXISTING",
                          in_insert=False)
            _save(ads, act.before_cancel, ads, "before_cancel")

        _save(si, lambda: (act.before_submit(si, "before_submit"), cascade()))
        self.assertIsNone(fake.thrown)

    def test_a_second_marker_is_still_bound_to_its_own_document_and_act(self):
        # Carrying two tokens must not become "any token satisfies anything". The schedule's marker
        # authorises CANCEL of ADS-PRE-EXISTING; it must not spend on a different document.
        fake = wire(headers={act.CONSENT_HEADER: f"{self.si_token} {self.ads_token}"},
                    markers=self.markers)
        si = FakeDoc(in_insert=False)

        def cascade():
            other = FakeDoc(doctype="Asset Depreciation Schedule", name="ADS-SOMEONE-ELSES",
                            in_insert=False)
            _save(other, act.before_cancel, other, "before_cancel")

        with self.assertRaises(FakePermissionError):
            _save(si, lambda: (act.before_submit(si, "before_submit"), cascade()))
        self.assertIn("ADS-SOMEONE-ELSES", fake.thrown[0])

    def test_one_token_still_behaves_exactly_as_before(self):
        # The single-marker path is the overwhelmingly common one and must not change shape.
        fake = wire(headers={act.CONSENT_HEADER: self.si_token}, markers=self.markers)
        si = FakeDoc(in_insert=False)
        _save(si, act.before_submit, si, "before_submit")
        self.assertIsNone(fake.thrown)


class TestTheCreationRecordAndTheCustodyChain(unittest.TestCase):
    """The two properties the 0.10.0 fix rests on that no test observed, found by mutation in the
    second lens: deleting `after_insert`'s governed-act guard, and stamping the ENCLOSING act instead
    of the current one, each left all 445 tests green. A safety property nothing can fail on is a
    comment, not a guarantee.
    """

    def setUp(self):
        self.fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())

    def test_the_recording_hook_stamps_nothing_outside_a_governed_act(self):
        # Observed DIRECTLY on the document, not inferred from a later refusal. The pre-existing
        # test asserted a refusal that fires for an unrelated reason (`_require_consent` returns
        # before `_may_ride` when nothing encloses), so the stamp's absence was invisible to it.
        loose = FakeDoc(doctype="Serial and Batch Bundle", name="SBB-LOOSE", in_insert=False)
        insert(loose, act.after_insert, loose, "after_insert")
        self.assertNotIn(act._CREATED_IN_ACT, loose.flags)

    def test_the_recording_hook_DOES_stamp_inside_a_governed_act(self):
        # The other half of the same property: the stamp must actually land where it is needed, or
        # the test above would pass on a hook that never stamps anything at all.
        si = FakeDoc(in_insert=False)
        created = FakeDoc(doctype="Serial and Batch Bundle", name="SBB-NEW", in_insert=False)

        def cascade():
            insert(created, act.after_insert, created, "after_insert")

        _save(si, lambda: (act.before_submit(si, "before_submit"), cascade()))
        self.assertTrue(created.flags.get(act._CREATED_IN_ACT))

    def test_custody_records_THIS_act_not_the_act_it_rode(self):
        # Three levels. A governed CANCEL licenses a nested SUBMIT of a document it created; that
        # submit must record ITSELF as the established act, so a cancel nested one level deeper is
        # judged against a submit and refused. Stamping the act it rode instead would let the
        # original cancel authority reach arbitrary depth and reopen the crossing underneath it.
        self.fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(action="cancel"))
        si = FakeDoc(in_insert=False)

        def deepest():
            victim = FakeDoc(doctype="Asset Depreciation Schedule", name="ADS-PRE-EXISTING",
                             in_insert=False)
            _save(victim, act.before_cancel, victim, "before_cancel")

        def middle():
            rev = FakeDoc(doctype="Journal Entry", name="REV-JE-1", in_insert=True)
            insert(rev, lambda: (act.before_submit(rev, "before_submit"), deepest()))

        with self.assertRaises(FakePermissionError):
            _save(si, lambda: (act.before_cancel(si, "before_cancel"), middle()))
        self.assertIn("ADS-PRE-EXISTING", self.fake.thrown[0])


def _in_preview_frame(previewed_doc, fn):
    """Run ``fn()`` beneath a REAL frame that looks like ERPNext's ledger preview.

    The frame walk keys on (function name, module name), so a faithful test has to produce an actual
    frame with both — not a mock. Built by exec'ing into a globals dict whose ``__name__`` is
    erpnext's module, which is exactly what `_previewing_document` reads.
    """
    g = {"__name__": "erpnext.controllers.stock_controller", "_fn": fn}
    exec("def get_accounting_ledger_preview(doc, filters=None):\n    return _fn()\n", g)
    return g["get_accounting_ledger_preview"](previewed_doc)


def _in_lookalike_frame(previewed_doc, fn):
    """Same function NAME, wrong module. The module is half the signal."""
    g = {"__name__": "attacker.controllers.stock_controller", "_fn": fn}
    exec("def get_accounting_ledger_preview(doc, filters=None):\n    return _fn()\n", g)
    return g["get_accounting_ledger_preview"](previewed_doc)


class TestConsentCoversThePreview(unittest.TestCase):
    """0.13.0. ERPNext previews a posting by performing it and rolling back
    (`stock_controller.py:2058-2066`), so with consent enforced the preview's own cascade was refused
    and the broker's PLAN step could not complete at all. Consent now covers the preview: previewing a
    submit needs the marker for that submit, and does not spend it.
    """

    def test_a_preview_with_NO_marker_is_REFUSED(self):
        """The preview is gated, not exempted. This is the whole point."""
        fake = wire(headers={act.CONSENT_HEADER: None}, markers=marker())
        with self.assertRaises(FakePermissionError):
            act.before_gl_preview(FakeDoc(), "before_gl_preview")
        self.assertIn("preview", fake.thrown[0].lower())

    def test_a_stock_preview_with_NO_marker_is_REFUSED(self):
        fake = wire(headers={act.CONSENT_HEADER: None}, markers=marker())
        with self.assertRaises(FakePermissionError):
            act.before_sl_preview(FakeDoc(), "before_sl_preview")
        self.assertIn("preview", fake.thrown[0].lower())

    def test_a_preview_with_a_valid_marker_is_allowed_and_does_NOT_spend_it(self):
        """Consenting once must authorise a POSTING, not merely a projection of one."""
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        act.before_gl_preview(FakeDoc(), "before_gl_preview")
        self.assertIsNone(fake.thrown)
        self.assertFalse(any(row["burned"] for row in fake.db.markers.values()),
                         "a preview commits nothing and must not spend the marker")

    def test_the_marker_survives_the_preview_and_the_real_submit_then_spends_it(self):
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        act.before_gl_preview(FakeDoc(), "before_gl_preview")
        self.assertFalse(any(row["burned"] for row in fake.db.markers.values()))
        act.before_submit(FakeDoc(), "before_submit")
        self.assertIsNone(fake.thrown)
        self.assertTrue(all(row["burned"] for row in fake.db.markers.values()),
                        "single-use still means single POSTING")

    def test_an_ungated_user_previews_untouched(self):
        """Inert on any site that never opted in, like every other handler here."""
        fake = wire(gated=False, headers={act.CONSENT_HEADER: None}, markers={})
        act.before_gl_preview(FakeDoc(), "before_gl_preview")
        self.assertIsNone(fake.thrown)

    def test_a_cascade_created_INSIDE_a_consented_preview_rides(self):
        """The failure this feature exists for: a Payment Ledger Entry that did not exist until the
        preview ran, so no human could have minted a marker naming it."""
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(docname="SI-0001"))
        invoice = FakeDoc(name="SI-0001")
        act.before_gl_preview(invoice, "before_gl_preview")
        ple = FakeDoc(doctype="Payment Ledger Entry", name="PLE-abc", in_insert=True)
        _in_preview_frame(invoice, lambda: act.before_submit(ple, "before_submit"))
        self.assertIsNone(fake.thrown, "the preview's own cascade must ride the consent it verified")

    def test_a_two_step_creation_INSIDE_a_consented_preview_rides(self):
        """ERPNext's ordinary cascade idiom is TWO calls — `doc.save()` then `doc.submit()` — and
        inside a preview there is no enclosing WRITE frame at all, because the previewed document is
        not being written. So `after_insert`'s write-frame walk finds nothing, records no creation,
        and the later submit arrives looking exactly like a pre-existing draft the caller named:
        refused, and the whole preview dies with it. `serial_batch_bundle.py:1166`/`:1172` is this
        shape and a preview with `update_stock` reaches it.

        The creation record has to be made from the PREVIEW walk as well as the write walk. This is
        the same asymmetry `_require_consent` already closed at its own call site.
        """
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(docname="SI-0001"))
        invoice = FakeDoc(name="SI-0001")
        act.before_gl_preview(invoice, "before_gl_preview")
        sbb = FakeDoc(doctype="Serial and Batch Bundle", name="SBB-NEW", in_insert=False)

        def cascade():
            # `doc.save()` -> Document.insert -> run_method("after_insert") at document.py:498.
            insert(sbb, act.after_insert, sbb, "after_insert")
            # `doc.submit()` -> _submit -> save() -> _save. Named and not `__islocal`, so :571-572
            # does not delegate back to insert() and `in_insert` stays False.
            _save(sbb, act.before_submit, sbb, "before_submit")

        _in_preview_frame(invoice, cascade)
        self.assertIsNone(fake.thrown,
                          "a document the PREVIEW itself created in two steps must ride the consent "
                          "the preview verified — no human could have named it")

    def test_after_insert_stamps_ONLY_when_an_enclosing_governed_act_is_found(self):
        """Asserts on the STAMP, at the altitude the 0.13.0 change was made.

        Found by mutation: replacing `after_insert`'s condition with `if True:` — stamping every
        insert on the site — turned NO existing test red. The downstream tests could not see it
        because a stamp licenses nothing on its own (`_may_ride` is unreachable without a live
        enclosing act), which is a true safety property and also exactly why it hid the mutant. The
        widened condition therefore needs a check that reads the stamp directly, or "stamps only when
        it should" is an architectural argument with no test behind it.
        """
        wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(docname="SI-0001"))
        # 1. No enclosing act of any kind: an ordinary insert anywhere on the site.
        loose = FakeDoc(doctype="Serial and Batch Bundle", name="SBB-LOOSE", in_insert=False)
        insert(loose, act.after_insert, loose, "after_insert")
        self.assertFalse(act._flag_get(loose, act._CREATED_IN_ACT),
                         "an insert outside any governed act must leave no creation record")
        # 2. Inside a CONSENTED preview: stamped, which is what 0.13.0 added.
        invoice = FakeDoc(name="SI-0001")
        act.before_gl_preview(invoice, "before_gl_preview")
        created = FakeDoc(doctype="Serial and Batch Bundle", name="SBB-NEW", in_insert=False)
        _in_preview_frame(invoice, lambda: insert(created, act.after_insert, created, "after_insert"))
        self.assertTrue(act._flag_get(created, act._CREATED_IN_ACT),
                        "the preview created this document, so the creation must be recorded")
        # 3. Same shape under a LOOKALIKE module. The module is half the signal on this new edge too.
        spoofed = FakeDoc(doctype="Serial and Batch Bundle", name="SBB-SPOOF", in_insert=False)
        _in_lookalike_frame(invoice, lambda: insert(spoofed, act.after_insert, spoofed,
                                                    "after_insert"))
        self.assertFalse(act._flag_get(spoofed, act._CREATED_IN_ACT),
                         "a frame with the right name in the wrong module records nothing")

    def test_a_two_step_creation_under_a_LOOKALIKE_preview_frame_is_REFUSED(self):
        """The end-to-end fail-closed counterpart of the ride test above. The existing lookalike test
        covers the single-call cascade; this covers the two-step one the 0.13.0 stamp reaches."""
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(docname="SI-0001"))
        invoice = FakeDoc(name="SI-0001")
        act.before_gl_preview(invoice, "before_gl_preview")
        sbb = FakeDoc(doctype="Serial and Batch Bundle", name="SBB-NEW", in_insert=False)

        def cascade():
            insert(sbb, act.after_insert, sbb, "after_insert")
            _save(sbb, act.before_submit, sbb, "before_submit")

        with self.assertRaises(FakePermissionError):
            _in_lookalike_frame(invoice, cascade)
        self.assertIn("SBB-NEW", fake.thrown[0])

    def test_the_SAME_cascade_with_NO_preview_frame_is_REFUSED(self):
        """Fail-closed direction: no recognised preview frame, no ride."""
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(docname="SI-0001"))
        invoice = FakeDoc(name="SI-0001")
        act.before_gl_preview(invoice, "before_gl_preview")
        ple = FakeDoc(doctype="Payment Ledger Entry", name="PLE-abc", in_insert=True)
        with self.assertRaises(FakePermissionError):
            act.before_submit(ple, "before_submit")
        self.assertIsNotNone(fake.thrown)

    def test_a_lookalike_module_does_NOT_license_a_cascade(self):
        """The function name alone is not the signal. Same lesson as `_writing_document`."""
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(docname="SI-0001"))
        invoice = FakeDoc(name="SI-0001")
        act.before_gl_preview(invoice, "before_gl_preview")
        ple = FakeDoc(doctype="Payment Ledger Entry", name="PLE-abc", in_insert=True)
        with self.assertRaises(FakePermissionError):
            _in_lookalike_frame(invoice, lambda: act.before_submit(ple, "before_submit"))

    def test_an_UNCONSENTED_preview_licenses_nothing(self):
        """No stamp without a verified marker, so nothing can ride an unconsented preview."""
        fake = wire(headers={act.CONSENT_HEADER: None}, markers=marker(docname="SI-0001"))
        invoice = FakeDoc(name="SI-0001")
        with self.assertRaises(FakePermissionError):
            act.before_gl_preview(invoice, "before_gl_preview")
        ple = FakeDoc(doctype="Payment Ledger Entry", name="PLE-abc", in_insert=True)
        with self.assertRaises(FakePermissionError):
            _in_preview_frame(invoice, lambda: act.before_submit(ple, "before_submit"))

    def test_a_pre_existing_draft_named_by_the_caller_still_does_NOT_ride_a_preview(self):
        """`_may_ride` is unchanged and still requires creation-by-the-act. A document the caller
        NAMED could have been put in front of a human, so it needs its own marker even inside a
        consented preview."""
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(docname="SI-0001"))
        invoice = FakeDoc(name="SI-0001")
        act.before_gl_preview(invoice, "before_gl_preview")
        named = FakeDoc(doctype="Serial and Batch Bundle", name="SBB-1", in_insert=False)
        with self.assertRaises(FakePermissionError):
            _in_preview_frame(invoice, lambda: act.before_submit(named, "before_submit"))

    def test_the_previewed_document_itself_still_needs_the_marker_spent(self):
        """A preview must not become a way to submit the previewed document for free: it is not
        created by the act, so it cannot ride, and its own submit spends the marker."""
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker(docname="SI-0001"))
        invoice = FakeDoc(name="SI-0001", in_insert=False)
        act.before_gl_preview(invoice, "before_gl_preview")
        act.before_submit(invoice, "before_submit")
        self.assertIsNone(fake.thrown)
        self.assertTrue(all(row["burned"] for row in fake.db.markers.values()))


class TestTheRemedyAnOperatorIsToldToRunActuallyWorks(unittest.TestCase):
    """A refusal is the one message an operator is guaranteed to read, at the moment they are
    blocked. ``test_consent.py`` already asserts this for ``consent_verdict``'s own text, but that
    test reaches exactly ONE of the places this package tells an operator how to get unblocked —
    so ``pacioli mint`` survived in two others.

    ``pacioli mint`` cannot produce a floor marker. It is a console script in the SEPARATE
    ``pacioli`` broker distribution (``pacioli-guard`` ships none), it takes a ``plan_id``
    positionally, and it writes a plan-bound marker into the BROKER's own store via
    ``store.mint_marker(token, plan_id, ...)`` — it never connects to the books and never creates a
    ``Pacioli Consent Marker`` row, which is the only thing this gate reads.

    For the PREVIEW refusal the advice is worse than merely wrong, it is circular: ``pacioli mint``
    refuses without a recorded plan ("the agent must call plan_submit first"), and ``plan_submit``
    is the call being refused. An operator following the message lands in a closed loop.
    """

    def _preview_refusal(self):
        fake = wire(headers={act.CONSENT_HEADER: None}, markers=marker())
        with self.assertRaises(FakePermissionError):
            act.before_gl_preview(FakeDoc(), "before_gl_preview")
        return fake.thrown[0]

    def test_the_preview_refusal_does_not_send_the_operator_around_a_closed_loop(self):
        self.assertNotIn("pacioli mint", self._preview_refusal())

    def test_the_preview_refusal_names_the_thing_that_must_actually_be_created(self):
        # What the gate reads is a `Pacioli Consent Marker` for this document and act. Name that,
        # so the remedy matches the mechanism being enforced.
        reason = self._preview_refusal()
        self.assertIn("Pacioli Consent Marker", reason)

    def test_every_refusal_this_module_emits_promises_no_unusable_route(self):
        """The coverage generalised, because this defect arrived twice in one hour — and asserted
        on the REAL messages, not on the module source.

        Both banned remedies are things an operator cannot actually do: `pacioli mint` builds a
        plan-bound marker in the BROKER's store, and the desk form cannot save a marker at all
        (`token_hash` is `reqd` + `read_only` with no default, and the controller sets only
        `minted_by`). A refusal may state what must EXIST; it may not name either route.

        This drives all three of `act`'s refusal sites — the plain no-marker refusal, the
        unspendable-marker refusal, and the preview refusal — because the first fix landed in one
        of them and left the others lying.
        """
        messages = []

        fake = wire(headers={act.CONSENT_HEADER: None}, markers=marker())
        with self.assertRaises(FakePermissionError):
            act.before_submit(FakeDoc(), "before_submit")
        messages.append(fake.thrown[0])

        messages.append(self._preview_refusal())

        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        act._claim_consent = lambda record: False
        try:
            with self.assertRaises(FakePermissionError):
                act.before_submit(FakeDoc(), "before_submit")
        finally:
            del act._claim_consent
        messages.append(fake.thrown[0])

        self.assertEqual(len(messages), 3, "every refusal site must be exercised here")
        for reason in messages:
            self.assertNotIn("pacioli mint", reason)
            self.assertNotIn("desk UI", reason)
            # And each must point at the route this package DOES ship, or the operator is left
            # accurately described and still stuck.
            self.assertIn("pacioli_guard.mint.mint_consent_marker", reason)
            self.assertIn("bench", reason.lower())

    def test_the_unspendable_marker_refusal_does_not_name_the_brokers_plan_bound_cli(self):
        # act.py's second refusal: a marker that could not be spent. Shipped text, same defect.
        fake = wire(headers={act.CONSENT_HEADER: TOKEN}, markers=marker())
        act._claim_consent = lambda record: False
        try:
            with self.assertRaises(FakePermissionError):
                act.before_submit(FakeDoc(), "before_submit")
        finally:
            del act._claim_consent
        self.assertIn("single-use", fake.thrown[0])
        self.assertNotIn("pacioli mint", fake.thrown[0])
