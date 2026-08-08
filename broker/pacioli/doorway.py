# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The DOORWAY — a searchable catalog instead of 265 resident tool schemas.

WHY THIS EXISTS. The broker's ``tools/list`` payload is ~229k bytes across 265 tools — roughly
57k-65k tokens delivered to every client before its first question. A harness that defers schemas
hides that cost; a vanilla MCP client eats all of it, and a small local model cannot connect at
all: the catalog exhausts the window. The six spine tools are ~7.5k bytes. What closes a gap this
shape is making the catalog NON-RESIDENT (the pattern proven in Proximo's lean mode, the 0.30
flip — ported here per ``docs/plans/2026-08-08-doorway-design.md``).

Opt-in via ``PACIOLI_TOOLSETS=dynamic``; unset (or ``all``) serves today's surface byte-identical.
In dynamic mode the door advertises nine tools — this module's three plus the PLAN/PROVE spine —
and every catalog tool stays callable: advertisement is a context choice, never reachability, and
never authorization (the guard enforces at the floor regardless of what any door advertises).

    pacioli_find_tools(query)       search names + one-line summaries
    pacioli_tool_schema(name)       the full schema, for the one or two that matched
    pacioli_call(tool, arguments)   dispatch through the full governed spine

THE SAFETY INVARIANT. ``pacioli_call`` dispatches through :meth:`PacioliBroker.dispatch` and
NOTHING else — the handler lives beside the others in :mod:`pacioli.tools` and the two reasons
are stated there. This module is deliberately pure: search and lookup take the catalog as an
argument and touch no globals, so they are testable without standing up a server, and import
NOTHING from :mod:`pacioli.tools` at module level (tools.py imports this module; the one place
the real catalog is needed, :func:`served_tools`, imports it lazily).
"""
from __future__ import annotations

import difflib
import os

# A search result's summary is the first sentence of the description, capped. The cap is the
# point: a result set carrying full descriptions would re-import the cost this mode removes.
_SUMMARY_MAX = 140
LIMIT_MIN, LIMIT_MAX, LIMIT_DEFAULT = 1, 50, 10

# Near-exact renames ONLY (Proximo's measured law: concept-level expansions land on words most
# descriptions contain, the AND stops filtering, and the blowup returns through the front door).
# ``plan_`` carries its underscore deliberately: bare "plan" matches every *_production_plan
# tool ahead of the PLAN spine (measured on this catalog, design review finding 4).
_SYNONYMS = {
    "post": ("submit",),
    "void": ("cancel",),
    "preview": ("plan_",),
    "dry-run": ("plan_",),
    "dryrun": ("plan_",),
    "verify": ("prove",),
    "audit": ("prove", "receipt"),
    "undo": ("cancel", "amend"),
}


def _variants(term: str) -> tuple[str, ...]:
    """A query term plus its synonyms — widened, never replaced: "post invoice" must find
    ``submit_sales_invoice`` via post->submit while "submit invoice" still finds it as typed."""
    return (term, *_SYNONYMS.get(term, ()))


def _summary_of(description: str) -> str:
    """First line/sentence of a tool description, collapsed to one short line."""
    first_line = (description or "").strip().split("\n", 1)[0].strip()
    sentence, _, _ = first_line.partition(". ")
    text = (sentence or first_line).strip()
    if len(text) > _SUMMARY_MAX:
        text = text[: _SUMMARY_MAX - 1].rstrip() + "…"
    return text


def search_tools(tools: list[dict], query: str, limit: int | None = None) -> list[dict]:
    """Term-wise AND search over tool names + descriptions, ranked by where the terms landed.

    Substring per TERM, not per phrase (models type phrases that appear nowhere contiguously);
    AND, not OR (an OR match on a common word returns most of the catalog — the exact payload
    this mode exists to remove). A blank query RAISES rather than returning everything.

    Rank, because the limit truncates: exact name, then every term in the name, then part of the
    name, then prose-only mentions LAST — a tool that merely mentions a term in its description
    must never outrank the tool whose name says it. Within a tier, more ORIGINAL terms (not
    synonym stand-ins) matching the name wins: measured on this catalog, "verify" must put
    ``prove_verify`` (its own name) above ``prove_orphans`` (reached only via the verify->prove
    synonym). Ties break alphabetically so repeated searches stay stable.

    ``limit`` is clamped to [1, 50] (default 10) — no door validates inputSchema, so the clamp is
    the enforcement (the 2026-07-26 redteam class).
    """
    if not isinstance(query, str):
        raise ValueError("query must be a string of keywords")
    terms = [t for t in query.strip().lower().split() if t]
    if not terms:
        raise ValueError("query must not be blank — the doorway never returns the whole catalog")
    if limit is None:
        limit = LIMIT_DEFAULT
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = LIMIT_DEFAULT
        limit = max(LIMIT_MIN, min(LIMIT_MAX, limit))

    variants_per_term = [_variants(t) for t in terms]

    scored: list[tuple[int, int, str, dict]] = []
    for tool in tools:
        name = tool["name"]
        lname = name.lower()
        ldesc = (tool.get("description") or "").lower()
        if not all(any(v in lname or v in ldesc for v in vs) for vs in variants_per_term):
            continue
        in_name = sum(any(v in lname for v in vs) for vs in variants_per_term)
        original_in_name = sum(t in lname for t in terms)
        if lname == "_".join(terms) or lname == "".join(terms):
            rank = 0                      # exact name
        elif in_name == len(terms):
            rank = 1                      # every term in the name
        elif in_name:
            rank = 2                      # name carries part of it
        else:
            rank = 3                      # prose-only mention
        scored.append((rank, -original_in_name, name,
                       {"name": name, "summary": _summary_of(tool.get("description") or "")}))

    # Rank is the FIRST sort key, so prose-only hits (rank 3 — the weakest evidence this search
    # produces) already fill last and never spend the limit before name matches do. (lean.py
    # needed an explicit strong/prose split only because its tier-fills sat between; ported
    # without the fills, the split was dead code — redteam M8.)
    scored.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3] for row in scored][:limit]


def tool_schema(tools: list[dict], name: str) -> dict:
    """Full description + JSON input schema for one catalog tool.

    Fail-closed on an unknown name, with near-miss suggestions: search-then-call loops mistype
    names, and a refusal carrying candidates is recoverable where an empty schema is not — a
    model handed ``{}`` would call blind, the worst outcome available here.
    """
    by_name = {t["name"]: t for t in tools}
    if not isinstance(name, str) or name not in by_name:
        near = difflib.get_close_matches(str(name), by_name, n=3, cutoff=0.6)
        hint = f" — did you mean: {', '.join(near)}" if near else ""
        raise KeyError(f"unknown tool {name!r}{hint}")
    tool = by_name[name]
    return {"name": name, "description": tool.get("description") or "",
            "inputSchema": tool["inputSchema"]}


# --- the doorway's own surface -------------------------------------------------------------
# These descriptions are the model's WHOLE MAP of a 265-tool catalog it cannot see, so each
# states its limits NEGATIVELY and says what to do instead (a tool description is a prompt;
# what the schema omits, the model fills in and states confidently). They must name: summaries
# are not schemas; zero results means the tool does not exist; there are no create_*/delete_*
# tools; writes still need plan + marker.

_TOOL_PROP = {"tool": {"type": "string",
                       "description": "Exact catalog tool name (from pacioli_find_tools)."}}

DOORWAY_TOOLS: list[dict] = [
    {
        "name": "pacioli_find_tools",
        "description": (
            "Search the governed tool catalog by keywords (e.g. 'submit invoice', 'list stock "
            "entries', 'cancel payment'). Returns up to `limit` (default 10, max 50) matches as "
            "{name, summary} rows — one-line summaries, NOT schemas: read "
            "pacioli_tool_schema(name) before calling anything you found. The catalog is "
            "per-doctype: get_*/list_* reads; plan_submit/submit_*, plan_cancel/cancel_*, "
            "plan_cascade_cancel/cascade_cancel, plan_reconcile/reconcile and amend_* governed "
            "writes; workflow_status/request_workflow_transition; prove_verify/prove_orphans. "
            "ALL keywords must match, so ZERO results usually means your WORDS missed, not that "
            "the capability is missing: retry with catalog vocabulary — one verb from the list "
            "above plus the doctype noun ('list sales invoice', 'submit payment entry'), not "
            "operator phrasing ('unpaid', 'refund', 'approve'). Only after such retries "
            "conclude the surface does not offer it, and report that plainly — NEVER invent or "
            "guess a tool name. There are no create_* or delete_* tools: drafts are authored in "
            "ERPNext (or by amend_*), deletion is not offered, and approvals go through "
            "request_workflow_transition."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Keywords, ALL of which must match (name or "
                                         "description). Must not be blank."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                          "description": "Max results (default 10)."}},
            "required": ["query"],
        },
    },
    {
        "name": "pacioli_tool_schema",
        "description": (
            "The full description and JSON input schema for ONE catalog tool, by exact name "
            "(from pacioli_find_tools). Unknown names are refused with near-miss suggestions — "
            "never call a tool whose schema you have not read, and never retry a refused name "
            "unchanged: pick a suggestion or search again."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string",
                                    "description": "Exact catalog tool name."}},
            "required": ["name"],
        },
    },
    {
        "name": "pacioli_call",
        "description": (
            "Dispatch one catalog tool through the FULL governed spine — the same gates, "
            "receipts and refusals as calling it directly, result returned verbatim. `tool` is "
            "the exact catalog name; `arguments` is that tool's own arguments object, and "
            "EVERYTHING the tool takes goes inside it — pacioli_target, consent_token, plan_id, "
            "marker are never valid at this level. Governed writes still require their recorded "
            "plan_* and a live human-minted consent marker; the doorway never bypasses, batches "
            "or substitutes for either. The doorway does not dispatch itself."),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_TOOL_PROP,
                "arguments": {"type": "object",
                              "description": "The named tool's own arguments, exactly as its "
                                             "schema (pacioli_tool_schema) declares them."}},
            "required": ["tool"],
        },
    },
]

DOORWAY_NAMES = frozenset(t["name"] for t in DOORWAY_TOOLS)

# The spine stays at the door: PLAN and PROVE resident means the governed flow is visible before
# the first search — a tool description is a prompt, and these nine ARE the dynamic-mode prompt.
RESIDENT_SPINE = ("plan_submit", "plan_cancel", "plan_cascade_cancel", "plan_reconcile",
                  "prove_verify", "prove_orphans")

_MODES = ("dynamic", "all")


def served_tools(env=None) -> list[dict]:
    """The tool list a door ADVERTISES, per ``PACIOLI_TOOLSETS`` — resolved once at door start
    (later env mutation is inert by design; same ``env=None -> os.environ`` rule as
    ``runtime.assemble``).

    - unset / empty / ``all`` → the full catalog, byte-identical to the pre-doorway surface
      (the doorway tools are callable but not advertised — advertisement is not reachability).
    - ``dynamic`` → nine residents: the three doorway tools + :data:`RESIDENT_SPINE`.
    - anything else → ``ValueError`` — a typo'd mode REFUSES TO START rather than serving a
      surprise; every door treats this exactly like a transport-config refusal (exit 2).

    Matching is exact on the stripped, lowercased value; empty string reads as unset (the
    common ``PACIOLI_TOOLSETS=`` shell form means "not configured", not "refuse").
    """
    from pacioli.tools import TOOLS  # lazy: tools.py imports this module at top level

    raw = (env if env is not None else os.environ).get("PACIOLI_TOOLSETS", "")
    mode = (raw or "").strip().lower() or "all"
    if mode not in _MODES:
        raise ValueError(
            f"PACIOLI_TOOLSETS={raw.strip()!r} is not a mode — valid: dynamic (the 9-tool "
            "doorway; every catalog tool stays callable through it), all (the full resident "
            "catalog, the default). Refusing to start rather than serving a surprise.")
    if mode == "dynamic":
        spine = {t["name"]: t for t in TOOLS}
        return DOORWAY_TOOLS + [spine[n] for n in RESIDENT_SPINE]
    return list(TOOLS)
