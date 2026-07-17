# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — the clock-domain seam (site wall clock <-> store clock).

Pacioli lives across TWO clocks: receipt ``.ts`` is the **store clock** (honest UTC,
``YYYY-MM-DDTHH:MM:SSZ``, ``store.py``), while every ERPNext ``creation``/``modified`` stamp is
the **site wall clock** — whatever timezone the bench runs (the 2026-07-16 bench ran
IST-flavored stamps against a UTC store; pins G3 caught the skew live). A ``--since/--until``
window that crosses both domains must be converted, never applied verbatim — this module is the
single seam that does it (ruling: ``docs/plans/2026-07-16-clock-domain-ruling.md``, T1: the
operator declares the site zone in the registry; the window means SITE time; the canonical
internal domain stays store-UTC).

Pure by the ``prove.py``/``close.py`` discipline: parses supplied strings against a supplied
zone name, never reads a wall clock, never touches store/bench/key — fully unit-testable.

Deny-biased throughout: an unknown zone, an unparseable stamp, or a non-string refuses with
:class:`ClockDomainError` naming the offender — a conversion that cannot be performed is never
silently skipped (a skipped conversion IS the original defect).

Precision note: store-side output truncates to whole seconds. Receipt stamps are whole-second,
so at receipt granularity an upper bound loses nothing and a lower bound widens by less than
one second — and a fractional ``...59.5Z`` shape would break the lexicographic string
comparison ``close._in_window`` relies on. Site-side output is whole-second for the same
reason (the sweep filters are server-side datetime comparisons; a whole-second bound is exact
against them).

DST edges (relevant only to zones that observe it): an ambiguous local time (fall-back hour)
reads at its FIRST occurrence (``fold=0``); a nonexistent local time (spring-forward gap) maps
at the pre-gap offset. Both deterministic — a window bound must mean one instant, every run.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_STORE_FMT = "%Y-%m-%dT%H:%M:%SZ"          # store.py's receipt-ts shape, exactly
_SITE_FMT = "%Y-%m-%d %H:%M:%S"            # frappe server-stamp shape (whole seconds)
_END_OF_DAY = " 23:59:59.999999"           # the F3 bare-date-until expansion, site domain

# Every naive-stamp shape a window bound may arrive in (frappe space-separated and ISO
# T-separated, with or without microseconds, plus a bare date).
_NAIVE_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def expand_bare_date_end_of_day(value):
    """The F3 bare-date-until semantic, ONE implementation (also used by the CLI's
    format-only ``_to_frappe_clock``): a bare date used as the inclusive upper bound expands
    to end-of-day; a bound already carrying a time is left alone. Domain-agnostic — the
    caller decides WHICH clock the day belongs to by where it applies this."""
    if isinstance(value, str) and ":" not in value:
        return value.strip() + _END_OF_DAY
    return value


class ClockDomainError(ValueError):
    """A clock-domain conversion that cannot be performed — unknown zone, unparseable stamp,
    or a non-string where a stamp/zone name belongs. Always carries the offender in its
    message; the caller refuses loudly, never proceeds unconverted."""


def _zone(tz_name):
    if not isinstance(tz_name, str) or not tz_name.strip():
        raise ClockDomainError(
            f"site timezone must be an IANA zone name string, got {tz_name!r}")
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ClockDomainError(
            f"unknown site timezone {tz_name!r} — declare a valid IANA zone name "
            f"(e.g. 'Asia/Kolkata'); a window cannot be converted against a zone "
            f"that does not resolve") from exc


def _parse_naive(value, what):
    if not isinstance(value, str):
        raise ClockDomainError(f"{what} must be a string stamp, got {value!r}")
    s = value.strip()
    if s.endswith("Z"):
        # The Z suffix is the STORE-clock marker (operators copy it straight off a statement's
        # receipt ts) — an explicit UTC stamp where a SITE-local bound is expected is a domain
        # conflict, and the refusal must say so, not "unrecognizable" (review finding 4).
        raise ClockDomainError(
            f"{what} {value!r} is a store-clock (Z-suffixed UTC) stamp, but with site_tz "
            f"declared the window means SITE time — restate the bound in the site's wall "
            f"clock, or drop the Z if you really meant that wall-clock reading")
    for fmt in _NAIVE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ClockDomainError(
        f"{what} {value!r} is not a recognizable stamp "
        f"(YYYY-MM-DD[ HH:MM:SS[.ffffff]], T-separated also accepted)")


def site_to_store(value, tz_name, *, end_of_day=False):
    """A site-local wall-clock bound -> the store-clock UTC string (``YYYY-MM-DDTHH:MM:SSZ``).

    ``end_of_day`` applies the F3 bare-date-until semantic IN THE SITE DOMAIN before
    converting (end-of-day where the books live, never end-of-day UTC); a bound already
    carrying a time is left alone. Refuses (:class:`ClockDomainError`) rather than guess."""
    if end_of_day:
        value = expand_bare_date_end_of_day(value)
    naive = _parse_naive(value, "window bound")
    local = naive.replace(tzinfo=_zone(tz_name), fold=0)
    return local.astimezone(timezone.utc).strftime(_STORE_FMT)


def store_to_site(value, tz_name):
    """A store-clock UTC string (receipt-ts shape, ``YYYY-MM-DDTHH:MM:SSZ``) -> the site-local
    frappe-format string (``YYYY-MM-DD HH:MM:SS``) the GL/repost sweeps filter on. Accepts ONLY
    the store shape — a frappe-shaped input here means a domain mix-up upstream, refused."""
    zone = _zone(tz_name)
    if not isinstance(value, str):
        raise ClockDomainError(f"store stamp must be a string, got {value!r}")
    try:
        naive_utc = datetime.strptime(value.strip(), _STORE_FMT)
    except ValueError as exc:
        raise ClockDomainError(
            f"store stamp {value!r} is not store-clock shaped ({_STORE_FMT!r}) — refusing to "
            f"convert a stamp whose domain is unclear") from exc
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(zone).strftime(_SITE_FMT)
