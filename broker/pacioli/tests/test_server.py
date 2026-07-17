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
import json
import unittest

from pacioli.server import _as_content, dispatch_raw, dispatch_tool


class FakeTextContent:
    def __init__(self, type, text):
        self.type = type
        self.text = text


class FakeTypes:
    """Stand-in for ``mcp.types`` — only ``TextContent`` is used by the render path."""
    TextContent = FakeTextContent


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


if __name__ == "__main__":
    unittest.main()
