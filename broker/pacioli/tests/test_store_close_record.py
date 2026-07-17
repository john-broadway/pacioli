# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Store-layer close-record + attestation gate (Half 3, Fork A1, Task 1) — the ``close_records``
table, canonical/HMAC helpers, and the ``record_close``/``attest``/``close_cursor``/
``close_gate_state`` methods on :class:`BrokerStore`.

Mirrors ``test_store_seal.py``'s fixture shape (in-memory sqlite + a fixed clock) and its
fail-closed discipline, with ONE deliberate divergence pinned by the design doc
(docs/plans/2026-07-15-close-half3-close-record.md, Global constraint 4): **zero close-record rows
is NOT latched** — absence of history is the honest genesis state for a cursor (no period has ever
closed, so the first ``--advance`` is legitimate), unlike the seal's zero-rows=SEALED. Every OTHER
failure mode (a ``seq`` gap, an unverifiable HMAC, a keyless open, a gapped close with no later
attest) is fail-closed to LATCHED, same discipline as the seal.

Run: `.venv/bin/python -m pytest pacioli/tests/test_store_close_record.py -q` from the broker app
root. No frappe required (bench-free fixture, same as test_store_seal.py).
"""
import sqlite3
import unittest

from pacioli.store import (
    AttestStaleError,
    BrokerStore,
    CloseGateLatchedError,
    CloseRecordIntegrityError,
    CloseRecordStaleError,
    NoAttestationPendingError,
    _close_record_canonical,
    _close_record_hmac,
)

KEY = b"close-record-key-on-box-until-increment-2"
CLOCK = "2026-07-15T00:00:00Z"


def _store(key=KEY):
    # a fixed clock keeps close-record timestamps deterministic, same fixture shape as
    # test_store_seal.py
    return BrokerStore(sqlite3.connect(":memory:"), key, now_iso=lambda: CLOCK)


class TestCloseRecordCanonicalAndHmac(unittest.TestCase):
    """The canonical-bytes + HMAC helpers, exercised directly — before any store plumbing."""

    def test_none_vs_empty_string_do_not_collide(self):
        # The nullable fields (period_since/period_until/attested_head) must have an unambiguous
        # canonical encoding: None (genesis/open-ended) is NOT the same fact as "" (an explicit
        # empty string), so the canonical bytes -- and therefore the HMAC -- must differ.
        with_none = _close_record_canonical(
            1, CLOCK, "close", None, "2026-07-31", None, 0, "", "close"
        )
        with_empty = _close_record_canonical(
            1, CLOCK, "close", "", "2026-07-31", None, 0, "", "close"
        )
        self.assertNotEqual(with_none, with_empty)

    def test_none_vs_empty_on_every_nullable_field(self):
        base = dict(
            seq=1, ts=CLOCK, action="close", period_since=None, period_until=None,
            attested_head=None, gapped=0, reason="", source="close",
        )
        canonical_base = _close_record_canonical(**base)
        for field in ("period_since", "period_until", "attested_head"):
            variant = dict(base)
            variant[field] = ""
            canonical_variant = _close_record_canonical(**variant)
            self.assertNotEqual(
                canonical_base, canonical_variant,
                f"{field}=None collided with {field}='' in canonical bytes",
            )

    def test_hmac_is_deterministic_for_same_inputs(self):
        a = _close_record_hmac(KEY, 1, CLOCK, "close", "2026-01-01", "2026-01-31", "deadbeef", 0,
                                "month end", "close")
        b = _close_record_hmac(KEY, 1, CLOCK, "close", "2026-01-01", "2026-01-31", "deadbeef", 0,
                                "month end", "close")
        self.assertEqual(a, b)

    def test_hmac_changes_if_any_field_changes(self):
        a = _close_record_hmac(KEY, 1, CLOCK, "close", "2026-01-01", "2026-01-31", "deadbeef", 0,
                                "month end", "close")
        b = _close_record_hmac(KEY, 1, CLOCK, "close", "2026-01-01", "2026-01-31", "deadbeef", 1,
                                "month end", "close")  # gapped flipped
        self.assertNotEqual(a, b)

    def test_canonical_bytes_are_domain_prefixed(self):
        raw = _close_record_canonical(1, CLOCK, "close", None, None, None, 0, "", "close")
        self.assertTrue(raw.startswith(b"close:"))


class TestEmptyGenesis(unittest.TestCase):
    """Deliberate divergence from the seal: zero rows is the HONEST genesis state, not sealed."""

    def test_fresh_store_gate_is_not_latched(self):
        s = _store()
        state = s.close_gate_state()
        self.assertFalse(state["latched"])
        self.assertIsNone(state["cursor"])
        self.assertIsNone(state["last_close_seq"])

    def test_fresh_store_cursor_is_none(self):
        s = _store()
        self.assertIsNone(s.close_cursor())


class TestRecordCloseHappyPath(unittest.TestCase):
    def test_record_close_advances_cursor(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)
        self.assertEqual(s.close_cursor(), "2026-01-31")

    def test_record_close_keeps_gate_open_when_not_gapped(self):
        s = _store()
        result = s.record_close(period_since=None, period_until="2026-01-31",
                                 attested_head="h1", gapped=False, expected_last_close_seq=None)
        self.assertFalse(result["latched"])
        state = s.close_gate_state()
        self.assertFalse(state["latched"])

    def test_second_close_advances_cursor_again(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)
        s.record_close(period_since="2026-01-31", period_until="2026-02-28", attested_head="h2",
                        gapped=False, expected_last_close_seq=1)
        self.assertEqual(s.close_cursor(), "2026-02-28")

    def test_record_close_default_reason_is_empty_string(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)
        row = s._conn.execute(
            "SELECT reason FROM close_records WHERE action='close'"
        ).fetchone()
        self.assertEqual(row[0], "")


class TestGappedCloseLatches(unittest.TestCase):
    def test_gapped_close_latches_the_gate(self):
        s = _store()
        result = s.record_close(period_since=None, period_until="2026-01-31",
                                 attested_head="h1", gapped=True, expected_last_close_seq=None)
        self.assertTrue(result["latched"])
        state = s.close_gate_state()
        self.assertTrue(state["latched"])
        self.assertTrue(state["reason"])  # non-empty explanation

    def test_gapped_close_still_reports_honest_cursor(self):
        # The workflow latch (a gapped close awaiting attestation) is NOT an integrity failure --
        # the content is trusted, only the workflow gate is up -- so cursor/last_close_seq stay
        # honestly populated (unlike the integrity-failure cases below, which null them out).
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        state = s.close_gate_state()
        self.assertEqual(state["cursor"], "2026-01-31")
        self.assertEqual(state["last_close_seq"], 1)


class TestAttestClears(unittest.TestCase):
    def test_attest_clears_the_gate(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        result = s.attest("reviewed the gap, it's fine", expected_seq=1)
        self.assertFalse(result["latched"])
        self.assertFalse(s.close_gate_state()["latched"])

    def test_attest_records_operator_source_and_reason(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        s.attest("reviewed the gap, it's fine", expected_seq=1)
        row = s._conn.execute(
            "SELECT action, reason, source FROM close_records WHERE action='attest'"
        ).fetchone()
        self.assertEqual(row[0], "attest")
        self.assertEqual(row[1], "reviewed the gap, it's fine")
        self.assertEqual(row[2], "operator")


class TestLatchAttestLatchAgainLifecycle(unittest.TestCase):
    def test_latch_attest_latch_again(self):
        s = _store()
        # 1. gapped close -> latched
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        self.assertTrue(s.close_gate_state()["latched"])

        # 2. attest -> clears
        s.attest("first gap reviewed", expected_seq=1)
        self.assertFalse(s.close_gate_state()["latched"])

        # 3. a clean close in between keeps it open
        s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                        attested_head="h2", gapped=False, expected_last_close_seq=1)
        self.assertFalse(s.close_gate_state()["latched"])

        # 4. a second gapped close -> latched again
        s.record_close(period_since="2026-02-28", period_until="2026-03-31",
                        attested_head="h3", gapped=True, expected_last_close_seq=3)
        self.assertTrue(s.close_gate_state()["latched"])

        # 5. attest again -> clears again
        s.attest("second gap reviewed", expected_seq=4)
        self.assertFalse(s.close_gate_state()["latched"])
        self.assertEqual(s.close_cursor(), "2026-03-31")


class TestAttestWithNothingPendingRefuses(unittest.TestCase):
    def test_attest_on_fresh_store_refuses(self):
        s = _store()
        with self.assertRaises(NoAttestationPendingError):
            s.attest("nothing to attest", expected_seq=None)

    def test_attest_after_non_gapped_close_refuses(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)
        with self.assertRaises(NoAttestationPendingError):
            s.attest("nothing gapped here", expected_seq=None)

    def test_attest_twice_in_a_row_refuses_the_second_time(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        s.attest("first attest clears it", expected_seq=1)
        with self.assertRaises(NoAttestationPendingError):
            s.attest("nothing left to attest", expected_seq=None)


class TestRecordCloseWhileLatchedRefuses(unittest.TestCase):
    def test_record_close_refuses_while_latched(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        with self.assertRaises(CloseGateLatchedError):
            s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                            attested_head="h2", gapped=False, expected_last_close_seq=1)

    def test_cursor_unchanged_after_refused_record_close(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        with self.assertRaises(CloseGateLatchedError):
            s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                            attested_head="h2", gapped=False, expected_last_close_seq=1)
        self.assertEqual(s.close_cursor(), "2026-01-31")


class TestSeqGapLatches(unittest.TestCase):
    def test_deleted_interior_row_latches(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)  # seq 1
        s.attest("reviewed", expected_seq=1)  # seq 2
        s._conn.execute("DELETE FROM close_records WHERE seq = 1")
        s._conn.commit()
        state = s.close_gate_state()
        self.assertTrue(state["latched"])
        self.assertIn("gap", state["reason"].lower())

    def test_gap_nulls_cursor_and_last_close_seq(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)  # seq 1
        s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                        attested_head="h2", gapped=False, expected_last_close_seq=1)  # seq 2
        s._conn.execute("DELETE FROM close_records WHERE seq = 1")
        s._conn.commit()
        state = s.close_gate_state()
        self.assertTrue(state["latched"])
        self.assertIsNone(state["cursor"])
        self.assertIsNone(state["last_close_seq"])


class TestTamperedRowLatches(unittest.TestCase):
    def test_bad_hmac_latches(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)
        s._conn.execute("UPDATE close_records SET hmac = ? WHERE seq = 1", ("0" * 64,))
        s._conn.commit()
        state = s.close_gate_state()
        self.assertTrue(state["latched"])
        self.assertIn("unverifiable", state["reason"].lower())

    def test_tampered_content_with_stale_hmac_latches(self):
        # Content edited but hmac left as-is (a keyless attacker's only option) -- must still
        # fail closed under a KEYED read, same discipline as seal's F1(a).
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)
        s._conn.execute(
            "UPDATE close_records SET period_until = ? WHERE seq = 1", ("2099-12-31",)
        )
        s._conn.commit()
        state = s.close_gate_state()
        self.assertTrue(state["latched"])


class TestKeylessStoreLatchesAndRefusesWrites(unittest.TestCase):
    def test_keyless_gate_state_is_latched(self):
        s = _store(key=None)
        state = s.close_gate_state()
        self.assertTrue(state["latched"])
        self.assertIn("key", state["reason"].lower())

    def test_keyless_record_close_refuses(self):
        s = _store(key=None)
        with self.assertRaises(ValueError):
            s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                            gapped=False, expected_last_close_seq=None)

    def test_keyless_attest_refuses(self):
        s = _store(key=None)
        with self.assertRaises(ValueError):
            s.attest("reason", expected_seq=None)

    def test_keyless_cursor_fails_closed(self):
        # Adversarial review finding 1: the cursor is period-loop CONTROL data, not a mere status
        # display -- an unverified read path for it would let a tampered history steer where the
        # next period starts. So close_cursor() goes through the SAME verified derivation as
        # close_gate_state(), and keyless (no key to verify anything with) fails CLOSED -- a
        # raise, never a value. (close_gate_state() itself stays readable keyless -- it reports
        # latched=True honestly -- so status/render surfaces are still never gated.)
        conn = sqlite3.connect(":memory:")
        keyed = BrokerStore(conn, KEY, now_iso=lambda: CLOCK)
        keyed.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                            gapped=False, expected_last_close_seq=None)
        keyless = BrokerStore(conn, None, now_iso=lambda: CLOCK)
        with self.assertRaises(CloseRecordIntegrityError):
            keyless.close_cursor()


class TestVerifiedCursor(unittest.TestCase):
    """Adversarial review finding 1 (SEVERE): close_cursor() must never serve a value off an
    unverified read. The exact reproduction from the review: tamper the hmac on the latest close
    row -- close_gate_state() correctly latches with cursor=None, but the OLD close_cursor() read
    period_until straight off the row and served the forged value. Fixed: close_cursor() derives
    through the same verified path and RAISES on any integrity failure."""

    def test_tampered_hmac_on_latest_close_raises_not_serves_forged_cursor(self):
        # The review's exact reproduction: two closes, tamper seq=2's row content so its hmac no
        # longer verifies -- the forged period_until must never come back as the cursor.
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)  # seq 1
        s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                        attested_head="h2", gapped=False, expected_last_close_seq=1)  # seq 2
        s._conn.execute(
            "UPDATE close_records SET period_until = ? WHERE seq = 2", ("2099-12-31",)
        )
        s._conn.commit()
        # Control: gate_state on the same rows correctly latches with cursor nulled.
        state = s.close_gate_state()
        self.assertTrue(state["latched"])
        self.assertIsNone(state["cursor"])
        # The fix: close_cursor() raises -- it must never return "2099-12-31" (nor anything else).
        with self.assertRaises(CloseRecordIntegrityError):
            s.close_cursor()

    def test_tampered_hmac_column_raises(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)
        s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                        attested_head="h2", gapped=False, expected_last_close_seq=1)
        s._conn.execute("UPDATE close_records SET hmac = ? WHERE seq = 2", ("0" * 64,))
        s._conn.commit()
        with self.assertRaises(CloseRecordIntegrityError):
            s.close_cursor()

    def test_seq_gap_raises(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)  # seq 1
        s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                        attested_head="h2", gapped=False, expected_last_close_seq=1)  # seq 2
        s._conn.execute("DELETE FROM close_records WHERE seq = 1")
        s._conn.commit()
        with self.assertRaises(CloseRecordIntegrityError):
            s.close_cursor()

    def test_workflow_latch_still_serves_verified_cursor(self):
        # A gapped close awaiting attestation is a WORKFLOW latch, not an integrity failure --
        # every row's hmac verifies -- so the cursor is trustworthy and still served (an operator
        # or the advance path's refusal render needs to see which period is stuck).
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        self.assertTrue(s.close_gate_state()["latched"])
        self.assertEqual(s.close_cursor(), "2026-01-31")

    def test_clean_history_still_serves_cursor(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)
        self.assertEqual(s.close_cursor(), "2026-01-31")


def _insert_legacy_open_ended_close(s, *, seq=1, period_since="2026-01-01", gapped=0,
                                     attested_head="h1"):
    """Inject a HISTORICAL open-ended close row (period_until=None) directly, with a VALID hmac
    -- the shape a pre-redteam-wave store could have written. The WRITE path now refuses
    period_until=None (redteam finding 3: two consecutive no-until advances would both close
    genesis..now, full overlap); these rows survive only as tolerated history."""
    ts = CLOCK
    from pacioli.store import _close_record_hmac as h
    mac = h(KEY, seq, ts, "close", period_since, None, attested_head, gapped, "", "close")
    s._conn.execute(
        "INSERT INTO close_records(seq, ts, action, period_since, period_until, attested_head,"
        " gapped, reason, source, hmac) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (seq, ts, "close", period_since, None, attested_head, gapped, "", "close", mac),
    )
    s._conn.commit()


class TestOpenEndedCloseRefusedOnWrite(unittest.TestCase):
    """Redteam finding 3 (open-ended-close overlap): two consecutive no-until advances would both
    close genesis..now -- full overlap, double-count. The WRITE path now refuses
    period_until=None outright (the CLI materializes a concrete until from the store clock before
    calling); historical open-ended rows written before this wave remain TOLERATED, verified,
    dead rows -- the disambiguation machinery (last_close_seq vs cursor) stays for them."""

    def test_record_close_refuses_period_until_none(self):
        s = _store()
        with self.assertRaises(ValueError):
            s.record_close(period_since=None, period_until=None, attested_head="h1",
                            gapped=False, expected_last_close_seq=None)
        _, rows = s.close_records_snapshot()
        self.assertEqual(rows, [])  # nothing written

    def test_historical_open_ended_row_is_tolerated_and_derives_cleanly(self):
        # Dead-rows tolerance: a legacy open-ended row (valid hmac) neither latches nor crashes;
        # cursor is honestly None with last_close_seq set -- the disambiguation from the earlier
        # adversarial review stays exactly for this case.
        s = _store()
        _insert_legacy_open_ended_close(s)
        state = s.close_gate_state()
        self.assertFalse(state["latched"])
        self.assertIsNone(state["cause"])
        self.assertIsNone(state["cursor"])
        self.assertEqual(state["last_close_seq"], 1)
        self.assertIsNone(s.close_cursor())  # ambiguous alone -- documented as such

    def test_genesis_vs_historical_open_ended_distinguished_by_last_close_seq(self):
        fresh = _store()
        self.assertIsNone(fresh.close_gate_state()["last_close_seq"])  # no close ever

        legacy = _store()
        _insert_legacy_open_ended_close(legacy)
        self.assertEqual(legacy.close_gate_state()["last_close_seq"], 1)  # a close exists

        # Both cursors are None -- the seq is the only (and the documented) distinction.
        self.assertIsNone(fresh.close_cursor())
        self.assertIsNone(legacy.close_cursor())

    def test_next_close_after_a_historical_open_ended_row_still_works(self):
        # The tolerance is not just read-side: the period loop continues past a legacy row (with
        # a concrete until, as the write path now requires).
        s = _store()
        _insert_legacy_open_ended_close(s)
        s.record_close(period_since=None, period_until="2026-07-01T00:00:00Z",
                        attested_head="h2", gapped=False, expected_last_close_seq=1)
        self.assertEqual(s.close_cursor(), "2026-07-01T00:00:00Z")


class TestAttestCompareAndAppend(unittest.TestCase):
    """Redteam finding 4 (model-fidelity F1, attest staleness): attest could clear a DIFFERENT
    gap than the operator reviewed -- A reviews the JAN gap; B attests it and advances into a
    gapped FEB; A's attest (reason "reviewed JANUARY") then clears FEB, a permanent wrong-reason
    row. Mirror of record_close's compare-and-append: ``attest(reason, *, expected_seq)`` --
    the seq of the gapped close being attested; inside the transaction, refuse
    (:class:`AttestStaleError`) when the currently-pending gapped close's seq differs."""

    def test_happy_path_with_correct_expected_seq(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)  # seq 1, pending
        result = s.attest("reviewed the JAN gap", expected_seq=1)
        self.assertFalse(result["latched"])

    def test_stale_expected_seq_raises_and_writes_no_row(self):
        # The exact wrong-reason reproduction: A plans against the JAN gap (seq 1); B attests it
        # and advances into a gapped FEB (seq 3); A's attest still carrying expected_seq=1 must
        # refuse -- the pending gap is now a DIFFERENT close than the one A reviewed.
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)  # seq 1 -- the JAN gap
        planned = s.close_gate_state()
        self.assertEqual(planned["last_close_seq"], 1)  # A's read

        s.attest("B reviewed JAN", expected_seq=1)  # seq 2 -- B clears it first
        s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                        attested_head="h2", gapped=True, expected_last_close_seq=1)  # seq 3, FEB

        with self.assertRaises(AttestStaleError):
            s.attest("A reviewed JANUARY", expected_seq=planned["last_close_seq"])

        # no wrong-reason attest row landed; FEB is still honestly pending
        _, rows = s.close_records_snapshot()
        self.assertEqual(len(rows), 3)
        state = s.close_gate_state()
        self.assertTrue(state["latched"])
        self.assertEqual(state["last_close_seq"], 3)

    def test_expected_none_with_a_pending_gap_refuses(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        with self.assertRaises(AttestStaleError):
            s.attest("reviewed nothing in particular", expected_seq=None)
        _, rows = s.close_records_snapshot()
        self.assertEqual(len(rows), 1)

    def test_nothing_pending_fires_before_stale(self):
        # Ordering pinned: with nothing gapped pending at all, the refusal is
        # NoAttestationPendingError (there is no gap to be stale ABOUT), never AttestStaleError.
        s = _store()
        with self.assertRaises(NoAttestationPendingError):
            s.attest("reviewed", expected_seq=7)


class TestHonestTamperCeiling(unittest.TestCase):
    """Redteam finding 5 (honesty): the close-record gate's limits, pinned behaviorally rather
    than argued in prose -- if either test ever starts failing the docstrings must be re-examined
    (the ceiling improved), never weakened back."""

    def test_tail_row_deletion_is_not_detected_by_the_gate_derivation(self):
        # The honest ceiling: deleting the NEWEST close_records row(s) leaves survivors
        # contiguous 1..N-1, every surviving hmac genuinely verifies, and the gate derivation
        # reads clean -- there is no count anchor for close_records yet (same disclosed limit
        # seal_events has without an off-box pin).
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)  # seq 1
        s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                        attested_head="h2", gapped=False, expected_last_close_seq=1)  # seq 2
        s._conn.execute("DELETE FROM close_records WHERE seq = 2")  # tail deletion
        s._conn.commit()
        state = s.close_gate_state()
        self.assertFalse(state["latched"])  # NOT detected -- the documented ceiling
        self.assertIsNone(state["cause"])
        self.assertEqual(state["cursor"], "2026-01-31")  # the cursor silently rolled back

    def test_out_of_vocabulary_action_rows_are_inert_in_the_workflow_derivation(self):
        # A row whose action is neither 'close' nor 'attest' (validly hmac'd -- only a key
        # holder can mint one) does not latch, unlatch, or move the cursor; its hmac and seq
        # contiguity are still checked like every row's.
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)  # seq 1
        from pacioli.store import _close_record_hmac as h
        mac = h(KEY, 2, CLOCK, "bogus", None, None, None, 0, "", "close")
        s._conn.execute(
            "INSERT INTO close_records(seq, ts, action, period_since, period_until,"
            " attested_head, gapped, reason, source, hmac) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (2, CLOCK, "bogus", None, None, None, 0, "", "close", mac),
        )
        s._conn.commit()
        state = s.close_gate_state()
        self.assertFalse(state["latched"])
        self.assertEqual(state["cursor"], "2026-01-31")  # unmoved by the bogus row
        self.assertEqual(state["last_close_seq"], 1)

    def test_stale_error_message_never_asserts_concurrency_as_fact(self):
        # Redteam finding 11: on an EMPTY store a wrong expected seq is plain API misuse -- no
        # race happened -- and a tail deletion also fires this error on replay. The message must
        # therefore never literally assert "a concurrent advance occurred"; it names the
        # mismatch and says the check cannot distinguish concurrency from history alteration.
        s = _store()
        with self.assertRaises(CloseRecordStaleError) as ctx:
            s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                            gapped=False, expected_last_close_seq=3)
        msg = str(ctx.exception)
        self.assertIn("cannot distinguish", msg)
        self.assertNotIn("landed first;", msg)  # the old unconditional-concurrency claim


class TestClockNow(unittest.TestCase):
    def test_clock_now_returns_the_injected_store_clock(self):
        # The CLI's future-until check (redteam finding 1) must compare against the SAME clock
        # source record_close stamps rows with -- clock_now() exposes it; an injected fixed
        # clock proves there is no second, drifting time source.
        s = _store()
        self.assertEqual(s.clock_now(), CLOCK)


class TestAppendOnly(unittest.TestCase):
    def test_public_close_surface_is_exactly_the_append_and_read_methods(self):
        # The API-shape half of the append-only claim: the ENTIRE public close-record surface on
        # BrokerStore is pinned by exact set equality -- two appenders and five verified reads,
        # nothing else. Any future mutation method (update_close/delete_close/correct_...) fails
        # this by construction rather than by substring luck. The BEHAVIORAL half -- that the
        # appenders themselves never touch a prior row -- is proven by the two SQL-trace/
        # row-identity tests below, not by this name check.
        public_close_surface = {
            name for name in dir(BrokerStore)
            if not name.startswith("_") and ("close" in name.lower() or name == "attest")
        }
        self.assertEqual(
            public_close_surface,
            {"record_close", "attest", "close_cursor", "close_gate_state",
             "close_records_snapshot",
             # the count-anchor slice's pin pair (2026-07-16) -- two plain READS (stored bytes,
             # no key, no write path), added here deliberately: still two appenders, five reads.
             "close_head", "close_count"},
        )

    def test_prior_rows_survive_untouched_across_multiple_appends(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        first_row = s._conn.execute(
            "SELECT seq, ts, action, period_since, period_until, attested_head, gapped, reason,"
            " source, hmac FROM close_records WHERE seq = 1"
        ).fetchone()

        s.attest("reviewed", expected_seq=1)
        s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                        attested_head="h2", gapped=False, expected_last_close_seq=1)

        first_row_after = s._conn.execute(
            "SELECT seq, ts, action, period_since, period_until, attested_head, gapped, reason,"
            " source, hmac FROM close_records WHERE seq = 1"
        ).fetchone()
        self.assertEqual(first_row, first_row_after)

    def test_append_issues_exactly_one_hmac_correcting_update_and_prior_rows_are_untouched(self):
        # The behavioral half of the append-only claim, traced directly against the SQL an append
        # actually executes: exactly ONE UPDATE fires (the hmac-correction step of the two-step
        # insert -- mirrors _append_seal_event), it targets close_records, it sets ONLY the hmac
        # column, and no DELETE fires at all. Combined with the byte-identical prior-row check
        # below, this pins that an append can never rewrite history -- the only UPDATE in the
        # whole flow is the one finishing the row the append itself just inserted.
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        first_row = s._conn.execute(
            "SELECT * FROM close_records WHERE seq = 1"
        ).fetchone()

        statements = []
        s._conn.set_trace_callback(statements.append)
        try:
            s.attest("reviewed", expected_seq=1)
        finally:
            s._conn.set_trace_callback(None)

        updates = [st for st in statements if st.strip().upper().startswith("UPDATE")]
        deletes = [st for st in statements if st.strip().upper().startswith("DELETE")]
        self.assertEqual(len(updates), 1, statements)
        self.assertEqual(deletes, [], statements)
        self.assertIn("close_records", updates[0])
        # The one UPDATE sets exactly the hmac column, nothing else -- one SET clause, one column.
        set_clause = updates[0].upper().split("SET", 1)[1].split("WHERE", 1)[0]
        self.assertIn("HMAC", set_clause)
        self.assertNotIn(",", set_clause)  # a single column assignment, not a multi-column rewrite

        # And the prior row survived the append byte-identical.
        first_row_after = s._conn.execute(
            "SELECT * FROM close_records WHERE seq = 1"
        ).fetchone()
        self.assertEqual(first_row, first_row_after)


class TestCompareAndAppend(unittest.TestCase):
    """Compare-and-append (Task 2 builder's finding, real): two concurrent ``close --advance``
    runs could both read the same gate_state/cursor, both pass record_close's latch-only check
    inside their own transactions, and double-write overlapping periods -- the latch check never
    validated that the CALLER'S view of the cursor was still current at write time.

    Fix: ``record_close`` requires ``expected_last_close_seq`` -- the ``last_close_seq`` from the
    ``close_gate_state()`` the caller planned its period bounds against (``None`` = "I expect no
    close record to exist yet"). Inside the same ``BEGIN IMMEDIATE``, after the fresh gate
    derivation, the CURRENT last close seq must equal it, else :class:`CloseRecordStaleError` --
    the receipts/seal head-read+append discipline applied to the cursor."""

    def test_expected_none_on_empty_store_succeeds(self):
        s = _store()
        state = s.record_close(period_since=None, period_until="2026-01-31",
                                attested_head="h1", gapped=False,
                                expected_last_close_seq=None)
        self.assertEqual(state["last_close_seq"], 1)
        self.assertEqual(s.close_cursor(), "2026-01-31")

    def test_happy_path_with_correct_expected_seq(self):
        s = _store()
        planned = s.close_gate_state()
        self.assertIsNone(planned["last_close_seq"])
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=planned["last_close_seq"])
        planned2 = s.close_gate_state()
        self.assertEqual(planned2["last_close_seq"], 1)
        s.record_close(period_since=planned2["cursor"], period_until="2026-02-28",
                        attested_head="h2", gapped=False,
                        expected_last_close_seq=planned2["last_close_seq"])
        self.assertEqual(s.close_cursor(), "2026-02-28")

    def test_stale_expected_raises_and_writes_no_row(self):
        # The exact race, simulated: caller A plans against an empty store (last_close_seq=None);
        # a concurrent advance lands its close row between A's read and A's write; A's own
        # record_close -- still carrying expected=None -- must raise and write NOTHING, because
        # A's period bounds were computed against a cursor that has since moved.
        s = _store()
        planned = s.close_gate_state()  # caller A's read: no close record yet

        # the concurrent winner lands between A's read and A's write
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)

        with self.assertRaises(CloseRecordStaleError):
            s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                            gapped=False,
                            expected_last_close_seq=planned["last_close_seq"])

        # no second row was written -- the loser's overlapping period never landed
        _, rows = s.close_records_snapshot()
        self.assertEqual(len(rows), 1)
        self.assertEqual(s.close_cursor(), "2026-01-31")

    def test_expected_none_when_a_record_exists_refuses(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)
        with self.assertRaises(CloseRecordStaleError):
            s.record_close(period_since=None, period_until="2026-02-28", attested_head="h2",
                            gapped=False, expected_last_close_seq=None)
        _, rows = s.close_records_snapshot()
        self.assertEqual(len(rows), 1)

    def test_stale_integer_refuses(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)  # seq 1
        s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                        attested_head="h2", gapped=False, expected_last_close_seq=1)  # seq 2
        with self.assertRaises(CloseRecordStaleError):
            s.record_close(period_since="2026-01-31", period_until="2026-03-31",
                            attested_head="h3", gapped=False, expected_last_close_seq=1)
        _, rows = s.close_records_snapshot()
        self.assertEqual(len(rows), 2)

    def test_latch_check_runs_before_stale_check(self):
        # Ordering pinned: a LATCHED gate refuses with CloseGateLatchedError even when the
        # caller's expected seq is ALSO stale -- the latch is the more fundamental refusal (the
        # gate is up for everyone; staleness is about this one caller's plan).
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)  # latches
        with self.assertRaises(CloseGateLatchedError):
            s.record_close(period_since=None, period_until="2026-02-28", attested_head="h2",
                            gapped=False, expected_last_close_seq=None)  # stale AND latched


class TestCloseRecordsSnapshot(unittest.TestCase):
    """One-read snapshot pattern (mirrors seal_state_snapshot) -- state + raw history rows from
    the SAME query, so a caller needing both never risks reading them across a concurrent write."""

    def test_state_matches_close_gate_state(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=True, expected_last_close_seq=None)
        state, rows = s.close_records_snapshot()
        self.assertEqual(state, s.close_gate_state())
        self.assertEqual(len(rows), 1)

    def test_is_exactly_one_select_statement(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                        gapped=False, expected_last_close_seq=None)
        statements = []
        s._conn.set_trace_callback(statements.append)
        try:
            s.close_records_snapshot()
        finally:
            s._conn.set_trace_callback(None)
        selects = [st for st in statements if st.strip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1, statements)




class TestCloseHeadCount(unittest.TestCase):
    """``close_head()``/``close_count()`` -- the (head, count) pin pair for ``close_records``,
    mirroring ``seal_head()``/``seal_count()`` byte-for-byte in posture: plain reads of stored
    values (no HMAC recompute, no key needed), so they are available on the same least-exposure
    keyless path."""

    def test_empty_table_is_none_zero(self):
        s = _store()
        self.assertIsNone(s.close_head())
        self.assertEqual(s.close_count(), 0)

    def test_head_is_latest_rows_stored_hmac_and_count_tracks(self):
        s = _store()
        s.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                       gapped=False, expected_last_close_seq=None)
        row1 = s._conn.execute(
            "SELECT hmac FROM close_records WHERE seq = 1").fetchone()[0]
        self.assertEqual(s.close_head(), row1)
        self.assertEqual(s.close_count(), 1)
        s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                       attested_head="h2", gapped=False, expected_last_close_seq=1)
        row2 = s._conn.execute(
            "SELECT hmac FROM close_records WHERE seq = 2").fetchone()[0]
        self.assertEqual(s.close_head(), row2)
        self.assertEqual(s.close_count(), 2)

    def test_keyless_open_still_reads_the_pair(self):
        # least-exposure posture: the pair is stored bytes, not a computation -- a keyless open
        # (the mint CLI path) can still take a pin.
        keyed = _store()
        keyed.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                           gapped=False, expected_last_close_seq=None)
        # reopen the same underlying db keyless: share the connection via backup into a fresh one
        import sqlite3 as _sq
        dst = _sq.connect(":memory:")
        keyed._conn.backup(dst)
        keyless = BrokerStore(dst, None, now_iso=lambda: CLOCK)
        self.assertEqual(keyless.close_count(), 1)
        self.assertEqual(keyless.close_head(), keyed.close_head())


class TestCloseAnchorPin(unittest.TestCase):
    """Off-box tail-rollback DETECTION for ``close_records`` -- the count-anchor slice
    (docs/plans/2026-07-16-close-count-anchor.md), mirroring ``test_store_seal.py::
    TestSealAnchorPin``. A pin (``close_head()`` + ``close_count()`` kept somewhere this box's
    own writer cannot reach) supplied back into ``close_gate_state()`` catches exactly what
    content-only seq-contiguity cannot: the newest close row(s) simply deleted (the silent
    cursor rollback), or the pinned row rewritten in place by a key holder. Audit-time
    detection, never prevention."""

    def _advance(self, s, until, since=None, expect=None):
        s.record_close(period_since=since, period_until=until, attested_head="h-" + until,
                       gapped=False, expected_last_close_seq=expect)

    def test_no_pin_is_identical(self):
        # Regression pin (Global constraint 1): kwargs entirely absent AND explicit None/None
        # are byte-identical to the pre-anchor derivation.
        s = _store()
        self._advance(s, "2026-01-31")
        plain = s.close_gate_state()
        explicit_none = s.close_gate_state(expected_close_head=None,
                                           expected_close_count=None)
        self.assertEqual(plain, explicit_none)
        self.assertFalse(plain["latched"])
        self.assertIsNone(plain["cause"])

    def test_tail_truncation_undetected_without_pin_but_caught_with_pin(self):
        # THE key test. Plain close_gate_state() is blind to a deleted tail (the disclosed
        # ceiling -- reproduced as the control: the cursor silently rolls back), but a pin taken
        # BEFORE the deletion catches it.
        s = _store()
        self._advance(s, "2026-01-31")
        self._advance(s, "2026-02-28", since="2026-01-31", expect=1)
        pinned_head = s.close_head()
        pinned_count = s.close_count()
        self.assertEqual(pinned_count, 2)

        s._conn.execute("DELETE FROM close_records WHERE seq = 2")
        s._conn.commit()

        # Control: the on-box blindness -- survivors contiguous, verifying, gate clean, and the
        # CURSOR HAS ROLLED BACK to January with no cause at all.
        control = s.close_gate_state()
        self.assertFalse(control["latched"])
        self.assertIsNone(control["cause"])
        self.assertEqual(control["cursor"], "2026-01-31")

        # The fix: the pin recorded before the deletion sees the count went backwards.
        fixed = s.close_gate_state(expected_close_head=pinned_head,
                                   expected_close_count=pinned_count)
        self.assertTrue(fixed["latched"])
        self.assertEqual(fixed["cause"], "anchor_behind")
        self.assertEqual(fixed["reason"],
                         "close history behind the off-box anchor (tail truncated?)")
        # rollback evidence: nothing derived from row content is surfaced
        self.assertIsNone(fixed["cursor"])
        self.assertIsNone(fixed["last_close_seq"])

    def test_divergence_at_pinned_count_is_latched(self):
        # A key HOLDER edits the row AT the pinned position in place, recomputing a valid HMAC
        # with the SAME key -- the all-row HMAC check passes (self-consistent), but the stored
        # hmac no longer matches what was pinned.
        s = _store()
        self._advance(s, "2026-01-31")
        pinned_head = s.close_head()
        pinned_count = s.close_count()

        doctored = _close_record_hmac(KEY, 1, CLOCK, "close", None, "2026-03-31",
                                      "h-2026-01-31", 0, "", "close")
        s._conn.execute(
            "UPDATE close_records SET period_until = ?, hmac = ? WHERE seq = 1",
            ("2026-03-31", doctored))
        s._conn.commit()

        control = s.close_gate_state()
        self.assertFalse(control["latched"])  # self-consistent forgery: on-box checks pass

        fixed = s.close_gate_state(expected_close_head=pinned_head,
                                   expected_close_count=pinned_count)
        self.assertTrue(fixed["latched"])
        self.assertEqual(fixed["cause"], "anchor_diverged")
        self.assertEqual(fixed["reason"],
                         "close history diverges from the off-box anchor")
        self.assertIsNone(fixed["cursor"])
        self.assertIsNone(fixed["last_close_seq"])

    def test_count_greater_than_pin_normal_append_reads_clean(self):
        s = _store()
        self._advance(s, "2026-01-31")
        pinned_head = s.close_head()
        pinned_count = s.close_count()
        self._advance(s, "2026-02-28", since="2026-01-31", expect=1)

        state = s.close_gate_state(expected_close_head=pinned_head,
                                   expected_close_count=pinned_count)
        self.assertFalse(state["latched"])
        self.assertIsNone(state["cause"])
        self.assertEqual(state["cursor"], "2026-02-28")

    def test_count_greater_than_pin_but_pinned_position_rewritten_is_latched(self):
        s = _store()
        self._advance(s, "2026-01-31")
        pinned_head = s.close_head()
        pinned_count = s.close_count()
        self._advance(s, "2026-02-28", since="2026-01-31", expect=1)

        doctored = _close_record_hmac(KEY, 1, CLOCK, "close", None, "2026-01-15",
                                      "h-2026-01-31", 0, "", "close")
        s._conn.execute(
            "UPDATE close_records SET period_until = ?, hmac = ? WHERE seq = 1",
            ("2026-01-15", doctored))
        s._conn.commit()

        state = s.close_gate_state(expected_close_head=pinned_head,
                                   expected_close_count=pinned_count)
        self.assertTrue(state["latched"])
        self.assertEqual(state["cause"], "anchor_diverged")

    def test_empty_table_with_nonzero_pin_is_latched(self):
        # A pin says two closes happened; the live table is empty -- the whole history was
        # wiped. Zero rows is the honest genesis state WITHOUT a pin (Global constraint 4 of the
        # close-record slice); WITH a contradicting pin it is a rollback.
        s = _store()
        state = s.close_gate_state(expected_close_head="ab" * 32,
                                   expected_close_count=2)
        self.assertTrue(state["latched"])
        self.assertEqual(state["cause"], "anchor_behind")

    def test_empty_table_native_pin_round_trips_cleanly(self):
        # (None, 0) is exactly what close_head()/close_count() return on a genuinely empty
        # table -- pinning those verbatim and replaying them must read clean, not malformed.
        s = _store()
        state = s.close_gate_state(expected_close_head=s.close_head(),
                                   expected_close_count=s.close_count())
        self.assertFalse(state["latched"])
        self.assertIsNone(state["cause"])

    def test_count_zero_pin_with_junk_head_is_latched_malformed(self):
        # verify pass 2026-07-16 (Item A): count 0 has no position to compare, but a count-0
        # pin whose head is neither None (the store-level native pair) nor GENESIS (the record-
        # level sentinel anchor.py enforces) is internally inconsistent -- fail closed, don't
        # silently agree. BrokerStore is public API; not every future caller routes through
        # anchor.py's record validation.
        s = _store()
        state = s.close_gate_state(expected_close_head="ab" * 32,
                                   expected_close_count=0)
        self.assertTrue(state["latched"])
        self.assertEqual(state["cause"], "anchor_malformed")

    def test_count_zero_pin_with_genesis_head_reads_clean(self):
        # the record-level sentinel (GENESIS, 0) -- exactly what a v3 pin of an empty close
        # table carries through `anchor check` -- must agree, same as the native (None, 0).
        from pacioli.prove import GENESIS
        s = _store()
        state = s.close_gate_state(expected_close_head=GENESIS,
                                   expected_close_count=0)
        self.assertFalse(state["latched"])
        self.assertIsNone(state["cause"])

    def test_count_zero_pin_vouches_for_nothing(self):
        # A count-0 pin has no position to compare -- history that GREW since is clean.
        s = _store()
        pinned_head = s.close_head()   # None
        pinned_count = s.close_count()  # 0
        self._advance(s, "2026-01-31")
        state = s.close_gate_state(expected_close_head=pinned_head,
                                   expected_close_count=pinned_count)
        self.assertFalse(state["latched"])
        self.assertIsNone(state["cause"])

    def test_partial_pin_pair_is_latched_not_raised(self):
        s = _store()
        self._advance(s, "2026-01-31")
        state = s.close_gate_state(expected_close_head=s.close_head())
        self.assertTrue(state["latched"])
        self.assertEqual(state["cause"], "anchor_malformed")
        self.assertIsNone(state["cursor"])

    def test_malformed_pin_types_are_latched_not_raised(self):
        s = _store()
        self._advance(s, "2026-01-31")
        for head, count in [(s.close_head(), True), (s.close_head(), -1),
                            (s.close_head(), "1"), (b"bytes", 1), ("", 1), (123, 1)]:
            state = s.close_gate_state(expected_close_head=head,
                                       expected_close_count=count)
            self.assertTrue(state["latched"], (head, count))
            self.assertEqual(state["cause"], "anchor_malformed", (head, count))

    def test_pin_failure_wins_over_workflow_latch_and_nulls_cursor(self):
        # gapped_awaiting_attestation keeps cursor populated (workflow latch, content verified);
        # an anchor failure does NOT -- rollback evidence outranks the workflow latch.
        s = _store()
        self._advance(s, "2026-01-31")
        pinned_head = s.close_head()
        pinned_count = s.close_count()
        s.record_close(period_since="2026-01-31", period_until="2026-02-28",
                       attested_head="h2", gapped=True, expected_last_close_seq=1)
        # workflow latch alone: cursor stays populated
        workflow = s.close_gate_state(expected_close_head=s.close_head(),
                                      expected_close_count=s.close_count())
        self.assertTrue(workflow["latched"])
        self.assertEqual(workflow["cause"], "gapped_awaiting_attestation")
        self.assertEqual(workflow["cursor"], "2026-02-28")
        # now tail-delete the gapped close; the OLD pin catches nothing (count matches again),
        # but a pin taken AFTER the gapped close does
        s._conn.execute("DELETE FROM close_records WHERE seq = 2")
        s._conn.commit()
        post_pin = s.close_gate_state(expected_close_head="cd" * 32,
                                      expected_close_count=2)
        self.assertTrue(post_pin["latched"])
        self.assertEqual(post_pin["cause"], "anchor_behind")
        self.assertIsNone(post_pin["cursor"])
        self.assertIsNone(post_pin["last_close_seq"])
        # and the pre-gap pin agrees with the survivor -- the rollback to BEFORE the gapped
        # close is invisible to it (honest ceiling: a pin only sees its own position)
        pre_pin = s.close_gate_state(expected_close_head=pinned_head,
                                     expected_close_count=pinned_count)
        self.assertFalse(pre_pin["latched"])

    def test_agreeing_pin_does_not_unlatch_integrity_failures(self):
        # an interior gap latches regardless of what the pin says -- the pin is checked ON TOP
        # of the derivation, never instead of it. The pin here names position 1, which survives
        # intact and agrees; the gap at seq 2 must still latch.
        s = _store()
        self._advance(s, "2026-01-31")
        pinned_head = s.close_head()
        pinned_count = s.close_count()  # 1
        self._advance(s, "2026-02-28", since="2026-01-31", expect=1)
        self._advance(s, "2026-03-31", since="2026-02-28", expect=2)
        s._conn.execute("DELETE FROM close_records WHERE seq = 2")  # interior deletion
        s._conn.commit()
        state = s.close_gate_state(expected_close_head=pinned_head,
                                   expected_close_count=pinned_count)
        self.assertTrue(state["latched"])
        self.assertEqual(state["cause"], "gap")

    def test_pin_disagreement_outranks_a_simultaneous_gap(self):
        # Mirror of the seal's ordering: when BOTH the derivation and the pin fail, the pin
        # cause wins (it carries the rollback evidence). An interior deletion that also drops
        # the live count below the pin reads anchor_behind, not gap.
        s = _store()
        self._advance(s, "2026-01-31")
        self._advance(s, "2026-02-28", since="2026-01-31", expect=1)
        self._advance(s, "2026-03-31", since="2026-02-28", expect=2)
        pinned_head = s.close_head()
        pinned_count = s.close_count()  # 3
        s._conn.execute("DELETE FROM close_records WHERE seq = 2")  # count now 2 < 3
        s._conn.commit()
        state = s.close_gate_state(expected_close_head=pinned_head,
                                   expected_close_count=pinned_count)
        self.assertTrue(state["latched"])
        self.assertEqual(state["cause"], "anchor_behind")

    def test_keyless_stays_latched_even_with_agreeing_pin(self):
        keyed = _store()
        keyed.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                           gapped=False, expected_last_close_seq=None)
        pinned_head, pinned_count = keyed.close_head(), keyed.close_count()
        import sqlite3 as _sq
        dst = _sq.connect(":memory:")
        keyed._conn.backup(dst)
        keyless = BrokerStore(dst, None, now_iso=lambda: CLOCK)
        state = keyless.close_gate_state(expected_close_head=pinned_head,
                                         expected_close_count=pinned_count)
        self.assertTrue(state["latched"])
        self.assertEqual(state["cause"], "keyless")

    def test_keyless_pin_disagreement_still_caught(self):
        # the pin comparison is stored-bytes vs pinned-bytes -- no key needed, so even a keyless
        # open can see a tail rollback when handed a pin.
        keyed = _store()
        keyed.record_close(period_since=None, period_until="2026-01-31", attested_head="h1",
                           gapped=False, expected_last_close_seq=None)
        import sqlite3 as _sq
        dst = _sq.connect(":memory:")
        keyed._conn.backup(dst)
        keyless = BrokerStore(dst, None, now_iso=lambda: CLOCK)
        state = keyless.close_gate_state(expected_close_head="ef" * 32,
                                         expected_close_count=4)
        self.assertTrue(state["latched"])
        self.assertEqual(state["cause"], "anchor_behind")

    def test_honest_ceiling_key_holder_rewrite_BEFORE_pinned_position_not_caught(self):
        # HONESTY pin, mirroring the seal's: close_records is per-row HMAC'd, not
        # prefix-chained, so the pin fixes ONE position -- a key holder rewriting an EARLIER row
        # (valid recomputed HMAC, seq untouched) passes both the derivation and the pin. This
        # test exists so the ceiling is a documented, pinned behavior, not an accident.
        s = _store()
        self._advance(s, "2026-01-31")
        self._advance(s, "2026-02-28", since="2026-01-31", expect=1)
        pinned_head = s.close_head()
        pinned_count = s.close_count()

        doctored = _close_record_hmac(KEY, 1, CLOCK, "close", None, "2026-01-15",
                                      "h-2026-01-31", 0, "", "close")
        s._conn.execute(
            "UPDATE close_records SET period_until = ?, hmac = ? WHERE seq = 1",
            ("2026-01-15", doctored))
        s._conn.commit()

        state = s.close_gate_state(expected_close_head=pinned_head,
                                   expected_close_count=pinned_count)
        self.assertFalse(state["latched"])  # the documented, honest limit


if __name__ == "__main__":
    unittest.main()
