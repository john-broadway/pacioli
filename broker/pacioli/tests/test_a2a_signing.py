# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""A2A agent-card signing (docs/plans/2026-07-17-a2a-card-signing.md) — the operator presses an
ES256/JOSE seal onto the card, publishes the public key at a JWKS endpoint, and a peer verifies.

Pacioli's own signing (composition-not-coupling; mechanism mirrors Proximo's SIGNET). These tests
pin the helpers + key custody, no socket. Needs `pacioli[a2a]` (a2a-sdk[signing] + cryptography).
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from pacioli.a2a import (
    build_agent_card,
    jwks,
    load_signing_key,
    public_jwk,
    sign_card,
    verifier_for_jwk,
)


def _write_key(path, curve=ec.SECP256R1(), mode=0o600):
    priv = ec.generate_private_key(curve)
    pem = priv.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    Path(path).write_bytes(pem)
    os.chmod(path, mode)
    return path


class TestLoadSigningKey(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.p = str(Path(self.dir.name) / "sign.key")

    def tearDown(self):
        self.dir.cleanup()

    def test_loads_a_p256_key_and_derives_a_stable_kid(self):
        _write_key(self.p)
        k1 = load_signing_key(self.p)
        k2 = load_signing_key(self.p)
        self.assertTrue(k1.kid)
        self.assertEqual(k1.kid, k2.kid)  # thumbprint is deterministic

    def test_refuses_a_non_p256_key(self):
        _write_key(self.p, curve=ec.SECP384R1())
        with self.assertRaises(ValueError):
            load_signing_key(self.p)

    def test_refuses_a_group_or_world_readable_key(self):
        # the seal-key discipline: a leaked signing key voids the whole assertion.
        _write_key(self.p, mode=0o644)
        with self.assertRaises(Exception) as cm:
            load_signing_key(self.p)
        self.assertIn("600", str(cm.exception))

    def test_refuses_a_missing_key(self):
        with self.assertRaises(Exception):
            load_signing_key(str(Path(self.dir.name) / "nope.key"))

    def test_refuses_a_non_ec_pem(self):
        Path(self.p).write_bytes(b"-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----\n")
        os.chmod(self.p, 0o600)
        with self.assertRaises(ValueError):
            load_signing_key(self.p)


class TestSignAndVerify(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.pa = _write_key(str(Path(self.dir.name) / "a.key"))
        self.pb = _write_key(str(Path(self.dir.name) / "b.key"))
        self.key_a = load_signing_key(self.pa)
        self.key_b = load_signing_key(self.pb)

    def tearDown(self):
        self.dir.cleanup()

    def _card(self, key, jku="http://127.0.0.1:8792/.well-known/jwks.json"):
        card = build_agent_card("http://127.0.0.1:8792/", secured=True,
                                signing_key=key, jwks_url=jku)
        return card

    def test_signed_card_verifies_under_the_matching_pinned_key(self):
        card = self._card(self.key_a)
        verify = verifier_for_jwk(public_jwk(self.key_a))
        verify(card)  # must not raise

    def test_a_card_signed_by_a_wrong_key_is_refused(self):
        card = self._card(self.key_a)
        verify = verifier_for_jwk(public_jwk(self.key_b))  # pinned to B, card signed by A
        with self.assertRaises(Exception):
            verify(card)

    def test_seal_is_es256_not_the_sdk_hs256_default(self):
        # the algorithm-confusion pin: the seal must be asymmetric ES256, never HS256.
        card = self._card(self.key_a)
        sig = card.signatures[0]
        import base64
        import json
        header = json.loads(base64.urlsafe_b64decode(
            sig.protected + "=" * (-len(sig.protected) % 4)))
        self.assertEqual(header["alg"], "ES256")

    def test_jku_is_set_on_the_signed_card(self):
        card = self._card(self.key_a, jku="http://host/.well-known/jwks.json")
        import base64
        import json
        header = json.loads(base64.urlsafe_b64decode(
            card.signatures[0].protected + "=" * (-len(card.signatures[0].protected) % 4)))
        self.assertEqual(header["jku"], "http://host/.well-known/jwks.json")

    def test_unsigned_card_has_no_signature(self):
        card = build_agent_card("http://127.0.0.1:8792/", secured=True)  # no signing_key
        self.assertFalse(getattr(card, "signatures", None))


class TestJwksExposesPublicOnly(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.key = load_signing_key(_write_key(str(Path(self.dir.name) / "k.key")))

    def tearDown(self):
        self.dir.cleanup()

    def test_public_jwk_carries_no_private_scalar(self):
        j = public_jwk(self.key)
        self.assertEqual(j["kty"], "EC")
        self.assertEqual(j["crv"], "P-256")
        self.assertEqual(j["alg"], "ES256")
        self.assertNotIn("d", j)  # the private scalar must NEVER appear
        self.assertEqual(j["kid"], self.key.kid)

    def test_jwks_is_a_key_set_of_public_keys(self):
        js = jwks(self.key)
        self.assertIn("keys", js)
        self.assertEqual(len(js["keys"]), 1)
        self.assertNotIn("d", js["keys"][0])


class TestBuildAppSigning(unittest.TestCase):
    """The wiring: a signing key makes build_app serve a signed card + a pre-auth JWKS route."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(
            '[targets.prod]\nbase_url = "https://erp.example.com"\n'
            'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n')
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}
        self.key = load_signing_key(_write_key(str(d / "sign.key")))

    def tearDown(self):
        self.dir.cleanup()

    def _app(self, signing_key):
        from pacioli.a2a import _a2a_via, build_app
        from pacioli.runtime import assemble
        broker = assemble(self.env, via=_a2a_via(None))
        return build_app(broker, rpc_url="http://127.0.0.1:8792/", token=None,
                         signing_key=signing_key)

    def _get(self, app, path):
        import asyncio
        import httpx

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8792") as hx:
                return await hx.get(path, headers={"Host": "127.0.0.1:8792"})
        return asyncio.run(go())

    def test_jwks_served_pre_auth_public_only(self):
        r = self._get(self._app(self.key), "/.well-known/jwks.json")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["keys"]), 1)
        self.assertEqual(body["keys"][0]["kid"], self.key.kid)
        self.assertNotIn("d", body["keys"][0])  # never the private scalar over the wire

    def test_served_card_is_signed_and_verifies(self):
        import asyncio
        import httpx
        from a2a.client import A2ACardResolver
        app = self._app(self.key)

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8792") as hx:
                return await A2ACardResolver(hx, "http://127.0.0.1:8792").get_agent_card()
        card = asyncio.run(go())
        self.assertTrue(card.signatures)
        verifier_for_jwk(public_jwk(self.key))(card)  # the served card verifies

    def test_no_jwks_route_when_unsigned(self):
        r = self._get(self._app(None), "/.well-known/jwks.json")
        self.assertEqual(r.status_code, 404)


class TestServeA2aSigningFailLoud(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(
            '[targets.prod]\nbase_url = "https://erp.example.com"\n'
            'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n')
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}
        self.d = d

    def tearDown(self):
        self.dir.cleanup()

    def _serve(self):
        # uvicorn.run BLOCKS forever — mock it so a serve that PROCEEDS (the unsigned/opt-out
        # path, or a fail-open bug) returns instead of hanging the test. A config refusal returns
        # exit 2 before ever reaching it, so mocking never masks a refusal.
        import io
        from contextlib import redirect_stderr
        from unittest import mock
        from pacioli.a2a import serve_a2a
        e = io.StringIO()
        with redirect_stderr(e), mock.patch("uvicorn.run", return_value=None):
            rc = serve_a2a(self.env, bind="127.0.0.1")
        return rc, e.getvalue()

    def test_set_but_missing_key_fails_loud(self):
        self.env["PACIOLI_A2A_SIGNING_KEY_FILE"] = str(self.d / "nope.key")
        rc, err = self._serve()
        self.assertEqual(rc, 2)
        self.assertIn("does not exist", err)

    def test_set_but_exposed_key_fails_loud(self):
        p = _write_key(str(self.d / "exposed.key"), mode=0o644)
        self.env["PACIOLI_A2A_SIGNING_KEY_FILE"] = p
        rc, err = self._serve()
        self.assertEqual(rc, 2)
        self.assertIn("600", err)

    def test_present_but_empty_key_path_fails_loud_not_unsigned(self):
        # security redteam 2026-07-17 (Major): an empty string means the var was SET (broken env
        # interpolation), which must FAIL LOUD — never silently serve unsigned when signing was
        # configured. Same posture as _resolve_transport_token's empty-value refusal.
        for empty in ("", "   ", "\t"):
            self.env["PACIOLI_A2A_SIGNING_KEY_FILE"] = empty
            rc, err = self._serve()
            self.assertEqual(rc, 2, f"empty={empty!r} should fail loud")
            self.assertIn("empty", err.lower())

    def test_absent_key_env_serves_unsigned_no_crash(self):
        # contrast: a genuinely ABSENT var (not in env) is the opt-out — unsigned, no error.
        self.env.pop("PACIOLI_A2A_SIGNING_KEY_FILE", None)
        rc, err = self._serve()
        self.assertNotEqual(rc, 2)  # reaches uvicorn (mocked) — not a config refusal

    def test_directory_at_key_path_fails_loud_not_a_traceback(self):
        # security redteam 2026-07-17 (Minor): a directory (0700 passes the mode check) must be a
        # clean exit-2, never a raw IsADirectoryError traceback.
        import os
        d = self.d / "keydir"
        d.mkdir(mode=0o700)
        self.env["PACIOLI_A2A_SIGNING_KEY_FILE"] = str(d)
        rc, err = self._serve()
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)


class TestKeygen(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_keygen_mints_a_loadable_0600_key(self):
        from pacioli.a2a import keygen
        p = str(Path(self.dir.name) / "new.key")
        key = keygen(p)
        self.assertTrue(key.kid)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        load_signing_key(p)  # round-trips through the loader (P-256, 0600)

    def test_keygen_refuses_to_overwrite(self):
        from pacioli.a2a import keygen
        from pacioli.runtime import RuntimeError_
        p = str(Path(self.dir.name) / "k.key")
        keygen(p)
        with self.assertRaises(RuntimeError_):
            keygen(p)

    def test_keygen_dangling_symlink_is_clean_runtimeerror(self):
        # security redteam 2026-07-17 (Minor): a dangling symlink at the target (exists()=False,
        # but O_EXCL still refuses) must raise a wrapped RuntimeError_, not a raw FileExistsError.
        from pacioli.a2a import keygen
        from pacioli.runtime import RuntimeError_
        link = Path(self.dir.name) / "dangling.key"
        os.symlink(str(Path(self.dir.name) / "nonexistent-target"), str(link))
        with self.assertRaises(RuntimeError_):
            keygen(str(link))

    def test_keygen_creates_parent_dir_not_world_writable(self):
        # security redteam 2026-07-17 (Minor): an auto-created parent dir must not be world-
        # writable (a world-writable dir lets a local user unlink-replace the 0600 key).
        old_umask = os.umask(0)  # most permissive umask — the worst case
        try:
            from pacioli.a2a import keygen
            deep = Path(self.dir.name) / "made" / "here"
            keygen(str(deep / "k.key"))
            mode = stat.S_IMODE(os.stat(deep).st_mode)
            self.assertEqual(mode & 0o022, 0, f"parent dir {oct(mode)} is group/world-writable")
        finally:
            os.umask(old_umask)


if __name__ == "__main__":
    unittest.main()
