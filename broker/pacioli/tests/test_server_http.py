# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The HTTP transport (``serve --http``) — pure config/auth seams, SDK-free.

The transports ruling (docs/plans/2026-07-16-any-transport-one-spine.md, John: "cover it all
go"): a transport carries, it never decides. These tests pin the deny-biased posture — default
loopback bind; a non-loopback bind REFUSES TO START without a token; the token is held by
reference only (the registry's own law); bearer checks are strict and constant-time. The
live wire itself is driven end-user style (curl against a running server), not unit-mocked."""
import unittest

from pacioli.server import (
    TransportConfigError,
    _bearer_ok,
    _bind_requires_auth,
    _resolve_transport_token,
)


class TestBindRequiresAuth(unittest.TestCase):
    def test_loopback_forms_do_not_require_auth(self):
        for bind in ("127.0.0.1", "::1", "localhost"):
            self.assertFalse(_bind_requires_auth(bind), bind)

    def test_non_loopback_requires_auth(self):
        for bind in ("0.0.0.0", "::", "192.168.1.5", "10.0.0.40", "erp.example.com"):
            self.assertTrue(_bind_requires_auth(bind), bind)

    def test_unparseable_bind_is_deny_biased(self):
        # A bind we cannot classify is treated as exposed — require the token.
        for bind in ("", None, "   ", "127.0.0.1; rm -rf /"):
            self.assertTrue(_bind_requires_auth(bind), repr(bind))


class TestResolveTransportToken(unittest.TestCase):
    def test_env_reference_resolves(self):
        self.assertEqual(_resolve_transport_token("env:SERVE_T", {"SERVE_T": "tok123"}), "tok123")

    def test_file_reference_resolves(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".tok", delete=False) as f:
            f.write("tok456\n")
        self.assertEqual(_resolve_transport_token(f"file:{f.name}", {}), "tok456")

    def test_inline_token_refused_never_echoed(self):
        # The registry's own law: secrets by reference, never inline on a command line.
        with self.assertRaises(TransportConfigError) as ctx:
            _resolve_transport_token("tok-literal-s3cret", {})
        self.assertNotIn("s3cret", str(ctx.exception))

    def test_missing_env_var_refuses(self):
        with self.assertRaises(TransportConfigError):
            _resolve_transport_token("env:NOPE", {})

    def test_empty_resolved_token_refuses(self):
        # An empty token would turn the bearer check into a doorstop that admits everyone.
        with self.assertRaises(TransportConfigError):
            _resolve_transport_token("env:SERVE_T", {"SERVE_T": "   "})


class TestBearerOk(unittest.TestCase):
    def test_exact_bearer_matches(self):
        self.assertTrue(_bearer_ok("Bearer tok123", "tok123"))

    def test_wrong_token_refused(self):
        self.assertFalse(_bearer_ok("Bearer wrong", "tok123"))

    def test_missing_or_malformed_header_refused(self):
        for h in (None, "", "tok123", "bearer tok123", "Basic tok123", "Bearer "):
            self.assertFalse(_bearer_ok(h, "tok123"), repr(h))

    def test_empty_configured_token_never_admits(self):
        self.assertFalse(_bearer_ok("Bearer ", ""))
        self.assertFalse(_bearer_ok("Bearer x", ""))


class TestServeHttpRefusals(unittest.TestCase):
    def test_non_loopback_bind_without_auth_refuses_to_start(self):
        from pacioli.server import serve_http
        import io
        from contextlib import redirect_stderr
        e = io.StringIO()
        with redirect_stderr(e):
            rc = serve_http({}, bind="0.0.0.0", port=8791, auth=None)
        self.assertEqual(rc, 2)
        self.assertIn("token", e.getvalue().lower())

    def test_inline_auth_refuses_to_start(self):
        from pacioli.server import serve_http
        import io
        from contextlib import redirect_stderr
        e = io.StringIO()
        with redirect_stderr(e):
            rc = serve_http({}, bind="127.0.0.1", port=8791, auth="inline-literal")
        self.assertEqual(rc, 2)


class TestDispatchOffload(unittest.TestCase):
    """Review finding 1 (CONFIRMED): dispatch under a threading.Lock ran ON the event loop
    inside the async handlers — accidental serialization, deliberate loop-blocking. Dispatch
    must run in a worker thread: the loop stays live while the lock serializes governed acts."""

    def test_concurrent_dispatches_complete_serialized_without_blocking_the_loop(self):
        import asyncio
        import time
        from pacioli.server import dispatch_tool_async

        inside, max_inside = [0], [0]

        class SlowBroker:
            def dispatch(self, name, arguments):
                inside[0] += 1
                max_inside[0] = max(max_inside[0], inside[0])
                time.sleep(0.15)
                inside[0] -= 1
                return {"ok": False, "reason": name, "stage": "request"}

        class T:  # minimal stand-in for mcp.types
            class TextContent:
                def __init__(self, type, text):
                    self.type, self.text = type, text

        async def main():
            broker = SlowBroker()
            loop_alive = []

            async def heartbeat():
                # If dispatch blocks the loop, this cannot tick while both calls run.
                await asyncio.sleep(0.05)
                loop_alive.append(True)

            results = await asyncio.gather(
                dispatch_tool_async(broker, T, "a", {}),
                dispatch_tool_async(broker, T, "b", {}),
                heartbeat(),
            )
            return results, loop_alive

        (r1, r2, _), alive = asyncio.run(main())
        self.assertTrue(alive, "the event loop was blocked during dispatch")
        self.assertEqual(max_inside[0], 1, "dispatch was NOT serialized across calls")
        self.assertTrue(r1 and r2)


class TestAsgiWrapper(unittest.TestCase):
    """The extracted ASGI app (review finding 4's reuse extraction makes it testable):
    401 before the MCP layer, and a dying lifespan channel exits quietly (finding 2)."""

    def _app(self, token):
        from pacioli.server import _asgi_app

        class FakeManager:
            def run(self):
                import contextlib

                @contextlib.asynccontextmanager
                async def cm():
                    yield
                return cm()

            async def handle_request(self, scope, receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": []})
        return _asgi_app(FakeManager(), token)

    def test_http_without_bearer_is_401_before_the_mcp_layer(self):
        import asyncio
        sent = []

        async def send(m):
            sent.append(m)

        async def receive():
            return {"type": "http.request"}

        asyncio.run(self._app("tok")({"type": "http", "headers": []}, receive, send))
        self.assertEqual(sent[0]["status"], 401)

    def test_lifespan_receive_exception_exits_quietly(self):
        import asyncio
        sent = []
        calls = [0]

        async def send(m):
            sent.append(m)

        async def receive():
            calls[0] += 1
            if calls[0] == 1:
                return {"type": "lifespan.startup"}
            raise RuntimeError("channel died")

        # Must not raise — a dead lifespan channel is uvicorn's problem, not a crash of ours.
        asyncio.run(self._app(None)({"type": "lifespan"}, receive, send))
        self.assertEqual(sent[0]["type"], "lifespan.startup.complete")


class TestHttpDoorPerimeter(unittest.TestCase):
    """The shared in-door perimeter (webguard) wraps the HTTP door too. Drives the composed
    guard_asgi(_asgi_app(...)) exactly as serve_http builds it — a bad Host is refused 400
    before the MCP layer, cross-origin 403, every POST protected (no discovery route)."""

    def _guarded(self, token=None, allowed=("127.0.0.1", "localhost", "::1")):
        import contextlib

        from pacioli.server import _asgi_app
        from pacioli.webguard import guard_asgi

        class FakeManager:
            def run(self):
                @contextlib.asynccontextmanager
                async def cm():
                    yield
                return cm()

            async def handle_request(self, scope, receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"mcp"})

        return guard_asgi(_asgi_app(FakeManager(), token),
                          allowed_hosts=list(allowed), protect=lambda p: True)

    def _post(self, app, headers):
        import asyncio
        sent = []

        async def send(m):
            sent.append(m)

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        scope = {"type": "http", "method": "POST", "path": "/",
                 "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
        asyncio.run(app(scope, receive, send))
        return next((m["status"] for m in sent if m["type"] == "http.response.start"), None)

    def test_bad_host_is_400_before_the_mcp_layer(self):
        status = self._post(self._guarded(),
                            {"host": "evil.example.com", "content-type": "application/json"})
        self.assertEqual(status, 400)

    def test_good_host_passes_to_the_mcp_layer(self):
        status = self._post(self._guarded(),
                            {"host": "127.0.0.1:8791", "content-type": "application/json"})
        self.assertEqual(status, 200)

    def test_cross_origin_post_is_403(self):
        status = self._post(self._guarded(),
                            {"host": "127.0.0.1", "sec-fetch-site": "cross-site",
                             "content-type": "application/json"})
        self.assertEqual(status, 403)

    def test_non_json_body_is_415(self):
        status = self._post(self._guarded(),
                            {"host": "127.0.0.1", "content-type": "text/plain",
                             "content-length": "2"})
        self.assertEqual(status, 415)


class TestTokenFileEncoding(unittest.TestCase):
    def test_token_file_reads_utf8_regardless_of_locale(self):
        # Review finding 3: the default reader must pin utf-8, not trust the locale.
        import tempfile
        from pacioli.server import _resolve_transport_token
        with tempfile.NamedTemporaryFile("w", suffix=".tok", delete=False,
                                         encoding="utf-8") as f:
            f.write("tok-é-9\n")
        self.assertEqual(_resolve_transport_token(f"file:{f.name}", {}), "tok-é-9")


class TestCliSurface(unittest.TestCase):
    def test_serve_parses_http_flags_and_default_is_stdio(self):
        from pacioli.cli import build_parser
        p = build_parser()
        a = p.parse_args(["serve"])
        self.assertFalse(getattr(a, "http", False))
        a = p.parse_args(["serve", "--http", "--bind", "127.0.0.1", "--port", "8791",
                          "--auth", "env:SERVE_T"])
        self.assertTrue(a.http)
        self.assertEqual((a.bind, a.port, a.auth), ("127.0.0.1", 8791, "env:SERVE_T"))


if __name__ == "__main__":
    unittest.main()
