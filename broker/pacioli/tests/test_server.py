# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""MCP adapter tests — the dispatch step and content rendering, without the MCP SDK.

The adapter is deliberately thin, but "thin" was where a real bug lived: the ``call_tool`` closure
referenced an unbound ``pacioli`` instead of the ``broker`` it was handed, so the live
``pacioli serve`` server raised ``NameError`` on the first tool call while every other test stayed
green (they drive ``PacioliBroker.dispatch`` directly and never the adapter). These tests exercise
``dispatch_tool`` and ``_as_content`` with a fake broker + a fake ``types`` module, so the one glue
path the SDK-only server used to hide now has coverage.
"""
import asyncio
import json
import sys
import unittest

from pacioli.server import (
    _as_content,
    _register_tool_handlers,
    dispatch_raw,
    dispatch_tool,
    serve,
)


class FakeTextContent:
    def __init__(self, type, text):
        self.type = type
        self.text = text


class FakeTool:
    """Stand-in for ``mcp.types.Tool`` — what ``list_tools`` builds for the advertised surface."""

    def __init__(self, name, description, inputSchema):  # noqa: N803 — the SDK's own kwarg name
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


class FakeTypes:
    """Stand-in for ``mcp.types`` — ``TextContent`` for the render path, ``Tool`` for the
    advertised surface ``list_tools`` builds."""
    TextContent = FakeTextContent
    Tool = FakeTool


class FakeBroker:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def dispatch(self, name, arguments):
        self.calls.append((name, arguments))
        return self._result


class TestDispatchTool(unittest.TestCase):
    def test_dispatches_through_the_broker_it_is_handed(self):
        broker = FakeBroker({"ok": True, "result": {"docstatus": 1}})
        out = dispatch_tool(broker, FakeTypes, "submit_sales_invoice", {"name": "SI-1"})
        # The bug this guards: dispatch must go through `broker`, not a stray module-level name.
        self.assertEqual(broker.calls, [("submit_sales_invoice", {"name": "SI-1"})])
        self.assertEqual(len(out), 1)
        self.assertEqual(json.loads(out[0].text), {"ok": True, "result": {"docstatus": 1}})

    def test_none_arguments_become_an_empty_dict(self):
        broker = FakeBroker({"ok": False, "reason": "x"})
        dispatch_tool(broker, FakeTypes, "plan_submit", None)
        self.assertEqual(broker.calls, [("plan_submit", {})])

    def test_a_governed_denial_renders_as_content_not_an_exception(self):
        broker = FakeBroker({"ok": False, "stage": "consent", "reason": "no marker presented"})
        out = dispatch_tool(broker, FakeTypes, "submit_sales_invoice", {"name": "SI-1"})
        body = json.loads(out[0].text)
        self.assertFalse(body["ok"])
        self.assertEqual(body["stage"], "consent")


class TestDispatchRaw(unittest.TestCase):
    """``dispatch_raw`` -- the ONE locked dispatch core both door renderings wrap (extracted for
    the A2A door, docs/plans/2026-07-16-a2a-door.md F-A2A-4): MCP doors render its dict as MCP
    content, the A2A executor as a DataPart artifact. Same lock, same broker path, no rendering."""

    def test_returns_the_dispatch_dict_unrendered(self):
        broker = FakeBroker({"ok": True, "result": {"docstatus": 1}})
        out = dispatch_raw(broker, "submit_sales_invoice", {"name": "SI-1"})
        self.assertEqual(out, {"ok": True, "result": {"docstatus": 1}})
        self.assertEqual(broker.calls, [("submit_sales_invoice", {"name": "SI-1"})])

    def test_none_arguments_become_an_empty_dict(self):
        broker = FakeBroker({"ok": False, "reason": "x"})
        dispatch_raw(broker, "plan_submit", None)
        self.assertEqual(broker.calls, [("plan_submit", {})])

    def test_dispatch_tool_is_render_over_raw(self):
        # the regression pin for the extraction: dispatch_tool's output must be exactly
        # _as_content over what dispatch_raw returns -- one core, two renderings.
        broker = FakeBroker({"ok": False, "stage": "plan", "reason": "forged"})
        raw = dispatch_raw(FakeBroker({"ok": False, "stage": "plan", "reason": "forged"}),
                           "submit_sales_invoice", {})
        rendered = dispatch_tool(broker, FakeTypes, "submit_sales_invoice", {})
        self.assertEqual(json.loads(rendered[0].text), raw)

    def test_holds_the_dispatch_lock_while_running(self):
        # F5 inherited: dispatch_raw must serialize behind the same process-wide lock the MCP
        # doors use -- probed by having the fake broker OBSERVE the lock state mid-dispatch.
        from pacioli import server as server_mod

        class LockProbeBroker:
            def __init__(self):
                self.locked_during_dispatch = None

            def dispatch(self, name, arguments):
                self.locked_during_dispatch = server_mod._DISPATCH_LOCK.locked()
                return {"ok": True}

        probe = LockProbeBroker()
        dispatch_raw(probe, "x", {})
        self.assertTrue(probe.locked_during_dispatch)


class TestAsContent(unittest.TestCase):
    def test_renders_sorted_deterministic_json(self):
        out = _as_content(FakeTypes, {"b": 2, "a": 1})
        self.assertEqual(out[0].type, "text")
        self.assertEqual(out[0].text, '{"a": 1, "b": 2}')  # sort_keys — stable for the client



class FakeApp:
    """Captures what ``_register_tool_handlers`` registers, the way the MCP SDK would.

    ``Server.list_tools()``/``call_tool()`` are decorator FACTORIES — they return the decorator —
    so the fake has to be shaped the same way or the closures never get built at all."""

    def __init__(self):
        self.registered = {}

    def list_tools(self):
        def deco(fn):
            self.registered["list_tools"] = fn
            return fn
        return deco

    def call_tool(self):
        def deco(fn):
            self.registered["call_tool"] = fn
            return fn
        return deco


class TestTheHandlersTheLiveServerActuallyRegisters(unittest.TestCase):
    """G7 — ``_register_tool_handlers``, the glue between the MCP SDK and the broker.

    This module's own header records why it matters: the ``call_tool`` closure once referenced an
    unbound ``pacioli`` instead of the ``broker`` it was handed, so ``pacioli serve`` raised
    NameError on the first tool call while every test stayed green — because they all drove
    ``PacioliBroker.dispatch`` directly and never the adapter. That bug was fixed and
    ``dispatch_tool`` got tests, but the CLOSURES that call it were still never built by a test.
    Same blind spot, one altitude up: nothing here had executed the code the live server runs.

    These build the real closures through a fake app shaped like ``mcp.server.Server`` and run
    them."""

    def setUp(self):
        self.app = FakeApp()
        self.broker = FakeBroker({"ok": True, "result": {"docstatus": 1}})
        self.served = [{"name": "plan_submit", "description": "PLAN a submit.",
                        "inputSchema": {"type": "object", "properties": {},
                                        "additionalProperties": False}}]
        _register_tool_handlers(self.app, self.broker, FakeTypes, self.served)

    def test_both_handlers_are_registered(self):
        self.assertEqual(set(self.app.registered), {"list_tools", "call_tool"})

    def test_list_tools_advertises_exactly_the_served_list(self):
        tools = asyncio.run(self.app.registered["list_tools"]())
        self.assertEqual([t.name for t in tools], ["plan_submit"])
        self.assertEqual(tools[0].inputSchema["additionalProperties"], False)

    def test_call_tool_dispatches_through_the_broker_it_was_HANDED(self):
        # The original bug, at the layer that actually reaches the client.
        asyncio.run(self.app.registered["call_tool"]("plan_submit", {"name": "SI-1"}))
        self.assertEqual(self.broker.calls, [("plan_submit", {"name": "SI-1"})])

    def test_call_tool_returns_rendered_content_not_a_raw_dict(self):
        out = asyncio.run(self.app.registered["call_tool"]("plan_submit", {"name": "SI-1"}))
        self.assertEqual(out[0].type, "text")
        self.assertEqual(json.loads(out[0].text)["ok"], True)

    def test_a_refusal_is_returned_as_content_too_not_raised(self):
        # A governed refusal is an ANSWER on this door. If the adapter raised instead, the client
        # would see a transport error and no receipt-bearing body.
        app, broker = FakeApp(), FakeBroker({"ok": False, "stage": "consent", "reason": "no marker"})
        _register_tool_handlers(app, broker, FakeTypes, self.served)
        out = asyncio.run(app.registered["call_tool"]("submit_sales_invoice", {}))
        self.assertEqual(json.loads(out[0].text)["stage"], "consent")


class TestServeRefusesToStartRatherThanServeSomethingWrong(unittest.TestCase):
    """``serve()``'s three startup refusals — the first thing an adopter meets when something is
    misconfigured, and every one of them was unexecuted by any test.

    All three must print to stderr and return exit code 2 rather than raising a traceback or, worse,
    starting a door that serves the wrong surface."""

    def _serve(self, env=None, **patched):
        import contextlib
        import io
        from unittest import mock
        err = io.StringIO()
        ctx = (mock.patch.multiple("pacioli.server", **patched) if patched
               else contextlib.nullcontext())
        with contextlib.redirect_stderr(err), ctx:
            code = serve(env if env is not None else {})
        return code, err.getvalue()

    def test_a_typod_toolsets_mode_refuses_before_anything_is_assembled(self):
        # Resolved FIRST by design: nothing is imported or assembled behind a typo.
        code, err = self._serve({"PACIOLI_TOOLSETS": "dynamicc"})
        self.assertEqual(code, 2)
        self.assertIn("dynamicc", err)

    def test_a_failure_to_assemble_the_broker_refuses(self):
        from unittest import mock
        from pacioli.runtime import RuntimeError_
        boom = mock.Mock(side_effect=RuntimeError_("no registry at /etc/pacioli/targets.toml"))
        code, err = self._serve({}, assemble=boom)
        self.assertEqual(code, 2)
        self.assertIn("no registry", err)

    def test_a_missing_mcp_extra_names_the_install_command(self):
        # The adopter-facing one: `pip install pacioli` without [server] must say so, not
        # ImportError. Blocking `mcp` in sys.modules reproduces exactly that install.
        import contextlib
        import io
        from unittest import mock
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.dict(sys.modules, {"mcp": None,
                                                                            "mcp.types": None,
                                                                            "mcp.server": None}):
            code = serve({})
        self.assertEqual(code, 2)
        self.assertIn("pip install", err.getvalue())
        self.assertIn("pacioli[server]", err.getvalue())


class TestServeHttpRefusesTheSameWay(unittest.TestCase):
    """The HTTP door's own startup refusals — the same three shapes as ``serve``, on the transport
    an adopter is most likely to expose. Untested until 2026-08-11."""

    def _serve_http(self, **kw):
        import contextlib
        import io
        from pacioli.server import serve_http
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = serve_http(**kw)
        return code, err.getvalue()

    def test_a_typod_toolsets_mode_refuses_before_the_port_is_bound(self):
        code, err = self._serve_http(env={"PACIOLI_TOOLSETS": "nope"})
        self.assertEqual(code, 2)
        self.assertIn("nope", err)

    def test_a_missing_mcp_extra_names_the_install_command(self):
        from unittest import mock
        import contextlib
        import io
        from pacioli.server import serve_http
        err = io.StringIO()
        blocked = {"mcp": None, "mcp.types": None, "mcp.server": None,
                   "mcp.server.streamable_http_manager": None, "uvicorn": None}
        with contextlib.redirect_stderr(err), mock.patch.dict(sys.modules, blocked):
            code = serve_http(env={})
        self.assertEqual(code, 2)
        self.assertIn("uvicorn", err.getvalue())
        self.assertIn("pacioli[server]", err.getvalue())


class TestTheTokenReferenceRefusalsNoTestHadEntered(unittest.TestCase):
    """``_resolve_transport_token``'s two unexercised refusals. The token guards the HTTP door, so
    every way of failing to read one must refuse rather than yield something falsy — an empty
    token would turn the bearer check into a doorstop that admits everyone."""

    def test_a_non_string_or_blank_reference_is_refused(self):
        from pacioli.server import TransportConfigError, _resolve_transport_token
        for ref in (None, 42, "", "   ", [], {}):
            with self.subTest(ref=ref):
                with self.assertRaises(TransportConfigError):
                    _resolve_transport_token(ref, {})

    def test_an_unreadable_token_file_is_refused_and_says_why(self):
        from pacioli.server import TransportConfigError, _resolve_transport_token
        with self.assertRaises(TransportConfigError) as caught:
            _resolve_transport_token("file:/definitely/not/a/real/path/token", {})
        self.assertIn("unreadable", str(caught.exception))

    def test_the_refusal_never_echoes_an_inline_secret(self):
        # Already covered elsewhere, re-asserted here because it is the property that makes these
        # refusals safe to print: a misconfigured operator must not get their own secret back in
        # a log line.
        from pacioli.server import TransportConfigError, _resolve_transport_token
        with self.assertRaises(TransportConfigError) as caught:
            _resolve_transport_token("sk-live-supersecret-value", {})
        self.assertNotIn("supersecret", str(caught.exception))



class TestWhichLayerActuallyRefusesAnUndeclaredArgument(unittest.TestCase):
    """Driven through a REAL ``mcp.server.Server``, because the fake cannot show this.

    ⚠️ **The 0.37.0 batch shipped a doctrine that had gone stale.** Since 2026-07-26 this codebase
    repeated "neither door validates `inputSchema` — the MCP SDK does not", and built the
    undeclared-argument refusal on it, promising the caller a message naming the accepted keys and
    the correction. The installed SDK (``mcp`` 1.28.x) DOES validate: ``Server.call_tool`` takes
    ``validate_input: bool = True`` and jsonschema-validates against the ADVERTISED schema before
    the registered handler runs. Now that every schema declares ``additionalProperties: false``,
    the SDK refuses first on the advertised path and our message never reaches the caller.

    **The OUTCOME is unchanged and is what matters** — refused, never silently dropped, now
    defended at two independent layers. What differs is the MESSAGE, so any claim about the
    guidance must name the door it reaches. Found by an independent review; pinned here so the
    doctrine cannot rot back.

    ``FakeApp`` above cannot catch this by construction: the real decorator returns the function
    but registers a *wrapper*, and the fake stores the raw closure."""

    def _real_server(self, broker):
        from mcp.server import Server
        import mcp.types as real_types
        from pacioli.doorway import served_tools
        app = Server("pacioli-test")
        _register_tool_handlers(app, broker, real_types, served_tools({}))
        return app, real_types

    def _call(self, app, real_types, name, args):
        handler = app.request_handlers[real_types.CallToolRequest]
        req = real_types.CallToolRequest(
            method="tools/call",
            params=real_types.CallToolRequestParams(name=name, arguments=args))
        result = asyncio.run(handler(req)).root
        return result.isError, (result.content[0].text if result.content else "")

    def test_the_SDK_refuses_an_undeclared_argument_before_our_handler_runs(self):
        broker = FakeBroker({"ok": True})
        app, rt = self._real_server(broker)
        is_error, text = self._call(app, rt, "plan_submit",
                                    {"name": "SI-1", "doctype": "Payment Entry"})
        self.assertTrue(is_error)
        self.assertIn("Additional properties are not allowed", text)
        self.assertIn("doctype", text)
        # THE POINT: our handler never ran, so the broker was never reached.
        self.assertEqual(broker.calls, [], "a rejected call must not reach the broker")

    def test_the_outcome_is_the_same_even_though_the_message_is_not(self):
        # Both layers refuse. Neither silently drops. That is the property 0.37.0 is really about,
        # and it is the one an adopter depends on.
        broker = FakeBroker({"ok": True})
        app, rt = self._real_server(broker)
        is_error, _ = self._call(app, rt, "plan_submit", {"name": "SI-1", "nonsense": 1})
        self.assertTrue(is_error)
        self.assertEqual(broker.calls, [])

    def test_a_correctly_formed_call_still_passes_straight_through(self):
        # The no-regression half: closing the schemas must not reject valid calls.
        broker = FakeBroker({"ok": True, "plan_id": "p1"})
        app, rt = self._real_server(broker)
        is_error, text = self._call(app, rt, "plan_submit", {"name": "SI-1"})
        self.assertFalse(is_error, text)
        self.assertEqual(broker.calls, [("plan_submit", {"name": "SI-1"})])

    def test_our_own_refusal_is_what_answers_where_the_SDK_has_no_schema(self):
        # The doors our message DOES reach, so the claim can be stated accurately rather than
        # dropped: A2A validates nothing, and `pacioli_call`'s INNER arguments are never seen by
        # the SDK — the outer envelope is all it has a schema for.
        from pacioli.tests.test_tools import make_broker
        real_broker, _c, _s = make_broker()
        out = real_broker.dispatch("pacioli_call",
                                   {"tool": "plan_submit",
                                    "arguments": {"name": "SI-1", "doctype": "Payment Entry"}})
        self.assertFalse(out["ok"])
        self.assertIn("pacioli_doctype", out["reason"])  # our correction, delivered

if __name__ == "__main__":
    unittest.main()
