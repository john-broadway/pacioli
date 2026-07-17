# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""``close`` under a declared ``site_tz`` — the clock-domain ruling (T1) wired to the CLI.

With ``site_tz`` on the target, ``--since/--until`` mean SITE time (the books' own calendar),
converted ONCE at the CLI boundary to the store's UTC domain (statement filter, cursor,
future-``--until`` compare); the GL sweep gets site-domain bounds. Absent ``site_tz`` →
byte-identical to before, plus an honest clock note on ``--reconcile``. Ruling:
docs/plans/2026-07-16-clock-domain-ruling.md."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pacioli.cli import cmd_close
from pacioli.runtime import open_store

_REG_TZ = ('[targets.prod]\nbase_url = "https://erp.example.com"\n'
           'api_key = "env:K"\napi_secret = "env:S"\ncompany = "Example Co"\n'
           'site_tz = "Asia/Kolkata"\ndefault = true\n')

_REG_NO_TZ = _REG_TZ.replace('site_tz = "Asia/Kolkata"\n', '')

_REG_BAD_TZ = _REG_TZ.replace('"Asia/Kolkata"', '"Not/AZone"')


def _routing_transport(routes, calls=None):
    calls = calls if calls is not None else []

    def transport(method, url, headers, params=None, body=None):
        calls.append((method, url, params, body))
        for fragment, response in routes.items():
            if fragment in url:
                return response
        return 404, None
    transport.calls = calls
    return transport


_RECON_ROUTES = {
    "/api/resource/GL%20Entry": (200, {"data": []}),
    "/api/resource/Accounts%20Settings": (200, {"data": {"enable_immutable_ledger": 1,
                                                          "delete_linked_ledger_entries": 0}}),
    "/api/resource/Repost%20Accounting%20Ledger": (200, {"data": []}),
}


class _Harness(unittest.TestCase):
    registry_text = _REG_TZ

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(self.registry_text)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _seed_act(self):
        store = open_store(self.env, "prod")
        intent = store.record_intent({"tool": "submit", "target": "prod", "docname": "SI-1"})
        store.record_outcome(intent, "committed", {}, None)

    def _run(self, **kw):
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cmd_close(self.env, target=kw.get("target"), since=kw.get("since"),
                       until=kw.get("until"),
                           expected_head=None, as_json=kw.get("as_json", False),
                           reconcile=kw.get("reconcile", False),
                           respond=kw.get("respond", False), envelope=kw.get("envelope"),
                           advance=kw.get("advance", False),
                           transport=kw.get("transport"))
        return rc, o.getvalue(), e.getvalue()


class TestStatementBoundsConvert(_Harness):
    """Plain close (store-only, offline) — the statement window converts site->store."""

    def test_period_bounds_render_as_store_utc(self):
        self._seed_act()
        rc, out, err = self._run(since="2026-01-01 00:00:00", until="2026-12-31 23:59:59",
                                 as_json=True)
        self.assertEqual(rc, 0, err)
        st = json.loads(out)
        # Asia/Kolkata is UTC+5:30 — the site-local bounds land 5h30m earlier in UTC.
        self.assertEqual(st["period"]["since"], "2025-12-31T18:30:00Z")
        self.assertEqual(st["period"]["until"], "2026-12-31T18:29:59Z")

    def test_bare_date_until_expands_end_of_day_in_the_site_domain(self):
        self._seed_act()
        rc, out, err = self._run(since="2026-01-01", until="2026-12-31", as_json=True)
        self.assertEqual(rc, 0, err)
        st = json.loads(out)
        self.assertEqual(st["period"]["since"], "2025-12-31T18:30:00Z")  # midnight, site
        self.assertEqual(st["period"]["until"], "2026-12-31T18:29:59Z")  # end-of-day, site

    def test_render_carries_the_clock_line(self):
        self._seed_act()
        rc, out, err = self._run(since="2026-01-01", until="2026-12-31")
        self.assertEqual(rc, 0, err)
        self.assertIn("Asia/Kolkata", out)  # the window's domain is named, not silent

    def test_unbounded_close_needs_no_conversion_and_no_clock_line(self):
        self._seed_act()
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Asia/Kolkata", out)


class TestNoSiteTzUnchanged(_Harness):
    registry_text = _REG_NO_TZ

    def test_bounds_pass_verbatim_when_site_tz_absent(self):
        self._seed_act()
        rc, out, err = self._run(since="2026-01-01", until="2026-12-31", as_json=True)
        self.assertEqual(rc, 0, err)
        st = json.loads(out)
        self.assertEqual(st["period"]["since"], "2026-01-01")
        self.assertEqual(st["period"]["until"], "2026-12-31")

    def test_reconcile_without_site_tz_says_two_clock_domains(self):
        self._seed_act()
        rc, out, err = self._run(since="2026-01-01", until="2026-12-31", reconcile=True,
                                 transport=_routing_transport(dict(_RECON_ROUTES)))
        self.assertIn("site_tz", out)  # the honest note: declare it to align the domains

    def test_reconcile_without_site_tz_json_clock_block(self):
        self._seed_act()
        rc, out, err = self._run(since="2026-01-01", until="2026-12-31", reconcile=True,
                                 as_json=True,
                                 transport=_routing_transport(dict(_RECON_ROUTES)))
        doc = json.loads(out)
        self.assertIn("clock", doc)
        self.assertIsNone(doc["clock"]["site_tz"])

    def test_missing_registry_file_still_closes_bounded_with_explicit_target(self):
        # A store-only operator with no registry keeps today's behavior when naming the target
        # explicitly (an UNNAMED target has always needed the registry to resolve the default —
        # unchanged). The clock helper degrades to no-conversion + a stderr warning that a
        # declared site_tz, if any, was not applied — never a crash, never a refusal.
        self._seed_act()
        Path(self.env["PACIOLI_REGISTRY"]).unlink()
        rc, out, err = self._run(since="2026-01-01", until="2026-12-31", as_json=True,
                                 target="prod")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["period"]["since"], "2026-01-01")
        self.assertIn("store clock domain", err)  # the honest warning, not silence


class TestBadZoneRefuses(_Harness):
    registry_text = _REG_BAD_TZ

    def test_bounded_close_refuses_naming_the_zone(self):
        # The operator DECLARED a zone; a declared-but-unresolvable conversion is never
        # silently skipped (a skipped conversion IS the original defect). Deny-biased.
        self._seed_act()
        rc, out, err = self._run(since="2026-01-01", until="2026-12-31")
        self.assertNotEqual(rc, 0)
        self.assertIn("Not/AZone", err)

    def test_unbounded_close_is_not_bricked_by_the_typo(self):
        # No window → nothing to convert → the typo\'d zone must not brick the close
        # (the registry-load split: type errors refuse at load, typos refuse AT USE).
        self._seed_act()
        rc, out, err = self._run()
        self.assertEqual(rc, 0, err)


class TestReconcileSweepBounds(_Harness):
    def test_sweep_receives_site_domain_bounds(self):
        self._seed_act()
        calls = []
        rc, out, err = self._run(since="2026-01-01 00:00:00", until="2026-12-31 23:59:59",
                                 reconcile=True,
                                 transport=_routing_transport(dict(_RECON_ROUTES), calls))
        self.assertEqual(rc, 0, err)
        gl_calls = [c for c in calls if "GL%20Entry" in c[1]]
        self.assertTrue(gl_calls)
        sent = json.dumps(gl_calls[0][2])  # params carry the filters JSON
        # The GL `creation` filter is the SITE clock — the operator's site-domain bounds go to
        # the bench as-is (round-tripped through the canonical UTC domain, identity).
        self.assertIn("2026-01-01 00:00:00", sent)
        self.assertIn("2026-12-31 23:59:59", sent)

    def test_reconcile_with_site_tz_json_clock_block_names_the_zone(self):
        self._seed_act()
        rc, out, err = self._run(since="2026-01-01", until="2026-12-31", reconcile=True,
                                 as_json=True,
                                 transport=_routing_transport(dict(_RECON_ROUTES)))
        doc = json.loads(out)
        self.assertEqual(doc["clock"]["site_tz"], "Asia/Kolkata")


class TestAdvanceMaterializedBoundsReachTheSweep(_Harness):
    """Review finding 0 (CONFIRMED crash regression): --advance materializes --until (and can
    cursor-default --since) AFTER the boundary conversion, so the site-domain originals hold
    None for that bound — the sweep must derive a site-domain string from the store-domain
    canonical (store_to_site), never crash on the missing original."""

    def test_advance_reconcile_with_omitted_until_completes_and_sweeps_site_domain(self):
        self._seed_act()
        calls = []
        rc, out, err = self._run(since="2026-07-01", reconcile=True, respond=True, advance=True,
                                 transport=_routing_transport(dict(_RECON_ROUTES), calls))
        self.assertEqual(rc, 0, out + err)
        gl_calls = [c for c in calls if "GL%20Entry" in c[1]]
        self.assertTrue(gl_calls)
        sent = json.dumps(gl_calls[0][2])
        # The materialized until (store clock, ...Z) must reach the bench as a SITE-domain
        # frappe-shaped stamp — no T separator, no Z suffix, +5:30 from the store instant.
        self.assertNotIn("Z\\\"", sent.replace(" ", ""))
        for c in gl_calls:
            for v in json.dumps(c[2]).split('"'):
                self.assertFalse(v.endswith("Z") and v[:4].isdigit(),
                                 f"store-domain stamp leaked to the sweep: {v}")

    def test_advance_reconcile_with_cursor_defaulted_since_completes(self):
        self._seed_act()
        # First advance records a cursor (store-domain stamp); the second, with --since
        # omitted, defaults from it and must convert it for the sweep instead of crashing.
        rc1, out1, err1 = self._run(since="2026-07-01", until="2026-07-10",
                                    respond=True, advance=True)
        self.assertEqual(rc1, 0, out1 + err1)
        calls = []
        rc2, out2, err2 = self._run(until="2026-07-15", reconcile=True, respond=True,
                                    advance=True,
                                    transport=_routing_transport(dict(_RECON_ROUTES), calls))
        self.assertEqual(rc2, 0, out2 + err2)
        self.assertTrue([c for c in calls if "GL%20Entry" in c[1]])


class TestReversedWindowGuardAfterConversion(_Harness):
    def test_mixed_separator_valid_window_is_accepted(self):
        """Review finding 1 (CONFIRMED): the reversed-window guard ran on the UNCONVERTED
        strings, where 'T' (0x54) sorts above ' ' (0x20) — a valid 01:00 -> 23:00 same-day
        window read as reversed. Post-conversion both bounds share one shape."""
        self._seed_act()
        rc, out, err = self._run(since="2026-07-10T01:00:00", until="2026-07-10 23:00:00",
                                 respond=True, advance=True)
        self.assertEqual(rc, 0, out + err)

    def test_genuinely_reversed_window_still_refuses_showing_operator_strings(self):
        self._seed_act()
        rc, out, err = self._run(since="2026-07-10 23:00:00", until="2026-07-10T01:00:00",
                                 respond=True, advance=True)
        self.assertEqual(rc, 2)
        self.assertIn("2026-07-10 23:00:00", err)  # the operator's own strings, not the UTC


class TestPlainJsonClockDisclosure(_Harness):
    def test_plain_bounded_json_carries_the_clock_block(self):
        """Review finding 2 (CONFIRMED): the machine-readable surface must disclose the
        conversion exactly like the human render does — never silent."""
        self._seed_act()
        rc, out, err = self._run(since="2026-01-01", until="2026-12-31", as_json=True)
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["clock"]["site_tz"], "Asia/Kolkata")
        self.assertEqual(doc["clock"]["window_site"]["since"], "2026-01-01")
        self.assertEqual(doc["clock"]["window_store"]["since"], "2025-12-31T18:30:00Z")

    def test_plain_unbounded_json_shape_unchanged(self):
        self._seed_act()
        rc, out, err = self._run(as_json=True)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("clock", json.loads(out))


class TestResolutionErrorIsNotUnreadable(_Harness):
    def test_unknown_target_on_readable_registry_does_not_claim_unreadable(self):
        """Review finding 3 (CONFIRMED): a readable registry whose target cannot be RESOLVED
        (typo'd name, ambiguous default) is not 'unreadable' — that warning misdirects the
        operator to file permissions while the real problem is target resolution."""
        self._seed_act()
        rc, out, err = self._run(since="2026-01-01", until="2026-12-31", target="nosuch",
                                 as_json=True)
        self.assertNotIn("unreadable", err)


class TestAdvanceFutureUntilUnderSiteTz(_Harness):
    def test_site_local_now_is_not_refused_as_future(self):
        """The G3 ergonomic fix: a site-local 'now' on a UTC-ahead site LOOKS future as a raw
        string but converts to a valid past instant — the cursor-poison guard must accept it
        (and keep refusing genuinely-future bounds)."""
        self._seed_act()
        # 'now' in Asia/Kolkata, minus a 2h safety margin: as a NAIVE string it is ~3h30m
        # AHEAD of the store clock (refuses un-converted); as an instant it is 2h PAST.
        # T-separated so the naive lexicographic compare actually sees it as future (a
        # space-separated stamp sorts BELOW a T-separated one — a separate wobble the
        # conversion also removes by normalizing both sides to the store shape).
        site_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30) - timedelta(hours=2)
        until = site_now.strftime("%Y-%m-%dT%H:%M:%S")
        rc, out, err = self._run(since="2026-01-01", until=until, respond=True, advance=True)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("future", out + err)

    def test_genuinely_future_site_bound_still_refuses(self):
        self._seed_act()
        site_future = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30) + timedelta(days=2)
        until = site_future.strftime("%Y-%m-%dT%H:%M:%S")
        rc, out, err = self._run(since="2026-01-01", until=until, respond=True, advance=True)
        self.assertNotEqual(rc, 0)
        self.assertIn("future", out + err)


if __name__ == "__main__":
    unittest.main()
