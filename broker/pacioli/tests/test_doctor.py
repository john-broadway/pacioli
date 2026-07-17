# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Doctor tests — read-only readiness checks, bench-free via the injected transport."""
import os
import tempfile
import unittest
from pathlib import Path

from pacioli.doctor import (FAIL, OK, WARN, check_credentials, check_registry, check_state,
                            probe_accounting_period_read, probe_accounts_settings,
                            probe_belt_exemptions, probe_bench, probe_company_read,
                            probe_gl_entry_read, probe_payment_ledger_read, probe_pcv_read,
                            probe_repost_read, probe_roles, probe_workflow_read, run_doctor)
from pacioli.registry import load_registry

REG = ('[targets.bench]\nbase_url = "https://erp.example.com"\n'
       'api_key = "brokerkey"\napi_secret = "env:S"\ncompany = "Example Co"\ndefault = true\n')


def _write_registry(dirname, text=REG):
    p = Path(dirname) / "registry.toml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def _transport_returning(status, payload):
    def transport(method, url, headers, params=None, body=None):
        transport.called = (method, url, headers)
        return status, payload
    return transport


def _queue_transport(responses):
    """A transport that answers each successive call from a fixed queue, in order — needed once a
    probe makes MORE than one call (F-S1's Accounting Period item-GET leg): unlike
    ``_transport_returning``, which answers every call identically and would misreport an item GET
    with the LIST's own list-shaped body, this lets a test hand the LIST and item-GET calls their
    own distinct responses, exactly like ``FakeTransport`` in ``test_erpnext.py``."""
    responses = list(responses)
    def transport(method, url, headers, params=None, body=None):
        transport.calls = getattr(transport, "calls", []) + [(method, url)]
        return responses.pop(0)
    return transport


def _routing_transport(routes):
    """Doctor's online path now makes THREE different probes (the method probe, the Company-read
    probe, and the workflow-read probe) — route by url substring so each can answer differently."""
    def transport(method, url, headers, params=None, body=None):
        transport.calls = getattr(transport, "calls", []) + [(method, url)]
        for fragment, (status, payload) in routes.items():
            if fragment in url:
                return status, payload
        return 404, None
    return transport


# A fully-ready set of probe answers: the method probe scoped tighter (403 = the prescribed
# posture), the Company DocType readable (200 = the v16 frozen-books grant present), the workflow
# list readable (200 = the required workflow-SoD read grant present), Accounts Settings readable
# (JE cancel blast-radius disclosure), PCV/Accounting Period readable (E6 — both required by
# get_period_locks, previously unprobed), and Payment Ledger Entry readable (F-R1 — the
# settling-PE disclosure, required for every supported doctype's plan_cancel/plan_cascade_cancel).
READY_ROUTES = {"/api/method/": (403, {}),
                # the belt-exemptions probe item-GETs the pinned company (route BEFORE the LIST
                # fragment — insertion order wins in _routing_transport)
                "/api/resource/Company/Example%20Co": (200, {"data": {"name": "Example Co"}}),
                "/api/resource/Company": (200, {"data": []}),
                "/api/resource/Workflow": (200, {"data": []}),
                "/api/resource/Accounts%20Settings": (
                    200, {"data": {"unlink_payment_on_cancellation_of_invoice": 1}}),
                "/api/resource/Period%20Closing%20Voucher": (200, {"data": []}),
                "/api/resource/Accounting%20Period": (200, {"data": []}),
                "/api/resource/Payment%20Ledger%20Entry": (200, {"data": []}),
                # Half-2 reconciliation reads (GL-Entry-read is required; Repost-read is the one
                # WARN-on-403 probe — both readable here for the fully-ready fixture).
                "/api/resource/GL%20Entry": (200, {"data": []}),
                "/api/resource/Repost%20Accounting%20Ledger": (200, {"data": []}),
                "/api/v2/method/User/get_roles": (
                    200, {"data": ["Accounts User", "Pacioli Seat", "All", "Guest", "Desk User"]})}


class TestCheckRegistry(unittest.TestCase):
    def test_missing_env_fails(self):
        findings, registry = check_registry({})
        self.assertIsNone(registry)
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("PACIOLI_REGISTRY", findings[0][1])

    def test_missing_file_fails(self):
        findings, registry = check_registry({"PACIOLI_REGISTRY": "/nope/registry.toml"})
        self.assertIsNone(registry)
        self.assertEqual(findings[0][0], FAIL)

    def test_valid_registry_reports_targets_and_default(self):
        with tempfile.TemporaryDirectory() as d:
            findings, registry = check_registry({"PACIOLI_REGISTRY": _write_registry(d)})
        self.assertIsNotNone(registry)
        self.assertEqual(findings[0][0], OK)
        self.assertIn("bench*", findings[0][1])

    def test_two_targets_no_default_warns(self):
        reg = ('[targets.a]\nbase_url = "https://a.example.com"\n'
               'api_key = "k"\napi_secret = "env:S"\n'
               '[targets.b]\nbase_url = "https://b.example.com"\n'
               'api_key = "k"\napi_secret = "env:S"\n')
        with tempfile.TemporaryDirectory() as d:
            findings, registry = check_registry({"PACIOLI_REGISTRY": _write_registry(d, reg)})
        self.assertIsNotNone(registry)
        self.assertIn(WARN, [level for level, _ in findings])


class TestCheckCredentials(unittest.TestCase):
    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)

    def test_resolving_reference_reports_ok_without_the_value(self):
        findings = check_credentials(self.target, {"S": "supersecret"}, lambda p: "")
        self.assertTrue(all(level == OK for level, _ in findings))
        self.assertFalse(any("supersecret" in msg for _, msg in findings))

    def test_unset_env_reference_fails_without_the_value(self):
        findings = check_credentials(self.target, {}, lambda p: "")
        self.assertIn(FAIL, [level for level, _ in findings])


class TestCheckState(unittest.TestCase):
    def test_missing_state_dir_env_fails(self):
        self.assertEqual(check_state({}, "bench")[0][0], FAIL)

    def test_fresh_install_is_ok_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            state = str(Path(d) / "state")
            findings = check_state({"PACIOLI_STATE_DIR": state}, "bench")
            self.assertFalse(Path(state).exists())  # read-only: nothing created
        self.assertNotIn(FAIL, [level for level, _ in findings])
        self.assertTrue(any("will be" in msg for _, msg in findings))

    def test_lax_seal_key_mode_fails(self):
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / "seal.key"
            key.write_bytes(b"x" * 32)
            os.chmod(key, 0o644)
            findings = check_state({"PACIOLI_STATE_DIR": d}, "bench")
        self.assertTrue(any(level == FAIL and "0600" in msg for level, msg in findings))


class TestProbeBench(unittest.TestCase):
    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)
        self.env = {"S": "supersecret"}

    def _probe(self, status, payload):
        return probe_bench(self.target, self.env, lambda p: "",
                           _transport_returning(status, payload))

    def test_scoped_403_is_the_prescribed_posture_and_passes(self):
        findings = self._probe(403, {"exc_type": "PermissionError"})
        self.assertEqual(findings[0][0], OK)
        self.assertIn("scoped tighter", findings[0][1])

    def test_administrator_is_a_failure_not_a_warning(self):
        findings = self._probe(200, {"message": "Administrator"})
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("Administrator", findings[0][1])

    def test_guest_means_the_credential_did_not_authenticate(self):
        self.assertEqual(self._probe(200, {"message": "Guest"})[0][0], FAIL)

    def test_rejected_credential_fails(self):
        self.assertEqual(self._probe(401, None)[0][0], FAIL)

    def test_named_user_passes_with_a_scope_note(self):
        findings = self._probe(200, {"message": "broker@example.com"})
        self.assertEqual(findings[0][0], OK)
        self.assertIn("broker@example.com", findings[0][1])
        self.assertEqual(findings[1][0], WARN)

    def test_unreachable_bench_fails_never_tracebacks(self):
        def transport(method, url, headers, params=None, body=None):
            raise OSError("no route to host")
        findings = probe_bench(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[0][0], FAIL)

    def test_probe_never_leaks_the_secret_into_findings(self):
        for status, payload in ((403, {}), (401, None), (200, {"message": "Administrator"}),
                                (500, None)):
            for _, msg in self._probe(status, payload):
                self.assertNotIn("supersecret", msg)


class TestProbeCompanyRead(unittest.TestCase):
    """The Company-read probe (v16 spine fix) — required since get_period_locks now reads
    Company.accounts_frozen_till_date as the frozen-books boundary's primary source. Same
    deliberate inversion as TestProbeWorkflowRead: 403 here is a FAIL, because the closed-books check
    REQUIRES this read (unlike probe_bench's method probe, where 403 is the prescribed PASS)."""

    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)
        self.env = {"S": "supersecret"}

    def _probe(self, status, payload):
        return probe_company_read(self.target, self.env, lambda p: "",
                                  _transport_returning(status, payload))

    def test_readable_even_if_empty_passes(self):
        findings = self._probe(200, {"data": []})
        self.assertEqual(findings[0][0], OK)

    def test_readable_with_companies_passes(self):
        findings = self._probe(200, {"data": [{"name": "Example Corp"}]})
        self.assertEqual(findings[0][0], OK)

    def test_403_fails_with_the_exact_remedy(self):
        findings = self._probe(403, {"exc_type": "PermissionError"})
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("grant read permission on Company", findings[0][1])
        self.assertIn("accounts_frozen_till_date", findings[0][1])

    def test_other_errors_fail_with_the_reason(self):
        findings = self._probe(500, None)
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("500", findings[0][1])

    def test_unexpected_200_body_fails_not_passes(self):
        findings = self._probe(200, {"message": "not-a-data-envelope"})
        self.assertEqual(findings[0][0], FAIL)

    def test_unreachable_bench_fails_never_tracebacks(self):
        def transport(method, url, headers, params=None, body=None):
            raise OSError("no route to host")
        findings = probe_company_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[0][0], FAIL)

    def test_probe_never_leaks_the_secret_into_findings(self):
        for status, payload in ((200, {"data": []}), (403, {}), (500, None)):
            for _, msg in self._probe(status, payload):
                self.assertNotIn("supersecret", msg)

    def test_probe_hits_the_company_resource_url(self):
        transport = _transport_returning(200, {"data": []})
        probe_company_read(self.target, self.env, lambda p: "", transport)
        _, url, _ = transport.called
        self.assertIn("/api/resource/Company", url)


class TestProbeRoles(unittest.TestCase):
    """The over-privilege probe (the tight-role seat). Reads the seat's OWN roles via the v2
    doctype-resolved method route (``/api/v2/method/User/get_roles`` — guard-grantable by config
    without a guard code change, unlike the bare dotted method) and refuses a spine-voiding seat.

    Extends :func:`probe_bench`'s "Administrator is a failure" doctrine from the literal *username*
    to the administrative *role*: a seat carrying **System Manager** can administer DocPerms,
    install/remove apps (including ``pacioli_guard`` itself), and mint API keys — it can dismantle
    the governance it is supposed to operate under, so doctor refuses to call the install ready.
    Deny-biased like :func:`probe_payment_ledger_read` (a required read): 403 / unparseable /
    empty is a FAIL, never a silent pass — NOT :func:`probe_bench`'s 403-is-PASS posture, because
    an un-auditable seat cannot be certified least-privilege."""

    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)
        self.env = {"S": "supersecret"}

    def _probe(self, status, payload):
        return probe_roles(self.target, self.env, lambda p: "",
                           _transport_returning(status, payload))

    def test_system_manager_role_fails(self):
        findings = self._probe(200, {"data": ["Accounts User", "System Manager", "All"]})
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("System Manager", findings[0][1])

    def test_administrator_role_fails(self):
        # Defensive: the literal Administrator user holds every role; should it ever surface as a
        # role string it is the same spine-void probe_bench catches by username.
        self.assertEqual(self._probe(200, {"data": ["Administrator", "All"]})[0][0], FAIL)

    def test_clean_minimal_seat_passes_without_warning(self):
        findings = self._probe(200, {"data": ["Accounts User", "Pacioli Seat",
                                              "All", "Guest", "Desk User"]})
        levels = [level for level, _ in findings]
        self.assertIn(OK, levels)
        self.assertNotIn(FAIL, levels)
        self.assertNotIn(WARN, levels)

    def test_accounts_manager_warns_but_does_not_fail(self):
        # Over-broad (delete, PCV-close, write on read-only doctypes) but not spine-voiding — the
        # broker never deletes and never closes a period. Surface it; don't refuse the install.
        findings = self._probe(200, {"data": ["Accounts User", "Accounts Manager", "All"]})
        levels = [level for level, _ in findings]
        self.assertNotIn(FAIL, levels)
        self.assertIn(WARN, levels)

    def test_system_manager_dominates_over_the_over_broad_warning(self):
        self.assertEqual(self._probe(200, {"data": ["Accounts Manager", "System Manager"]})[0][0],
                         FAIL)

    def test_403_fails_with_the_get_roles_grant_remedy(self):
        findings = self._probe(403, {"exc_type": "PermissionError"})
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("User.get_roles", findings[0][1])

    def test_other_errors_fail_with_the_reason(self):
        findings = self._probe(500, None)
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("500", findings[0][1])

    def test_unparseable_200_body_fails_not_passes(self):
        self.assertEqual(self._probe(200, {"message": "not-a-data-list"})[0][0], FAIL)

    def test_data_not_a_list_fails(self):
        self.assertEqual(self._probe(200, {"data": {"not": "a list"}})[0][0], FAIL)

    def test_empty_role_list_is_deny_biased_fail(self):
        # A real authenticated seat always carries at least All/Guest; an empty list is anomalous
        # and cannot certify least-privilege — deny-biased.
        self.assertEqual(self._probe(200, {"data": []})[0][0], FAIL)

    def test_whitespace_padded_spine_voiding_role_still_fails(self):
        # Defense-in-depth: a padded "  System Manager  " must not evade the exact-name match.
        findings = self._probe(200, {"data": ["Accounts User", "  System Manager  ", "All"]})
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("System Manager", findings[0][1])

    def test_blank_or_whitespace_only_list_is_deny_biased_fail(self):
        # After strip+drop-blanks nothing real remains → the deny-biased empty-list FAIL, not a
        # silent OK on a list of blanks.
        self.assertEqual(self._probe(200, {"data": ["", "   "]})[0][0], FAIL)

    def test_unreachable_bench_fails_never_tracebacks(self):
        def transport(method, url, headers, params=None, body=None):
            raise OSError("no route to host")
        findings = probe_roles(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[0][0], FAIL)

    def test_probe_never_leaks_the_secret_into_findings(self):
        for status, payload in ((200, {"data": ["System Manager"]}),
                                (200, {"data": ["Accounts User", "All"]}),
                                (403, {}), (500, None)):
            for _, msg in self._probe(status, payload):
                self.assertNotIn("supersecret", msg)

    def test_probe_hits_the_v2_get_roles_url(self):
        transport = _transport_returning(200, {"data": ["Accounts User", "All"]})
        probe_roles(self.target, self.env, lambda p: "", transport)
        _, url, _ = transport.called
        self.assertIn("/api/v2/method/User/get_roles", url)


def _param_routing_transport(routes):
    """Like ``_routing_transport`` but records ``params`` per call — probe_belt_exemptions' LIST
    page-pins (the F-V1 lesson) are asserted off the recorded params, not trusted silently."""
    def transport(method, url, headers, params=None, body=None):
        transport.calls = getattr(transport, "calls", []) + [(method, url, params)]
        for fragment, (status, payload) in routes.items():
            if fragment in url:
                return status, payload
        return 404, None
    return transport


class TestProbeBeltExemptions(unittest.TestCase):
    """The belt-exemption drift watch (docs/plans/2026-07-09-probe-belt-exemptions.md): the three
    STOCK fields that silently disable ERPNext's frozen/closed-period belts for a role —
    ``Accounting Period.exempted_role``, ``Company.role_allowed_for_frozen_entries`` (v16), and
    legacy ``Accounts Settings.frozen_accounts_modifier`` (≤v15) — cross-referenced against the
    seat's OWN roles. Deny-biased: any unreadable source is a FAIL. Version-safe by construction:
    every field is read off a FULL document with ``.get()`` (absent → blank), never selected in a
    LIST ``fields`` clause (the F-C1 v15 lesson)."""

    ROLES = (200, {"data": ["Accounts User", "Pacioli Seat", "All", "Guest", "Desk User"]})

    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)  # company-pinned: "Example Co"
        self.env = {"S": "supersecret"}

    def _probe(self, routes):
        transport = _param_routing_transport(routes)
        findings = probe_belt_exemptions(self.target, self.env, lambda p: "", transport)
        return findings, transport

    def _routes(self, *, roles=None, company_doc=None, settings_doc=None,
                ap_list=None, ap_docs=None):
        """Route table for the pinned-target call shape; item routes FIRST (fragment routing
        matches in insertion order, and the item URLs contain the LIST URL as a prefix)."""
        routes = {}
        for name, doc in (ap_docs or {}).items():
            routes[f"/api/resource/Accounting%20Period/{name}"] = (200, {"data": doc})
        routes["/api/resource/Accounting%20Period"] = ap_list or (200, {"data": []})
        routes["/api/resource/Accounts%20Settings"] = settings_doc or (200, {"data": {}})
        routes["/api/resource/Company/Example%20Co"] = company_doc or (200, {"data": {"name": "Example Co"}})
        routes["/api/v2/method/User/get_roles"] = roles or self.ROLES
        return routes

    def test_all_blank_everywhere_is_ok(self):
        findings, _ = self._probe(self._routes())
        self.assertEqual([level for level, _ in findings], [OK])
        self.assertIn("no role escape hatch", findings[0][1])

    def test_absent_fields_count_as_blank_the_v15_shape(self):
        # A v15 Company doc has no role_allowed_for_frozen_entries at all; a v16 Accounts
        # Settings has no frozen_accounts_modifier — absence is BLANK, never an error (F-C1:
        # full-doc .get(), no LIST fields selection to blow up on the missing column).
        findings, _ = self._probe(self._routes(
            company_doc=(200, {"data": {"name": "Example Co"}}),
            settings_doc=(200, {"data": {"unlink_payment_on_cancellation_of_invoice": 1}})))
        self.assertEqual([level for level, _ in findings], [OK])

    def test_ap_exemption_held_by_seat_fails_naming_period_and_role(self):
        findings, _ = self._probe(self._routes(
            ap_list=(200, {"data": [{"name": "AP1"}]}),
            ap_docs={"AP1": {"name": "AP1", "exempted_role": "Accounts User"}}))
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("AP1", findings[0][1])
        self.assertIn("Accounts User", findings[0][1])
        self.assertIn("does not fire", findings[0][1])

    def test_ap_exemption_not_held_warns(self):
        findings, _ = self._probe(self._routes(
            ap_list=(200, {"data": [{"name": "AP1"}]}),
            ap_docs={"AP1": {"name": "AP1", "exempted_role": "Night Auditor"}}))
        levels = [level for level, _ in findings]
        self.assertIn(WARN, levels)
        self.assertNotIn(FAIL, levels)
        warn = next(msg for level, msg in findings if level == WARN)
        self.assertIn("Night Auditor", warn)
        self.assertIn("Administrator", warn)  # the no-anti-Admin-carve-out note

    def test_exempted_role_all_fails_the_auto_role_hazard(self):
        # exempted_role="All" exempts EVERY authenticated seat — the cross-ref set deliberately
        # keeps the frappe-auto roles (unlike probe_roles' display filter), so this is a FAIL.
        findings, _ = self._probe(self._routes(
            ap_list=(200, {"data": [{"name": "AP1"}]}),
            ap_docs={"AP1": {"name": "AP1", "exempted_role": "All"}}))
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("All", findings[0][1])

    def test_company_frozen_role_held_fails(self):
        findings, _ = self._probe(self._routes(
            company_doc=(200, {"data": {"name": "Example Co",
                                        "role_allowed_for_frozen_entries": "Pacioli Seat"}})))
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("Example Co", findings[0][1])
        self.assertIn("Pacioli Seat", findings[0][1])

    def test_legacy_accounts_settings_modifier_not_held_warns(self):
        findings, _ = self._probe(self._routes(
            settings_doc=(200, {"data": {"frozen_accounts_modifier": "Accounts Manager"}})))
        levels = [level for level, _ in findings]
        self.assertIn(WARN, levels)
        self.assertNotIn(FAIL, levels)

    def test_held_and_unheld_exemptions_produce_fail_plus_warn(self):
        findings, _ = self._probe(self._routes(
            company_doc=(200, {"data": {"name": "Example Co",
                                        "role_allowed_for_frozen_entries": "Night Auditor"}}),
            ap_list=(200, {"data": [{"name": "AP1"}]}),
            ap_docs={"AP1": {"name": "AP1", "exempted_role": "Accounts User"}}))
        levels = [level for level, _ in findings]
        self.assertIn(FAIL, levels)
        self.assertIn(WARN, levels)

    def test_whitespace_padded_exemption_still_caught(self):
        findings, _ = self._probe(self._routes(
            ap_list=(200, {"data": [{"name": "AP1"}]}),
            ap_docs={"AP1": {"name": "AP1", "exempted_role": "  Accounts User  "}}))
        self.assertEqual(findings[0][0], FAIL)

    def test_blank_or_whitespace_only_exemption_is_not_set(self):
        findings, _ = self._probe(self._routes(
            company_doc=(200, {"data": {"name": "Example Co",
                                        "role_allowed_for_frozen_entries": "   "}}),
            ap_list=(200, {"data": [{"name": "AP1"}]}),
            ap_docs={"AP1": {"name": "AP1", "exempted_role": ""}}))
        self.assertEqual([level for level, _ in findings], [OK])

    def test_roles_403_fails_with_the_grant_remedy(self):
        findings, _ = self._probe(self._routes(roles=(403, {})))
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("User.get_roles", findings[0][1])

    def test_unreadable_company_doc_fails_deny_biased(self):
        findings, _ = self._probe(self._routes(company_doc=(500, None)))
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("Company", findings[0][1])

    def test_unreadable_ap_list_fails_deny_biased(self):
        findings, _ = self._probe(self._routes(ap_list=(403, {"exc_type": "PermissionError"})))
        self.assertEqual(findings[0][0], FAIL)

    def test_unreadable_ap_item_fails_deny_biased(self):
        findings, _ = self._probe(self._routes(
            ap_list=(200, {"data": [{"name": "AP1"}]}),
            ap_docs={}))  # item GET falls through to the 404 default
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("AP1", findings[0][1])

    def test_non_string_exemption_value_fails_deny_biased(self):
        findings, _ = self._probe(self._routes(
            ap_list=(200, {"data": [{"name": "AP1"}]}),
            ap_docs={"AP1": {"name": "AP1", "exempted_role": 7}}))
        self.assertEqual(findings[0][0], FAIL)

    def test_lists_carry_the_page_pin(self):
        # F-V1: an unpinned LIST silently truncates at 20 rows — a closing period past row 20
        # would be invisible to the probe. Every LIST this probe makes must send
        # limit_page_length "0".
        _, transport = self._probe(self._routes(
            ap_list=(200, {"data": [{"name": "AP1"}]}),
            ap_docs={"AP1": {"name": "AP1"}}))
        lists = [params for method, url, params in transport.calls
                 if params is not None and "fields" in (params or {})]
        self.assertTrue(lists, "expected at least one LIST call with params")
        for params in lists:
            self.assertEqual(params.get("limit_page_length"), "0")

    def test_pinned_target_reads_only_the_pinned_company(self):
        _, transport = self._probe(self._routes())
        company_calls = [url for _, url, _ in transport.calls if "/api/resource/Company" in url]
        self.assertEqual(company_calls, [f"{self.target.base_url}/api/resource/Company/Example%20Co"])

    def test_unpinned_target_lists_and_reads_every_company(self):
        reg = ('[targets.bench]\nbase_url = "https://erp.example.com"\n'
               'api_key = "brokerkey"\napi_secret = "env:S"\ndefault = true\n')
        target = load_registry(toml_text=reg).get(None)
        routes = {"/api/resource/Company/Co%20A": (200, {"data": {"name": "Co A"}}),
                  "/api/resource/Company/Co%20B": (
                      200, {"data": {"name": "Co B",
                                     "role_allowed_for_frozen_entries": "Night Auditor"}}),
                  "/api/resource/Company": (200, {"data": [{"name": "Co A"}, {"name": "Co B"}]}),
                  "/api/resource/Accounting%20Period": (200, {"data": []}),
                  "/api/resource/Accounts%20Settings": (200, {"data": {}}),
                  "/api/v2/method/User/get_roles": self.ROLES}
        transport = _param_routing_transport(routes)
        findings = probe_belt_exemptions(target, self.env, lambda p: "", transport)
        levels = [level for level, _ in findings]
        self.assertIn(WARN, levels)
        self.assertTrue(any("Co B" in msg for _, msg in findings))

    def test_malformed_company_list_row_fails_never_silently_skips(self):
        # Deny-bias symmetry with the AP loop: a companies-LIST row without a name must FAIL the
        # probe, never silently drop that company from the audit (its exemption would go
        # unwatched while doctor reports ok).
        reg = ('[targets.bench]\nbase_url = "https://erp.example.com"\n'
               'api_key = "brokerkey"\napi_secret = "env:S"\ndefault = true\n')
        target = load_registry(toml_text=reg).get(None)
        routes = {"/api/resource/Company": (200, {"data": [{"nope": 1}]}),
                  "/api/v2/method/User/get_roles": self.ROLES}
        findings = probe_belt_exemptions(target, self.env, lambda p: "",
                                         _param_routing_transport(routes))
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("missing a name", findings[0][1])

    def test_transport_exception_mid_probe_fails_and_names_the_cause(self):
        def transport(method, url, headers, params=None, body=None):
            if "get_roles" in url:
                return 200, {"data": ["Accounts User", "All"]}
            raise ConnectionError("boom mid-probe")
        findings = probe_belt_exemptions(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("boom mid-probe", findings[0][1])

    def test_never_echoes_the_secret(self):
        for routes in (self._routes(), self._routes(roles=(403, {})),
                       self._routes(company_doc=(500, None))):
            findings, _ = self._probe(routes)
            for _, msg in findings:
                self.assertNotIn("supersecret", msg)


class TestProbeWorkflowRead(unittest.TestCase):
    """The workflow-read probe — the DELIBERATE INVERSION of probe_bench's 403 rule: there,
    403 = PASS (the write-side method probe scoped tighter is the prescribed posture); HERE,
    403 = FAIL, because the workflow-SoD gate REQUIRES the Workflow DocType to be readable —
    without the custom read grant every governed submit/cancel denies."""

    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)
        self.env = {"S": "supersecret"}

    def _probe(self, status, payload):
        return probe_workflow_read(self.target, self.env, lambda p: "",
                                   _transport_returning(status, payload))

    def test_readable_even_if_empty_passes(self):
        findings = self._probe(200, {"data": []})
        self.assertEqual(findings[0][0], OK)

    def test_readable_with_workflows_passes(self):
        findings = self._probe(200, {"data": [{"name": "SI Approval"}]})
        self.assertEqual(findings[0][0], OK)

    def test_403_fails_with_the_exact_remedy(self):
        findings = self._probe(403, {"exc_type": "PermissionError"})
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("grant custom read permission on Workflow", findings[0][1])
        self.assertIn("every submit/cancel denies", findings[0][1])

    def test_other_errors_fail_with_the_reason(self):
        findings = self._probe(500, None)
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("500", findings[0][1])

    def test_unexpected_200_body_fails_not_passes(self):
        findings = self._probe(200, {"message": "not-a-data-envelope"})
        self.assertEqual(findings[0][0], FAIL)

    def test_unreachable_bench_fails_never_tracebacks(self):
        def transport(method, url, headers, params=None, body=None):
            raise OSError("no route to host")
        findings = probe_workflow_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[0][0], FAIL)

    def test_probe_never_leaks_the_secret_into_findings(self):
        for status, payload in ((200, {"data": []}), (403, {}), (500, None)):
            for _, msg in self._probe(status, payload):
                self.assertNotIn("supersecret", msg)

    def test_probe_hits_the_workflow_resource_url(self):
        transport = _transport_returning(200, {"data": []})
        probe_workflow_read(self.target, self.env, lambda p: "", transport)
        _, url, _ = transport.called
        self.assertIn("/api/resource/Workflow", url)

    def test_loops_over_every_supported_doctype(self):
        # Breadth: the probe now checks readability for EACH doctype this broker supports (Sales
        # Invoice AND Purchase Invoice) — one finding per doctype, all readable here.
        from pacioli.erpnext import SUPPORTED_DOCTYPES
        calls = []

        def transport(method, url, headers, params=None, body=None):
            import json as _json
            calls.append(_json.loads(params["filters"])[0][2])  # the document_type filter value
            return 200, {"data": []}

        findings = probe_workflow_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(set(calls), set(SUPPORTED_DOCTYPES))
        self.assertEqual(len(findings), len(SUPPORTED_DOCTYPES))
        self.assertTrue(all(level == OK for level, _ in findings))

    def test_any_doctype_403_fails_naming_that_doctype(self):
        # 403 on ANY doctype in the set = the same FAIL+remedy for that doctype; the others still
        # report their own finding independently (never masked by one failure).
        def transport(method, url, headers, params=None, body=None):
            import json as _json
            doctype = _json.loads(params["filters"])[0][2]
            if doctype == "Purchase Invoice":
                return 403, {"exc_type": "PermissionError"}
            return 200, {"data": []}

        findings = probe_workflow_read(self.target, self.env, lambda p: "", transport)
        levels = [level for level, _ in findings]
        self.assertIn(FAIL, levels)
        self.assertIn(OK, levels)
        fail_msgs = [msg for level, msg in findings if level == FAIL]
        self.assertTrue(any("Purchase Invoice" in msg for msg in fail_msgs))
        self.assertTrue(any("grant custom read permission on Workflow" in msg
                            for msg in fail_msgs))


class TestProbeAccountsSettings(unittest.TestCase):
    """The Accounts Settings-read probe (envelope E5, widened by F-R1) — required since EVERY
    supported doctype's plan_cancel/plan_cascade_cancel now reads
    unlink_payment_on_cancellation_of_invoice (the settling-PE disclosure, not just Journal
    Entry's own EG-note/unlink flag), and an unreadable settings doc RAISES, refusing the whole
    plan. Same inversion as Company/Workflow: 403 is a FAIL — and, since F-R1, it refuses every
    doctype's cancel plan, not just Journal Entry's, which the remedy now names honestly."""

    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)
        self.env = {"S": "supersecret"}

    def _probe(self, status, payload):
        return probe_accounts_settings(self.target, self.env, lambda p: "",
                                       _transport_returning(status, payload))

    def test_readable_reports_the_unlink_state(self):
        findings = self._probe(200, {"data": {"unlink_payment_on_cancellation_of_invoice": 1}})
        self.assertEqual(findings[0][0], OK)
        self.assertIn("is ON", findings[0][1])

    def test_readable_off_reports_off(self):
        findings = self._probe(200, {"data": {"unlink_payment_on_cancellation_of_invoice": 0}})
        self.assertEqual(findings[0][0], OK)
        self.assertIn("is OFF", findings[0][1])

    def test_403_fails_naming_the_widened_f_r1_scope(self):
        # NAMED CASUALTY (F-R1, flips deliberately): this test used to be
        # test_403_fails_naming_the_je_cancel_scope, asserting the remedy said "other doctypes'
        # cancels are unaffected" — true before F-R1 (only Journal Entry's plan_cancel read
        # Accounts Settings), false after (every supported doctype's plan_cancel/
        # plan_cascade_cancel now reads it for the settling-PE disclosure). The remedy text is
        # updated to say so honestly instead of repeating a claim F-R1 made false.
        findings = self._probe(403, {"exc_type": "PermissionError"})
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("grant read permission on Accounts Settings", findings[0][1])
        self.assertIn("every supported doctype", findings[0][1])
        self.assertNotIn("other doctypes' cancels are unaffected", findings[0][1])

    def test_unexpected_200_body_fails(self):
        findings = self._probe(200, {"data": [1, 2, 3]})  # list, not the single dict
        self.assertEqual(findings[0][0], FAIL)

    def test_unreachable_never_tracebacks(self):
        def transport(method, url, headers, params=None, body=None):
            raise OSError("no route")
        findings = probe_accounts_settings(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[0][0], FAIL)

    def test_never_leaks_the_secret(self):
        for status, payload in ((200, {"data": {}}), (403, {}), (500, None)):
            for _, msg in self._probe(status, payload):
                self.assertNotIn("supersecret", msg)

    def test_hits_the_accounts_settings_single_url_percent_encoded(self):
        # Found live on the bench (PHASE R): the space in "Accounts Settings" MUST be encoded —
        # a raw space makes urllib reject the URL ("can't contain control characters").
        transport = _transport_returning(200, {"data": {}})
        probe_accounts_settings(self.target, self.env, lambda p: "", transport)
        _, url, _ = transport.called
        self.assertIn("/api/resource/Accounts%20Settings/Accounts%20Settings", url)
        self.assertNotIn("Accounts Settings", url)  # no raw space survives into the path


class TestProbePcvRead(unittest.TestCase):
    """The Period Closing Voucher-read probe (envelope E6) — required since get_period_locks
    reads the latest submitted PCV for the PCV boundary of the closed books, and an unreadable
    source RAISES, refusing the whole plan/submit/cancel. This source had NO probe before this
    change (Company/Accounts Settings/Workflow did). Same required-read inversion as those three:
    403 is a FAIL."""

    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)
        self.env = {"S": "supersecret"}

    def _probe(self, status, payload):
        return probe_pcv_read(self.target, self.env, lambda p: "",
                              _transport_returning(status, payload))

    def test_readable_even_if_empty_passes(self):
        findings = self._probe(200, {"data": []})
        self.assertEqual(findings[0][0], OK)

    def test_readable_with_rows_passes(self):
        findings = self._probe(200, {"data": [{"name": "PCV-0001"}]})
        self.assertEqual(findings[0][0], OK)

    def test_403_fails_with_the_exact_remedy(self):
        findings = self._probe(403, {"exc_type": "PermissionError"})
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("grant read permission on Period Closing Voucher", findings[0][1])
        self.assertIn("get_period_locks", findings[0][1])

    def test_other_errors_fail_with_the_reason(self):
        findings = self._probe(500, None)
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("500", findings[0][1])

    def test_unexpected_200_body_fails_not_passes(self):
        findings = self._probe(200, {"message": "not-a-data-envelope"})
        self.assertEqual(findings[0][0], FAIL)

    def test_unreachable_bench_fails_never_tracebacks(self):
        def transport(method, url, headers, params=None, body=None):
            raise OSError("no route to host")
        findings = probe_pcv_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[0][0], FAIL)

    def test_probe_never_leaks_the_secret_into_findings(self):
        for status, payload in ((200, {"data": []}), (403, {}), (500, None)):
            for _, msg in self._probe(status, payload):
                self.assertNotIn("supersecret", msg)

    def test_hits_the_pcv_resource_url_percent_encoded(self):
        # Same live bug class as Accounts Settings (PHASE R) — the space in "Period Closing
        # Voucher" MUST be encoded from the start; do not repeat that bug here.
        transport = _transport_returning(200, {"data": []})
        probe_pcv_read(self.target, self.env, lambda p: "", transport)
        _, url, _ = transport.called
        self.assertIn("/api/resource/Period%20Closing%20Voucher", url)
        self.assertNotIn("Period Closing Voucher", url)  # no raw space survives into the path


class TestProbeAccountingPeriodRead(unittest.TestCase):
    """The Accounting Period-read probe (F-S1) — required since get_period_locks LISTs periods
    containing a posting date, then does a full-document item GET per hit to read
    closed_documents, and an unreadable LIST *or* item GET RAISES, refusing the whole
    plan/submit/cancel. Same required-read inversion throughout: 403 is a FAIL."""

    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)
        self.env = {"S": "supersecret"}

    def _probe(self, status, payload):
        return probe_accounting_period_read(self.target, self.env, lambda p: "",
                                            _transport_returning(status, payload))

    def test_readable_even_if_empty_passes(self):
        # Zero periods = nothing to item-GET — exactly one finding, empty-bench still PASSES.
        findings = self._probe(200, {"data": []})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], OK)

    def test_readable_with_rows_exercises_the_item_get_leg_too(self):
        # A period exists: the LIST passes AND the item-GET leg (get_period_locks's own second
        # read, for closed_documents) is exercised — two findings, both readable.
        transport = _queue_transport([
            (200, {"data": [{"name": "2026"}]}),
            (200, {"data": {"name": "2026", "closed_documents": []}}),
        ])
        findings = probe_accounting_period_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(len(findings), 2)
        self.assertEqual([f[0] for f in findings], [OK, OK])
        self.assertIn("item read", findings[1][1])

    def test_item_get_403_fails_with_its_own_remedy(self):
        transport = _queue_transport([
            (200, {"data": [{"name": "2026"}]}),
            (403, {"exc_type": "PermissionError"}),
        ])
        findings = probe_accounting_period_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0][0], OK)          # the LIST itself is still readable
        self.assertEqual(findings[1][0], FAIL)
        self.assertIn("grant read permission on Accounting Period", findings[1][1])
        self.assertIn("get_period_locks", findings[1][1])
        self.assertIn("2026", findings[1][1])          # names WHICH period it tried to read

    def test_item_get_unexpected_body_fails(self):
        transport = _queue_transport([
            (200, {"data": [{"name": "2026"}]}),
            (200, {"message": "not-a-data-envelope"}),
        ])
        findings = probe_accounting_period_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[1][0], FAIL)

    def test_item_get_unreachable_bench_fails_never_tracebacks(self):
        calls = {"n": 0}
        def transport(method, url, headers, params=None, body=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (200, {"data": [{"name": "2026"}]})
            raise OSError("no route to host")
        findings = probe_accounting_period_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[1][0], FAIL)

    def test_list_row_missing_name_fails_before_any_item_get(self):
        transport = _queue_transport([(200, {"data": [{}]})])
        findings = probe_accounting_period_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[1][0], FAIL)
        self.assertIn("missing a name", findings[1][1])

    def test_403_fails_with_the_exact_remedy(self):
        findings = self._probe(403, {"exc_type": "PermissionError"})
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("grant read permission on Accounting Period", findings[0][1])
        self.assertIn("get_period_locks", findings[0][1])

    def test_other_errors_fail_with_the_reason(self):
        findings = self._probe(500, None)
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("500", findings[0][1])

    def test_unexpected_200_body_fails_not_passes(self):
        findings = self._probe(200, {"message": "not-a-data-envelope"})
        self.assertEqual(findings[0][0], FAIL)

    def test_unreachable_bench_fails_never_tracebacks(self):
        def transport(method, url, headers, params=None, body=None):
            raise OSError("no route to host")
        findings = probe_accounting_period_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[0][0], FAIL)

    def test_probe_never_leaks_the_secret_into_findings(self):
        for status, payload in ((200, {"data": []}), (403, {}), (500, None)):
            for _, msg in self._probe(status, payload):
                self.assertNotIn("supersecret", msg)

    def test_hits_the_accounting_period_resource_url_percent_encoded(self):
        transport = _transport_returning(200, {"data": []})
        probe_accounting_period_read(self.target, self.env, lambda p: "", transport)
        _, url, _ = transport.called
        self.assertIn("/api/resource/Accounting%20Period", url)
        self.assertNotIn("Accounting Period", url)  # no raw space survives into the path

    def test_hits_the_item_url_percent_encoded_when_a_period_exists(self):
        transport = _queue_transport([
            (200, {"data": [{"name": "2026"}]}),
            (200, {"data": {"name": "2026", "closed_documents": []}}),
        ])
        probe_accounting_period_read(self.target, self.env, lambda p: "", transport)
        _, item_url = transport.calls[1]
        self.assertEqual(item_url,
                         "https://erp.example.com/api/resource/Accounting%20Period/2026")


class TestProbePaymentLedgerRead(unittest.TestCase):
    """F-R1: the Payment Ledger Entry-read probe — required since plan_cancel/plan_cascade_cancel
    now read Payment Ledger Entry (get_settling_references) for EVERY supported doctype's
    settling-PE disclosure, and an unreadable read RAISES, refusing the whole plan. Same
    required-read inversion as Company/PCV/Accounting Period/Accounts Settings: 403 is a FAIL."""

    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)
        self.env = {"S": "supersecret"}

    def _probe(self, status, payload):
        return probe_payment_ledger_read(self.target, self.env, lambda p: "",
                                         _transport_returning(status, payload))

    def test_readable_even_if_empty_passes(self):
        findings = self._probe(200, {"data": []})
        self.assertEqual(findings[0][0], OK)

    def test_readable_with_rows_passes(self):
        findings = self._probe(200, {"data": [{"name": "PLE-0001"}]})
        self.assertEqual(findings[0][0], OK)

    def test_403_fails_with_the_exact_remedy(self):
        findings = self._probe(403, {"exc_type": "PermissionError"})
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("grant read permission on Payment Ledger Entry", findings[0][1])
        self.assertIn("settling-PE", findings[0][1])

    def test_other_errors_fail_with_the_reason(self):
        findings = self._probe(500, None)
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("500", findings[0][1])

    def test_unexpected_200_body_fails_not_passes(self):
        findings = self._probe(200, {"message": "not-a-data-envelope"})
        self.assertEqual(findings[0][0], FAIL)

    def test_unreachable_bench_fails_never_tracebacks(self):
        def transport(method, url, headers, params=None, body=None):
            raise OSError("no route to host")
        findings = probe_payment_ledger_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[0][0], FAIL)

    def test_probe_never_leaks_the_secret_into_findings(self):
        for status, payload in ((200, {"data": []}), (403, {}), (500, None)):
            for _, msg in self._probe(status, payload):
                self.assertNotIn("supersecret", msg)

    def test_hits_the_payment_ledger_entry_resource_url_percent_encoded(self):
        transport = _transport_returning(200, {"data": []})
        probe_payment_ledger_read(self.target, self.env, lambda p: "", transport)
        _, url, _ = transport.called
        self.assertIn("/api/resource/Payment%20Ledger%20Entry", url)
        self.assertNotIn("Payment Ledger Entry", url)  # no raw space survives into the path


class TestProbeGlEntryRead(unittest.TestCase):
    """The Close, Half 2: the GL Entry-read probe — closes the "GL Entry has no probe" asymmetry.
    Required for `close --reconcile`'s GL sweep (sweep_gl_entries); an unreadable read RAISES,
    refusing the whole reconciliation. Same required-read inversion as Payment Ledger Entry/
    Company/PCV/Accounting Period: 403 is a FAIL."""

    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)
        self.env = {"S": "supersecret"}

    def _probe(self, status, payload):
        return probe_gl_entry_read(self.target, self.env, lambda p: "",
                                   _transport_returning(status, payload))

    def test_readable_even_if_empty_passes(self):
        findings = self._probe(200, {"data": []})
        self.assertEqual(findings[0][0], OK)

    def test_readable_with_rows_passes(self):
        findings = self._probe(200, {"data": [{"name": "SI-1-GL-0001"}]})
        self.assertEqual(findings[0][0], OK)

    def test_403_fails_with_the_exact_remedy(self):
        findings = self._probe(403, {"exc_type": "PermissionError"})
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("grant read permission on GL Entry", findings[0][1])
        self.assertIn("reconciliation", findings[0][1])

    def test_other_errors_fail_with_the_reason(self):
        findings = self._probe(500, None)
        self.assertEqual(findings[0][0], FAIL)
        self.assertIn("500", findings[0][1])

    def test_unexpected_200_body_fails_not_passes(self):
        findings = self._probe(200, {"message": "not-a-data-envelope"})
        self.assertEqual(findings[0][0], FAIL)

    def test_unreachable_bench_fails_never_tracebacks(self):
        def transport(method, url, headers, params=None, body=None):
            raise OSError("no route to host")
        findings = probe_gl_entry_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[0][0], FAIL)

    def test_probe_never_leaks_the_secret_into_findings(self):
        for status, payload in ((200, {"data": []}), (403, {}), (500, None)):
            for _, msg in self._probe(status, payload):
                self.assertNotIn("supersecret", msg)

    def test_hits_the_gl_entry_resource_url_percent_encoded(self):
        transport = _transport_returning(200, {"data": []})
        probe_gl_entry_read(self.target, self.env, lambda p: "", transport)
        _, url, _ = transport.called
        self.assertIn("/api/resource/GL%20Entry", url)
        self.assertNotIn("GL Entry", url)  # no raw space survives into the path


class TestProbeRepostRead(unittest.TestCase):
    """The Close, Half 2: the Repost Accounting Ledger-read probe — the ONE WARN-on-403 probe in
    this module, because an unreadable repost read is NON-FATAL (close --reconcile still runs
    and completes, it just cannot attribute a second-generation row to the repost that caused
    it). Contrast every required-read probe above (403 = FAIL): here 403, and any other
    unreadable response, is a WARN, never a FAIL."""

    def setUp(self):
        self.target = load_registry(toml_text=REG).get(None)
        self.env = {"S": "supersecret"}

    def _probe(self, status, payload):
        return probe_repost_read(self.target, self.env, lambda p: "",
                                 _transport_returning(status, payload))

    def test_readable_even_if_empty_passes(self):
        findings = self._probe(200, {"data": []})
        self.assertEqual(findings[0][0], OK)

    def test_readable_with_rows_passes(self):
        findings = self._probe(200, {"data": [{"name": "RAL-0001"}]})
        self.assertEqual(findings[0][0], OK)

    def test_403_warns_not_fails(self):
        findings = self._probe(403, {"exc_type": "PermissionError"})
        self.assertEqual(findings[0][0], WARN)
        self.assertNotEqual(findings[0][0], FAIL)
        self.assertIn("Repost Accounting Ledger unreadable", findings[0][1])
        self.assertIn("reconciliation runs", findings[0][1])

    def test_other_errors_warn_not_fail(self):
        findings = self._probe(500, None)
        self.assertEqual(findings[0][0], WARN)
        self.assertIn("500", findings[0][1])

    def test_unexpected_200_body_warns_not_passes(self):
        findings = self._probe(200, {"message": "not-a-data-envelope"})
        self.assertEqual(findings[0][0], WARN)

    def test_unreachable_bench_warns_never_tracebacks_never_fails(self):
        def transport(method, url, headers, params=None, body=None):
            raise OSError("no route to host")
        findings = probe_repost_read(self.target, self.env, lambda p: "", transport)
        self.assertEqual(findings[0][0], WARN)

    def test_probe_never_leaks_the_secret_into_findings(self):
        for status, payload in ((200, {"data": []}), (403, {}), (500, None)):
            for _, msg in self._probe(status, payload):
                self.assertNotIn("supersecret", msg)

    def test_hits_the_repost_resource_url_percent_encoded(self):
        transport = _transport_returning(200, {"data": []})
        probe_repost_read(self.target, self.env, lambda p: "", transport)
        _, url, _ = transport.called
        self.assertIn("/api/resource/Repost%20Accounting%20Ledger", url)
        self.assertNotIn("Repost Accounting Ledger", url)  # no raw space survives into the path

    def test_credential_does_not_resolve_warns_not_fails(self):
        findings = probe_repost_read(self.target, {}, lambda p: "",
                                     _transport_returning(200, {"data": []}))
        self.assertEqual(findings[0][0], WARN)


class TestRunDoctor(unittest.TestCase):
    def test_ready_end_to_end_with_a_scoped_credential(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(READY_ROUTES))
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "ready.")
        self.assertFalse(any("supersecret" in line for line in lines))

    def test_missing_company_read_grant_makes_the_install_not_ready(self):
        # The v16 spine-fix upgrade break, made loud: a credential scoped BEFORE this fix (method
        # probe 403 = fine, workflow read fine) but WITHOUT the new Company read grant fails.
        routes = {"/api/method/": (403, {}),
                  "/api/resource/Company": (403, {"exc_type": "PermissionError"}),
                  "/api/resource/Workflow": (200, {"data": []})}
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(routes))
        self.assertEqual(code, 1)
        self.assertIn("NOT ready", lines[-1])
        self.assertTrue(any("grant read permission on Company" in line for line in lines))

    def test_missing_workflow_read_grant_makes_the_install_not_ready(self):
        # The upgrade break, made loud: a credential scoped BEFORE the workflow-SoD gate (method
        # probe 403 = fine, Company read fine) but WITHOUT the new Workflow read grant
        # (workflow read 403) fails.
        routes = {"/api/method/": (403, {}),
                  "/api/resource/Company": (200, {"data": []}),
                  "/api/resource/Workflow": (403, {"exc_type": "PermissionError"})}
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(routes))
        self.assertEqual(code, 1)
        self.assertIn("NOT ready", lines[-1])
        self.assertTrue(any("grant custom read permission on Workflow" in line
                            for line in lines))

    def test_missing_accounts_settings_read_grant_makes_the_install_not_ready(self):
        # Envelope E5: a credential that reads Company + Workflow but not Accounts Settings fails —
        # a Journal Entry cancel plan would refuse live, and doctor surfaces it up front.
        routes = {"/api/method/": (403, {}),
                  "/api/resource/Company": (200, {"data": []}),
                  "/api/resource/Workflow": (200, {"data": []}),
                  "/api/resource/Accounts%20Settings": (403, {"exc_type": "PermissionError"})}
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(routes))
        self.assertEqual(code, 1)
        self.assertIn("NOT ready", lines[-1])
        self.assertTrue(any("grant read permission on Accounts Settings" in line for line in lines))

    def test_missing_pcv_read_grant_makes_the_install_not_ready(self):
        # Envelope E6: a credential that reads everything else but not Period Closing Voucher
        # fails — get_period_locks would raise live on every plan/submit/cancel, and doctor
        # surfaces it up front instead of at the first real call.
        routes = dict(READY_ROUTES)
        routes["/api/resource/Period%20Closing%20Voucher"] = (403, {"exc_type": "PermissionError"})
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(routes))
        self.assertEqual(code, 1)
        self.assertIn("NOT ready", lines[-1])
        self.assertTrue(any("grant read permission on Period Closing Voucher" in line
                            for line in lines))

    def test_missing_accounting_period_read_grant_makes_the_install_not_ready(self):
        # Envelope E6: a credential that reads everything else but not Accounting Period fails —
        # get_period_locks would raise live on every plan/submit/cancel, and doctor surfaces it
        # up front instead of at the first real call.
        routes = dict(READY_ROUTES)
        routes["/api/resource/Accounting%20Period"] = (403, {"exc_type": "PermissionError"})
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(routes))
        self.assertEqual(code, 1)
        self.assertIn("NOT ready", lines[-1])
        self.assertTrue(any("grant read permission on Accounting Period" in line
                            for line in lines))

    def test_missing_payment_ledger_read_grant_makes_the_install_not_ready(self):
        # F-R1: a credential that reads everything else but not Payment Ledger Entry fails —
        # get_settling_references would raise live on every plan_cancel/plan_cascade_cancel, and
        # doctor surfaces it up front instead of at the first real call.
        routes = dict(READY_ROUTES)
        routes["/api/resource/Payment%20Ledger%20Entry"] = (403, {"exc_type": "PermissionError"})
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(routes))
        self.assertEqual(code, 1)
        self.assertIn("NOT ready", lines[-1])
        self.assertTrue(any("grant read permission on Payment Ledger Entry" in line
                            for line in lines))

    def test_offline_skips_the_probe_and_can_still_pass(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, offline=True,
                                     transport=_transport_returning(200, {"message": "x"}))
        self.assertEqual(code, 0)
        self.assertTrue(any("--offline" in line for line in lines))
        self.assertTrue(any("PCV-read probe" in line and "Accounting-Period-read probe" in line
                            for line in lines))
        self.assertTrue(any("Payment-Ledger-Entry-read probe" in line for line in lines))
        self.assertTrue(any("GL-Entry-read probe" in line for line in lines))
        self.assertTrue(any("Repost-read probe" in line for line in lines))
        self.assertTrue(any("roles probe" in line for line in lines))

    def test_missing_gl_entry_read_grant_makes_the_install_not_ready(self):
        # The Close, Half 2: a credential that reads everything else but not GL Entry fails —
        # sweep_gl_entries would raise live on every `close --reconcile`, and doctor surfaces it
        # up front instead of at the first real call.
        routes = dict(READY_ROUTES)
        routes["/api/resource/GL%20Entry"] = (403, {"exc_type": "PermissionError"})
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(routes))
        self.assertEqual(code, 1)
        self.assertIn("NOT ready", lines[-1])
        self.assertTrue(any("grant read permission on GL Entry" in line for line in lines))

    def test_unreadable_repost_read_does_not_block_readiness(self):
        # The one deliberate WARN-on-403 probe: a credential that reads everything else but not
        # Repost Accounting Ledger is STILL ready — this read is non-fatal corroboration, not
        # the reconciliation's audit source.
        routes = dict(READY_ROUTES)
        routes["/api/resource/Repost%20Accounting%20Ledger"] = (
            403, {"exc_type": "PermissionError"})
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(routes))
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "ready.")
        self.assertTrue(any("Repost Accounting Ledger unreadable" in line for line in lines))

    def test_ready_end_to_end_lists_both_new_probes(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(READY_ROUTES))
        self.assertEqual(code, 0)
        self.assertTrue(any("GL Entry read: readable" in line for line in lines))
        self.assertTrue(any("Repost Accounting Ledger read: readable" in line for line in lines))

    def test_administrator_makes_the_install_not_ready(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(
                env, transport=_transport_returning(200, {"message": "Administrator"}))
        self.assertEqual(code, 1)
        self.assertIn("NOT ready", lines[-1])

    def test_over_privileged_seat_makes_the_install_not_ready(self):
        # The tight-role seat: a seat carrying System Manager voids the least-privilege spine —
        # doctor refuses to call the install ready even with every read grant present.
        routes = dict(READY_ROUTES)
        routes["/api/v2/method/User/get_roles"] = (
            200, {"data": ["Accounts User", "System Manager", "All"]})
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(routes))
        self.assertEqual(code, 1)
        self.assertIn("NOT ready", lines[-1])
        self.assertTrue(any("System Manager" in line for line in lines))

    def test_missing_roles_grant_makes_the_install_not_ready(self):
        # The tight-role seat's new required read: a credential without the User.get_roles grant
        # (403) cannot be certified least-privilege, so doctor refuses — deny-biased.
        routes = dict(READY_ROUTES)
        routes["/api/v2/method/User/get_roles"] = (403, {"exc_type": "PermissionError"})
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, transport=_routing_transport(routes))
        self.assertEqual(code, 1)
        self.assertIn("NOT ready", lines[-1])
        self.assertTrue(any("User.get_roles" in line for line in lines))

    def test_unknown_target_is_a_clear_refusal(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"PACIOLI_REGISTRY": _write_registry(d),
                   "PACIOLI_STATE_DIR": str(Path(d) / "state"), "S": "supersecret"}
            code, lines = run_doctor(env, target_name="nope",
                                     transport=_transport_returning(403, {}))
        self.assertEqual(code, 1)
        self.assertTrue(any("unknown target" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
