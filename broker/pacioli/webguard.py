# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — WEBGUARD: the in-door perimeter shared by both network doors.

A raw-ASGI hardening wrapper both the HTTP door (:func:`pacioli.server.serve_http`) and the A2A
door (:func:`pacioli.a2a.build_app`) mount OUTSIDE their own bearer gate, so the transport
perimeter cannot drift between them (the same reason ``dispatch_raw`` is shared, applied to the
door edge). Three fail-closed checks, all deny-biased:

1. **Host allowlist (DNS-rebind guard)** — every http request: a ``Host`` header not on the
   allowlist is refused (400) before anything else runs. Default allowlist = the bind host plus
   the loopback forms; ``["*"]`` disables it and warns LOUDLY (legitimate only behind a reverse
   proxy that validates Host).
2. **Cross-origin guard** — protected POSTs only (loopback-CSRF defense): a browser
   ``Sec-Fetch-Site: cross-site``/``same-site``, or an ``Origin`` that does not match the server
   host, or a body whose ``Content-Type`` is not ``application/json``, is refused (403/415). A
   browser cannot set ``application/json`` cross-origin without a preflight this app never
   answers; non-browser clients (curl, the MCP/A2A SDKs) send none of the trigger headers and
   pass clean.
3. **Body-size cap** — protected POSTs: a ``Content-Length`` over the cap is refused (413)
   immediately, AND — stronger than a Content-Length check alone — a chunked body with no
   declared length is counted as it streams and aborted (413) the moment it crosses the cap, so
   the CL header cannot be dropped to evade the floor.

**Why raw ASGI, not Starlette middleware** (the one divergence from Proximo's webguard, whose
faces are both Starlette): Pacioli's HTTP door is a raw ASGI callable wrapping the MCP session
manager, its A2A door is a Starlette app. A Starlette middleware cannot wrap the former; a
scope-level wrapper wraps both, because every ASGI app is ``(scope, receive, send)``-callable.
Composition-not-coupling — this is Pacioli's own perimeter, sharing no code with any other
project's.

Bearer auth is NOT here — it stays per-door (the A2A door's JSON-RPC ``-32001`` envelope, the
HTTP door's plain 401) so each door keeps its protocol-correct 401 body. This wrapper's own
refusals are pre-protocol and use a uniform tiny JSON body.

**Honest scope, disclosed not implied (security redteam 2026-07-16):** this guard covers HTTP
scopes only (``scope["type"] == "http"``) — a websocket scope passes straight through (the
doors as packaged negotiate no websocket transport, so it is unreachable today, but the coverage
is HTTP by construction, not "every scope"). And the body-size + read-deadline bounds are
PER-REQUEST: they bound one connection's bytes and time, not the NUMBER of concurrent
connections. A per-deployment ceiling on connection count / total memory is the reverse proxy's
or the OS's job (the same honest boundary TLS sits on) — this door does not and cannot impose it
from inside a single request.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import warnings
from urllib.parse import urlparse

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Wall-clock deadline for reading a protected POST's body. The guard reads the body BEFORE the
# per-door bearer gate (it wraps outside it), so without a time bound a stalled/slow client could
# pin an asyncio task pre-auth indefinitely — a slow-POST/slowloris sink (security redteam
# 2026-07-16, Major). A governed call's body is tiny and arrives in one segment; 30s is generous
# for a legitimate slow network yet turns an infinite hold into a finite one. Bytes are bounded by
# DEFAULT_MAX_BODY_BYTES; this bounds TIME — both dimensions of a per-connection cost. A
# per-DEPLOYMENT ceiling on the NUMBER of concurrent connections is out of scope here (the reverse
# proxy / OS's job, like TLS) and disclosed as such.
DEFAULT_BODY_READ_TIMEOUT = 30.0

# A cross-origin browser request carrying these Sec-Fetch-Site values is a forgery attempt against
# a loopback control plane. Modern browsers set the header; non-browser clients never do.
# "same-origin"/"none" (a legit same-origin XHR / a typed URL) are allowed.
_CROSS_ORIGIN_FETCH_SITES = frozenset({"cross-site", "same-site"})

# Params for a governed call are tiny; anything larger is a mistake or a memory-pressure probe.
DEFAULT_MAX_BODY_BYTES = 128 * 1024


def default_allowed_hosts(bind):
    """The DNS-rebind allowlist default: the bind host plus the loopback forms, sorted/deduped."""
    hosts = set(LOOPBACK_HOSTS)
    if isinstance(bind, str) and bind.strip():
        hosts.add(bind.strip())
    return sorted(hosts)


def socket_is_local(server):
    """Is ``scope["server"]`` -- the address this connection actually landed on -- local?

    Shared by every door, and deliberately ONE definition. It was written for the REST door on
    2026-08-17 and lived in ``rest.py``; on 2026-08-24 the HTTP and A2A doors were found to have
    no request-time socket check at all, and copying a hardened security classifier into three
    modules is how two of the copies quietly drift. What must never be re-invented is the
    classification below; the REFUSAL each door emits is not shared, because each door answers in
    its own shape (REST a JSON body, HTTP text/plain, A2A a JSON-RPC error envelope).

    ``scope["server"]`` is server-reported and not forgeable by a client (confirmed: uvicorn's
    ProxyHeadersMiddleware, which ``uvicorn.run`` enables by default, rewrites ``client`` and
    ``scheme`` and never ``server``, so a spoofed ``X-Forwarded-For`` cannot move it). This is the
    whole point: ``bind`` is a CLAIM an operator typed, the socket is a FACT.

    The first version of this reused :func:`pacioli.server._bind_requires_auth`, and that was
    wrong in the way this codebase keeps relearning: same name, different thing. That function
    allowlists three literals an OPERATOR types as a bind argument. A SOCKET address is a
    different domain, and it measured badly on three real shapes -- ``127.0.1.1`` (loopback),
    ``::ffff:127.0.0.1`` (IPv4-mapped loopback on a dual-stack listener), and a Unix socket,
    whose ``server`` is ``(path, None)`` and which has no address at all. Each would have been
    refused. A UDS deployment is arguably a STRICTER boundary than TCP loopback, being gated by
    filesystem permissions rather than reachable by any local UID, and the fix silently
    forbade it.

    Deny-biased on anything it cannot classify. Returns True (allow) for an absent ``server``.
    That branch was first documented as "the in-process harness case", which an adversarial lens
    refuted on 2026-08-24: a REAL uvicorn reaches it too. An abstract-namespace Unix socket (Linux
    ``\0name``) makes ``socket.getsockname()`` return ``bytes``, and uvicorn's ``get_local_addr``
    handles only ``tuple`` and ``str`` before falling through to ``return None`` -- so a live
    abstract-UDS deployment has ``scope["server"] is None``. Measured, not read.

    Allowing it is still right, but NOT for the reason given one paragraph up: an abstract UDS has
    no filesystem permissions gating it at all. It is confined to the network namespace instead,
    which makes it comparable to TCP loopback (reachable by any local UID), and loopback is
    already allowed. In both the absent-server cases -- real abstract UDS and the in-process
    harness -- the builder's declared-bind refusal is the only cover, exactly as before this check
    existed, never worse.
    """
    if not server:
        return True
    host = server[0]
    port = server[1] if len(server) > 1 else None
    if port is None:
        return True                      # Unix domain socket: no address, filesystem-gated
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False                     # unclassifiable: treat as exposed
    # The `ipv4_mapped` unwrap is DEAD as written, measured 2026-08-24 on CPython 3.11/3.12/3.13/
    # 3.14 (every version `requires-python = ">=3.11"` claims): `IPv6Address.is_loopback` already
    # unwraps a mapped address itself, so `ip.is_loopback` alone answers True for
    # `::ffff:127.0.0.1`. A mutation lens proved the shape-table row cannot tell the two apart.
    # Kept, not deleted, and labelled -- the same call the REST door made about `_read_body`'s
    # bounds being dead on POST and load-bearing on GET. It costs nothing, it is correct if a
    # future implementation stops unwrapping, and dead defensive code that LOOKS load-bearing is
    # its own hazard. This comment is the difference.
    return (ip.ipv4_mapped or ip).is_loopback if ip.version == 6 else ip.is_loopback


def _host_ok(host_header, allowed):
    """True when the request's ``Host`` (port stripped, case-folded) is on the allowlist. A ``*``
    in the allowlist means the check is disabled (accept any). An empty/None Host fails closed."""
    if "*" in allowed:
        return True
    if not isinstance(host_header, str) or not host_header.strip():
        return False
    host = host_header.strip()
    # strip a trailing :port — but not from a bare bracketed/unbracketed IPv6 literal's colons.
    if host.startswith("["):
        host = host.split("]", 1)[0].lstrip("[")
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.casefold() in {a.casefold() for a in allowed}


def _is_cross_origin(origin, host_header):
    """True when an ``Origin`` header is present and does not match the server's own host. A
    ``null``/malformed origin is treated as cross (fail closed). Non-browser clients omit
    ``Origin`` entirely, so they never reach a True here."""
    parts = urlparse(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return True
    return parts.netloc.casefold() != (host_header or "").strip().casefold()


def refuse_exposed_socket(inner, token, *, reject):
    """Wrap ``inner`` so an UNTOKENED door answering on a non-local socket is refused at once.

    The CONDITION lives here and nowhere else. All three doors need it, it is the whole guard, and
    a security predicate copied into three modules is a predicate that will differ in two of them
    within a year. What is genuinely per-door is the response SHAPE, so that arrives as ``reject``:
    REST answers its ``{"ok": false, "error": ...}`` envelope, the MCP HTTP door answers
    text/plain like its own 401, and A2A answers this module's ``{"error": ...}`` perimeter shape.

    ``token in (None, "")`` rather than ``is None``: this guard exists precisely for the case where
    no bearer gate will run, and an empty token installs no gate on any door. The two conditions
    MUST agree. On 2026-08-24 the REST door had them disagreeing -- socket check on ``is None``,
    bearer gate on ``is not None`` -- and the two narrownesses cancelled into an accidental
    fail-closed that answered 401 to everyone. Widening only the gate would have turned that
    accident into a genuinely open door, measured.

    ALWAYS wrap this OUTSIDE :func:`guard_asgi`, never inside. The perimeter drains a POST body in
    full before calling the app it wraps, so a refusal from inside costs the perimeter's whole
    read deadline before it answers. Order of wrapping is a security property.
    """
    async def guarded(scope, receive, send):
        if (scope.get("type") == "http" and token in (None, "")
                and not socket_is_local(scope.get("server"))):
            await reject(send)
            return
        await inner(scope, receive, send)

    return guarded


def _reject(status, message):
    """Return an ASGI-send coroutine factory for a uniform perimeter rejection."""
    body = json.dumps({"error": message}).encode()

    async def send_rejection(send):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    return send_rejection


def _header(scope, name):
    """First value of header ``name`` from an ASGI scope, decoded, or None. Case-insensitive on
    BOTH sides: the client's header casing is handled by ``k.lower()`` (so a ``Host:`` sent with
    any casing is still matched — this is what makes the Host guard casing-proof), and ``name``
    is lowered too so a caller can't silently miss a header by passing a capitalized name."""
    target = name.lower().encode()
    for k, v in scope.get("headers", []):
        if k.lower() == target:
            try:
                return v.decode("latin-1")
            except Exception:
                return None
    return None


def guard_asgi(app, *, allowed_hosts, protect, max_body_bytes=DEFAULT_MAX_BODY_BYTES,
               body_read_timeout=DEFAULT_BODY_READ_TIMEOUT):
    """Wrap an ASGI ``app`` in the shared perimeter. ``allowed_hosts`` is the Host allowlist
    (``["*"]`` disables + warns); ``protect(path)`` selects the door's control endpoints (the
    cross-origin + body-size checks apply to POSTs there); ``max_body_bytes`` caps a protected
    POST body by size and ``body_read_timeout`` caps the time spent reading it (the slow-POST
    bound — see :data:`DEFAULT_BODY_READ_TIMEOUT`). Lifespan and non-http scopes pass straight
    through."""
    allowed = list(allowed_hosts)
    if "*" in allowed:
        warnings.warn(
            "pacioli door host allowlist contains '*' — the DNS-rebind/Host guard is DISABLED; "
            "any Host header is accepted. Only safe behind a reverse proxy that validates Host.",
            stacklevel=2)

    async def guarded(scope, receive, send):
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return

        # 1. Host allowlist — every request, before anything else.
        if not _host_ok(_header(scope, "host"), allowed):
            await _reject(400, "bad or missing Host header")(send)
            return

        path = scope.get("path", "/")
        if scope.get("method") == "POST" and protect(path):
            # 2. Cross-origin guard.
            if (_header(scope, "sec-fetch-site") or "").casefold() in _CROSS_ORIGIN_FETCH_SITES:
                await _reject(403, "cross-origin request refused")(send)
                return
            origin = _header(scope, "origin")
            if origin is not None and _is_cross_origin(origin, _header(scope, "host")):
                await _reject(403, "cross-origin request refused")(send)
                return

            cl = _header(scope, "content-length")
            has_body = (cl not in (None, "0")) or _header(scope, "transfer-encoding") is not None
            if has_body:
                media = (_header(scope, "content-type") or "").split(";")[0].strip().casefold()
                if media != "application/json":
                    await _reject(415, "Content-Type must be application/json")(send)
                    return
                # 3a. declared length over the cap → refuse immediately (cheap).
                if cl is not None and cl.isdigit() and int(cl) > max_body_bytes:
                    await _reject(413, "request body too large")(send)
                    return
            # 3b. buffer-and-check the body on EVERY protected POST BEFORE the app runs — the
            # declared length is never trusted, so a chunked request that drops Content-Length
            # (or lies in it) is still refused. Buffer at most cap+1 bytes (bounded, safe: the
            # cap is small), reject 413 if exceeded, else replay the buffered body to the app.
            # This is robust where a raise-inside-receive is NOT: an app that catches broadly
            # (the a2a-sdk's dispatcher does) would swallow an exception into a 200, but it never
            # gets the chance — the guard owns the read and answers before the app is called.
            # The read is deadline-bounded (this happens pre-auth, so a stalled client must not
            # pin a task forever): a body that outlives ``body_read_timeout`` is refused 408.
            try:
                over, buffered, disconnected = await asyncio.wait_for(
                    _read_capped(receive, max_body_bytes), timeout=body_read_timeout)
            except (asyncio.TimeoutError, TimeoutError):
                await _reject(408, "request body read timed out")(send)
                return
            if over:
                await _reject(413, "request body too large")(send)
                return
            receive = _replay_receive(buffered, receive, disconnected=disconnected)

        await app(scope, receive, send)

    return guarded


async def _read_capped(receive, cap):
    """Drain the request body from ``receive``, accumulating up to ``cap`` bytes. Returns
    ``(over, buffered, disconnected)`` — ``over`` True the moment the cumulative body exceeds
    ``cap`` (stops reading further), else the full ``buffered`` bytes; ``disconnected`` True
    when the client went away mid-body. Never reads more than ``cap + one chunk`` into memory."""
    buffered = bytearray()
    more = True
    while more:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return False, bytes(buffered), True
        buffered += message.get("body", b"")
        if len(buffered) > cap:
            return True, b"", False
        more = message.get("more_body", False)
    return False, bytes(buffered), False


def _replay_receive(body, receive, *, disconnected):
    """An ASGI ``receive`` that yields the already-buffered ``body`` as one ``http.request``
    message — so the wrapped app reads exactly the capped body the guard vouched for, once —
    then DELEGATES to the real ``receive``. Apps poll receive() mid-response to watch for a
    client disconnect (the MCP streamable-HTTP session manager does, while streaming SSE);
    synthesizing ``http.disconnect`` here made every such response abort as "client gone"
    while the client sat connected and waiting. A live client blocks on the real receive;
    a real disconnect surfaces from it. Only a disconnect the guard ALREADY saw during the
    body read is answered directly — never re-poll a channel known dead."""
    delivered = False

    async def replay():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnected:
            return {"type": "http.disconnect"}
        return await receive()

    return replay
