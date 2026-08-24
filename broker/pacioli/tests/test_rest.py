# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The REST door: routing, refusals, and the spine underneath.

John, 2026-08-17: *"every door governed"*. This is the fourth door, and it was built after a night
in which three of the author's own claims about the third door were falsified by adversarial
review. The proof shape here is the corrected one from the start rather than retrofitted:

* **Ground truth is** :data:`pacioli.tools.TOOLS`, never the routing tables the door was built
  from. Deriving the expected value from the thing under test is how a length check stayed green
  while every advertised tool name was garbage.
* **Outcomes, never messages.** Status codes and the broker's recorded calls.
* No hand-rolled async harness, so nothing here can hang: this door is plain ASGI and is driven
  directly, no framework, no transport, no lifespan.
"""
import json
import unittest

import anyio

from pacioli.rest import (
    AMEND,
    CANCEL,
    FIXED_ROUTES,
    SUBMIT,
    _rest_via,
    build_rest_app,
    resolve,
)
from pacioli.server import TransportConfigError
from pacioli.tools import TOOLS

_TOKEN = "rest-token-not-a-real-secret"


class _Record:
    """Stands in for the broker. No credential, nothing planned, consented or spent."""

    def __init__(self, answer=None, raises=None):
        self.calls = []
        self._answer = answer if answer is not None else {"ok": True}
        self._raises = raises

    def dispatch(self, name, arguments):
        self.calls.append((name, arguments))
        if self._raises is not None:
            raise self._raises
        return self._answer


def _call(app, method, path, *, body=None, headers=None, host="127.0.0.1", server=None):
    """Drive the ASGI app directly. Returns (status, parsed_json_body_or_raw).

    ``server`` is the ASGI local-socket address. Left unset by default so most tests exercise the
    harness-without-a-socket path; the socket-exposure tests set it explicitly."""
    raw = b"" if body is None else json.dumps(body).encode()
    hdrs = [(b"host", host.encode()), (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode())]
    for k, v in (headers or {}).items():
        hdrs.append((k.lower().encode(), v.encode()))
    scope = {"type": "http", "method": method, "path": path, "headers": hdrs,
             "query_string": b""}
    if server is not None:
        scope["server"] = server
    sent = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    anyio.run(app, scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        return status, json.loads(payload)
    except ValueError:
        return status, payload


class TestTheRoutingTableIsTheCatalog(unittest.TestCase):
    """The door's vocabulary must be the spine's, derived from TOOLS rather than asserted."""

    def test_every_mechanical_tool_in_the_catalog_is_reachable(self):
        """The VALUES half. Note what this does and does not prove, because an adversarial pass
        showed the original version of this test proved less than its name: both sides derive
        from TOOLS with the same predicate, so the tool names are equal by construction and
        replacing _slug() with the identity function left this GREEN. The KEYS are covered by
        the three tests below, with expectations that do not come from the door."""
        want = {t["name"] for t in TOOLS
                if t["name"].split("_")[0] in ("submit", "cancel", "amend")}
        got = set(SUBMIT.values()) | set(CANCEL.values()) | set(AMEND.values())
        self.assertEqual(got, want)

    def test_the_url_slugs_are_hyphenated_not_underscored(self):
        """The KEY computation, checked against the convention rather than against itself.
        _slug() replaced by identity passes every derived-expectation test in this file; it fails
        this one."""
        for table in (SUBMIT, CANCEL, AMEND):
            for slug in table:
                self.assertNotIn("_", slug, f"{slug!r} is not the hyphen convention")

    def test_specific_known_routes_are_hardcoded_here_on_purpose(self):
        """Expectations written by hand, owing nothing to TOOLS or to the door's own tables. If
        the slug convention or the routing changes, this is the test that argues."""
        for path, tool in (
            ("/v1/submit/sales-invoice", "submit_sales_invoice"),
            ("/v1/cancel/journal-entry", "cancel_journal_entry"),
            ("/v1/amend/purchase-order", "amend_purchase_order"),
            ("/v1/submit/stock-reconciliation", "submit_stock_reconciliation"),
        ):
            self.assertEqual(resolve("POST", path), tool)

    def test_no_two_doctypes_collide_on_one_slug(self):
        """A collision would silently drop a doctype from the door while every derived test
        stayed green, because the dict would simply be shorter on both sides."""
        for prefix, table in (("submit", SUBMIT), ("cancel", CANCEL), ("amend", AMEND)):
            in_catalog = [t for t in TOOLS if t["name"].startswith(prefix + "_")]
            self.assertEqual(len(table), len(in_catalog),
                             f"{prefix}: {len(in_catalog)} tools collapsed into {len(table)} slugs")

    def test_every_fixed_route_names_a_real_tool(self):
        names = {t["name"] for t in TOOLS}
        for (method, path), tool in FIXED_ROUTES.items():
            self.assertIn(tool, names, f"{method} {path} routes to a tool that does not exist")

    def test_every_route_resolves_to_the_tool_it_claims(self):
        for (method, path), tool in FIXED_ROUTES.items():
            self.assertEqual(resolve(method, path), tool)
        for verb, table in (("submit", SUBMIT), ("cancel", CANCEL), ("amend", AMEND)):
            for slug, tool in table.items():
                self.assertEqual(resolve("POST", f"/v1/{verb}/{slug}"), tool)

    def test_unroutable_shapes_resolve_to_nothing(self):
        """Each of these would be a door DECIDING something if it fell through to dispatch."""
        for method, path in (
            ("POST", "/v1/submit/sales_invoice"),   # underscore: one URL convention, hyphens
            ("POST", "/v1/submit/no-such-doctype"),
            ("GET", "/v1/submit/sales-invoice"),    # right path, wrong method
            ("POST", "/v1/prove/verify"),           # right path, wrong method
            ("POST", "/v1/submit"),
            ("POST", "/v1/submit/sales-invoice/extra"),
            ("POST", "/v2/submit/sales-invoice"),
            ("POST", "/v1/submit/" + "x" * 200),
            ("POST", "/"),
        ):
            self.assertIsNone(resolve(method, path), f"{method} {path} should not route")

    def test_there_is_no_consent_route_and_no_consent_tool(self):
        """A SECURITY PROPERTY, pinned so nobody adds it as a convenience.

        The marker must be minted by a different principal than the credential it authorises;
        `minted_by` is set server-side from the session. A REST route that minted consent would
        let the broker's own credential authorise itself, which is the exact bypass the guard
        exists to close. The catalog contains no consent tool, so the door cannot grow one by
        accident; this fails loudly if either fact changes."""
        self.assertEqual([t["name"] for t in TOOLS
                          if "consent" in t["name"] or "marker" in t["name"]], [])
        for path in ("/v1/consent", "/v1/consent/PLAN-1", "/v1/marker", "/v1/plan/consent"):
            self.assertIsNone(resolve("POST", path))


class TestTheDoorRefusesToBeBuiltOpen(unittest.TestCase):
    """Checklist point 2, and the lesson the MCP HTTP door learned the hard way on 2026-08-17:
    the refusal lives in the BUILDER, because the builder is public API a `uvicorn --factory`
    path can reach without going through `serve_rest` at all."""

    def test_non_loopback_with_no_token_refuses(self):
        for bind in ("0.0.0.0", "192.168.1.50", "::", "10.0.0.7"):
            with self.assertRaises(TransportConfigError, msg=bind):
                build_rest_app(_Record(), token=None, bind=bind)

    def test_non_loopback_with_an_empty_token_refuses(self):
        """An empty token is not a token. Without this it builds a door whose bearer gate then
        refuses everyone, including the correct caller: a silent lock, not an honest refusal."""
        with self.assertRaises(TransportConfigError):
            build_rest_app(_Record(), token="", bind="0.0.0.0")

    def test_loopback_and_tokened_non_loopback_both_build(self):
        """The refusal tests above would all pass against a builder that refused everything."""
        self.assertIsNotNone(build_rest_app(_Record(), token=None, bind="127.0.0.1"))
        self.assertIsNotNone(build_rest_app(_Record(), token=_TOKEN, bind="0.0.0.0"))


class TestTheDoorCarriesAndDoesNotDecide(unittest.TestCase):
    def test_a_route_reaches_the_spine_with_the_tool_and_args_it_was_given(self):
        broker = _Record({"ok": True, "plan_id": "PLAN-1"})
        app = build_rest_app(broker)
        status, body = _call(app, "POST", "/v1/submit/sales-invoice",
                             body={"name": "SI-1", "consent_token": "tok"})
        self.assertEqual(status, 200)
        self.assertEqual(broker.calls,
                         [("submit_sales_invoice", {"name": "SI-1", "consent_token": "tok"})])
        self.assertEqual(body, {"ok": True, "plan_id": "PLAN-1"})

    def test_a_governed_refusal_is_200_and_not_flattened_into_a_transport_error(self):
        """`ok: False` is the spine's VERDICT. Rendering it as 4xx would let a caller confuse
        'the governance refused you' with 'the door could not route you' — different facts, and
        only one of them means try again with a different URL."""
        broker = _Record({"ok": False, "error": "no recorded plan for this document"})
        status, body = _call(build_rest_app(broker), "POST", "/v1/submit/sales-invoice",
                             body={"name": "SI-1"})
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], False)

    def test_an_unrouted_path_never_reaches_the_broker(self):
        broker = _Record()
        status, _ = _call(build_rest_app(broker), "POST", "/v1/submit/not-a-doctype",
                          body={"name": "X"})
        self.assertEqual(status, 404)
        self.assertEqual(broker.calls, [])

    def test_a_glue_level_exception_names_the_type_only(self):
        """Never the message: it can carry paths or parameters. Copied deliberately from the A2A
        door, where it is a security property rather than a style choice."""
        # The fixture's own path is deliberately NOT under a home directory: the first version of
        # this test used one, and the repo's release leak-audit refused the whole public tree over
        # it. A test proving that messages do not leak paths, leaking a path shape. Any absolute
        # path proves the point equally well.
        secret = "/srv/example/private-path"
        broker = _Record(raises=RuntimeError(f"{secret} exposed in a message"))
        status, body = _call(build_rest_app(broker), "POST", "/v1/reconcile", body={})
        self.assertEqual(status, 500)
        self.assertIn("RuntimeError", body["error"])
        self.assertNotIn(secret, body["error"])
        self.assertNotIn("exposed", body["error"])

    def test_a_body_that_is_not_a_json_object_is_refused_before_dispatch(self):
        broker = _Record()
        app = build_rest_app(broker)
        for bad in ("[]", '"a string"', "17"):
            scope_body = bad.encode()
            status, _ = _call_raw(app, scope_body)
            self.assertEqual(status, 400, bad)
        self.assertEqual(broker.calls, [])

    def test_an_empty_body_is_an_empty_argument_dict(self):
        broker = _Record()
        status, _ = _call(build_rest_app(broker), "GET", "/v1/prove/verify")
        self.assertEqual(status, 200)
        self.assertEqual(broker.calls, [("prove_verify", {})])


def _call_multi(app, header_pairs):
    """Send REPEATED headers, which _call's dict-shaped kwarg cannot express."""
    body = b"{}"
    hdrs = [(b"host", b"127.0.0.1"), (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode())]
    hdrs += [(k.lower().encode(), v.encode()) for k, v in header_pairs]
    scope = {"type": "http", "method": "POST", "path": "/v1/reconcile", "headers": hdrs,
             "query_string": b""}
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    anyio.run(app, scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, payload


def _call_raw(app, raw):
    """Like _call but sends bytes verbatim, for malformed-body cases."""
    scope = {"type": "http", "method": "POST", "path": "/v1/reconcile",
             "headers": [(b"host", b"127.0.0.1"), (b"content-type", b"application/json")],
             "query_string": b""}
    sent = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    anyio.run(app, scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, payload


class TestTheDoorsPerimeter(unittest.TestCase):
    def test_no_bearer_and_wrong_bearer_are_401_and_never_reach_the_broker(self):
        broker = _Record()
        app = build_rest_app(broker, token=_TOKEN)
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": _TOKEN}):
            status, _ = _call(app, "POST", "/v1/reconcile", body={}, headers=headers)
            self.assertEqual(status, 401, headers)
        self.assertEqual(broker.calls, [])

    def test_the_correct_bearer_is_admitted(self):
        """Without this, a gate that refused everything would pass every test above."""
        broker = _Record()
        app = build_rest_app(broker, token=_TOKEN)
        status, _ = _call(app, "POST", "/v1/reconcile", body={},
                          headers={"Authorization": f"Bearer {_TOKEN}"})
        self.assertEqual(status, 200)
        self.assertEqual(len(broker.calls), 1)

    def test_a_host_outside_the_allowlist_is_refused_by_the_shared_perimeter(self):
        broker = _Record()
        app = build_rest_app(broker, token=_TOKEN, allowed_hosts=["127.0.0.1"])
        status, _ = _call(app, "POST", "/v1/reconcile", body={}, host="evil.example")
        self.assertEqual(status, 400)
        self.assertEqual(broker.calls, [])


class TestTheFindingsFromTheSecurityLens(unittest.TestCase):
    """Each of these pins a defect an adversarial pass demonstrated against the first version."""

    def test_a_non_loopback_socket_with_no_token_is_refused_at_request_time(self):
        """HIGH. `bind` is a CLAIM. The lens built a fully-open door by handing the builder
        bind="127.0.0.1" and then running uvicorn on 0.0.0.0: both the token decision and the
        Host allowlist came from the string, and a forged `Host: 127.0.0.1` reached dispatch
        unauthenticated from off-box. scope["server"] is the socket the connection actually
        landed on and cannot be forged by a client."""
        broker = _Record()
        app = build_rest_app(broker, token=None, bind="127.0.0.1")
        status, _ = _call(app, "POST", "/v1/reconcile", body={}, server=("192.0.2.1", 8793))
        self.assertEqual(status, 403)
        self.assertEqual(broker.calls, [])

    def test_a_loopback_socket_with_no_token_is_still_served(self):
        """The refusal above must not become a door that refuses its own default deployment."""
        broker = _Record()
        app = build_rest_app(broker, token=None, bind="127.0.0.1")
        status, _ = _call(app, "POST", "/v1/reconcile", body={}, server=("127.0.0.1", 8793))
        self.assertEqual(status, 200)
        self.assertEqual(len(broker.calls), 1)

    def test_a_token_makes_a_non_loopback_socket_legitimate(self):
        broker = _Record()
        app = build_rest_app(broker, token=_TOKEN, bind="0.0.0.0")
        status, _ = _call(app, "POST", "/v1/reconcile", body={},
                          headers={"Authorization": f"Bearer {_TOKEN}"},
                          server=("192.0.2.1", 8793))
        self.assertEqual(status, 200)

    def test_duplicate_authorization_headers_are_refused_not_last_wins(self):
        """LOW alone, but the deployment guidance is to front this with a reverse proxy, and a
        proxy authorising on the FIRST header while we honoured the LAST is two layers
        disagreeing about which credential a request carried."""
        broker = _Record()
        app = build_rest_app(broker, token=_TOKEN)
        scope_headers = [("Authorization", "Bearer wrong"), ("Authorization", f"Bearer {_TOKEN}")]
        status, _ = _call_multi(app, scope_headers)
        self.assertEqual(status, 400)
        self.assertEqual(broker.calls, [])

    def test_max_body_bytes_actually_reaches_the_perimeter(self):
        """It did not. The builder computed a cap, used it for its own read, and never forwarded
        it to guard_asgi, so the perimeter enforced its own 128 KiB no matter what was asked for.
        It failed SAFE (always stricter), which is exactly why it would never have been noticed."""
        broker = _Record()
        big = {"name": "x" * 150_000}
        refused, _ = _call(build_rest_app(broker), "POST", "/v1/reconcile", body=big)
        self.assertEqual(refused, 413)
        allowed, _ = _call(build_rest_app(broker, max_body_bytes=400 * 1024),
                           "POST", "/v1/reconcile", body=big)
        self.assertEqual(allowed, 200)

    def test_a_unix_socket_is_local_and_a_lan_address_is_not(self):
        """The classifier, over the shapes a real socket actually reports. The first version
        reused `_bind_requires_auth`, which allowlists three literals an OPERATOR types as a bind
        argument -- a different domain -- and would have refused every one of the first four.

        Lives in `pacioli.webguard` since 2026-08-24, shared by all three doors: the HTTP and A2A
        doors were found to have no request-time socket check at all, and three copies of a
        security classifier is how two of them drift."""
        from pacioli.webguard import socket_is_local as _socket_is_local
        for server, local in ((("/run/pacioli.sock", None), True),   # UDS: no address at all
                              (("127.0.0.1", 8793), True),
                              (("127.0.1.1", 8793), True),           # still loopback/8
                              (("::ffff:127.0.0.1", 8793), True),    # dual-stack mapped loopback
                              (("::1", 8793), True),
                              (("192.0.2.1", 8793), False),
                              (("0.0.0.0", 8793), False),
                              (("not-an-address", 8793), False),     # deny-biased
                              ((None, 8793), False),
                              (None, True)):                          # absent: harness case
            self.assertIs(_socket_is_local(server), local, f"{server!r}")

    def test_the_socket_refusal_happens_before_the_perimeter_drains_a_body(self):
        """It did not. guard_asgi drains a POST body IN FULL before calling the app it wraps, so
        the check living inside the app meant a slow POST to a misconfigured door burned the
        perimeter's whole 30s deadline and answered 408 instead of refusing at once. The guard is
        now outside the perimeter, so a receive() that never returns is never even awaited."""
        broker = _Record()
        app = build_rest_app(broker, token=None, bind="127.0.0.1")
        sent = []

        async def never_called():
            raise AssertionError("the body was read before the socket was refused")

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/v1/reconcile",
                 "headers": [(b"host", b"127.0.0.1"), (b"content-type", b"application/json"),
                             (b"content-length", b"50")],
                 "query_string": b"", "server": ("192.0.2.1", 8793)}
        anyio.run(app, scope, never_called, send)
        self.assertEqual(next(m["status"] for m in sent
                              if m["type"] == "http.response.start"), 403)
        self.assertEqual(broker.calls, [])

    def test_a_read_timeout_is_a_408_in_the_doors_own_json_shape(self):
        """The GET-slowloris fix let TimeoutError escape: uvicorn answered plain-text 500,
        breaking this file's every-failure-is-JSON contract, and logged a full traceback at ERROR
        for every legitimately slow client."""
        broker = _Record()
        app = build_rest_app(broker, body_read_timeout=0.2)
        sent = []

        async def never_finishes():
            await anyio.sleep(30)
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": "/v1/prove/verify",
                 "headers": [(b"host", b"127.0.0.1")], "query_string": b""}
        anyio.run(app, scope, never_finishes, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        payload = b"".join(m.get("body", b"") for m in sent
                           if m["type"] == "http.response.body")
        self.assertEqual(status, 408)
        self.assertIs(json.loads(payload)["ok"], False)
        self.assertEqual(broker.calls, [])

    def test_a_stalled_body_read_no_longer_escapes_as_an_exception(self):
        """MEDIUM. The perimeter's size AND timeout protections apply to POST only, so the three
        GET routes reached _read_body with a raw receive and no deadline: a GET declaring a body
        and then going silent was still open after 38s. Unauthenticated on the default config."""
        broker = _Record()
        app = build_rest_app(broker, body_read_timeout=0.2)

        async def never_finishes():
            await anyio.sleep(30)
            return {"type": "http.request", "body": b"", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": "/v1/prove/verify",
                 "headers": [(b"host", b"127.0.0.1")], "query_string": b""}
        anyio.run(app, scope, never_finishes, send)   # must NOT raise past the app
        self.assertEqual(broker.calls, [])


class TestTheLedgerStamp(unittest.TestCase):
    """Checklist point 3: every recorded act says which door and who asked, as labels."""

    def test_the_stamp_names_the_door_and_the_reference_never_the_token(self):
        self.assertEqual(_rest_via(None), {"transport": "rest", "principal": "loopback"})
        self.assertEqual(_rest_via("env:REST_T"),
                         {"transport": "rest", "principal": "env:REST_T"})

    def test_the_stamp_shape_matches_the_other_doors(self):
        from pacioli.a2a import _a2a_via
        from pacioli.server import _http_via
        for via in (_rest_via("env:X"), _http_via("env:X"), _a2a_via("env:X")):
            self.assertEqual(set(via), {"transport", "principal"})


class TestAnEmptyTokenIsNoTokenAtRuntime(unittest.TestCase):
    """``token=""`` must be handled identically to ``token=None`` at BOTH runtime sites, together.

    Found 2026-08-24. ``build_rest_app``'s BUILD check has used ``token in (None, "")`` since the
    door was built, but the two RUNTIME sites inside it stayed narrow:

    * the socket refusal fired only when ``token is None``, so an empty token SKIPPED it;
    * the bearer gate installed only when ``token is not None``, so an empty token INSTALLED one,
      and ``_bearer_ok`` fails closed on an empty configured token, so it then refused everyone.

    The two narrownesses CANCELLED into an accidental fail-closed: 401 to everybody, including a
    caller with the right token. That is exactly why they have to be widened together, and why
    these tests assert the SPECIFIC refusal rather than merely "a refusal". Widening the bearer
    gate alone leaves the socket check asleep and converts the accidental doorstop into a
    genuinely open door on a non-local socket -- the hole the 2026-08-17 security lens found and
    closed. Asserting 403-with-the-socket-refusal reds at 200 on that half-fix; asserting only
    "not 200" would sail straight through it.
    """

    def test_an_empty_token_on_an_exposed_socket_gets_the_socket_refusal(self):
        broker = _Record()
        app = build_rest_app(broker, token="", bind="127.0.0.1")
        status, body = _call(app, "POST", "/v1/reconcile", server=("192.0.2.1", 8793))
        self.assertEqual(status, 403)
        self.assertIn("non-local socket", body["error"])
        self.assertEqual(broker.calls, [])

    def test_a_none_token_on_an_exposed_socket_gets_the_same_refusal(self):
        """The parity control. The point of the fix is that these two answer IDENTICALLY, so the
        test that would still pass if only one of them worked is the one worth writing down."""
        broker = _Record()
        app = build_rest_app(broker, token=None, bind="127.0.0.1")
        status, body = _call(app, "POST", "/v1/reconcile", server=("192.0.2.1", 8793))
        self.assertEqual(status, 403)
        self.assertIn("non-local socket", body["error"])
        self.assertEqual(broker.calls, [])

    def test_an_empty_token_on_a_local_socket_serves_like_no_token(self):
        """The other end. An empty token must not become a silent lock on the dev default either:
        loopback with no token is the documented open-for-development posture, and ``""`` is no
        token. Without this, 'refuse everything' would satisfy the two tests above."""
        broker = _Record()
        app = build_rest_app(broker, token="", bind="127.0.0.1")
        status, _ = _call(app, "POST", "/v1/reconcile", server=("127.0.0.1", 8793))
        self.assertEqual(status, 200)
        self.assertEqual(len(broker.calls), 1)
