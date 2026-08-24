# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""`cli.main` routes each verb to its handler — the one thing nothing tested.

Every other CLI suite calls `cmd_*` directly, so the dispatch chain in `main()` (about fifty
branches) was never driven by anything. That is the wrong half to leave uncovered: `main` is the
real entry point, it is what an operator's shell actually reaches, and a mis-wired verb there
sends a governed command to the wrong handler while every direct-call test stays green.

Found 2026-08-24 the honest way: the coverage floor caught `cli.py` at 88.6% against its floor of
89% after the 0.39.0 fold rewrote `cmd_close`'s reconcile path, and CI went red on public main.
The floor's own message is "Raise the tests, not the floor", so these are the tests. They assert
ROUTING and ARGUMENT FORWARDING, not just that something was called: a chain that reached the
right function with the wrong arguments is the failure worth catching.
"""
import unittest
from unittest import mock

from pacioli import cli


class _Routed(Exception):
    """Raised by the stub so a handler can never fall through to real work."""


def _stub(recorder):
    def f(*a, **kw):
        recorder.append((a, kw))
        return 0
    return f


class TestMainRoutesEveryVerb(unittest.TestCase):
    def _route(self, argv, handler, env=None):
        """Run `main(argv)` with `handler` stubbed. Returns the (args, kwargs) it received."""
        got = []
        with mock.patch.object(cli, handler, _stub(got)):
            rc = cli.main(argv, env=env if env is not None else {})
        self.assertEqual(rc, 0, f"{argv} did not return the handler's value")
        self.assertEqual(len(got), 1, f"{argv} did not reach {handler} exactly once")
        return got[0]

    def test_mint_forwards_plan_target_and_ttl(self):
        a, _ = self._route(["mint", "PLAN-1", "--target", "t", "--ttl", "600"], "cmd_mint")
        self.assertEqual(a[1], "PLAN-1")
        self.assertEqual(a[2], "t")
        self.assertEqual(a[3], 600)

    def test_verify_forwards_target(self):
        a, _ = self._route(["verify", "--target", "t"], "cmd_verify")
        self.assertEqual(a[1], "t")

    def test_orphans_forwards_target(self):
        a, _ = self._route(["orphans", "--target", "t"], "cmd_orphans")
        self.assertEqual(a[1], "t")

    def test_close_forwards_the_reconcile_flag(self):
        """`--reconcile` is the flag the 0.39.0 fold rerouted through the spine. If `main` stopped
        forwarding it, the close would silently skip the audit sweep and still exit 0."""
        _, kw = self._route(["close", "--target", "t", "--reconcile"], "cmd_close")
        self.assertIs(kw["reconcile"], True)
        _, kw = self._route(["close", "--target", "t"], "cmd_close")
        self.assertIs(kw["reconcile"], False)

    def test_anchor_write_and_check_are_different_handlers(self):
        """One `anchor` verb, two handlers, chosen by a sub-subcommand. The exact shape where a
        dispatch chain sends a write to a reader, or worse the reverse."""
        self._route(["anchor", "write", "--target", "t"], "cmd_anchor_write")
        self._route(["anchor", "check", "--target", "t", "--in", "/dev/null"],
                    "cmd_anchor_check")

    def test_doctor_forwards_offline(self):
        a, _ = self._route(["doctor", "--target", "t", "--offline"], "cmd_doctor")
        self.assertIs(a[2], True)

    def test_seal_and_unseal_are_not_swapped(self):
        """CONTAIN and its release. Swapping these two would leave a broker unsealed when an
        operator asked for the opposite, which is the worst dispatch bug this file can have."""
        a, _ = self._route(["seal", "--reason", "r", "--target", "t"], "cmd_seal")
        self.assertEqual(a[1], "r")
        a, _ = self._route(["unseal", "--reason", "r", "--target", "t"], "cmd_unseal")
        self.assertEqual(a[1], "r")

    def test_seal_status_and_close_status_route_separately(self):
        self._route(["seal-status", "--target", "t"], "cmd_seal_status")
        self._route(["close-status", "--target", "t"], "cmd_close_status")

    def test_attest_forwards_reason(self):
        a, _ = self._route(["attest", "--reason", "r", "--target", "t"], "cmd_attest")
        self.assertEqual(a[1], "r")

    def test_a2a_keygen_routes(self):
        self._route(["a2a-keygen"], "cmd_a2a_keygen")

    def test_an_unknown_command_returns_2(self):
        """The chain's fall-through. argparse rejects an unknown verb itself, so this drives the
        `return 2` directly with a namespace argparse would never build -- the branch still has to
        be right, because it is the only thing standing between a future un-wired verb and a
        silent exit 0."""
        class _NS:
            command = "not-a-verb"
        with mock.patch.object(cli, "build_parser") as bp:
            bp.return_value.parse_args.return_value = _NS()
            self.assertEqual(cli.main([], env={}), 2)


class TestServeRoutesToTheRightDoor(unittest.TestCase):
    """`serve` is one verb over four doors, chosen by flags. A mis-route here starts the wrong
    door on the operator's port -- and one of those doors is the unauthenticated stdio one."""

    def _serve(self, argv, module, func):
        got = []
        with mock.patch(f"pacioli.{module}.{func}", _stub(got)):
            rc = cli.main(argv, env={})
        self.assertEqual(rc, 0)
        self.assertEqual(len(got), 1, f"{argv} did not reach pacioli.{module}.{func}")
        return got[0]

    def test_http_flag_starts_the_http_door(self):
        _, kw = self._serve(["serve", "--http", "--port", "9001"], "server", "serve_http")
        self.assertEqual(kw["port"], 9001)

    def test_a2a_flag_starts_the_a2a_door(self):
        self._serve(["serve", "--a2a"], "a2a", "serve_a2a")

    def test_rest_flag_starts_the_rest_door(self):
        self._serve(["serve", "--rest"], "rest", "serve_rest")

    def test_no_flag_starts_the_stdio_door(self):
        self._serve(["serve"], "server", "serve")

    def test_allowed_hosts_is_split_and_stripped(self):
        """It arrives as one comma string and must reach the door as a clean list. A door handed
        `" a, b"` would allowlist a host with a leading space and refuse the real one."""
        _, kw = self._serve(["serve", "--http", "--allowed-hosts", " a.example , b.example "],
                            "server", "serve_http")
        self.assertEqual(kw["allowed_hosts"], ["a.example", "b.example"])

    def test_no_allowed_hosts_means_none_not_empty_list(self):
        """`None` lets the door compute its own default from the bind; `[]` would be an empty
        allowlist that refuses everything. Different meanings, one falsy."""
        _, kw = self._serve(["serve", "--http"], "server", "serve_http")
        self.assertIsNone(kw["allowed_hosts"])


if __name__ == "__main__":
    unittest.main()
