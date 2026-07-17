# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — SERVER: the doors (glue).

Deliberately thin. All tool logic, schemas, and the governed flow live in :mod:`pacioli.tools`
(SDK-free, fully unit-tested); this module only bridges MCP transports to
:meth:`pacioli.tools.PacioliBroker.dispatch`. The ``mcp`` package is an optional dependency
(``pacioli[server]``) and is imported lazily so the pure/CLI paths never require it.

**A DOOR ADMITS; IT NEVER DECIDES** (the doors ruling,
``docs/plans/2026-07-16-any-transport-one-spine.md`` — John, 2026-07-16: the spine's trust
properties are transport-independent by construction; the guard binds the credential, consent
is minted out-of-band by the human's hand, and PLAN→CONSENT→PROVE never read the wire). The MCP
doors into one spine (the third door — A2A, the first non-MCP protocol — lives in
:mod:`pacioli.a2a` and wraps this module's :func:`dispatch_raw` + perimeter primitives):

* :func:`serve` — the **stdio door** (the original, byte-identical).
* :func:`serve_http` — the **HTTP door** (MCP streamable HTTP), OFF by default and opt-in via
  ``pacioli serve --http``. Deny-biased posture: binds loopback by default; a non-loopback
  bind REFUSES TO START without a bearer token; the token is held **by reference only**
  (``env:VAR`` / ``file:/path`` — the registry's own secrets law, never inline on a command
  line); an unclassifiable bind is treated as exposed. TLS is the perimeter's job (reverse
  proxy) and is said so honestly rather than half-built here.

Both doors dispatch through ONE process-wide lock (:data:`_DISPATCH_LOCK`) — the store's
single-writer discipline is preserved by serializing dispatch, not by trusting the wire
(concurrent dispatch is its own future slice with its own redteam).
"""
from __future__ import annotations

import hmac
import sys
import threading

from pacioli.runtime import RuntimeError_, assemble
from pacioli.tools import TOOLS

_DISPATCH_LOCK = threading.Lock()

_LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost"})

# The stdio door's ledger stamp (F3): the principal is the process spawn itself — whoever
# could exec this binary. A label, never secret material.
STDIO_VIA = {"transport": "stdio", "principal": "local-spawn"}


def _http_via(auth_ref):
    """The HTTP door's ledger stamp: the principal is the token's REFERENCE label (e.g.
    ``env:SERVE_T``) — never the token itself — or ``loopback`` when no token is configured
    (a loopback-only door's principal is whoever shares the host's loopback)."""
    return {"transport": "http",
            "principal": auth_ref if auth_ref is not None else "loopback"}


class TransportConfigError(ValueError):
    """A door configuration that must refuse to start — non-loopback without a token,
    an inline token where a reference belongs, an unresolvable reference. Never carries
    the offending secret material in its message."""


def _bind_requires_auth(bind):
    """True unless ``bind`` is a recognized loopback form. Deny-biased: an empty, non-string,
    or otherwise unclassifiable bind reads as EXPOSED (token required) — never as safe."""
    if not isinstance(bind, str):
        return True
    return bind.strip() not in _LOOPBACK_BINDS


def _resolve_transport_token(ref, env, *, read_file=None):
    """Resolve the door's bearer token from a reference — ``env:VAR`` or ``file:/path``
    ONLY (the registry's secrets law). An inline literal is refused WITHOUT echoing it; a
    reference that resolves to nothing (missing var, blank file) is refused too — an empty
    token would turn the bearer check into a doorstop that admits everyone."""
    if not isinstance(ref, str) or not ref.strip():
        raise TransportConfigError("transport token reference must be env:VAR or file:/path")
    ref = ref.strip()
    if ref.startswith("env:"):
        token = (env or {}).get(ref[4:], "")
    elif ref.startswith("file:"):
        # encoding pinned: a token must read identically on every locale (review finding 3).
        reader = read_file or (lambda p: open(p, encoding="utf-8").read())
        try:
            token = reader(ref[5:])
        except OSError as exc:
            raise TransportConfigError(f"transport token file unreadable: {exc}") from exc
    else:
        raise TransportConfigError(
            "transport token must be held by reference (env:VAR or file:/path), never inline "
            "on a command line")
    token = token.strip()
    if not token:
        raise TransportConfigError(f"transport token reference {ref.split(':', 1)[0]}:… resolved "
                               "to an empty value; refusing to start with a token that "
                               "admits everyone")
    return token


def _bearer_ok(header, token):
    """Strict, constant-time bearer check. An empty configured token NEVER admits (that is a
    config error upstream, but this check stays fail-closed anyway); anything but an exact
    ``Bearer <token>`` refuses."""
    if not token or not isinstance(header, str) or not header.startswith("Bearer "):
        return False
    presented = header[len("Bearer "):]
    return hmac.compare_digest(presented.encode(), token.encode())


def _register_tool_handlers(app, broker, types):
    """The ONE registration of the MCP tool surface — both doors call this (review finding 4:
    the stdio and HTTP copies had already diverged into a maintenance hazard). Handlers offload
    to :func:`dispatch_tool_async` so no door ever runs dispatch on its event loop."""
    @app.list_tools()
    async def list_tools():
        return [types.Tool(name=t["name"], description=t["description"],
                           inputSchema=t["inputSchema"]) for t in TOOLS]

    @app.call_tool()
    async def call_tool(name, arguments):
        return await dispatch_tool_async(broker, types, name, arguments)


async def dispatch_tool_async(broker, types, name, arguments):
    """Run :func:`dispatch_tool` in a worker thread (review finding 1: the sync dispatch under
    ``_DISPATCH_LOCK`` used to run ON the event loop — accidental serialization, deliberate
    loop-blocking). The loop stays live for other connections/heartbeats while the
    ``threading.Lock`` inside ``dispatch_tool`` keeps governed acts strictly one-at-a-time
    across BOTH doors (F5) — serialization by the lock, never by starving the loop."""
    import anyio

    return await anyio.to_thread.run_sync(dispatch_tool, broker, types, name, arguments)


def _asgi_app(manager, token):
    """Build the HTTP door's ASGI app: bearer gate BEFORE the MCP layer, lifespan plumbing,
    everything else to the session manager. Extracted so the 401 and lifespan paths are
    unit-testable without binding a socket."""
    async def asgi(scope, receive, send):
        if scope["type"] == "lifespan":
            # uvicorn's lifespan protocol — run the session manager for the app's lifetime.
            # A channel that dies mid-protocol (review finding 2) exits QUIETLY: the process
            # state is uvicorn's to manage; crashing the handler helps nothing and can strand
            # its graceful shutdown.
            try:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    async with manager.run():
                        await send({"type": "lifespan.startup.complete"})
                        while True:
                            message = await receive()
                            if message["type"] == "lifespan.shutdown":
                                await send({"type": "lifespan.shutdown.complete"})
                                return
            except Exception:
                return
            return
        if token is not None:
            headers = {k.decode().lower(): v.decode()
                       for k, v in scope.get("headers", [])}
            if not _bearer_ok(headers.get("authorization"), token):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body",
                            "body": b"401 unauthorized: this endpoint requires a bearer token"})
                return
        await manager.handle_request(scope, receive, send)
    return asgi


def serve(env=None):
    """Run the agent-facing MCP stdio server. Blocks until the client disconnects."""
    try:
        import asyncio

        import mcp.types as types
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError:
        print("error: the MCP server needs the 'mcp' package. Install with: "
              "pip install 'pacioli[server]'", file=sys.stderr)
        return 2

    try:
        broker = assemble(env, via=STDIO_VIA)
    except RuntimeError_ as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    app = Server("pacioli")
    _register_tool_handlers(app, broker, types)

    async def _run():
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())

    asyncio.run(_run())
    return 0


def serve_http(env=None, *, bind="127.0.0.1", port=8791, auth=None, allowed_hosts=None):
    """Run the HTTP door (MCP streamable HTTP). Blocks until stopped.

    Deny-biased start-up order — every refusal happens BEFORE anything binds:
    (1) a non-loopback ``bind`` with no ``auth`` refuses (exit 2) — opening this door exposed
    without a token is the one thing this function must never do; (2) ``auth``, when given,
    must be a reference (``env:VAR``/``file:/path``) that resolves to a non-empty token —
    inline literals and empty resolutions refuse; (3) only then are the SDK/uvicorn imports
    attempted and the broker assembled. With a token configured, EVERY request must present
    exactly ``Authorization: Bearer <token>`` (constant-time) or it is answered 401 before
    the MCP layer ever sees it.

    The shared in-door perimeter (:func:`pacioli.webguard.guard_asgi` — Host allowlist +
    cross-origin guard + body-size cap) wraps the whole ASGI app; the MCP endpoint has no
    unauthenticated discovery route, so every POST is a protected control path. ``allowed_hosts``
    is the Host allowlist (``None`` → the bind host + loopback forms)."""
    resolved_env = env if env is not None else None
    if _bind_requires_auth(bind) and auth is None:
        print(f"error: bind {bind!r} is not loopback — refusing to start the HTTP transport "
              "without a bearer token (--auth env:VAR or file:/path). Exposing an "
              "ungoverned door is the one thing a governance product must never do",
              file=sys.stderr)
        return 2
    token = None
    if auth is not None:
        import os
        try:
            token = _resolve_transport_token(auth, env if env is not None else dict(os.environ))
        except TransportConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        import mcp.types as types
        import uvicorn
        from mcp.server import Server
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    except ImportError:
        print("error: the HTTP transport needs the 'mcp' package (with its HTTP extras) and "
              "'uvicorn'. Install with: pip install 'pacioli[server]'", file=sys.stderr)
        return 2

    try:
        broker = assemble(resolved_env, via=_http_via(auth))
    except RuntimeError_ as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    app = Server("pacioli")
    _register_tool_handlers(app, broker, types)

    from pacioli.webguard import default_allowed_hosts, guard_asgi

    manager = StreamableHTTPSessionManager(app=app, stateless=True)
    asgi = guard_asgi(
        _asgi_app(manager, token),
        allowed_hosts=allowed_hosts or default_allowed_hosts(bind),
        protect=lambda p: True)  # no unauthenticated discovery route — every POST is control

    print(f"pacioli HTTP transport on {bind}:{port} "
          f"({'bearer token required' if token else 'loopback, no token'}); "
          "TLS is the perimeter's job — front with a reverse proxy before any non-local "
          "exposure", file=sys.stderr)
    uvicorn.run(asgi, host=bind, port=port, log_level="warning")
    return 0


def dispatch_raw(broker, name, arguments):
    """The ONE locked dispatch core every door's rendering wraps (extracted for the A2A door,
    ``docs/plans/2026-07-16-a2a-door.md`` F-A2A-4): returns the broker's dispatch dict
    unrendered — the MCP doors wrap it as MCP content (:func:`dispatch_tool`), the A2A executor
    as a DataPart artifact. New doors wrap THIS, never re-grow a lock of their own.

    Serialized behind :data:`_DISPATCH_LOCK` (F5, the doors ruling): the transports may accept
    concurrent connections, but dispatch processes ONE governed act at a time — the store's
    single-writer discipline is preserved here, not trusted to the transport."""
    with _DISPATCH_LOCK:
        return broker.dispatch(name, arguments or {})


def dispatch_tool(broker, types, name, arguments):
    """Dispatch one MCP tool call through the broker and render the result as MCP content.

    Extracted from the ``@app.call_tool()`` closure so the adapter's dispatch step is unit-testable
    without the MCP SDK or a live stdio transport — the closure was previously the one glue path no
    test exercised, and a stray free variable in it (``pacioli.dispatch`` instead of
    ``broker.dispatch``) rode through every green suite because nothing ever called it.

    Rendering only — the locked dispatch itself is :func:`dispatch_raw` (one core, per-door
    renderings)."""
    return _as_content(types, dispatch_raw(broker, name, arguments))


def _as_content(types, result):
    """Render a dispatch result as MCP text content (compact JSON — deterministic for the client)."""
    import json

    return [types.TextContent(type="text", text=json.dumps(result, sort_keys=True))]
