# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The OPERATOR tools — the CLI door's three audit reads, gated at the spine.

``close --reconcile`` was the one path in the package that constructed its own
:class:`~pacioli.erpnext.ErpnextClient` outside :meth:`~pacioli.tools.PacioliBroker.dispatch`
(``docs/plans/2026-08-17-the-cli-is-a-door.md``). Routing it through the spine needed three reads
no catalog tool covers — GL Entry sweep, Accounts Settings posture, Repost Accounting Ledger —
and none of them belongs in ``TOOLS`` (they are reads; the catalog's 265 is "the full submittable
transaction surface" and must stay true) nor in ``DOORWAY_TOOLS`` (dispatch reaches doorway names
from EVERY door, so that placement would hand each MCP/A2A agent the GL sweep by name,
unadvertised but reachable — the corrected plan, 2026-08-18).

So: a third category. In the dispatch table, absent from every served surface, and refused by
``dispatch()`` itself unless the broker's own door stamp says ``transport == "cli"`` — a SPINE
rule, not a door decision ("a door admits, it never decides"). Deny-biased: a broker with no
stamp (``via=None``, every legacy in-process path) is refused too — absence of identity never
grants. ``company`` comes from the registry target's pin, never from caller arguments, mirroring
the CLI's own company-scoped-sweep refusal.
"""
from __future__ import annotations

# The CLI door's stamp — the shape the other three doors already use (server.STDIO_VIA,
# server._http_via, a2a._a2a_via). ``assemble(via=CLI_VIA)`` threads it into every store this
# broker opens AND onto the broker itself, which is what the dispatch gate reads.
CLI_VIA = {"transport": "cli", "principal": "local-operator"}

_TARGET_PROP = {
    "pacioli_target": {
        "type": "string",
        "description": "Registry target to route to (omit to use the default target).",
    }
}

_BOUND = {
    "type": "string",
    "description": "Frappe server-clock bound, 'YYYY-MM-DD HH:MM:SS' (site domain — the GL "
                   "`creation` axis is the SITE clock; the CLI converts before calling).",
}

OPERATOR_TOOLS: list[dict] = [
    {
        "name": "sweep_gl_entries",
        "description": (
            "OPERATOR-ONLY (local CLI door): every GL Entry created in [since, until] for the "
            "target's pinned company — the close --reconcile audit sweep. Refused on every "
            "agent door."),
        "inputSchema": {
            "type": "object",
            "properties": {"since": dict(_BOUND), "until": dict(_BOUND), **_TARGET_PROP},
            "required": ["since", "until"],
        },
    },
    {
        "name": "get_accounts_settings",
        "description": (
            "OPERATOR-ONLY (local CLI door): read named Accounts Settings fields — the "
            "immutable-ledger posture snapshot for close --reconcile. Refused on every "
            "agent door."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fields": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                           "description": "Accounts Settings field names to read."},
                **_TARGET_PROP},
            "required": ["fields"],
        },
    },
    {
        "name": "get_reposts",
        "description": (
            "OPERATOR-ONLY (local CLI door): Repost Accounting Ledger documents created in "
            "[since, until] for the target's pinned company — second-generation attribution "
            "for close --reconcile. Refused on every agent door."),
        "inputSchema": {
            "type": "object",
            "properties": {"since": dict(_BOUND), "until": dict(_BOUND), **_TARGET_PROP},
            "required": ["since", "until"],
        },
    },
]

for _tool in OPERATOR_TOOLS:
    # Closed to undeclared arguments, exactly like the catalog and the doorway (`_close_schemas`
    # / doorway.py's identical loop): `tools._unknown_args_deny` enforces it, the declaration is
    # what a validating reader sees, and a loop means a fourth operator tool cannot be born open.
    _tool["inputSchema"]["additionalProperties"] = False

OPERATOR_NAMES = frozenset(t["name"] for t in OPERATOR_TOOLS)
