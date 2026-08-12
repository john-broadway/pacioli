# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The door, driven through the REAL mcp SDK, on whichever major is installed.

Every other adapter test uses fakes, and fakes are why this defect class keeps recurring: mcp 2.0.0
removed the decorator registration API on 2026-07-28 and CI stayed green for fourteen days because
nothing here ever constructed a real Server. These tests do, and they assert the OUTCOME rather
than the SDK's wording, because 1.10+ validates tool arguments and 2.x does not.
"""
import json
import unittest

try:
    import anyio
    import mcp.types as types
    from mcp.client.session import ClientSession
    from mcp.server import Server
    HAVE_SDK = True
except ImportError:                                    # pragma: no cover - env without [server]
    HAVE_SDK = False

from pacioli.doorway import served_tools
from pacioli.server import build_server
from pacioli.tests.test_tools import make_broker

SERVED = [{"name": "plan_submit",
           "description": "PLAN a submit.",
           "inputSchema": {"type": "object",
                           "properties": {"pacioli_doctype": {"type": "string"}},
                           "additionalProperties": False}}]

# The REAL plan_submit entry, pulled from the full catalog `serve()` actually advertises
# (`served_tools({})`, 265 tools) rather than the hand-written SERVED above. Used only by the
# refusal test, so that ONE schema, not two, backs the refusal on both majors: on 1.x the SDK
# validates a call against this schema before the handler runs, and on 2.x the real broker
# enforces the SAME schema at dispatch. `SERVED` stays the synthetic, one-tool list for the two
# tests that isolate the call/list paths cheaply.
_REAL_PLAN_SUBMIT = [t for t in served_tools({}) if t["name"] == "plan_submit"]
assert len(_REAL_PLAN_SUBMIT) == 1, "plan_submit vanished from the catalog; this test no longer proves what it claims"


class _Broker:
    def __init__(self):
        self.calls = []

    def dispatch(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True, "tool": name, "args": arguments}


async def _round_trip(broker, work, served=SERVED):
    """Run the real server and a real client over in-process streams, then run `work(session)`."""
    app = build_server(Server, types, broker, served)
    c2s_send, c2s_recv = anyio.create_memory_object_stream(16)
    s2c_send, s2c_recv = anyio.create_memory_object_stream(16)
    result = {}
    async with anyio.create_task_group() as tg:
        async def _serve():
            with anyio.CancelScope() as scope:
                result["scope"] = scope
                await app.run(c2s_recv, s2c_send, app.create_initialization_options())
        tg.start_soon(_serve)
        async with ClientSession(s2c_recv, c2s_send) as session:
            await session.initialize()
            result["value"] = await work(session)
        result["scope"].cancel()
    return result["value"]


@unittest.skipUnless(HAVE_SDK, "the mcp SDK is not installed (pacioli[server])")
class TestTheDoorAgainstTheRealSDK(unittest.TestCase):
    def test_the_door_starts_and_advertises_exactly_the_served_list(self):
        async def work(session):
            return await session.list_tools()
        out = anyio.run(_round_trip, _Broker(), work)
        self.assertEqual([t.name for t in out.tools], [t["name"] for t in SERVED])

    def test_a_governed_call_round_trips_through_the_broker(self):
        broker = _Broker()

        async def work(session):
            return await session.call_tool("plan_submit", {"pacioli_doctype": "Sales Invoice"})
        out = anyio.run(_round_trip, broker, work)
        self.assertEqual(broker.calls,
                         [("plan_submit", {"pacioli_doctype": "Sales Invoice"})])
        self.assertEqual(json.loads(out.content[0].text)["ok"], True)

    def test_an_undeclared_argument_is_refused_on_whichever_major_is_installed(self):
        """The OUTCOME, not the message. mcp 1.10+ refuses in the SDK before our handler; 2.x does
        not validate at all and our dispatch refuses. Either way the caller does not get a success
        for a call it misspelled, and nothing is claimed or spent.

        This is the ONE test in this file that needs the REAL broker, not ``_Broker``, and the
        REAL ``plan_submit`` schema (``_REAL_PLAN_SUBMIT``), not the hand-written ``SERVED``.
        ``_Broker`` always answers ``ok: True`` no matter what it is handed, so on 2.x (no
        SDK-side schema validation) an undeclared argument would sail straight through to it and
        the call would read as a success: the stub would be asserting that a stub does not
        refuse, which proves nothing about the door. The real ``PacioliBroker`` (built by
        :func:`pacioli.tests.test_tools.make_broker`) enforces its own ``additionalProperties`` at
        ``dispatch`` and answers ``ok: False`` for exactly this call, which is the layer 2.x
        actually depends on. And advertising the REAL schema rather than the synthetic one means
        the refusal on 1.x (the SDK validating before the handler runs) and the refusal on 2.x
        (the broker validating in ``dispatch``) are backed by the SAME schema, not two different
        ones that happen to agree on this one case."""
        broker, _client, _store_provider = make_broker()

        async def work(session):
            return await session.call_tool("plan_submit",
                                           {"name": "SI-1", "doctype": "Sales Invoice"})
        out = anyio.run(_round_trip, broker, work, _REAL_PLAN_SUBMIT)
        refused = bool(getattr(out, "isError", False) or getattr(out, "is_error", False)) or \
            json.loads(out.content[0].text).get("ok") is False
        self.assertTrue(refused, "an undeclared argument must not succeed")

    def test_the_real_catalog_meets_the_real_sdk_on_whichever_major_is_installed(self):
        """The other three tests in this class use a synthetic one-tool SERVED so the call and
        refusal paths stay cheap and isolated. But `serve()` advertises `served_tools(env)`, the
        FULL catalog (265 tools as of this writing, never hardcoded here), and nothing else in
        this suite hands that real, complete list to a real `mcp.server.Server`. If a 2.x point
        release tightened `types.Tool` on some field none of the synthetic SERVED's one tool
        exercises, every test in this file could stay green on 2.x and only 1.x would catch it.
        This proves the real, full catalog constructs and lists cleanly through a real
        `tools/list` round trip on whichever major is installed."""
        catalog = served_tools({})

        async def work(session):
            return await session.list_tools()
        out = anyio.run(_round_trip, _Broker(), work, catalog)
        self.assertEqual(len(out.tools), len(catalog))
