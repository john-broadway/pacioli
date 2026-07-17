# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Bench-free unit tests for the CONSENT pure core (pacioli.consent) — the marker.

A marker is a single-use, out-of-band, human-minted grant the agent cannot derive, bound to a
specific plan and time-boxed. Its lifecycle is a state machine — ``live → reserved → consumed`` (or
``reserved → live`` on a failed submit) — so the single-use guarantee holds under *concurrency*, not
just against a crash: the glue CAS-claims ``live → reserved`` BEFORE the irreversible submit, so a
second racing call finds it already reserved. Deny-biased throughout.

Run: `python3 -m unittest pacioli.tests.test_consent` from the broker app root. No frappe required.
"""
import dataclasses
import unittest

from pacioli.consent import (
    CONSUMED,
    LIVE,
    RESERVED,
    Marker,
    commit,
    hash_token,
    new_marker,
    release,
    reserve,
    verify,
)

TOKEN = "s3cr3t-token-minted-out-of-band"
PLAN = "plan-abc"
GOOD = new_marker(TOKEN, PLAN, expires_at=1000.0)  # state defaults to LIVE


class TestVerify(unittest.TestCase):
    def test_valid_live_marker_verifies(self):
        self.assertEqual(verify(GOOD, TOKEN, PLAN, now=500.0), (True, None))

    def test_none_marker_denied(self):
        self.assertFalse(verify(None, TOKEN, PLAN, now=500.0)[0])

    def test_wrong_token_denied(self):
        self.assertFalse(verify(GOOD, "not-the-token", PLAN, now=500.0)[0])

    def test_blank_token_denied(self):
        self.assertFalse(verify(GOOD, "", PLAN, now=500.0)[0])
        self.assertFalse(verify(GOOD, None, PLAN, now=500.0)[0])

    def test_wrong_plan_denied(self):
        self.assertFalse(verify(GOOD, TOKEN, "some-other-plan", now=500.0)[0])

    def test_falsy_plan_on_both_sides_denied(self):
        # None == None must NOT read as a verified binding
        blank = dataclasses.replace(GOOD, plan_id=None)
        self.assertFalse(verify(blank, TOKEN, None, now=500.0)[0])
        self.assertFalse(verify(GOOD, TOKEN, "", now=500.0)[0])

    def test_expired_denied(self):
        self.assertFalse(verify(GOOD, TOKEN, PLAN, now=1000.0)[0])
        self.assertFalse(verify(GOOD, TOKEN, PLAN, now=1500.0)[0])

    def test_nan_or_inf_now_denied(self):
        # a NaN clock makes `now >= expires_at` silently False — must fail closed
        self.assertFalse(verify(GOOD, TOKEN, PLAN, now=float("nan"))[0])
        self.assertFalse(verify(GOOD, TOKEN, PLAN, now=float("inf"))[0])
        self.assertFalse(verify(GOOD, TOKEN, PLAN, now="500")[0])

    def test_non_live_marker_denied(self):
        # only a LIVE marker verifies; a reserved or consumed one never re-authorises
        self.assertFalse(verify(dataclasses.replace(GOOD, state=RESERVED), TOKEN, PLAN, now=500.0)[0])
        self.assertFalse(verify(dataclasses.replace(GOOD, state=CONSUMED), TOKEN, PLAN, now=500.0)[0])


class TestTokenStorage(unittest.TestCase):
    def test_raw_token_not_stored(self):
        self.assertNotEqual(GOOD.token_hash, TOKEN)
        self.assertEqual(GOOD.token_hash, hash_token(TOKEN))


class TestReserveCommitRelease(unittest.TestCase):
    def test_reserve_valid_returns_reserved_marker(self):
        ok, reason, m = reserve(GOOD, TOKEN, PLAN, now=500.0)
        self.assertTrue(ok, reason)
        self.assertEqual(m.state, RESERVED)

    def test_reserve_invalid_returns_none(self):
        ok, _, m = reserve(GOOD, "wrong", PLAN, now=500.0)
        self.assertFalse(ok)
        self.assertIsNone(m)

    def test_reserve_non_live_denied(self):
        # can't reserve an already-reserved marker (the concurrency guard)
        ok, _, _ = reserve(dataclasses.replace(GOOD, state=RESERVED), TOKEN, PLAN, now=500.0)
        self.assertFalse(ok)

    def test_commit_reserved_becomes_consumed(self):
        _, _, m = reserve(GOOD, TOKEN, PLAN, now=500.0)
        self.assertEqual(commit(m).state, CONSUMED)

    def test_release_reserved_becomes_live_again(self):
        # a failed submit releases the reservation so the human's grant isn't burned
        _, _, m = reserve(GOOD, TOKEN, PLAN, now=500.0)
        released = release(m)
        self.assertEqual(released.state, LIVE)
        self.assertTrue(verify(released, TOKEN, PLAN, now=500.0)[0])

    def test_committed_marker_never_verifies_again(self):
        _, _, m = reserve(GOOD, TOKEN, PLAN, now=500.0)
        consumed = commit(m)
        self.assertFalse(verify(consumed, TOKEN, PLAN, now=500.0)[0])
        # and can't be re-reserved
        self.assertFalse(reserve(consumed, TOKEN, PLAN, now=500.0)[0])


if __name__ == "__main__":
    unittest.main()
