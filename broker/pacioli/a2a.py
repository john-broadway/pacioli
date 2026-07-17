# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — the A2A door (glue): the first non-MCP door.

**A DOOR ADMITS; IT NEVER DECIDES** (the doors ruling). Stdio and streamable-HTTP are both MCP;
this door speaks A2A v1.0 (Agent2Agent — agent→agent delegation, JSON-RPC over HTTP, Agent Card
discovery at ``/.well-known/agent-card.json``) — the first protocol the spine has never seen. It
carries the FULL governed tool surface, curates nothing, and routes every call through
:func:`pacioli.server.dispatch_raw` — the same process-wide lock and the same
``PacioliBroker.dispatch`` the MCP doors use. No second dispatch path, no second refusal logic.

Wire convention (the house shape — Proximo's A2A door speaks the same one; shape, not coupling):
the inbound Message must carry a DataPart whose data is ``{"tool": "<name>", "params": {...}}``.
``"skill"`` is accepted as an alias for ``"tool"``; absent/null ``params`` is an empty dict; any
other shape produces a clean failed task, never a traceback. An UNKNOWN tool goes through
dispatch like any other call and comes back as the structured ``stage: request`` deny PROVE
already records — hostile enumeration is a ledger entry, not an invisible transport error.

Deny-biased posture, identical to the HTTP door (the same tested primitives from
:mod:`pacioli.server`, not re-grown copies): OFF by default (``pacioli serve --a2a`` is a
deliberate act); binds loopback by default; a non-loopback bind REFUSES TO START without a
bearer token held by reference (``env:VAR``/``file:/path``); the JSON-RPC route answers 401
before the SDK sees the request. The Agent Card stays readable WITHOUT auth — an A2A client must
be able to discover how to authenticate before it can authenticate — and when a token is
configured the card DECLARES the bearer scheme so clients self-configure from discovery.

HONEST NOTES, said plainly rather than half-fixed:

* **A standing listener.** An A2A door listens by the protocol's nature. Acceptable only because
  doors are opt-in, loopback-default, and deny-biased at startup (F6, the doors ruling).
* **Card signing is opt-in (0.29.0).** The card is served UNSIGNED by default; set
  ``PACIOLI_A2A_SIGNING_KEY_FILE`` to an EC P-256 key (mint one with ``pacioli a2a-keygen``) and
  the card is ES256-signed, with its public key served at ``/.well-known/jwks.json``. The key
  lives in the OPERATOR's tier (same as the seal key + consent marker) — 0600, refuse-if-exposed,
  never auto-minted. Honest ceiling: a signing key only proves authorship if the agent cannot
  REWRITE it (a compromised broker re-signs a forged card), so it must live outside the agent's
  own write reach; and a peer must pin the public key OUT-OF-BAND (a card's own ``jku`` is not a
  trust root — see :func:`verifier_for_jwk`).
* **The in-door perimeter covers Host/CORS/size; TLS stays the proxy's job.** Since 0.28.0
  this door mounts the shared :func:`pacioli.webguard.guard_asgi` (Host-header/DNS-rebind
  allowlist + cross-origin guard + a body-size cap) outside the bearer gate, across both
  network doors — so a bad Host is 400, a browser cross-origin POST is 403/415, and an
  oversized body (including a chunked one that drops ``Content-Length``) is 413, all in-process
  rather than delegated. TLS is the one honest remaining reverse-proxy job — front with a proxy
  for TLS before any non-local exposure. Non-browser callers (curl, a local process) send none
  of the cross-origin triggers and pass the perimeter transparently.

The ``a2a-sdk`` is an OPTIONAL dependency (``pip install 'pacioli[a2a]'``), imported lazily —
the pure cores and every CLI path never require it.
"""
from __future__ import annotations

import sys
from pathlib import Path

from pacioli.runtime import RuntimeError_, assemble
from pacioli.server import (
    TransportConfigError,
    _bearer_ok,
    _bind_requires_auth,
    _resolve_transport_token,
    dispatch_raw,
)
from pacioli.tools import TOOLS

DEFAULT_PORT = 8792  # the HTTP door is 8791 — adjacency reads as family


def _a2a_via(auth_ref):
    """The A2A door's ledger stamp (F3): the principal is the token's REFERENCE label (e.g.
    ``env:A2A_T``) — never the token itself — or ``loopback`` when no token is configured."""
    return {"transport": "a2a",
            "principal": auth_ref if auth_ref is not None else "loopback"}


SIGNING_KEY_ENV = "PACIOLI_A2A_SIGNING_KEY_FILE"  # noqa: S105 — env var NAME, not a secret
_JWKS_PATH = "/.well-known/jwks.json"


# --- card signing (docs/plans/2026-07-17-a2a-card-signing.md) ---------------------------------
# ES256/JWS agent-card signing — Pacioli's OWN (composition-not-coupling; mechanism mirrors
# Proximo's SIGNET). The key lives in the operator's tier (same as the seal key + consent
# marker): a PEM EC P-256 held BY REFERENCE at PACIOLI_A2A_SIGNING_KEY_FILE, 0600, refuse-if-
# exposed — the honest ceiling is that a signing key only proves authorship if the agent cannot
# REWRITE it (a compromised broker re-signs a forged card), so it must live where the agent's
# own surface cannot author it. Opt-in: unset → unsigned card; set → sign or FAIL LOUD. All
# crypto imports are lazy (the pure/CLI paths never need them).


class SigningKey:
    """The operator's A2A signing key: private PEM (to sign) + public key + a stable ``kid``
    (RFC 7638 JWK thumbprint). Built by :func:`load_signing_key`."""

    __slots__ = ("private_pem", "public_key", "kid")

    def __init__(self, private_pem, public_key, kid):
        self.private_pem = private_pem
        self.public_key = public_key
        self.kid = kid


def _b64url(b):
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _p256_xy(pub):
    nums = pub.public_numbers()
    return _b64url(nums.x.to_bytes(32, "big")), _b64url(nums.y.to_bytes(32, "big"))


def _thumbprint(pub):
    """RFC 7638 JWK thumbprint over the canonical required members — a stable, derived ``kid``."""
    import hashlib
    import json as _json
    x, y = _p256_xy(pub)
    members = {"crv": "P-256", "kty": "EC", "x": x, "y": y}
    canon = _json.dumps(members, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64url(hashlib.sha256(canon).digest())


def load_signing_key(path):
    """Load the operator's EC **P-256** signing key (PEM) from ``path``, refusing anything that
    would make signing meaningless: a missing file, a key that is group/world-readable (the
    seal-key discipline — a leaked key voids the assertion), a non-EC or non-P-256 key. Returns a
    :class:`SigningKey` with a derived thumbprint ``kid``. Never auto-mints (use
    ``pacioli a2a-keygen``) and never serves unsigned when a key was configured (the caller
    fails loud on this raising)."""
    import stat as _stat

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from pacioli.runtime import RuntimeError_ as _RE

    p = Path(path)
    if not p.exists():
        raise _RE(f"A2A signing key {path} does not exist — mint one with `pacioli a2a-keygen` "
                  f"or unset {SIGNING_KEY_ENV} to serve an unsigned card")
    if not p.is_file():
        # a directory / FIFO / device at the path passes the mode check but blows up (or hangs)
        # on read — refuse cleanly (security redteam 2026-07-17) rather than a raw OSError.
        raise _RE(f"A2A signing key {path} is not a regular file")
    mode = _stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        raise _RE(f"A2A signing key {path} has permissions {oct(mode)}; it must be 0600 "
                  f"(owner-only) — a leaked signing key voids every card seal. run: chmod 600 "
                  f"{path}")
    pem = p.read_bytes()
    try:
        priv = serialization.load_pem_private_key(pem, password=None)
    except Exception as exc:  # noqa: BLE001 — any parse failure is a bad-key config error
        raise ValueError(f"A2A signing key {path} is not a readable PEM private key: "
                         f"{type(exc).__name__}") from exc
    if not isinstance(priv, ec.EllipticCurvePrivateKey) or \
            not isinstance(priv.curve, ec.SECP256R1):
        raise ValueError(
            "A2A signing key must be an EC P-256 (prime256v1) private key for ES256 "
            f"(got {type(priv).__name__})")
    pub = priv.public_key()
    return SigningKey(private_pem=pem, public_key=pub, kid=_thumbprint(pub))


def sign_card(card, key, *, jku=None):
    """Press an ES256/JOSE seal onto ``card`` (mutates in place, returns it). ``alg`` is PINNED
    to ES256 — never the a2a-sdk's HS256 default — so the seal is asymmetric and cannot be forged
    from the public key (the JWT algorithm-confusion class, closed at the source)."""
    from a2a.utils.signing import create_agent_card_signer

    header = {"alg": "ES256", "typ": "JOSE", "kid": key.kid, "jku": jku}
    signer = create_agent_card_signer(signing_key=key.private_pem, protected_header=header)
    return signer(card)


def public_jwk(key):
    """The operator's PUBLIC key as a JWK (RFC 7517) — public point only, NEVER the private
    scalar ``d``. This is what a peer needs to verify a card seal."""
    x, y = _p256_xy(key.public_key)
    return {"kty": "EC", "crv": "P-256", "x": x, "y": y,
            "kid": key.kid, "use": "sig", "alg": "ES256"}


def jwks(key):
    """A JWK Set wrapping the operator's public key — served at ``/.well-known/jwks.json``."""
    return {"keys": [public_jwk(key)]}


def verifier_for_jwk(jwk):
    """An **ES256-only** verifier pinned to ONE trusted public JWK (obtained OUT-OF-BAND, not
    fetched from a card's ``jku``) — the CLIENT-side safe pattern. Binds to that key and IGNORES
    whatever ``kid``/``jku`` a card presents, so a MITM cannot substitute their key by pointing
    ``jku`` at an attacker JWKS. The ``algorithms=['ES256']`` allowlist refuses an HS256 downgrade
    outright. Returns a callable that raises on a card that does not verify (or carries no seal).
    Included here (rather than only in a client) so the broker's own tests prove the seal it
    presses is verifiable and downgrade-proof."""
    from a2a.utils.signing import create_signature_verifier
    from jwt import PyJWK

    pinned = PyJWK.from_dict(jwk)

    def key_provider(kid, jku):
        return pinned

    return create_signature_verifier(key_provider=key_provider, algorithms=["ES256"])


def _parse_tool_call(message, get_data_parts):
    """Extract ``(tool_name, params)`` from an inbound A2A Message per the wire convention.

    Returns ``(None, None)`` when no DataPart carries a ``tool``/``skill`` key — the caller
    fails the task with the expected-shape message. The FIRST matching part wins; ``params``
    that is anything but a dict is treated as empty (a non-dict params is a malformed call the
    dispatch layer's own schema validation will refuse loudly, with the tool named)."""
    if message is None:
        return None, None
    for payload in get_data_parts(message.parts):
        if isinstance(payload, dict) and ("tool" in payload or "skill" in payload):
            tool_name = payload.get("tool", payload.get("skill"))
            raw = payload.get("params")
            return tool_name, (raw if isinstance(raw, dict) else {})
    return None, None


def make_executor(broker):
    """Build the A2A executor bound to ``broker``. Imports the SDK here so importing
    :mod:`pacioli.a2a` itself never requires it (the class statement needs the SDK's
    ``AgentExecutor`` base at definition time)."""
    import uuid

    import anyio
    from a2a.helpers.proto_helpers import (
        get_data_parts,
        new_data_part,
        new_task,
        new_text_part,
    )
    from a2a.server.agent_execution import AgentExecutor
    from a2a.server.tasks import TaskUpdater
    from a2a.types.a2a_pb2 import Message, Role, TaskState
    from a2a.utils.errors import UnsupportedOperationError

    class PacioliAgentExecutor(AgentExecutor):
        """Stateless A2A executor — parse, route through the ONE locked dispatch, reply."""

        async def _fail(self, event_queue, context, message):
            # SDK 1.1 lifecycle: a bare TaskStatusUpdateEvent with no prior Task event is
            # refused (InvalidAgentResponseError in active_task), and a SUBMITTED-Task-then-
            # failed-status pair races the non-streaming response snapshot (probed live:
            # the client read SUBMITTED). ONE terminal Task event — state FAILED, the
            # explanation riding as an agent message in its history — is the shape the SDK
            # returns faithfully in both streaming and polling modes.
            agent_msg = Message(
                message_id=str(uuid.uuid4()), role=Role.ROLE_AGENT,
                parts=[new_text_part(message)],
                task_id=context.task_id or "", context_id=context.context_id or "")
            await event_queue.enqueue_event(new_task(
                context.task_id or "", context.context_id or "",
                TaskState.TASK_STATE_FAILED, history=[agent_msg]))

        async def execute(self, context, event_queue):
            updater = TaskUpdater(event_queue, context.task_id or "", context.context_id or "")
            tool_name, params = _parse_tool_call(context.message, get_data_parts)

            if tool_name is None:
                await self._fail(
                    event_queue, context,
                    'Expected a DataPart with shape {"tool": "<name>", "params": {...}}.'
                    " No such part found in the inbound message.")
                return

            try:
                # the same worker-thread offload + process-wide lock as every other door (F5):
                # the loop stays live, governed acts run strictly one-at-a-time.
                result = await anyio.to_thread.run_sync(dispatch_raw, broker, tool_name, params)
            except Exception as exc:  # noqa: BLE001 — last-resort sanitize; never leak a traceback
                # dispatch() itself answers structured denies as ok:False DICTS, so anything
                # raising here is glue-level. Name the exception TYPE only — never its message
                # (which can carry paths/params) and never a traceback.
                await self._fail(event_queue, context,
                                 f"tool '{tool_name}' failed: {type(exc).__name__}")
                return

            await updater.add_artifact(parts=[new_data_part(result)], name="result")
            await updater.complete()

        async def cancel(self, context, event_queue):
            raise UnsupportedOperationError()

    return PacioliAgentExecutor()


def build_agent_card(rpc_url, *, secured=False, signing_key=None, jwks_url=None):
    """The machine-readable capability advertisement: one ``AgentSkill`` per governed tool —
    the FULL surface an MCP client sees, 1-to-1, tags carrying the tool-family prefix so a peer
    can filter without a naming convention. ``secured=True`` declares the bearer scheme the
    server enforces, so clients self-configure from discovery instead of learning auth from a
    401. When ``signing_key`` is given the card is ES256-signed (:func:`sign_card`) with
    ``jku`` → ``jwks_url`` (the served JWKS); unsigned otherwise (opt-in — the key-custody
    ruling in ``docs/plans/2026-07-17-a2a-card-signing.md``)."""
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentInterface,
        AgentSkill,
        SecurityRequirement,
    )
    from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol

    from pacioli import __version__

    skills = [
        AgentSkill(
            id=t["name"],
            name=t["name"],
            description=t["description"],
            tags=[t["name"].split("_", 1)[0]],
        )
        for t in TOOLS
    ]

    interface = AgentInterface(
        url=rpc_url,
        protocol_binding=TransportProtocol.JSONRPC,
        protocol_version=PROTOCOL_VERSION_CURRENT,
    )

    card = AgentCard(
        name="Pacioli",
        description=(
            "Governed ERPNext bookkeeping agent — every write is planned, human-consented "
            "(out-of-band marker), and receipted (PLAN→CONSENT→PROVE); deny-by-default beyond "
            "the granted surface. A door admits; it never decides."
        ),
        version=__version__,
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        supported_interfaces=[interface],
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=skills,
    )

    if secured:
        card.security_schemes["bearerAuth"].http_auth_security_scheme.scheme = "bearer"
        req = SecurityRequirement()
        _ = req.schemes["bearerAuth"]  # auto-creates an empty scope list
        card.security_requirements.append(req)

    if signing_key is not None:
        sign_card(card, signing_key, jku=jwks_url)

    return card


def _bearer_middleware_asgi(app, token, rpc_path):
    """Wrap ``app`` so the JSON-RPC route requires ``Authorization: Bearer <token>`` — answered
    as a JSON-RPC error envelope (-32001) BEFORE the SDK sees the request. Discovery routes
    (the card) stay readable pre-auth. No-op wrapper when ``token`` is None (loopback-only)."""
    if token is None:
        return app

    import json

    rpc = "/" + rpc_path.strip("/") if rpc_path.strip("/") else "/"
    body = json.dumps({"jsonrpc": "2.0", "id": None,
                       "error": {"code": -32001, "message": "unauthorized"}}).encode()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "/").rstrip("/") == rpc.rstrip("/"):
            headers = {k.decode().lower(): v.decode()
                       for k, v in scope.get("headers", [])}
            if not _bearer_ok(headers.get("authorization"), token):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
                return
        await app(scope, receive, send)

    return guarded


def build_app(broker, *, rpc_url, token=None, allowed_hosts=None, signing_key=None):
    """Assemble the A2A ASGI app: SDK card + JSON-RPC routes over the executor, the shared
    in-door perimeter (Host allowlist + cross-origin + body cap, :func:`pacioli.webguard.guard_asgi`)
    outside a bearer gate on the RPC path only. Extracted from :func:`serve_a2a` so the
    401/card/refusal paths are testable without binding a socket (the same reason the HTTP door
    has ``_asgi_app``).

    When ``signing_key`` is given the agent card is ES256-signed and a ``GET
    /.well-known/jwks.json`` route serves the public key — BOTH readable pre-auth (discovery must
    precede authentication), like the card, but still under the perimeter's Host check.

    **Defense-in-depth (security redteam 2026-07-16):** re-checks the advertised host itself —
    a public ``rpc_url`` with no ``token`` REFUSES here, not only in :func:`serve_a2a`. This is
    public API an embedder or a ``uvicorn --factory`` path can reach directly, bypassing
    ``serve_a2a``'s own bind check; the refusal must live where the app is actually built, the
    same guard Proximo's ``build_app`` carries. ``serve_a2a`` already refused a public bind
    before reaching here, so on the shipped path this never fires — it is the belt for the
    direct-call path.

    ``allowed_hosts`` is the perimeter's Host allowlist (``None`` → the advertised host +
    loopback forms); the cross-origin/body-cap checks apply to POSTs on the RPC path only, so the
    agent-card GET stays readable pre-auth while the Host check still covers it."""
    from urllib.parse import urlparse

    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes.agent_card_routes import create_agent_card_routes
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
    from a2a.server.tasks import InMemoryTaskStore
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from pacioli.webguard import default_allowed_hosts, guard_asgi

    host = urlparse(rpc_url).hostname
    if _bind_requires_auth(host) and token is None:
        raise TransportConfigError(
            f"advertised host {host!r} is not loopback — refusing to "
            "build an A2A app with no bearer token; a governed door that admits everyone is the "
            "one thing this must never construct")

    jwks_url = None
    if signing_key is not None:
        parts = urlparse(rpc_url)
        jwks_url = f"{parts.scheme}://{parts.netloc}{_JWKS_PATH}"
    card = build_agent_card(rpc_url, secured=token is not None,
                            signing_key=signing_key, jwks_url=jwks_url)
    handler = DefaultRequestHandler(
        agent_executor=make_executor(broker),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    rpc_path = urlparse(rpc_url).path or "/"
    routes = (
        create_jsonrpc_routes(request_handler=handler, rpc_url=rpc_path)
        + create_agent_card_routes(agent_card=card)
    )
    if signing_key is not None:
        # Publish the operator's PUBLIC key so a peer can verify the card seal (the card's jku
        # target). OUTSIDE the bearer gate — like the card, discovery must be readable pre-auth —
        # but still under the perimeter's Host check. Public point only (never the private key).
        _jwks_body = jwks(signing_key)

        async def _serve_jwks(_request):
            return JSONResponse(_jwks_body)

        routes = [*routes, Route(_JWKS_PATH, _serve_jwks, methods=["GET"])]
    gated = _bearer_middleware_asgi(Starlette(routes=routes), token, rpc_path)
    # protect EVERY path, not just the RPC path (security redteam 2026-07-16, Minor): the
    # cross-origin + size checks apply to POSTs only, and the agent card is a GET, so guarding
    # all paths never touches discovery — but a bare ``path == rpc_path`` compare would skip the
    # checks for an un-normalized request target (``/./``, absolute-form) that a future/ fronting
    # router might still route to the RPC handler. Path-independent, exactly like the HTTP door,
    # removes that coincidental-safety gap. (The bearer gate keeps its own RPC-path scoping so the
    # card stays readable pre-auth; only the perimeter widens.)
    return guard_asgi(
        gated,
        allowed_hosts=allowed_hosts or default_allowed_hosts(host),
        protect=lambda p: True)


def serve_a2a(env=None, *, bind="127.0.0.1", port=DEFAULT_PORT, auth=None, allowed_hosts=None):
    """Run the A2A door. Blocks until stopped.

    Deny-biased start-up order, IDENTICAL to the HTTP door's — every refusal happens BEFORE
    anything binds: (1) a non-loopback ``bind`` with no ``auth`` refuses (exit 2); (2) ``auth``
    must be a reference that resolves to a non-empty token; (3) only then are the SDK imports
    attempted and the broker assembled with ``via.transport: "a2a"``. ``allowed_hosts`` (the
    in-door Host allowlist) is threaded to :func:`build_app`; ``None`` → the bind host +
    loopback forms."""
    import os
    if _bind_requires_auth(bind) and auth is None:
        print(f"error: bind {bind!r} is not loopback — refusing to start the A2A transport "
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
        import uvicorn  # noqa: F401 — availability probe before assembling anything
        import a2a  # noqa: F401
    except ImportError:
        print("error: the A2A transport needs the 'a2a-sdk' package and 'uvicorn'. "
              "Install with: pip install 'pacioli[a2a]'", file=sys.stderr)
        return 2

    # Card signing is opt-in: PACIOLI_A2A_SIGNING_KEY_FILE set → load-or-FAIL-LOUD (never serve
    # unsigned when signing was intended); unset → unsigned card. Loaded BEFORE assembling the
    # broker so a bad key path refuses to start rather than serving unsigned.
    signing_key = None
    key_path = (env if env is not None else os.environ).get(SIGNING_KEY_ENV)
    if key_path is not None:
        # PRESENT-but-empty (a broken env interpolation resolving an unset upstream var to "")
        # must FAIL LOUD, not silently serve unsigned — the var being set means signing was
        # intended (security redteam 2026-07-17, Major; same posture as the empty-token refusal
        # in _resolve_transport_token). Absent (None) is the genuine opt-out.
        stripped = key_path.strip()
        if not stripped:
            print(f"error: {SIGNING_KEY_ENV} is set but empty — refusing to serve an unsigned "
                  "card when signing was configured; point it at a key (`pacioli a2a-keygen`) "
                  f"or UNSET {SIGNING_KEY_ENV} to serve unsigned deliberately", file=sys.stderr)
            return 2
        try:
            signing_key = load_signing_key(stripped)
        except (RuntimeError_, ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        broker = assemble(env, via=_a2a_via(auth))
    except RuntimeError_ as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    app = build_app(broker, rpc_url=f"http://{bind}:{port}/", token=token,
                    allowed_hosts=allowed_hosts, signing_key=signing_key)
    card_note = (f"SIGNED (ES256, kid {signing_key.kid[:12]}…); public key at {_JWKS_PATH}"
                 if signing_key else "UNSIGNED — set PACIOLI_A2A_SIGNING_KEY_FILE to sign")
    print(f"pacioli A2A transport on {bind}:{port} "
          f"({'bearer token required' if token else 'loopback, no token'}); "
          f"agent card at /.well-known/agent-card.json ({card_note}); "
          "a standing listener by A2A's nature — TLS is the perimeter's job",
          file=sys.stderr)
    uvicorn.run(app, host=bind, port=port, log_level="warning")
    return 0


def keygen(out_path):
    """Mint a fresh EC **P-256** signing key at ``out_path`` (0600, refuse-to-overwrite) for
    ``PACIOLI_A2A_SIGNING_KEY_FILE`` — a deliberate operator act (the key is never auto-minted on
    serve). Returns the loaded :class:`SigningKey` so the caller can print its ``kid``."""
    import os

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    p = Path(out_path)
    if p.exists():
        raise RuntimeError_(f"refusing to overwrite an existing key at {out_path} — a signing "
                            "key is an identity; move or remove it deliberately first")
    # mode=0o700 so an auto-created parent is never group/world-WRITABLE (a writable dir lets a
    # local user unlink-replace the 0600 key); mkdir's mode only narrows under umask, never
    # widens, so 0o700 is a ceiling regardless of umask (security redteam 2026-07-17). An
    # already-existing parent keeps the operator's own perms (exist_ok).
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    # 0600 from the first byte — never a window where the fresh private key is world-readable.
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        # O_EXCL refuses any existing last component — including a DANGLING symlink that
        # `p.exists()` reported False for. Wrap it so the CLI shows a clean error, not a raw
        # traceback (security redteam 2026-07-17). The safety property (no overwrite, no
        # world-readable window) held regardless; this is UX only.
        raise RuntimeError_(f"refusing to create the key at {out_path}: {type(exc).__name__} "
                            "(a symlink or special file already occupies the path — remove it "
                            "deliberately first)") from exc
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return load_signing_key(str(p))
