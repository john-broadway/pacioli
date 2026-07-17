# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""The A2A door (docs/plans/2026-07-16-a2a-door.md) — the first non-MCP door.

Four layers, cheapest first: the pure pieces (via stamp, wire-convention parser) with no SDK at
all; the card against the real a2a-sdk; the serve refusal table (the SAME deny-biased startup
the HTTP door pins); and the full in-process end-to-end — a REAL a2a-sdk client resolving the
card and sending a real message through JSON-RPC → executor → dispatch_raw → the spine, over
httpx's ASGITransport (no socket). The e2e proves the door claim: a forged submit is refused at
``stage: plan`` OVER A2A, byte-for-byte the same deny an MCP client gets.

Needs the ``a2a-sdk`` (dev venv; ``pip install 'pacioli[a2a]'``) — these tests are the door's
own suite, not the pure cores'.
"""
import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

from pacioli.a2a import DEFAULT_PORT, _a2a_via, _parse_tool_call, build_agent_card, serve_a2a
from pacioli.tools import TOOLS

REG = '[targets.prod]\nbase_url = "https://erp.example.com"\n' \
      'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n'


class TestA2aVia(unittest.TestCase):
    def test_token_reference_is_the_principal(self):
        self.assertEqual(_a2a_via("env:A2A_T"),
                         {"transport": "a2a", "principal": "env:A2A_T"})

    def test_no_token_is_loopback_principal(self):
        self.assertEqual(_a2a_via(None),
                         {"transport": "a2a", "principal": "loopback"})

    def test_port_is_adjacent_to_the_http_door(self):
        self.assertEqual(DEFAULT_PORT, 8792)


class TestParseToolCall(unittest.TestCase):
    """The wire convention, SDK-free: a fake extractor stands in for get_data_parts."""

    @staticmethod
    def _msg(*payloads):
        return SimpleNamespace(parts=list(payloads))

    @staticmethod
    def _extract(parts):
        return parts

    def test_tool_and_params(self):
        name, params = _parse_tool_call(
            self._msg({"tool": "plan_submit", "params": {"doctype": "Sales Invoice"}}),
            self._extract)
        self.assertEqual(name, "plan_submit")
        self.assertEqual(params, {"doctype": "Sales Invoice"})

    def test_skill_alias(self):
        name, params = _parse_tool_call(self._msg({"skill": "prove_verify"}), self._extract)
        self.assertEqual(name, "prove_verify")
        self.assertEqual(params, {})

    def test_tool_wins_over_skill_when_both_present(self):
        name, _ = _parse_tool_call(
            self._msg({"tool": "a", "skill": "b"}), self._extract)
        self.assertEqual(name, "a")

    def test_first_matching_part_wins(self):
        name, _ = _parse_tool_call(
            self._msg({"note": "not a call"}, {"tool": "first"}, {"tool": "second"}),
            self._extract)
        self.assertEqual(name, "first")

    def test_no_matching_part_is_none(self):
        name, params = _parse_tool_call(self._msg({"note": "nope"}), self._extract)
        self.assertIsNone(name)
        self.assertIsNone(params)

    def test_none_message_is_none(self):
        self.assertEqual(_parse_tool_call(None, self._extract), (None, None))

    def test_non_dict_params_becomes_empty(self):
        # a malformed params is left for dispatch's own schema validation to refuse LOUDLY
        # with the tool named — the parser never invents a shape.
        _, params = _parse_tool_call(
            self._msg({"tool": "plan_submit", "params": ["not", "a", "dict"]}), self._extract)
        self.assertEqual(params, {})

    def test_non_dict_payload_is_skipped(self):
        name, _ = _parse_tool_call(
            self._msg("just text", {"tool": "x"}), self._extract)
        self.assertEqual(name, "x")


class TestBuildAgentCard(unittest.TestCase):
    def test_full_surface_one_skill_per_tool(self):
        card = build_agent_card("http://127.0.0.1:8792/")
        self.assertEqual(len(card.skills), len(TOOLS))
        self.assertEqual({s.id for s in card.skills}, {t["name"] for t in TOOLS})

    def test_card_carries_the_broker_version(self):
        import pacioli
        card = build_agent_card("http://127.0.0.1:8792/")
        self.assertEqual(card.version, pacioli.__version__)

    def test_secured_card_declares_bearer(self):
        card = build_agent_card("http://127.0.0.1:8792/", secured=True)
        self.assertIn("bearerAuth", card.security_schemes)
        self.assertEqual(
            card.security_schemes["bearerAuth"].http_auth_security_scheme.scheme, "bearer")
        self.assertTrue(card.security_requirements)

    def test_unsecured_card_declares_nothing(self):
        card = build_agent_card("http://127.0.0.1:8792/")
        self.assertFalse(card.security_requirements)

    def test_skill_tags_carry_the_family_prefix(self):
        card = build_agent_card("http://127.0.0.1:8792/")
        by_id = {s.id: s for s in card.skills}
        self.assertIn("plan", by_id["plan_submit"].tags)


class TestServeA2aRefusals(unittest.TestCase):
    """The deny-biased startup table — the SAME refusals the HTTP door pins, applied to this
    door's entry point. Every refusal must happen BEFORE any bind/import/assembly."""

    def test_non_loopback_bind_without_auth_refuses_to_start(self):
        e = io.StringIO()
        with redirect_stderr(e):
            rc = serve_a2a({}, bind="0.0.0.0", port=DEFAULT_PORT, auth=None)
        self.assertEqual(rc, 2)
        self.assertIn("token", e.getvalue().lower())

    def test_inline_auth_refuses_to_start(self):
        e = io.StringIO()
        with redirect_stderr(e):
            rc = serve_a2a({}, bind="127.0.0.1", port=DEFAULT_PORT, auth="inline-literal")
        self.assertEqual(rc, 2)
        self.assertNotIn("inline-literal", e.getvalue())  # never echo the offending value

    def test_empty_env_token_refuses_to_start(self):
        e = io.StringIO()
        with redirect_stderr(e):
            rc = serve_a2a({"A2A_T": "   "}, bind="127.0.0.1", auth="env:A2A_T")
        self.assertEqual(rc, 2)
        self.assertIn("empty", e.getvalue().lower())

    def test_unclassifiable_bind_reads_as_exposed(self):
        e = io.StringIO()
        with redirect_stderr(e):
            rc = serve_a2a({}, bind="", auth=None)
        self.assertEqual(rc, 2)


class _A2aAppFixture(unittest.TestCase):
    """A real broker (registry + on-disk store, no bench) behind the real A2A app, driven
    in-process over httpx ASGITransport — no socket, full JSON-RPC stack."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _app(self, token=None):
        from pacioli.a2a import _a2a_via, build_app
        from pacioli.runtime import assemble
        broker = assemble(self.env, via=_a2a_via("env:A2A_T" if token else None))
        return build_app(broker, rpc_url="http://127.0.0.1:8792/", token=token)

    def _get(self, app, path, headers=None):
        import httpx

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8792") as hx:
                return await hx.get(path, headers=headers or {})
        return asyncio.run(go())

    def _post(self, app, path, body, headers=None):
        import httpx

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8792") as hx:
                return await hx.post(path, json=body, headers=headers or {})
        return asyncio.run(go())


class TestA2aPerimeter(_A2aAppFixture):
    RPC_BODY = {"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {}}

    def test_card_readable_without_auth_even_when_token_set(self):
        app = self._app(token="s3cret")
        r = self._get(app, "/.well-known/agent-card.json")
        self.assertEqual(r.status_code, 200)
        card = r.json()
        self.assertEqual(card["name"], "Pacioli")

    def test_rpc_without_bearer_is_401_when_token_set(self):
        app = self._app(token="s3cret")
        r = self._post(app, "/", self.RPC_BODY)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], -32001)

    def test_rpc_with_wrong_bearer_is_401(self):
        app = self._app(token="s3cret")
        r = self._post(app, "/", self.RPC_BODY,
                       headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_rpc_with_correct_bearer_reaches_the_sdk(self):
        app = self._app(token="s3cret")
        r = self._post(app, "/", self.RPC_BODY,
                       headers={"Authorization": "Bearer s3cret"})
        self.assertNotEqual(r.status_code, 401)  # past the gate; SDK owns the rest

    def test_build_app_public_url_without_token_refuses(self):
        # DEFENSE-IN-DEPTH (security redteam 2026-07-16, Major): build_app is public API an
        # embedder / a uvicorn --factory path can reach directly, bypassing serve_a2a's own
        # bind check. It must refuse a public advertised URL with no token ITSELF — the exact
        # guard Proximo's build_app carries. Without this, build_app(rpc_url=public, token=None)
        # constructs a fully-open governed door.
        from pacioli.a2a import build_app
        from pacioli.runtime import assemble
        from pacioli.server import TransportConfigError
        broker = assemble(self.env, via={"transport": "a2a", "principal": "loopback"})
        for public in ("http://0.0.0.0:8792/", "http://192.168.1.50:8792/",
                       "http://[::]:8792/"):
            with self.assertRaises(TransportConfigError, msg=public):
                build_app(broker, rpc_url=public, token=None)

    def test_build_app_public_url_with_token_is_allowed(self):
        from pacioli.a2a import build_app
        from pacioli.runtime import assemble
        broker = assemble(self.env, via={"transport": "a2a", "principal": "env:A2A_T"})
        app = build_app(broker, rpc_url="http://192.168.1.50:8792/", token="s3cret")
        self.assertIsNotNone(app)

    def test_build_app_loopback_without_token_still_builds(self):
        # no regression: the dev default (loopback, no token) must keep working.
        from pacioli.a2a import build_app
        from pacioli.runtime import assemble
        broker = assemble(self.env, via={"transport": "a2a", "principal": "loopback"})
        self.assertIsNotNone(build_app(broker, rpc_url="http://127.0.0.1:8792/", token=None))

    def test_loopback_no_token_rpc_is_open(self):
        app = self._app(token=None)
        r = self._post(app, "/", self.RPC_BODY)
        self.assertNotEqual(r.status_code, 401)


class TestA2aDoorPerimeter(_A2aAppFixture):
    """The shared in-door perimeter (webguard) reaches through the REAL A2A build_app —
    distinct from test_webguard.py's unit proofs. Driven over httpx ASGITransport."""

    RPC_BODY = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {}}

    def _post_host(self, app, host, path="/", extra=None):
        import httpx

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8792") as hx:
                headers = {"Host": host, "Content-Type": "application/json"}
                if extra:
                    headers.update(extra)
                return await hx.post(path, json=self.RPC_BODY, headers=headers)
        return asyncio.run(go())

    def test_bad_host_is_400_through_the_door(self):
        r = self._post_host(self._app(), "evil.example.com")
        self.assertEqual(r.status_code, 400)

    def test_loopback_host_passes_the_perimeter(self):
        r = self._post_host(self._app(), "127.0.0.1:8792")
        self.assertNotEqual(r.status_code, 400)  # past the Host guard

    def test_cross_origin_post_is_403_through_the_door(self):
        r = self._post_host(self._app(), "127.0.0.1:8792",
                            extra={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)

    def test_card_get_stays_reachable_with_good_host(self):
        import httpx

        app = self._app()

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8792") as hx:
                return await hx.get("/.well-known/agent-card.json",
                                    headers={"Host": "127.0.0.1:8792"})
        r = asyncio.run(go())
        self.assertEqual(r.status_code, 200)

    def test_card_get_bad_host_is_400(self):
        # the Host guard covers the card route too (uniform), even though cross-origin/size
        # do not touch a GET.
        import httpx

        app = self._app()

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8792") as hx:
                return await hx.get("/.well-known/agent-card.json",
                                    headers={"Host": "evil.example.com"})
        r = asyncio.run(go())
        self.assertEqual(r.status_code, 400)

    def test_allowed_hosts_param_widens_the_allowlist(self):
        from pacioli.a2a import _a2a_via, build_app
        from pacioli.runtime import assemble
        broker = assemble(self.env, via=_a2a_via(None))
        app = build_app(broker, rpc_url="http://127.0.0.1:8792/", token=None,
                        allowed_hosts=["proxy.internal", "127.0.0.1"])
        r = self._post_host(app, "proxy.internal")
        self.assertNotEqual(r.status_code, 400)


class TestA2aEndToEnd(_A2aAppFixture):
    """The door claim, proven with the OFFICIAL a2a-sdk client: resolve the card, send a real
    message, read the task back — client → JSON-RPC → executor → dispatch_raw → spine."""

    def _send(self, app, payload):
        import httpx
        from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
        from a2a.helpers.proto_helpers import get_data_parts, new_data_message
        from a2a.types.a2a_pb2 import SendMessageRequest, TaskState

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8792",
                                         timeout=20) as hx:
                card = await A2ACardResolver(hx, "http://127.0.0.1:8792").get_agent_card()
                client = ClientFactory(
                    ClientConfig(httpx_client=hx, streaming=False, polling=True)).create(card)
                req = SendMessageRequest(message=new_data_message(payload))
                task, artifacts, final_state = None, [], None
                async for resp in client.send_message(req):
                    if resp.HasField("task"):
                        task = resp.task
                    if resp.HasField("artifact_update"):
                        artifacts.append(resp.artifact_update.artifact)
                    if resp.HasField("status_update"):
                        final_state = resp.status_update.status.state
                if task is not None:
                    artifacts = list(task.artifacts) + artifacts
                    if final_state is None:
                        final_state = task.status.state
                datas = []
                for a in artifacts:
                    if a.name == "result":
                        datas.extend(get_data_parts(a.parts))
                return final_state, datas, TaskState
        return asyncio.run(go())

    def test_card_resolves_with_the_full_surface(self):
        import httpx
        from a2a.client import A2ACardResolver

        app = self._app()

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8792") as hx:
                return await A2ACardResolver(hx, "http://127.0.0.1:8792").get_agent_card()
        card = asyncio.run(go())
        self.assertEqual(card.name, "Pacioli")
        self.assertEqual(len(card.skills), len(TOOLS))

    def test_prove_verify_completes_over_a2a(self):
        # an offline governed READ against the real store — the happy path over the wire.
        app = self._app()
        state, datas, TaskState = self._send(app, {"tool": "prove_verify", "params": {}})
        self.assertEqual(state, TaskState.TASK_STATE_COMPLETED)
        self.assertTrue(datas and isinstance(datas[0], dict))
        self.assertIn("ok", datas[0])

    def test_forged_submit_refused_at_stage_plan_over_a2a(self):
        # THE door claim: the same structured deny an MCP client gets, over a protocol the
        # spine has never seen. The task COMPLETES (a governed refusal is an answer, not a
        # transport failure) and the artifact carries ok:False at the plan stage.
        app = self._app()
        state, datas, TaskState = self._send(
            app, {"tool": "submit_sales_invoice",
                  "params": {"name": "SINV-FORGED", "plan_token": "forged",
                             "marker": "m-forged"}})
        self.assertEqual(state, TaskState.TASK_STATE_COMPLETED)
        self.assertTrue(datas)
        body = datas[0]
        self.assertFalse(body["ok"])
        self.assertEqual(body.get("stage"), "plan")

    def test_unknown_tool_is_a_structured_deny_not_a_transport_error(self):
        app = self._app()
        state, datas, TaskState = self._send(app, {"tool": "drop_all_tables", "params": {}})
        self.assertEqual(state, TaskState.TASK_STATE_COMPLETED)
        self.assertTrue(datas)
        self.assertFalse(datas[0]["ok"])
        self.assertEqual(datas[0].get("stage"), "request")

    def test_message_without_tool_part_fails_the_task_cleanly(self):
        from a2a.helpers.proto_helpers import new_text_message  # a message with NO DataPart
        import httpx
        from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
        from a2a.types.a2a_pb2 import SendMessageRequest, TaskState

        app = self._app()

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8792",
                                         timeout=20) as hx:
                card = await A2ACardResolver(hx, "http://127.0.0.1:8792").get_agent_card()
                client = ClientFactory(
                    ClientConfig(httpx_client=hx, streaming=False, polling=True)).create(card)
                req = SendMessageRequest(message=new_text_message("hello, no tool here"))
                final_state = None
                async for resp in client.send_message(req):
                    if resp.HasField("status_update"):
                        final_state = resp.status_update.status.state
                    elif resp.HasField("task") and final_state is None:
                        # the Task event carries the SUBMITTED state; never let it
                        # overwrite a later terminal status_update
                        final_state = resp.task.status.state
                return final_state, TaskState
        state, TaskState_ = asyncio.run(go())
        self.assertEqual(state, TaskState_.TASK_STATE_FAILED)

    # NOTE (disclosed residual, inherited from 0.25.0 F3): a live-bench committed receipt
    # carrying via.transport="a2a" over the real wire is a LAB PIN (the same one staged for
    # stdio/http) — an act's intent receipt is only written on a write path, which needs a
    # reachable bench. The seam is pinned below instead, exactly as 0.25.0 pinned the other
    # two doors: the stamp threads assembly → store and rides every intent.


class TestA2aViaSeam(unittest.TestCase):
    """The a2a stamp through the SAME seam the other doors are pinned at (test_via.py)."""

    def test_a2a_via_stamped_into_every_intent_at_the_seam(self):
        from pacioli.tests.test_store import _store as _mem_store
        store = _mem_store()
        store.set_via(_a2a_via("env:A2A_T"))
        r = store.record_intent({"tool": "submit", "target": "t", "docname": "SI-1"})
        self.assertEqual(r.body["via"], {"transport": "a2a", "principal": "env:A2A_T"})

    def test_assemble_threads_the_a2a_stamp_into_opened_stores(self):
        import tempfile as _tf
        from pathlib import Path as _P

        from pacioli.runtime import assemble
        with _tf.TemporaryDirectory() as d:
            (_P(d) / "targets.toml").write_text(REG)
            env = {"PACIOLI_REGISTRY": str(_P(d) / "targets.toml"),
                   "PACIOLI_STATE_DIR": d, "K": "kk", "S": "ss"}
            broker = assemble(env, via=_a2a_via(None))
            store = broker._store("prod")
            r = store.record_intent({"tool": "probe"})
            self.assertEqual(r.body["via"],
                             {"transport": "a2a", "principal": "loopback"})


class TestCliA2aSurface(unittest.TestCase):
    """``pacioli serve --a2a`` — parser + routing, mirroring the HTTP door's CLI pins."""

    def test_serve_parses_a2a_flags_and_default_is_stdio(self):
        from pacioli.cli import build_parser
        p = build_parser()
        a = p.parse_args(["serve"])
        self.assertFalse(getattr(a, "a2a", False))
        a = p.parse_args(["serve", "--a2a", "--bind", "127.0.0.1", "--port", "8792",
                          "--auth", "env:A2A_T"])
        self.assertTrue(a.a2a)
        self.assertEqual((a.bind, a.port, a.auth), ("127.0.0.1", 8792, "env:A2A_T"))

    def test_http_and_a2a_together_is_a_usage_error(self):
        from pacioli.cli import build_parser
        p = build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["serve", "--http", "--a2a"])

    def test_port_defaults_per_door(self):
        # one --port flag, two doors: unset resolves to each door's own default
        # (http 8791, a2a 8792), never one door's default leaking into the other.
        from pacioli.cli import build_parser
        p = build_parser()
        self.assertIsNone(p.parse_args(["serve", "--http"]).port)
        self.assertIsNone(p.parse_args(["serve", "--a2a"]).port)

    def test_main_routes_a2a_to_serve_a2a(self):
        from unittest import mock

        from pacioli import cli as cli_mod
        with mock.patch("pacioli.a2a.serve_a2a", return_value=0) as sa:
            rc = cli_mod.main(["serve", "--a2a", "--auth", "env:A2A_T"], env={"A2A_T": "t"})
        self.assertEqual(rc, 0)
        sa.assert_called_once()
        kwargs = sa.call_args.kwargs
        self.assertEqual(kwargs["bind"], "127.0.0.1")
        self.assertEqual(kwargs["port"], 8792)
        self.assertEqual(kwargs["auth"], "env:A2A_T")

    def test_allowed_hosts_flag_parses_and_threads(self):
        from unittest import mock

        from pacioli import cli as cli_mod
        p = cli_mod.build_parser()
        a = p.parse_args(["serve", "--a2a", "--allowed-hosts", "proxy.internal, 127.0.0.1"])
        self.assertEqual(a.allowed_hosts, "proxy.internal, 127.0.0.1")
        with mock.patch("pacioli.a2a.serve_a2a", return_value=0) as sa:
            cli_mod.main(["serve", "--a2a", "--auth", "env:A2A_T",
                          "--allowed-hosts", "proxy.internal,127.0.0.1"], env={"A2A_T": "t"})
        self.assertEqual(sa.call_args.kwargs["allowed_hosts"],
                         ["proxy.internal", "127.0.0.1"])

    def test_allowed_hosts_absent_threads_none(self):
        from unittest import mock

        from pacioli import cli as cli_mod
        with mock.patch("pacioli.server.serve_http", return_value=0) as sh:
            cli_mod.main(["serve", "--http"], env={})
        self.assertIsNone(sh.call_args.kwargs["allowed_hosts"])

    def test_main_routes_http_with_its_own_default_port(self):
        from unittest import mock

        from pacioli import cli as cli_mod
        with mock.patch("pacioli.server.serve_http", return_value=0) as sh:
            rc = cli_mod.main(["serve", "--http"], env={})
        self.assertEqual(rc, 0)
        self.assertEqual(sh.call_args.kwargs["port"], 8791)


if __name__ == "__main__":
    unittest.main()
