# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Glue-level tests for guard.enforce — the frappe wall around the pure decision core.

These use an in-memory ``frappe`` fake, injected by reassigning ``enforce.frappe``. The design
under test: Pacioli scopes **the principal the request executes as** (``frappe.session.user``),
gated on an api-key ``Authorization`` header being present so plain desk/cookie sessions and
Bearer/OAuth stay untouched. By the time frappe runs ``auth_hooks`` it has ALREADY authenticated
the credential and settled ``session.user`` — including resolving a ``Frappe-Authorization-Source``
(non-User doctype) credential to its owning user. So Pacioli reads frappe's settled identity rather
than re-deriving it from the header (which could diverge from the executing principal).

⚠️ These prove the *wiring logic* (gate + which principal is scoped + the 403 throw path). What they
CANNOT prove: that real frappe actually settles ``session.user`` as modelled (verified once against
the live 16.25.0 bench) or that the **alt-source** resolution really lands in ``session.user`` — that
end-to-end fact now lives ONLY in the lab re-proof, since Pacioli no longer resolves alt-source itself.

Run: ``python3 -m unittest guard.tests.test_enforce`` from the app root. No frappe required.
"""
import base64
import sys
import types
import unittest

# enforce.py does a hard ``import frappe`` at module top (it IS the frappe glue). Satisfy that with
# an empty stub so the module imports bench-free; every test reassigns ``enforce.frappe`` to its own
# fake before calling in. Keeps enforce.py pristine — no test-only import shim.
sys.modules.setdefault("frappe", types.ModuleType("frappe"))

from pacioli_guard import enforce
from pacioli_guard.scope import is_permitted


class FakeRow:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeScopeDoc:
    """Stands in for a loaded 'API Key Scope' DocType — the attributes _scope_from_doctype reads.

    ``**contain`` (``enabled`` / ``rate_limit_per_minute`` / ``enforce_workflow``) is applied via
    ``__dict__`` only when passed, so a bare ``FakeScopeDoc(...)`` genuinely LACKS those
    attributes — it models a pre-migration doc on a not-yet-upgraded site, which
    ``getattr(doc, ..., None)`` must read as enabled / no-limit / workflow-gate-off."""

    def __init__(self, allow_resource=False, method_patterns=(), resource_doctypes=(), **contain):
        self.allow_resource = allow_resource
        self.methods = [FakeRow(pattern=p) for p in method_patterns]
        self.resource_doctypes = [FakeRow(ref_doctype=d) for d in resource_doctypes]
        self.__dict__.update(contain)


class FakeSession:
    def __init__(self, user="Guest"):
        self.user = user


class FakeReq:
    def __init__(self, path, method):
        self.path = path
        self.method = method


class FakeLocal:
    def __init__(self, request=None):
        self.request = request


class FakePermissionError(Exception):
    pass


class FakeDB:
    """Models the two frappe.db reads enforce.py still makes once it has the user: the scope-doc
    discovery and the deprecated legacy-field read."""

    def __init__(self, scopes, legacy, legacy_columns):
        self._scopes = scopes            # {user: FakeScopeDoc}
        self._legacy = legacy            # {user: raw api_scope value (str/dict)}
        self._legacy_columns = set(legacy_columns)

    def get_value(self, doctype, filters, fieldname):
        # scope-doc discovery: get_value("API Key Scope", {"user": u}, "name")
        if doctype == enforce.SCOPE_DOCTYPE and isinstance(filters, dict) and "user" in filters:
            u = filters["user"]
            return f"AKS::{u}" if u in self._scopes else None
        # legacy read: get_value("User", <user>, "api_scope")
        if doctype == "User" and isinstance(filters, str) and fieldname == enforce.LEGACY_SCOPE_FIELD:
            return self._legacy.get(filters)
        return None

    def has_column(self, doctype, column):
        return column in self._legacy_columns


class FakeWorkflowModule:
    """Models ``frappe.model.workflow`` — specifically the one function ``enforce.py`` calls
    directly, ``get_workflow_name(doctype)``. This is the INTERNAL, frappe-cached lookup (no
    System-Manager REST wall, no auth-hook recursion) that lets the guard check Workflow existence
    from inside the auth hook itself.
    """

    def __init__(self, workflows=None, error_doctypes=()):
        self._workflows = dict(workflows or {})     # {doctype: workflow_name}
        self._error_doctypes = set(error_doctypes)   # doctypes whose lookup raises

    def get_workflow_name(self, doctype):
        if doctype in self._error_doctypes:
            raise RuntimeError("workflow lookup exploded")
        return self._workflows.get(doctype)  # None (frappe may also return "") for "no workflow"


class FakeModel:
    def __init__(self, workflows=None, error_doctypes=()):
        self.workflow = FakeWorkflowModule(workflows, error_doctypes)


class FakeCache:
    """Models the two calls _rate_window_count makes on frappe.cache(): incr + expire."""

    def __init__(self):
        self.counts = {}
        self.expires = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, ttl):
        self.expires[key] = ttl


class ExplodingCache:
    """A cache whose counter is unusable — the fail-closed branch of the rate limiter."""

    def incr(self, key):
        raise RuntimeError("redis is on fire")

    def expire(self, key, ttl):  # pragma: no cover — incr already raised
        raise RuntimeError("redis is on fire")


class FakeFrappe:
    def __init__(self, *, headers=None, session_user="Guest", scopes=None, legacy=None,
                 legacy_columns=(), request=None, form_dict=None, cache=None, log_raises=False,
                 workflows=None, workflow_error_doctypes=()):
        self._headers = headers or {}
        self.session = FakeSession(session_user)
        self._scopes = scopes or {}
        self.db = FakeDB(self._scopes, legacy or {}, legacy_columns)
        self.local = FakeLocal(request)
        self.form_dict = form_dict or {}
        self.PermissionError = FakePermissionError
        self.thrown = None
        self._cache = cache if cache is not None else FakeCache()
        self._log_raises = log_raises
        self.denial_logs = []  # (title, message) rows frappe.log_error received
        self.model = FakeModel(workflows, workflow_error_doctypes)

    def get_request_header(self, key, default=None):
        return self._headers.get(key, default)

    def get_doc(self, doctype, name):
        assert doctype == enforce.SCOPE_DOCTYPE and name.startswith("AKS::")
        return self._scopes[name[len("AKS::"):]]

    def cache(self):
        return self._cache

    def log_error(self, title=None, message=None):
        if self._log_raises:
            raise RuntimeError("Error Log itself is broken")
        self.denial_logs.append((title, message))

    def throw(self, msg, exc=None):
        self.thrown = (msg, exc)
        raise (exc or Exception)(msg)


PING_ONLY = FakeScopeDoc(method_patterns=["frappe.ping"])
A_GRANT = FakeScopeDoc(method_patterns=["a.method"])
B_GRANT = FakeScopeDoc(method_patterns=["b.method"])
AUTH = {"Authorization": "token ANY_KEY:secret"}  # a well-formed api-key header (opens the gate)


class _Base(unittest.TestCase):
    def setUp(self):
        self._real = enforce.frappe

    def tearDown(self):
        enforce.frappe = self._real


class TestScopeSubject(_Base):
    """Pacioli scopes frappe.session.user — the principal the request runs as."""

    def test_scopes_the_session_user_not_the_header_key(self):
        # session settled to a SCOPED user (e.g. cookie won, or the key's own user); the header
        # carries SOME api-key. The scope enforced must be session.user's, never a header re-derivation.
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="boss@x", scopes={"boss@x": PING_ONLY})
        scope = enforce._scope_for_request()
        self.assertIsNotNone(scope)
        # Testing WHOSE grant is enforced (session subject), isolated from deny-unknown resolution.
        self.assertTrue(is_permitted(scope, "method", "frappe.ping", method_resolved=True))
        self.assertFalse(is_permitted(scope, "method", "evil.delete", method_resolved=True))
        self.assertFalse(is_permitted(scope, "resource", ("ToDo", "create")))

    def test_basic_scheme_opens_the_gate(self):
        basic = base64.b64encode(b"K:s").decode()
        enforce.frappe = FakeFrappe(
            headers={"Authorization": f"Basic {basic}"}, session_user="alice@x",
            scopes={"alice@x": PING_ONLY},
        )
        self.assertIsNotNone(enforce._scope_for_request())

    def test_two_user_ownership_distinguishes_grants(self):
        # The SAME api-key header value; only session.user differs -> different grant enforced.
        for user, allowed, denied in (("a@x", "a.method", "b.method"), ("b@x", "b.method", "a.method")):
            enforce.frappe = FakeFrappe(headers=AUTH, session_user=user,
                                        scopes={"a@x": A_GRANT, "b@x": B_GRANT})
            scope = enforce._scope_for_request()
            # Testing GRANT OWNERSHIP (which user's grant applies), isolated from deny-unknown.
            self.assertTrue(is_permitted(scope, "method", allowed, method_resolved=True), user)
            self.assertFalse(is_permitted(scope, "method", denied, method_resolved=True), user)


class TestGate(_Base):
    """Only api-key-authenticated requests are scoped; everything else is left untouched."""

    def test_no_auth_header_is_untouched(self):
        enforce.frappe = FakeFrappe(session_user="alice@x", scopes={"alice@x": PING_ONLY})
        self.assertIsNone(enforce._scope_for_request())

    def test_bearer_scheme_is_untouched(self):
        # OAuth Bearer is a distinct credential class -> not this api-key guard, even for a user
        # who HAS a grant. api_key_from_auth_header returns None for bearer, so the gate closes.
        enforce.frappe = FakeFrappe(headers={"Authorization": "Bearer abc.def"},
                                    session_user="alice@x", scopes={"alice@x": PING_ONLY})
        self.assertIsNone(enforce._scope_for_request())

    def test_api_key_header_but_guest_session_is_noop(self):
        # A malformed multi-colon token: frappe swallows its ValueError and never authenticates, so
        # session.user stays Guest. We must no-op (frappe's own final guard then 401s) -- NOT crash,
        # NOT enforce a phantom scope. (Old header-derivation fed the doctype to get_value -> 500.)
        enforce.frappe = FakeFrappe(headers={"Authorization": "token x:secret:extra"},
                                    session_user="Guest", scopes={"x": PING_ONLY})
        self.assertIsNone(enforce._scope_for_request())


class TestGrantResolution(_Base):
    def test_resolved_user_without_any_grant_is_unscoped(self):
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="nobody@x", scopes={})
        self.assertIsNone(enforce._scope_for_request())

    def test_legacy_json_field_fallback_parses(self):
        enforce.frappe = FakeFrappe(
            headers=AUTH, session_user="carol@x", scopes={},
            legacy={"carol@x": '{"methods": ["legacy.m"], "allow_resource": false}'},
            legacy_columns=["api_scope"],
        )
        scope = enforce._scope_for_request()
        self.assertIsNotNone(scope)
        # Testing the LEGACY FIELD PARSE, isolated from deny-unknown resolution.
        self.assertTrue(is_permitted(scope, "method", "legacy.m", method_resolved=True))

    def test_legacy_malformed_json_is_none_not_crash(self):
        enforce.frappe = FakeFrappe(
            headers=AUTH, session_user="dave@x", scopes={},
            legacy={"dave@x": "{not valid json"}, legacy_columns=["api_scope"],
        )
        self.assertIsNone(enforce._scope_for_request())


class TestCheckScopeEntrypoint(_Base):
    """The literal auth_hooks entrypoint: classify -> is_permitted -> 403 throw, or no-op."""

    def _fake(self, session_user, scopes, path, method):
        return FakeFrappe(headers=AUTH, session_user=session_user, scopes=scopes,
                          request=FakeReq(path, method), form_dict={})

    def test_throws_permissionerror_on_out_of_scope_call(self):
        enforce.frappe = self._fake("alice@x", {"alice@x": PING_ONLY},
                                    "/api/method/frappe.client.delete", "POST")
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()

    def test_noop_on_in_scope_call(self):
        # A bare /api/method/<name> call is now deny-unknown (unresolved, not doctype-qualified) --
        # so an in-scope pass needs either a SAFE_METHODS name (used here) or a resolved shape.
        safe_probe = FakeScopeDoc(method_patterns=["frappe.auth.get_logged_user"])
        enforce.frappe = self._fake("alice@x", {"alice@x": safe_probe},
                                    "/api/method/frappe.auth.get_logged_user", "GET")
        self.assertIsNone(enforce.check_scope())
        self.assertIsNone(enforce.frappe.thrown)

    def test_noop_for_unscoped_user_even_on_dangerous_path(self):
        enforce.frappe = self._fake("nobody@x", {}, "/api/resource/ToDo", "DELETE")
        self.assertIsNone(enforce.check_scope())

    def test_out_of_scope_deny_writes_an_audit_row(self):
        enforce.frappe = self._fake("alice@x", {"alice@x": PING_ONLY},
                                    "/api/method/frappe.client.delete", "POST")
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()
        self.assertEqual(len(enforce.frappe.denial_logs), 1)
        title, message = enforce.frappe.denial_logs[0]
        self.assertIn("out of scope", title)
        self.assertIn("frappe.client.delete", message)

    def test_resource_verb_narrowing_flows_through_the_glue(self):
        # A read-only resource grant (verb_delete unticked) must let a GET pass but deny a DELETE
        # on the same DocType — the per-credential verb narrowing, driven off the DocType fields.
        read_only = FakeScopeDoc(allow_resource=True, resource_doctypes=["ToDo"],
                                 verb_read=1, verb_create=0, verb_write=0, verb_delete=0)
        enforce.frappe = self._fake("dana@x", {"dana@x": read_only},
                                    "/api/resource/ToDo", "GET")
        self.assertIsNone(enforce.check_scope())  # read allowed
        enforce.frappe = self._fake("dana@x", {"dana@x": read_only},
                                    "/api/resource/ToDo/some-name", "DELETE")
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()  # delete denied by the verb narrowing

    def test_all_verbs_ticked_is_unchanged_full_crud(self):
        # Every verb box on (the migrate default) = the pre-narrowing behavior: full CRUD.
        full = FakeScopeDoc(allow_resource=True, resource_doctypes=["ToDo"],
                            verb_read=1, verb_create=1, verb_write=1, verb_delete=1)
        enforce.frappe = self._fake("evan@x", {"evan@x": full},
                                    "/api/resource/ToDo/some-name", "DELETE")
        self.assertIsNone(enforce.check_scope())


class TestBodyDoctypeScoping(_Base):
    """End-to-end ``check_scope()`` proof that the body-doctype rewrite (``body_scoped_target``)
    is actually wired in: a credential granted ONLY ``"Sales Invoice.submit"`` can submit a Sales
    Invoice via ``frappe.client.submit``/``run_doc_method``/``savedocs`` but is DENIED the same
    RPC against a Journal Entry -- the exact per-doctype enforcement the URL-path ``run_method``
    vector already had, now closed for the body-carried shapes too. Fail-closed cases (unparseable
    doctype) are also driven through the full entrypoint, not just the pure core.
    """

    def _fake(self, scope_doc, path, method, form_dict):
        return FakeFrappe(headers=AUTH, session_user="alice@x", scopes={"alice@x": scope_doc},
                          request=FakeReq(path, method), form_dict=form_dict)

    SI_SUBMIT_ONLY = FakeScopeDoc(method_patterns=["Sales Invoice.submit"])
    SI_CANCEL_ONLY = FakeScopeDoc(method_patterns=["Sales Invoice.cancel"])

    def test_client_submit_allowed_for_the_granted_doctype(self):
        enforce.frappe = self._fake(
            self.SI_SUBMIT_ONLY, "/api/method/frappe.client.submit", "POST",
            {"doc": '{"doctype": "Sales Invoice", "name": "SINV-1"}'},
        )
        self.assertIsNone(enforce.check_scope())

    def test_client_submit_denied_for_a_different_doctype(self):
        # The scoping payoff: a credential granted ONLY Sales Invoice.submit cannot ride
        # frappe.client.submit to submit a Journal Entry, even though the method NAME
        # "frappe.client.submit" is generic and not itself in the allowlist.
        enforce.frappe = self._fake(
            self.SI_SUBMIT_ONLY, "/api/method/frappe.client.submit", "POST",
            {"doc": '{"doctype": "Journal Entry", "name": "JE-1"}'},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Journal Entry.submit", str(ctx.exception))

    def test_client_submit_with_generic_method_grant_no_longer_bypasses(self):
        # Before this fix: granting the bare "frappe.client.submit" name let a credential submit
        # ANY doctype through it (the documented residual). After: the rewritten target
        # ("<DocType>.submit") is what's checked, so a bare-name-only grant no longer suffices.
        bare_grant = FakeScopeDoc(method_patterns=["frappe.client.submit"])
        enforce.frappe = self._fake(
            bare_grant, "/api/method/frappe.client.submit", "POST",
            {"doc": '{"doctype": "Journal Entry", "name": "JE-1"}'},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        # Pin that the REWRITE did the work: the denial names the RESOLVED target
        # "Journal Entry.submit", not the bare "frappe.client.submit". Delete the body-doctype
        # rewrite and deny-unknown would still deny the bare name — but name it differently — so
        # without this assertion the test passes for the wrong reason.
        self.assertIn("Journal Entry.submit", str(ctx.exception))

    def test_client_submit_unparseable_doc_fails_closed_at_the_entrypoint(self):
        enforce.frappe = self._fake(
            self.SI_SUBMIT_ONLY, "/api/method/frappe.client.submit", "POST", {"doc": "not-json"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("out of scope", enforce.frappe.denial_logs[0][0])

    def test_client_cancel_allowed_for_the_granted_doctype(self):
        enforce.frappe = self._fake(
            self.SI_CANCEL_ONLY, "/api/method/frappe.client.cancel", "POST",
            {"doctype": "Sales Invoice", "name": "SINV-1"},
        )
        self.assertIsNone(enforce.check_scope())

    def test_client_cancel_denied_for_a_different_doctype(self):
        enforce.frappe = self._fake(
            self.SI_CANCEL_ONLY, "/api/method/frappe.client.cancel", "POST",
            {"doctype": "Journal Entry", "name": "JE-1"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Journal Entry.cancel", str(ctx.exception))

    def test_run_doc_method_submit_denied_for_a_different_doctype(self):
        enforce.frappe = self._fake(
            self.SI_SUBMIT_ONLY, "/api/method/run_doc_method", "POST",
            {"dt": "Journal Entry", "dn": "JE-1", "method": "submit"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Journal Entry.submit", str(ctx.exception))

    def test_bulk_submit_literal_grant_no_longer_bypasses(self):
        # REGRESSION (guard-bypass redteam CRITICAL, 2026-07-06): the exact exploit — an operator
        # enables the Desk "Bulk Edit > Submit" feature by granting the bulk RPC's literal method
        # name. Before the fix, that grant let the credential bulk-submit ANY doctype doctype-blind.
        # After: the request rewrites to "<DocType>.submit", which the literal grant does not match.
        bulk_grant = FakeScopeDoc(method_patterns=[
            "Sales Invoice.submit",
            "frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs"])
        enforce.frappe = self._fake(
            bulk_grant,
            "/api/method/frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs",
            "POST",
            {"doctype": "Journal Entry", "docnames": '["JE-1"]', "action": "submit"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Journal Entry.submit", str(ctx.exception))

    def test_desk_cancel_denied_for_a_different_doctype(self):
        # Companion HIGH: the Desk UI's own cancel endpoint, now per-doctype scoped.
        enforce.frappe = self._fake(
            self.SI_CANCEL_ONLY, "/api/method/frappe.desk.form.save.cancel", "POST",
            {"doctype": "Journal Entry", "name": "JE-1"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Journal Entry.cancel", str(ctx.exception))

    def test_client_save_with_docstatus_1_denied_for_a_different_doctype(self):
        # Completeness audit: frappe.client.save carrying docstatus=1 submits via Document.save()'s
        # transition detection — a credential scoped only to Sales Invoice.submit cannot ride it to
        # submit a Journal Entry.
        enforce.frappe = self._fake(
            self.SI_SUBMIT_ONLY, "/api/method/frappe.client.save", "POST",
            {"doc": '{"doctype": "Journal Entry", "name": "JE-1", "docstatus": 1}'},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Journal Entry.submit", str(ctx.exception))

    def test_client_save_draft_now_denied_bare_by_deny_unknown(self):
        # FLIPPED (deny-unknown, §2): a genuine draft create (docstatus 0) is NOT a submit, so it
        # still stays on the original doctype-blind create path (body_scoped_target returns None,
        # unchanged) -- but that original bare "frappe.client.save" target is now ITSELF
        # unresolved-and-not-SAFE_METHODS, so is_permitted's own deny-unknown default denies it
        # separately, where it used to pass on the bare-name grant alone. Redirect to POST
        # /api/resource/<DocType> to create a draft under a scoped credential now.
        bare_save = FakeScopeDoc(method_patterns=["frappe.client.save"])
        enforce.frappe = self._fake(
            bare_save, "/api/method/frappe.client.save", "POST",
            {"doc": '{"doctype": "ToDo"}'},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("out of scope", enforce.frappe.denial_logs[0][0])

    def test_run_doc_method_submit_allowed_for_the_granted_doctype(self):
        enforce.frappe = self._fake(
            self.SI_SUBMIT_ONLY, "/api/method/run_doc_method", "POST",
            {"dt": "Sales Invoice", "dn": "SINV-1", "method": "submit"},
        )
        self.assertIsNone(enforce.check_scope())

    def test_run_doc_method_get_pdf_now_denied_for_an_ungranted_doctype(self):
        # FLIPPED (deny-unknown, §6): run_doc_method now resolves EVERY inner method to
        # "<DocType>.<method>", so a bare "run_doc_method" grant (doctype-blind) no longer suffices --
        # the credential must hold "Journal Entry.get_pdf" specifically.
        blind_grant = FakeScopeDoc(method_patterns=["run_doc_method"])
        enforce.frappe = self._fake(
            blind_grant, "/api/method/run_doc_method", "POST",
            {"dt": "Journal Entry", "dn": "JE-1", "method": "get_pdf"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Journal Entry.get_pdf", str(ctx.exception))

    def test_run_doc_method_get_pdf_allowed_for_the_granted_doctype(self):
        # The other half of the flip: granting the per-doctype "<DocType>.get_pdf" shape now works.
        scoped_grant = FakeScopeDoc(method_patterns=["Journal Entry.get_pdf"])
        enforce.frappe = self._fake(
            scoped_grant, "/api/method/run_doc_method", "POST",
            {"dt": "Journal Entry", "dn": "JE-1", "method": "get_pdf"},
        )
        self.assertIsNone(enforce.check_scope())

    def test_savedocs_submit_denied_for_a_different_doctype(self):
        enforce.frappe = self._fake(
            self.SI_SUBMIT_ONLY, "/api/method/frappe.desk.form.save.savedocs", "POST",
            {"doc": '{"doctype": "Journal Entry", "name": "JE-1"}', "action": "Submit"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Journal Entry.submit", str(ctx.exception))

    def test_savedocs_submit_allowed_for_the_granted_doctype(self):
        enforce.frappe = self._fake(
            self.SI_SUBMIT_ONLY, "/api/method/frappe.desk.form.save.savedocs", "POST",
            {"doc": '{"doctype": "Sales Invoice", "name": "SINV-1"}', "action": "Submit"},
        )
        self.assertIsNone(enforce.check_scope())

    def test_savedocs_draft_save_now_denied_bare_by_deny_unknown(self):
        # FLIPPED (deny-unknown, §2): action=Save never rewrites (body_scoped_target still returns
        # None) -- but the un-rewritten bare "frappe.desk.form.save.savedocs" target is itself
        # unresolved and not on SAFE_METHODS, so it is now denied on the bare-name grant alone (the
        # same side-effect closure as frappe.client.save above).
        savedocs_grant = FakeScopeDoc(method_patterns=["frappe.desk.form.save.savedocs"])
        enforce.frappe = self._fake(
            savedocs_grant, "/api/method/frappe.desk.form.save.savedocs", "POST",
            {"doc": '{"doctype": "Journal Entry", "name": "JE-1"}', "action": "Save"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("out of scope", enforce.frappe.denial_logs[0][0])

    def test_savedocs_unknown_action_fails_closed_at_the_entrypoint(self):
        enforce.frappe = self._fake(
            self.SI_SUBMIT_ONLY, "/api/method/frappe.desk.form.save.savedocs", "POST",
            {"doc": '{"doctype": "Sales Invoice", "name": "SINV-1"}', "action": "Discard"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("out of scope", enforce.frappe.denial_logs[0][0])


class TestUrlPathBehaviorUnchangedByBodyDoctypeRewrite(_Base):
    """Regression: the URL-path run_method vector (SI/PI/PE's guard-scopeable submit shape) must
    behave EXACTLY as before -- body_scoped_target only fires for the enumerated bare RPC names, and
    an already-doctype-qualified target ("Sales Invoice.submit") is not one of them."""

    def _fake(self, scope_doc, path, method, run_method):
        return FakeFrappe(headers=AUTH, session_user="alice@x", scopes={"alice@x": scope_doc},
                          request=FakeReq(path, method), form_dict={"run_method": run_method})

    def test_sales_invoice_submit_via_run_method_still_allowed(self):
        doc = FakeScopeDoc(method_patterns=["Sales Invoice.submit"])
        enforce.frappe = self._fake(doc, "/api/resource/Sales Invoice/SINV-1", "POST", "submit")
        self.assertIsNone(enforce.check_scope())

    def test_purchase_invoice_cancel_via_run_method_still_allowed(self):
        doc = FakeScopeDoc(method_patterns=["Purchase Invoice.cancel"])
        enforce.frappe = self._fake(doc, "/api/resource/Purchase Invoice/PINV-1", "POST", "cancel")
        self.assertIsNone(enforce.check_scope())

    def test_payment_entry_submit_via_run_method_still_denied_for_wrong_doctype(self):
        doc = FakeScopeDoc(method_patterns=["Sales Invoice.submit"])
        enforce.frappe = self._fake(doc, "/api/resource/Payment Entry/PE-1", "POST", "submit")
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()


class TestFormDictExtractionRobustness(_Base):
    """End-to-end proof (through the full ``check_scope()`` entrypoint, not just the pure core in
    ``test_scope.py``) that a malformed/hostile ``frappe.form_dict`` value can no longer crash the
    auth hook. ``check_scope()`` runs with NO try/except around it (module docstring, ``enforce.py``
    top) — a ``frappe.PermissionError`` becomes a clean 403, but any OTHER uncaught exception (the
    ``AttributeError``/``TypeError`` these values used to raise inside ``scope.py`` before this fix)
    would propagate raw out of the hook instead. These pin the FIXED behavior: a clean, audited
    ``FakePermissionError`` denial, same as any other out-of-scope call — never a crash.
    """

    def _fake(self, scope_doc, path, method, form_dict):
        return FakeFrappe(headers=AUTH, session_user="alice@x", scopes={"alice@x": scope_doc},
                          request=FakeReq(path, method), form_dict=form_dict)

    def test_non_string_cmd_denies_cleanly_instead_of_crashing_the_hook(self):
        enforce.frappe = self._fake(
            PING_ONLY, "/api/method/frappe.ping", "POST", {"cmd": ["frappe.client.delete_doc"]},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("out of scope", enforce.frappe.denial_logs[0][0])
        self.assertIn("other", str(ctx.exception))

    def test_non_string_bulk_action_denies_cleanly_instead_of_crashing_the_hook(self):
        bulk_grant = FakeScopeDoc(method_patterns=["Journal Entry.submit"])
        enforce.frappe = self._fake(
            bulk_grant,
            "/api/method/frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs",
            "POST",
            {"doctype": "Journal Entry", "docnames": '["JE-1"]', "action": ["submit"]},
        )
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()
        self.assertIn("out of scope", enforce.frappe.denial_logs[0][0])

    def test_case_varied_bulk_update_doctype_is_still_hard_denied_through_the_hook(self):
        # The Bulk Update 2-hop laundering vector, end-to-end: a credential broad enough to
        # otherwise reach anything ("*") still cannot ride a case-varied "bulk update" through the
        # real chokepoint -- the hard-deny fires before the grant is even consulted.
        broad_grant = FakeScopeDoc(method_patterns=["*"])
        enforce.frappe = self._fake(
            broad_grant, "/api/method/run_doc_method", "POST",
            {"dt": "bulk update", "dn": "some-name", "method": "bulk_update"},
        )
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()
        self.assertIn("out of scope", enforce.frappe.denial_logs[0][0])


class TestContainKillSwitch(_Base):
    """CONTAIN: `enabled` unticked = every request from the credential denied at the chokepoint,
    instantly (the grant is read per request — no restart, no cache to wait out)."""

    def test_disabled_grant_denies_even_an_allowlisted_call(self):
        killed = FakeScopeDoc(method_patterns=["frappe.ping"], enabled=0)
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="alice@x",
                                    scopes={"alice@x": killed},
                                    request=FakeReq("/api/method/frappe.ping", "GET"))
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("disabled", str(ctx.exception))
        title, _ = enforce.frappe.denial_logs[0]
        self.assertIn("kill switch", title)

    def test_kill_fires_before_the_request_is_even_read(self):
        # A killed credential is denied unconditionally — no request object needed, no classify.
        killed = FakeScopeDoc(method_patterns=["frappe.ping"], enabled=0)
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="alice@x",
                                    scopes={"alice@x": killed}, request=None)
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()

    def test_enabled_grant_behaves_normally(self):
        # A bare /api/method/<name> call needs a SAFE_METHODS name to pass deny-unknown resolution
        # (this test is about the kill switch, not method scoping).
        live = FakeScopeDoc(method_patterns=["frappe.auth.get_logged_user"], enabled=1)
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="alice@x",
                                    scopes={"alice@x": live},
                                    request=FakeReq("/api/method/frappe.auth.get_logged_user", "GET"))
        self.assertIsNone(enforce.check_scope())

    def test_pre_contain_doc_without_the_field_keeps_working(self):
        # Backward compat: a doc from before `bench migrate` added the columns has NEITHER
        # attribute (bare FakeScopeDoc). Absence is not a kill; in-scope passes, out-of-scope 403s.
        legacy = FakeScopeDoc(method_patterns=["frappe.auth.get_logged_user"])
        self.assertFalse(hasattr(legacy, "enabled"))
        self.assertFalse(hasattr(legacy, "rate_limit_per_minute"))
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="alice@x",
                                    scopes={"alice@x": legacy},
                                    request=FakeReq("/api/method/frappe.auth.get_logged_user", "GET"))
        self.assertIsNone(enforce.check_scope())
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="alice@x",
                                    scopes={"alice@x": legacy},
                                    request=FakeReq("/api/method/evil.wipe", "POST"))
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()


class TestContainRate(_Base):
    """CONTAIN: the per-credential speed limit. Counts EVERY request the scoped credential makes
    (velocity, not success rate); over the limit = 403 naming the limit; cache failure while a
    limit is set fails CLOSED for that credential."""

    # Default path/grant is a SAFE_METHODS name -- these tests are about rate mechanics, not method
    # scoping, and a bare /api/method/<name> call now needs deny-unknown resolution to pass at all.
    def _fake(self, doc, path="/api/method/frappe.auth.get_logged_user", cache=None):
        return FakeFrappe(headers=AUTH, session_user="alice@x", scopes={"alice@x": doc},
                          request=FakeReq(path, "GET"), cache=cache)

    def test_limit_allows_n_then_denies_and_names_the_limit(self):
        doc = FakeScopeDoc(method_patterns=["frappe.auth.get_logged_user"], rate_limit_per_minute=2)
        enforce.frappe = self._fake(doc)
        self.assertIsNone(enforce.check_scope())
        self.assertIsNone(enforce.check_scope())
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("rate limit", str(ctx.exception))
        self.assertIn("2 requests per minute", str(ctx.exception))
        title, _ = enforce.frappe.denial_logs[0]
        self.assertIn("rate limit", title)

    def test_denied_calls_burn_budget_too(self):
        # Rate is checked BEFORE scope, so an out-of-scope hammer also exhausts the window —
        # total request velocity is what's contained.
        doc = FakeScopeDoc(method_patterns=["frappe.auth.get_logged_user"], rate_limit_per_minute=1)
        enforce.frappe = self._fake(doc, path="/api/method/evil.wipe")
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()  # denied out-of-scope; still counted (count=1, within limit)
        enforce.frappe.local.request = FakeReq("/api/method/frappe.auth.get_logged_user", "GET")
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()  # in-scope, but the window is spent
        self.assertIn("rate limit", str(ctx.exception))

    def test_no_limit_never_touches_the_cache(self):
        # Backward compat + zero overhead: limit absent/0 -> the counter is never consulted,
        # proven by handing over a cache that would explode on first touch.
        for doc in (FakeScopeDoc(method_patterns=["frappe.auth.get_logged_user"]),
                    FakeScopeDoc(method_patterns=["frappe.auth.get_logged_user"], rate_limit_per_minute=0)):
            enforce.frappe = self._fake(doc, cache=ExplodingCache())
            self.assertIsNone(enforce.check_scope())

    def test_cache_failure_with_a_limit_set_fails_closed(self):
        doc = FakeScopeDoc(method_patterns=["frappe.auth.get_logged_user"], rate_limit_per_minute=100)
        enforce.frappe = self._fake(doc, cache=ExplodingCache())
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("failing closed", str(ctx.exception))

    def test_counter_window_expiry_is_set(self):
        doc = FakeScopeDoc(method_patterns=["frappe.auth.get_logged_user"], rate_limit_per_minute=5)
        enforce.frappe = self._fake(doc)
        self.assertIsNone(enforce.check_scope())
        cache = enforce.frappe._cache
        self.assertEqual(len(cache.counts), 1)
        (key,) = cache.counts
        self.assertIn("alice@x", key)
        self.assertEqual(cache.expires[key], 120)


class TestDenyStillFiresWhenLoggingExplodes(_Base):
    """The audit row is fail-open; the deny is not. An exception inside frappe.log_error must
    never suppress the PermissionError."""

    def test_out_of_scope_deny_survives_a_broken_logger(self):
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="alice@x",
                                    scopes={"alice@x": PING_ONLY}, log_raises=True,
                                    request=FakeReq("/api/method/evil.wipe", "POST"))
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()

    def test_kill_switch_deny_survives_a_broken_logger(self):
        killed = FakeScopeDoc(method_patterns=["frappe.ping"], enabled=0)
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="alice@x",
                                    scopes={"alice@x": killed}, log_raises=True,
                                    request=FakeReq("/api/method/frappe.ping", "GET"))
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()

    def test_rate_deny_survives_a_broken_logger(self):
        doc = FakeScopeDoc(method_patterns=["frappe.auth.get_logged_user"], rate_limit_per_minute=1)
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="alice@x",
                                    scopes={"alice@x": doc}, log_raises=True,
                                    request=FakeReq("/api/method/frappe.auth.get_logged_user", "GET"))
        self.assertIsNone(enforce.check_scope())
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()


class TestWorkflowEnforcement(_Base):
    """The opt-in ``enforce_workflow`` gate: mirrors ``TestCheckScopeEntrypoint``'s shape (a fully
    wired ``check_scope()`` call through the fake), but exercising the NEW gate that sits after
    the existing ``is_permitted`` check. Off by default; on, it refuses a docstatus-changing call
    against a workflow-governed doctype unless the call IS ``apply_workflow``.

    KNOWLEDGE-PINNED, NOT LIVE-VERIFIED: these prove the wiring (flag -> shape check -> internal
    workflow lookup -> deny/pass), not that real Frappe's ``frappe.model.workflow.get_workflow_name``
    actually behaves as modelled here, nor that a raw JSON PUT body really surfaces a ``docstatus``
    key through ``frappe.form_dict`` the way this fake's ``form_dict`` does. Both are stated as
    open questions for a future live bench gate — see the guard CHANGELOG/README.
    """

    def _fake(self, session_user, scope_doc, path, method, form_dict=None,
              workflows=None, workflow_error_doctypes=()):
        return FakeFrappe(
            headers=AUTH, session_user=session_user, scopes={session_user: scope_doc},
            request=FakeReq(path, method), form_dict=form_dict or {},
            workflows=workflows, workflow_error_doctypes=workflow_error_doctypes,
        )

    def test_workflow_doctype_submit_flag_on_is_denied(self):
        doc = FakeScopeDoc(method_patterns=["Sales Invoice.submit"], enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/resource/Sales Invoice/SINV-1", "POST",
            form_dict={"run_method": "submit"},
            workflows={"Sales Invoice": "Invoice Approval"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Invoice Approval", str(ctx.exception))
        title, message = enforce.frappe.denial_logs[0]
        self.assertIn("workflow bypass", title)
        self.assertIn("Sales Invoice", message)
        self.assertIn("Invoice Approval", message)
        self.assertIn("apply_workflow", message)

    def test_same_call_with_flag_off_passes(self):
        doc = FakeScopeDoc(method_patterns=["Sales Invoice.submit"], enforce_workflow=0)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/resource/Sales Invoice/SINV-1", "POST",
            form_dict={"run_method": "submit"},
            workflows={"Sales Invoice": "Invoice Approval"},
        )
        self.assertIsNone(enforce.check_scope())

    def test_non_workflow_doctype_submit_passes_even_with_flag_on(self):
        # The doctype is allowlisted and permitted, but carries no active Workflow at all --
        # rule 3 in the design: no workflow configured = this gate passes.
        doc = FakeScopeDoc(method_patterns=["ToDo.submit"], enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/resource/ToDo/TD-1", "POST",
            form_dict={"run_method": "submit"},
            workflows={},  # no doctype has an active workflow
        )
        self.assertIsNone(enforce.check_scope())

    def test_apply_workflow_on_a_workflow_doctype_passes(self):
        # FLIPPED (deny-unknown, §5): apply_workflow is now resolved per-doctype (nested `doc` param,
        # the same shape the broker sends -- {"doc": {...}, "action": ..}), not a bare-name grant --
        # the credential must hold "<DocType>.apply_workflow", not the generic method name.
        doc = FakeScopeDoc(method_patterns=["Sales Invoice.apply_workflow"], enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/method/frappe.model.workflow.apply_workflow", "POST",
            form_dict={"doc": {"doctype": "Sales Invoice", "name": "SINV-1"}, "action": "Approve"},
            workflows={"Sales Invoice": "Invoice Approval"},
        )
        self.assertIsNone(enforce.check_scope())

    def test_apply_workflow_bare_grant_no_longer_bypasses(self):
        # The bare method-name grant (pre-flip shape) no longer suffices -- proves the flip actually
        # closes the doctype-blind apply_workflow hole, not just documents the new happy path.
        bare_grant = FakeScopeDoc(method_patterns=["frappe.model.workflow.apply_workflow"],
                                  enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", bare_grant, "/api/method/frappe.model.workflow.apply_workflow", "POST",
            form_dict={"doc": {"doctype": "Sales Invoice", "name": "SINV-1"}, "action": "Approve"},
            workflows={"Sales Invoice": "Invoice Approval"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("out of scope", enforce.frappe.denial_logs[0][0])
        self.assertIn("Sales Invoice.apply_workflow", str(ctx.exception))

    def test_apply_workflow_denied_for_an_ungranted_doctype(self):
        doc = FakeScopeDoc(method_patterns=["Sales Invoice.apply_workflow"], enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/method/frappe.model.workflow.apply_workflow", "POST",
            form_dict={"doc": {"doctype": "Journal Entry", "name": "JE-1"}, "action": "Approve"},
            workflows={"Journal Entry": "JE Approval"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Journal Entry.apply_workflow", str(ctx.exception))

    def test_raw_put_with_docstatus_to_a_workflow_doctype_is_denied(self):
        doc = FakeScopeDoc(allow_resource=True, resource_doctypes=["Sales Invoice"],
                           verb_read=1, verb_create=1, verb_write=1, verb_delete=1,
                           enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/resource/Sales Invoice/SINV-1", "PUT",
            form_dict={"docstatus": 1},
            workflows={"Sales Invoice": "Invoice Approval"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Sales Invoice", str(ctx.exception))

    def test_workflow_lookup_error_on_docstatus_changing_call_is_denied(self):
        # Deny-biased: the internal lookup itself raised. An unverifiable answer is never read as
        # "no workflow" for a call that otherwise looks docstatus-changing.
        doc = FakeScopeDoc(method_patterns=["Sales Invoice.submit"], enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/resource/Sales Invoice/SINV-1", "POST",
            form_dict={"run_method": "submit"},
            workflow_error_doctypes={"Sales Invoice"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("workflow", str(ctx.exception).lower())

    def test_workflow_gate_fires_for_a_body_doctype_submit(self):
        # REGRESSION (guard-bypass redteam HIGH, 2026-07-06): the gate ran on classify()'s PRE-rewrite
        # target ("frappe.client.submit" -> doctype "frappe.client" -> no workflow -> silent no-op),
        # so Journal Entry — which submits EXCLUSIVELY via frappe.client.submit — had zero workflow
        # protection. The gate now judges the body-rewritten target, so a workflow-governed JE submit
        # via frappe.client.submit is caught the same as the URL-path shape.
        doc = FakeScopeDoc(method_patterns=["Journal Entry.submit"], enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/method/frappe.client.submit", "POST",
            form_dict={"doc": '{"doctype": "Journal Entry", "name": "JE-1"}'},
            workflows={"Journal Entry": "JE Approval"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("JE Approval", str(ctx.exception))
        title, message = enforce.frappe.denial_logs[0]
        self.assertIn("workflow bypass", title)
        self.assertIn("Journal Entry", message)

    def test_workflow_gate_fires_for_a_bulk_submit(self):
        # Companion: the bulk submit RPC (the CRITICAL) is also workflow-gated via its rewrite.
        doc = FakeScopeDoc(method_patterns=["Journal Entry.submit"], enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc,
            "/api/method/frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs",
            "POST",
            form_dict={"doctype": "Journal Entry", "docnames": '["JE-1"]', "action": "submit"},
            workflows={"Journal Entry": "JE Approval"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("JE Approval", str(ctx.exception))

    def test_pre_migration_doc_without_enforce_workflow_attr_behaves_as_off(self):
        # A doc from before `bench migrate` added the column has NO enforce_workflow attribute at
        # all (bare FakeScopeDoc) -- absence is not an opt-in; a submit to a workflow-governed
        # doctype that the credential is otherwise permitted to call still passes.
        legacy = FakeScopeDoc(method_patterns=["Sales Invoice.submit"])
        self.assertFalse(hasattr(legacy, "enforce_workflow"))
        enforce.frappe = self._fake(
            "alice@x", legacy, "/api/resource/Sales Invoice/SINV-1", "POST",
            form_dict={"run_method": "submit"},
            workflows={"Sales Invoice": "Invoice Approval"},
        )
        self.assertIsNone(enforce.check_scope())

    def test_non_docstatus_call_with_flag_on_is_untouched(self):
        # A permitted, non-docstatus-changing call on a workflow-governed doctype never even
        # consults the workflow lookup path -- proven by handing over error_doctypes covering it.
        doc = FakeScopeDoc(allow_resource=True, resource_doctypes=["Sales Invoice"],
                           verb_read=1, verb_create=1, verb_write=1, verb_delete=1,
                           enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/resource/Sales Invoice/SINV-1", "GET",
            workflows={"Sales Invoice": "Invoice Approval"},
            workflow_error_doctypes={"Sales Invoice"},  # would raise if ever consulted
        )
        self.assertIsNone(enforce.check_scope())

    def test_empty_string_workflow_name_reads_as_no_workflow_passes(self):
        # frappe's get_workflow_name returns "" (not None) for a cached "no active workflow" —
        # both falsy shapes must read as "no workflow" and pass, not deny.
        doc = FakeScopeDoc(method_patterns=["Sales Invoice.submit"], enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/resource/Sales Invoice/SINV-1", "POST",
            form_dict={"run_method": "submit"},
            workflows={"Sales Invoice": ""},  # cached "no workflow" shape
        )
        self.assertIsNone(enforce.check_scope())

    def test_savedocs_submit_on_a_workflow_doctype_is_denied(self):
        # The Desk UI submit path (redteam CRITICAL, 2026-07-03): its method name is "savedocs",
        # not "*.submit", and the doctype is in the `doc` body param -- must still be caught.
        # NOTE: since the body-doctype scoping fix, a savedocs Submit is scope-enforced as
        # "Sales Invoice.submit" (not the bare "savedocs" name) -- the credential must hold BOTH
        # patterns to reach this (later) workflow gate at all; see TestBodyDoctypeScoping below for
        # the scope-gate's own new behavior on this exact vector.
        doc = FakeScopeDoc(method_patterns=["frappe.desk.form.save.savedocs", "Sales Invoice.submit"],
                           enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/method/frappe.desk.form.save.savedocs", "POST",
            form_dict={"doc": '{"doctype": "Sales Invoice", "name": "SINV-1"}', "action": "Submit"},
            workflows={"Sales Invoice": "Invoice Approval"},
        )
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        self.assertIn("Invoice Approval", str(ctx.exception))
        self.assertIn("workflow bypass", enforce.frappe.denial_logs[0][0])

    def test_savedocs_draft_save_now_denied_bare_before_reaching_the_workflow_gate(self):
        # FLIPPED (deny-unknown, §2): action=Save is a plain draft (docstatus 0), never workflow-
        # gated -- that part is unchanged. But the bare "frappe.desk.form.save.savedocs" grant this
        # test held is now ITSELF unresolved-and-not-SAFE_METHODS, so the scope gate denies it
        # before the (separately correct) workflow gate is ever reached -- proven by the denial
        # reason being "out of scope", not "workflow bypass".
        doc = FakeScopeDoc(method_patterns=["frappe.desk.form.save.savedocs"], enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/method/frappe.desk.form.save.savedocs", "POST",
            form_dict={"doc": '{"doctype": "Sales Invoice", "name": "SINV-1"}', "action": "Save"},
            workflows={"Sales Invoice": "Invoice Approval"},
        )
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()
        self.assertIn("out of scope", enforce.frappe.denial_logs[0][0])

    def test_post_create_as_submitted_on_a_workflow_doctype_is_denied(self):
        # Insert-as-submitted (redteam MEDIUM): POST create carrying docstatus 1.
        doc = FakeScopeDoc(allow_resource=True, resource_doctypes=["Sales Invoice"],
                           verb_read=1, verb_create=1, verb_write=1, verb_delete=1,
                           enforce_workflow=1)
        enforce.frappe = self._fake(
            "alice@x", doc, "/api/resource/Sales Invoice", "POST",
            form_dict={"docstatus": 1, "customer": "ACME"},
            workflows={"Sales Invoice": "Invoice Approval"},
        )
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()
        self.assertIn("workflow bypass", enforce.frappe.denial_logs[0][0])


if __name__ == "__main__":
    unittest.main()
