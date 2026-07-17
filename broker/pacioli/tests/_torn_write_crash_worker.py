# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""NOT a test module (no ``test_`` prefix — pytest never collects this). A standalone worker
process for ``test_store_torn_write.py::TestMidTxnCrashRecovery``.

Opens a file-backed :class:`~pacioli.store.BrokerStore`, records an intent, then hand-replicates
``record_outcome``'s transaction body so a real ``time.sleep`` can be planted strictly BETWEEN the
receipt insert and the marker ``UPDATE`` — still inside the same ``BEGIN IMMEDIATE``, still before
``COMMIT``. It writes a ready-sentinel file just before sleeping so the parent test can time a real
``SIGKILL`` to land inside that exact window: after a write has touched the main db file, before
the transaction that write belongs to ever commits. That is the actual "process dies mid-write"
scenario the residual describes — this worker exists to let a test manufacture it for real rather
than assert it from documentation.
"""
import sqlite3
import sys
import time

from pacioli import consent, prove
from pacioli.consent import new_marker, reserve
from pacioli.store import BrokerStore

KEY = b"seal-key-on-box-until-increment-2"


def main(db_path, ready_path, sleep_seconds):
    conn = sqlite3.connect(db_path)
    store = BrokerStore(conn, KEY)
    store.mint_marker("tok", "p1", 1e12)
    _, _, reserved = reserve(new_marker("tok", "p1", 1e12), "tok", "p1", now=1.0)
    assert store.claim_marker(reserved)
    intent = store.record_intent({"tool": "submit", "docname": "SI-1"})

    final_marker = reserved.__class__(
        reserved.token_hash, reserved.plan_id, reserved.expires_at, "consumed"
    )

    # record_outcome's body, unrolled by hand so the sleep can sit strictly between the two writes
    # its docstring claims are atomic together (the receipt insert and the marker settle).
    body = {"finalizes": intent.seq, "status": "committed", "result": {"docstatus": 1}}
    conn.execute("BEGIN IMMEDIATE")
    r = prove.append(KEY, store._head_receipt(), prove.OUTCOME, body, ts=store._now_iso())
    store._insert(r)  # write #1: the outcome receipt lands in the main db file

    with open(ready_path, "w") as f:
        f.write("ready")
    time.sleep(sleep_seconds)  # the parent SIGKILLs us somewhere in here -- before write #2, before COMMIT

    conn.execute(  # write #2: the marker settle
        "UPDATE markers SET state=? WHERE token_hash=? AND state=?",
        (final_marker.state, final_marker.token_hash, consent.RESERVED),
    )
    conn.execute("COMMIT")

    # Only reached if we were NOT killed -- tells the parent the txn actually completed, so it can
    # tell a "killed too late" run apart from a real bug.
    with open(ready_path + ".done", "w") as f:
        f.write("done")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]))
