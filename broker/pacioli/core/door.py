# Copyright (c) 2026, John Broadway and contributors
# License: AGPL-3.0-or-later
"""THE DOOR: four tools over the table, plus the two PROVE tools, instead of 265 resident schemas.

A door admits; it never decides. These four decide nothing: they look an address up in the table,
let the policy refuse by its sentence, and hand the call to the catalog tool that already governs
that act, through ``PacioliBroker.dispatch`` and nothing else. The spine order (PLAN, closed books,
CONSENT, claim, execute, PROVE) runs inside the inner tool exactly as before.

    pacioli_find(query)                      words to addresses: "<Doctype> <verb>", with status and why
    pacioli_plan(doctype, act, name)         PLAN a submit, a cancel, or a cascade cancel
    pacioli_read(doctype, name | filters)    one document, or a list
    pacioli_act(doctype, act, name, plan_id, marker)   the governed submit / cancel / amend

``route`` is pure: it takes the table and the catalog's doctype slugs as arguments and returns the
inner tool and its arguments, or the refusal. The broker wires it in with five lines per tool.
"""
from __future__ import annotations

from pacioli.core.table import Table

PLAN_ACTS = {"submit": "plan_submit", "cancel": "plan_cancel", "cascade_cancel": "plan_cascade_cancel"}
ACT_TOOLS = {"submit": "submit_{slug}", "cancel": "cancel_{slug}", "amend": "amend_{slug}"}

_TARGET = {"type": "string", "description": "Which books (registry target). Omit for the default target."}
_DOCTYPE = {"type": "string", "description": "The DocType name as frappe spells it, e.g. 'Sales Invoice'."}

CORE_TOOLS: list[dict] = [
    {
        "name": "pacioli_find",
        "description": (
            "Words to addresses. Returns up to `limit` rows of {address, status, why} where address "
            "is '<Doctype> <verb>' and verb is one of get, list, submit, cancel, amend. status is "
            "served (the door governs it), held (refused, with the sentence in why), or unjudged "
            "(refused until a person writes its row). Every word must match the doctype's name or "
            "module, so zero results means your words missed: retry with the DocType noun "
            "('sales invoice', 'payment entry', 'stock'), not operator phrasing. NEVER invent an "
            "address; ask here first."),
        "inputSchema": {"type": "object",
                        "properties": {"query": {"type": "string", "description": "Keywords; not blank."},
                                       "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                        "required": ["query"], "additionalProperties": False},
    },
    {
        "name": "pacioli_plan",
        "description": (
            "PLAN an act before it happens: the memorandum. act is submit, cancel or cascade_cancel. "
            "Returns the plan with ERPNext's own ledger preview, bound to the draft's current version, "
            "refused if the books are closed for that date. Nothing posts. Hand the plan_id and a human-"
            "minted consent marker to pacioli_act."),
        "inputSchema": {"type": "object",
                        "properties": {"doctype": _DOCTYPE,
                                       "act": {"type": "string", "enum": sorted(PLAN_ACTS)},
                                       "name": {"type": "string", "description": "The document name."},
                                       "consent_token": {"type": "string",
                                                         "description": "Only where the floor requires it for the preview."},
                                       "pacioli_target": _TARGET},
                        "required": ["doctype", "act", "name"], "additionalProperties": False},
    },
    {
        "name": "pacioli_read",
        "description": (
            "Read one document (give name) or list documents (give filters and/or limit). Reads are "
            "governed by the seat's scope on the floor, never by this door."),
        "inputSchema": {"type": "object",
                        "properties": {"doctype": _DOCTYPE,
                                       "name": {"type": "string"},
                                       "filters": {"type": "object"},
                                       "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                       "pacioli_target": _TARGET},
                        "required": ["doctype"], "additionalProperties": False},
    },
    {
        "name": "pacioli_act",
        "description": (
            "The governed act: submit or cancel with a plan_id from pacioli_plan and a marker a HUMAN "
            "minted for that exact document (never this seat); or amend, which drafts the corrected "
            "copy after a governed cancel and posts nothing. Every act leaves a receipt in the chain "
            "prove_verify checks. There is no delete: the accountant's undo is cancel then amend."),
        "inputSchema": {"type": "object",
                        "properties": {"doctype": _DOCTYPE,
                                       "act": {"type": "string", "enum": sorted(ACT_TOOLS)},
                                       "name": {"type": "string"},
                                       "plan_id": {"type": "string"},
                                       "marker": {"type": "string"},
                                       "consent_token": {"type": "string"},
                                       "pacioli_target": _TARGET},
                        "required": ["doctype", "act", "name"], "additionalProperties": False},
    },
]
CORE_NAMES = frozenset(t["name"] for t in CORE_TOOLS)
CORE_RESIDENT_SPINE = ("prove_verify", "prove_orphans")
_DECLARED = {t["name"]: frozenset(t["inputSchema"]["properties"]) for t in CORE_TOOLS}


def _carry(args: dict, *keys: str) -> dict:
    return {k: args[k] for k in keys if k in args and args[k] is not None}


def route(table: Table, name: str, args: dict, slugs: dict[str, tuple[str, str]]) -> tuple[str | None, dict, str | None]:
    """(inner_tool, inner_args, None) or (None, {}, reason). Pure: no I/O, no dispatch.

    `slugs` maps a doctype the catalog already wraps to its (singular, plural) tool slug. The table
    says what the vendor has and what the policy serves; `slugs` says what the catalog can execute
    today. The two are kept apart on purpose so a served-but-unwired doctype refuses with the true
    reason (wiring) rather than a false one (policy)."""
    if not isinstance(args, dict):
        return None, {}, f"{name} takes an object"
    unknown = sorted(set(args) - _DECLARED[name])
    if unknown:
        return None, {}, f"{name} does not take {unknown}; it takes {sorted(_DECLARED[name])}"
    doctype, act = args.get("doctype"), args.get("act")
    if not isinstance(doctype, str) or not doctype.strip():
        return None, {}, f"{name} requires 'doctype': the DocType name as frappe spells it"
    if name == "pacioli_read":
        if "name" in args and "filters" in args:
            return None, {}, "pacioli_read takes name (one document) OR filters (a list), not both"
        if "name" in args and not (isinstance(args["name"], str) and args["name"].strip()):
            return None, {}, "pacioli_read 'name' must be a non-empty document name; omit it to list"
        verb = "get" if "name" in args else "list"
    elif name == "pacioli_plan":
        if act not in PLAN_ACTS:
            return None, {}, f"pacioli_plan act must be one of {sorted(PLAN_ACTS)}, got {act!r}"
        verb = "cancel" if act == "cascade_cancel" else act
    else:
        if act not in ACT_TOOLS:
            return None, {}, f"pacioli_act act must be one of {sorted(ACT_TOOLS)}, got {act!r}"
        verb = act
    v = table.judge(doctype, verb)
    if not v.ok:
        return None, {}, v.reason
    if name != "pacioli_read" and not isinstance(args.get("name"), str):
        return None, {}, f"{name} requires 'name': the document to {verb}"
    pair = slugs.get(doctype)
    if pair is None:
        return None, {}, (f"{doctype} {verb} is in the vendor's table and the policy serves it, but the catalog "
                          f"has no {verb} tool wired for it yet; the wiring is the next cut, not a refusal "
                          "of the act")
    slug, plural = pair
    if name == "pacioli_plan":
        inner = {"name": args["name"], "pacioli_doctype": doctype, **_carry(args, "consent_token", "pacioli_target")}
        return PLAN_ACTS[act], inner, None
    if name == "pacioli_read":
        if verb == "get":
            return f"get_{slug}", {"name": args["name"], **_carry(args, "pacioli_target")}, None
        return f"list_{plural}", _carry(args, "filters", "limit", "pacioli_target"), None
    if act in ("submit", "cancel"):
        missing = [k for k in ("plan_id", "marker") if not isinstance(args.get(k), str)]
        if missing:
            return None, {}, (f"pacioli_act {act} requires {missing}: plan_id from pacioli_plan and a marker a "
                              "human minted for this document")
        inner = {"name": args["name"], **_carry(args, "plan_id", "marker", "consent_token", "pacioli_target")}
        return ACT_TOOLS[act].format(slug=slug), inner, None
    return ACT_TOOLS["amend"].format(slug=slug), {"name": args["name"], **_carry(args, "pacioli_target")}, None


def find(table: Table, args: dict) -> dict:
    if not isinstance(args, dict):
        return {"ok": False, "error": "pacioli_find takes an object with 'query'"}
    unknown = sorted(set(args) - _DECLARED["pacioli_find"])
    if unknown:
        return {"ok": False, "error": f"pacioli_find does not take {unknown}"}
    try:
        rows = table.find(args.get("query"), args.get("limit") or 10)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "results": rows, "versions": table.versions(),
            "note": "status served = pacioli_plan then pacioli_act; held and unjudged refuse, with the why"}
