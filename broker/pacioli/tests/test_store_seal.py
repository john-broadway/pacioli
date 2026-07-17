# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Store-layer seal state (CONTAIN's persistence) — the ``seal_events`` table, genesis seeding,
and the ``seal``/``unseal``/``seal_state``/``seal_events`` methods on :class:`BrokerStore`.

Pinned here, for real rather than argued from docstrings:

  * A keyed store always has a genesis row (fresh OR upgraded from a pre-seal-feature store) and
    reads as unsealed until something seals it.
  * ``seal_state`` is **fail-closed**: zero rows, a ``seq`` gap (rollback), or a latest event whose
    HMAC no longer verifies (keyed) all read as SEALED, never as "must be fine".
  * The seal-event HMAC is domain-separated from the receipt HMAC (``b"seal:"`` prefix) — a real
    receipt HMAC computed under the SAME key can never pass as a seal-event HMAC.
  * A keyless store cannot seal/unseal (no key to HMAC with) but can still read ``seal_state``
    (defense-in-depth: content-only, no HMAC check).

Run: `python3 -m unittest pacioli.tests.test_store_seal` from the broker app root. No frappe
required (mirrors ``test_store.py``'s bench-free fixture: in-memory SQLite + a fixed clock).
"""
import os
import sqlite3
import tempfile
import threading
import unittest

from pacioli.store import _SCHEMA, BrokerStore

KEY = b"seal-key-on-box-until-increment-2"
CLOCK = "2026-07-14T00:00:00Z"


def _store(key=KEY):
    # a fixed clock keeps seal-event timestamps deterministic, same fixture shape as test_store.py
    return BrokerStore(sqlite3.connect(":memory:"), key, now_iso=lambda: CLOCK)


class TestGenesisSeeding(unittest.TestCase):
    def test_fresh_keyed_store_has_genesis_and_is_unsealed(self):
        s = _store()
        events = s.seal_events()
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["seq"], 1)
        self.assertEqual(e["action"], "genesis")
        self.assertEqual(e["reason"], "seal state initialized")
        self.assertEqual(e["source"], "init")
        self.assertEqual(e["ts"], CLOCK)
        self.assertTrue(e["verified"])

        state = s.seal_state()
        self.assertFalse(state["sealed"])
        self.assertEqual(state["cause"], None)
        self.assertEqual(state["seq"], 1)
        self.assertEqual(state["reason"], "seal state initialized")
        self.assertEqual(state["source"], "init")
        self.assertEqual(state["since"], CLOCK)

    def test_second_keyed_open_does_not_reseed(self):
        conn = sqlite3.connect(":memory:")
        BrokerStore(conn, KEY, now_iso=lambda: CLOCK)
        # Re-open the SAME underlying db a second time (simulates a process restart against the
        # same file) — genesis must not double-append.
        s2 = BrokerStore(conn, KEY, now_iso=lambda: CLOCK)
        self.assertEqual(len(s2.seal_events()), 1)

    def test_reopen_of_already_seeded_store_takes_no_write_lock(self):
        # Review F1: a keyed BrokerStore is constructed on EVERY MCP dispatch (tools.py's
        # ``_route`` opens a fresh keyed store per call), so an already-seeded store's reopen
        # must not pay for a ``BEGIN IMMEDIATE`` write lock just to confirm "already seeded" —
        # that would serialize every concurrent dispatch to a target at store-open time forever.
        # Trace every statement SQLite actually executes for the SECOND open only; none of them
        # may be ``BEGIN IMMEDIATE`` once the table already holds its genesis row.
        conn = sqlite3.connect(":memory:")
        BrokerStore(conn, KEY, now_iso=lambda: CLOCK)  # first open: seeds genesis

        statements = []
        conn.set_trace_callback(statements.append)
        try:
            BrokerStore(conn, KEY, now_iso=lambda: CLOCK)  # second open: already seeded
        finally:
            conn.set_trace_callback(None)

        self.assertNotIn("BEGIN IMMEDIATE", statements)

    def test_pre_seal_feature_store_gains_genesis_on_first_keyed_open(self):
        # A state db exactly as 0.19.0 created it: receipts/markers/plans exist, seal_events does
        # NOT. This is the upgrade path — CREATE TABLE IF NOT EXISTS adds the table, genesis
        # seeding fires because it starts empty.
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE receipts (
                seq INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL, kind TEXT NOT NULL,
                body TEXT NOT NULL, ts TEXT NOT NULL, hmac TEXT NOT NULL
            );
            CREATE TABLE markers (
                token_hash TEXT PRIMARY KEY, plan_id TEXT NOT NULL, expires_at REAL NOT NULL,
                state TEXT NOT NULL
            );
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY, target TEXT NOT NULL, docname TEXT NOT NULL,
                doc_version TEXT NOT NULL, posting_date TEXT NOT NULL, projected_gl TEXT NOT NULL,
                risk_flags TEXT NOT NULL, ts TEXT NOT NULL, op TEXT NOT NULL DEFAULT 'submit',
                doctype TEXT NOT NULL DEFAULT 'Sales Invoice', graph TEXT NOT NULL DEFAULT '[]',
                party_type TEXT NOT NULL DEFAULT '', party TEXT NOT NULL DEFAULT '',
                receivable_payable_account TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self.assertNotIn(
            "seal_events",
            {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")},
        )
        store = BrokerStore(conn, KEY, now_iso=lambda: CLOCK)  # opening migrates + seeds
        events = store.seal_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "genesis")
        self.assertFalse(store.seal_state()["sealed"])

    def test_keyless_open_does_not_seed(self):
        s = _store(key=None)
        self.assertEqual(s.seal_events(), [])


class TestGenesisRace(unittest.TestCase):
    def test_concurrent_keyed_opens_yield_exactly_one_genesis_row(self):
        # The race the double-checked lock in ``_seed_seal_genesis`` exists for: two DIFFERENT
        # connections to the SAME on-disk file (":memory:" can't share across connections, so
        # this needs a real file), both opening keyed at once. The outer, lock-free read can let
        # both threads see zero rows; only the re-check taken AFTER acquiring the write lock
        # stops the loser from seeding a second genesis row.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            # Pre-create the full schema (including the empty seal_events table) OUTSIDE any
            # BrokerStore construction, so genesis seeding itself — not schema creation — is the
            # thing being raced below.
            setup = sqlite3.connect(path)
            setup.executescript(_SCHEMA)
            setup.close()

            errors = []
            barrier = threading.Barrier(2)

            def open_one():
                try:
                    barrier.wait(timeout=5)
                    conn = sqlite3.connect(path, timeout=5)
                    BrokerStore(conn, KEY, now_iso=lambda: CLOCK)
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=open_one) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            final = BrokerStore(sqlite3.connect(path), KEY, now_iso=lambda: CLOCK)
            events = final.seal_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["action"], "genesis")
            self.assertFalse(final.seal_state()["sealed"])
        finally:
            os.unlink(path)


class TestSealUnseal(unittest.TestCase):
    def test_seal_makes_state_sealed(self):
        s = _store()
        result = s.seal("closing the books early")
        self.assertTrue(result["sealed"])
        self.assertEqual(result["reason"], "closing the books early")
        self.assertEqual(result["source"], "operator")
        self.assertEqual(result["seq"], 2)
        self.assertTrue(s.seal_state()["sealed"])

    def test_seal_default_source_is_operator(self):
        s = _store()
        s.seal("reason")
        e = s.seal_events()[-1]
        self.assertEqual(e["source"], "operator")

    def test_seal_accepts_explicit_source(self):
        s = _store()
        result = s.seal("gap escalated to contain", source="response")
        self.assertEqual(result["source"], "response")
        self.assertEqual(s.seal_events()[-1]["source"], "response")

    def test_seal_then_unseal_is_unsealed(self):
        s = _store()
        s.seal("closing the books early")
        result = s.unseal("books balanced, reopening")
        self.assertFalse(result["sealed"])
        self.assertEqual(result["reason"], "books balanced, reopening")
        self.assertEqual(result["source"], "operator")
        self.assertFalse(s.seal_state()["sealed"])

    def test_double_seal_is_allowed_and_recorded(self):
        s = _store()
        s.seal("first confession")
        result = s.seal("second confession")
        self.assertTrue(result["sealed"])
        events = [e for e in s.seal_events() if e["action"] == "seal"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["reason"], "first confession")
        self.assertEqual(events[1]["reason"], "second confession")

    def test_double_unseal_is_allowed_and_recorded(self):
        s = _store()
        s.unseal("still fine, first check")
        result = s.unseal("still fine, second check")
        self.assertFalse(result["sealed"])
        events = [e for e in s.seal_events() if e["action"] == "unseal"]
        self.assertEqual(len(events), 2)

    def test_keyless_cannot_seal(self):
        s = _store(key=None)
        with self.assertRaises(ValueError):
            s.seal("reason")

    def test_keyless_cannot_unseal(self):
        s = _store(key=None)
        with self.assertRaises(ValueError):
            s.unseal("reason")

    def test_keyless_can_still_read_seal_state(self):
        s = _store(key=None)
        state = s.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "no seal history")


class TestFailClosedSealState(unittest.TestCase):
    def test_zero_rows_is_sealed(self):
        s = _store()
        s._conn.execute("DELETE FROM seal_events")
        s._conn.commit()
        state = s.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "no seal history")
        self.assertEqual(state["seq"], 0)
        self.assertIsNone(state["since"])
        self.assertIsNone(state["reason"])
        self.assertIsNone(state["source"])

    def test_keyless_zero_rows_is_sealed(self):
        s = _store(key=None)
        state = s.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "no seal history")

    def test_row_deletion_gap_is_sealed(self):
        s = _store()
        s.seal("closing")  # seq 2
        s.unseal("reopening")  # seq 3 -- currently unsealed
        s._conn.execute("DELETE FROM seal_events WHERE seq = 2")
        s._conn.commit()
        state = s.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "seal history gap (rollback?)")

    def test_gap_case_nulls_the_misleading_reason_since_source(self):
        # A consumer reading only `reason` next to `sealed=True` could otherwise see the
        # SURVIVING row's own genuine "reopening" text and be misled into thinking an operator
        # explicitly reopened -- when the true explanation is a gapped history, fail-closed for a
        # completely different reason. `cause` is the ONLY authoritative explanation once it is
        # set; reason/since/source must not smuggle in a stale row's own claim next to it.
        s = _store()
        s.seal("closing")  # seq 2
        s.unseal("reopening")  # seq 3 -- would otherwise read as unsealed
        s._conn.execute("DELETE FROM seal_events WHERE seq = 2")
        s._conn.commit()
        state = s.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "seal history gap (rollback?)")
        self.assertIsNone(state["reason"])
        self.assertIsNone(state["since"])
        self.assertIsNone(state["source"])
        self.assertEqual(state["seq"], 3)  # seq itself is not row content -- not nulled

    def test_unverifiable_case_nulls_the_misleading_reason_since_source(self):
        # Same principle for the OTHER fail-closed cause: an edited/forged latest row must not
        # have its own (possibly doctored) reason/source surfaced as if trustworthy.
        s = _store()
        s.seal("closing")  # seq 2
        s.unseal("reopening")  # seq 3 -- would otherwise read as unsealed
        s._conn.execute("UPDATE seal_events SET hmac = ? WHERE seq = 3", ("0" * 64,))
        s._conn.commit()
        state = s.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "unverifiable")
        self.assertIsNone(state["reason"])
        self.assertIsNone(state["since"])
        self.assertIsNone(state["source"])
        self.assertEqual(state["seq"], 3)

    def test_tampered_latest_hmac_is_sealed(self):
        s = _store()
        s.seal("closing")  # seq 2
        s.unseal("reopening")  # seq 3 -- would otherwise read as unsealed
        s._conn.execute(
            "UPDATE seal_events SET hmac = ? WHERE seq = 3", ("0" * 64,)
        )
        s._conn.commit()
        state = s.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "unverifiable")

    def test_deleted_seq_is_never_reused_gap_survives_further_appends(self):
        # The sharper version of the rollback attack: delete the newest row, THEN append more
        # events hoping the deleted number gets silently recycled and the history looks clean
        # again. AUTOINCREMENT must refuse to reuse it -- if it didn't, this would "heal" the gap
        # and defeat seal_state's whole rollback defense.
        s = _store()
        s.seal("closing")  # seq 2
        s.unseal("reopening")  # seq 3
        s._conn.execute("DELETE FROM seal_events WHERE seq = 3")
        s._conn.commit()
        s.seal("closing again")  # would be seq 3 if reused, must be seq 4
        seqs = [e["seq"] for e in s.seal_events()]
        self.assertEqual(seqs, [1, 2, 4])
        state = s.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "seal history gap (rollback?)")

    def test_keyless_gap_still_detected(self):
        # Defense-in-depth: contiguity is content-only, so it must still catch a gap keyless.
        conn = sqlite3.connect(":memory:")
        keyed = BrokerStore(conn, KEY, now_iso=lambda: CLOCK)
        keyed.seal("closing")
        keyed.unseal("reopening")
        conn.execute("DELETE FROM seal_events WHERE seq = 2")
        conn.commit()
        keyless = BrokerStore(conn, None, now_iso=lambda: CLOCK)
        state = keyless.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "seal history gap (rollback?)")

    def test_keyless_cannot_detect_in_place_edit(self):
        # Honest limit, pinned rather than just asserted in prose: a keyless open trusts row
        # CONTENT, so an edited (but structurally intact, contiguous) row is NOT caught here —
        # only the keyed HMAC check catches that. Document the ceiling; don't claim otherwise.
        conn = sqlite3.connect(":memory:")
        keyed = BrokerStore(conn, KEY, now_iso=lambda: CLOCK)
        keyed.seal("closing")
        conn.execute("UPDATE seal_events SET reason = 'forged' WHERE seq = 2")
        conn.commit()
        keyless = BrokerStore(conn, None, now_iso=lambda: CLOCK)
        state = keyless.seal_state()
        self.assertTrue(state["sealed"])  # still sealed, but for the RIGHT reason (action=seal)
        self.assertIsNone(state["cause"])
        self.assertEqual(state["reason"], "forged")

    def test_keyed_interior_row_content_edit_is_unverifiable(self):
        # F1(a) (security redteam 2026-07-15): before this fix, seal_state's KEYED path only
        # recomputed the LATEST row's HMAC — a keyless attacker with DB-file write access could
        # rewrite an INTERIOR row's content (flip a past `seal`->`unseal`, launder a reason) and
        # leave its stored hmac untouched (no key to recompute a valid one), and the rewrite read
        # CLEAN (cause=None) because seal_state never looked at that row again once a later row
        # existed. This is the keyed-open counterpart to
        # test_keyless_cannot_detect_in_place_edit above: SAME tamper, but on a KEYED read this
        # must now fail closed, because every row's HMAC is checked, not only the latest's.
        s = _store()
        s.seal("closing")       # seq 2 -- the row that gets tampered below
        s.unseal("reopening")   # seq 3 -- latest; its own HMAC is untouched and still verifies
        s._conn.execute("UPDATE seal_events SET reason = 'forged' WHERE seq = 2")  # hmac left as-is
        s._conn.commit()
        state = s.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "unverifiable")
        self.assertIsNone(state["reason"])
        self.assertIsNone(state["since"])
        self.assertIsNone(state["source"])
        self.assertEqual(state["seq"], 3)  # the latest surviving row's own seq, not nulled

    def test_keyed_interior_row_edit_on_action_flips_seal_to_unseal_is_still_caught(self):
        # The sharpest version of the F1(a) attack: flip a PAST `seal` to `unseal` (laundering a
        # confession), not just its reason text. Content changed, hmac untouched -- must still
        # fail closed on a keyed read, not merely read as if the flip were legitimate.
        s = _store()
        s.seal("closing early")  # seq 2
        s.seal("closing again")  # seq 3 -- latest, untouched
        s._conn.execute("UPDATE seal_events SET action = 'unseal' WHERE seq = 2")
        s._conn.commit()
        state = s.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "unverifiable")

    def test_legitimate_multi_row_history_still_reads_clean_after_f1a(self):
        # Regression pin: F1(a)'s all-row check must not false-positive on an untampered,
        # multi-row, multi-action history -- every row here has a genuine, matching hmac.
        s = _store()
        s.seal("a")
        s.unseal("b")
        s.seal("c")
        s.unseal("d")
        state = s.seal_state()
        self.assertFalse(state["sealed"])
        self.assertIsNone(state["cause"])
        self.assertEqual(state["reason"], "d")


class TestDomainSeparation(unittest.TestCase):
    def test_receipt_hmac_cannot_forge_seal_event_hmac(self):
        s = _store()
        # A real receipt, sealed under the SAME key, at the SAME clock tick as the genesis event.
        receipt = s.record_intent({"doc": "A"})
        genesis = s.seal_events()[0]
        self.assertEqual(genesis["ts"], receipt.ts)  # same clock tick -- the interesting case

        # Replay the receipt's legitimate HMAC into the seal_events row as if it belonged there.
        s._conn.execute(
            "UPDATE seal_events SET hmac = ? WHERE seq = ?", (receipt.hmac, genesis["seq"])
        )
        s._conn.commit()

        state = s.seal_state()
        self.assertTrue(state["sealed"])
        self.assertEqual(state["cause"], "unverifiable")


class TestSealEventsHistory(unittest.TestCase):
    def test_ordering_is_oldest_first(self):
        s = _store()
        s.seal("a")
        s.unseal("b")
        events = s.seal_events()
        self.assertEqual([e["seq"] for e in events], [1, 2, 3])
        self.assertEqual([e["action"] for e in events], ["genesis", "seal", "unseal"])

    def test_verified_true_for_untampered_keyed_history(self):
        s = _store()
        s.seal("a")
        s.unseal("b")
        self.assertTrue(all(e["verified"] is True for e in s.seal_events()))

    def test_verified_none_for_keyless_history(self):
        conn = sqlite3.connect(":memory:")
        keyed = BrokerStore(conn, KEY, now_iso=lambda: CLOCK)
        keyed.seal("a")
        keyless = BrokerStore(conn, None, now_iso=lambda: CLOCK)
        events = keyless.seal_events()
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["verified"] is None for e in events))

    def test_verified_false_for_tampered_row_not_just_latest(self):
        s = _store()
        s.seal("a")
        s.unseal("b")
        s._conn.execute("UPDATE seal_events SET reason = 'x' WHERE seq = 2")  # tamper the middle
        s._conn.commit()
        events = s.seal_events()
        flags = {e["seq"]: e["verified"] for e in events}
        self.assertFalse(flags[2])
        self.assertTrue(flags[1])
        self.assertTrue(flags[3])

    def test_fixed_clock_timestamps_on_seal_and_unseal(self):
        s = _store()
        s.seal("a")
        s.unseal("b")
        self.assertTrue(all(e["ts"] == CLOCK for e in s.seal_events()))


class TestSealHeadAndCount(unittest.TestCase):
    """``seal_head()``/``seal_count()`` -- the seal table's own (head, count) pair, mirroring
    ``head()`` on the receipt side. This is the raw pin surface the anchor task (Task 2) will
    wire into ``pacioli anchor write``; here it is just exposed and proven correct."""

    def test_empty_table_head_is_none_and_count_is_zero(self):
        s = _store()
        s._conn.execute("DELETE FROM seal_events")
        s._conn.commit()
        self.assertIsNone(s.seal_head())
        self.assertEqual(s.seal_count(), 0)

    def test_head_is_latest_row_hmac(self):
        s = _store()
        genesis = s.seal_events()[0]
        self.assertEqual(s.seal_head(), genesis["hmac"])
        s.seal("closing")
        latest = s.seal_events()[-1]
        self.assertEqual(s.seal_head(), latest["hmac"])
        self.assertNotEqual(s.seal_head(), genesis["hmac"])

    def test_count_tracks_row_count(self):
        s = _store()
        self.assertEqual(s.seal_count(), 1)  # genesis
        s.seal("closing")
        self.assertEqual(s.seal_count(), 2)
        s.unseal("reopening")
        self.assertEqual(s.seal_count(), 3)

    def test_keyless_can_read_head_and_count(self):
        # Same least-exposure posture as seal_state() itself: no key needed to read these.
        conn = sqlite3.connect(":memory:")
        keyed = BrokerStore(conn, KEY, now_iso=lambda: CLOCK)
        keyed.seal("closing")
        keyless = BrokerStore(conn, None, now_iso=lambda: CLOCK)
        self.assertEqual(keyless.seal_head(), keyed.seal_head())
        self.assertEqual(keyless.seal_count(), keyed.seal_count())


class TestSealStateSnapshot(unittest.TestCase):
    """``seal_state_snapshot()`` (F3, correctness redteam 2026-07-15) -- ONE consistent read of
    (state, head, count) together, mirroring ``verify_snapshot()`` on the receipt side. The bug
    this closes: ``cmd_anchor_write`` used to build the seal half of a v2 pin from THREE separate
    reads (``seal_state()`` / ``seal_head()`` / ``seal_count()``), each its own query -- a
    concurrent writer landing between any two of them could pair a stale derivation with a fresh
    head/count (or vice versa), emitting a self-inconsistent pin that later false-alarms
    "diverges from the off-box anchor" against untouched history."""

    def test_shape_matches_the_three_separate_accessors_on_a_quiet_store(self):
        s = _store()
        s.seal("closing")
        state, head, count = s.seal_state_snapshot()
        self.assertEqual(state, s.seal_state())
        self.assertEqual(head, s.seal_head())
        self.assertEqual(count, s.seal_count())

    def test_empty_table_returns_none_head_zero_count(self):
        s = _store(key=None)
        state, head, count = s.seal_state_snapshot()
        self.assertIsNone(head)
        self.assertEqual(count, 0)
        self.assertEqual(state["cause"], "no seal history")

    def test_is_exactly_one_select_statement(self):
        # The actual fix, proven directly rather than inferred: one fetch, not three. Nothing can
        # "land between" reads that never happen.
        s = _store()
        s.seal("closing")
        statements = []
        s._conn.set_trace_callback(statements.append)
        try:
            s.seal_state_snapshot()
        finally:
            s._conn.set_trace_callback(None)
        selects = [st for st in statements if st.strip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1, statements)

    def test_RED_three_separate_reads_can_yield_a_self_inconsistent_triple(self):
        # RED / documentation: reproduces the OLD `cmd_anchor_write` bug directly against
        # BrokerStore's own three separate accessors (`seal_state()` then `seal_head()` then
        # `seal_count()`) -- a concurrent writer (another CLI invocation, or an auto-CONTAIN
        # `close --respond`) landing between them can pair a STALE state (read before the write)
        # with a FRESH head/count (read after it). This is the exact race
        # `seal_state_snapshot()` closes (see the class below / `cmd_anchor_write`); kept here as
        # a permanent pin on WHY the fix exists, not a test of current `cmd_anchor_write` (which
        # no longer calls these three separately -- see TestSealSnapshotWiredIntoAnchorWrite in
        # test_anchor.py).
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            writer = BrokerStore(sqlite3.connect(path), KEY, now_iso=lambda: CLOCK)  # seeds seq 1
            reader = BrokerStore(sqlite3.connect(path), KEY, now_iso=lambda: CLOCK)

            state = reader.seal_state()  # read #1 -- sees count 1 (genesis only), unsealed
            writer.seal("closing")       # a "concurrent" writer lands HERE -- seq 2, now sealed
            head = reader.seal_head()    # read #2 -- sees the NEW head (seq 2)
            count = reader.seal_count()  # read #3 -- sees the NEW count (2)

            # The bug: `state` describes the world BEFORE the write (unsealed, seq 1), but
            # `head`/`count` describe the world AFTER it (seq 2). A pin built from this triple
            # pairs a stale derivation with a head/count that do not correspond to it --
            # `state["seq"]` (what was actually derived) disagrees with `count` (what was
            # actually pinned).
            self.assertFalse(state["sealed"])
            self.assertEqual(state["seq"], 1)
            self.assertEqual(count, 2)
            self.assertNotEqual(state["seq"], count)  # the inconsistency this fix closes
        finally:
            os.unlink(path)

    def test_GREEN_snapshot_triple_is_always_self_consistent_across_the_same_race(self):
        # Same race timing as the RED test above, but through `seal_state_snapshot()`: since it
        # is ONE `SELECT`, the concurrent writer's append either landed before the read (and the
        # snapshot reflects the new state fully) or after it (and the snapshot reflects the old
        # state fully) -- never a mix. Either outcome is internally consistent; the ONE case that
        # can never happen is `state["seq"] != count`.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            writer = BrokerStore(sqlite3.connect(path), KEY, now_iso=lambda: CLOCK)
            reader = BrokerStore(sqlite3.connect(path), KEY, now_iso=lambda: CLOCK)

            writer.seal("closing")  # seq 2, sealed -- happens BEFORE the one snapshot read below
            state, head, count = reader.seal_state_snapshot()

            self.assertEqual(state["seq"], count)  # always true for a single-read snapshot
            self.assertEqual(head, reader.seal_head())
            self.assertTrue(state["sealed"])
            self.assertEqual(count, 2)
        finally:
            os.unlink(path)


class TestSealAnchorPin(unittest.TestCase):
    """Off-box tail-rollback DETECTION for the seal table -- mirrors
    ``verify(expected_head=...)`` on the receipt side (``test_store.py::TestReceiptAppend::
    test_truncation_detected_with_pinned_head``). A pin (``seal_head()`` + ``seal_count()`` taken
    at some point and kept somewhere this box's own writer cannot reach) supplied back into
    ``seal_state()`` catches exactly what content-only, on-box seq-contiguity cannot: the newest
    row(s) simply deleted, or an earlier row rewritten in place by a key holder. This is
    audit-time detection, gated by the caller actually supplying the pin -- never real-time
    prevention; nothing here blocks a write."""

    def test_no_pin_is_0_20_0_identical(self):
        # Regression pin: the new keyword args entirely absent (both the implicit default AND an
        # explicit None/None) must be byte-identical to the pre-anchor derivation.
        s = _store()
        s.seal("closing")
        plain = s.seal_state()
        explicit_none = s.seal_state(expected_seal_head=None, expected_seal_count=None)
        self.assertEqual(plain, explicit_none)
        self.assertFalse(plain["cause"])
        self.assertTrue(plain["sealed"])

    def test_tail_truncation_undetected_without_pin_but_caught_with_pin(self):
        # THE key test. Plain seal_state() is blind to a deleted tail -- the redteam finding,
        # reproduced here as the control -- but a pin taken BEFORE the deletion catches it.
        s = _store()
        s.seal("closing")  # seq 2 -- now sealed
        pinned_head = s.seal_head()
        pinned_count = s.seal_count()
        self.assertEqual(pinned_count, 2)

        s._conn.execute("DELETE FROM seal_events WHERE seq = 2")  # tail-delete the newest row
        s._conn.commit()

        # Control: the on-box blindness the redteam found -- genesis is now "latest", reads
        # UNSEALED with no cause at all (a clean, un-gapped, verifying history).
        control = s.seal_state()
        self.assertFalse(control["sealed"])
        self.assertIsNone(control["cause"])

        # The fix: pinning the head+count recorded before the deletion catches it.
        fixed = s.seal_state(expected_seal_head=pinned_head, expected_seal_count=pinned_count)
        self.assertTrue(fixed["sealed"])
        self.assertEqual(fixed["cause"],
                         "seal history behind the off-box anchor (tail truncated?)")
        self.assertIsNone(fixed["since"])
        self.assertIsNone(fixed["reason"])
        self.assertIsNone(fixed["source"])
        self.assertEqual(fixed["seq"], 1)  # the surviving genesis row's own seq -- not nulled

    def test_divergence_at_pinned_count_is_sealed(self):
        # A key-holding attacker edits the row AT the pinned position in place, recomputing a
        # valid HMAC with the SAME key -- count is unchanged, and the latest-row-only HMAC check
        # alone would pass (self-consistent), but the content no longer matches what was pinned.
        s = _store()
        s.seal("closing")  # seq 2
        pinned_head = s.seal_head()
        pinned_count = s.seal_count()
        self.assertEqual(pinned_count, 2)

        from pacioli.store import _seal_event_hmac
        new_mac = _seal_event_hmac(KEY, 2, CLOCK, "unseal", "forged reopen", "operator")
        s._conn.execute(
            "UPDATE seal_events SET action=?, reason=?, source=?, hmac=? WHERE seq=2",
            ("unseal", "forged reopen", "operator", new_mac),
        )
        s._conn.commit()

        # Without the pin, the (self-consistent) forged content reads as legitimately unsealed --
        # the latest-row HMAC check has nothing to object to.
        control = s.seal_state()
        self.assertFalse(control["sealed"])
        self.assertIsNone(control["cause"])

        fixed = s.seal_state(expected_seal_head=pinned_head, expected_seal_count=pinned_count)
        self.assertTrue(fixed["sealed"])
        self.assertEqual(fixed["cause"], "seal history diverges from the off-box anchor")
        self.assertIsNone(fixed["since"])
        self.assertIsNone(fixed["reason"])
        self.assertIsNone(fixed["source"])

    def test_count_greater_than_pin_normal_append_still_reads_correct(self):
        # New, legitimate events since the pin -- normal operation. The pin only vouches for
        # history up to its own count; when the pinned position still agrees, the CURRENT (grown)
        # state is reported honestly, not forced sealed just because the chain grew.
        s = _store()
        pinned_head = s.seal_head()  # genesis, seq 1
        pinned_count = s.seal_count()  # 1
        s.seal("closing")  # seq 2 -- happens AFTER the pin was taken, legitimately

        result = s.seal_state(expected_seal_head=pinned_head, expected_seal_count=pinned_count)
        self.assertTrue(result["sealed"])  # reflects the CURRENT state honestly
        self.assertIsNone(result["cause"])
        self.assertEqual(result["reason"], "closing")
        self.assertEqual(result["seq"], 2)

    def test_count_greater_than_pin_but_pinned_position_rewritten_is_sealed(self):
        # The belt: even though the chain grew normally since the pin, if the row AT the pinned
        # position no longer matches (a key holder rewrote history below/at the pin), that is
        # caught even though it is no longer "latest" and the plain derivation never looks at it
        # again.
        s = _store()
        pinned_head = s.seal_head()  # genesis, seq 1
        pinned_count = s.seal_count()  # 1
        s.seal("closing")  # seq 2 -- the chain grows normally after the pin

        from pacioli.store import _seal_event_hmac
        new_mac = _seal_event_hmac(KEY, 1, CLOCK, "genesis", "rewritten", "init")
        s._conn.execute(
            "UPDATE seal_events SET reason=?, hmac=? WHERE seq=1",
            ("rewritten", new_mac),
        )
        s._conn.commit()

        result = s.seal_state(expected_seal_head=pinned_head, expected_seal_count=pinned_count)
        self.assertTrue(result["sealed"])
        self.assertEqual(result["cause"], "seal history diverges from the off-box anchor")
        self.assertIsNone(result["since"])
        self.assertIsNone(result["reason"])
        self.assertIsNone(result["source"])

    def test_empty_table_with_pin_is_sealed(self):
        s = _store()
        s._conn.execute("DELETE FROM seal_events")
        s._conn.commit()
        result = s.seal_state(expected_seal_head="a" * 64, expected_seal_count=1)
        self.assertTrue(result["sealed"])
        self.assertEqual(result["cause"],
                         "seal history behind the off-box anchor (tail truncated?)")

    def test_partial_pin_pair_is_sealed_not_raised(self):
        # Deny-biased: a malformed pin (only one of head/count supplied) must never raise --
        # fold into the conservative/sealed side, same posture as every other unreadable input.
        s = _store()
        result = s.seal_state(expected_seal_head="a" * 64, expected_seal_count=None)
        self.assertTrue(result["sealed"])
        self.assertIsNotNone(result["cause"])

        result2 = s.seal_state(expected_seal_head=None, expected_seal_count=1)
        self.assertTrue(result2["sealed"])
        self.assertIsNotNone(result2["cause"])

    def test_malformed_pin_types_are_sealed_not_raised(self):
        # A pin of the wrong type (e.g. bytes where hmac.compare_digest needs matching str/str or
        # bytes/bytes, or a negative count) must never crash seal_state -- fold into sealed.
        s = _store()
        self.assertTrue(
            s.seal_state(expected_seal_head=b"not-a-str", expected_seal_count=1)["sealed"])
        self.assertTrue(
            s.seal_state(expected_seal_head="a" * 64, expected_seal_count=-1)["sealed"])
        self.assertTrue(
            s.seal_state(expected_seal_head="a" * 64, expected_seal_count=True)["sealed"])

    def test_empty_table_native_pin_round_trips_cleanly(self):
        # seal_head()/seal_count() themselves return (None, 0) on a genuinely empty table (a
        # keyless store that never seeded genesis). Replaying that exact pair back into
        # seal_state() must NOT be misclassified as a malformed pin -- it is the legitimate
        # "nothing sealed yet" pin, mirroring the receipt side's GENESIS/count==0 pairing, and
        # must defer to the honest "no seal history" cause the base derivation already gives.
        s = _store(key=None)
        self.assertIsNone(s.seal_head())
        self.assertEqual(s.seal_count(), 0)
        result = s.seal_state(expected_seal_head=s.seal_head(),
                              expected_seal_count=s.seal_count())
        self.assertTrue(result["sealed"])
        self.assertEqual(result["cause"], "no seal history")

    def test_keyless_pin_check_still_works(self):
        # seal_state() is readable keyless (content-only); the pin check operates on the same
        # rows regardless of key, so it must still catch a tail truncation with no key in reach.
        conn = sqlite3.connect(":memory:")
        keyed = BrokerStore(conn, KEY, now_iso=lambda: CLOCK)
        keyed.seal("closing")
        pinned_head = keyed.seal_head()
        pinned_count = keyed.seal_count()
        conn.execute("DELETE FROM seal_events WHERE seq = 2")
        conn.commit()

        keyless = BrokerStore(conn, None, now_iso=lambda: CLOCK)
        result = keyless.seal_state(expected_seal_head=pinned_head,
                                    expected_seal_count=pinned_count)
        self.assertTrue(result["sealed"])
        self.assertEqual(result["cause"],
                         "seal history behind the off-box anchor (tail truncated?)")

    def test_honest_ceiling_key_holder_rewrite_BEFORE_the_pinned_position_is_not_caught(self):
        # F1(b) (security redteam 2026-07-15): the docstrings/README used to claim the pin
        # catches "a rewrite at-or-before the pinned position" and called the ceiling "identical
        # to receipts". Both were false — seal_events has no prefix-chaining (unlike the receipt
        # chain's prev_hash), so the pin's exact-hmac comparison protects ONLY the single row it
        # names by position, never anything earlier. This test proves the corrected, honest claim
        # behaviorally: a key-HOLDING attacker rewrites a row strictly BEFORE the pinned position
        # (not the pinned row itself) with a freshly self-consistent HMAC (the same key) — this
        # must NOT be caught, by either the all-row keyed check (F1(a): the recomputed hmac is
        # genuinely valid for the new content) or the pin (it only compares the exact pinned-
        # position row). If this test ever starts failing (the rewrite becomes detected), the
        # docstrings must be re-examined — that would mean the ceiling improved and the honest
        # claim should be updated to match, not weakened back.
        s = _store()
        s.seal("closing")     # seq 2 -- this becomes the PINNED position
        pinned_head = s.seal_head()
        pinned_count = s.seal_count()
        self.assertEqual(pinned_count, 2)
        s.unseal("reopening")  # seq 3 -- grows the history past the pin, legitimately

        # The key holder rewrites seq 1 (genesis) -- strictly BEFORE the pinned seq 2 -- with a
        # fresh, self-consistent HMAC computed under the SAME real key.
        from pacioli.store import _seal_event_hmac
        new_mac = _seal_event_hmac(KEY, 1, CLOCK, "genesis", "rewritten by a key holder", "init")
        s._conn.execute(
            "UPDATE seal_events SET reason=?, hmac=? WHERE seq=1",
            ("rewritten by a key holder", new_mac),
        )
        s._conn.commit()

        # Neither the plain (unpinned) derivation nor the all-row keyed check objects: seq 1's
        # hmac is genuinely valid for its new content.
        control = s.seal_state()
        self.assertFalse(control["sealed"])
        self.assertIsNone(control["cause"])

        # The pin (seq 2, taken before the rewrite) does not name seq 1, so it never looks at it
        # either — the honest ceiling this fix documents, not a regression.
        result = s.seal_state(expected_seal_head=pinned_head, expected_seal_count=pinned_count)
        self.assertFalse(result["sealed"])
        self.assertIsNone(result["cause"])


if __name__ == "__main__":
    unittest.main()
