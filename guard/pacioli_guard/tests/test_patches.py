"""Bench-free tests for the 0.6.0 migrate-audit patch's PURE heuristic (`grant_warning`).

The patch's `execute()` (frappe iteration + logging) is deliberately untestable here — it needs a
bench, is wrapped whole in try/except, and its live behavior is a Gate 10 pin. What IS testable is
the static classification of a single `methods` grant pattern, which is the entire substance of the
audit: everything else is plumbing.
"""
import unittest

from pacioli_guard.patches.v0_6_0.warn_unresolved_method_grants import grant_warning
from pacioli_guard.scope import SAFE_METHODS


class TestGrantWarning(unittest.TestCase):
    # -- patterns the audit must stay SILENT on (still fire under deny-unknown) --

    def test_every_safe_method_is_silent(self):
        for name in SAFE_METHODS:
            with self.subTest(name=name):
                self.assertIsNone(grant_warning(name))

    def test_per_doctype_grant_is_silent(self):
        self.assertIsNone(grant_warning("Sales Invoice.submit"))

    def test_per_doctype_grant_with_method_glob_is_silent(self):
        # "Sales Invoice.*" — the doctype-part is concrete; only the METHOD is globbed. Still a
        # per-doctype grant (fires on resolved targets), so no warning.
        self.assertIsNone(grant_warning("Sales Invoice.*"))

    def test_single_word_doctype_grant_is_silent(self):
        # Uppercase single-word doctype (e.g. "Customer.get_pdf") — no space, but title-case.
        self.assertIsNone(grant_warning("Customer.get_pdf"))

    def test_apply_workflow_regrant_shape_is_silent(self):
        self.assertIsNone(grant_warning("Journal Entry.apply_workflow"))

    def test_surrounding_whitespace_warns_because_enforcement_does_not_strip(self):
        # Redteam (mechanical lens #2): _clean_allowlist keeps the UNSTRIPPED pattern, so a padded
        # row is dead at enforcement (fnmatch/SAFE membership are exact) — the audit must flag it,
        # not silently strip-and-approve it.
        msg = grant_warning("  Sales Invoice.submit  ")
        self.assertIsNotNone(msg)
        self.assertIn("whitespace", msg)

    # -- patterns the audit must WARN on (deny-unknown stops honoring them bare) --

    def test_bare_name_warns(self):
        self.assertIsNotNone(grant_warning("run_doc_method"))

    def test_dotted_rpc_module_path_warns(self):
        self.assertIsNotNone(grant_warning("frappe.client.submit"))

    def test_safe_method_glob_is_not_exact_and_warns(self):
        # SAFE_METHODS membership is EXACT-name; a glob that would fnmatch a member is not one.
        self.assertIsNotNone(grant_warning("frappe.auth.*"))

    def test_wildcard_doctype_part_warns(self):
        self.assertIsNotNone(grant_warning("*.submit"))
        self.assertIsNotNone(grant_warning("*"))

    def test_bulk_update_hard_deny_warns_even_though_title_case(self):
        # "Bulk Update.<anything>" is UNGRANTABLE as of 0.6.0 — it must warn despite looking like
        # a plausible per-doctype grant, and the message should say it is denied, not just bare.
        msg = grant_warning("Bulk Update.bulk_update")
        self.assertIsNotNone(msg)
        self.assertIn("hard-den", msg)

    def test_capitalized_rpc_module_path_warns(self):
        # Redteam (mechanical lens #3): a Title-cased custom-app RPC module path (e.g.
        # "MyApp.api.do_thing") used to slip the head[0].isupper() check and get NO warning, though
        # it only ever fired bare and 0.6.0 now denies it. A dot in the pre-method part = module path.
        msg = grant_warning("MyApp.api.do_thing")
        self.assertIsNotNone(msg)
        self.assertIn("module path", msg)

    def test_dotted_method_bulk_update_still_flagged_as_ungrantable(self):
        # Matches is_permitted's split-on-first hard-deny extraction: a dotted method name must not
        # slide "Bulk Update" past the audit either.
        msg = grant_warning("Bulk Update.x.bulk_update")
        self.assertIsNotNone(msg)
        self.assertIn("hard-den", msg)

    def test_empty_and_non_string_warn(self):
        self.assertIsNotNone(grant_warning(""))
        self.assertIsNotNone(grant_warning("   "))
        self.assertIsNotNone(grant_warning(None))
        self.assertIsNotNone(grant_warning(42))


if __name__ == "__main__":
    unittest.main()
