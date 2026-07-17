# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Bench-free unit tests for the pure decision core (guard.scope).

Run: `python3 -m unittest guard.tests.test_scope` from the app root. No frappe required.
"""
import base64
import unittest

from pacioli_guard.scope import (
    APPLY_WORKFLOW_METHOD,
    SAFE_METHODS,
    ApiScope,
    api_key_from_auth_header,
    body_scoped_target,
    classify,
    docstatus_target_doctype,
    is_docstatus_changing,
    is_permitted,
    is_rate_allowed,
    method_target_resolved,
)

METHOD_ONLY = ApiScope.from_dict(
    {"methods": ["frappe.auth.get_logged_user", "erpnext.selling.*"], "allow_resource": False}
)
RESOURCE_ALLOWLIST = ApiScope.from_dict(
    {"methods": [], "allow_resource": True, "resource_doctypes": ["ToDo", "Note"]}
)
RESOURCE_ALL = ApiScope.from_dict({"allow_resource": True})


class TestClassify(unittest.TestCase):
    def test_method_path(self):
        self.assertEqual(
            classify("/api/method/frappe.auth.get_logged_user", "GET"),
            ("method", "frappe.auth.get_logged_user"),
        )

    def test_resource_create(self):
        self.assertEqual(classify("/api/resource/ToDo", "POST"), ("resource", ("ToDo", "create")))

    def test_resource_read_url_encoded_doctype(self):
        self.assertEqual(
            classify("/api/resource/Sales%20Invoice/SINV-001", "GET"),
            ("resource", ("Sales Invoice", "read")),
        )

    def test_resource_verbs(self):
        self.assertEqual(classify("/api/resource/ToDo/x", "PUT")[1][1], "write")
        self.assertEqual(classify("/api/resource/ToDo/x", "DELETE")[1][1], "delete")

    def test_run_method_is_a_method_call(self):
        self.assertEqual(
            classify("/api/resource/Sales%20Order/SO-1", "POST", run_method="submit"),
            ("method", "Sales Order.submit"),
        )

    def test_unrecognised_path(self):
        self.assertEqual(classify("/app/todo", "GET"), ("other", None))


class TestClassifyVersioned(unittest.TestCase):
    """Frappe mounts v1 rules at both /api and /api/v1, and a separate v2 surface at /api/v2 that
    uses /document/ (not /resource/) and puts the doc-method name in the PATH. Scoped creds must be
    able to use these, deny-by-default must hold, and unknown shapes must fail CLOSED."""

    # v1 alias == bare /api
    def test_v1_method_alias(self):
        self.assertEqual(
            classify("/api/v1/method/frappe.auth.get_logged_user", "GET"),
            ("method", "frappe.auth.get_logged_user"),
        )

    def test_v1_resource_alias(self):
        self.assertEqual(classify("/api/v1/resource/ToDo", "POST"), ("resource", ("ToDo", "create")))

    def test_v1_run_method_alias(self):
        self.assertEqual(
            classify("/api/v1/resource/Sales%20Order/SO-1", "POST", run_method="submit"),
            ("method", "Sales Order.submit"),
        )

    # v2 method (RPC)
    def test_v2_method(self):
        self.assertEqual(
            classify("/api/v2/method/frappe.auth.get_logged_user", "GET"),
            ("method", "frappe.auth.get_logged_user"),
        )

    def test_v2_controller_method_two_segments(self):
        self.assertEqual(classify("/api/v2/method/ToDo/get_count", "GET"), ("method", "ToDo.get_count"))

    # v2 /document == v1 /resource
    def test_v2_document_list_and_create(self):
        self.assertEqual(classify("/api/v2/document/ToDo", "GET"), ("resource", ("ToDo", "read")))
        self.assertEqual(classify("/api/v2/document/ToDo", "POST"), ("resource", ("ToDo", "create")))

    def test_v2_document_write_and_delete(self):
        self.assertEqual(classify("/api/v2/document/ToDo/abc/", "PATCH"), ("resource", ("ToDo", "write")))
        self.assertEqual(classify("/api/v2/document/ToDo/abc/", "DELETE"), ("resource", ("ToDo", "delete")))

    def test_v2_document_url_encoded(self):
        self.assertEqual(
            classify("/api/v2/document/Sales%20Invoice/SINV-1/", "GET"),
            ("resource", ("Sales Invoice", "read")),
        )

    # v2 doc-method: method name is IN THE PATH -> must be classified as a METHOD call
    def test_v2_doc_method_in_path(self):
        self.assertEqual(
            classify("/api/v2/document/Sales%20Order/SO-1/method/submit/", "POST"),
            ("method", "Sales Order.submit"),
        )

    # CRITICAL regression: frappe <path:name> matches slash-bearing document names (naming series
    # like ACC/2024/00001). A doc-method on such a doc MUST stay a method call, not resource CRUD.
    def test_v2_doc_method_with_slash_in_name(self):
        self.assertEqual(
            classify("/api/v2/document/ToDo/foo/bar/method/submit/", "POST"),
            ("method", "ToDo.submit"),
        )

    def test_v2_doc_method_slash_name_denied_by_resource_only_scope(self):
        res_only = ApiScope.from_dict({"allow_resource": True, "resource_doctypes": ["ToDo"]})
        kind, target = classify("/api/v2/document/ToDo/foo/bar/method/submit/", "POST")
        self.assertEqual(kind, "method")
        self.assertFalse(is_permitted(res_only, kind, target))

    # HIGH: a v1 POST to an ITEM url is always execute_doc_method in frappe (never create). If the
    # method name isn't visible (run_method not forwarded), fail CLOSED instead of mislabeling create.
    def test_v1_item_post_without_run_method_fails_closed(self):
        self.assertEqual(classify("/api/resource/ToDo/abc", "POST"), ("other", None))
        self.assertEqual(classify("/api/resource/ToDo/abc/", "POST"), ("other", None))

    def test_v1_collection_post_is_still_create(self):
        self.assertEqual(classify("/api/resource/ToDo", "POST"), ("resource", ("ToDo", "create")))

    # unknown / exotic shapes fail CLOSED
    def test_v2_meta_is_other(self):
        self.assertEqual(classify("/api/v2/doctype/ToDo/meta", "GET"), ("other", None))

    def test_unknown_version_is_other(self):
        self.assertEqual(classify("/api/v3/method/x", "GET"), ("other", None))

    # security properties across the v2 surface
    def test_v2_resource_denied_for_method_only_scope(self):
        method_only = ApiScope.from_dict(
            {"methods": ["frappe.auth.get_logged_user"], "allow_resource": False}
        )
        kind, target = classify("/api/v2/document/ToDo", "POST")
        self.assertFalse(is_permitted(method_only, kind, target))

    def test_v2_doc_method_needs_method_grant_not_resource(self):
        # A resource grant on the doctype must NOT let a v2 doc-method (submit) through.
        res_only = ApiScope.from_dict({"allow_resource": True, "resource_doctypes": ["Sales Order"]})
        kind, target = classify("/api/v2/document/Sales Order/SO-1/method/submit/", "POST")
        self.assertEqual(kind, "method")
        self.assertFalse(is_permitted(res_only, kind, target))


class TestBareMethodPathPercentEncodingAsymmetry(unittest.TestCase):
    """form_dict/path extraction seam, PINNED-NOT-FIXED: three near-identical path-segment
    extractions in this module handle percent-encoding INCONSISTENTLY. ``/api/resource/<doctype>``
    and ``/api/v2/document/<doctype>`` segments are run through ``urllib.parse.unquote`` before
    becoming the classified target; the v2 ``/api/v2/method/<name>`` segment is ALSO unquoted; but
    the v1/bare ``/api/method/<name>`` segment (``_classify_full``'s own ``"/api/method/"`` branch)
    is NOT -- it is read raw off the path with no ``unquote()`` call at all.

    NEEDS BENCH PIN, not asserted here: which layer decodes what before this code (or frappe's own
    routing) ever sees ``req.path`` is a WSGI/Werkzeug question this pure module can't answer, and
    getting it wrong in either direction is possible -- adding ``unquote()`` where frappe does NOT
    itself decode would be a no-op; adding it where frappe's Werkzeug rule has ALREADY decoded once
    would DOUBLE-decode and diverge the other way. Unlike the ``cmd``/``action`` crash fixes and the
    ``Bulk Update`` case fix elsewhere in this residual (each safe to make unconditionally, since
    they can only ever deny MORE), this one cannot be resolved without exercising a live bench, so
    it is pinned as a known, disclosed, still-open asymmetry rather than guessed at.
    """

    def test_v1_bare_method_path_is_read_raw_not_percent_decoded(self):
        self.assertEqual(
            classify("/api/method/frappe.client.delete%5Fdoc", "POST"),
            ("method", "frappe.client.delete%5Fdoc"),
        )

    def test_v2_single_segment_method_path_is_percent_decoded(self):
        # The v2 twin of the case above DOES unquote -- the asymmetry is internal to this module,
        # not something frappe forced onto only one of the two branches.
        self.assertEqual(
            classify("/api/v2/method/frappe.client.delete%5Fdoc", "POST"),
            ("method", "frappe.client.delete_doc"),
        )


class TestIsPermitted(unittest.TestCase):
    def test_unscoped_allows_everything(self):
        self.assertTrue(is_permitted(None, "resource", ("ToDo", "create")))
        self.assertTrue(is_permitted(None, "other", None))

    def test_allowlisted_method_passes(self):
        self.assertTrue(is_permitted(METHOD_ONLY, "method", "frappe.auth.get_logged_user"))

    def test_glob_method_passes(self):
        # Testing glob-pattern matching itself, isolated from deny-unknown resolution (fed True as
        # a resolved per-doctype-style call would be).
        self.assertTrue(is_permitted(METHOD_ONLY, "method", "erpnext.selling.doctype.sales_order.x",
                                     method_resolved=True))

    def test_offlist_method_denied(self):
        self.assertFalse(is_permitted(METHOD_ONLY, "method", "frappe.client.get_list"))

    def test_resource_denied_when_not_allowed(self):
        # The bypass this whole app closes: a method-only credential cannot hit raw CRUD.
        self.assertFalse(is_permitted(METHOD_ONLY, "resource", ("ToDo", "create")))

    def test_resource_doctype_allowlist(self):
        self.assertTrue(is_permitted(RESOURCE_ALLOWLIST, "resource", ("ToDo", "create")))
        self.assertFalse(is_permitted(RESOURCE_ALLOWLIST, "resource", ("User", "create")))

    def test_resource_all_when_allowlist_empty(self):
        self.assertTrue(is_permitted(RESOURCE_ALL, "resource", ("Anything", "read")))

    def test_other_fails_closed_when_scoped(self):
        self.assertFalse(is_permitted(METHOD_ONLY, "other", None))

    def test_empty_method_target_denied(self):
        self.assertFalse(is_permitted(METHOD_ONLY, "method", ""))


class TestResourceVerbNarrowing(unittest.TestCase):
    """Per-credential resource-verb scoping: a DocType allowlist alone admitted every verb; this
    lets a credential be locked to e.g. read-only across its granted DocTypes."""

    def _read_only(self):
        return ApiScope.from_dict({"allow_resource": True, "resource_doctypes": ["Sales Invoice"],
                                   "resource_verbs": ["read"]})

    def test_read_only_credential_can_read(self):
        self.assertTrue(is_permitted(self._read_only(), "resource", ("Sales Invoice", "read")))

    def test_read_only_credential_cannot_write_create_or_delete(self):
        s = self._read_only()
        for verb in ("create", "write", "delete"):
            self.assertFalse(is_permitted(s, "resource", ("Sales Invoice", verb)),
                             f"{verb} must be denied for a read-only credential")

    def test_unspecified_verbs_means_all_verbs_backward_compatible(self):
        # A grant that names a DocType without narrowing verbs admits every verb, exactly as before.
        s = ApiScope.from_dict({"allow_resource": True, "resource_doctypes": ["ToDo"]})
        self.assertIsNone(s.resource_verbs)  # unspecified, not an empty set
        for verb in ("read", "create", "write", "delete"):
            self.assertTrue(is_permitted(s, "resource", ("ToDo", verb)))

    def test_explicitly_empty_verbs_denies_all_resource_verbs(self):
        # An operator who unticks all four boxes means "no resource verb" — NOT full access.
        s = ApiScope.from_dict({"allow_resource": True, "resource_doctypes": ["ToDo"],
                                "resource_verbs": []})
        self.assertEqual(s.resource_verbs, frozenset())
        for verb in ("read", "create", "write", "delete"):
            self.assertFalse(is_permitted(s, "resource", ("ToDo", verb)))

    def test_read_create_credential_the_broker_shape(self):
        # The broker's own posture: read + create (amend inserts a draft), no write/delete.
        s = ApiScope.from_dict({"allow_resource": True, "resource_doctypes": ["Sales Invoice"],
                                "resource_verbs": ["read", "create"]})
        self.assertTrue(is_permitted(s, "resource", ("Sales Invoice", "read")))
        self.assertTrue(is_permitted(s, "resource", ("Sales Invoice", "create")))
        self.assertFalse(is_permitted(s, "resource", ("Sales Invoice", "delete")))

    def test_verbs_narrow_even_the_all_doctypes_grant(self):
        s = ApiScope.from_dict({"allow_resource": True, "resource_verbs": ["read"]})  # no doctype list
        self.assertTrue(is_permitted(s, "resource", ("Anything", "read")))
        self.assertFalse(is_permitted(s, "resource", ("Anything", "delete")))

    def test_unknown_verbs_are_dropped_not_trusted(self):
        s = ApiScope.from_dict({"allow_resource": True, "resource_verbs": ["read", "RAED", "", 5]})
        self.assertEqual(s.resource_verbs, frozenset({"read"}))

    def test_case_and_whitespace_normalised(self):
        s = ApiScope.from_dict({"allow_resource": True, "resource_verbs": [" Read ", "DELETE"]})
        self.assertEqual(s.resource_verbs, frozenset({"read", "delete"}))


class TestFromDict(unittest.TestCase):
    def test_rejects_non_mapping(self):
        self.assertIsNone(ApiScope.from_dict("nope"))
        self.assertIsNone(ApiScope.from_dict(None))
        self.assertIsNone(ApiScope.from_dict([1, 2]))

    def test_defaults_are_deny(self):
        s = ApiScope.from_dict({})
        self.assertFalse(s.allow_resource)
        self.assertEqual(s.methods, frozenset())
        self.assertEqual(s.resource_doctypes, frozenset())


class TestFromGrant(unittest.TestCase):
    """`from_grant` is the pure seam the 'API Key Scope' DocType glue feeds: the structured
    fields (allow_resource + method/doctype allowlists) map to the same scope as `from_dict`,
    so the security-critical shaping stays out from behind the frappe wall."""

    def test_equivalent_to_from_dict(self):
        got = ApiScope.from_grant(
            True, ["frappe.auth.get_logged_user", "erpnext.selling.*"], ["ToDo", "Note"]
        )
        want = ApiScope.from_dict(
            {
                "allow_resource": True,
                "methods": ["frappe.auth.get_logged_user", "erpnext.selling.*"],
                "resource_doctypes": ["ToDo", "Note"],
            }
        )
        self.assertEqual(got, want)

    def test_defaults_are_deny(self):
        s = ApiScope.from_grant(None, None, None)
        self.assertFalse(s.allow_resource)
        self.assertEqual(s.methods, frozenset())
        self.assertEqual(s.resource_doctypes, frozenset())

    def test_allow_resource_coerced_from_check_int(self):
        # Frappe Check fields come back as 0/1 ints, not bools; deny-by-default rides on bool().
        self.assertIs(ApiScope.from_grant(1, [], []).allow_resource, True)
        self.assertIs(ApiScope.from_grant(0, [], []).allow_resource, False)


class TestAuthHeaderParsing(unittest.TestCase):
    """The credential is the SAME whether sent as ``token key:secret`` or ``Basic b64(key:secret)`` —
    Frappe authenticates both. Recognising only ``token`` was a scope bypass (a scoped key sent as
    Basic auth read as unscoped → full access)."""

    def test_token_scheme(self):
        self.assertEqual(api_key_from_auth_header("token abc123:secretxyz"), "abc123")

    def test_basic_scheme_decodes_base64(self):
        hdr = "Basic " + base64.b64encode(b"abc123:secretxyz").decode()
        self.assertEqual(api_key_from_auth_header(hdr), "abc123")

    def test_scheme_is_case_insensitive(self):
        hdr = "bAsIc " + base64.b64encode(b"k:s").decode()
        self.assertEqual(api_key_from_auth_header(hdr), "k")

    def test_absent_header(self):
        self.assertIsNone(api_key_from_auth_header(""))
        self.assertIsNone(api_key_from_auth_header(None))

    def test_unknown_scheme_rejected(self):
        self.assertIsNone(api_key_from_auth_header("Bearer xyz"))

    def test_basic_not_valid_base64(self):
        self.assertIsNone(api_key_from_auth_header("Basic @@@notbase64@@@"))

    def test_basic_without_colon(self):
        hdr = "Basic " + base64.b64encode(b"nocolon").decode()
        self.assertIsNone(api_key_from_auth_header(hdr))

    def test_empty_key_rejected(self):
        self.assertIsNone(api_key_from_auth_header("token :secret"))


class TestFlagCoercion(unittest.TestCase):
    """``allow_resource`` is a deny-by-default security flag. A legacy JSON blob may carry it as a
    string; ``bool("0")``/``bool("false")`` are both True in Python, which would silently GRANT
    resource CRUD. Coercion must be strict and deny-biased."""

    def test_string_and_falsy_shapes_deny(self):
        for v in ("0", "false", "False", "no", "off", "", None, 0, False):
            self.assertIs(
                ApiScope.from_dict({"allow_resource": v}).allow_resource, False, f"{v!r} should deny"
            )

    def test_truthy_shapes_allow(self):
        for v in (1, True, "1", "true", "TRUE", "yes", "on"):
            self.assertIs(
                ApiScope.from_dict({"allow_resource": v}).allow_resource, True, f"{v!r} should allow"
            )


class TestNullAndBlankFiltering(unittest.TestCase):
    """A child row with a NULL/blank ``pattern`` or ``ref_doctype`` (reachable despite reqd:1, which
    is app-level only) must never crash enforcement or flip a scope's meaning."""

    def test_none_and_blank_patterns_dropped(self):
        s = ApiScope.from_grant(False, ["a.b", None, "", "   "], [])
        self.assertEqual(s.methods, frozenset({"a.b"}))

    def test_none_pattern_does_not_crash_is_permitted(self):
        # any() must exhaust the set; a None pattern used to blow up fnmatchcase(target, None).
        s = ApiScope.from_grant(False, ["a.b", None], [])
        self.assertFalse(is_permitted(s, "method", "c.d"))

    def test_is_permitted_guards_nonstr_pattern_defense_in_depth(self):
        # Even a directly-constructed scope carrying a non-str pattern must not raise. Fed
        # method_resolved=True -- this test is about the non-str-pattern defense, not deny-unknown.
        s = ApiScope(methods=frozenset({None, "a.b"}))
        self.assertFalse(is_permitted(s, "method", "c.d", method_resolved=True))
        self.assertTrue(is_permitted(s, "method", "a.b", method_resolved=True))

    def test_blank_only_resource_doctypes_means_allow_all_not_deny_all(self):
        # frozenset({None}) is non-empty → would skip "empty allowlist = all" and deny everything.
        s = ApiScope.from_grant(True, [], [None, "", "   "])
        self.assertEqual(s.resource_doctypes, frozenset())
        self.assertTrue(is_permitted(s, "resource", ("ToDo", "read")))


class TestRolesProbeGrantContract(unittest.TestCase):
    """Locks the cross-package contract the broker's ``doctor.probe_roles`` depends on: the
    seat's-own-roles read (``GET /api/v2/method/User/get_roles``) is admissible with a CONFIG-ONLY
    ``User.get_roles`` methods-grant and NO guard code change. If a future guard change breaks the
    v2 two-segment resolution or the deny-unknown gate, this test fails LOUD next to the probe's
    remedy text rather than silently 403-ing every doctor run. (Characterization/regression lock —
    the guard already behaves this way; nothing here changes guard code.)"""

    def test_v2_get_roles_classifies_doctype_resolved(self):
        self.assertEqual(classify("/api/v2/method/User/get_roles", "GET"),
                         ("method", "User.get_roles"))
        self.assertTrue(method_target_resolved("/api/v2/method/User/get_roles", "GET"))

    def test_config_grant_admits_the_resolved_call(self):
        scope = ApiScope.from_dict({"methods": ["User.get_roles"], "allow_resource": False})
        self.assertTrue(is_permitted(scope, "method", "User.get_roles", method_resolved=True))

    def test_without_the_grant_the_call_is_denied(self):
        scope = ApiScope.from_dict({"methods": ["frappe.auth.get_logged_user"],
                                    "allow_resource": False})
        self.assertFalse(is_permitted(scope, "method", "User.get_roles", method_resolved=True))

    def test_the_bare_dotted_route_needs_a_guard_release_not_a_config_grant(self):
        # Scout C's dead-end, locked: the bare /api/method/<dotted> route is unresolved and
        # get_roles is not a SAFE_METHOD, so even a matching grant is denied — proving the v2 route
        # (this increment's choice) is the config-only path, and the bare route would require a
        # reviewed SAFE_METHODS addition (a guard code release).
        bare = "frappe.core.doctype.user.user.get_roles"
        scope = ApiScope.from_dict({"methods": [bare], "allow_resource": False})
        self.assertNotIn(bare, SAFE_METHODS)
        self.assertFalse(is_permitted(scope, "method", bare, method_resolved=False))


if __name__ == "__main__":
    unittest.main()


class TestCmdLegacyRpcBypass(unittest.TestCase):
    """Frappe's app.py routes on ?cmd= BEFORE path routing (handler.handle → execute_cmd), so a
    request's real target is the cmd, not the URL path. Classifying by path alone let any credential
    with one allowed method smuggle an arbitrary whitelisted call via ?cmd=. cmd dominates."""

    def test_cmd_present_classifies_as_that_method(self):
        self.assertEqual(
            classify("/api/method/frappe.auth.get_logged_user", "POST",
                     cmd="frappe.client.delete_doc"),
            ("method", "frappe.client.delete_doc"),
        )

    def test_cmd_bypass_denied_for_method_only_scope(self):
        # allowlisted path, but the real dispatch target (cmd) is not on the allowlist
        kind, target = classify("/api/method/frappe.auth.get_logged_user", "POST",
                                cmd="frappe.client.delete_doc")
        self.assertFalse(is_permitted(METHOD_ONLY, kind, target))

    def test_cmd_on_its_allowlist_is_permitted(self):
        kind, target = classify("/api/method/x", "POST", cmd="frappe.auth.get_logged_user")
        self.assertTrue(is_permitted(METHOD_ONLY, kind, target))

    def test_empty_cmd_ignored(self):
        self.assertEqual(classify("/api/resource/ToDo", "POST", cmd=""),
                         ("resource", ("ToDo", "create")))

    def test_cmd_non_string_truthy_denies_instead_of_crashing(self):
        # form_dict extraction seam: a JSON body can carry `"cmd": [...]` / `{...}` / a bare
        # int/bool -- frappe pre-parses a JSON body into form_dict verbatim, so cmd is NOT
        # guaranteed to be a string by the time it reaches this pure function. The OLD code
        # (`if cmd and cmd.strip():`) called `.strip()` unconditionally on a truthy cmd and raised
        # AttributeError for every one of these -- an uncaught exception inside classify(), whose
        # only caller (enforce.py's check_scope(), run from a frappe auth_hook with NO try/except
        # around it) would then propagate it raw. What frappe's own execute_cmd() would actually do
        # with a non-string form_dict.cmd is NOT verified here (needs bench pin) -- but this pure
        # function must never crash on attacker-shaped input, so a non-string truthy cmd is treated
        # as unresolvable and denied, matching the deny-biased-on-ambiguity posture used everywhere
        # else in this module (e.g. `_run_doc_method_doctype`).
        for bad_cmd in (["a", "b"], {"a": 1}, 5, 5.5, True):
            with self.subTest(cmd=bad_cmd):
                self.assertEqual(
                    classify("/api/method/frappe.auth.get_logged_user", "POST", cmd=bad_cmd),
                    ("other", None),
                )

    def test_cmd_falsy_non_string_still_falls_through_to_path(self):
        # The falsy shapes (0, [], {}, False) never reached `.strip()` even in the OLD code --
        # `if cmd` short-circuits before the crash-prone call. Pinned here so the new
        # isinstance-first branch doesn't regress them from "as absent as cmd=None" into a hard
        # deny: a falsy cmd carries no signal either way.
        for falsy_cmd in (0, [], {}, False):
            with self.subTest(cmd=falsy_cmd):
                self.assertEqual(
                    classify("/api/resource/ToDo", "POST", cmd=falsy_cmd),
                    ("resource", ("ToDo", "create")),
                )


class TestRunMethodMisclassification(unittest.TestCase):
    """Frappe honors ?run_method only on an ITEM url (read_doc GET / execute_doc_method POST).
    On a COLLECTION, or on item PUT/PATCH/DELETE, frappe IGNORES run_method and does real CRUD —
    so classifying those as a method reopened the raw-CRUD bypass."""

    def test_collection_post_with_run_method_is_still_create(self):
        # frappe: create_doc, ignores run_method → must classify as resource create, not method
        self.assertEqual(classify("/api/resource/Sales Order", "POST", run_method="x"),
                         ("resource", ("Sales Order", "create")))

    def test_collection_get_with_run_method_is_still_list(self):
        self.assertEqual(classify("/api/resource/Sales Order", "GET", run_method="x"),
                         ("resource", ("Sales Order", "read")))

    def test_item_put_with_run_method_is_still_write(self):
        self.assertEqual(classify("/api/resource/Sales Order/SO-1", "PUT", run_method="x"),
                         ("resource", ("Sales Order", "write")))

    def test_item_delete_with_run_method_is_still_delete(self):
        self.assertEqual(classify("/api/resource/Sales Order/SO-1", "DELETE", run_method="x"),
                         ("resource", ("Sales Order", "delete")))

    def test_method_only_scope_cannot_create_via_run_method(self):
        scope = ApiScope.from_dict({"methods": ["Sales Order.*"], "allow_resource": False})
        for method in ("POST",):  # collection create
            kind, target = classify("/api/resource/Sales Order", method, run_method="x")
            self.assertFalse(is_permitted(scope, kind, target),
                             f"{method} collection+run_method must not be a permitted method")
        for method in ("PUT", "DELETE"):  # item write/delete
            kind, target = classify("/api/resource/Sales Order/SO-1", method, run_method="x")
            self.assertFalse(is_permitted(scope, kind, target),
                             f"{method} item+run_method must not be a permitted method")

    def test_item_get_with_run_method_is_honored(self):
        # read_doc DOES honor run_method — this is the legitimate doc-method-over-GET path
        self.assertEqual(classify("/api/resource/Sales Invoice/SI-1", "GET", run_method="get_x"),
                         ("method", "Sales Invoice.get_x"))

    def test_item_post_with_run_method_is_honored(self):
        # execute_doc_method — the submit path the broker relies on
        self.assertEqual(classify("/api/resource/Sales Invoice/SI-1", "POST", run_method="submit"),
                         ("method", "Sales Invoice.submit"))

    def test_run_method_list_value_does_not_crash_and_fails_an_exact_grant(self):
        # form_dict extraction seam: a duplicated query-string key (?run_method=submit&run_method=x)
        # or a hostile JSON-array value can hand classify() a list instead of a string. Unlike cmd,
        # run_method only ever flows into an f-string (`f"{doctype}.{run_method}"`), which accepts
        # any type without raising -- so this does NOT crash. Pinned as existing (accidentally-safe)
        # behavior, not a fix: the garbled target string ("Sales Order.['submit', 'evil']") cannot
        # match an EXACT grant pattern, so a credential scoped to one exact method is still denied
        # regardless of which element (if any) frappe's own dispatcher would actually pick.
        kind, target = classify("/api/resource/Sales Order/SO-1", "POST",
                                run_method=["submit", "evil"])
        self.assertEqual(kind, "method")
        scope = ApiScope.from_dict({"methods": ["Sales Order.submit"]})
        self.assertFalse(is_permitted(scope, kind, target, method_resolved=True))


class TestKillSwitch(unittest.TestCase):
    """CONTAIN: ``enabled`` is the kill switch. Off = every request denied, allowlists ignored.
    The bias flips at the right edge: the field's ABSENCE (a pre-CONTAIN grant, or a dict that
    never carried the key) is NOT a kill — but any *present* value coerces deny-biased, so an
    explicit 0/"false" (and even an ambiguous "") kills."""

    def test_default_is_enabled(self):
        self.assertTrue(ApiScope().enabled)
        self.assertTrue(ApiScope.from_dict({}).enabled)

    def test_absent_field_is_not_a_kill(self):
        # A legacy grant written before `enabled` existed keeps working — backward compatible.
        # method_resolved=True: this test is about the enabled-absence, not deny-unknown resolution.
        s = ApiScope.from_dict({"methods": ["frappe.ping"]})
        self.assertTrue(s.enabled)
        self.assertTrue(is_permitted(s, "method", "frappe.ping", method_resolved=True))
        s2 = ApiScope.from_grant(True, ["frappe.ping"], [], enabled=None)
        self.assertTrue(s2.enabled)
        self.assertTrue(is_permitted(s2, "resource", ("ToDo", "read")))

    def test_disabled_denies_everything_even_allowlisted(self):
        s = ApiScope.from_dict(
            {"methods": ["frappe.ping"], "allow_resource": True, "enabled": 0}
        )
        self.assertFalse(s.enabled)
        self.assertFalse(is_permitted(s, "method", "frappe.ping"))
        self.assertFalse(is_permitted(s, "resource", ("ToDo", "read")))
        self.assertFalse(is_permitted(s, "other", None))

    def test_check_int_and_string_shapes(self):
        # Frappe Check comes back 0/1; a legacy JSON blob may carry strings. Deny-biased when present.
        for v in (0, False, "0", "false", "off", ""):
            self.assertFalse(ApiScope.from_grant(False, [], [], enabled=v).enabled, f"{v!r}")
        for v in (1, True, "1", "true", "yes", "on"):
            self.assertTrue(ApiScope.from_grant(False, [], [], enabled=v).enabled, f"{v!r}")

    def test_unscoped_credential_is_unaffected(self):
        # The kill switch lives ON a grant; no grant = stock frappe behaviour, untouched.
        self.assertTrue(is_permitted(None, "method", "anything"))


class TestRateMath(unittest.TestCase):
    """CONTAIN: the pure per-window rate decision. ``current_count`` INCLUDES this request
    (the post-increment counter), so limit N passes exactly N requests per window."""

    def test_zero_and_absent_mean_no_limit(self):
        self.assertTrue(is_rate_allowed(0, 10_000_000))
        self.assertTrue(is_rate_allowed(None, 10_000_000))

    def test_negative_and_garbage_mean_no_limit(self):
        self.assertTrue(is_rate_allowed(-5, 10_000_000))
        self.assertTrue(is_rate_allowed("garbage", 10_000_000))

    def test_nth_passes_n_plus_first_denied(self):
        self.assertTrue(is_rate_allowed(5, 4))
        self.assertTrue(is_rate_allowed(5, 5))
        self.assertFalse(is_rate_allowed(5, 6))

    def test_limit_of_one(self):
        self.assertTrue(is_rate_allowed(1, 1))
        self.assertFalse(is_rate_allowed(1, 2))

    def test_string_limit_coerces_numerically(self):
        self.assertTrue(is_rate_allowed("60", 60))
        self.assertFalse(is_rate_allowed("60", 61))

    def test_grant_coercion_backward_compatible(self):
        # A pre-CONTAIN doc has no field -> None -> 0 -> the glue never even touches the cache.
        self.assertEqual(ApiScope.from_grant(False, [], []).rate_limit_per_minute, 0)
        self.assertEqual(
            ApiScope.from_grant(False, [], [], rate_limit_per_minute=None).rate_limit_per_minute, 0
        )
        self.assertEqual(
            ApiScope.from_grant(False, [], [], rate_limit_per_minute=60).rate_limit_per_minute, 60
        )


class TestEnforceWorkflowFlag(unittest.TestCase):
    """``enforce_workflow`` — the opt-in gate flag on ``ApiScope``. Off by default and backward
    compatible with a pre-migration grant, exactly like ``enabled``/``rate_limit_per_minute``/
    ``resource_verbs`` before it (a NEW gate can newly DENY previously-passing calls the moment
    it's on, so a site-wide or default-on flip would break live credentials on upgrade)."""

    def test_default_is_off(self):
        self.assertFalse(ApiScope().enforce_workflow)
        self.assertFalse(ApiScope.from_dict({}).enforce_workflow)

    def test_absent_field_is_off_not_a_crash(self):
        # A legacy/pre-migration grant that never carried the key reads as off.
        s = ApiScope.from_dict({"methods": ["frappe.ping"]})
        self.assertFalse(s.enforce_workflow)
        s2 = ApiScope.from_grant(True, ["frappe.ping"], [], enforce_workflow=None)
        self.assertFalse(s2.enforce_workflow)

    def test_check_int_and_string_shapes(self):
        # Frappe Check comes back 0/1; a legacy JSON blob may carry strings. Deny-biased shapes
        # (falsy strings/values) read off; truthy shapes read on -- same coercion as allow_resource.
        for v in (0, False, "0", "false", "off", "", None):
            self.assertFalse(
                ApiScope.from_grant(False, [], [], enforce_workflow=v).enforce_workflow, f"{v!r}"
            )
        for v in (1, True, "1", "true", "yes", "on"):
            self.assertTrue(
                ApiScope.from_grant(False, [], [], enforce_workflow=v).enforce_workflow, f"{v!r}"
            )

    def test_from_dict_string_shapes(self):
        self.assertTrue(ApiScope.from_dict({"enforce_workflow": "1"}).enforce_workflow)
        self.assertFalse(ApiScope.from_dict({"enforce_workflow": "0"}).enforce_workflow)


class TestIsDocstatusChanging(unittest.TestCase):
    """Pure vector-detection for the ``enforce_workflow`` gate: does this classified call look
    like an attempt to move a document's docstatus outside apply_workflow?"""

    def test_submit_method_suffix(self):
        self.assertTrue(is_docstatus_changing("method", "Sales Invoice.submit", "POST", {}))

    def test_cancel_method_suffix(self):
        self.assertTrue(is_docstatus_changing("method", "Sales Order.cancel", "POST", {}))

    def test_non_docstatus_method_is_false(self):
        self.assertFalse(is_docstatus_changing("method", "Sales Invoice.get_pdf", "GET", {}))

    def test_apply_workflow_itself_is_never_flagged(self):
        # The sanctioned path -- never treated as a bypass attempt, regardless of shape.
        self.assertFalse(is_docstatus_changing("method", APPLY_WORKFLOW_METHOD, "POST",
                                               {"doctype": "Sales Invoice", "action": "Approve"}))

    def test_raw_put_with_docstatus_key_is_true(self):
        self.assertTrue(is_docstatus_changing("resource", ("Sales Invoice", "write"), "PUT",
                                              {"docstatus": 1}))

    def test_raw_patch_with_docstatus_key_is_true(self):
        self.assertTrue(is_docstatus_changing("resource", ("Sales Invoice", "write"), "PATCH",
                                              {"docstatus": 1}))

    def test_get_with_docstatus_key_is_false(self):
        # Wrong verb -- GET can't write anyway; not a bypass shape.
        self.assertFalse(is_docstatus_changing("resource", ("Sales Invoice", "read"), "GET",
                                               {"docstatus": 1}))

    def test_post_create_with_submitting_docstatus_is_true(self):
        # Insert-as-submitted: a POST create carrying docstatus 1/2 IS a bypass shape (covered
        # 2026-07-03 after a redteam flagged the omission). Presence isn't the signal here (unlike
        # PUT) -- only a SUBMITTING value is, because a draft legitimately posts docstatus 0.
        self.assertTrue(is_docstatus_changing("resource", ("Sales Invoice", "create"), "POST",
                                              {"docstatus": 1}))
        self.assertTrue(is_docstatus_changing("resource", ("Sales Invoice", "create"), "POST",
                                              {"docstatus": "2"}))

    def test_post_create_draft_docstatus_is_false(self):
        # A plain draft create (docstatus 0, or absent) is legitimate -- must NOT be flagged.
        self.assertFalse(is_docstatus_changing("resource", ("Sales Invoice", "create"), "POST",
                                               {"docstatus": 0}))
        self.assertFalse(is_docstatus_changing("resource", ("Sales Invoice", "create"), "POST",
                                               {"customer": "ACME"}))

    def test_post_create_unhashable_docstatus_is_deny_biased_true_not_a_crash(self):
        # A list/dict docstatus (duplicate form keys, crafted JSON) is never a normal draft --
        # an unreadable docstatus in a create is treated as a docstatus move (deny-biased),
        # never a TypeError out of the frozenset membership test.
        for garbage in ([1], ["1"], {"v": 1}, [0]):
            with self.subTest(docstatus=garbage):
                self.assertTrue(is_docstatus_changing(
                    "resource", ("Sales Invoice", "create"), "POST", {"docstatus": garbage}))

    def test_savedocs_submit_action_is_true(self):
        # The Desk UI Save/Submit/Cancel endpoint: docstatus is driven by `action`, not the method
        # name -- so the suffix check misses it. Submit/Cancel/Update flag; only Save (draft) passes.
        for action in ("Submit", "Cancel", "Update"):
            self.assertTrue(is_docstatus_changing(
                "method", "frappe.desk.form.save.savedocs", "POST",
                {"doc": '{"doctype": "Sales Invoice", "name": "SINV-1"}', "action": action}), action)

    def test_savedocs_save_action_is_false(self):
        self.assertFalse(is_docstatus_changing(
            "method", "frappe.desk.form.save.savedocs", "POST",
            {"doc": '{"doctype": "Sales Invoice"}', "action": "Save"}))

    def test_savedocs_missing_action_is_deny_biased_true(self):
        self.assertTrue(is_docstatus_changing(
            "method", "frappe.desk.form.save.savedocs", "POST",
            {"doc": '{"doctype": "Sales Invoice"}'}))

    def test_put_without_docstatus_key_is_false(self):
        self.assertFalse(is_docstatus_changing("resource", ("Sales Invoice", "write"), "PUT",
                                               {"customer": "ACME"}))

    def test_other_kind_is_always_false(self):
        self.assertFalse(is_docstatus_changing("other", None, "POST", {"docstatus": 1}))

    def test_empty_method_target_is_false(self):
        self.assertFalse(is_docstatus_changing("method", "", "POST", {}))

    def test_bare_method_name_no_dot_still_matches_suffix(self):
        # No DocType prefix at all -- still matches the shape (doctype extraction, tested below,
        # is what keeps this from ever being treated as a real workflow-governed doctype).
        self.assertTrue(is_docstatus_changing("method", "submit", "POST", {}))

    def test_generic_rpc_named_like_docstatus_method_still_matches_shape(self):
        # frappe.client.submit (the generic-RPC footgun, documented as a residual) matches the
        # SHAPE too -- it's the doctype lookup downstream (not this function) that makes it a
        # no-op, since "frappe.client" is never a real workflow-governed doctype.
        self.assertTrue(is_docstatus_changing("method", "frappe.client.submit", "POST",
                                              {"doctype": "Sales Invoice", "name": "SINV-1"}))


class TestDocstatusTargetDoctype(unittest.TestCase):
    """Pure doctype extraction from a classified ``(kind, target)`` pair -- feeds the guard's
    Workflow-existence lookup."""

    def test_method_target_splits_on_last_dot(self):
        self.assertEqual(docstatus_target_doctype("method", "Sales Invoice.submit"), "Sales Invoice")

    def test_method_target_preserves_spaces(self):
        self.assertEqual(docstatus_target_doctype("method", "Sales Order.cancel"), "Sales Order")

    def test_resource_target_is_first_element(self):
        self.assertEqual(
            docstatus_target_doctype("resource", ("Sales Invoice", "write")), "Sales Invoice"
        )

    def test_method_target_without_dot_is_none(self):
        self.assertIsNone(docstatus_target_doctype("method", "submit"))

    def test_generic_rpc_extracts_the_module_path_not_a_real_doctype(self):
        # Demonstrates the self-limiting behavior TestIsDocstatusChanging documents: the
        # "doctype" extracted for a generic RPC is garbage, never a real DocType name.
        self.assertEqual(docstatus_target_doctype("method", "frappe.client.submit"), "frappe.client")

    def test_other_kind_is_none(self):
        self.assertIsNone(docstatus_target_doctype("other", None))

    def test_empty_resource_target_is_none(self):
        self.assertIsNone(docstatus_target_doctype("resource", ()))

    def test_savedocs_extracts_doctype_from_doc_param(self):
        # savedocs carries the doctype in the `doc` body param (JSON string), not the method name.
        self.assertEqual(
            docstatus_target_doctype("method", "frappe.desk.form.save.savedocs",
                                     {"doc": '{"doctype": "Sales Invoice", "name": "SINV-1"}'}),
            "Sales Invoice")

    def test_savedocs_accepts_already_parsed_doc_dict(self):
        self.assertEqual(
            docstatus_target_doctype("method", "frappe.desk.form.save.savedocs",
                                     {"doc": {"doctype": "Purchase Invoice"}}),
            "Purchase Invoice")

    def test_savedocs_unparseable_doc_is_none_deny_biased(self):
        # Garbage / missing doc -> None -> the glue treats it as a failed lookup (deny), never
        # as "no workflow".
        self.assertIsNone(docstatus_target_doctype("method", "frappe.desk.form.save.savedocs",
                                                   {"doc": "not-json"}))
        self.assertIsNone(docstatus_target_doctype("method", "frappe.desk.form.save.savedocs", {}))


class TestWorkflowBypassVectorsThroughClassify(unittest.TestCase):
    """Demonstrates the four vectors named in the build brief all reduce to a shape
    ``is_docstatus_changing`` catches -- because ``classify`` (already tested exhaustively above)
    funnels every routing mechanism into the same ``(kind, target)`` pair, this pure function
    covers all four for free, with no per-vector special-casing."""

    def test_v1_run_method_submit(self):
        kind, target = classify("/api/resource/Sales Invoice/SINV-1", "POST", run_method="submit")
        self.assertTrue(is_docstatus_changing(kind, target, "POST", {"run_method": "submit"}))

    def test_v1_run_method_cancel(self):
        kind, target = classify("/api/resource/Sales Invoice/SINV-1", "POST", run_method="cancel")
        self.assertTrue(is_docstatus_changing(kind, target, "POST", {"run_method": "cancel"}))

    def test_v2_path_doc_method_submit(self):
        kind, target = classify("/api/v2/document/Sales Invoice/SINV-1/method/submit/", "POST")
        self.assertTrue(is_docstatus_changing(kind, target, "POST", {}))

    def test_legacy_cmd_naming_a_submit_shaped_target(self):
        kind, target = classify("/api/method/frappe.auth.get_logged_user", "POST",
                                cmd="Sales Invoice.submit")
        self.assertTrue(is_docstatus_changing(kind, target, "POST", {"cmd": "Sales Invoice.submit"}))

    def test_raw_put_docstatus_body(self):
        kind, target = classify("/api/resource/Sales Invoice/SINV-1", "PUT")
        self.assertTrue(is_docstatus_changing(kind, target, "PUT", {"docstatus": 1}))

    def test_apply_workflow_call_is_the_allowlisted_exception(self):
        kind, target = classify("/api/method/frappe.model.workflow.apply_workflow", "POST")
        self.assertEqual(target, APPLY_WORKFLOW_METHOD)
        self.assertFalse(is_docstatus_changing(kind, target, "POST",
                                               {"doctype": "Sales Invoice", "action": "Approve"}))


class TestBodyScopedTarget(unittest.TestCase):
    """``body_scoped_target`` closes the generic-RPC / body-doctype residual for submit/cancel ONLY:
    it resolves ``frappe.client.submit``/``.cancel``, ``run_doc_method`` (v1 ``dt``/``docs`` + v2
    ``document``), and Desk ``savedocs`` Submit/Update/Cancel to the SAME per-doctype
    ``("method", "<DocType>.submit"/"cancel")`` shape the URL-path ``run_method`` vector already
    produces -- so ``is_permitted``'s existing ``kind == "method"`` branch enforces it identically,
    with no widening of enforcement surface (no ``resource_doctypes`` involved).

    Every case here is fed a ``(kind, target)`` pair as ``classify()`` itself would already produce
    for that request (bare method name, no doctype) -- proving the rewrite composes with the
    existing classifier rather than replacing it.
    """

    # -- frappe.model.workflow.apply_workflow (§5: resolved per-doctype, NOT safe-listed) -----------

    def test_apply_workflow_resolves_per_doctype(self):
        self.assertEqual(
            body_scoped_target("method", APPLY_WORKFLOW_METHOD, "POST",
                               {"doc": {"doctype": "Sales Invoice", "name": "SINV-1"},
                                "action": "Approve"}),
            ("method", "Sales Invoice.apply_workflow"),
        )

    def test_apply_workflow_json_string_doc(self):
        self.assertEqual(
            body_scoped_target("method", APPLY_WORKFLOW_METHOD, "POST",
                               {"doc": '{"doctype": "Journal Entry", "name": "JE-1"}',
                                "action": "Approve"}),
            ("method", "Journal Entry.apply_workflow"),
        )

    def test_apply_workflow_missing_doc_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", APPLY_WORKFLOW_METHOD, "POST", {"action": "Approve"}),
            ("other", None),
        )

    def test_apply_workflow_unparseable_doc_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", APPLY_WORKFLOW_METHOD, "POST",
                               {"doc": "not-json", "action": "Approve"}),
            ("other", None),
        )

    # -- frappe.client.submit --------------------------------------------------------------------

    def test_client_submit_json_string_doc(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.submit", "POST",
                               {"doc": '{"doctype": "Sales Invoice", "name": "SINV-1"}'}),
            ("method", "Sales Invoice.submit"),
        )

    def test_client_submit_already_parsed_doc_dict(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.submit", "POST",
                               {"doc": {"doctype": "Journal Entry", "name": "JE-1"}}),
            ("method", "Journal Entry.submit"),
        )

    def test_client_submit_unparseable_doc_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.submit", "POST", {"doc": "not-json"}),
            ("other", None),
        )

    def test_client_submit_missing_doc_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.submit", "POST", {}),
            ("other", None),
        )

    # -- frappe.client.cancel (doctype is a plain sibling param, NOT nested in `doc`) -------------

    def test_client_cancel_plain_doctype_param(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.cancel", "POST",
                               {"doctype": "Journal Entry", "name": "JE-1"}),
            ("method", "Journal Entry.cancel"),
        )

    def test_client_cancel_missing_doctype_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.cancel", "POST", {"name": "JE-1"}),
            ("other", None),
        )

    def test_client_cancel_blank_doctype_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.cancel", "POST",
                               {"doctype": "   ", "name": "JE-1"}),
            ("other", None),
        )

    def test_client_cancel_ignores_a_doc_param_shape(self):
        # Confirms cancel does NOT read the submit/savedocs `doc`-nested shape -- its doctype is a
        # plain sibling param, never nested.
        self.assertEqual(
            body_scoped_target("method", "frappe.client.cancel", "POST",
                               {"doc": '{"doctype": "Sales Invoice"}', "name": "JE-1"}),
            ("other", None),
        )

    # -- savedocs (Submit/Update -> submit; Cancel -> cancel; Save -> untouched) -------------------

    def test_savedocs_submit_action_rewrites_to_submit(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.savedocs", "POST",
                               {"doc": '{"doctype": "Sales Invoice", "name": "SINV-1"}',
                                "action": "Submit"}),
            ("method", "Sales Invoice.submit"),
        )

    def test_savedocs_update_action_also_rewrites_to_submit(self):
        # Update keeps docstatus at Submitted (re-save of an already-submitted doc) -- same
        # permission requirement as Submit.
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.savedocs", "POST",
                               {"doc": '{"doctype": "Sales Invoice", "name": "SINV-1"}',
                                "action": "Update"}),
            ("method", "Sales Invoice.submit"),
        )

    def test_savedocs_cancel_action_rewrites_to_cancel(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.savedocs", "POST",
                               {"doc": '{"doctype": "Sales Invoice", "name": "SINV-1"}',
                                "action": "Cancel"}),
            ("method", "Sales Invoice.cancel"),
        )

    def test_savedocs_save_action_is_untouched(self):
        # A plain draft save is NOT submit/cancel -- out of this increment's scope. `None` means
        # the caller keeps enforcing the ORIGINAL classify() target (the savedocs method name
        # itself), unchanged.
        self.assertIsNone(
            body_scoped_target("method", "frappe.desk.form.save.savedocs", "POST",
                               {"doc": '{"doctype": "Sales Invoice"}', "action": "Save"})
        )

    def test_savedocs_missing_action_fails_closed(self):
        # Can't confirm this ISN'T a submit/cancel -- deny-biased, unlike the plain-Save case above.
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.savedocs", "POST",
                               {"doc": '{"doctype": "Sales Invoice"}'}),
            ("other", None),
        )

    def test_savedocs_unknown_action_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.savedocs", "POST",
                               {"doc": '{"doctype": "Sales Invoice"}', "action": "Discard"}),
            ("other", None),
        )

    def test_savedocs_action_non_string_denies_instead_of_crashing(self):
        # form_dict extraction seam: `action != _DRAFT_ACTION` (the draft check above) is safe for
        # any type, but the fallthrough `_SAVEDOCS_ACTION_VERB.get(action)` is a dict lookup -- an
        # UNHASHABLE action (a list/dict body glitch) raises TypeError there, not a lookup miss.
        # Also covers `frappe.desk.form.save.submit` (the bare alias, SAME branch -- see
        # test_save_submit_alias_* below). Deny-biased: fails closed like an unrecognised action
        # string, never crashes the hook.
        for bad_action in (["Submit"], {"Submit": True}):
            with self.subTest(action=bad_action):
                self.assertEqual(
                    body_scoped_target("method", "frappe.desk.form.save.savedocs", "POST",
                                       {"doc": '{"doctype": "Sales Invoice"}', "action": bad_action}),
                    ("other", None),
                )

    def test_savedocs_submit_with_unparseable_doc_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.savedocs", "POST",
                               {"doc": "not-json", "action": "Submit"}),
            ("other", None),
        )

    # -- run_doc_method (v1 dt/dn/docs + v2 document/method) ---------------------------------------

    def test_run_doc_method_v1_dt_present_submit(self):
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST",
                               {"dt": "Sales Invoice", "dn": "SINV-1", "method": "submit"}),
            ("method", "Sales Invoice.submit"),
        )

    def test_run_doc_method_v1_docs_fallback_when_dt_absent_cancel(self):
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST",
                               {"docs": '{"doctype": "Sales Invoice", "name": "SINV-1"}',
                                "method": "cancel"}),
            ("method", "Sales Invoice.cancel"),
        )

    def test_run_doc_method_v1_blank_dt_falls_through_to_docs(self):
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST",
                               {"dt": "  ", "docs": '{"doctype": "Journal Entry"}',
                                "method": "submit"}),
            ("method", "Journal Entry.submit"),
        )

    def test_run_doc_method_v2_document_param_submit(self):
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "GET",
                               {"document": {"doctype": "Journal Entry", "name": "JE-1"},
                                "method": "submit"}),
            ("method", "Journal Entry.submit"),
        )

    def test_run_doc_method_non_submit_cancel_method_now_resolves_per_doctype(self):
        # FLIPPED (deny-unknown, §6): EVERY inner method resolves to "<DocType>.<method>" now, not
        # just submit/cancel/discard -- run_doc_method is doctype-AND-method-blind by name alone.
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST",
                               {"dt": "Sales Invoice", "dn": "SINV-1", "method": "get_pdf"}),
            ("method", "Sales Invoice.get_pdf"),
        )

    def test_run_doc_method_missing_method_param_fails_closed(self):
        # FLIPPED (deny-unknown, §6): a missing/non-str method name can't be resolved at all, so it
        # now fails CLOSED rather than falling back to the doctype-blind bare name.
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST",
                               {"dt": "Sales Invoice", "dn": "SINV-1"}),
            ("other", None),
        )

    def test_run_doc_method_submit_with_unresolvable_doctype_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST", {"method": "submit"}),
            ("other", None),
        )

    # -- everything else is untouched (deliberately still-open residual) --------------------------

    def test_unrecognised_method_is_untouched(self):
        self.assertIsNone(body_scoped_target("method", "frappe.client.get_list", "GET", {}))

    def test_client_save_and_insert_stay_doctype_blind(self):
        # Explicitly out of THIS increment's scope -- documented residual, not silently folded in.
        self.assertIsNone(body_scoped_target("method", "frappe.client.save", "POST",
                                             {"doc": '{"doctype": "Sales Invoice"}'}))
        self.assertIsNone(body_scoped_target("method", "frappe.client.insert", "POST",
                                             {"doc": '{"doctype": "Sales Invoice"}'}))

    def test_resource_kind_is_untouched(self):
        self.assertIsNone(body_scoped_target("resource", ("Sales Invoice", "write"), "PUT",
                                             {"docstatus": 1}))

    def test_other_kind_is_untouched(self):
        self.assertIsNone(body_scoped_target("other", None, "POST", {}))

    def test_non_string_target_is_untouched(self):
        self.assertIsNone(body_scoped_target("method", None, "POST", {}))

    def test_non_dict_form_is_untouched(self):
        self.assertIsNone(body_scoped_target("method", "frappe.client.submit", "POST", None))

    def test_url_path_run_method_shape_never_reaches_this_function(self):
        # Sanity: the URL-path vector produces a target that ALREADY carries the doctype
        # ("Sales Invoice.submit"), which is not one of the recognised bare RPC names above, so
        # this function correctly leaves it alone (the caller never needed to rewrite it).
        kind, target = classify("/api/resource/Sales Invoice/SINV-1", "POST", run_method="submit")
        self.assertIsNone(body_scoped_target(kind, target, "POST", {"run_method": "submit"}))


class TestBodyScopedTargetRedteamGaps(unittest.TestCase):
    """REGRESSION (guard-bypass redteam, 2026-07-06): the first cut of body_scoped_target only
    recognised frappe.client.submit/.cancel, run_doc_method and savedocs — so two OTHER frappe RPCs
    that also call doc.submit()/doc.cancel() directly (and thus work against Journal Entry's
    unwhitelisted override, the exact shape this residual-close exists to make safe) fell back to
    the doctype-BLIND literal-method check: a CRITICAL bulk bypass and the Desk cancel endpoint.
    """

    # -- frappe.desk.form.save.cancel (the Desk UI's own cancel; plain sibling params) -----------

    def test_desk_cancel_plain_doctype_param(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.cancel", "POST",
                               {"doctype": "Journal Entry", "name": "JE-1"}),
            ("method", "Journal Entry.cancel"),
        )

    def test_desk_cancel_missing_doctype_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.cancel", "POST", {"name": "JE-1"}),
            ("other", None),
        )

    # -- frappe...bulk_update.submit_cancel_or_update_docs (the CRITICAL) -------------------------

    def test_bulk_submit_rewrites_per_doctype(self):
        self.assertEqual(
            body_scoped_target(
                "method",
                "frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs", "POST",
                {"doctype": "Journal Entry", "docnames": '["JE-1"]', "action": "submit"}),
            ("method", "Journal Entry.submit"),
        )

    def test_bulk_cancel_rewrites_per_doctype(self):
        self.assertEqual(
            body_scoped_target(
                "method",
                "frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs", "POST",
                {"doctype": "Sales Invoice", "docnames": '["SINV-1"]', "action": "cancel"}),
            ("method", "Sales Invoice.cancel"),
        )

    def test_bulk_update_action_fails_closed(self):
        # "update" is an arbitrary field write, not a submit/cancel — it must NOT ride a blind grant.
        self.assertEqual(
            body_scoped_target(
                "method",
                "frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs", "POST",
                {"doctype": "Journal Entry", "docnames": '["JE-1"]', "action": "update",
                 "data": '{"remark": "x"}'}),
            ("other", None),
        )

    def test_bulk_missing_or_unknown_action_fails_closed(self):
        for form in ({"doctype": "Journal Entry", "docnames": '["JE-1"]'},
                     {"doctype": "Journal Entry", "docnames": '["JE-1"]', "action": "delete"}):
            self.assertEqual(
                body_scoped_target(
                    "method",
                    "frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs",
                    "POST", form),
                ("other", None),
            )

    def test_bulk_submit_missing_doctype_fails_closed(self):
        self.assertEqual(
            body_scoped_target(
                "method",
                "frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs", "POST",
                {"docnames": '["JE-1"]', "action": "submit"}),
            ("other", None),
        )

    def test_bulk_action_non_string_denies_instead_of_crashing(self):
        # form_dict extraction seam: a hostile/malformed JSON body can carry `"action": [...]` --
        # `_BULK_ACTION_VERB.get(action)` is a dict lookup, and a dict.get() on an UNHASHABLE key
        # (a list/dict) raises TypeError, not a lookup miss. That TypeError is not caught anywhere
        # between here and enforce.py's check_scope() (no try/except around the auth_hook it runs
        # in). Deny-biased: a non-string action can't be resolved to a verb at all, so it must fail
        # closed exactly like an unrecognised action STRING already does (test_bulk_missing_or_
        # unknown_action_fails_closed above), not crash the hook.
        for bad_action in (["submit"], {"submit": True}):
            with self.subTest(action=bad_action):
                self.assertEqual(
                    body_scoped_target(
                        "method",
                        "frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs",
                        "POST",
                        {"doctype": "Journal Entry", "docnames": '["JE-1"]', "action": bad_action}),
                    ("other", None),
                )


class TestBodyScopedTargetCompletenessAudit(unittest.TestCase):
    """REGRESSION (completeness audit vs frappe 17 source, 2026-07-06): the allowlist-of-names
    approach missed the Desk Submit-button alias and — the deeper class — that frappe.client.save/
    .insert/.bulk_update submit or cancel a doc by carrying ``docstatus`` 1/2 in the body (via
    Document.save()'s docstatus-transition detection). This pins recognition of the docstatus-by-BODY
    class, not just method names.
    """

    # -- frappe.desk.form.save.submit (bare alias of savedocs, the real Desk Submit endpoint) ----

    def test_save_submit_alias_submit_action(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.submit", "POST",
                               {"doc": '{"doctype": "Journal Entry"}', "action": "Submit"}),
            ("method", "Journal Entry.submit"),
        )

    def test_save_submit_alias_missing_action_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.submit", "POST",
                               {"doc": '{"doctype": "Journal Entry"}'}),
            ("other", None),
        )

    # -- frappe.client.save / .insert with docstatus in the body (the docstatus-by-content class) -

    def test_client_save_with_docstatus_1_is_submit(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.save", "POST",
                               {"doc": {"doctype": "Journal Entry", "name": "JE-1", "docstatus": 1}}),
            ("method", "Journal Entry.submit"),
        )

    def test_client_save_with_docstatus_2_is_cancel(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.save", "POST",
                               {"doc": '{"doctype": "Sales Invoice", "docstatus": 2}'}),
            ("method", "Sales Invoice.cancel"),
        )

    def test_client_insert_with_docstatus_1_is_submit(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.insert", "POST",
                               {"doc": {"doctype": "Payment Entry", "docstatus": 1}}),
            ("method", "Payment Entry.submit"),
        )

    def test_client_save_draft_stays_create_residual(self):
        # docstatus 0 / absent → a draft create, NOT a submit/cancel → None (unchanged, doctype-blind
        # create residual — a separate axis this scoping does not claim to close).
        self.assertIsNone(
            body_scoped_target("method", "frappe.client.save", "POST",
                               {"doc": {"doctype": "Journal Entry", "docstatus": 0}}))
        self.assertIsNone(
            body_scoped_target("method", "frappe.client.save", "POST",
                               {"doc": {"doctype": "Journal Entry"}}))

    def test_client_save_unparseable_docstatus_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.save", "POST",
                               {"doc": {"doctype": "Journal Entry", "docstatus": "weird"}}),
            ("other", None),
        )

    def test_client_save_submit_missing_doctype_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.save", "POST",
                               {"doc": {"docstatus": 1}}),
            ("other", None),
        )

    # -- multi-doc save batches: any docstatus-changing item deny-closes -------------------------

    def test_bulk_update_client_with_a_submit_item_deny_closes(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.client.bulk_update", "POST",
                               {"docs": '[{"doctype": "Journal Entry", "docname": "JE-1", '
                                        '"docstatus": 1}]'}),
            ("other", None),
        )

    def test_insert_many_all_drafts_stays_create_residual(self):
        self.assertIsNone(
            body_scoped_target("method", "frappe.client.insert_many", "POST",
                               {"docs": [{"doctype": "ToDo"}, {"doctype": "Note", "docstatus": 0}]}))

    def test_cancel_all_linked_docs_always_deny_closes(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.linked_with.cancel_all_linked_docs",
                               "POST", {"docs": '[{"doctype": "Journal Entry", "name": "JE-1"}]'}),
            ("other", None),
        )

    # -- frappe.desk.form.save.discard (draft 0 -> cancelled 2; scoped as <DocType>.discard) ------

    def test_desk_discard_scopes_per_doctype(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.discard", "POST",
                               {"doctype": "Sales Invoice", "name": "SINV-1"}),
            ("method", "Sales Invoice.discard"),
        )

    def test_desk_discard_missing_doctype_fails_closed(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.desk.form.save.discard", "POST", {"name": "SINV-1"}),
            ("other", None),
        )

    # -- v2's SEPARATE top-level bulk_update (bare name, not frappe.client.bulk_update) -----------

    def test_v2_bare_bulk_update_with_submit_item_deny_closes(self):
        self.assertEqual(
            body_scoped_target("method", "bulk_update", "POST",
                               {"docs": '[{"doctype": "Journal Entry", "name": "JE-1", '
                                        '"docstatus": 1}]'}),
            ("other", None),
        )

    # -- run_doc_method: the dotted alias + method=discard ----------------------------------------

    def test_run_doc_method_dotted_alias_submit(self):
        self.assertEqual(
            body_scoped_target("method", "frappe.handler.run_doc_method", "POST",
                               {"dt": "Journal Entry", "dn": "JE-1", "method": "submit"}),
            ("method", "Journal Entry.submit"),
        )

    def test_run_doc_method_discard_scopes_per_doctype(self):
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST",
                               {"dt": "Sales Invoice", "dn": "SINV-1", "method": "discard"}),
            ("method", "Sales Invoice.discard"),
        )

    def test_run_doc_method_non_docstatus_method_now_scopes_per_doctype(self):
        # FLIPPED (deny-unknown, §6): a harmless controller method (e.g. get_pdf) now ALSO resolves
        # to "<DocType>.<method>" — no longer doctype-blind (the class of hole §6 closes).
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST",
                               {"dt": "Sales Invoice", "dn": "SINV-1", "method": "get_pdf"}),
            ("method", "Sales Invoice.get_pdf"),
        )

    def test_run_doc_method_non_str_method_fails_closed(self):
        # A non-str method value (e.g. a list/dict body glitch) can't be resolved -- deny-close.
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST",
                               {"dt": "Sales Invoice", "dn": "SINV-1", "method": ["get_pdf"]}),
            ("other", None),
        )


class TestClassifyProvenanceFixtureTable(unittest.TestCase):
    """§1 LOCKED: ``classify()`` (2-tuple) and ``method_target_resolved()`` (bool) are both thin
    wrappers over the SAME ``_classify_full`` computation -- never independently derived, and never
    inferred from the resulting target's string shape (``"Sales Invoice.submit"`` reaching a bare
    ``/api/method/`` path is syntactically identical to a genuine per-doctype call). This fixture
    table asserts BOTH together for every classify() branch, so the two can never silently drift."""

    # (label, args-kwargs, expected classify() 2-tuple, expected method_target_resolved() bool)
    CASES = [
        ("bare /api/method/<name>",
         dict(path="/api/method/frappe.auth.get_logged_user", http_method="GET"),
         ("method", "frappe.auth.get_logged_user"), False),
        ("legacy ?cmd=",
         dict(path="/api/method/x", http_method="POST", cmd="frappe.client.delete_doc"),
         ("method", "frappe.client.delete_doc"), False),
        ("v1 resource-item run_method (URL-resolved)",
         dict(path="/api/resource/Sales%20Order/SO-1", http_method="POST", run_method="submit"),
         ("method", "Sales Order.submit"), True),
        ("v2 single-segment /method/<name>",
         dict(path="/api/v2/method/frappe.auth.get_logged_user", http_method="GET"),
         ("method", "frappe.auth.get_logged_user"), False),
        ("v2 two-segment /method/<dt>/<method> (URL-resolved)",
         dict(path="/api/v2/method/ToDo/get_count", http_method="GET"),
         ("method", "ToDo.get_count"), True),
        ("v2 path-carried doc-method (URL-resolved)",
         dict(path="/api/v2/document/Sales%20Order/SO-1/method/submit/", http_method="POST"),
         ("method", "Sales Order.submit"), True),
        ("resource kind -- resolved is meaningless, must read False",
         dict(path="/api/resource/ToDo", http_method="POST"),
         ("resource", ("ToDo", "create")), False),
        ("other kind -- resolved is meaningless, must read False",
         dict(path="/app/todo", http_method="GET"),
         ("other", None), False),
    ]

    def test_classify_and_resolved_agree_for_every_branch(self):
        for label, kwargs, expected_classify, expected_resolved in self.CASES:
            with self.subTest(label):
                self.assertEqual(
                    classify(kwargs["path"], kwargs["http_method"],
                            kwargs.get("run_method"), kwargs.get("cmd")),
                    expected_classify,
                )
                self.assertEqual(
                    method_target_resolved(kwargs["path"], kwargs["http_method"],
                                           kwargs.get("run_method"), kwargs.get("cmd")),
                    expected_resolved,
                )


class TestDenyUnknownMethodScoping(unittest.TestCase):
    """§2/§4 LOCKED: a ``methods`` grant on a ``kind == "method"`` call is honored ONLY when the call
    is doctype-RESOLVED or the bare name is on the tiny curated ``SAFE_METHODS`` -- everything else
    is denied even if a broad pattern in ``scope.methods`` would otherwise fnmatch it."""

    def test_bare_unsafe_method_denied_even_when_granted(self):
        s = ApiScope.from_dict({"methods": ["frappe.client.delete_doc"]})
        self.assertFalse(is_permitted(s, "method", "frappe.client.delete_doc"))
        self.assertFalse(is_permitted(s, "method", "frappe.client.delete_doc", method_resolved=False))

    def test_bare_unsafe_method_denied_even_with_a_glob_grant(self):
        # A broad glob is not a resolution -- "frappe.client.*" still doesn't lift the bare-name deny.
        s = ApiScope.from_dict({"methods": ["frappe.client.*"]})
        self.assertFalse(is_permitted(s, "method", "frappe.client.delete_doc"))

    def test_bare_safe_method_allowed_if_granted(self):
        for name in SAFE_METHODS:
            s = ApiScope.from_dict({"methods": [name]})
            self.assertTrue(is_permitted(s, "method", name),
                            f"{name} is on SAFE_METHODS and granted -- must pass unresolved")

    def test_safe_method_membership_alone_grants_nothing(self):
        # SAFE_METHODS is NECESSARY-NOT-SUFFICIENT: scope.methods must still name it.
        s = ApiScope.from_dict({"methods": ["something.else"]})
        for name in SAFE_METHODS:
            self.assertFalse(is_permitted(s, "method", name),
                             f"{name} is safe but NOT granted -- must still deny")

    def test_resolved_anything_allowed_if_granted(self):
        # Once resolved, ANY granted per-doctype-style name passes -- resolution is what was missing.
        s = ApiScope.from_dict({"methods": ["Sales Invoice.some_custom_method"]})
        self.assertTrue(is_permitted(s, "method", "Sales Invoice.some_custom_method",
                                     method_resolved=True))

    def test_resolved_but_ungranted_still_denied(self):
        # Resolution lifts the bare-name gate; it never substitutes for the grant itself.
        s = ApiScope.from_dict({"methods": ["Sales Invoice.submit"]})
        self.assertFalse(is_permitted(s, "method", "Journal Entry.submit", method_resolved=True))


class TestBulkUpdateHardDeny(unittest.TestCase):
    """§7 LOCKED: ``Bulk Update`` is an *ungrantable* doctype-part -- the 2-hop laundering vector (its
    own instance method reads document_type/field from the SAVED RECORD, invisible to any classifier)
    is denied regardless of grant or resolution, checked BEFORE the grant match."""

    def test_bulk_update_denied_even_with_an_exact_grant(self):
        s = ApiScope.from_dict({"methods": ["Bulk Update.bulk_update"]})
        self.assertFalse(is_permitted(s, "method", "Bulk Update.bulk_update", method_resolved=True))

    def test_bulk_update_denied_even_with_a_wildcard_grant(self):
        s = ApiScope.from_dict({"methods": ["Bulk Update.*"]})
        self.assertFalse(is_permitted(s, "method", "Bulk Update.bulk_update", method_resolved=True))

    def test_bulk_update_denied_via_run_doc_method_route_too(self):
        # The run_doc_method route resolves to the SAME "Bulk Update.bulk_update" target -- one
        # hard-deny check covers both routes into the instance method.
        rewritten = body_scoped_target("method", "run_doc_method", "POST",
                                       {"dt": "Bulk Update", "dn": "some-name",
                                        "method": "bulk_update"})
        self.assertEqual(rewritten, ("method", "Bulk Update.bulk_update"))
        s = ApiScope.from_dict({"methods": ["Bulk Update.bulk_update"]})
        self.assertFalse(is_permitted(s, "method", rewritten[1], method_resolved=True))

    def test_other_doctypes_named_bulk_update_prefix_are_unaffected(self):
        # Sanity: the hard-deny keys on the exact doctype-part (split before the FIRST dot), not a
        # substring match -- a real doctype that merely CONTAINS "Bulk Update" is not swept in.
        s = ApiScope.from_dict({"methods": ["Bulk Update Log.submit"]})
        self.assertTrue(is_permitted(s, "method", "Bulk Update Log.submit", method_resolved=True))

    def test_bulk_update_denied_when_a_dotted_method_name_would_slide_the_boundary(self):
        # Redteam (mechanical lens): a run_doc_method `method` carrying a "." would rewrite to
        # "Bulk Update.x.bulk_update"; an rsplit(".",1) doctype-part is "Bulk Update.x" and dodges
        # the exact-name set. split(".",1) keeps the doctype-part "Bulk Update" -> still hard-denied
        # even under a wildcard grant. (frappe can't getattr a dotted method, but the guard holds its
        # own invariant rather than lean on that accident.)
        s = ApiScope.from_dict({"methods": ["Bulk Update.*", "*"]})
        self.assertFalse(is_permitted(s, "method", "Bulk Update.x.bulk_update", method_resolved=True))

    def test_bulk_update_denied_for_a_diacritic_variant(self):
        # Redteam (bypass lens, CONFIRMED at the classifier level): the case-fold mirror covered
        # CASE but not DIACRITICS. "Bulk Update".casefold() != "bülk update".casefold(), so a
        # diacritic-substituted doctype-part sailed through is_permitted as True under a wildcard
        # grant, while MariaDB's default accent-insensitive collation plausibly still resolves it to
        # the real "Bulk Update" record (the same 2-hop laundering vector this hard-deny exists to
        # close). Fold accents too — deny-only, same safe direction as the case fold.
        s = ApiScope.from_dict({"methods": ["*"]})
        for variant in ("Bülk Update.bulk_update", "Bùlk Ûpdate.bulk_update",
                        "Bulk Updàte.bulk_update"):
            with self.subTest(variant=variant):
                self.assertFalse(is_permitted(s, "method", variant, method_resolved=True))
        # and via the run_doc_method body route
        rewritten = body_scoped_target("method", "run_doc_method", "POST",
                                       {"dt": "Bülk Update", "dn": "x", "method": "bulk_update"})
        self.assertFalse(is_permitted(s, "method", rewritten[1], method_resolved=True))

    def test_a_real_doctype_that_merely_normalizes_near_bulk_update_is_not_swept_in(self):
        # The accent fold must not over-reach into a genuinely different doctype name.
        s = ApiScope.from_dict({"methods": ["Bulk Update Log.submit"]})
        self.assertTrue(is_permitted(s, "method", "Bulk Update Log.submit", method_resolved=True))

    def test_bulk_update_denied_with_trailing_space_on_the_doctype_segment(self):
        # Redteam (bypass lens #4): a URL path "/api/resource/Bulk Update /x?run_method=bulk_update"
        # is unquoted WITHOUT strip -> target "Bulk Update .bulk_update"; the .strip() in the
        # doctype-part extraction closes the whitespace evasion of the exact-name set.
        s = ApiScope.from_dict({"methods": ["*"]})
        self.assertFalse(is_permitted(s, "method", "Bulk Update .bulk_update", method_resolved=True))

    def test_run_doc_method_dt_decoy_disagreeing_with_body_doc_denies(self):
        # Redteam (bypass lens #1, CRITICAL): frappe v2 run_doc_method has no `dt` param and acts on
        # `document`; a dt="Sales Invoice" decoy alongside document={"doctype":"Journal Entry"} would
        # spoof the guard into authorizing "Sales Invoice.submit" while frappe submits the JE. The
        # doctype sources disagree -> body_scoped_target fails closed rather than trust `dt`.
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST",
                               {"dt": "Sales Invoice",
                                "document": {"doctype": "Journal Entry", "name": "JE-1"},
                                "method": "submit"}),
            ("other", None),
        )

    def test_run_doc_method_dt_decoy_launders_bulk_update_document_denied(self):
        # Same spoof aimed at the hard-deny: dt="Sales Invoice" decoy + a Bulk Update `document`.
        # Disagreement denies before the rewrite; even if it didn't, the doctype-part fix would.
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST",
                               {"dt": "Sales Invoice",
                                "document": {"doctype": "Bulk Update", "document_type": "Journal Entry",
                                             "field": "docstatus", "update_value": 2},
                                "method": "bulk_update"}),
            ("other", None),
        )

    def test_run_doc_method_dt_and_document_in_agreement_still_resolves(self):
        # Not over-blocking: a `dt` that AGREES with the body `document` (or a single source) resolves
        # normally -- only a DISAGREEMENT (the spoof) is denied.
        self.assertEqual(
            body_scoped_target("method", "run_doc_method", "POST",
                               {"dt": "Journal Entry",
                                "document": {"doctype": "Journal Entry", "name": "JE-1"},
                                "method": "submit"}),
            ("method", "Journal Entry.submit"),
        )

    # -- form_dict extraction seam: case-insensitivity on the ungrantable doctype-part -------------
    #
    # None of the checks above vary the CASE of "Bulk Update" -- every source that can populate the
    # doctype-part (a URL path segment, a `doctype`/`dt`/`document.doctype` body field) is a plain
    # string an attacker fully controls, and this module's own hard-deny set is compared with exact
    # Python string equality. MariaDB/MySQL's default collation for a VARCHAR column (frappe's own
    # `tabDocType.name`, `utf8mb4_general_ci`/`_unicode_ci`) compares case-INSENSITIVELY, so a
    # doctype string that differs only in case very plausibly still resolves to the SAME real
    # "Bulk Update" record at frappe's DB layer -- reopening the exact 2-hop laundering vector this
    # hard-deny exists to close, the moment a credential holds any grant pattern broad enough to
    # fnmatch the differently-cased string (e.g. "*", or "Bulk Update.*" if written case-loosely).
    # NEEDS BENCH PIN: frappe's actual doctype-lookup collation/case-sensitivity has not been
    # exercised against a live bench. The fix is taken regardless, on cost-benefit alone: a
    # case-insensitive HARD-DENY can only deny MORE than before, never less -- there is no
    # legitimate call this could wrongly block (only the credential's own GRANT matching, which
    # stays case-sensitive fnmatch and is untouched here, decides what a scoped credential may do;
    # this check only ever subtracts from that, on one specific doctype-part).

    def test_bulk_update_denied_for_a_lowercase_case_variant(self):
        s = ApiScope.from_dict({"methods": ["*"]})
        self.assertFalse(is_permitted(s, "method", "bulk update.bulk_update", method_resolved=True))

    def test_bulk_update_denied_for_an_uppercase_case_variant(self):
        s = ApiScope.from_dict({"methods": ["*"]})
        self.assertFalse(is_permitted(s, "method", "BULK UPDATE.bulk_update", method_resolved=True))

    def test_bulk_update_denied_for_a_case_variant_via_run_doc_method(self):
        # The rewrite path preserves whatever case the body sent -- `dt` flows straight through to
        # the hard-deny check, unmodified by body_scoped_target itself.
        rewritten = body_scoped_target("method", "run_doc_method", "POST",
                                       {"dt": "bulk UPDATE", "dn": "some-name",
                                        "method": "bulk_update"})
        self.assertEqual(rewritten, ("method", "bulk UPDATE.bulk_update"))
        s = ApiScope.from_dict({"methods": ["*"]})
        self.assertFalse(is_permitted(s, "method", rewritten[1], method_resolved=True))

    def test_other_doctypes_named_bulk_update_prefix_still_unaffected_by_the_case_fix(self):
        # The case-insensitive check must stay an EXACT match, not a substring one -- a differently
        # cased "Bulk Update Log" (a distinct real doctype merely sharing the prefix) is still not
        # swept in, mirroring test_other_doctypes_named_bulk_update_prefix_are_unaffected above.
        s = ApiScope.from_dict({"methods": ["bulk update log.submit"]})
        self.assertTrue(is_permitted(s, "method", "bulk update log.submit", method_resolved=True))


class TestContainerDoctypeHardDeny(unittest.TestCase):
    """Container/tool-DocType 2-hop hardening (John's ruling 2026-07-10, docs/plans/
    2026-07-10-container-doctype-2hop-hardening.md): four tool-DocTypes the broker never governs but
    whose granted controller method drives writes to OTHER doctypes named in the request body/record
    (the `Bulk Update` shape) are now *ungrantable*, via the same pre-grant hard-deny. Data Import /
    Bank Statement Import are source-traced (`form_start_import` insert+submit of an arbitrary
    `reference_doctype`); Unreconcile Payment / Repost Accounting Ledger are the plausible siblings —
    a hard-deny is safe regardless of full-depth tracing since the broker uses none of them.

    NOT swept in: `Payment Reconciliation.reconcile` — the broker's OWN F-R2 write needs it, and its
    malicious use is byte-identical to the legit one, so it cannot be classifier-closed without
    breaking the broker; it stays a disclosed residual + operator rule (grant it only to the broker's
    own credential). That regression is pinned below."""

    _NEW = ("Data Import", "Bank Statement Import", "Unreconcile Payment", "Repost Accounting Ledger")

    def test_each_new_container_doctype_denied_even_under_a_wildcard_grant(self):
        s = ApiScope.from_dict({"methods": ["*"]})
        for dt in self._NEW:
            for method in ("form_start_import", "submit", "bulk_update", "anything"):
                with self.subTest(doctype=dt, method=method):
                    self.assertFalse(is_permitted(s, "method", f"{dt}.{method}",
                                                  method_resolved=True))

    def test_data_import_v2_two_segment_route_resolves_and_is_denied(self):
        # End-to-end: the actual v2 route the audit traced resolves to a "Data Import.<method>"
        # target, which the pre-grant hard-deny then refuses even with an exact method grant.
        kind, target = classify("/api/v2/method/Data Import/form_start_import", "POST")
        self.assertEqual((kind, target), ("method", "Data Import.form_start_import"))
        s = ApiScope.from_dict({"methods": ["Data Import.form_start_import"]})
        self.assertFalse(is_permitted(s, "method", target, method_resolved=True))

    def test_denied_via_run_doc_method_body_route_too(self):
        s = ApiScope.from_dict({"methods": ["*"]})
        rewritten = body_scoped_target("method", "run_doc_method", "POST",
                                       {"dt": "Repost Accounting Ledger", "dn": "x",
                                        "method": "submit"})
        self.assertEqual(rewritten, ("method", "Repost Accounting Ledger.submit"))
        self.assertFalse(is_permitted(s, "method", rewritten[1], method_resolved=True))

    def test_case_and_accent_variants_denied(self):
        # Same fold as Bulk Update: MariaDB's collation resolves these to the same real DocType.
        s = ApiScope.from_dict({"methods": ["*"]})
        for variant in ("data import.form_start_import", "DATA IMPORT.form_start_import",
                        "Bänk Statement Import.form_start_import"):
            with self.subTest(variant=variant):
                self.assertFalse(is_permitted(s, "method", variant, method_resolved=True))

    def test_payment_reconciliation_reconcile_stays_grantable_no_broker_regression(self):
        # THE regression that scopes this whole change: the broker's own F-R2 reconcile MUST still
        # pass — Payment Reconciliation is NOT ungrantable (it can't be, without breaking the broker).
        s = ApiScope.from_dict({"methods": ["Payment Reconciliation.reconcile"]})
        self.assertTrue(is_permitted(s, "method", "Payment Reconciliation.reconcile",
                                     method_resolved=True))

    def test_a_real_doctype_sharing_a_prefix_is_not_swept_in(self):
        # Exact doctype-part match, not substring — a distinct real doctype merely sharing a prefix
        # (e.g. "Data Import Log") is unaffected.
        s = ApiScope.from_dict({"methods": ["Data Import Log.submit"]})
        self.assertTrue(is_permitted(s, "method", "Data Import Log.submit", method_resolved=True))
