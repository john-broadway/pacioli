# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Runtime/CLI tests — config assembly, seal-key handling, and the human mint path."""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pacioli.runtime import (RuntimeError_, assemble, load_or_create_seal_key, open_store,
                            state_db_path)
from pacioli.cli import cmd_mint, cmd_verify


REG = '[targets.prod]\nbase_url = "https://erp.example.com"\n' \
      'api_key = "env:K"\napi_secret = "env:S"\ndefault = true\n'


class TestSealKey(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "seal.key"

    def tearDown(self):
        self.dir.cleanup()

    def test_creates_key_with_0600_and_reloads_same(self):
        k1 = load_or_create_seal_key(self.path)
        self.assertEqual(len(k1), 32)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")
        k2 = load_or_create_seal_key(self.path)
        self.assertEqual(k1, k2)

    def test_group_or_world_readable_key_refused(self):
        load_or_create_seal_key(self.path)
        os.chmod(self.path, 0o644)
        with self.assertRaises(RuntimeError_) as ctx:
            load_or_create_seal_key(self.path)
        self.assertIn("permission", str(ctx.exception).lower())

    def test_short_key_refused(self):
        self.path.write_bytes(b"short")
        os.chmod(self.path, 0o600)
        with self.assertRaises(RuntimeError_):
            load_or_create_seal_key(self.path)


class TestStatePaths(unittest.TestCase):
    def test_db_per_target(self):
        self.assertNotEqual(state_db_path("/s", "prod"), state_db_path("/s", "staging"))

    def test_target_name_is_sanitised_for_the_filesystem(self):
        p = str(state_db_path("/s", "../../etc/passwd"))
        self.assertNotIn("..", p)
        self.assertTrue(p.startswith("/s/"))


class TestAssemble(unittest.TestCase):
    def test_assemble_builds_a_working_broker(self):
        with tempfile.TemporaryDirectory() as d:
            reg = Path(d) / "targets.toml"
            reg.write_text(REG)
            env = {"PACIOLI_REGISTRY": str(reg), "PACIOLI_STATE_DIR": d,
                   "K": "kk", "S": "ss"}
            broker = assemble(env)
            out = broker.dispatch("prove_orphans", {})
            self.assertTrue(out["ok"])

    def test_missing_registry_is_a_clear_error(self):
        with self.assertRaises(RuntimeError_) as ctx:
            assemble({"PACIOLI_REGISTRY": "/nonexistent/targets.toml"})
        self.assertIn("targets.toml", str(ctx.exception))


class TestMintCli(unittest.TestCase):
    """The human's side of CONSENT: keyless store, high-entropy machine-minted token, printed once."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "targets.toml").write_text(REG)
        self.env = {"PACIOLI_REGISTRY": str(d / "targets.toml"),
                    "PACIOLI_STATE_DIR": str(d), "K": "kk", "S": "ss"}

    def tearDown(self):
        self.dir.cleanup()

    def _mint(self, plan_id="p1", ttl=900):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cmd_mint(self.env, plan_id=plan_id, target=None, ttl=ttl)
        return rc, out.getvalue() + err.getvalue()

    def test_mint_prints_a_high_entropy_token_once_and_stores_only_the_hash(self):
        # A plan must exist first — consent binds to a recorded plan, not a free-typed id.
        store = open_store(self.env, "prod")
        from pacioli.plan import new_plan
        store.record_plan(new_plan("p1", "prod", "v1", "2026-07-01", docname="SI-1"))

        rc, out = self._mint()
        self.assertEqual(rc, 0)
        token = [ln for ln in out.splitlines() if ln.startswith("marker: ")][0].split(" ", 1)[1]
        self.assertGreaterEqual(len(token), 32)
        store2 = open_store(self.env, "prod")
        self.assertEqual(store2.marker_state(token), "live")
        db_bytes = (Path(self.dir.name) / "prod.db").read_bytes()
        self.assertNotIn(token.encode(), db_bytes)  # only the hash is at rest

    def test_mint_refuses_an_unknown_plan(self):
        # Seed genesis via a keyed open first (same precondition every real target has by the
        # time a human ever mints — the agent's plan_submit call already dispatched keyed): a
        # target NO keyed caller has EVER opened has a genuinely empty seal_events table, which
        # BrokerStore.seal_state (Task 1) fail-closes to sealed=True, cause="no seal history" —
        # correct, but it would make THIS test's mint refusal about the seal, not the unknown
        # plan_id it means to isolate. See pacioli.tools.PacioliBroker._seal_gate / cli.cmd_mint's
        # own keyless pre-check (Task 2) for why an uninitialized store now surfaces here at all.
        open_store(self.env, "prod")
        rc, out = self._mint(plan_id="never-recorded")
        self.assertNotEqual(rc, 0)
        self.assertIn("plan", out.lower())

    def test_mint_on_a_torn_store_prints_a_clean_error_not_a_traceback(self):
        # A zero-byte store file is a torn write, not an empty ledger (StoreCorruptError). The
        # CLI must confess it like every other open_store refusal: "error: ...", exit 2.
        (Path(self.dir.name) / "prod.db").write_bytes(b"")
        rc, out = self._mint()
        self.assertEqual(rc, 2)
        self.assertIn("error:", out)

    def test_verify_cli_reports_ok(self):
        store = open_store(self.env, "prod")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_verify(self.env, target=None, expected_head=None)
        self.assertEqual(rc, 0)
        self.assertIn("ok", buf.getvalue().lower())

    def test_verify_cli_on_a_corrupt_body_fails_cleanly_not_a_traceback(self):
        # Redteam verify pass: a garbled receipt body must give the operator a clean FAILED, not a
        # raw json traceback out of the head/count reads that run after verify().
        store = open_store(self.env, "prod")
        store.record_intent({"doc": "SI-1"})
        store._conn.execute("UPDATE receipts SET body=? WHERE seq=0", ("{bad json",))
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = cmd_verify(self.env, target=None, expected_head=None)  # must NOT raise
        self.assertEqual(rc, 1)
        self.assertIn("failed", err.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
