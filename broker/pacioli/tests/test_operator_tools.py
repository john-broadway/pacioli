# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The operator tools (``pacioli.operator``) — the CLI door's three audit reads, gated at the
spine (``docs/plans/2026-08-17-the-cli-is-a-door.md``, corrected 2026-08-18).

The three names are dispatchable ONLY through a broker that carries the CLI door's own ``via``
stamp. Every agent door (stdio/http/a2a — and ``via=None``, the undeclared legacy shape) must be
refused BEFORE any client or store is touched: the plan's original DOORWAY_TOOLS placement would
have handed every MCP and A2A agent the GL sweep by name, unadvertised but reachable.

Expected names here are LITERAL STRINGS on purpose (the 08-17 circular-test law): the expected
side of a surface assertion must owe nothing to the module under test."""
from __future__ import annotations

import sqlite3
import unittest

from pacioli.doorway import served_tools
from pacioli.operator import CLI_VIA, OPERATOR_NAMES, OPERATOR_TOOLS
from pacioli.registry import load_registry
from pacioli.store import BrokerStore
from pacioli.tools import READ_ONLY_TOOLS, PacioliBroker

OPERATOR_NAME_LITERALS = ("sweep_gl_entries", "get_accounts_settings", "get_reposts")

REG = ('[targets.prod]\nbase_url = "https://erp.example.com"\ncompany = "Example Corp"\n'
       'api_key = "k"\napi_secret = "env:S"\ndefault = true\n')
REG_UNPINNED = ('[targets.prod]\nbase_url = "https://erp.example.com"\n'
                'api_key = "k"\napi_secret = "env:S"\ndefault = true\n')


class _AuditClient:
    """Records every call; answers each of the three reads with a distinct sentinel."""

    def __init__(self):
        self.calls = []

    def sweep_gl_entries(self, company, since, until):
        self.calls.append(("sweep_gl_entries", company, since, until))
        return [{"name": "GL-1", "company": company}]

    def get_accounts_settings(self, fields):
        self.calls.append(("get_accounts_settings", tuple(fields)))
        return {f: 1 for f in fields}

    def get_reposts(self, company, since, until):
        self.calls.append(("get_reposts", company, since, until))
        return [{"name": "REPOST-1", "company": company}]


class _MustNotBeCalled:
    """A refused dispatch must never construct or touch a client."""

    def __getattr__(self, name):
        raise AssertionError(f"client touched ({name}) on a path that must refuse first")


def make_broker(via=None, client=None, reg=REG, store_provider=None):
    kwargs = {}
    if via is not None:
        kwargs["via"] = via
    return PacioliBroker(
        registry=load_registry(toml_text=reg),
        store_provider=store_provider
        or (lambda name: BrokerStore(sqlite3.connect(":memory:"), key=b"k" * 32)),
        client_provider=lambda target: client if client is not None else _AuditClient(),
        now_epoch=lambda: 1_000.0,
        now_date=lambda: "2026-07-01",
        **kwargs,
    )


SWEEP_ARGS = {"since": "2026-06-01 00:00:00", "until": "2026-06-30 23:59:59"}


class TestSpineGate(unittest.TestCase):
    """The operator tools refuse every door but the CLI's, before touching anything."""

    def test_agent_door_dispatch_refuses_sweep_by_name(self):
        broker = make_broker(via={"transport": "stdio", "principal": "local-spawn"},
                             client=_MustNotBeCalled())
        result = broker.dispatch("sweep_gl_entries", dict(SWEEP_ARGS))
        self.assertFalse(result["ok"])
        self.assertIn("operator", result["reason"])

    def test_undeclared_via_refuses_operator_tools(self):
        # via=None is every legacy/in-process path: absence of identity never grants.
        # `operator-only` in the reason is LOAD-BEARING: _MustNotBeCalled raises on any client
        # touch, and dispatch's backstop turns that raise into a structured deny too — so
        # asserting ok:False ALONE passes even with the gate removed (the raise, not the gate,
        # made it False). Asserting the GATE's own words distinguishes a real refusal from a
        # downstream error wearing the same ok:False (lens 1, 2026-08-18).
        broker = make_broker(client=_MustNotBeCalled())
        for name in OPERATOR_NAME_LITERALS:
            result = broker.dispatch(name, dict(SWEEP_ARGS))
            self.assertFalse(result["ok"], name)
            self.assertIn("operator-only", result["reason"], name)

    def test_http_and_a2a_stamps_refuse_too(self):
        for transport in ("http", "a2a"):
            broker = make_broker(via={"transport": transport, "principal": "bearer"},
                                 client=_MustNotBeCalled())
            result = broker.dispatch("sweep_gl_entries", dict(SWEEP_ARGS))
            self.assertFalse(result["ok"], transport)
            self.assertIn("operator-only", result["reason"], transport)

    def test_pacioli_call_cannot_reach_operator_tools_from_agent_door(self):
        # The inner dispatch runs on the SAME broker, so the same stamp refuses it — pinned
        # anyway: pacioli_call is the one by-name path an agent is TOLD to use. The reason
        # must be the GATE's (see test_undeclared_via_refuses_operator_tools) — a bare ok:False
        # would pass on the inner tool's own error too.
        broker = make_broker(via={"transport": "stdio", "principal": "local-spawn"},
                             client=_MustNotBeCalled())
        result = broker.dispatch("pacioli_call",
                                 {"tool": "sweep_gl_entries", "arguments": dict(SWEEP_ARGS)})
        self.assertFalse(result["ok"])
        self.assertIn("operator-only", result["reason"])

    def test_cli_door_dispatch_reaches_sweep(self):
        client = _AuditClient()
        broker = make_broker(via=CLI_VIA, client=client)
        result = broker.dispatch("sweep_gl_entries", dict(SWEEP_ARGS))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["rows"], [{"name": "GL-1", "company": "Example Corp"}])
        # company comes from the registry pin, never from caller args
        self.assertEqual(client.calls, [("sweep_gl_entries", "Example Corp",
                                         SWEEP_ARGS["since"], SWEEP_ARGS["until"])])


class TestSurface(unittest.TestCase):
    """Unadvertised everywhere: not served, not counted, argument-closed."""

    def test_operator_names_absent_from_served_surface(self):
        # Every served mode, not just the default: dynamic mode serves the doorway three, and a
        # regression appending OPERATOR_TOOLS to DOORWAY_TOOLS would leak them here (lens 1,
        # 2026-08-18 — the default-only check missed that shape).
        for env in ({}, {"PACIOLI_TOOLSETS": "all"}, {"PACIOLI_TOOLSETS": "dynamic"}):
            served = {t["name"] for t in served_tools(env)}
            for name in OPERATOR_NAME_LITERALS:
                self.assertNotIn(name, served, (env, name))

    def test_operator_names_match_the_literals(self):
        # Two-sided: the module exports exactly the three the CLI reroute needs.
        self.assertEqual(OPERATOR_NAMES, frozenset(OPERATOR_NAME_LITERALS))
        self.assertEqual({t["name"] for t in OPERATOR_TOOLS}, set(OPERATOR_NAME_LITERALS))

    def test_operator_tools_are_read_only(self):
        for name in OPERATOR_NAME_LITERALS:
            self.assertIn(name, READ_ONLY_TOOLS)

    def test_schemas_closed_to_undeclared_arguments(self):
        for tool in OPERATOR_TOOLS:
            self.assertIs(tool["inputSchema"].get("additionalProperties"), False, tool["name"])

    def test_unknown_arg_refused_naming_the_key(self):
        broker = make_broker(via=CLI_VIA, client=_MustNotBeCalled())
        result = broker.dispatch("sweep_gl_entries", dict(SWEEP_ARGS, bogus=1))
        self.assertFalse(result["ok"])
        self.assertIn("bogus", result["reason"])


class TestHandlers(unittest.TestCase):
    """The three reads, on the CLI door."""

    def test_sweep_refuses_company_unpinned_target(self):
        broker = make_broker(via=CLI_VIA, client=_MustNotBeCalled(), reg=REG_UNPINNED)
        result = broker.dispatch("sweep_gl_entries", dict(SWEEP_ARGS))
        self.assertFalse(result["ok"])
        self.assertIn("company", result["reason"])

    def test_reposts_refuses_company_unpinned_target(self):
        broker = make_broker(via=CLI_VIA, client=_MustNotBeCalled(), reg=REG_UNPINNED)
        result = broker.dispatch("get_reposts", dict(SWEEP_ARGS))
        self.assertFalse(result["ok"])

    def test_sweep_requires_both_bounds(self):
        broker = make_broker(via=CLI_VIA, client=_MustNotBeCalled())
        result = broker.dispatch("sweep_gl_entries", {"since": "2026-06-01 00:00:00"})
        self.assertFalse(result["ok"])

    def test_get_accounts_settings_passes_fields_verbatim(self):
        client = _AuditClient()
        broker = make_broker(via=CLI_VIA, client=client)
        fields = ["enable_immutable_ledger", "delete_linked_ledger_entries"]
        result = broker.dispatch("get_accounts_settings", {"fields": list(fields)})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["settings"], {f: 1 for f in fields})
        self.assertEqual(client.calls, [("get_accounts_settings", tuple(fields))])

    def test_get_reposts_happy_path(self):
        client = _AuditClient()
        broker = make_broker(via=CLI_VIA, client=client)
        result = broker.dispatch("get_reposts", dict(SWEEP_ARGS))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["rows"], [{"name": "REPOST-1", "company": "Example Corp"}])

    def test_operator_reads_survive_a_sealed_store(self):
        # The close path is the confession path: a sealed store must never hide the books
        # that explain it (Global constraint #6) — same law as every other read.
        stores = {}

        def store_provider(name):
            if name not in stores:
                stores[name] = BrokerStore(sqlite3.connect(":memory:"), key=b"k" * 32)
            return stores[name]

        broker = make_broker(via=CLI_VIA, client=_AuditClient(), store_provider=store_provider)
        store_provider("prod").seal("incident under investigation", source="operator")
        result = broker.dispatch("sweep_gl_entries", dict(SWEEP_ARGS))
        self.assertTrue(result["ok"], result)


class TestOneSpine(unittest.TestCase):
    """The ruling, guarded against accidental regression: after the CLI reroute, NO module in
    the package constructs an :class:`ErpnextClient` of its own — the client's home module
    aside, the one construction site is ``runtime.assemble``'s client_provider, so every ERPNext
    call in the package flows through a provider a dispatch handler reaches. This test was RED
    while ``cli.py`` (close --reconcile) built its own client.

    SCOPE (lens 1, 2026-08-18): this is an AST check, so it catches every static spelling of the
    construction — a plain ``ErpnextClient(...)``, an aliased ``from pacioli.erpnext import
    ErpnextClient as X; X(...)``, and any attribute call ``…erpnext.ErpnextClient(...)`` — not
    only the literal ``ErpnextClient(`` substring the first version grepped for (which an
    aliased import defeated silently). It does NOT catch a deliberately obfuscated
    ``getattr(erpnext, "Erpnext"+"Client")(...)``; no static check can, and that is not the
    regression this guards — a future session re-adding a direct client writes the call
    plainly. The claim is "guards accidental regression," never "unbypassable.\""""

    def test_no_client_construction_outside_erpnext_and_runtime(self):
        import ast
        import pathlib

        import pacioli
        pkg = pathlib.Path(pacioli.__file__).parent
        offenders = []
        for path in sorted(pkg.glob("*.py")):
            if path.name in ("erpnext.py", "runtime.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            # Names locally bound to the ErpnextClient symbol by an import (direct or aliased).
            bound = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == "ErpnextClient":
                            bound.add(alias.asname or alias.name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                # `X(...)` where X was imported as ErpnextClient, or any `<expr>.ErpnextClient(...)`
                # (covers `erpnext.ErpnextClient`, `pacioli.erpnext.ErpnextClient`, however the
                # module was imported).
                if ((isinstance(func, ast.Name) and func.id in bound)
                        or (isinstance(func, ast.Attribute) and func.attr == "ErpnextClient")):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "ErpnextClient constructed outside erpnext.py/runtime.py — a direct "
                         "path around the spine (docs/plans/2026-08-17-the-cli-is-a-door.md)")


if __name__ == "__main__":
    unittest.main()
