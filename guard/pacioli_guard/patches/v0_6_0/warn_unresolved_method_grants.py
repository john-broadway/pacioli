"""0.6.0 migrate audit — WARN (log-only) on `methods` grants that deny-unknown stops honoring.

Guard 0.6.0 flips the method-scope posture (see CHANGELOG 0.6.0): a `methods` grant now fires only
on a doctype-RESOLVED call or an exact `SAFE_METHODS` name. A pre-0.6.0 grant holding a bare RPC
name (`run_doc_method`, `frappe.client.submit`, a broad glob, …) will silently stop matching the
bare/unresolved calls it used to cover. This patch walks every **API Key Scope** grant's method
rows at `bench migrate` time and logs a warning per pattern the new posture will treat differently,
so the operator upgrading finds out from the migrate log — not from a production 403.

Honest limits, by design:
- **Best-effort STATIC HEURISTIC.** It classifies the grant PATTERN's string shape; it cannot
  replay a live request, so it cannot know whether a given pattern was only ever exercised via
  resolved shapes (in which case the grant keeps working and the warning is noise). WARN means
  "review this grant", never "this grant is broken".
- **LOG-ONLY.** It mutates nothing and denies nothing — enforcement lives in `scope.is_permitted`.
- **It must NEVER break `bench migrate`.** The whole of `execute()` is try/except-wrapped; an
  exception (schema not yet migrated, unexpected data, logging failure, anything) is swallowed
  after a last-ditch print. A failed *audit* is acceptable; a failed *migrate* is not.
- Live migrate behavior is knowledge-pinned until the Gate 10 bench run (see `../../GO-LIVE.md`).
"""
from __future__ import annotations

from pacioli_guard.scope import SAFE_METHODS, _UNGRANTABLE_METHOD_DOCTYPES

_GLOB_CHARS = ("*", "?", "[")


def grant_warning(pattern):
    """Pure: one warning string for a single `methods` grant pattern, or ``None`` if the pattern
    still plausibly fires under deny-unknown (an exact SAFE_METHODS name, or a per-doctype
    ``<DocType>.<method>`` shape).

    This is a SHAPE heuristic, deliberately biased to OVER-warn (a warning is log-only noise; a
    missed dead grant is a silent production 403). It distinguishes a ``<DocType>.<method>`` grant
    from an RPC module path structurally: a DocType name carries no ``.`` (a frappe constraint) and
    is Title-cased, so a per-doctype grant is a single Title/space-cased segment before the last
    dot; anything multi-segment or lowercase before the last dot is treated as an RPC module path.
    Residual (documented, acceptable — over-warns, never under-warns): a single-segment
    lowercase-named custom DocType (`item.submit`) is warned though it still fires; and it keys on
    the SAME first-segment doctype extraction and ``.strip``/exact-match rules as
    :func:`is_permitted`, so its verdict tracks the gate rather than a second, drifting heuristic."""
    if not isinstance(pattern, str) or not pattern.strip():
        return "empty or non-string pattern — matches nothing under any version; remove it"
    if pattern != pattern.strip():
        return (
            "surrounding whitespace — the guard matches grant patterns EXACTLY and does NOT strip "
            "them, so this padded row is dead (matches nothing under any version); remove the "
            "leading/trailing whitespace"
        )
    # Doctype-part = FIRST segment, matching is_permitted's hard-deny extraction (doctypes carry no
    # dot) — so a dotted method name can't slide "Bulk Update" past this the way an rpartition would.
    if pattern.split(".", 1)[0] in _UNGRANTABLE_METHOD_DOCTYPES:
        return (
            "'Bulk Update' method targets are hard-denied (ungrantable) as of guard 0.6.0 — its "
            "instance method reads the victim doctype from a saved record, invisible to the "
            "request classifier; this grant row can never fire and should be removed"
        )
    if pattern in SAFE_METHODS:
        return None
    head, sep, _method = pattern.rpartition(".")
    if not sep or not head:
        return (
            "bare method name — guard 0.6.0 deny-unknown no longer honors a methods grant on an "
            "unresolved (bare /api/method | ?cmd=) call unless the exact name is in SAFE_METHODS; "
            "grant the per-doctype '<DocType>.<method>' pattern(s) this credential actually needs"
        )
    if any(ch in head for ch in _GLOB_CHARS):
        return (
            "wildcard doctype-part — cannot be statically confirmed as a per-doctype grant, and a "
            "broad glob also matches bare RPC module paths, which deny-unknown (guard 0.6.0) no "
            "longer honors on unresolved calls; narrow to explicit '<DocType>.<method>' patterns"
        )
    if "." in head:
        return (
            "multi-segment dotted name — an RPC module path (e.g. frappe.client.submit, "
            "MyApp.api.do_thing), not a '<DocType>.<method>' grant (a DocType carries no dot); "
            "guard 0.6.0 deny-unknown denies it on bare/unresolved calls unless the exact name is "
            "in SAFE_METHODS — grant the per-doctype pattern(s) the credential actually needs"
        )
    if head[0].isupper() or " " in head:
        return None  # plausible per-doctype grant — still fires on doctype-resolved calls
    return (
        "looks like a dotted RPC module path, not a '<DocType>.<method>' grant — guard 0.6.0 "
        "deny-unknown denies it on bare/unresolved calls (it is not in SAFE_METHODS); if the "
        "credential needs the underlying operation, grant the per-doctype pattern(s) instead"
    )


def execute():
    """Frappe patch entry point (`patches.txt`). Best-effort, log-only — see module docstring."""
    try:
        import frappe

        rows = frappe.get_all(
            "API Key Scope Method",
            fields=["parent", "pattern"],
            filters={"parenttype": "API Key Scope"},
        )
        warned = 0
        for row in rows:
            message = grant_warning(row.get("pattern"))
            if message is None:
                continue
            warned += 1
            frappe.log_error(
                title="Pacioli Guard 0.6.0 migrate audit: review this methods grant",
                message=(
                    f"API Key Scope {row.get('parent')!r}, methods pattern "
                    f"{row.get('pattern')!r}: {message}"
                ),
            )
        if warned:
            print(
                f"Pacioli Guard 0.6.0 migrate audit: {warned} methods grant pattern(s) need "
                "review under deny-unknown — see Error Log "
                "('Pacioli Guard 0.6.0 migrate audit: …') for each one."
            )
    except Exception as exc:  # noqa: BLE001 — a failed AUDIT must never fail the MIGRATE
        try:
            print(f"Pacioli Guard 0.6.0 migrate audit skipped (best-effort): {exc}")
        except Exception:
            pass
