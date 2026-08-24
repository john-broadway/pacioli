# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The REST door. **A door admits; it never decides.**

John, 2026-08-17: *"every door governed"*, and the surface is the **governed verb set**, not the
265-tool catalog projected into HTTP. The spine is the product; a 265-route API would reframe
Pacioli as somebody's ERPNext proxy with governance bolted on. Thirteen routes reach 163 governed
tools, because ``{doctype}`` is a path parameter.

This module carries. Every route resolves a tool NAME and hands it to
:func:`pacioli.server.dispatch_raw` -- the same process-wide lock, the same worker-thread offload,
the same refusals as every other door. There is no validation here and that is correct: mcp 2.0.0
removed SDK-side validation entirely and the OUTCOME did not move, because ``dispatch`` is the
refusal and always was.

**There is no consent endpoint, and that is a security property, not a gap.** The broker's catalog
contains no consent or marker tool at all -- checked, zero. A ``Pacioli Consent Marker`` is minted
on the bench by a DIFFERENT principal from the credential it authorises, and ``minted_by`` is set
server-side from the session. A REST route that minted consent would hand the broker's own
credential the power to authorise itself, which is precisely the bypass the guard exists to close.
The door plans and it submits; it cannot consent. Anyone reading this file looking for the missing
endpoint should stop looking.

**No framework.** Thirteen routes do not justify a web framework, and a framework is a dependency
plus a security surface. This is a plain ASGI app over the shared perimeter
(:func:`pacioli.webguard.guard_asgi`), so the ``rest`` extra is ``uvicorn`` and ``anyio`` -- notably
NOT ``mcp``, which this door has no use for. anyio is declared because uvicorn does not depend on
it and mcp does: every environment this was written in had mcp, so the dispatch path's
``anyio.to_thread.run_sync`` resolved by accident and ``pacioli[rest]`` alone would have installed
a door that died at the first request.
"""
from __future__ import annotations

import json
import sys

import anyio

from pacioli.server import (
    TransportConfigError,
    _bearer_ok,
    _bind_requires_auth,
    _resolve_transport_token,
    dispatch_raw,
)
from pacioli.tools import TOOLS

DEFAULT_PORT = 8793
_MAX_DOCTYPE_SLUG = 64


def _rest_via(auth_ref):
    """The REST door's ledger stamp (checklist point 3, the principal lands in the ledger).

    The principal is the token's REFERENCE label (``env:REST_T``), never the token, or
    ``loopback`` when no token is configured. Same shape as :func:`pacioli.server._http_via` and
    :func:`pacioli.a2a._a2a_via`, so a receipt says which door and who asked."""
    return {"transport": "rest",
            "principal": auth_ref if auth_ref is not None else "loopback"}


def _slug(doctype_tool_suffix):
    """``sales_invoice`` -> ``sales-invoice``. One convention, hyphens, chosen once here."""
    return doctype_tool_suffix.replace("_", "-")


def _doctype_map(prefix):
    """``{slug: tool_name}`` for one mechanical verb, derived from the catalog.

    ALLOWLIST MEMBERSHIP, never string assembly. Building ``f"{prefix}_{slug}"`` from the URL and
    letting dispatch refuse the unknown ones would make the door decide the vocabulary -- it would
    accept a shape dispatch never agreed to, and a typo would surface as a dispatch error rather
    than a 404 from the door that owns the routing. The set comes from ``TOOLS``, which
    ``test_tool_surface`` pins independently.
    """
    return {_slug(t["name"][len(prefix) + 1:]): t["name"]
            for t in TOOLS if t["name"].startswith(prefix + "_")}


SUBMIT = _doctype_map("submit")
CANCEL = _doctype_map("cancel")
AMEND = _doctype_map("amend")

# The fixed routes: (METHOD, path) -> tool name. Everything else is {doctype}-parameterised above.
FIXED_ROUTES = {
    ("POST", "/v1/plan/submit"): "plan_submit",
    ("POST", "/v1/plan/cancel"): "plan_cancel",
    ("POST", "/v1/plan/cascade-cancel"): "plan_cascade_cancel",
    ("POST", "/v1/plan/reconcile"): "plan_reconcile",
    ("POST", "/v1/cascade-cancel"): "cascade_cancel",
    ("POST", "/v1/reconcile"): "reconcile",
    ("POST", "/v1/workflow/transition"): "request_workflow_transition",
    ("GET", "/v1/workflow/status"): "workflow_status",
    ("GET", "/v1/prove/verify"): "prove_verify",
    ("GET", "/v1/prove/orphans"): "prove_orphans",
}

_PARAM_ROUTES = {"submit": SUBMIT, "cancel": CANCEL, "amend": AMEND}


def resolve(method, path):
    """(method, path) -> tool name, or None. Pure, so the routing table is testable alone.

    Returns ``None`` for anything unrouted; the caller answers 404. No fallthrough to dispatch:
    a name this door cannot resolve is a name this door does not send.
    """
    fixed = FIXED_ROUTES.get((method, path))
    if fixed is not None:
        return fixed
    if method != "POST":
        return None
    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "v1":
        return None
    verb, slug = parts[1], parts[2]
    if len(slug) > _MAX_DOCTYPE_SLUG:
        return None
    return _PARAM_ROUTES.get(verb, {}).get(slug)


async def _json_response(send, status, payload):
    body = json.dumps(payload, sort_keys=True).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive, limit, timeout):
    """Read the request body, bounded in BYTES and in TIME.

    The earlier version of this docstring said the perimeter "caps size by Content-Length" and
    called this the belt for a chunked body. That undersold the perimeter, which already drains
    and caps every protected POST regardless of the declared length -- and it hid a real hole,
    because the perimeter's protections (size AND its 30s read timeout) apply to **POST only**.
    The three GET routes reached this function with the raw, unwrapped ASGI receive and no
    deadline of any kind. Measured by an adversarial pass: a GET declaring a body, sending one
    byte and going silent, was still held open after 38 seconds, while the equivalent POST was
    refused at 30. On the DEFAULT deployment (loopback, no token) any local process could open
    those and hold them until file descriptors ran out.

    Both bounds here are DEAD ON POST and load-bearing on GET, proven by response shape: an
    oversized POST answers with the perimeter's ``{"error": ...}`` (no ``ok`` key) because
    ``guard_asgi`` always wins that race, while the same oversized GET answers with this module's
    ``{"ok": false, "error": ...}``. Kept rather than deleted because the GET path has no other
    protection, and both layers now receive the same ``cap``/``timeout`` values so they cannot
    drift -- but nothing structurally enforces that coupling, which is precisely how the
    ``max_body_bytes`` dead-knob arose.
    """
    chunks, total = [], 0
    while True:
        with anyio.fail_after(timeout):
            message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def build_rest_app(broker, *, token=None, bind="127.0.0.1", allowed_hosts=None,
                   max_body_bytes=None, body_read_timeout=None):
    """Assemble the REST door's ASGI app: bearer gate, then routing, then the spine.

    DEFENSE-IN-DEPTH, and this door was built knowing why. On 2026-08-17 the MCP HTTP door's
    equivalent builder shipped WITHOUT this refusal, because the deny-biased check lived only in
    its ``serve_`` wrapper -- and a security lens demonstrated a fully-open governed door by
    calling the builder directly. :func:`pacioli.a2a.build_app` had carried the guard since a
    redteam finding on 2026-07-16 and the lesson did not travel. It travels here. The empty-string
    form (``token in (None, "")``) is the corrected version: an empty token is not a token, and
    without this it would build a door whose bearer gate then refused everyone, a silent lock
    rather than an honest refusal.
    """
    from pacioli.webguard import (
        DEFAULT_BODY_READ_TIMEOUT,
        DEFAULT_MAX_BODY_BYTES,
        default_allowed_hosts,
        guard_asgi,
        refuse_exposed_socket,
    )

    if _bind_requires_auth(bind) and token in (None, ""):
        raise TransportConfigError(
            f"bind {bind!r} is not loopback — refusing to build a REST door with no bearer "
            "token; a governed door that admits everyone is the one thing this must never "
            "construct")
    cap = max_body_bytes or DEFAULT_MAX_BODY_BYTES
    read_timeout = body_read_timeout or DEFAULT_BODY_READ_TIMEOUT

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        if token not in (None, ""):
            raw_headers = scope.get("headers", [])
            # RFC 9110 makes Authorization a singleton. A dict comprehension over the ASGI header
            # LIST silently keeps the last occurrence, so two Authorization headers meant the last
            # one won: send a wrong token then the real one and you were in. Harmless alone (you
            # still need the real token), but the deployment guidance here is to front this with a
            # reverse proxy, and a proxy that authorises on the FIRST header while we honour the
            # LAST is two layers disagreeing about which credential a request carried. Refuse.
            auths = [v for k, v in raw_headers if k.decode().lower() == "authorization"]
            if len(auths) > 1:
                await _json_response(
                    send, 400,
                    {"ok": False, "error": "multiple Authorization headers"})
                return
            if not _bearer_ok(auths[0].decode() if auths else None, token):
                await _json_response(
                    send, 401,
                    {"ok": False, "error": "unauthorized: this endpoint requires a bearer token"})
                return

        method, path = scope.get("method", ""), scope.get("path", "/")
        tool = resolve(method, path)
        if tool is None:
            await _json_response(send, 404, {"ok": False, "error": f"no route for {method} {path}"})
            return

        try:
            raw = await _read_body(receive, cap, read_timeout)
        except TimeoutError:
            # The timeout that fixed the GET slowloris then escaped uncaught: uvicorn answered
            # plain-text 500 (breaking this file's every-failure-is-JSON contract) and logged a
            # full traceback at ERROR for every legitimately slow client. Same 408 shape the
            # perimeter already uses for its own read timeout.
            await _json_response(send, 408,
                                 {"ok": False, "error": "request body read timed out"})
            return
        if raw is None:
            await _json_response(send, 413, {"ok": False, "error": "request body too large"})
            return
        if raw:
            try:
                params = json.loads(raw)
            except ValueError:
                await _json_response(send, 400, {"ok": False, "error": "body is not valid JSON"})
                return
            if not isinstance(params, dict):
                await _json_response(
                    send, 400, {"ok": False, "error": "body must be a JSON object of arguments"})
                return
        else:
            params = {}

        try:
            # The same worker-thread offload and process-wide lock as every other door (F5): the
            # loop stays live, governed acts run strictly one-at-a-time.
            result = await anyio.to_thread.run_sync(dispatch_raw, broker, tool, params)
        except Exception as exc:  # noqa: BLE001 — last-resort sanitize; never leak a traceback
            # dispatch() answers structured denies as ok:False DICTS, so anything raising here is
            # glue-level. Name the exception TYPE only, never its message (which can carry paths
            # or parameters) and never a traceback. Copied deliberately from the A2A door: this
            # is a security property, not a style choice.
            await _json_response(send, 500,
                                 {"ok": False, "error": f"tool '{tool}' failed: {type(exc).__name__}"})
            return
        # 200 carries the spine's own verdict, refusals included. An ok:False is a GOVERNED
        # answer, not a transport error, and flattening it to 4xx would let a caller confuse
        # "the spine refused you" with "the door could not route you".
        await _json_response(send, 200, result)

    # max_body_bytes MUST be forwarded. It was not, and the perimeter therefore enforced its own
    # 128 KiB default no matter what a caller configured: a public kwarg the code silently ignored
    # (found independently by two adversarial passes). It failed safe, always stricter and never
    # looser, which is exactly why nobody would have noticed.
    return refuse_exposed_socket(
        guard_asgi(app,
                   allowed_hosts=allowed_hosts or default_allowed_hosts(bind),
                   protect=lambda p: True,
                   max_body_bytes=cap,
                   body_read_timeout=read_timeout),
        token,
        # This door's own envelope. Every other REST refusal carries `ok`, so this one does too.
        reject=lambda send: _json_response(
            send, 403,
            {"ok": False, "error": "this door was built without a bearer token and is "
                                   "answering on a non-local socket; refusing"}))


def serve_rest(env=None, *, bind="127.0.0.1", port=DEFAULT_PORT, auth=None, allowed_hosts=None):
    """Run the REST door. Blocks until stopped.

    Deny-biased start-up order, IDENTICAL to the HTTP and A2A doors: (1) a typo'd
    ``PACIOLI_TOOLSETS`` refuses this door exactly as it refuses the others, or one operator
    mistake refuses one transport and is silently swallowed by another; (2) a non-loopback
    ``bind`` with no ``auth`` refuses (exit 2); (3) ``auth`` must be a reference resolving to a
    non-empty token; (4) only then are imports attempted and the broker assembled with
    ``via.transport: "rest"``.
    """
    import os

    from pacioli.doorway import served_tools
    from pacioli.runtime import RuntimeError_, assemble

    try:
        served_tools(env)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if _bind_requires_auth(bind) and auth is None:
        print(f"error: bind {bind!r} is not loopback — refusing to start the REST transport "
              "without a bearer token (--auth env:VAR or file:/path). Exposing an "
              "ungoverned door is the one thing a governance product must never do",
              file=sys.stderr)
        return 2
    token = None
    if auth is not None:
        try:
            token = _resolve_transport_token(auth, env if env is not None else dict(os.environ))
        except TransportConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        import uvicorn
    except ImportError:
        print("error: the REST transport needs 'uvicorn'. Install with: "
              "pip install 'pacioli[rest]'", file=sys.stderr)
        return 2

    try:
        broker = assemble(env, via=_rest_via(auth))
    except RuntimeError_ as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    app = build_rest_app(broker, token=token, bind=bind, allowed_hosts=allowed_hosts)
    print(f"pacioli REST transport on {bind}:{port} "
          f"({'bearer token required' if token else 'loopback, no token'}); "
          f"{len(FIXED_ROUTES) + len(_PARAM_ROUTES)} routes over "
          f"{len(FIXED_ROUTES) + len(SUBMIT) + len(CANCEL) + len(AMEND)} governed tools; "
          "no consent endpoint by design; TLS is the perimeter's job — front with a reverse "
          "proxy before any non-local exposure", file=sys.stderr)
    uvicorn.run(app, host=bind, port=port, log_level="warning")
    return 0
