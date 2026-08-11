# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""``api.py`` is the operator's only window onto their own grant. These pin it against inverting.

**Two behavioural defects, found 2026-08-11 by an independent review of a docstring pass.** Both
were caught because a docstring disagreed with its code — and in both cases the CODE was the thing
that was wrong, not the prose.

1. ``_resource_posture`` never read ``allow_all_doctypes``. A grant of
   ``allow_resource=1, allow_all_doctypes=1`` with an empty child table is the **widest** access
   this app can express — ``is_permitted`` returns True for every doctype — and the posture report
   called it **``denies_all``**, the narrowest of its four states. Its own docstring says *"an
   operator should never be quietly unaware of how wide their own grant is, in either direction"*,
   and it inverted the answer in precisely the direction that matters.

2. ``CONSENT_HANDLERS`` named 2 of the 4 enforcing handlers. ``hooks.py`` has registered
   ``before_gl_preview`` and ``before_sl_preview`` since 0.13.0, and both DENY (``act.py``'s
   ``_deny("consent (preview)", …)``). ``_gate_registered`` therefore reported the gate loaded on
   a site carrying only the pre-0.13.0 pair — ⭐ **which is exactly the stale-hooks-cache shape the
   probe was written to catch.** The 2026-07-29 incident was a site whose cached registry predated
   the handlers it needed; a site whose cache predates 0.13.0 is the same failure, one release
   later, and the receipt would have said "registered".
"""
import sys
import types
import unittest

_frappe = sys.modules.setdefault("frappe", types.ModuleType("frappe"))
# `api.py` evaluates `@frappe.whitelist()` at import time, so the stub needs a passthrough —
# same shim `test_consent.py` installs, kept idempotent so import order does not matter.
if not hasattr(_frappe, "whitelist"):
    _frappe.whitelist = lambda *a, **k: (lambda fn: fn)

from pacioli_guard import api  # noqa: E402
from pacioli_guard.scope import ApiScope, is_permitted  # noqa: E402


class FakeDoc:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _ApiHarness(unittest.TestCase):
    """Point `api.frappe` at a fake and restore it exactly — never `del`."""

    def _set(self, obj, name, value):
        missing = object()
        previous = getattr(obj, name, missing)
        setattr(obj, name, value)
        if previous is missing:
            self.addCleanup(lambda: delattr(obj, name) if hasattr(obj, name) else None)
        else:
            self.addCleanup(setattr, obj, name, previous)


class TestResourcePostureNeverUnderstatesTheGrant(_ApiHarness):
    def _posture(self, **scope_fields):
        doc = FakeDoc(**scope_fields)
        self._set(api.frappe, "db", types.SimpleNamespace(
            get_value=lambda dt, filters, field=None, **k: "SCOPE-1"))
        self._set(api.frappe, "get_doc", lambda dt, name: doc)
        return api._resource_posture("broker@example.com")

    def test_allow_all_doctypes_with_an_empty_table_reports_all_doctypes(self):
        """THE DEFECT. The widest grant the app can express must not read as the narrowest."""
        self.assertEqual(
            self._posture(allow_resource=1, allow_all_doctypes=1, resource_doctypes=[]),
            "all_doctypes")

    def test_that_grant_really_is_the_widest_one(self):
        """Guard-the-guard: the test above is only meaningful if this shape genuinely permits
        everything. Asserted through the real decision core, not assumed."""
        widest = ApiScope.from_dict({"allow_resource": 1, "allow_all_doctypes": 1,
                                     "resource_doctypes": [], "resource_verbs": ["read"]})
        for doctype in ("Sales Invoice", "Journal Entry", "Stock Entry", "Anything At All"):
            with self.subTest(doctype=doctype):
                self.assertTrue(is_permitted(widest, "resource", (doctype, "read")))

    def test_allow_all_doctypes_wins_even_when_rows_are_also_named(self):
        """The flag grants everything regardless of the child table, so the posture must say so
        rather than reporting the (irrelevant) narrower list."""
        self.assertEqual(
            self._posture(allow_resource=1, allow_all_doctypes=1,
                          resource_doctypes=[FakeDoc(ref_doctype="Sales Invoice")]),
            "all_doctypes")

    # -- the states that were already right, kept so the fix cannot break them ----------

    def test_resource_branch_off_is_still_off(self):
        self.assertEqual(self._posture(allow_resource=0, allow_all_doctypes=1), "off")

    def test_a_genuinely_empty_allowlist_is_still_denies_all(self):
        self.assertEqual(
            self._posture(allow_resource=1, allow_all_doctypes=0, resource_doctypes=[]),
            "denies_all")

    def test_a_named_list_is_still_narrow(self):
        self.assertEqual(
            self._posture(allow_resource=1, allow_all_doctypes=0,
                          resource_doctypes=[FakeDoc(ref_doctype="Sales Invoice")]),
            "narrow")


class TestTheGateReceiptCountsEveryEnforcingHandler(_ApiHarness):
    def _registered(self, events):
        self._set(api.frappe, "get_hooks", lambda name: {"*": events})
        return api._gate_registered()

    def _all_four(self):
        from pacioli_guard import hooks
        return {event: handler for event, handler in hooks.doc_events["*"].items()
                if event != "after_insert"}

    def test_CONSENT_HANDLERS_names_every_denying_handler_hooks_registers(self):
        """Derived from hooks.py, so it cannot be satisfied by editing one list to match a
        hardcoded copy of the other. `after_insert` is excluded because hooks.py's own comment
        says it decides nothing and refuses nothing."""
        self.assertEqual(dict(api.CONSENT_HANDLERS), self._all_four())

    def test_a_site_missing_the_PREVIEW_handlers_is_not_reported_as_registered(self):
        """THE DEFECT, and it is the stale-hooks-cache shape one release later: a cache from
        before 0.13.0 carries the submit/cancel pair and neither preview gate."""
        pre_0_13_0 = {"before_submit": "pacioli_guard.act.before_submit",
                      "before_cancel": "pacioli_guard.act.before_cancel"}
        self.assertFalse(self._registered(pre_0_13_0))

    def test_a_fully_registered_site_is_reported_as_registered(self):
        self.assertTrue(self._registered(self._all_four()))

    def test_a_site_missing_before_submit_is_still_not_registered(self):
        events = self._all_four()
        events.pop("before_submit")
        self.assertFalse(self._registered(events))

    def test_a_handler_registered_under_the_wrong_name_does_not_count(self):
        events = {event: "some.other.app.handler" for event in self._all_four()}
        self.assertFalse(self._registered(events))

    def test_it_stays_deny_biased_when_it_cannot_look(self):
        def boom(_name):
            raise RuntimeError("no site context")
        self._set(api.frappe, "get_hooks", boom)
        self.assertFalse(api._gate_registered())
