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


FORGED_PARENT = Receipt(seq=0, prev_hash=GENESIS, kind=INTENT, body={"n": 0}, ts="t",
                        hmac="f" * 64)


def _resealed_splice():
    """A 3-receipt chain whose middle link was re-sealed over a parent that is not receipt 0.

    Built with the REAL key, so every seal is genuinely valid and every ``seq`` is intact. The
    only lie is the linkage: receipt 1 points at :data:`FORGED_PARENT` instead of receipt 0.
    This is the sole tamper shape the ``prev_hash`` comparison alone can catch.
    """
    chain = _chain({"n": 0}, {"n": 1}, {"n": 2})
    spliced = append(KEY, FORGED_PARENT, "intent", {"n": 1}, ts="2026-07-01T00:00:01Z")
    return [chain[0], spliced, chain[2]]


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

    # ------------------------------------------------------------------
    # Which branch refused MATTERS. `verify_chain` checks `seq` before `prev_hash`, so the two
    # obvious tampers below both trip the SEQ branch and never reach the linkage check at all.
    # Until 2026-08-11 these two tests asserted only `assertFalse(ok)` and discarded the reason,
    # so they passed on a refusal they were not named for, and the prev_hash branch had never
    # executed in the life of the repo (found by the first branch-coverage run). Each now pins
    # the branch it actually exercises; the linkage branch gets its own test below.
    # ------------------------------------------------------------------

    def test_dropped_receipt_detected_by_the_SEQ_check(self):
        # drop the middle receipt. The survivor carries seq 2 where 1 is expected, so this is a
        # seq refusal — NOT a linkage refusal, despite looking like one.
        chain = _chain({"n": 0}, {"n": 1}, {"n": 2})
        ok, reason = verify_chain(KEY, [chain[0], chain[2]])
        self.assertFalse(ok)
        self.assertIn("seq", reason)
        self.assertIn("receipt 1", reason)

    def test_reordered_chain_detected_by_the_SEQ_check(self):
        chain = _chain({"n": 0}, {"n": 1})
        ok, reason = verify_chain(KEY, [chain[1], chain[0]])
        self.assertFalse(ok)
        self.assertIn("seq", reason)

    def test_resealed_splice_detected_by_the_LINKAGE_check(self):
        """The one tamper only the ``prev_hash`` check catches: a KEY HOLDER re-sealing a spliced
        receipt.

        Every cheaper forgery is caught by a different branch, which is why this branch had no
        test. Rewrite ``prev_hash`` and keep the old seal and the SEAL check catches it; drop or
        reorder a receipt and the SEQ check catches it. But an attacker holding the sealing key
        can forge ``prev_hash`` *and re-seal over it*, leaving seq intact and every seal valid.
        Then the chain is internally seal-consistent and only the linkage comparison notices that
        receipt 1 no longer points at receipt 0.

        That is the case the receipt book exists for: the ledger records the agent's own actions,
        and the honesty note at the top of ``prove.py`` says the on-box chain is not tamper-evident
        against someone with host access. This check is what stands between "host access" and
        "host access AND an undetectable rewrite".
        """
        tampered = _resealed_splice()

        # the splice really is a lie about linkage, and really is sealed over a different parent
        self.assertNotEqual(FORGED_PARENT.hmac, tampered[0].hmac)
        self.assertEqual(tampered[1].seq, 1)
        self.assertEqual(tampered[1].prev_hash, FORGED_PARENT.hmac)

        ok, reason = verify_chain(KEY, tampered)
        self.assertFalse(ok, "a re-sealed splice must not verify")
        self.assertIn("receipt 1", reason)
        self.assertIn("prev_hash", reason)

    def test_the_resealed_splice_passes_every_OTHER_check(self):
        """Guard-the-guard: proves the test above refuses via LINKAGE and not via seq or seal.

        Without this, ``test_resealed_splice_detected_by_the_LINKAGE_check`` would keep passing
        even if the splice started being caught by a cheaper branch — which is precisely how the
        two tests above spent the repo's whole life asserting a refusal they never caused.
        """
        tampered = _resealed_splice()

        # 1. Every seq is exactly its index, so the SEQ branch cannot be what refuses.
        self.assertEqual([r.seq for r in tampered], [0, 1, 2])

        # 2. Every seal verifies against that receipt's own sealed fields, so the SEAL branch
        #    cannot be what refuses either. Rebuilding each receipt over a parent carrying its
        #    own recorded prev_hash must reproduce its hmac exactly.
        for r in tampered:
            parent = None if r.seq == 0 else Receipt(
                seq=r.seq - 1, prev_hash=GENESIS, kind=INTENT, body={}, ts="t",
                hmac=r.prev_hash,
            )
            self.assertEqual(append(KEY, parent, r.kind, r.body, r.ts).hmac, r.hmac,
                             f"receipt {r.seq}'s seal must verify on its own contents")

        # 3. So the only thing left to object to is the linkage.
        ok, reason = verify_chain(KEY, tampered)
        self.assertFalse(ok)
        self.assertIn("prev_hash", reason)


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
