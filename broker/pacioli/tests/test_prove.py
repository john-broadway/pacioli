# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Bench-free unit tests for the PROVE pure core (pacioli.prove) — the hash-chained receipt ledger.

Run: `python3 -m unittest pacioli.tests.test_prove` from the broker app root. No frappe required.
"""
import dataclasses
import unittest
from decimal import Decimal

from pacioli.prove import GENESIS, INTENT, Receipt, append, head, orphans, verify_chain

KEY = b"seal-key-lives-off-box"
OTHER_KEY = b"a-different-key"


def _chain(*bodies, key=KEY):
    """Build a chain of ``intent`` receipts from bodies, each chained to the last."""
    receipts, prev = [], None
    for i, body in enumerate(bodies):
        prev = append(key, prev, "intent", body, ts=f"2026-07-01T00:00:{i:02d}Z")
        receipts.append(prev)
    return receipts


class TestAppend(unittest.TestCase):
    def test_first_receipt_chains_from_genesis(self):
        r = append(KEY, None, "intent", {"doc": "SINV-001"}, ts="2026-07-01T00:00:00Z")
        self.assertEqual(r.seq, 0)
        self.assertEqual(r.prev_hash, GENESIS)
        self.assertTrue(r.hmac)

    def test_second_receipt_chains_from_first(self):
        r0 = append(KEY, None, "intent", {"doc": "A"}, ts="t0")
        r1 = append(KEY, r0, "intent", {"doc": "B"}, ts="t1")
        self.assertEqual(r1.seq, 1)
        self.assertEqual(r1.prev_hash, r0.hmac)

    def test_same_body_different_position_gives_different_hmac(self):
        # identical body must not seal identically at different chain positions (prev_hash differs)
        r0 = append(KEY, None, "intent", {"doc": "X"}, ts="t")
        r1 = append(KEY, r0, "intent", {"doc": "X"}, ts="t")
        self.assertNotEqual(r0.hmac, r1.hmac)


class TestVerifyChain(unittest.TestCase):
    def test_valid_chain_verifies(self):
        ok, reason = verify_chain(KEY, _chain({"doc": "A"}, {"doc": "B"}, {"doc": "C"}))
        self.assertTrue(ok, reason)

    def test_empty_chain_verifies(self):
        self.assertEqual(verify_chain(KEY, []), (True, None))

    def test_tampered_body_detected(self):
        chain = _chain({"amount": 100}, {"amount": 200})
        chain[0] = dataclasses.replace(chain[0], body={"amount": 999})  # rewrite a posted amount
        ok, reason = verify_chain(KEY, chain)
        self.assertFalse(ok)
        self.assertIn("0", str(reason))

    def test_wrong_key_detected(self):
        # a chain sealed with KEY must not verify under OTHER_KEY (forged-with-wrong-key)
        ok, _ = verify_chain(OTHER_KEY, _chain({"doc": "A"}))
        self.assertFalse(ok)

    def test_broken_linkage_detected(self):
        # drop the middle receipt: seq/prev_hash linkage must break
        chain = _chain({"n": 0}, {"n": 1}, {"n": 2})
        ok, _ = verify_chain(KEY, [chain[0], chain[2]])
        self.assertFalse(ok)

    def test_reordered_chain_detected(self):
        chain = _chain({"n": 0}, {"n": 1})
        ok, _ = verify_chain(KEY, [chain[1], chain[0]])
        self.assertFalse(ok)


class TestOrphans(unittest.TestCase):
    def test_intent_without_outcome_is_orphan(self):
        # an intent with no matching outcome = a crash between execute and finalize
        intent = append(KEY, None, "intent", {"doc": "SINV-9", "plan": "p1"}, ts="t0")
        self.assertEqual([r.seq for r in orphans([intent])], [0])

    def test_intent_with_outcome_not_orphan(self):
        intent = append(KEY, None, "intent", {"doc": "SINV-9"}, ts="t0")
        outcome = append(KEY, intent, "outcome", {"finalizes": 0, "status": "committed"}, ts="t1")
        self.assertEqual(orphans([intent, outcome]), [])

    def test_outcome_for_other_intent_leaves_orphan(self):
        i0 = append(KEY, None, "intent", {"doc": "A"}, ts="t0")
        i1 = append(KEY, i0, "intent", {"doc": "B"}, ts="t1")
        outcome = append(KEY, i1, "outcome", {"finalizes": 0, "status": "committed"}, ts="t2")
        # i0 is finalized; i1 is still orphaned
        self.assertEqual([r.seq for r in orphans([i0, i1, outcome])], [1])

    def test_failed_outcome_leaves_intent_orphan(self):
        # only a COMMITTED outcome finalizes. A "failed"/uncertain outcome (e.g. a timeout that may
        # have landed) must keep the intent orphaned so the reconciliation sweep still checks it.
        intent = append(KEY, None, "intent", {"doc": "SINV-9"}, ts="t0")
        failed = append(KEY, intent, "outcome", {"finalizes": 0, "status": "failed"}, ts="t1")
        self.assertEqual([r.seq for r in orphans([intent, failed])], [0])


class TestBodyValidation(unittest.TestCase):
    def test_non_json_native_body_rejected(self):
        # default=str would have let a set/object silently str()-collapse into the seal
        with self.assertRaises(ValueError):
            append(KEY, None, "intent", {"x": {1, 2}}, ts="t")

    def test_decimal_body_rejected(self):
        # Decimal("10.50") and the string "10.50" must NOT be allowed to seal identically
        with self.assertRaises(ValueError):
            append(KEY, None, "intent", {"amount": Decimal("10.50")}, ts="t")

    def test_non_string_key_rejected(self):
        with self.assertRaises(ValueError):
            append(KEY, None, "intent", {1: "x"}, ts="t")


class TestExpectedHead(unittest.TestCase):
    def test_matching_head_verifies(self):
        chain = _chain({"n": 0}, {"n": 1})
        self.assertTrue(verify_chain(KEY, chain, expected_head=chain[-1].hmac)[0])

    def test_tail_truncation_detected_with_expected_head(self):
        # the internal chain of a truncated list is still self-consistent; only an off-box head
        # anchor catches the dropped tail (the most-likely-fraudulent newest receipt)
        chain = _chain({"n": 0}, {"n": 1}, {"n": 2})
        real_head = chain[-1].hmac
        ok, reason = verify_chain(KEY, chain[:-1], expected_head=real_head)
        self.assertFalse(ok)
        self.assertIn("head", str(reason).lower())

    def test_full_wipe_detected_with_expected_head(self):
        chain = _chain({"n": 0})
        ok, _ = verify_chain(KEY, [], expected_head=chain[-1].hmac)
        self.assertFalse(ok)


class TestHead(unittest.TestCase):
    def test_head_returns_last(self):
        chain = _chain({"n": 0}, {"n": 1})
        self.assertEqual(head(chain), chain[-1])

    def test_head_of_empty_is_none(self):
        self.assertIsNone(head([]))


if __name__ == "__main__":
    unittest.main()


class TestNonFiniteFloatsRefused(unittest.TestCase):
    """A financial ledger must never seal NaN/Infinity — they are not valid JSON and break any
    strict external parser (the off-box anchor of increment 2)."""

    def test_nan_refused(self):
        with self.assertRaises(ValueError):
            append(b"k" * 32, None, INTENT, {"amount": float("nan")}, ts="t0")

    def test_infinity_refused(self):
        with self.assertRaises(ValueError):
            append(b"k" * 32, None, INTENT, {"amount": float("inf")}, ts="t0")

    def test_nested_infinity_refused(self):
        with self.assertRaises(ValueError):
            append(b"k" * 32, None, INTENT, {"gl": [{"debit": float("-inf")}]}, ts="t0")
