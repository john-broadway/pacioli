# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The HTTP door, driven through the REAL SDK stack on whichever mcp major is installed.

Every other HTTP test in this suite runs against a ``FakeManager``. That made the perimeter and
the bearer gate testable without a socket, which was worth having, but it left the real
``StreamableHTTPSessionManager`` never once constructed by a test on either major: the door's own
board recorded it as "verified by signature only on 2.x, never executed". This file executes it.
Same reasoning as ``test_server_live_sdk.py`` for the stdio door, and the same history behind it:
mcp 2.0.0 changed the SDK under us and fakes could not have noticed.

It drives :func:`pacioli.server.build_http_asgi` -- the exact stack ``serve_http`` serves, not a
reassembly of it -- over in-process ASGI, with the SDK's own streamable-HTTP client.

**Probed facts, measured 2026-08-17, and one of them corrected a premise this file was first
written on.** The obvious reading is that the client entry point was renamed across the major
(``streamablehttp_client`` to ``streamable_http_client``) and that the name is what to branch on.
It is not:

* **mcp 1.29.0 carries BOTH names**, the old one deprecated in favour of the new. 2.0.0 carries
  only the new one. So the rename is not a major boundary and branching on it only earns a
  DeprecationWarning on 1.x, which is how the mistake surfaced.
* ``streamable_http_client`` has the same shape on both majors (``url``, ``http_client``), so the
  two legs share one code path here, which is the door's own thesis about transports.
* What genuinely differs is the HTTP package the SDK is typed to: **httpx on 1.x, httpx2 on 2.x**,
  different distributions.
* **And they are NOT mutually exclusive**, which the first version of this file asserted and two
  independent adversarial passes disproved. The ``[a2a]`` extra pulls ``a2a-sdk``, which requires
  ``httpx``, so CI's ``.[server,a2a]`` install carries httpx on BOTH majors. Selecting by "which
  one imports first" picked httpx every time and never exercised httpx2 at all, including on the
  2.x leg that exists to exercise it. httpx2 arrives only as an mcp 2.x dependency, so its
  presence is the reliable signal and httpx's is not. Hence the import order below.
* The old name is kept as a fallback, not for 2.x but for the low end of the declared floor
  (``mcp>=1.10``), which predates the new spelling. Confirmed by an adversarial pass to be a
  live path: at ``mcp==1.10.0`` all six tests pass through it rather than skipping.

**Residual, so no one reads more into this than it proves:** in-process ASGI is not a socket.
``uvicorn.run`` and real TCP are not exercised here; what is exercised is every layer
``serve_http`` builds above them.
"""
import json
import unittest
from contextlib import asynccontextmanager

try:
    import anyio
    import mcp.types as types
    from mcp.client.session import ClientSession
    from mcp.server import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    # httpx2 FIRST, and the order is the whole point. Both packages are routinely installed at
    # once: the [a2a] extra pulls a2a-sdk, which requires httpx, so CI's `.[server,a2a]` install
    # has httpx present on BOTH mcp majors. Selecting by "which one imports" therefore always
    # picked httpx and never once exercised httpx2, including on the 2.x leg. httpx2 is only ever
    # present as an mcp 2.x dependency, so ITS presence is the reliable signal; httpx's is not.
    try:
        import httpx2 as _http
    except ImportError:
        import httpx as _http
    try:
        from mcp.client.streamable_http import streamable_http_client as _CONNECT
        _TAKES_CLIENT = True
    except ImportError:                                # below the new spelling, inside the floor
        from mcp.client.streamable_http import streamablehttp_client as _CONNECT
        _TAKES_CLIENT = False
    HAVE_SDK = True
except ImportError:                                    # pragma: no cover - env without [server]
    HAVE_SDK = False

from pacioli.doorway import served_tools
from pacioli.server import build_http_asgi
from pacioli.tools import TOOLS

# Loopback, because the door's default Host allowlist is the bind host plus loopback forms and
# httpx's conventional "testserver" base would be refused 400 by the perimeter before anything
# else ran. Using the real allowlisted host means the bad-Host leg below tests the guard rather
# than an artefact of the harness.
_BASE = "http://127.0.0.1"
_URL = _BASE + "/mcp"
_TOKEN = "smoke-token-not-a-real-secret"


class _Record:
    """Stands in for the broker. No ERPNext credential, nothing planned, consented or spent."""

    def __init__(self):
        self.calls = []

    def dispatch(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True, "smoke": name}


def _door(broker, *, token=None, allowed_hosts=None):
    return build_http_asgi(Server, types, StreamableHTTPSessionManager, broker,
                           served_tools({}), token=token, bind="127.0.0.1",
                           allowed_hosts=allowed_hosts)


# Every wait here is bounded. Found by an adversarial pass: production `_asgi_app` catches
# Exception around its whole lifespan block and returns QUIETLY, which is a deliberate prior
# decision (a channel dying mid-protocol must not strand uvicorn's shutdown). The consequence for
# a hand-rolled harness is that a session manager which raises on startup produces no ack, no
# exception, and no failure -- the handshake below simply blocks forever. Confirmed by forcing
# that raise: pytest had to be SIGTERMed. There is no pytest-timeout in this repo and no
# `timeout-minutes` on any CI job, so an unbounded wait here is a suite that hangs instead of a
# test that reddens. The fix belongs in the harness, not in `_asgi_app`.
_LIFESPAN_TIMEOUT = 20.0
_WORK_TIMEOUT = 120.0


async def _with_lifespan(asgi, work):
    """Run ``work()`` with the app's lifespan started.

    ASGI test transports do not run the lifespan protocol, and this door starts
    ``manager.run()`` inside it. Without this the first request fails inside the session
    manager, which reads as an SDK fault and is not one.
    """
    to_send, to_recv = anyio.create_memory_object_stream(8)
    from_send, from_recv = anyio.create_memory_object_stream(8)
    result = {}

    async def receive():
        return await to_recv.receive()

    async def send(message):
        await from_send.send(message)

    async with anyio.create_task_group() as tg:
        tg.start_soon(asgi, {"type": "lifespan"}, receive, send)
        await to_send.send({"type": "lifespan.startup"})
        with anyio.fail_after(_LIFESPAN_TIMEOUT):
            started = await from_recv.receive()
        if started["type"] != "lifespan.startup.complete":
            raise AssertionError(f"the door did not start its lifespan: {started!r}")
        try:
            with anyio.fail_after(_WORK_TIMEOUT):
                result["value"] = await work()
        finally:
            await to_send.send({"type": "lifespan.shutdown"})
            with anyio.move_on_after(_LIFESPAN_TIMEOUT):
                await from_recv.receive()
            # Bounding the ack is not enough on its own: the task group only exits once the app
            # task finishes, so an app wedged past shutdown would hang here instead. Cancel it,
            # the same way door_check.py's _round_trip cancels its serve scope. An exception
            # already in flight tears the group down by itself; this covers the quiet path.
            tg.cancel_scope.cancel()
    return result["value"]


def _http_client(asgi, headers=None):
    return _http.AsyncClient(transport=_http.ASGITransport(app=asgi), base_url=_BASE,
                             headers=headers or {})


@asynccontextmanager
async def _streams(asgi, headers=None):
    """The SDK's streamable-HTTP client, pointed at the door in-process, per major."""
    if _TAKES_CLIENT:
        client = _http_client(asgi, headers)
        async with client, _CONNECT(_URL, http_client=client) as opened:
            yield opened[0], opened[1]
    else:                                              # pragma: no cover - old floor only
        def factory(headers=None, timeout=None, auth=None):
            return _http.AsyncClient(transport=_http.ASGITransport(app=asgi), base_url=_BASE,
                                     headers=headers, timeout=timeout, auth=auth)

        async with _CONNECT(_URL, headers=headers, httpx_client_factory=factory) as opened:
            yield opened[0], opened[1]


def _session(asgi, work, headers=None):
    """Open a real MCP session over the real HTTP door and run ``work(session)``."""
    async def run():
        async def inner():
            async with _streams(asgi, headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await work(session)
        return await _with_lifespan(asgi, inner)
    return anyio.run(run)


def _post(asgi, headers, host=None):
    """One raw POST at the door. Returns the status code, which is the whole assertion:
    a refusal here happens before the MCP layer and has no MCP shape to read."""
    async def run():
        async def inner():
            base = f"http://{host}" if host else _BASE
            client = _http.AsyncClient(transport=_http.ASGITransport(app=asgi), base_url=base,
                                       headers=headers)
            async with client:
                sent = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                                       "method": "tools/list"})
                return sent.status_code
        return await _with_lifespan(asgi, inner)
    return anyio.run(run)


@unittest.skipUnless(HAVE_SDK, "the mcp SDK is not installed (pacioli[server])")
class TestTheHttpDoorAgainstTheRealStack(unittest.TestCase):
    def test_the_door_lists_the_full_catalog_over_real_http(self):
        """BY NAME, against TOOLS, and both halves of that were adversarial findings.

        It asserted only the LENGTH until a pass replaced every advertised tool's name and
        schema with garbage, kept the count at 265, and watched all six tests stay green.
        Cardinality is not identity.

        And it compared against `served_tools({})`, the very function the door is built from, so
        breaking `served_tools` to return [] emptied BOTH sides and everything still passed. The
        ground truth is now `pacioli.tools.TOOLS`, which served_tools consumes but does not
        define, and which test_tool_surface.py pins on its own."""
        asgi = _door(_Record())
        listed = _session(asgi, lambda s: s.list_tools())
        self.assertEqual(sorted(t.name for t in listed.tools),
                         sorted(t["name"] for t in TOOLS))

    def test_a_call_reaches_the_broker_and_renders_back_over_real_http(self):
        broker = _Record()
        asgi = _door(broker)
        args = {"name": "PACIOLI-HTTP-LIVE-NOT-A-REAL-DOCUMENT"}

        out = _session(asgi, lambda s: s.call_tool("plan_submit", args))
        self.assertEqual(broker.calls, [("plan_submit", args)])
        self.assertEqual(json.loads(out.content[0].text),
                         {"ok": True, "smoke": "plan_submit"})

    def test_a_configured_token_still_admits_a_correct_bearer(self):
        """The negative legs below prove the gate refuses. This proves it is not refusing
        everything, which a gate that always said no would also do."""
        asgi = _door(_Record(), token=_TOKEN)
        listed = _session(asgi, lambda s: s.list_tools(),
                          headers={"Authorization": f"Bearer {_TOKEN}"})
        self.assertEqual(sorted(t.name for t in listed.tools),
                         sorted(t["name"] for t in TOOLS))


@unittest.skipUnless(HAVE_SDK, "the mcp SDK is not installed (pacioli[server])")
class TestTheHttpDoorRefusesAgainstTheRealStack(unittest.TestCase):
    """The refusals, against the real session manager rather than a FakeManager. Each asserts a
    status code, never a message: the outcome is the contract."""

    def test_no_bearer_is_401_when_a_token_is_configured(self):
        self.assertEqual(_post(_door(_Record(), token=_TOKEN), headers={}), 401)

    def test_a_wrong_bearer_is_401(self):
        asgi = _door(_Record(), token=_TOKEN)
        self.assertEqual(_post(asgi, headers={"Authorization": "Bearer not-the-token"}), 401)

    def test_a_host_outside_the_allowlist_is_400_before_anything_else(self):
        asgi = _door(_Record(), token=_TOKEN, allowed_hosts=["127.0.0.1"])
        # Bad Host AND no bearer: 400 proves the Host guard runs first, since the bearer gate
        # would have answered 401 for this same request.
        self.assertEqual(_post(asgi, headers={}, host="evil.example"), 400)


class TestTheHttpDoorRefusesAnExposedSocketAgainstTheRealStack(unittest.TestCase):
    """The HTTP door had a BUILD-time non-loopback refusal (2026-08-17) and no request-time one.

    Found 2026-08-24. The vault recorded this gap as the A2A door's alone; it was the HTTP door's
    too. `build_http_asgi` refuses to CONSTRUCT on a non-loopback `bind`, but `bind` is a claim an
    operator typed, and the REST door already learned in the most direct way possible that a claim
    is not a fact: `build_rest_app(bind="127.0.0.1")` served by `uvicorn.run(host="0.0.0.0")` was
    demonstrated answering a forged `Host: 127.0.0.1` from off-box, unauthenticated. Host
    allowlisting only ever defeated BROWSER rebinding; a direct client sets Host freely.

    Same shape here, reachable the same way: a direct embedder or a `uvicorn --factory` path can
    build loopback-declared and serve anywhere. The classifier is the shared
    `pacioli.webguard.socket_is_local`, deciding on `scope["server"]` -- the address the
    connection actually landed on, server-reported and unforgeable.

    Driven through `_door()`, the real built stack, deliberately WITHOUT the lifespan dance the
    other tests here need: the refusal is wrapped OUTSIDE the perimeter and outside the session
    manager, so it must answer without any of that running. If these ever start needing a
    lifespan, the wrapping order has regressed and that is itself the finding.
    """

    def _answer(self, token, server):
        """Returns (status_or_None, broker, body_was_read).

        `None` means the request got PAST the socket guard: past the guard is the session manager,
        which needs the lifespan this harness deliberately does not run, so it raises. That raise
        is swallowed here and CANNOT manufacture a pass, because every refusal assertion below is
        a positive one on what was actually SENT: a guard that wrongly refused would put a 403 in
        `sent` and red the test regardless.

        `body_was_read` is the ordering property. The socket refusal must answer BEFORE the
        perimeter, and `guard_asgi` drains a POST body in full before calling the app it wraps. A
        refusal that costs the perimeter's whole 30s read deadline is not a cheap refusal, which
        is the fix-review finding that moved the REST door's guard outside the perimeter.
        """
        broker = _Record()
        asgi = _door(broker, token=token)
        sent = []
        read = []

        async def send(message):
            sent.append(message)

        async def receive():
            read.append(1)
            return {"type": "http.request", "body": b"{}", "more_body": False}

        scope = {"type": "http", "method": "POST", "path": "/mcp",
                 "headers": [(b"host", b"127.0.0.1"), (b"content-type", b"application/json"),
                             (b"content-length", b"2")],
                 "query_string": b"", "server": server}
        try:
            anyio.run(asgi, scope, receive, send)
        except Exception:
            pass
        starts = [m["status"] for m in sent if m["type"] == "http.response.start"]
        return (starts[0] if starts else None), broker, bool(read)

    def test_no_token_on_a_non_local_socket_is_refused(self):
        status, broker, body_read = self._answer(None, ("192.0.2.1", 8791))
        self.assertEqual(status, 403)
        self.assertEqual(broker.calls, [])
        self.assertFalse(body_read, "refused only after the perimeter drained the body")

    def test_an_empty_token_on_a_non_local_socket_is_refused_identically(self):
        """An empty token is no token, so it must land on the SAME refusal as None rather than on
        the bearer gate's 401. Before 2026-08-24 this answered 401: `_asgi_app` installed a gate on
        `token is not None`, and `_bearer_ok` fails closed on an empty configured token, so the
        door refused everyone. Fail-closed, but by accident, and widening that gate alone would
        have turned the accident into a genuinely open door here."""
        status, broker, body_read = self._answer("", ("192.0.2.1", 8791))
        self.assertEqual(status, 403)
        self.assertEqual(broker.calls, [])
        self.assertFalse(body_read, "refused only after the perimeter drained the body")

    def test_a_tokened_door_on_a_non_local_socket_is_not_refused_by_this_check(self):
        """The control that stops the check from being 'refuse every non-local socket'. A door WITH
        a bearer token is a legitimate non-local deployment; this guard exists only for the
        untokened one. 401 here (the bearer gate answering) rather than 403 proves the socket check
        stood aside and let the real gate decide."""
        status, _, _ = self._answer(_TOKEN, ("192.0.2.1", 8791))
        self.assertEqual(status, 401)

    def test_a_unix_socket_is_not_refused(self):
        """A UDS reports `(path, None)` and is filesystem-gated, arguably a STRICTER boundary than
        TCP loopback: reachable only through a path with permissions, not by any local UID. The
        first version of the REST classifier refused it, and this pins that the door which
        inherited that classifier last does not re-acquire the bug.

        `None` is the pass here, meaning the guard stood aside and the request went on to the
        session manager. There is nothing between the guard and the manager to answer for an
        UNTOKENED door, and the socket check only ever fires for an untokened one, so "not
        refused" is the only observable this level can offer. The classifier's own shape-by-shape
        table lives in `test_rest.py` against `pacioli.webguard.socket_is_local`, shared by all
        three doors."""
        status, _, _ = self._answer(None, ("/run/pacioli.sock", None))
        self.assertIsNone(status)
