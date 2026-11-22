# Copyright (c) 2026, John Broadway and contributors
# License: AGPL-3.0-or-later
"""A resource write that moves ``docstatus`` is a submit or a cancel, and needs that grant.

Reported privately (GHSA-7267-5929-54c6, hariprasanth9317-tech): a credential holding the resource
``write`` or ``create`` verb and NO ``<DocType>.submit`` grant could post or reverse a document by
putting ``docstatus`` in a plain REST body, because frappe's ``Document.save``/``insert`` treats a
0->1 or 1->2 body as the real transition.

The expected values here are frappe's, not this package's: which body the v1 handler applies is
read from ``frappe/api/v1.py`` ``get_request_form_data`` (the wiring test at the bottom pins that
source), and which body v2 applies from ``frappe/api/v2.py`` (``data = frappe.form_dict``).
"""
import json
import os
import pathlib
import unittest

from pacioli_guard.scope import (
    RESOURCE_BODY_UNREADABLE,
    is_docstatus_changing,
    resource_docstatus_target,
    resource_identity_problem,
    resource_write_bodies,
)
from pacioli_guard.tests.test_enforce import (
    AUTH,
    FakeFrappe,
    FakePermissionError,
    FakeReq,
    FakeScopeDoc,
    _Base,
)
from pacioli_guard import enforce

V1 = "/api/resource/Sales Invoice/SINV-1"
V1_COLLECTION = "/api/resource/Sales Invoice"
V2 = "/api/v2/document/Sales Invoice/SINV-1"
V2_COLLECTION = "/api/v2/document/Sales Invoice"


class TestWhichBodiesTheHandlerCouldApply(unittest.TestCase):
    """``resource_write_bodies`` returns EVERY body frappe's handler could apply to the document.

    More than one on v1, on purpose. The v1 handler parses with orjson and falls back to
    ``form_dict`` when that fails; this package parses with the standard library. Two parsers
    never agree on every input (``NaN``, an oversized integer, a lone surrogate), so a gate that
    trusted its own parse could be shown a draft while frappe applied a submit from the query
    string. Judging both candidates needs no agreement between parsers.
    """

    def test_v2_applies_form_dict_and_nothing_else(self):
        form = {"docstatus": 1, "data": '{"docstatus": 0}'}
        self.assertEqual(resource_write_bodies(V2, form, '{"docstatus": 2}'), (form,))

    def test_v1_json_body_with_no_data_key(self):
        self.assertEqual(resource_write_bodies(V1, {"docstatus": 1}, '{"docstatus": 1}'),
                         ({"docstatus": 1}, {"docstatus": 1}))

    def test_v1_reads_a_json_body_frappe_did_not_put_in_form_dict(self):
        # A JSON body under a non-JSON content type: make_form_dict leaves form_dict empty, and
        # the v1 handler still parses the raw bytes. The gate must see what the handler sees.
        self.assertIn({"docstatus": 1}, resource_write_bodies(V1, {}, '{"docstatus": 1}'))

    def test_v1_data_field_is_a_candidate(self):
        form = {"data": '{"docstatus": 2}'}
        self.assertIn({"docstatus": 2}, resource_write_bodies(V1, form, "data=..."))

    def test_v1_data_already_a_dict(self):
        self.assertIn({"docstatus": 1}, resource_write_bodies(V1, {"data": {"docstatus": 1}}, ""))

    def test_v1_form_dict_is_always_a_candidate_because_it_is_the_fallback(self):
        # The parser-differential case: this package's parser accepts NaN, orjson refuses it and
        # frappe then applies form_dict, which carries the submit from the query string.
        bodies = resource_write_bodies(V1, {"docstatus": "1"}, '{"docstatus": 0, "x": NaN}')
        self.assertIn({"docstatus": "1"}, bodies)
        self.assertEqual(resource_write_bodies(V1, {"grand_total": 5}, ""), ({"grand_total": 5},))

    def test_v1_text_this_parser_refuses_but_that_opens_an_object_is_unreadable(self):
        # The other direction of the same differential: if THIS parser fails on text frappe's
        # parser might still read as an object, the gate has not seen the document. A byte-order
        # mark is the worked example.
        for raw in ('\ufeff{"docstatus": 1}', ' \n\t{"docstatus": 1,}', "{" * 100000):
            with self.subTest(raw=raw[:20]):
                self.assertIn(RESOURCE_BODY_UNREADABLE, resource_write_bodies(V1, {}, raw))
        self.assertIn(RESOURCE_BODY_UNREADABLE,
                      resource_write_bodies(V1, {"data": '\ufeff{"docstatus": 2}'}, ""))

    def test_v1_text_too_deep_for_this_parser_is_unreadable(self):
        # How deep is too deep differs by interpreter, so the parser is made to give up here
        # rather than fed a depth that may or may not exhaust it.
        from unittest import mock
        from pacioli_guard import scope as scope_mod
        with mock.patch.object(scope_mod.json, "loads", side_effect=RecursionError):
            self.assertEqual(resource_write_bodies(V1, {"remarks": "x"}, '{"docstatus": 1}'),
                             (RESOURCE_BODY_UNREADABLE, {"remarks": "x"}))

    def test_v1_form_encoded_text_is_not_a_document(self):
        # An ordinary form post: the raw body is `a=1&b=2`, frappe's parse fails too, and
        # form_dict is what gets applied. This must stay readable or every form client breaks.
        for raw in ("remarks=x&due_date=2026-01-01", "", "   ", "docstatus=1"):
            with self.subTest(raw=raw):
                self.assertEqual(resource_write_bodies(V1, {"remarks": "x"}, raw),
                                 ({"remarks": "x"},))

    def test_v1_prefixed_path_is_v1(self):
        self.assertIn(
            {"docstatus": 1},
            resource_write_bodies("/api/v1/resource/Sales Invoice/SINV-1", {}, '{"docstatus": 1}'))

    def test_a_body_that_is_not_an_object_is_unreadable(self):
        for raw in ("[1, 2]", "7", '"x"', "null", "true"):
            with self.subTest(raw=raw):
                self.assertIn(RESOURCE_BODY_UNREADABLE, resource_write_bodies(V1, {}, raw))
        self.assertIn(RESOURCE_BODY_UNREADABLE, resource_write_bodies(V1, {"data": [1]}, ""))

    def test_a_raw_body_that_could_not_be_read_is_unreadable_on_v1(self):
        self.assertIn(RESOURCE_BODY_UNREADABLE, resource_write_bodies(V1, {}, None))
        # v2 never reads the raw body, so not having it changes nothing there.
        self.assertEqual(resource_write_bodies(V2, {"x": 1}, None), ({"x": 1},))

    def test_form_dict_that_is_not_a_dict_is_unreadable(self):
        self.assertEqual(resource_write_bodies(V2, None, ""), (RESOURCE_BODY_UNREADABLE,))


class TestResourceDocstatusTarget(unittest.TestCase):
    """The extra grant a resource write needs, from the body the handler will apply."""

    R_WRITE = ("resource", ("Sales Invoice", "write"))
    R_CREATE = ("resource", ("Sales Invoice", "create"))

    def test_update_to_docstatus_1_is_a_submit(self):
        for http in ("PUT", "PATCH"):
            with self.subTest(http=http):
                self.assertEqual(resource_docstatus_target(*self.R_WRITE, http, ({"docstatus": 1},)),
                                 ("method", "Sales Invoice.submit"))

    def test_update_to_docstatus_2_is_a_cancel(self):
        self.assertEqual(resource_docstatus_target(*self.R_WRITE, "PUT", ({"docstatus": "2"},)),
                         ("method", "Sales Invoice.cancel"))

    def test_create_as_submitted_is_a_submit(self):
        self.assertEqual(resource_docstatus_target(*self.R_CREATE, "POST", ({"docstatus": 1},)),
                         ("method", "Sales Invoice.submit"))

    def test_a_draft_write_needs_nothing_more(self):
        for body in ({}, {"docstatus": 0}, {"docstatus": "0"}, {"grand_total": 5},
                     {"docstatus": None}):
            with self.subTest(body=body):
                self.assertIsNone(resource_docstatus_target(*self.R_WRITE, "PUT", (body,)))
                self.assertIsNone(resource_docstatus_target(*self.R_CREATE, "POST", (body,)))

    def test_a_docstatus_that_cannot_be_read_fails_closed(self):
        for ds in ("x", [1], {"a": 1}, 3, -1):
            with self.subTest(ds=ds):
                self.assertEqual(
                    resource_docstatus_target(*self.R_WRITE, "PUT", ({"docstatus": ds},)),
                    ("other", None))

    def test_an_unreadable_body_fails_closed_on_a_write(self):
        self.assertEqual(
            resource_docstatus_target(*self.R_WRITE, "PUT", ({}, RESOURCE_BODY_UNREADABLE)),
            ("other", None))
        self.assertEqual(
            resource_docstatus_target(*self.R_CREATE, "POST", (RESOURCE_BODY_UNREADABLE,)),
            ("other", None))

    def test_any_candidate_that_moves_docstatus_decides(self):
        self.assertEqual(
            resource_docstatus_target(*self.R_WRITE, "PUT", ({"docstatus": 0}, {"docstatus": "1"})),
            ("method", "Sales Invoice.submit"))

    def test_candidates_that_disagree_on_the_act_fail_closed(self):
        self.assertEqual(
            resource_docstatus_target(*self.R_WRITE, "PUT", ({"docstatus": 1}, {"docstatus": 2})),
            ("other", None))

    def test_no_candidates_at_all_fails_closed(self):
        self.assertEqual(resource_docstatus_target(*self.R_WRITE, "PUT", ()), ("other", None))

    def test_reads_and_deletes_are_not_judged_by_a_body(self):
        for http, verb in (("GET", "read"), ("DELETE", "delete")):
            with self.subTest(http=http):
                self.assertIsNone(resource_docstatus_target(
                    "resource", ("Sales Invoice", verb), http, ({"docstatus": 1},)))
                self.assertIsNone(resource_docstatus_target(
                    "resource", ("Sales Invoice", verb), http, (RESOURCE_BODY_UNREADABLE,)))

    def test_other_kinds_are_left_to_their_own_rewrite(self):
        self.assertIsNone(resource_docstatus_target("method", "frappe.client.save", "POST",
                                                    ({"docstatus": 1},)))
        self.assertIsNone(resource_docstatus_target("other", None, "POST", ({"docstatus": 1},)))


class _BodyReq(FakeReq):
    """A request that can hand over its raw body, as werkzeug's does."""

    def __init__(self, path, method, raw=""):
        super().__init__(path, method)
        self._raw = raw

    def get_data(self, as_text=False, **_):
        if isinstance(self._raw, Exception):
            raise self._raw
        return self._raw if as_text else self._raw.encode()


WRITE_NO_METHODS = dict(allow_resource=True, resource_doctypes=["Sales Invoice"],
                        verb_read=1, verb_create=1, verb_write=1, verb_delete=0)


class TestCheckScopeRefusesALifecycleMoveByBody(_Base):
    """Through the real entry point. The grant: resource read/create/write on Sales Invoice, and
    an EMPTY methods list, so no submit and no cancel was granted."""

    def _fake(self, scope_doc, path, method, form_dict, raw=""):
        return FakeFrappe(headers=AUTH, session_user="alice@x", scopes={"alice@x": scope_doc},
                          request=_BodyReq(path, method, raw), form_dict=form_dict)

    def _denied(self, path, method, form, raw="", scope_doc=None):
        enforce.frappe = self._fake(scope_doc or FakeScopeDoc(**WRITE_NO_METHODS),
                                    path, method, form, raw)
        with self.assertRaises(FakePermissionError) as ctx:
            enforce.check_scope()
        return str(ctx.exception)

    def _allowed(self, path, method, form, raw="", scope_doc=None):
        enforce.frappe = self._fake(scope_doc or FakeScopeDoc(**WRITE_NO_METHODS),
                                    path, method, form, raw)
        self.assertIsNone(enforce.check_scope())

    # --- the report's own requests -------------------------------------------------------------
    def test_v1_put_docstatus_1_is_refused_without_a_submit_grant(self):
        msg = self._denied(V1, "PUT", {"docstatus": 1}, '{"docstatus": 1}')
        self.assertIn("Sales Invoice.submit", msg)

    def test_v1_put_docstatus_2_is_refused_without_a_cancel_grant(self):
        msg = self._denied(V1, "PUT", {"docstatus": 2}, '{"docstatus": 2}')
        self.assertIn("Sales Invoice.cancel", msg)

    def test_v2_put_docstatus_1_is_refused(self):
        self.assertIn("Sales Invoice.submit", self._denied(V2, "PUT", {"docstatus": 1}))

    def test_v1_create_as_submitted_is_refused(self):
        self.assertIn("Sales Invoice.submit",
                      self._denied(V1_COLLECTION, "POST", {"docstatus": 1, "customer": "C"},
                                   '{"docstatus": 1, "customer": "C"}'))

    def test_v2_create_as_submitted_is_refused(self):
        self.assertIn("Sales Invoice.submit",
                      self._denied(V2_COLLECTION, "POST", {"docstatus": 1}))

    # --- the same move in the shapes the old read could not see --------------------------------
    def test_v1_json_under_a_non_json_content_type_is_refused(self):
        self.assertIn("Sales Invoice.submit", self._denied(V1, "PUT", {}, '{"docstatus": 1}'))

    def test_v1_data_form_field_is_refused(self):
        self.assertIn("Sales Invoice.submit",
                      self._denied(V1, "PUT", {"data": '{"docstatus": 1}'}, "data=%7B..."))

    def test_v1_nested_data_object_is_refused(self):
        self.assertIn("Sales Invoice.cancel",
                      self._denied(V1, "PUT", {"data": {"docstatus": 2}},
                                   '{"data": {"docstatus": 2}}'))

    def test_the_parser_differential_shape_is_refused(self):
        # Raw body this package parses as a draft; frappe's parser refuses it and applies the
        # query string instead.
        self.assertIn("Sales Invoice.submit",
                      self._denied(V1, "PUT", {"docstatus": "1"}, '{"docstatus": 0, "x": NaN}'))

    def test_a_body_that_is_not_an_object_is_refused(self):
        self._denied(V1, "PUT", {}, "[1]")

    def test_a_raw_body_that_cannot_be_read_is_refused_on_v1(self):
        self._denied(V1, "PUT", {"grand_total": 5}, RuntimeError("stream gone"))

    # --- what must keep working ----------------------------------------------------------------
    def test_a_draft_field_write_is_still_allowed(self):
        self._allowed(V1, "PUT", {"remarks": "x"}, '{"remarks": "x"}')
        self._allowed(V2, "PUT", {"remarks": "x"})
        self._allowed(V1_COLLECTION, "POST", {"customer": "C", "docstatus": 0},
                      '{"customer": "C", "docstatus": 0}')

    def test_a_read_is_not_judged_by_its_body(self):
        self._allowed(V1, "GET", {"docstatus": 1}, "")

    def test_the_submit_grant_plus_the_write_verb_allows_it(self):
        doc = FakeScopeDoc(method_patterns=["Sales Invoice.submit"], **WRITE_NO_METHODS)
        self._allowed(V1, "PUT", {"docstatus": 1}, '{"docstatus": 1}', scope_doc=doc)
        # ...and a submit grant is not a cancel grant.
        self.assertIn("Sales Invoice.cancel",
                      self._denied(V1, "PUT", {"docstatus": 2}, '{"docstatus": 2}', scope_doc=doc))

    def test_the_submit_grant_alone_does_not_grant_the_resource_write(self):
        # Both are needed: the PUT also writes fields, which is the resource `write` verb.
        doc = FakeScopeDoc(method_patterns=["Sales Invoice.submit"])
        msg = self._denied(V1, "PUT", {"docstatus": 1}, '{"docstatus": 1}', scope_doc=doc)
        self.assertIn("resource", msg)

    def test_v2_patch_docstatus_is_refused(self):
        # PATCH reaches the same v2 handler as PUT; a review showed nothing drove it end to end.
        self.assertIn("Sales Invoice.submit", self._denied(V2, "PATCH", {"docstatus": 1}))

    def test_a_docstatus_too_large_to_be_a_number_is_refused_not_a_crash(self):
        self._denied(V1, "PUT", {}, '{"docstatus": Infinity}')
        self._denied(V1, "PUT", {"data": '{"docstatus": 1e400}'}, "")

    # --- an update that is not an update (review of the first cut) -----------------------------
    # frappe's `Document._save` turns into `insert()` when the document says it is new
    # (`__islocal`) or has no name, and `doc.update(body)` lets the body say either, or point
    # `name` at another document. The loaded document keeps its docstatus, so a submitted one
    # is INSERTED AS SUBMITTED, or its state is written onto the document the body names, with
    # no `docstatus` key in the body at all.
    def test_an_update_that_declares_itself_new_is_refused(self):
        self._denied(V1, "PUT", {"__islocal": 1}, '{"__islocal": 1}')
        self._denied(V2, "PATCH", {"__islocal": 1})
        self._denied(V1, "PUT", {"__islocal": "1"}, "__islocal=1")

    def test_an_update_that_blanks_or_changes_the_name_is_refused(self):
        for name in (None, "", "SINV-2", 0, ["SINV-1"], " SINV-1"):
            with self.subTest(name=name):
                self._denied(V2, "PUT", {"name": name})
        self._denied(V1, "PUT", {}, '{"name": "SINV-2", "modified": "2026-01-01"}')

    def test_a_whole_document_put_back_under_its_own_name_is_allowed(self):
        self._allowed(V1, "PUT", {"name": "SINV-1", "remarks": "x"},
                      '{"name": "SINV-1", "remarks": "x"}')
        self._allowed(V2, "PUT", {"name": "SINV-1", "remarks": "x"})

    def test_a_name_with_slashes_matches_its_own_path(self):
        self._allowed("/api/v2/document/Sales Invoice/ACC/2026/0001", "PUT",
                      {"name": "ACC/2026/0001", "remarks": "x"})
        self._allowed("/api/resource/Sales Invoice/ACC/2026/0001", "PUT",
                      {"name": "ACC/2026/0001"}, '{"name": "ACC/2026/0001"}')

    def test_the_path_is_not_decoded_a_second_time(self):
        # werkzeug hands over a path it has already decoded. A document really named `A%2FB`
        # arrives as that text; decoding it again would read `A/B`, and a body naming `A/B`
        # would then match the gate's answer while frappe had loaded `A%2FB` (review, 0.18.0).
        self._denied("/api/resource/Sales Invoice/A%2FB", "PUT", {"name": "A/B"},
                     '{"name": "A/B"}')
        self._allowed("/api/resource/Sales Invoice/A%2FB", "PUT", {"name": "A%2FB"},
                      '{"name": "A%2FB"}')

    def test_the_v1_prefixed_path_names_the_same_document(self):
        self._allowed("/api/v1/resource/Sales Invoice/SINV-1", "PUT", {"name": "SINV-1"},
                      '{"name": "SINV-1"}')

    def test_a_create_may_name_its_document(self):
        self._allowed(V1_COLLECTION, "POST", {"name": "SINV-9", "customer": "C"},
                      '{"name": "SINV-9", "customer": "C"}')

    # --- second review round --------------------------------------------------------------------
    def test_a_v2_update_of_a_document_whose_name_ends_in_method_is_a_resource_write(self):
        # frappe routes the path-carried doc-method for GET and POST only (`api/v2.py`). A PUT or
        # PATCH to `/document/<dt>/X/method/get_pdf` is `update_doc` on the document NAMED
        # `X/method/get_pdf`, so it must be judged as a write, never as a call to `get_pdf`.
        path = "/api/v2/document/Sales Invoice/X/method/get_pdf"
        method_only = FakeScopeDoc(method_patterns=["Sales Invoice.get_pdf"])
        for http in ("PUT", "PATCH", "DELETE"):
            with self.subTest(http=http):
                self.assertIn("resource", self._denied(path, http, {"remarks": "x"},
                                                       scope_doc=method_only))
        both = FakeScopeDoc(method_patterns=["Sales Invoice.get_pdf"], **WRITE_NO_METHODS)
        self.assertIn("Sales Invoice.submit",
                      self._denied(path, "PUT", {"docstatus": 1}, scope_doc=both))
        # GET and POST are still the method call.
        self._allowed(path, "POST", {}, scope_doc=method_only)

    def test_every_other_verb_on_a_method_path_is_still_the_method(self):
        # werkzeug answers HEAD from a GET rule, so HEAD on `.../method/submit` runs submit. Only
        # the three verbs frappe routes to update_doc/delete_doc may be read as a resource write;
        # anything else stays the method call (the first cut of this fix listed GET and POST and
        # let HEAD fall through as a resource READ).
        path = "/api/v2/document/Sales Invoice/SINV-1/method/submit"
        read_only = FakeScopeDoc(allow_resource=True, resource_doctypes=["Sales Invoice"],
                                 verb_read=1, verb_create=0, verb_write=0, verb_delete=0)
        for http in ("HEAD", "GET", "POST", "OPTIONS", "TRACE", "head"):
            with self.subTest(http=http):
                self.assertIn("Sales Invoice.submit",
                              self._denied(path, http, {}, scope_doc=read_only))

    def test_an_empty_path_segment_is_part_of_the_name(self):
        # frappe's router does not merge slashes: `/A//B` is the document `A//B`. Dropping the
        # empty segment read it as `A/B` and vouched for a body naming that other document.
        for path in ("/api/resource/Sales Invoice/A//B", "/api/v1/resource/Sales Invoice/A//B",
                     "/api/v2/document/Sales Invoice/A//B"):
            with self.subTest(path=path):
                raw = '{"name": "A/B"}' if "/v2/" not in path else ""
                self._denied(path, "PUT", {"name": "A/B"}, raw)
        self._allowed("/api/v2/document/Sales Invoice/A//B", "PUT", {"name": "A//B"})
        self._allowed("/api/v2/document/Sales Invoice/SINV-1/", "PUT", {"name": "SINV-1"})

    def test_a_body_may_not_set_the_save_action(self):
        # `_action` is not reserved, `check_if_latest` keeps one that is already set, and
        # "discard" skips frappe's docstatus transition check and every before/on hook, the
        # consent gate's included.
        self._denied(V1, "PUT", {"_action": "discard", "docstatus": 0},
                     '{"_action": "discard", "docstatus": 0}')
        self._denied(V2, "PATCH", {"_action": "discard", "grand_total": 0})
        self._denied(V2, "PUT", {"_action": "save"})
        granted = FakeScopeDoc(method_patterns=["Sales Invoice.submit"], **WRITE_NO_METHODS)
        self._denied(V2_COLLECTION, "POST", {"_action": "discard", "docstatus": 1},
                     scope_doc=granted)
        # The underscore columns frappe really returns are still writable.
        self._allowed(V2, "PUT", {"_user_tags": ",x", "_comments": "[]"})


class TestARefusalThatDoesNotRaiseStillStops(_Base):
    """Each 0.18.0 refusal in `check_scope` is followed by `return`. `_deny` raises on a real bench,
    so the return is the second line of defence: if a refusal ever came back (a patched `throw`, a
    swallowed error), the request must still stop there and not fall through to the checks below.
    Each body here also trips a LATER check, so a missing `return` shows up as a second refusal."""

    def _refusals(self, path, method, form, raw):
        enforce.frappe = FakeFrappe(headers=AUTH, session_user="alice@x",
                                    scopes={"alice@x": FakeScopeDoc(**WRITE_NO_METHODS)},
                                    request=_BodyReq(path, method, raw), form_dict=form)
        seen = []
        real = enforce._deny
        enforce._deny = lambda reason, message: seen.append(message)
        try:
            self.assertIsNone(enforce.check_scope())
        finally:
            enforce._deny = real
        return seen

    def test_the_action_refusal_stops(self):
        body = {"_action": "discard", "docstatus": 1}
        seen = self._refusals(V1, "PUT", body, json.dumps(body))
        self.assertEqual(len(seen), 1, seen)
        self.assertIn("_action", seen[0])

    def test_the_identity_refusal_stops(self):
        body = {"__islocal": 1, "docstatus": 1}
        seen = self._refusals(V1, "PUT", body, json.dumps(body))
        self.assertEqual(len(seen), 1, seen)
        self.assertIn("__islocal", seen[0])

    def test_the_docstatus_refusal_stops(self):
        body = {"docstatus": 1}
        seen = self._refusals(V1, "PUT", body, json.dumps(body))
        self.assertEqual(len(seen), 1, seen)
        self.assertIn("Sales Invoice.submit", seen[0])


class TestResourceIdentityProblem(unittest.TestCase):
    def test_only_updates_are_judged_for_identity(self):
        for http in ("GET", "POST", "DELETE"):
            self.assertIsNone(resource_identity_problem(V1, http, ({"__islocal": 1, "name": ""},)))

    def test_any_double_underscore_key_is_a_problem(self):
        for key in ("__islocal", "__unsaved", "__anything"):
            self.assertIsNotNone(resource_identity_problem(V1, "PUT", ({key: 0},)))

    def test_every_candidate_is_judged(self):
        self.assertIsNotNone(
            resource_identity_problem(V1, "PUT", ({"remarks": "x"}, {"name": "SINV-2"})))
        self.assertIsNone(
            resource_identity_problem(V1, "PUT", ({"remarks": "x"}, {"name": "SINV-1"})))

    def test_a_name_must_be_a_string_or_a_number_even_when_it_prints_like_the_path(self):
        # str(True) == "True" and str(["X"]) is not "X": only a str or an int may match the path.
        self.assertIsNotNone(resource_identity_problem(
            "/api/resource/Sales Invoice/True", "PUT", ({"name": True},)))
        self.assertIsNotNone(resource_identity_problem(
            "/api/resource/Sales Invoice/1.0", "PUT", ({"name": 1.0},)))
        self.assertIsNone(resource_identity_problem(
            "/api/resource/Sales Invoice/7", "PUT", ({"name": 7},)))

    def test_a_name_frappe_reads_as_no_name_is_a_problem_even_when_it_matches_the_path(self):
        # `not self.get("name")` is true for 0, so frappe would insert. str(0) == "0" must not
        # vouch for it (review, 0.18.0).
        for path_name, name in (("0", 0), ("-1", -1)):
            with self.subTest(name=name):
                self.assertIsNotNone(resource_identity_problem(
                    f"/api/resource/Sales Invoice/{path_name}", "PUT", ({"name": name},)))

    def test_a_path_that_names_no_document_cannot_vouch_for_a_name(self):
        self.assertIsNotNone(
            resource_identity_problem("/api/resource/Sales Invoice", "PUT", ({"name": "X"},)))


class TestWorkflowGateReadsTheAppliedBody(_Base):
    """The workflow gate judged ``frappe.form_dict``. On v1 that is not always the body applied."""

    def _fake(self, form, raw):
        doc = FakeScopeDoc(method_patterns=["Sales Invoice.submit"], enforce_workflow=1,
                           **WRITE_NO_METHODS)
        return FakeFrappe(headers=AUTH, session_user="alice@x", scopes={"alice@x": doc},
                          request=_BodyReq(V1, "PUT", raw), form_dict=form,
                          workflows={"Sales Invoice": "SI Approval"})

    def test_pure_gate_still_keys_on_the_body_it_is_given(self):
        self.assertTrue(is_docstatus_changing(
            "resource", ("Sales Invoice", "write"), "PUT", {"docstatus": 1}))

    def test_a_docstatus_hidden_from_form_dict_is_still_a_workflow_bypass(self):
        for form, raw in (({}, '{"docstatus": 1}'), ({"data": '{"docstatus": 1}'}, "data=...")):
            with self.subTest(form=form):
                enforce.frappe = self._fake(form, raw)
                with self.assertRaises(FakePermissionError) as ctx:
                    enforce.check_scope()
                self.assertIn("workflow", str(ctx.exception).lower())

    def test_a_docstatus_spelled_oddly_is_still_a_workflow_bypass(self):
        # The scope check reads docstatus with int(), so " 1" is a submit there. The workflow gate
        # must not read the same body more loosely than the gate before it.
        for http, path, ds in (("POST", V1_COLLECTION, " 1"), ("POST", V1_COLLECTION, 1.0),
                               ("POST", V2_COLLECTION, "2 "), ("POST", V1_COLLECTION, True)):
            with self.subTest(http=http, ds=ds):
                doc = FakeScopeDoc(method_patterns=["Sales Invoice.submit", "Sales Invoice.cancel"],
                                   enforce_workflow=1, **WRITE_NO_METHODS)
                enforce.frappe = FakeFrappe(
                    headers=AUTH, session_user="alice@x", scopes={"alice@x": doc},
                    request=_BodyReq(path, http, ""), form_dict={"docstatus": ds},
                    workflows={"Sales Invoice": "SI Approval"})
                with self.assertRaises(FakePermissionError):
                    enforce.check_scope()
                self.assertIn("workflow bypass", enforce.frappe.denial_logs[0][0])

    def test_a_bom_prefixed_body_is_refused_at_the_scope_check(self):
        doc = FakeScopeDoc(**WRITE_NO_METHODS)
        enforce.frappe = FakeFrappe(
            headers=AUTH, session_user="alice@x", scopes={"alice@x": doc},
            request=_BodyReq(V1, "PUT", '\ufeff{"docstatus": 1}'), form_dict={})
        with self.assertRaises(FakePermissionError):
            enforce.check_scope()


class TestFrappeWiring(unittest.TestCase):
    """``resource_write_bodies`` mirrors frappe source. If frappe changes it, this goes red."""

    REFS = os.environ.get("PACIOLI_FRAPPE_REFS", "")

    def setUp(self):
        if not self.REFS:
            self.skipTest("PACIOLI_FRAPPE_REFS is unset — the frappe WIRING claim is UNVERIFIED "
                          "in this run (point it at a frappe checkout to verify)")
        self.api = pathlib.Path(self.REFS) / "frappe" / "api"

    @staticmethod
    def _squash(text):
        return "".join(text.split())

    def test_v1_handlers_take_the_body_from_get_request_form_data(self):
        src = self._squash((self.api / "v1.py").read_text())
        self.assertIn(self._squash(
            "def get_request_form_data():\n"
            "    if frappe.form_dict.data is None:\n"
            "        data = frappe.safe_decode(frappe.request.get_data())\n"
            "    else:\n"
            "        data = frappe.form_dict.data\n"
            "    try:\n"
            "        return frappe.parse_json(data)\n"
            "    except ValueError:\n"
            "        return frappe.form_dict\n"), src)
        self.assertIn(self._squash(
            "def create_doc(doctype: str):\n    data = get_request_form_data()"), src)
        self.assertIn(self._squash(
            "def update_doc(doctype: str, name: str):\n    data = get_request_form_data()"), src)

    def test_v2_handlers_take_the_body_from_form_dict(self):
        src = self._squash((self.api / "v2.py").read_text())
        self.assertIn(self._squash(
            "def create_doc(doctype: str):\n    data = frappe.form_dict"), src)
        self.assertIn(self._squash(
            "def update_doc(doctype: str, name: str):\n    data = frappe.form_dict"), src)

    def test_save_turns_into_insert_on_islocal_or_a_missing_name(self):
        model = pathlib.Path(self.REFS) / "frappe" / "model"
        self.assertIn(self._squash(
            'if self.get("__islocal") or not self.get("name"):\n    return self.insert()'),
            self._squash((model / "document.py").read_text()))
        base = (model / "base_document.py").read_text()
        reserved = base[base.index("RESERVED_KEYWORDS = frozenset("):]
        reserved = reserved[:reserved.index("\n)\n")]
        self.assertIn('"doctype"', reserved)
        self.assertNotIn('"name"', reserved)
        self.assertNotIn("__islocal", reserved)

    def test_v2_routes_the_path_carried_method_for_get_and_post_only(self):
        src = self._squash((self.api / "v2.py").read_text())
        self.assertIn(self._squash(
            'Rule(\n"/document/<doctype>/<path:name>/method/<method>/",\n'
            'methods=["GET", "POST"],\nendpoint=execute_doc_method,\n)'), src)
        self.assertIn(self._squash(
            'Rule("/document/<doctype>/<path:name>/", methods=["PATCH", "PUT"], '
            'endpoint=update_doc)'), src)

    def test_the_router_does_not_merge_slashes(self):
        self.assertIn("merge_slashes=False",
                      self._squash((self.api / "__init__.py").read_text()))

    def test_a_preset_action_survives_and_discard_skips_the_transition_check(self):
        doc = self._squash((pathlib.Path(self.REFS) / "frappe" / "model" / "document.py").read_text())
        self.assertIn(self._squash('if not hasattr(self, "_action"):\n    self._action = "save"'), doc)
        self.assertIn(self._squash(
            'if not self.meta.issingle and self._action != "discard":\n'
            '    self.check_docstatus_transition(previous.docstatus)'), doc)


if __name__ == "__main__":
    unittest.main()
