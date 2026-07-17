# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The clock-domain seam (``pacioli.clock``) — pure site-clock <-> store-clock conversion.

Born from the 2026-07-16 bench finding (pins G3): one ``--since/--until`` window string was
applied verbatim to TWO clocks — receipt ``.ts`` (store, UTC) and GL ``creation`` (the ERPNext
site wall clock). ``pacioli.clock`` is the single conversion seam; pure by the prove.py
discipline (parses supplied strings, never reads a wall clock), so it is unit-testable here
with pinned instants. Ruling: docs/plans/2026-07-16-clock-domain-ruling.md (T1)."""
import unittest

from pacioli.clock import ClockDomainError, site_to_store, store_to_site

_KOLKATA = "Asia/Kolkata"     # UTC+5:30, no DST — the bench's own flavor
_CHICAGO = "America/Chicago"  # DST edges


class TestSiteToStore(unittest.TestCase):
    """Site-local wall-clock string -> store-clock UTC string (``YYYY-MM-DDTHH:MM:SSZ``, the
    exact shape receipt ``.ts`` carries, so string comparison in ``close._in_window`` stays
    valid)."""

    def test_frappe_shaped_stamp_converts(self):
        self.assertEqual(site_to_store("2026-07-16 01:30:00", _KOLKATA),
                         "2026-07-15T20:00:00Z")

    def test_iso_t_separator_accepted(self):
        self.assertEqual(site_to_store("2026-07-16T01:30:00", _KOLKATA),
                         "2026-07-15T20:00:00Z")

    def test_microseconds_truncated_to_seconds(self):
        # Receipt stamps are whole-second; a sub-second bound truncates (documented: at receipt
        # granularity nothing is lost — and a '.5Z' shape would break lexicographic comparison).
        self.assertEqual(site_to_store("2026-07-16 01:30:00.500000", _KOLKATA),
                         "2026-07-15T20:00:00Z")

    def test_bare_date_reads_as_midnight(self):
        self.assertEqual(site_to_store("2026-07-16", _KOLKATA), "2026-07-15T18:30:00Z")

    def test_bare_date_end_of_day_expands_in_the_site_domain(self):
        # The F3 bare-date-until semantic, applied BEFORE conversion: end-of-day where the books
        # live, not end-of-day UTC.
        self.assertEqual(site_to_store("2026-07-16", _KOLKATA, end_of_day=True),
                         "2026-07-16T18:29:59Z")

    def test_end_of_day_leaves_a_bound_with_a_time_alone(self):
        self.assertEqual(site_to_store("2026-07-16 01:30:00", _KOLKATA, end_of_day=True),
                         "2026-07-15T20:00:00Z")

    def test_dst_ambiguous_local_time_is_deterministic_first_occurrence(self):
        # 2026-11-01 01:30 in Chicago happens twice (fall-back); fold=0 = the CDT (first) pass.
        self.assertEqual(site_to_store("2026-11-01 01:30:00", _CHICAGO),
                         "2026-11-01T06:30:00Z")

    def test_dst_nonexistent_local_time_is_deterministic(self):
        # 2026-03-08 02:30 in Chicago never happens (spring-forward); zoneinfo maps it
        # deterministically (fold=0 reads it at the pre-gap offset, CST/-6).
        self.assertEqual(site_to_store("2026-03-08 02:30:00", _CHICAGO),
                         "2026-03-08T08:30:00Z")

    def test_unknown_zone_refuses_with_the_zone_named(self):
        with self.assertRaises(ClockDomainError) as ctx:
            site_to_store("2026-07-16 01:30:00", "Not/AZone")
        self.assertIn("Not/AZone", str(ctx.exception))

    def test_unparseable_stamp_refuses_with_the_value_named(self):
        with self.assertRaises(ClockDomainError) as ctx:
            site_to_store("next tuesday", _KOLKATA)
        self.assertIn("next tuesday", str(ctx.exception))

    def test_non_string_refuses(self):
        with self.assertRaises(ClockDomainError):
            site_to_store(20260716, _KOLKATA)

    def test_non_string_zone_refuses(self):
        with self.assertRaises(ClockDomainError):
            site_to_store("2026-07-16 01:30:00", None)

    def test_z_suffixed_stamp_refusal_names_the_domain_conflict(self):
        # Review finding 4: a Z-suffixed stamp is the STORE shape (operators copy it from
        # statements) — refusing is right, but the refusal must say WHY: a store-domain stamp
        # where a site-local bound is expected, not a generic "not recognizable".
        with self.assertRaises(ClockDomainError) as ctx:
            site_to_store("2026-07-16T01:30:00Z", _KOLKATA)
        msg = str(ctx.exception)
        self.assertIn("store", msg)
        self.assertIn("site", msg)


class TestStoreToSite(unittest.TestCase):
    """Store-clock UTC string -> site-local frappe-format string (``YYYY-MM-DD HH:MM:SS``, the
    shape ``sweep_gl_entries``/``get_reposts`` filter on)."""

    def test_store_stamp_converts(self):
        self.assertEqual(store_to_site("2026-07-15T20:00:00Z", _KOLKATA),
                         "2026-07-16 01:30:00")

    def test_round_trip_is_identity(self):
        s = "2026-07-16 01:30:00"
        self.assertEqual(store_to_site(site_to_store(s, _KOLKATA), _KOLKATA), s)

    def test_unknown_zone_refuses(self):
        with self.assertRaises(ClockDomainError):
            store_to_site("2026-07-15T20:00:00Z", "Not/AZone")

    def test_unparseable_stamp_refuses(self):
        with self.assertRaises(ClockDomainError):
            store_to_site("2026-07-15 20:00:00", _KOLKATA)  # frappe shape is NOT a store stamp

    def test_non_string_refuses(self):
        with self.assertRaises(ClockDomainError):
            store_to_site(None, _KOLKATA)


if __name__ == "__main__":
    unittest.main()
