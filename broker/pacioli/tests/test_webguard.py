# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The in-door perimeter (docs/plans/2026-07-16-webguard-parity.md) — Host allowlist,
cross-origin guard, and a receive-side body-size cap, as ONE raw-ASGI wrapper both network doors
mount outside their bearer gate.

Pure/ASGI-level: no mcp, no a2a-sdk, no socket — a hand-driven ASGI harness exercises the wrapper
directly. The door-wiring proofs live in test_server_http.py / test_a2a.py; this file pins the
perimeter mechanism itself.
"""
import asyncio
import unittest

from pacioli.webguard import (
    DEFAULT_MAX_BODY_BYTES,
    _host_ok,
    _is_cross_origin,
    default_allowed_hosts,
    guard_asgi,
)


def _headers(**kv):
    return [(k.encode().replace(b"_", b"-"), v.encode()) for k, v in kv.items()]


class _Recorder:
    """A trivial inner ASGI app: records that it was reached, replies 200."""

    def __init__(self):
        self.reached = False
        self.body_seen = b""

    async def __call__(self, scope, receive, send):
        self.reached = True
        # drain the body so a receive-side cap has something to count
        more = True
        while more:
            msg = await receive()
            self.body_seen += msg.get("body", b"")
            more = msg.get("more_body", False)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _drive(app, *, method="POST", path="/", headers=None, body=b"",
           chunks=None):
    """Drive one request through an ASGI app; return (status, body_bytes, inner_reached?)."""
    scope = {"type": "http", "method": method, "path": path,
             "headers": headers or []}
    sent = []
    # body delivered either as one chunk (body=) or a list of chunks (chunks=)
    if chunks is None:
        parts = [{"type": "http.request", "body": body, "more_body": False}]
    else:
        parts = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
                 for i, c in enumerate(chunks)]
    it = iter(parts)

    async def receive():
        try:
            return next(it)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, out


class TestHostHelpers(unittest.TestCase):
    def test_default_allowed_hosts_includes_bind_and_loopback(self):
        allowed = default_allowed_hosts("192.168.1.50")
        self.assertIn("192.168.1.50", allowed)
        self.assertIn("127.0.0.1", allowed)
        self.assertIn("localhost", allowed)
        self.assertIn("::1", allowed)

    def test_host_ok_strips_port_and_casefolds(self):
        allowed = {"127.0.0.1", "localhost"}
        self.assertTrue(_host_ok("127.0.0.1:8792", allowed))
        self.assertTrue(_host_ok("LOCALHOST", allowed))
        self.assertFalse(_host_ok("evil.example.com", allowed))
        self.assertFalse(_host_ok("", allowed))
        self.assertFalse(_host_ok(None, allowed))

    def test_star_allowlist_accepts_any(self):
        self.assertTrue(_host_ok("anything.at.all", {"*"}))

    def test_is_cross_origin(self):
        self.assertFalse(_is_cross_origin("http://127.0.0.1:8792", "127.0.0.1:8792"))
        self.assertTrue(_is_cross_origin("http://evil.example", "127.0.0.1:8792"))
        self.assertTrue(_is_cross_origin("null", "127.0.0.1:8792"))  # sandboxed → cross
        self.assertTrue(_is_cross_origin("garbage", "127.0.0.1:8792"))


class TestGuardHost(unittest.TestCase):
    def _wrap(self, inner, allowed=("127.0.0.1", "localhost", "::1")):
        return guard_asgi(inner, allowed_hosts=list(allowed), protect=lambda p: True)

    def test_good_host_passes(self):
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner), method="GET",
                           headers=_headers(host="127.0.0.1:8792"))
        self.assertEqual(status, 200)
        self.assertTrue(inner.reached)

    def test_bad_host_is_400_and_inner_never_reached(self):
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner), method="GET",
                           headers=_headers(host="evil.example.com"))
        self.assertEqual(status, 400)
        self.assertFalse(inner.reached)

    def test_host_check_applies_to_all_methods_incl_card_get(self):
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner), method="GET",
                           path="/.well-known/agent-card.json",
                           headers=_headers(host="evil.example.com"))
        self.assertEqual(status, 400)

    def test_star_disables_host_check(self):
        inner = _Recorder()
        app = guard_asgi(inner, allowed_hosts=["*"], protect=lambda p: True)
        status, _ = _drive(app, method="GET", headers=_headers(host="whatever.com"))
        self.assertEqual(status, 200)

    def test_lifespan_passes_through_untouched(self):
        seen = []

        async def inner(scope, receive, send):
            seen.append(scope["type"])
            await send({"type": "lifespan.startup.complete"})

        app = guard_asgi(inner, allowed_hosts=["127.0.0.1"], protect=lambda p: True)

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(msg):
            pass

        asyncio.run(app({"type": "lifespan"}, receive, send))
        self.assertEqual(seen, ["lifespan"])


class TestGuardCrossOrigin(unittest.TestCase):
    LOOPBACK = ("127.0.0.1", "localhost", "::1")

    def _wrap(self, inner, protect=lambda p: True):
        return guard_asgi(inner, allowed_hosts=list(self.LOOPBACK), protect=protect)

    def test_sec_fetch_site_cross_is_403(self):
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner),
                           headers=_headers(host="127.0.0.1", sec_fetch_site="cross-site",
                                            content_type="application/json"),
                           body=b'{"x":1}')
        self.assertEqual(status, 403)
        self.assertFalse(inner.reached)

    def test_same_site_is_also_403(self):
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner),
                           headers=_headers(host="127.0.0.1", sec_fetch_site="same-site",
                                            content_type="application/json"),
                           body=b'{"x":1}')
        self.assertEqual(status, 403)

    def test_same_origin_passes(self):
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner),
                           headers=_headers(host="127.0.0.1:8792", sec_fetch_site="same-origin",
                                            content_type="application/json",
                                            origin="http://127.0.0.1:8792"),
                           body=b'{"x":1}')
        self.assertEqual(status, 200)
        self.assertTrue(inner.reached)

    def test_cross_origin_header_is_403(self):
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner),
                           headers=_headers(host="127.0.0.1:8792",
                                            origin="http://evil.example",
                                            content_type="application/json"),
                           body=b'{"x":1}')
        self.assertEqual(status, 403)

    def test_non_json_body_is_415(self):
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner),
                           headers=_headers(host="127.0.0.1", content_type="text/plain",
                                            content_length="7"),
                           body=b'x=1&y=2')
        self.assertEqual(status, 415)

    def test_non_browser_client_passes(self):
        # curl / the SDKs: no sec-fetch-site, no origin, application/json — clean.
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner),
                           headers=_headers(host="127.0.0.1", content_type="application/json"),
                           body=b'{"jsonrpc":"2.0"}')
        self.assertEqual(status, 200)
        self.assertTrue(inner.reached)

    def test_unprotected_path_skips_cross_origin(self):
        # a path outside protect() is never cross-origin-checked (e.g. the A2A card route)
        inner = _Recorder()
        app = self._wrap(inner, protect=lambda p: p == "/rpc")
        status, _ = _drive(app, method="POST", path="/other",
                           headers=_headers(host="127.0.0.1", sec_fetch_site="cross-site"))
        self.assertEqual(status, 200)

    def test_get_is_never_cross_origin_guarded(self):
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner), method="GET",
                           headers=_headers(host="127.0.0.1", sec_fetch_site="cross-site"))
        self.assertEqual(status, 200)  # a GET can't mutate; only POST is guarded


class TestGuardBodyCap(unittest.TestCase):
    LOOPBACK = ("127.0.0.1", "localhost", "::1")

    def _wrap(self, inner, cap=DEFAULT_MAX_BODY_BYTES):
        return guard_asgi(inner, allowed_hosts=list(self.LOOPBACK),
                          protect=lambda p: True, max_body_bytes=cap)

    def test_content_length_over_cap_is_413_immediately(self):
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner, cap=100),
                           headers=_headers(host="127.0.0.1", content_type="application/json",
                                            content_length="101"),
                           body=b"x" * 101)
        self.assertEqual(status, 413)
        self.assertFalse(inner.reached)

    def test_small_body_passes(self):
        inner = _Recorder()
        status, _ = _drive(self._wrap(inner, cap=1000),
                           headers=_headers(host="127.0.0.1", content_type="application/json",
                                            content_length="10"),
                           body=b'{"x":1234}')
        self.assertEqual(status, 200)
        self.assertTrue(inner.reached)

    def test_chunked_no_content_length_over_cap_is_capped(self):
        # THE stronger-than-Proximo case: no Content-Length, body streamed in chunks that
        # cumulatively exceed the cap. A CL-only cap would MISS this; the receive-side counter
        # aborts the body read mid-stream, so the RESPONSE is the guard's 413 (the app is
        # entered to pull the body but never sends its own 200 — that's how a streaming cap
        # must work).
        inner = _Recorder()
        chunks = [b"a" * 60, b"b" * 60]  # 120 total, cap 100, no content-length header
        status, out = _drive(self._wrap(inner, cap=100),
                             headers=_headers(host="127.0.0.1", content_type="application/json"),
                             chunks=chunks)
        self.assertEqual(status, 413)
        self.assertNotIn(b"ok", out)  # the app's own 200 body never became the response

    def test_chunked_under_cap_passes(self):
        inner = _Recorder()
        chunks = [b"a" * 30, b"b" * 30]  # 60 total, cap 100
        status, _ = _drive(self._wrap(inner, cap=100),
                           headers=_headers(host="127.0.0.1", content_type="application/json"),
                           chunks=chunks)
        self.assertEqual(status, 200)
        self.assertTrue(inner.reached)
        self.assertEqual(inner.body_seen, b"a" * 30 + b"b" * 30)


class TestGuardBodyReadTimeout(unittest.TestCase):
    """The slow-body / slowloris defense (security redteam 2026-07-16, Major): the body read
    is bounded by TIME as well as bytes, and it happens OUTSIDE the bearer gate, so an
    unauthenticated stalled connection cannot pin a task indefinitely. A read that outlives the
    deadline is refused 408 — the app is never reached."""

    LOOPBACK = ("127.0.0.1", "localhost", "::1")

    def _drive_slow(self, delay, timeout):
        from pacioli.webguard import guard_asgi
        inner = _Recorder()
        app = guard_asgi(inner, allowed_hosts=list(self.LOOPBACK), protect=lambda p: True,
                         body_read_timeout=timeout)
        scope = {"type": "http", "method": "POST", "path": "/",
                 "headers": _headers(host="127.0.0.1", content_type="application/json",
                                     content_length="1000")}
        sent = []
        delivered = {"n": 0}

        async def receive():
            # first call stalls past the deadline, simulating a client that stops sending
            delivered["n"] += 1
            if delivered["n"] == 1:
                await asyncio.sleep(delay)
            return {"type": "http.request", "body": b"x", "more_body": False}

        async def send(msg):
            sent.append(msg)

        asyncio.run(app(scope, receive, send))
        status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
        return status, inner.reached

    def test_stalled_body_is_408_not_a_hang(self):
        # the read stalls 5s but the deadline is 0.1s → 408, app never reached, returns promptly.
        status, reached = self._drive_slow(delay=5.0, timeout=0.1)
        self.assertEqual(status, 408)
        self.assertFalse(reached)

    def test_prompt_body_under_the_deadline_passes(self):
        # a body that arrives well within the deadline is unaffected.
        status, reached = self._drive_slow(delay=0.0, timeout=5.0)
        self.assertEqual(status, 200)
        self.assertTrue(reached)


class TestGuardReplayAfterBody(unittest.TestCase):
    """The guard buffers a protected POST body and replays it to the app — but an app may keep
    polling ``receive()`` DURING its response to watch for a client disconnect (the MCP
    streamable-HTTP session manager does exactly this while it streams SSE). The replay must
    never synthesize ``http.disconnect`` for a client that is still connected — doing so
    aborted every HTTP-door SSE response before the result was sent (found live, the doors
    lab-pin window 2026-07-17). After the buffered body, polls delegate to the REAL receive:
    a live client blocks, a real disconnect surfaces."""

    def _scope(self):
        return {"type": "http", "method": "POST", "path": "/mcp",
                "headers": _headers(host="127.0.0.1", content_type="application/json",
                                    content_length="7")}

    @staticmethod
    def _sse_style_inner(seen, poll_timeout):
        """An inner app shaped like the session manager: drain the body, then poll receive()
        mid-response for a client disconnect."""
        async def inner(scope, receive, send):
            more = True
            while more:
                m = await receive()
                more = m.get("more_body", False)
            try:
                m = await asyncio.wait_for(receive(), timeout=poll_timeout)
                seen["after_body"] = m["type"]
            except asyncio.TimeoutError:
                seen["after_body"] = "receive-blocked (client still connected)"
        return inner

    def test_connected_client_is_never_reported_disconnected(self):
        seen = {}
        delivered = {"done": False}

        async def receive():
            if not delivered["done"]:
                delivered["done"] = True
                return {"type": "http.request", "body": b'{"x":1}', "more_body": False}
            await asyncio.Event().wait()  # a live client: nothing more to send, NO disconnect

        app = guard_asgi(self._sse_style_inner(seen, poll_timeout=0.2),
                         allowed_hosts=["127.0.0.1"], protect=lambda p: True)

        async def send(msg):
            pass

        asyncio.run(app(self._scope(), receive, send))
        self.assertEqual(seen["after_body"], "receive-blocked (client still connected)")

    def test_real_disconnect_still_reaches_the_app(self):
        seen = {}
        delivered = {"done": False}

        async def receive():
            if not delivered["done"]:
                delivered["done"] = True
                return {"type": "http.request", "body": b'{"x":1}', "more_body": False}
            return {"type": "http.disconnect"}  # the client actually went away

        app = guard_asgi(self._sse_style_inner(seen, poll_timeout=2.0),
                         allowed_hosts=["127.0.0.1"], protect=lambda p: True)

        async def send(msg):
            pass

        asyncio.run(app(self._scope(), receive, send))
        self.assertEqual(seen["after_body"], "http.disconnect")

    def test_disconnect_during_body_read_is_reported_not_swallowed(self):
        # the client vanished mid-body: the guard saw http.disconnect while buffering, so a
        # later poll must report it immediately (never re-poll a dead channel, never block).
        seen = {}
        parts = iter([
            {"type": "http.request", "body": b'{"x"', "more_body": True},
            {"type": "http.disconnect"},
        ])

        async def receive():
            return next(parts)  # StopIteration past here = the guard re-polled a dead channel

        app = guard_asgi(self._sse_style_inner(seen, poll_timeout=2.0),
                         allowed_hosts=["127.0.0.1"], protect=lambda p: True)

        async def send(msg):
            pass

        asyncio.run(app(self._scope(), receive, send))
        self.assertEqual(seen["after_body"], "http.disconnect")


if __name__ == "__main__":
    unittest.main()
