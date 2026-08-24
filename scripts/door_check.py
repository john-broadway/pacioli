# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Prove the INSTALLED broker's MCP door builds and answers, credential-free.

Run by ``scripts/install_smoke.sh`` inside the smoke venv, against the artifact that
was just installed there, not against this repo's tree.

**Why this exists.** ``pacioli serve --help`` cannot see a dead door. argparse prints
help without ever reaching :func:`pacioli.server.build_server`, and ``serve()``
assembles the broker *before* it builds the server, so a credential-less run never
touches the builder at all. Measured 2026-08-16 against the 0.38.0 wheel: a venv with
no ``mcp`` installed, and a venv whose door advertised zero tools, BOTH passed every
assertion the smoke script made: version, ``serve --help``, and the offline ``TOOLS``
count. Three green checks over a door that could not serve anything.

So this drives real in-process client/server round trips, the same way
``pacioli/tests/test_server_live_sdk.py`` does against the source tree. Two legs, because
they fail independently:

1. **list** the full catalog. Catches a door that does not build, and a door that builds
   but advertises nothing (the mutation this was proven against). ``tools/list`` never
   reaches the broker, so :class:`_Refuse` raises if anything tries.
2. **call** one tool through dispatch. A list-only check never touches
   ``dispatch_tool_async``, its lazy ``anyio`` import, the dispatch lock, or ``_as_content``
   constructing real SDK ``TextContent`` objects. Those are packaging-sensitive and
   major-sensitive, which is exactly the class an artifact-level check exists for.
   :class:`_Record` stands in for the broker, so no ERPNext credential is involved and
   nothing is planned, consented or spent.

Exits 0 and prints ``<n> tools over mcp <version>, dispatch ok``; exits 1 with the reason
on stderr. The mcp major is printed rather than pinned, because the door must work on
whichever one resolved, and naming it makes the leg legible in the smoke output.
"""
import json
import sys
from importlib.metadata import version as _dist_version

import anyio
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.server import Server

from pacioli.doorway import served_tools
from pacioli.server import build_server
from pacioli.tools import TOOLS

# A draft name no bench would carry. The stub broker never looks at it; it is here so that
# anything which somehow reached a real ERPNext could not match a document.
_SMOKE_DOC = "PACIOLI-INSTALL-SMOKE-NOT-A-REAL-DOCUMENT"


class _Refuse:
    """A broker that must never be reached. Used for the list leg."""

    def dispatch(self, name, arguments):
        raise AssertionError("tools/list reached the broker; it must not")


class _Record:
    """A broker that records what dispatch was handed. Used for the call leg."""

    def __init__(self):
        self.calls = []

    def dispatch(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True, "smoke": name}


async def _round_trip(broker, served, work):
    """Run the real server and a real client over in-process streams, then ``work(session)``."""
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


def _check_list(catalog):
    """The door advertises exactly the tool catalog, BY NAME, against an INDEPENDENT truth.

    Two adversarial findings shaped this, both against earlier versions of this function:

    1. Counting is not identity. Replacing every advertised tool's name and schema with garbage,
       count preserved at 265, exited 0 -- a client would have built calls from names nothing
       answers to.
    2. Worse: the expected value used to be derived from ``served_tools({})``, the same function
       the door was built from. Breaking ``served_tools`` to return ``[]`` made both sides empty
       and everything passed. That is asserting a constant against itself.

    So the ground truth here is :data:`pacioli.tools.TOOLS`, which ``served_tools`` consumes but
    does not define, and which ``test_tool_surface.py::test_exact_name_set`` pins separately.
    Breaking either ``served_tools`` or the door's advertisement now reddens this.
    """
    async def work(session):
        return await session.list_tools()

    listed = anyio.run(_round_trip, _Refuse(), catalog, work)
    got = sorted(t.name for t in listed.tools)
    want = sorted(t["name"] for t in TOOLS)
    if got != want:
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        detail = f"{len(got)} advertised vs {len(want)} in the catalog"
        if missing:
            detail += f"; missing e.g. {missing[:3]}"
        if extra:
            detail += f"; unexpected e.g. {extra[:3]}"
        return f"the door did not advertise the catalog: {detail}"
    return None


def _check_dispatch(catalog):
    """Call one advertised tool and prove it reached the broker and rendered back.

    ``plan_submit`` is the tool used because it is the head of the governed spine and its
    schema requires only ``name``. The args must be schema-valid: mcp 1.x validates against
    the advertised schema before the handler runs, 2.x does not, and a check that only
    passed on one major would be worse than none.
    """
    broker = _Record()
    args = {"name": _SMOKE_DOC}

    async def work(session):
        return await session.call_tool("plan_submit", args)

    out = anyio.run(_round_trip, broker, catalog, work)
    if broker.calls != [("plan_submit", args)]:
        return f"dispatch did not reach the broker as sent: {broker.calls!r}"
    try:
        payload = json.loads(out.content[0].text)
    except (AttributeError, IndexError, ValueError) as exc:
        return f"the door did not render dispatch back as text content: {exc}"
    if payload != {"ok": True, "smoke": "plan_submit"}:
        return f"the rendered payload is not what the broker returned: {payload!r}"
    return None


def main():
    catalog = served_tools({})
    if not catalog:
        print("the served catalog is empty; the door would advertise nothing", file=sys.stderr)
        return 1
    for check in (_check_list, _check_dispatch):
        problem = check(catalog)
        if problem:
            print(problem, file=sys.stderr)
            return 1
    print(f"{len(TOOLS)} tools over mcp {_dist_version('mcp')}, dispatch ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
