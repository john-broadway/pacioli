# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Torn-write / mid-txn atomicity for the store (the PROVE ledger receipts chain the whole
system's honesty rests on). Two claims, pinned for real rather than asserted from documentation:

  (a) a process that dies mid-``record_outcome`` (or a sibling store write) leaves the PREVIOUS
      valid state readable on restart -- never a half-applied write. Proven here with a real
      subprocess + a real ``SIGKILL`` landing strictly before ``COMMIT`` (see
      ``_torn_write_crash_worker.py``), not just argued from SQLite's documentation.
  (b) a corrupted/torn store file on load is a structured refusal, NOT a silent empty read (the
      ``reads-as-empty`` bug class this codebase already treats as a bug -- see TH-1/TH-2). Two
      sub-cases: a file truncated to exactly zero bytes (SQLite's own ``sqlite3.connect`` would
      otherwise treat that silently as a legitimate brand-new empty database -- the real gap this
      module closes), and a file truncated mid-page (SQLite already raises
      ``sqlite3.DatabaseError`` for this on its own -- pinned here as a regression guard, not a
      new fix).

Run: `python3 -m unittest pacioli.tests.test_store_torn_write` from the broker app root.
"""
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pacioli.store import BrokerStore, StoreCorruptError, refuse_if_torn

KEY = b"seal-key-on-box-until-increment-2"
_WORKER = str(Path(__file__).parent / "_torn_write_crash_worker.py")


class TestRefuseIfTorn(unittest.TestCase):
    """Unit-level: the guard itself, no process involved."""

    def test_nonexistent_path_passes_through(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "never-opened.db"
            refuse_if_torn(p)  # must not raise -- this is the genuine first-use case

    def test_existing_nonempty_file_passes_through(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "real.db"
            BrokerStore(sqlite3.connect(str(p)), KEY)  # writes real schema -> nonzero size
            self.assertGreater(p.stat().st_size, 0)
            refuse_if_torn(p)  # must not raise

    def test_existing_zero_byte_file_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "torn.db"
            p.touch()  # a real 0-byte file, exactly what a torn write / truncation leaves behind
            self.assertEqual(p.stat().st_size, 0)
            with self.assertRaises(StoreCorruptError) as ctx:
                refuse_if_torn(p)
            msg = str(ctx.exception).lower()
            self.assertIn(str(p).lower(), msg)  # actionable: names the file

    def test_sub_header_truncation_refused_the_one_byte_silent_destroy(self):
        # Redteam (ledger-integrity lens): a file truncated to EXACTLY 1 byte escapes both the old
        # size==0 check AND sqlite3's own corruption detection (2+ bytes -> DatabaseError, but 1
        # byte opens silently as an empty db), then the reopen's executescript DESTROYS the ledger
        # with a green verify(). A valid SQLite file is never smaller than its 100-byte header, so
        # refuse ANY existing file below that -- the mechanism, not the exact byte, is the fix.
        for size in (1, 2, 50, 99):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as d:
                p = Path(d) / "torn.db"
                p.write_bytes(b"\x00" * size)
                self.assertEqual(p.stat().st_size, size)
                with self.assertRaises(StoreCorruptError):
                    refuse_if_torn(p)

    def test_a_valid_hundred_byte_or_larger_store_still_passes(self):
        # The floor must not over-reach: a real, fully-formed store (its schema alone is far past
        # 100 bytes) is never a torn file.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "real.db"
            BrokerStore(sqlite3.connect(str(p)), KEY)
            self.assertGreaterEqual(p.stat().st_size, 100)
            refuse_if_torn(p)  # must not raise

    def test_sqlite_itself_would_have_silently_accepted_the_zero_byte_file(self):
        # The premise refuse_if_torn exists to defeat: without the guard, sqlite3.connect() on a
        # pre-existing 0-byte file succeeds and reads back as a legitimate, empty, valid database
        # -- no exception, no signal anything is wrong. This pins that premise stays true (if a
        # future sqlite3 ever started refusing 0-byte files on its own, the guard would still be
        # correct, just redundant -- worth knowing either way).
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "torn.db"
            p.touch()
            conn = sqlite3.connect(str(p))  # no raise
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            self.assertEqual(rows, [])  # reads as a fresh, empty db -- exactly the silent-empty bug


class TestOpenStoreRefusesTornFile(unittest.TestCase):
    """Integration: the guard wired into the real ``open_store`` seam every production caller
    (server, mint CLI, anchor) goes through."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = {"PACIOLI_STATE_DIR": self.dir.name}

    def tearDown(self):
        self.dir.cleanup()

    def test_first_ever_open_is_unaffected(self):
        from pacioli.runtime import open_store
        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit"})  # works normally, no false-positive refusal

    def test_reopening_a_zero_byte_torn_store_refuses_not_silently_empty(self):
        from pacioli.runtime import open_store, state_db_path

        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "docname": "SI-1"})
        db_path = state_db_path(self.dir.name, "prod")
        self.assertGreater(db_path.stat().st_size, 0)

        # simulate the torn write: something truncated the store's file to zero bytes
        with open(db_path, "r+b") as f:
            f.truncate(0)

        with self.assertRaises(StoreCorruptError):
            open_store(self.env, "prod")  # must NOT silently hand back a fresh empty store

    def test_reopening_a_one_byte_torn_store_refuses_never_destroys_the_ledger(self):
        # The critical end-to-end scenario the redteam reproduced: a 1-byte torn file, if opened,
        # reads as empty AND the reopen's executescript reinitializes the file, permanently
        # erasing the prior receipt with a clean verify(). refuse_if_torn must stop it first.
        from pacioli.runtime import open_store, state_db_path

        store = open_store(self.env, "prod")
        store.record_intent({"tool": "submit", "docname": "SI-1"})
        db_path = state_db_path(self.dir.name, "prod")
        with open(db_path, "r+b") as f:
            f.truncate(1)  # exactly one byte -- the case sqlite would otherwise open silently

        with self.assertRaises(StoreCorruptError):
            open_store(self.env, "prod")


class TestCorruptStoreOnLoadRefusesNotEmpty(unittest.TestCase):
    """The other corruption shape: a file truncated mid-page (nonzero, malformed) rather than
    wiped to zero. SQLite already refuses this loudly on its own -- pinned as a regression guard
    so nothing upstream ever starts swallowing it into a silent empty read."""

    def test_mid_page_truncation_raises_not_reads_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "half.db"
            s = BrokerStore(sqlite3.connect(str(p)), KEY)
            for i in range(50):
                s.record_intent({"n": i})
            full_size = p.stat().st_size
            self.assertGreater(full_size, 0)
            with open(p, "r+b") as f:
                f.truncate(full_size // 2)

            with self.assertRaises(sqlite3.DatabaseError):
                BrokerStore(sqlite3.connect(str(p)), KEY)


class TestCorruptReceiptBodyIsStructured(unittest.TestCase):
    """Redteam verify pass: a receipt whose JSON body is garbled -- a large store tail-truncated so
    SQLite still opens and SELECTs cleanly but an individual body no longer parses -- must surface
    as store corruption through the SAME structured handling a torn FILE gets, never a raw
    json.JSONDecodeError crashing past every deny (dispatch's catch, the CLI's). A corrupt body is
    store corruption, the same family as a torn file. `verify()` -- the integrity checker -- must
    report it as (False, reason), never crash: a chain whose bytes won't parse does not verify."""

    def _store_with_a_garbled_body(self):
        s = BrokerStore(sqlite3.connect(":memory:"), KEY, now_iso=lambda: "2026-07-01T00:00:00Z")
        s.record_intent({"doc": "SI-1"})
        # a valid row whose body column is no longer valid JSON -- exactly what a byte-level
        # corruption of the stored body produces (isolation_level=None -> this autocommits)
        s._conn.execute("UPDATE receipts SET body=? WHERE seq=0", ("{not valid json",))
        return s

    def test_verify_reports_corruption_gracefully_never_crashes(self):
        s = self._store_with_a_garbled_body()
        ok, reason = s.verify()  # must NOT raise -- verify's whole job is to report this
        self.assertFalse(ok)
        self.assertIn("corrupt", reason.lower())

    def test_verify_snapshot_reports_corruption_gracefully(self):
        s = self._store_with_a_garbled_body()
        ok, reason, receipts = s.verify_snapshot()
        self.assertFalse(ok)
        self.assertEqual(receipts, [])

    def test_receipts_raises_store_corrupt_not_a_raw_json_error(self):
        s = self._store_with_a_garbled_body()
        with self.assertRaises(StoreCorruptError):
            s.receipts()


class TestMidTxnCrashRecovery(unittest.TestCase):
    """(a): a real process killed strictly before COMMIT, mid record_outcome, must never leave a
    torn write behind -- the previous valid state stays fully readable, the half-applied write is
    simply absent (rolled back), not corrupt."""

    def test_sigkill_before_commit_leaves_prior_state_intact(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "crash.db")
            ready_path = os.path.join(d, "crash.db.ready")

            proc = subprocess.Popen([sys.executable, _WORKER, db_path, ready_path, "5"])
            try:
                deadline = time.monotonic() + 5.0
                while not os.path.exists(ready_path):
                    if time.monotonic() > deadline:
                        self.fail("worker never reached the ready sentinel")
                    time.sleep(0.01)
                time.sleep(0.05)  # be well inside the sleep window, comfortably before write #2
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

            self.assertFalse(
                os.path.exists(ready_path + ".done"),
                "worker completed its commit before being killed -- test didn't exercise the "
                "mid-txn window; widen the sleep or the kill delay",
            )

            # Reopening must not raise, must not silently look empty, and must not show a torn
            # half-applied outcome.
            store = BrokerStore(sqlite3.connect(db_path), KEY)
            receipts = store.receipts()
            self.assertEqual([r.kind for r in receipts], ["intent"])  # only the committed intent
            ok, reason = store.verify()
            self.assertTrue(ok, reason)
            self.assertEqual(store.marker_state("tok"), "reserved")  # the settle never landed

            # the store keeps working correctly after recovery -- not just readable, but writable
            r2 = store.record_intent({"after": "recovery"})
            self.assertEqual(r2.seq, 1)
            self.assertTrue(store.verify()[0])


if __name__ == "__main__":
    unittest.main()
