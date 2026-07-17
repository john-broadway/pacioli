# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — the SQLite-backed persistence (glue) that makes the pure cores' delegated
guarantees real:

  * **Atomic marker claim** — ``claim_marker`` is a single ``UPDATE ... WHERE state='live'``; SQLite
    makes the compare-and-swap atomic, so only one concurrent caller transitions ``live → reserved``.
  * **Serialized append** — ``record_intent`` / ``record_outcome`` open ``BEGIN IMMEDIATE`` before
    reading the chain head, so the head-read-then-append critical section holds the write lock for its
    whole duration; a concurrent appender waits (``busy_timeout``) rather than reading a stale head and
    losing an ``INSERT`` to a ``seq`` collision. The chain can't fork *or* raise on ordinary load.
  * **Atomic settle** — ``record_outcome`` appends the outcome receipt AND updates the marker state in
    the *same* transaction, so a posting's proof and its consent-spend commit together or not at all.
  * **Off-box head** — ``head()`` exposes the chain head for external pinning (``pacioli anchor
    write`` records it); ``verify(expected_head)`` then catches tail-truncation/wipe — the on-box
    gap the off-box pin closes, *since the last pin*.
  * **Fail-closed seal state (a FORWARD control, not rollback-resistant)** — ``seal_events`` is an
    append-only, HMAC'd history (domain-separated from receipts — see :func:`_seal_event_hmac`) of
    the broker's own decision to CONTAIN. ``seal_state`` derives the current answer from that
    history rather than from a bare flag: an interior row deletion (a ``seq`` gap), a keyless
    in-place edit of ANY row — interior or latest, not only the latest's (a keyed HMAC mismatch;
    every row is recomputed and checked — 2026-07-15, security redteam F1(a)), or a forged
    history with no key at all ALL read as SEALED, never as an accidental "must be fine". This is
    per-row keyed HMAC integrity, NOT prefix-chaining like the receipt chain's ``prev_hash`` —
    each seal-event row's HMAC covers only its own content, so a KEY-HOLDING attacker can still
    forge a self-consistent rewrite of any row other than the one an off-box pin names by
    position (see :meth:`BrokerStore.seal_state`'s "Honest tamper ceiling" for the exact,
    non-overclaimed scope). Content-only derivation (no pin supplied) is
    NOT rollback-resistant against a keyless attacker with DB-file write access who deletes the
    NEWEST ``seal_events`` row(s) (tail truncation): seq-contiguity cannot see a missing tail, so a
    genuine earlier row can become "latest" and read as a legitimate — possibly unsealed — state.
    This is the SAME on-box limit the receipt chain has always disclosed above; ``seal_head()``/
    ``seal_count()`` now expose the SAME (head, count) pin surface for this table, and
    ``seal_state(expected_seal_head=..., expected_seal_count=...)`` catches a rollback against a
    pin recorded off-box — **audit-time DETECTION, gated by the caller supplying the pin, never
    real-time prevention** (see :meth:`BrokerStore.seal_state`'s own docstring for the exact
    mechanism and its honest ceiling). A keyed store seeds an unsealed genesis row at first open —
    both a brand-new store and one upgraded from a pre-seal version (see
    :meth:`BrokerStore._seed_seal_genesis`).

This is glue: it holds a clock and the seal key. The key is on-box — documented, not hidden; the
off-box head pin bounds what a key-holder can forge to receipts appended after the last pin, it
does not remove the exposure (SPEC §5). Receipt bodies must be JSON-native (``prove.append``
enforces it); the caller normalises an ERPNext result before it reaches ``record_outcome``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pacioli.prove import GENESIS

from pacioli import consent, prove
from pacioli.plan import Plan
from pacioli.prove import Receipt

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    seq       INTEGER PRIMARY KEY,
    prev_hash TEXT NOT NULL,
    kind      TEXT NOT NULL,
    body      TEXT NOT NULL,
    ts        TEXT NOT NULL,
    hmac      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS markers (
    token_hash TEXT PRIMARY KEY,
    plan_id    TEXT NOT NULL,
    expires_at REAL NOT NULL,
    state      TEXT NOT NULL
);
-- One live grant per plan: a second mint while one is outstanding is a mistake, not a feature.
CREATE UNIQUE INDEX IF NOT EXISTS one_live_marker_per_plan ON markers(plan_id) WHERE state='live';
CREATE TABLE IF NOT EXISTS plans (
    plan_id      TEXT PRIMARY KEY,
    target       TEXT NOT NULL,
    docname      TEXT NOT NULL,
    doc_version  TEXT NOT NULL,
    posting_date TEXT NOT NULL,
    projected_gl TEXT NOT NULL,
    risk_flags   TEXT NOT NULL,
    ts           TEXT NOT NULL,
    op           TEXT NOT NULL DEFAULT 'submit',
    doctype      TEXT NOT NULL DEFAULT 'Sales Invoice',
    graph        TEXT NOT NULL DEFAULT '[]',
    party_type   TEXT NOT NULL DEFAULT '',
    party        TEXT NOT NULL DEFAULT '',
    receivable_payable_account TEXT NOT NULL DEFAULT '',
    company      TEXT NOT NULL DEFAULT ''
);
-- Append-only history of the broker's own decision to CONTAIN. ``CREATE TABLE IF NOT EXISTS``
-- means a store that predates this feature (0.19.0 and earlier) gains this table with zero rows
-- the next time it is opened -- no ALTER migration needed, only genesis seeding (see
-- BrokerStore._seed_seal_genesis). AUTOINCREMENT keeps `seq` monotonically increasing across
-- deletes (never reused), which is what makes a gap in 1..N mean "a row is gone" rather than
-- "SQLite recycled a freed rowid" -- load-bearing for seal_state's INTERIOR-gap detection (tail
-- truncation -- deleting the newest row(s), leaving survivors contiguous -- is a different,
-- undetected case; see seal_state's own docstring for the honest ceiling).
CREATE TABLE IF NOT EXISTS seal_events (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    hmac   TEXT NOT NULL
);
-- Append-only history of the broker's own decision to CLOSE a period / ATTEST a gapped one (Half
-- 3, Fork A1) -- the store-side half of the attestation gate. Same ``CREATE TABLE IF NOT EXISTS``
-- + AUTOINCREMENT discipline as seal_events (see that table's comment): a pre-0.23.0 store gains
-- this table empty, with NO genesis seeding (unlike seal_events) -- zero rows is the HONEST
-- genesis state for a cursor ("no period has ever closed"), not a fail-closed case; see
-- BrokerStore._derive_close_gate_state for the full, deliberately-diverging-from-seal derivation.
-- period_since/period_until/attested_head are nullable: NULL is "open-ended / not applicable"
-- (an `attest` row has no period bounds at all), a DIFFERENT fact from an explicit empty string --
-- see _close_record_canonical, which never collapses the two.
CREATE TABLE IF NOT EXISTS close_records (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    action        TEXT NOT NULL,
    period_since  TEXT,
    period_until  TEXT,
    attested_head TEXT,
    gapped        INTEGER NOT NULL DEFAULT 0,
    reason        TEXT NOT NULL,
    source        TEXT NOT NULL,
    hmac          TEXT NOT NULL
);
"""


def _migrate_plans_op(conn):
    """Schema evolution for a pre-UNDO state db: add the ``op`` column when it is missing.
    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so an installed store keeps
    its old shape until this runs. The default backfills history honestly: every plan recorded
    before UNDO existed WAS a submit plan."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
    if cols and "op" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN op TEXT NOT NULL DEFAULT 'submit'")


def _migrate_plans_doctype(conn):
    """Schema evolution for a pre-breadth state db: add the ``doctype`` column when it is
    missing. Same shape as :func:`_migrate_plans_op` one column later — the default backfills
    history honestly: every plan recorded before Purchase Invoice breadth existed WAS a Sales
    Invoice plan. Must run AFTER ``_migrate_plans_op`` so a db older than *both* columns picks
    them up in the right order (op first, then doctype) rather than failing on a stale table
    shape."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
    if cols and "doctype" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN doctype TEXT NOT NULL DEFAULT 'Sales Invoice'")


def _migrate_plans_graph(conn):
    """Schema evolution for a pre-cascade_cancel state db: add the ``graph`` column when it is
    missing. Same shape as :func:`_migrate_plans_doctype` one column later — the default
    backfills history honestly: every plan recorded before cascade_cancel existed had no graph.
    Must run AFTER ``_migrate_plans_op``/``_migrate_plans_doctype`` so a db older than all three
    columns picks them up in the right order. The ``cols and`` guard (matching both siblings)
    is load-bearing on a brand-new store: this runs BEFORE ``conn.executescript(_SCHEMA)``
    creates the ``plans`` table, so on a fresh db ``cols`` is empty and this must no-op rather
    than ``ALTER`` a table that doesn't exist yet."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
    if cols and "graph" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN graph TEXT NOT NULL DEFAULT '[]'")


def _migrate_plans_reconcile(conn):
    """Schema evolution for a pre-F-R2 state db: add the ``party_type``/``party``/
    ``receivable_payable_account``/``company`` columns when missing. Same shape as
    :func:`_migrate_plans_graph` one column set later — the default backfills history honestly:
    every plan recorded before F-R2 existed carried none of these (they are reconcile-only
    fields), so blank is the correct read for old history, never NULL. Must run AFTER
    ``_migrate_plans_graph`` so a db older than all four column sets picks them up in order; the
    ``cols and`` guard (matching every sibling migration) is load-bearing on a brand-new store."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
    if not cols:
        return
    for col in ("party_type", "party", "receivable_payable_account", "company"):
        if col not in cols:
            conn.execute(f"ALTER TABLE plans ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Domain-separation prefix for seal-event HMACs. Prepended to the canonical bytes BEFORE hashing
# (not folded into the JSON dict as a field) so the two schemes never converge by accident: a
# receipt's canonical bytes (``prove._canonical``) never begin with this prefix, and a seal
# event's canonical bytes always do. Same key, same hash family (HMAC-SHA256) as receipts
# (``prove._seal``) — only the sealed byte string differs, and only *because* of this prefix.
_SEAL_DOMAIN = b"seal:"


def _seal_event_canonical(seq, ts, action, reason, source):
    """Deterministic, domain-separated bytes for one ``seal_events`` row's sealed fields.

    Shares its determinism reasoning with :func:`prove._canonical` (``sort_keys`` + no whitespace
    so the same logical content always seals identically regardless of dict ordering) but is
    deliberately NOT that function, and deliberately does not import or call it: mirroring the
    *shape* of the receipt machinery while keeping the two domains structurally separate is the
    point — a maintenance shortcut that routed seal events through ``prove._canonical`` (even with
    different field names) would make the two schemes closer to convergent than this codebase's
    honesty about domain separation wants to risk.
    """
    return _SEAL_DOMAIN + json.dumps(
        {"seq": seq, "ts": ts, "action": action, "reason": reason, "source": source},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _seal_event_hmac(key, seq, ts, action, reason, source):
    """HMAC-SHA256 over the domain-separated canonical seal-event tuple.

    Same key, same hash family as a receipt's seal (:func:`prove._seal`) — but because the input
    bytes are prefixed (:data:`_SEAL_DOMAIN`) and shaped differently (``action``/``reason``/
    ``source`` vs. a receipt's ``prev_hash``/``kind``/``body``), a receipt HMAC computed under the
    exact same key can never verify as a seal-event HMAC, and vice versa — even if a seq and ts
    happened to coincide between the two tables (see ``TestDomainSeparation`` in
    ``test_store_seal.py``, which replays a real receipt HMAC into a seal-event row and confirms
    it fails verification).
    """
    return hmac.new(
        key, _seal_event_canonical(seq, ts, action, reason, source), hashlib.sha256
    ).hexdigest()


# Domain-separation prefix for close-record HMACs (Half 3, Fork A1) -- same reasoning as
# _SEAL_DOMAIN: prepended to the canonical bytes BEFORE hashing so a receipt's or a seal-event's
# canonical bytes can never collide with a close-record's, even under the same key.
_CLOSE_DOMAIN = b"close:"


def _close_record_canonical(seq, ts, action, period_since, period_until, attested_head, gapped,
                             reason, source):
    """Deterministic, domain-separated bytes for one ``close_records`` row's sealed fields.

    Same ``sort_keys`` + no-whitespace determinism as :func:`_seal_event_canonical`, deliberately
    not shared code with it (mirrors the SHAPE, keeps the domains structurally separate).

    The three nullable fields (``period_since``/``period_until``/``attested_head``) are passed
    through to ``json.dumps`` as-is -- Python's ``None`` serializes to JSON ``null``, an explicit
    ``""`` serializes to JSON ``""``; these are never the same bytes, so "no bound" (an `attest`
    row, or an open-ended period) can never collide with "an explicit empty string" as long as no
    caller coalesces ``None`` to ``""`` before it reaches here (this function never does).
    """
    return _CLOSE_DOMAIN + json.dumps(
        {
            "seq": seq,
            "ts": ts,
            "action": action,
            "period_since": period_since,
            "period_until": period_until,
            "attested_head": attested_head,
            "gapped": gapped,
            "reason": reason,
            "source": source,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _close_record_hmac(key, seq, ts, action, period_since, period_until, attested_head, gapped,
                        reason, source):
    """HMAC-SHA256 over the domain-separated canonical close-record tuple. Same key, same hash
    family as every other HMAC in this module (:func:`_seal_event_hmac`, ``prove._seal``) -- the
    domain prefix + differently-shaped tuple is what keeps the three schemes from ever converging."""
    return hmac.new(
        key,
        _close_record_canonical(seq, ts, action, period_since, period_until, attested_head,
                                 gapped, reason, source),
        hashlib.sha256,
    ).hexdigest()


class CloseGateLatchedError(Exception):
    """``record_close`` refused: the attestation gate is LATCHED -- either a prior gapped close is
    awaiting attestation, or the close-record history itself failed to verify (a seq gap, a bad
    HMAC, or a keyless open). See :meth:`BrokerStore.close_gate_state` for the specific cause."""


class CloseRecordStaleError(Exception):
    """``record_close`` refused: the caller's ``expected_last_close_seq`` does not match the
    store's CURRENT last close seq at write time. Compare-and-append, the same head-read+append
    discipline the receipt chain and the seal already follow, applied to the cursor.

    **What a mismatch means — honestly (redteam 2026-07-15, honesty F1):** USUALLY a concurrent
    advance landed a close row between the caller's gate-state read and this write, so the period
    bounds the caller computed were planned against a cursor that has since moved — re-read the
    gate state, redo the period math, retry. But this check **cannot distinguish** that benign
    race from a history that was ALTERED underneath the caller — e.g. a TAIL-ROW DELETION
    (survivors stay contiguous ``1..N-1``, every surviving HMAC still verifies, so the gap and
    unverifiable checks both pass, and the first sign is THIS error firing on a replay). One
    mismatch: re-run. A RECURRING mismatch with no concurrent operator in sight: inspect
    ``pacioli close-status`` and treat it as a possible rollback, not a retry loop. (The tail
    ceiling itself is documented at :meth:`BrokerStore.close_gate_state` — and since the
    count-anchor slice (2026-07-16) an operator-held off-box pin closes it at audit time:
    :meth:`BrokerStore.close_head`/:meth:`BrokerStore.close_count` are the pin pair, ``pacioli
    anchor write``/``anchor check`` carry and replay it. This error remains the only ON-BOX,
    write-time tripwire.)"""


class AttestStaleError(Exception):
    """``attest`` refused: the caller's ``expected_seq`` does not name the gapped close that is
    CURRENTLY awaiting attestation. The mirror of :class:`CloseRecordStaleError` for the
    ceremony's own compare-and-append (redteam 2026-07-15, model-fidelity F1): without it, an
    operator who reviewed gap A could have their attest land on a DIFFERENT gap B that slipped in
    between their review and their write — a permanent attest row whose recorded reason describes
    the wrong gap. Refusing forces the operator to re-read ``close-status`` and review THE gap
    that is actually pending. Same honest limit as its mirror: a mismatch usually means another
    operator attested/advanced first, but the check cannot distinguish that from history
    alteration underneath the caller."""


class CloseRecordIntegrityError(Exception):
    """``close_cursor`` refused: the close-record history could not be verified — a ``seq`` gap,
    a row whose HMAC does not verify, or a keyless open (no key in reach to verify anything with).
    The cursor is period-loop CONTROL data (it decides where the next period starts), so an
    integrity failure fails CLOSED here: a clear raise, never a value read off unverified rows.
    :meth:`BrokerStore.close_gate_state` on the same store stays readable (it reports
    ``latched=True`` with the specific cause) — status surfaces are never gated, only the
    cursor-as-a-value is."""


class NoAttestationPendingError(Exception):
    """``attest`` refused: there is no gapped close currently awaiting attestation to clear. An
    attestation must attest *something* -- it never fires speculatively, and it never clears a
    LATCHED gate whose cause is an integrity failure rather than a real pending gap (attesting
    cannot repair a corrupt history)."""


def _check_seal_pin(rows, expected_seal_head, expected_seal_count):
    """The off-box pin comparison behind :meth:`BrokerStore.seal_state`'s ``expected_seal_head``/
    ``expected_seal_count`` kwargs. Pure — takes the same ``rows`` :meth:`seal_state` already
    fetched (``(seq, ts, action, reason, source, hmac)`` tuples, ordered by ``seq``), no query of
    its own — so :meth:`seal_state` reasons about ONE snapshot for both the content-only
    derivation and this pin check, same discipline as :meth:`BrokerStore.verify_snapshot` on the
    receipt side.

    Returns ``None`` when the pin agrees with the live rows (or vouches for nothing — see the
    ``expected_seal_count == 0`` case below), or a cause string (the conservative/sealed side) on
    ANY disagreement or malformed input. **Never raises** — a bad pin pair is exactly the input
    this function exists to turn into a cause string, not a crash; the caller (:meth:`seal_state`)
    already reads ``rows`` before calling this, so any genuine SQL error already happened, and
    exclusively there.

    Mirrors ``anchor.compare``'s LOOKUP MECHANISM on the receipt side (``chain_hmacs[count-1]``),
    applied to this table's own AUTOINCREMENT ``seq``: the row AT the pinned count is looked up by
    its ``seq`` value, not by list position, so an unrelated interior gap elsewhere in ``rows``
    can't misalign the lookup. This is the mechanism only, not the guarantee — ``seal_events`` has
    no prefix-chaining, so unlike the receipt side this comparison fixes only the ONE row it names,
    never the whole pre-pin history (see :meth:`BrokerStore.seal_state`'s own "Honest tamper
    ceiling" paragraph for the exact, non-overclaimed scope).

    ``(None, 0)`` is accepted as the ONE exception to "both or neither": it is exactly what
    :meth:`BrokerStore.seal_head`/:meth:`BrokerStore.seal_count` themselves return on a genuinely
    empty table (mirrors the receipt side's ``GENESIS``/``count == 0`` pairing), so a caller that
    pins those two return values verbatim and later replays them must not have that legitimate
    pin misclassified as malformed. A count of ``0`` vouches for nothing either way (there is no
    position to compare), so whatever ``expected_seal_head`` accompanies it is not otherwise
    checked — the same posture ``anchor.compare`` takes once its own ``a_count == 0``.
    """
    if expected_seal_head is None and expected_seal_count is None:
        return None  # nothing pinned — the caller already short-circuits this case, kept defensive

    try:
        # Count is type-checked FIRST (before the head/count "both or neither" shape check) so a
        # genuine ``expected_seal_count == 0`` can be recognised even when paired with
        # ``expected_seal_head=None`` — see the ``(None, 0)`` docstring note above.
        if isinstance(expected_seal_count, bool) or not isinstance(expected_seal_count, int) \
                or expected_seal_count < 0:
            return "malformed off-box seal anchor pin (count must be a non-negative integer)"
        if expected_seal_count == 0:
            return None  # the pin says "empty then" — nothing pinned to compare a position against

        if expected_seal_head is None:
            return "malformed off-box seal anchor pin (head and count must be supplied together)"
        if not isinstance(expected_seal_head, str) or not expected_seal_head:
            return "malformed off-box seal anchor pin (head must be a non-empty string)"

        live_count = len(rows)
        if live_count < expected_seal_count:
            return "seal history behind the off-box anchor (tail truncated?)"

        pinned_row = next((r for r in rows if r[0] == expected_seal_count), None)
        if pinned_row is None:
            # live_count >= expected_seal_count yet no row carries that exact seq: only reachable
            # with an interior gap AT the pinned position, itself already a rollback signal — fold
            # into the same conservative answer rather than raising or guessing.
            return "seal history diverges from the off-box anchor"
        live_hmac_at_pin = pinned_row[5]
        if not hmac.compare_digest(live_hmac_at_pin, expected_seal_head):
            return "seal history diverges from the off-box anchor"
        return None
    except Exception:
        # Any failure evaluating the pin (a type comparison error, etc.) is a malformed-pin
        # problem, not a connection problem — fold it into the same fail-closed answer rather
        # than letting it crash a caller that must never let an unreadable pin through as unsealed.
        return "malformed off-box seal anchor pin"


def _check_close_pin(rows, expected_close_head, expected_close_count):
    """The off-box pin comparison behind :meth:`BrokerStore.close_gate_state`'s
    ``expected_close_head``/``expected_close_count`` kwargs — the count-anchor slice
    (docs/plans/2026-07-16-close-count-anchor.md), mirroring :func:`_check_seal_pin` mechanism
    for mechanism: pure (takes the same ``rows`` the derivation already fetched — ``(seq, ts,
    action, period_since, period_until, attested_head, gapped, reason, source, hmac)`` tuples,
    ordered by ``seq`` — no query of its own); never raises; ``(None, 0)`` accepted as the one
    exception to "both or neither" (it is exactly what :meth:`BrokerStore.close_head`/
    :meth:`BrokerStore.close_count` themselves return on a genuinely empty table), and
    ``(GENESIS, 0)`` — the record-level sentinel a v3 pin of an empty table carries — accepted
    equally; any OTHER head at count 0 is refused as malformed (stricter than the seal's
    count-0 branch on purpose — the close table's count-0 pin is CLI-reachable, the seal's is
    not); a legitimate count of ``0`` vouches for nothing either way; the row AT the pinned
    count is looked up by its ``seq`` VALUE, never list position.

    Returns ``None`` on agreement, else ``(cause, reason)`` — a stable machine-checkable tag
    plus human text, because :meth:`BrokerStore.close_gate_state`'s contract separates the two
    (the seal's contract folds them into one string; parity is with each table's own CONTRACT,
    not the string type):

    * live count < pinned → ``("anchor_behind", …)`` — the headline catch: the newest close
      row(s) were deleted (the silent cursor rollback the derivation's own docstring names as
      its ceiling), and only a pin recorded before the deletion can see the count went
      backwards.
    * the row at the pinned seq is missing or its stored ``hmac`` does not
      ``hmac.compare_digest``-equal the pinned head → ``("anchor_diverged", …)``.
    * a malformed pin pair (partial, wrong types) → ``("anchor_malformed", …)``, folded, never
      raised.

    Same honest ceiling as the seal's pin, stated the same way: ``close_records`` is per-row
    HMAC'd, NOT prefix-chained, so this comparison fixes only the ONE row the pin names (plus
    the count) — a key holder rewriting any OTHER row is not caught here (pinned by
    ``test_honest_ceiling_key_holder_rewrite_BEFORE_the_pinned_position_not_caught``)."""
    if expected_close_head is None and expected_close_count is None:
        return None  # nothing pinned — the caller already short-circuits this case, kept defensive

    try:
        # Count is type-checked FIRST so a genuine ``expected_close_count == 0`` can be
        # recognised even when paired with ``expected_close_head=None`` — the empty-table
        # native pin (mirrors _check_seal_pin's ``(None, 0)`` note). That is the STORE-level
        # sentinel only (what close_head() itself returns on an empty table) — the anchor
        # RECORD format (anchor.py) uses a DIFFERENT sentinel at count 0, GENESIS, because a
        # JSON field is never Python None; anchor.py's record-level validation guarantees only
        # GENESIS (never arbitrary text) reaches this function paired with count 0 from
        # ``pacioli anchor check``.
        if isinstance(expected_close_count, bool) or not isinstance(expected_close_count, int) \
                or expected_close_count < 0:
            return ("anchor_malformed",
                    "malformed off-box close anchor pin (count must be a non-negative integer)")
        if expected_close_count == 0:
            # A count-0 pin has no position to compare — but its head must still be one of the
            # two legitimate empty-table sentinels (verify pass 2026-07-16, Item A: BrokerStore
            # is public API, and not every future caller routes through anchor.py's record
            # validation; a count-0 pin carrying arbitrary text is internally inconsistent —
            # fail closed rather than silently agree). Stricter than _check_seal_pin's count-0
            # branch on purpose: the seal's count-0 path is CLI-unreachable (anchor write
            # refuses a zero-row seal history), the close table's is a REAL pin.
            if expected_close_head is None or expected_close_head == GENESIS:
                return None  # "nothing closed then" — vouches for nothing on replay
            return ("anchor_malformed",
                    "malformed off-box close anchor pin (count 0 must pin the GENESIS close "
                    "head or none at all)")

        if expected_close_head is None:
            return ("anchor_malformed",
                    "malformed off-box close anchor pin (head and count must be supplied "
                    "together)")
        if not isinstance(expected_close_head, str) or not expected_close_head:
            return ("anchor_malformed",
                    "malformed off-box close anchor pin (head must be a non-empty string)")

        live_count = len(rows)
        if live_count < expected_close_count:
            return ("anchor_behind",
                    "close history behind the off-box anchor (tail truncated?)")

        pinned_row = next((r for r in rows if r[0] == expected_close_count), None)
        if pinned_row is None:
            # live_count >= pinned yet no row carries that exact seq: an interior gap AT the
            # pinned position — itself rollback evidence; fold into the conservative answer.
            return ("anchor_diverged",
                    "close history diverges from the off-box anchor")
        live_hmac_at_pin = pinned_row[9]
        if not hmac.compare_digest(live_hmac_at_pin, expected_close_head):
            return ("anchor_diverged",
                    "close history diverges from the off-box anchor")
        return None
    except Exception:
        # Any failure evaluating the pin is a malformed-pin problem, not a connection problem —
        # fail closed, never crash a caller that must not let an unreadable pin through as open.
        return ("anchor_malformed", "malformed off-box close anchor pin")


class StoreCorruptError(Exception):
    """The state db file exists on disk but is not a legitimate store — e.g. a torn write
    (crash mid-creation, disk full, an interrupted copy, external truncation) left it at zero
    bytes. ``sqlite3.connect`` treats a zero-byte file as a perfectly valid, brand-new, empty
    database with no error and no signal anything is wrong — indistinguishable, at the file
    level, from a target whose store has simply never been used. That ambiguity is exactly the
    ``reads-as-empty`` bug class this codebase refuses to accept (see TH-1/TH-2): a torn ledger
    must never be silently reinterpreted as "no history". Raised by :func:`refuse_if_torn`."""


# The SQLite file-format header is exactly 100 bytes; no valid database file is ever smaller.
# A file that EXISTS but is below this cannot be a real store — it is a torn write. Checking a
# real floor, not `== 0`, is the actual fix (redteam, ledger-integrity lens): a file truncated to
# exactly 1 byte escapes both a `== 0` check AND SQLite's own corruption detection (2+ bytes ->
# DatabaseError, but 1 byte opens SILENTLY as an empty db), and the reopen's schema script then
# destroys the ledger with a clean verify(). Refusing everything below the header closes that
# whole class in one comparison instead of trusting SQLite to catch the 2..99 range incidentally.
_SQLITE_HEADER_BYTES = 100


def refuse_if_torn(path):
    """Guard called BEFORE ``sqlite3.connect(path)`` opens (or creates) the state db at ``path``.

    If a file already exists there but is smaller than a valid SQLite header
    (:data:`_SQLITE_HEADER_BYTES`), refuse rather than let ``connect`` silently treat it as (or
    reinitialize it into) a legitimate new, empty database. A path that does not exist yet is the
    genuine first-use case and passes through untouched — ``connect`` will create it there.

    This MUST run before ``connect``, not after: ``sqlite3.connect`` creates a zero-byte
    placeholder file as a side effect of merely opening a connection, the instant it is called —
    by the time a connection object exists, the "did this file exist with content a moment ago"
    signal this check depends on is already gone.

    Known narrow residual (redteam, TOCTOU): two callers racing a target's genuine FIRST-ever open
    can see the winner's transient 0-byte placeholder and false-refuse. The window is microseconds
    and the store re-opens fresh per request; the safe response to this error is to INVESTIGATE
    (backups / the off-box anchor pin), never to blindly delete — a full fix (atomic create) is a
    separate increment.
    """
    p = Path(path)
    if p.exists() and p.stat().st_size < _SQLITE_HEADER_BYTES:
        raise StoreCorruptError(
            f"state db {p} exists but is only {p.stat().st_size} bytes — smaller than a valid "
            f"SQLite header ({_SQLITE_HEADER_BYTES} bytes), so it cannot be a real ledger. A torn "
            "write (crash mid-creation, disk full, an interrupted copy, external truncation) can "
            "leave a store file this way, and SQLite would otherwise silently treat it as a "
            "brand-new empty ledger — erasing any prior receipts/markers/plans with no trace and "
            "no error. Refusing rather than guessing. Investigate before doing anything else "
            "(check backups / the off-box anchor pin for the last known head); do NOT delete the "
            "file until you have confirmed it holds no history a live writer is mid-way through."
        )


class BrokerStore:
    """A receipt ledger + marker store on one SQLite connection.

    :param conn: an open ``sqlite3.Connection`` (a single file = sovereign, portable; or ``:memory:``).
    :param key: the HMAC seal key (bytes) — on-box until increment 2. ``None`` opens the store
        **keyless**: marker and plan ops work (the human mint CLI runs with no seal key in reach —
        least exposure), receipt ops refuse.
    :param now_iso: clock returning an ISO timestamp for receipt ``ts`` (injected for testability).
    """

    def __init__(self, conn, key, now_iso=_utc_iso):
        self._conn = conn
        self._key = key
        self._now_iso = now_iso
        self._via = None  # set_via — the door's stamp; None = undeclared (in-process/CLI)
        conn.isolation_level = None  # manage transactions explicitly (BEGIN IMMEDIATE), not implicitly
        conn.execute("PRAGMA busy_timeout=5000")  # wait for the write lock instead of erroring out
        # Pinned explicitly rather than trusted to a compiled-in default: FULL fsyncs the journal
        # before the main file is touched and again before deleting it, which is what lets a
        # process that dies mid ``record_outcome`` (a real kill -9 strictly before COMMIT) find
        # only the previous, fully-committed state on restart — never a half-applied write. See
        # pacioli/tests/test_store_torn_write.py::TestMidTxnCrashRecovery for a real crash proving it.
        conn.execute("PRAGMA synchronous=FULL")
        _migrate_plans_op(conn)  # BEFORE the schema script: an old table must gain `op` first
        _migrate_plans_doctype(conn)  # then `doctype` — order matters, see its docstring
        _migrate_plans_graph(conn)  # then `graph` — same ordering rule, one column later
        _migrate_plans_reconcile(conn)  # then the reconcile fields — same ordering rule
        conn.executescript(_SCHEMA)
        self._seed_seal_genesis()  # AFTER the schema script — seal_events must exist first

    # --- markers -----------------------------------------------------------------
    def mint_marker(self, token, plan_id, expires_at):
        """Insert a fresh ``live`` marker (the out-of-band mint a human runs). Returns the Marker."""
        m = consent.new_marker(token, plan_id, expires_at)
        self._conn.execute(
            "INSERT INTO markers(token_hash, plan_id, expires_at, state) VALUES(?,?,?,?)",
            (m.token_hash, m.plan_id, m.expires_at, m.state),
        )
        return m

    def claim_marker(self, reserved):
        """Atomic CAS ``live → reserved``. Returns ``True`` iff this call won (exactly one row moved).

        Lock contention beyond ``busy_timeout`` returns ``False`` (deny-biased: did not win) rather
        than raising into the caller."""
        try:
            cur = self._conn.execute(
                "UPDATE markers SET state=? WHERE token_hash=? AND plan_id=? AND state=?",
                (consent.RESERVED, reserved.token_hash, reserved.plan_id, consent.LIVE),
            )
        except sqlite3.OperationalError:
            return False
        return cur.rowcount == 1

    def marker_state(self, token):
        """The stored state for a raw token, or ``None`` if no such marker."""
        row = self._conn.execute(
            "SELECT state FROM markers WHERE token_hash=?", (consent.hash_token(token),)
        ).fetchone()
        return row[0] if row else None

    def get_marker(self, token):
        """The full :class:`consent.Marker` a raw token presents as, or ``None``."""
        token_hash = consent.hash_token(token)
        if token_hash is None:
            return None
        row = self._conn.execute(
            "SELECT token_hash, plan_id, expires_at, state FROM markers WHERE token_hash=?",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        return consent.Marker(token_hash=row[0], plan_id=row[1], expires_at=row[2], state=row[3])

    # --- plans -------------------------------------------------------------------
    def record_plan(self, plan):
        """Persist a :class:`Plan` so ``submit`` (a separate MCP call, maybe a separate process)
        can validate against exactly what the human consented to. Duplicate ``plan_id`` refused."""
        self._conn.execute(
            "INSERT INTO plans(plan_id, target, docname, doc_version, posting_date,"
            " projected_gl, risk_flags, ts, op, doctype, graph, party_type, party,"
            " receivable_payable_account, company) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan.plan_id, plan.target, plan.docname, plan.doc_version, plan.posting_date,
             json.dumps(plan.projected_gl), json.dumps(plan.risk_flags), plan.ts, plan.op,
             plan.doctype, json.dumps(plan.graph), plan.party_type, plan.party,
             plan.receivable_payable_account, plan.company),
        )
        return plan

    def get_plan(self, plan_id):
        """The stored :class:`Plan`, or ``None``."""
        row = self._conn.execute(
            "SELECT plan_id, target, docname, doc_version, posting_date, projected_gl,"
            " risk_flags, ts, op, doctype, graph, party_type, party,"
            " receivable_payable_account, company FROM plans WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        if row is None:
            return None
        return Plan(plan_id=row[0], target=row[1], docname=row[2], doc_version=row[3],
                    posting_date=row[4], projected_gl=json.loads(row[5]),
                    risk_flags=json.loads(row[6]), ts=row[7], op=row[8], doctype=row[9],
                    graph=json.loads(row[10]), party_type=row[11], party=row[12],
                    receivable_payable_account=row[13], company=row[14])

    # --- receipts ----------------------------------------------------------------
    def _require_key(self):
        if self._key is None:
            raise ValueError("this store was opened without a seal key; receipt/seal ops need one")

    def set_via(self, via):
        """Declare WHICH door (and which principal) every subsequent intent is recorded
        through — F3 of the doors ruling: with many doors, the book must say who asked.
        The stamp is a dict like ``{"transport": "http", "principal": "env:SERVE_T"}``; the
        principal is always a LABEL (a token's reference, ``local-spawn``), never secret
        material. Called once by the door at assembly; a non-dict refuses (deny-biased —
        a stamp that can't be recorded faithfully is never recorded approximately)."""
        if not isinstance(via, dict):
            raise ValueError(f"via must be a dict, got {via!r}")
        self._via = dict(via)

    def record_intent(self, body):
        """Append an ``intent`` receipt. ``BEGIN IMMEDIATE`` serialises the head-read + append so a
        concurrent appender waits rather than colliding on ``seq``. Returns the Receipt.

        When a door declared itself (:meth:`set_via`), its stamp is merged into the body and
        ALWAYS wins over any ``via`` key the body carries — no caller may declare its own
        door. Undeclared (``None``) leaves the body byte-identical to pre-doors behavior."""
        self._require_key()
        if self._via is not None:
            body = dict(body, via=dict(self._via))
        with self._immediate():
            r = prove.append(self._key, self._head_receipt(), prove.INTENT, body, ts=self._now_iso())
            self._insert(r)
        return r

    def record_outcome(self, intent, status, result, final_marker):
        """Append the outcome receipt AND settle the marker in one ``BEGIN IMMEDIATE`` transaction
        (atomic settle, serialised append).

        Only ``status == "committed"`` finalises the intent (``prove.orphans``); a ``failed``/uncertain
        outcome leaves the intent an orphan for reconciliation. ``result`` must be JSON-native. The
        marker update is state-guarded (``... AND state='reserved'``) so it only ever settles a marker
        this flow actually claimed — never clobbers an unrelated one.
        """
        body = {"finalizes": intent.seq, "status": status, "result": result}
        self._require_key()
        with self._immediate():
            r = prove.append(self._key, self._head_receipt(), prove.OUTCOME, body, ts=self._now_iso())
            self._insert(r)
            if final_marker is not None:
                self._conn.execute(
                    "UPDATE markers SET state=? WHERE token_hash=? AND state=?",
                    (final_marker.state, final_marker.token_hash, consent.RESERVED),
                )
        return r

    def receipts(self):
        rows = self._conn.execute(
            "SELECT seq, prev_hash, kind, body, ts, hmac FROM receipts ORDER BY seq"
        ).fetchall()
        return [self._row_to_receipt(row) for row in rows]

    def head(self):
        """The chain head ``hmac`` (pin this off-box), or ``None`` for an empty ledger."""
        r = self._head_receipt()
        return r.hmac if r else None

    def verify(self, expected_head=None):
        """Verify the whole chain; pass ``expected_head`` (an off-box-recorded head) to also catch
        tail-truncation / a full wipe. A chain whose bytes won't even parse (a corrupt body) is
        reported as ``(False, reason)`` — verify's whole job is to detect corruption, so it never
        crashes on it; a torn FILE is still refused earlier, at open (:func:`refuse_if_torn`)."""
        self._require_key()
        try:
            return prove.verify_chain(self._key, self.receipts(), expected_head=expected_head)
        except StoreCorruptError as exc:
            return False, str(exc)

    def verify_snapshot(self, expected_head=None):
        """Verify the chain and return ``(ok, reason, receipts)`` — the receipts list returned is
        the *exact* one that was verified, read **once**.

        A caller that must both verify the chain AND inspect it (the off-box anchor: keyed verify,
        then compare the head-pin against the chain's hmacs) must reason about ONE snapshot. Calling
        :meth:`verify` and then :meth:`receipts` separately is two unguarded reads — a concurrent
        host-level writer (the anchor's own threat model) could change the chain between them, so
        the comparison would run against bytes the verify never covered. One read closes that gap:
        the same in-memory list backs both the verify and whatever the caller does next."""
        self._require_key()
        try:
            receipts = self.receipts()  # a single SELECT — one consistent snapshot
        except StoreCorruptError as exc:
            # A body that won't parse: no trustworthy snapshot to hand back. Report corruption with
            # an empty snapshot — a caller building on it (the Close) reads it as an empty,
            # non-balancing period, never as a clean chain.
            return False, str(exc), []
        ok, reason = prove.verify_chain(self._key, receipts, expected_head=expected_head)
        return ok, reason, receipts

    def orphans(self):
        """Intent receipts with no committed outcome — reconcile each against the real ``docstatus``."""
        return prove.orphans(self.receipts())

    # --- seal state (CONTAIN) -----------------------------------------------------
    def seal(self, reason, *, source="operator"):
        """Append a ``seal`` event — the broker's own recorded decision to CONTAIN, HMAC'd with the
        same machinery as every receipt (domain-separated, see :func:`_seal_event_hmac`) so the
        decision to stop writing is itself tamper-evident against an interior edit or an in-place
        rewrite of the latest row — NOT against a keyless attacker with file access who deletes the
        newest row(s) outright (tail truncation defeats seq-contiguity; see :meth:`seal_state`'s
        docstring for the honest ceiling).

        Refuses keyless (:meth:`_require_key`), same reasoning as every receipt op: sealing without
        a key would append an entry no later keyed reader could tell apart from a forgery, which
        defeats the entire point of an *evented* seal.

        Appending while already sealed is ALLOWED and recorded: a second ``seal`` call does not
        error and does not need to check the current state first — the history stays honest
        (idempotent STATE, non-idempotent HISTORY: a second confession is still a confession, and
        the record of *how many times* and *why* someone reached for CONTAIN is itself useful).

        ``source`` names who is sealing — ``"operator"`` (the default; the CLI's ``pacioli seal``)
        or ``"response"`` (an envelope-escalated CONTAIN finding) — never ``"init"``, which is
        reserved for the genesis row this class seeds itself (:meth:`_seed_seal_genesis`).

        Returns the freshly recomputed :meth:`seal_state` (not just the appended row), so a caller
        gets the authoritative post-write truth in one call rather than append-then-re-derive.
        """
        self._require_key()
        with self._immediate():
            self._append_seal_event("seal", reason, source)
        return self.seal_state()

    def unseal(self, reason):
        """Append an ``unseal`` event — always ``source="operator"``: unsealing is never automatic.
        Auto-*sealing* exists (an envelope-escalated CONTAIN finding, ``source="response"``), but
        nothing in this codebase auto-*clears* a seal — that stays the human's hand (the seal is
        reversible, but only by a human saying so).

        Refuses keyless, same reasoning as :meth:`seal`. Allowed while already unsealed (recorded;
        the state does not change, but the history still gains an honest row — an operator
        confirming "still fine" is a real, auditable act, not a no-op).

        Returns the freshly recomputed :meth:`seal_state`, same contract as :meth:`seal`.
        """
        self._require_key()
        with self._immediate():
            self._append_seal_event("unseal", reason, "operator")
        return self.seal_state()

    def seal_state(self, *, expected_seal_head=None, expected_seal_count=None):
        """Derive the broker's current CONTAIN state from the ``seal_events`` history — never from
        a single boolean flag, so there is no bit to just flip. **Fail-closed by construction**:
        every branch below that cannot fully vouch for the history answers ``sealed=True`` rather
        than guessing unsealed.

        - **Zero rows** → ``sealed=True, cause="no seal history"``. A keyed store always gains a
          genesis row at its first open (:meth:`_seed_seal_genesis`), so an empty table under a
          keyed open is either a pre-genesis transient no live caller should observe, or a history
          deleted outright — both must read as sealed, never as "never sealed, so unsealed".
        - **A gap in ``seq`` 1..N** → ``sealed=True, cause="seal history gap (rollback?)"``.
          ``seq`` is ``AUTOINCREMENT`` (never reused, even across deletes — see ``_SCHEMA``), so a
          missing number can only mean a row was deleted from the MIDDLE of the history —
          contiguity catches exactly that (e.g. deleting an inconvenient ``seal`` from the middle).
          It does **NOT** catch deleting the NEWEST row(s) (tail truncation): the survivors stay
          contiguous ``1..k``, an earlier ``seal``/``unseal``/``genesis`` becomes "latest", and if
          that surviving row's HMAC still verifies (it was never touched, only outlived), this
          reads as a genuine, un-gapped history — possibly UNSEALED. A keyless attacker with
          DB-file write access but no HMAC key can roll back a seal this way; this is the SAME
          on-box limit the receipt chain has (see ``prove.py``) — closed there by the off-box
          ``pacioli anchor`` pin, and closed here too when the caller supplies one (see the pin
          paragraph below for the exact mechanism).
        - **Any surviving row's HMAC does not verify (keyed open only)** →
          ``sealed=True, cause="unverifiable"``. **Every row is checked, not only the latest's**
          (closed 2026-07-15, security redteam F1(a) — the prior version checked the latest row
          alone, so a keyless edit of an INTERIOR row's content read clean once that row was no
          longer latest): an edited ``reason``/``source``/``action``/``ts`` on ANY row recomputes
          to a different HMAC under the caller's key, so a keyless doctoring of that row cannot
          pass as legitimate, wherever in the history it sits. (A DELETED row is a different case
          — see the gap bullet above.) This still does **not** catch a KEY-HOLDER who recomputes
          a fresh, self-consistent HMAC for the row they edited — that forgery verifies cleanly
          here, exactly as it always could (key possession is authorship, on-box); see the
          "Honest tamper ceiling" paragraph below for what an off-box pin additionally closes,
          and what it honestly does not.
        - **Keyless open**: cannot compute or check an HMAC at all (no key in reach — the CLI's
          least-exposure path), so this trusts row CONTENT for state while still enforcing the
          zero-rows and contiguity checks above. This is documented honestly as defense-in-depth,
          NOT an authoritative verdict: a keyless caller cannot detect an in-place edit to the
          latest row's content, only its absence or a gap.
        - **Otherwise**: ``sealed = (latest.action == "seal")`` — a ``genesis`` or ``unseal``
          latest action reads as unsealed.

        Returns ``{"sealed": bool, "since": ts|None, "reason": str|None, "source": str|None,
        "seq": int, "cause": str|None}``. When ``cause`` is set (ANY fail-closed case — zero rows,
        a seq gap, an unverifiable latest event, or a pin mismatch — see below)
        ``since``/``reason``/``source`` are ALL ``None`` — ``cause`` is the sole authoritative
        explanation, so a fail-closed answer never carries a stale or possibly-doctored row's own
        claimed reason/source next to ``sealed=True`` (e.g. a gap's surviving row could otherwise
        show a genuine PRE-gap ``unseal`` reason, misleading a consumer that reads ``reason``
        without also checking ``cause``). ``seq`` still names the latest surviving row's number
        (or ``0`` for zero rows) — it is SQLite's own bookkeeping, not row content, so it is never
        nulled. ``cause`` is ``None`` only when ``sealed`` reflects a genuinely verified/contiguous
        history (sealed OR unsealed) with an agreeing pin (or no pin at all).

        **Off-box pin (tail-rollback detection) — mirrors** ``head()`` **/**
        ``verify(expected_head=...)`` **on the receipt side.** :meth:`seal_head` exposes this
        table's own head ``hmac`` and :meth:`seal_count` its row count — the same (head, count)
        pair the receipt chain's off-box anchor pins, applied to this table. Pass both back in as
        ``expected_seal_head``/``expected_seal_count`` (keyword-only; default ``None`` on both —
        with neither supplied, this method is **byte-identical** to the derivation above, a
        regression pin proven in tests, not just a design intent) and, ON TOP of the derivation
        above, this closes exactly the gap that derivation's own docstring names as its ceiling:

          * live row count < the pinned count → ``sealed=True, cause="seal history behind the
            off-box anchor (tail truncated?)"`` — the headline catch: the newest row(s) were
            deleted and nothing replaced them, so seq-contiguity alone (the derivation above)
            reads the surviving prefix as a clean, un-gapped history. A pin recorded off-box
            before the deletion is the only thing that can see the row count went backwards.
          * live row count >= the pinned count, but the row actually AT the pinned count (found by
            its ``seq`` value, not by list position, so an unrelated interior gap elsewhere can't
            misalign the lookup) has an ``hmac`` that does not ``hmac.compare_digest``-equal
            ``expected_seal_head`` → ``sealed=True, cause="seal history diverges from the off-box
            anchor"``. This is the belt beyond the all-row keyed HMAC check above (F1(a)): a
            key-HOLDING attacker (key possession is authorship, on-box — see the ceiling
            paragraph below) can edit any row, including one that is no longer "latest", and
            recompute a self-consistent HMAC for it with the same key — that recomputed HMAC
            verifies cleanly under the all-row check above, which only asks "does this row's
            HMAC match ITS OWN content", not "does it match what was pinned". The off-box pin
            catches it for the one row it names by position: rewriting THAT row while preserving
            the exact pinned hmac would need an HMAC-SHA256 collision, not just key possession —
            the same single-position COMPARISON MECHANISM ``anchor.compare`` uses for the receipt
            chain's ``chain_hmacs[count-1]``, but **not the same guarantee**: receipts are
            prefix-chained (``prev_hash``), so that one comparison fixes the WHOLE pre-pin
            history, even against a key holder. ``seal_events`` has no such chaining, so this
            comparison fixes only the single row the pin names — a key holder rewriting any OTHER
            row (including one earlier than the pinned position) is not caught here or by the
            pin. See the "Honest tamper ceiling" paragraph below for the full, non-overclaimed
            scope.
          * live row count > the pinned count AND the pinned position still agrees → the CURRENT
            (grown) state is reported normally (whatever the derivation above already computed) —
            the pin only vouches for history up to its own count; new, legitimate events since the
            pin are never held against the caller.
          * ``expected_seal_count == 0`` vouches for nothing (there is no position to compare
            against) and is accepted with ANY ``expected_seal_head`` — including ``None``, which
            is exactly what :meth:`seal_head`/:meth:`seal_count` themselves return on a genuinely
            empty table, so replaying that pair back in round-trips cleanly rather than reading as
            malformed.
          * any OTHER malformed pair (only one of the two supplied when count is non-zero; a
            non-string/wrong-type head; a negative/non-int/bool count) → folded into
            ``sealed=True`` with a cause naming the pin as unusable, exactly like every other
            branch here — this method **never raises** on a bad pin; a genuine ``sqlite3`` error
            reading the table is a different failure class and is still allowed to propagate (see
            the "must never raise" paragraph below).
          * every comparison here is ``hmac.compare_digest`` — never ``==`` — matching every other
            HMAC comparison in this module.

        **Honest guarantee — audit-time DETECTION, not real-time prevention.** Exactly as for the
        receipt chain (``prove.py``, SPEC §5): nothing *on-box* stops a principal who can write the
        DB file, and this pin does not change that — it does not run on every ``seal``/``unseal``
        call, and nothing here blocks a write. What it buys is that a rollback STOPS BEING SILENT:
        an operator who pinned :meth:`seal_head`/:meth:`seal_count` off-box (somewhere this box's
        own writer cannot reach) can, by supplying that pin back in, prove the seal history was
        truncated or rewritten since the pin was taken — a fact the plain (unpinned) derivation
        above cannot see at all. The detection window is the operator's own check cadence, not
        continuous; a rollback followed by a re-forward before the next check is invisible, the
        same disclosed limit the receipt chain's own anchor carries.

        **Must never raise on malformed row CONTENT, or on a malformed pin** — that is exactly what
        this method exists to turn into a sealed-with-cause answer rather than a crash a caller
        (the dispatch gate) would have to catch. A broken connection or genuine SQL error is a
        different failure class (the store itself is unreachable, not merely undecidable) and is
        allowed to propagate — callers on a write path must deny-bias that too, just via their own
        exception handling rather than a cause string here.

        **Honest tamper ceiling — per-row keyed integrity, NOT prefix-chaining like receipts; a
        FORWARD control, rollback-resistant only at the exact pinned position, UNLESS a pin is
        supplied (corrected 2026-07-15, security redteam F1(b) — see below for what changed and
        why the old text here was wrong).** Without a pin, this defends completely against a
        keyless forger with no HMAC key at all: an interior row silently dropped is caught
        (contiguity), and — since F1(a), every row above — a keyless edit of ANY row's content,
        interior or latest, is caught too (that row's own stored HMAC no longer matches its own
        recomputed one). It does **NOT** defend against a keyless attacker who deletes the NEWEST
        seal-event row(s) (tail truncation, or wiping the table and ``sqlite_sequence`` and
        letting genesis re-seed): seq-contiguity cannot see a missing tail, so a genuine, unedited
        earlier row can become "latest" and read as a legitimate — possibly unsealed — state.
        This is the SAME on-box limit the receipt chain has always disclosed (SPEC §5) — closed
        there by an off-box anchor pin, and closed here too WHEN the caller supplies one (the pin
        paragraph above): passing ``expected_seal_head``/``expected_seal_count`` catches a tail
        truncation, and catches a rewrite of the row actually AT the pinned position (an exact
        HMAC comparison — moving it while preserving the pinned value needs an HMAC-SHA256
        collision, not just key possession).

        **This is NOT the same guarantee the receipt chain's pin carries, and this method must
        never claim it is — the prior text here (through 0.21.0's Task 3) did, and was wrong.**
        The receipt chain is prefix-chained (every hmac commits to its entire history through
        ``prev_hash``), so ``anchor.compare``'s ``chain_hmacs[count-1]`` pin fixes the WHOLE
        pre-pin history — even against a key holder, editing anything before the pin forces every
        hmac after it to change too. ``seal_events`` has no such chaining: each row's HMAC covers
        only that row's own ``(seq, ts, action, reason, source)`` tuple, independent of every
        other row. So a key-HOLDING attacker can edit any row OTHER than the exact
        pinned-position row — including one earlier than the pin — and recompute a fresh,
        self-consistent HMAC for just that row; neither the all-row keyed check above (it
        verifies cleanly under the real key) nor the pin (it only compares the one row at the
        pinned count) catches it. Only the single row the pin names by position is fixed; the
        rest of the pre-pin history is not — this is a narrower ceiling than the receipt chain's
        pin, not an identical one. Anyone holding this store's HMAC key can ADDITIONALLY forge a
        fully self-consistent seal-event history AFTER the pinned position (a correctly-signed
        fake ``unseal`` appended after a real ``seal``, for instance) — key possession is
        authorship, on-box, until the key itself is anchored off-box; a pin only vouches for the
        one position it names (plus the count), never for the rest of the history before it or
        for anything after it. This detection is still audit-time only (the guarantee paragraph
        above) — it never runs unless the caller asks for it, and it never prevents the write
        itself.
        """
        rows = self._conn.execute(
            "SELECT seq, ts, action, reason, source, hmac FROM seal_events ORDER BY seq"
        ).fetchall()

        result = self._derive_seal_state(rows)

        if expected_seal_head is None and expected_seal_count is None:
            return result  # no pin supplied: byte-identical to the pre-anchor derivation

        pin_cause = _check_seal_pin(rows, expected_seal_head, expected_seal_count)
        if pin_cause is None:
            return result  # pin agrees (or vouches for nothing) — defer to the derivation above
        seq = rows[-1][0] if rows else 0
        return {"sealed": True, "since": None, "reason": None, "source": None,
                "seq": seq, "cause": pin_cause}

    def seal_state_snapshot(self):
        """One consistent snapshot of the seal table for a caller that needs the derived
        :meth:`seal_state` (plain, unpinned) AND the raw ``(head, count)`` pin pair from the
        *exact same* rows — mirrors :meth:`verify_snapshot` on the receipt side.

        **F3 (correctness redteam 2026-07-15):** ``pacioli anchor write`` used to build a v2 pin
        from THREE separate reads — ``seal_state()``, ``seal_head()``, ``seal_count()`` — each
        its own ``SELECT``. A concurrent writer (another CLI invocation, or an auto-CONTAIN
        ``close --respond``) landing between any two of those reads could pair a HEAD read
        before the write with a COUNT read after it (or vice versa), emitting a self-inconsistent
        pin — one that later false-alarms "seal history diverges from the off-box anchor"
        against a history that was never actually tampered with. One ``SELECT`` closes the
        window: nothing can land "between" reads that never happen.

        Returns ``(state, head, count)``: ``state`` is :meth:`_derive_seal_state`'s own dict for
        these rows (this method never threads a pin of its own — a caller checking a
        *previously recorded* pin still calls :meth:`seal_state` with its own
        ``expected_seal_head``/``expected_seal_count``, on whatever later snapshot it reads);
        ``head`` is the latest row's ``hmac`` or ``None`` for an empty table (mirrors
        :meth:`seal_head`); ``count`` is ``len(rows)`` (mirrors :meth:`seal_count`). Readable
        keyless, same least-exposure posture as :meth:`seal_state`/:meth:`seal_head`/
        :meth:`seal_count` themselves — this method does no key-gating of its own beyond what
        ``_derive_seal_state`` already does."""
        rows = self._conn.execute(
            "SELECT seq, ts, action, reason, source, hmac FROM seal_events ORDER BY seq"
        ).fetchall()
        state = self._derive_seal_state(rows)
        head = rows[-1][5] if rows else None
        return state, head, len(rows)

    def _derive_seal_state(self, rows):
        """The content-only derivation :meth:`seal_state` has always done — extracted verbatim so
        the pin check in :meth:`seal_state` can run AFTER it against the same ``rows`` without a
        second query. See :meth:`seal_state`'s own docstring for the full behavior contract; this
        split changes no behavior, only where the code lives."""
        if not rows:
            return {"sealed": True, "since": None, "reason": None, "source": None, "seq": 0,
                    "cause": "no seal history"}

        seqs = [row[0] for row in rows]
        if seqs != list(range(1, len(seqs) + 1)):
            seq = rows[-1][0]
            return {"sealed": True, "since": None, "reason": None, "source": None, "seq": seq,
                    "cause": "seal history gap (rollback?)"}

        seq, ts, action, reason, source, mac = rows[-1]

        if self._key is not None:
            # F1(a) (security redteam 2026-07-15): verify EVERY row's HMAC, not only the
            # latest's. Before this, a keyless attacker with DB-file write access could rewrite
            # an INTERIOR row's content (flip a past `seal`->`unseal`, launder a reason) and
            # leave its stored hmac untouched (they have no key to recompute a valid one) --
            # this method never looked at that row again once it was no longer "latest", so the
            # rewrite read clean (`cause=None`). `_verify_seal_row` is the same recompute this
            # method already ran for the latest row alone; `seal_events()` runs the identical
            # check per-row for its own `verified` flags -- reused here, not reimplemented.
            if not all(self._verify_seal_row(row) for row in rows):
                return {"sealed": True, "since": None, "reason": None, "source": None,
                        "seq": seq, "cause": "unverifiable"}

        return {"sealed": action == "seal", "since": ts, "reason": reason, "source": source,
                "seq": seq, "cause": None}

    def _verify_seal_row(self, row):
        """Recompute one ``seal_events`` row's HMAC under this store's key and compare it
        (``hmac.compare_digest``) against the stored value. Caller must already know
        ``self._key`` is not ``None`` — this is the single recompute both
        :meth:`_derive_seal_state` (every row, F1(a)) and :meth:`seal_events` (every row's
        ``verified`` flag) share, so the two never drift into computing it two different ways.
        Never raises: any failure evaluating the HMAC (a type error, etc.) is a content problem,
        not a connection problem, and folds into ``False`` — the caller's fail-closed answer,
        not a crash."""
        seq, ts, action, reason, source, mac = row
        try:
            return hmac.compare_digest(
                mac, _seal_event_hmac(self._key, seq, ts, action, reason, source)
            )
        except Exception:
            return False

    def seal_head(self):
        """The latest ``seal_events`` row's ``hmac`` (pin this off-box), or ``None`` for an empty
        table. Mirrors :meth:`head` on the receipt side exactly: a plain accessor, no key needed
        (the ``hmac`` column value is read as stored, not recomputed), so it is available on the
        same least-exposure keyless path :meth:`seal_state`/:meth:`seal_events` already are.

        Paired with :meth:`seal_count`, this is the (head, count) an operator records off-box —
        somewhere this box's own writer cannot reach — and later supplies back into
        :meth:`seal_state` as ``expected_seal_head``/``expected_seal_count`` to detect a tail
        rollback. See :meth:`seal_state`'s own docstring for the honest guarantee (audit-time
        detection, not prevention)."""
        row = self._conn.execute(
            "SELECT hmac FROM seal_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def seal_count(self):
        """The number of rows currently in ``seal_events`` — the count half of the off-box pin
        pair (:meth:`seal_head` is the head half). A plain ``COUNT(*)``, no key needed, same
        least-exposure posture as :meth:`seal_head`."""
        return self._conn.execute("SELECT COUNT(*) FROM seal_events").fetchone()[0]

    def seal_events(self):
        """The full ``seal_events`` history, oldest first, each row as a dict with a ``verified``
        flag: ``True``/``False`` when this store was opened keyed (the HMAC was recomputed and
        checked against the stored one), or ``None`` when opened keyless (no key in reach to check
        anything — the row is reported as-is, unverified, never silently upgraded to ``True``).

        This is the full audit surface behind :meth:`seal_state`'s single-row summary — every
        seal/unseal decision ever recorded, including ones that are no longer "latest" (a CLI
        ``seal-status`` render is expected to show a tail of this to a human; nothing in this
        method limits how much of it a caller reads).
        """
        rows = self._conn.execute(
            "SELECT seq, ts, action, reason, source, hmac FROM seal_events ORDER BY seq"
        ).fetchall()
        events = []
        for row in rows:
            seq, ts, action, reason, source, mac = row
            verified = None if self._key is None else self._verify_seal_row(row)
            events.append({"seq": seq, "ts": ts, "action": action, "reason": reason,
                            "source": source, "hmac": mac, "verified": verified})
        return events

    def _seed_seal_genesis(self):
        """Seed the ``seal_events`` genesis row on first KEYED open of a store whose table is
        empty — covers both a brand-new store (created moments ago by the ``executescript``
        immediately above, in ``__init__``) and an existing pre-0.20.0 store being opened keyed for
        the first time since this feature shipped (``CREATE TABLE IF NOT EXISTS`` in ``_SCHEMA``
        just added the empty table to it; this seeds it). Both cases look identical from here:
        zero rows, key present.

        Keyless opens NEVER seed — there is no key to HMAC a genesis row with, and a placeholder/
        unsigned genesis would be worse than no genesis at all (indistinguishable from a forgery to
        every later keyed reader). A store that has only ever been opened keyless (the mint CLI's
        least-exposure path) therefore has an empty ``seal_events`` table and reads as SEALED
        (fail-closed) via :meth:`seal_state` until its first keyed open — correct: an unverifiable
        store IS the sealed case, not a special "not yet initialized" exception to it.

        **Double-checked locking, not an unconditional** :meth:`_immediate` **(review F1)**: a
        keyed ``BrokerStore`` is constructed fresh on EVERY MCP tool dispatch (``tools.py``'s
        ``_route`` opens one per call via ``store_provider``), so this method runs on every single
        dispatch to a target for the rest of that store's life — not once at process start.
        Genesis seeding is load-bearing exactly ONCE per store (its first keyed open, ever), so
        the write lock has to be the EXCEPTION here, not the rule ``record_intent``/
        ``record_outcome`` correctly follow (those genuinely need the lock on every call, because
        every call might append). The first read below is a plain, transaction-free
        ``SELECT COUNT(*)`` — the same lock-free-read posture the migration functions above
        already take with their ``PRAGMA table_info`` checks: a read whose only job is to confirm
        "already done" does not get to hold the write lock hostage for every later caller.
        Only when that outer read sees zero rows does this reach for :meth:`_immediate` at all —
        and once inside it, the count is read AGAIN before appending, because another connection
        may have raced this one and already seeded between the two reads: skipping the re-check
        would let two connections opening the same fresh file at once both see zero from the outer
        read and both append a genesis row. The re-check inside the lock is what makes that
        impossible — the second connection blocks on ``BEGIN IMMEDIATE`` until the first commits,
        then sees the first's committed row on its re-check and skips.
        """
        if self._key is None:
            return
        count = self._conn.execute("SELECT COUNT(*) FROM seal_events").fetchone()[0]
        if count != 0:
            return
        with self._immediate():
            count = self._conn.execute("SELECT COUNT(*) FROM seal_events").fetchone()[0]
            if count == 0:
                self._append_seal_event("genesis", "seal state initialized", "init")

    def _append_seal_event(self, action, reason, source):
        """Insert one row into ``seal_events`` and return it as a dict. Caller must already hold
        the write lock (:meth:`_immediate`) — this method does no locking of its own, matching
        ``_head_receipt``/``_insert``'s internals-only contract.

        Inserts first with a placeholder ``hmac``, reads back the ``seq`` SQLite actually assigned
        (``cur.lastrowid`` — ``seq`` is ``INTEGER PRIMARY KEY AUTOINCREMENT``, an alias for
        ``rowid``), THEN computes the real HMAC over that assigned ``seq`` and corrects the row.
        This two-step is necessary, not incidental: the HMAC's canonical tuple includes ``seq``
        (:func:`_seal_event_canonical`), but only SQLite's own AUTOINCREMENT bookkeeping guarantees
        a deleted row's number is never reused — computing ``seq`` ourselves (e.g. ``MAX(seq)+1``
        over the currently-live rows) would let a deleted row's number silently come back after a
        DELETE, healing the exact gap :meth:`seal_state` relies on to detect an INTERIOR deletion
        (tail truncation — deleting the newest row(s) — is a separate, undetected case; see
        :meth:`seal_state`'s own docstring). Both statements run inside the caller's
        ``BEGIN IMMEDIATE``, so no other connection ever
        observes the placeholder — the row appears atomically, already correctly sealed.
        """
        ts = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO seal_events(ts, action, reason, source, hmac) VALUES(?,?,?,?,?)",
            (ts, action, reason, source, ""),
        )
        seq = cur.lastrowid
        mac = _seal_event_hmac(self._key, seq, ts, action, reason, source)
        self._conn.execute("UPDATE seal_events SET hmac = ? WHERE seq = ?", (mac, seq))
        return {"seq": seq, "ts": ts, "action": action, "reason": reason, "source": source}

    # --- close-record + attestation gate (Half 3, Fork A1) ------------------------
    def record_close(self, *, period_since, period_until, attested_head, gapped, reason="",
                     expected_last_close_seq):
        """Append a ``close`` row -- the store-side half of ``pacioli close --advance``.

        Refuses (:class:`CloseGateLatchedError`) if the gate is currently LATCHED -- derived
        freshly INSIDE this same ``BEGIN IMMEDIATE`` transaction (no separate pre-check), so a
        concurrent appender can't slip a close in between this call's own latch-check and its
        insert (no TOCTOU: the read that decides "is it safe to write" and the write itself share
        one lock acquisition, same discipline as :meth:`record_intent`/:meth:`record_outcome`).

        **Compare-and-append** (``expected_last_close_seq``, REQUIRED keyword — Task 2 builder's
        concurrency finding): the latch check alone never validated that the CALLER'S view of the
        cursor was still current at write time, so two concurrent advances could both read the
        same gate state, both pass the latch check inside their own transactions, and double-write
        overlapping periods. The caller passes the ``last_close_seq`` from the
        :meth:`close_gate_state` it planned its period bounds against (``None`` = "I expect no
        close record to exist yet"); inside this same transaction, AFTER the latch check, the
        current last close seq must equal it, else :class:`CloseRecordStaleError` — the loser's
        overlapping period never lands, and the operator redoes the period math against the moved
        cursor. This is the receipt chain's / seal's head-read+append discipline applied to the
        cursor. Ordering is pinned by test: the latch refusal fires FIRST when both apply (the
        gate is up for everyone; staleness is about this one caller's plan).

        **``period_until`` must be concrete** (redteam 2026-07-15, finding 3): ``None`` refuses
        with ``ValueError``. An open-ended close made two consecutive no-``--until`` advances
        both close genesis..now — full overlap, a double-count — and left the cursor
        indistinguishable from "no close ever" without the ``last_close_seq`` disambiguation.
        The CLI now materializes the effective until from the store clock (:meth:`clock_now`)
        before calling; HISTORICAL open-ended rows written before this rule remain tolerated,
        verified, dead rows in the derivation (their ``None`` cursor honestly reads as
        open-ended in ``close-status``).

        Refuses keyless (:meth:`_require_key`) -- same reasoning as every seal/receipt append:
        writing a close row with no key would append an entry no later keyed reader could
        distinguish from a forgery.

        ``gapped`` records whether ``gate_required`` (the response envelope's own computed verdict
        -- see ``response.py``) was true for the window this close covers; it is the ONLY thing
        that can later latch the gate (via a subsequent :meth:`close_gate_state` derivation).

        Returns the freshly recomputed :meth:`close_gate_state` (not just the appended row), same
        contract as :meth:`seal`/:meth:`unseal`.
        """
        self._require_key()
        if period_until is None:
            raise ValueError(
                "record_close requires a concrete period_until -- an open-ended close would let "
                "consecutive advances close overlapping genesis..now windows (double-count); "
                "materialize the effective until from the store clock (clock_now()) before "
                "calling"
            )
        with self._immediate():
            rows = self._close_rows()
            state = self._derive_close_gate_state(rows)
            if state["latched"]:
                raise CloseGateLatchedError(
                    f"close gate is LATCHED ({state['reason']}) -- attest the gap before "
                    "advancing the close cursor"
                )
            if state["last_close_seq"] != expected_last_close_seq:
                raise CloseRecordStaleError(
                    "the close cursor does not match: this close was planned against "
                    f"last_close_seq={expected_last_close_seq!r} but the store now shows "
                    f"{state['last_close_seq']!r} -- usually a concurrent advance landed "
                    "between the plan and this write, but this check cannot distinguish that "
                    "from history alteration (e.g. a deleted tail row); re-read the gate state, "
                    "recompute the period bounds, and retry -- if the mismatch recurs, inspect "
                    "`pacioli close-status` before retrying again"
                )
            self._append_close_record(
                "close", period_since, period_until, attested_head,
                1 if gapped else 0, reason, "close",
            )
        return self.close_gate_state()

    def attest(self, reason, *, expected_seq):
        """Append an ``attest`` row -- the operator ceremony that clears a gapped close, mirroring
        :meth:`unseal`'s shape exactly: always ``source="operator"``, a required ``reason``,
        append-only history (a correction is a new row, never an edit).

        Refuses keyless FIRST (:meth:`_require_key` raises ``ValueError`` before any gate
        derivation runs — same reasoning and same mechanism as :meth:`record_close`), so the
        derivation's own ``"keyless"`` cause is never what stops a keyless attest; the key check
        is.

        On a keyed store, refuses (:class:`NoAttestationPendingError`) unless the gate's LATCHED
        cause is precisely "a gapped close is awaiting attestation" -- an attest with nothing
        gapped pending refuses, and (deliberately) so does an attest attempted against a gate
        LATCHED for an INTEGRITY reason (a seq gap, an unverifiable HMAC): attesting cannot repair
        a corrupt history, only a human genuinely reviewing a genuine gap.

        **Compare-and-append** (``expected_seq``, REQUIRED keyword — redteam 2026-07-15,
        model-fidelity F1): the seq of the gapped close the operator actually REVIEWED (the
        ``last_close_seq`` from the gate state they planned against). Inside this same
        transaction, AFTER the pending check, the currently-pending gapped close's seq must equal
        it, else :class:`AttestStaleError` — without this, an attest could clear a DIFFERENT gap
        than the one its recorded reason describes (operator A reviews the JAN gap; operator B
        attests it and advances into a gapped FEB; A's attest with reason "reviewed JANUARY"
        would have cleared FEB — a permanent wrong-reason row). Ordering pinned by test: with
        nothing pending at all, :class:`NoAttestationPendingError` fires first (there is no gap
        to be stale ABOUT).

        Returns the freshly recomputed :meth:`close_gate_state`.
        """
        self._require_key()
        with self._immediate():
            rows = self._close_rows()
            state = self._derive_close_gate_state(rows)
            if state.get("cause") != "gapped_awaiting_attestation":
                raise NoAttestationPendingError(
                    "no gapped close is awaiting attestation -- attest only clears a real, "
                    "currently-pending gap"
                )
            if state["last_close_seq"] != expected_seq:
                raise AttestStaleError(
                    f"the pending gapped close is seq {state['last_close_seq']} "
                    f"(period {state['cursor'] or 'open-ended'}-ending), not "
                    f"seq {expected_seq!r} -- the gap awaiting attestation is not the one this "
                    "attest was planned against (usually another operator attested/advanced in "
                    "between, though this check cannot distinguish that from history "
                    "alteration); re-read `pacioli close-status` and review THE pending gap "
                    "before attesting"
                )
            self._append_close_record("attest", None, None, None, 0, reason, "operator")
        return self.close_gate_state()

    def clock_now(self):
        """The store's OWN clock reading (the injected ``now_iso`` — :func:`_utc_iso` in
        production). Exposed for the CLI's future-``--until`` check (redteam 2026-07-15,
        finding 1) and its no-``--until`` materialization (finding 3): both must compare/stamp
        against the SAME clock source ``record_close`` stamps rows with — never a second,
        potentially drifting ``datetime.now()`` of their own."""
        return self._now_iso()

    def close_cursor(self):
        """The **verified** cursor: the ``period_until`` of the latest ``close``-action row, read
        through the exact same derivation as :meth:`close_gate_state` — never straight off the
        row (adversarial review finding 1: the cursor is period-loop CONTROL data, deciding where
        the next period starts, so an unverified read path for it would let a tampered history
        serve a forged cursor even while ``close_gate_state`` on the same rows correctly latches).

        **Fails closed** on any integrity failure — a ``seq`` gap, any row whose HMAC does not
        verify, or a keyless open — by raising :class:`CloseRecordIntegrityError`, never by
        returning a value. A WORKFLOW latch (a verified gapped close awaiting attestation) is not
        an integrity failure: every row's HMAC checks out there, so the cursor is trustworthy and
        still returned (the advance path's own refusal render needs it to name the stuck period).
        ``close_gate_state()`` itself stays readable keyless/latched — status surfaces are never
        gated (Global constraint 5); only the cursor-as-a-value fails closed.

        **``None`` is ambiguous alone** (adversarial review finding 2): it means EITHER "no close
        record exists yet" (honest genesis — the first advance is legitimate) OR "the latest close
        was legitimately open-ended (``period_until=None``, matching statement semantics)". The
        one verified, documented way to distinguish — what the advance path's ``--since`` default
        logic must use — is :meth:`close_gate_state`: ``state["last_close_seq"] is None`` ⇔ no
        close row exists; a seq number means a close exists and ``state["cursor"]`` is its
        verified ``period_until`` (``None`` there = that close was open-ended)."""
        state = self.close_gate_state()
        if state["cause"] in ("keyless", "gap", "unverifiable"):
            raise CloseRecordIntegrityError(
                f"close-record history failed verification ({state['reason']}) -- refusing to "
                "serve a cursor off unverified rows; investigate before advancing any period"
            )
        return state["cursor"]

    def close_gate_state(self, *, expected_close_head=None, expected_close_count=None):
        """Derive the current attestation-gate state from the ``close_records`` history -- never
        from a bare flag, same discipline as :meth:`seal_state`. See
        :meth:`_derive_close_gate_state` for the full, deny-biased derivation; this is a thin
        one-query wrapper around it (mirrors :meth:`seal_state`'s own shape).

        Returns ``{"latched": bool, "reason": str, "cause": str|None, "cursor": str|None,
        "last_close_seq": int|None}``. ``reason`` is ``""`` when NOT latched (unlike the seal's
        ``None``-when-clean convention) -- always a string, never absent. ``cause`` is a stable,
        machine-checkable tag (``"keyless"``, ``"gap"``, ``"unverifiable"``, or
        ``"gapped_awaiting_attestation"``) when latched, else ``None`` -- :meth:`attest` checks
        this exact tag to tell "a real gap is pending" apart from "the history itself is broken".

        **Honest tamper ceiling — per-row keyed integrity, NOT prefix-chaining (redteam
        2026-07-15, honesty F1; mirrors** :meth:`seal_state` **'s essay, scaled to what this
        table actually has).** Against a keyless forger this derivation catches an interior row
        deletion (seq contiguity) and any content edit (per-row HMAC). It does **NOT** catch a
        TAIL-ROW DELETION on its own: deleting the newest ``close_records`` row(s) leaves
        survivors contiguous ``1..N-1`` with genuinely-verifying HMACs, so the gate reads clean
        and the CURSOR SILENTLY ROLLS BACK to an earlier period's until — the same disclosed
        limit ``seal_events`` has, closed the same way (the count-anchor slice, 2026-07-16):
        :meth:`close_head`/:meth:`close_count` expose this table's own ``(head, count)`` pin
        pair, and supplying a previously recorded pin back in as ``expected_close_head``/
        ``expected_close_count`` (keyword-only; with neither supplied this method is
        **byte-identical** to the plain derivation — a regression pin proven by test) closes
        exactly that ceiling: count went DOWN → ``latched=True, cause="anchor_behind"``; the
        row AT the pinned count (looked up by ``seq`` value) no longer carries the pinned
        ``hmac`` → ``cause="anchor_diverged"``; a malformed pin → ``cause="anchor_malformed"``,
        folded, never raised (see :func:`_check_close_pin`). A pin failure outranks the plain
        derivation's own answer and NULLS ``cursor``/``last_close_seq`` (rollback evidence
        means row content is not trustworthy — same nulling rule the integrity causes follow);
        an AGREEING pin never unlatches anything (checked on top of the derivation, never
        instead of it). Without a supplied pin, the only tripwire remains a compare-and-append
        mismatch on a later write (:class:`CloseRecordStaleError` — which honestly cannot tell
        rollback from a benign race). The pin's own ceiling, stated honestly: this table is
        per-row HMAC'd, not prefix-chained, so a pin fixes the ONE row it names (plus the
        count) — a KEY HOLDER rewriting any other row, or appending inert validly-signed rows
        (rows whose ``action`` is outside the close/attest vocabulary cannot latch, unlatch, or
        move the cursor; their HMAC and seq are still checked like every row's), is not caught:
        key possession is authorship everywhere else in this store too. Truth over implication:
        this gate is a workflow latch with keyed integrity checks plus audit-time rollback
        detection when the operator supplies a pin, not a rollback-proof ledger.
        """
        rows = self._close_rows()
        result = self._derive_close_gate_state(rows)

        if expected_close_head is None and expected_close_count is None:
            return result  # no pin supplied: byte-identical to the pre-anchor derivation

        pin = _check_close_pin(rows, expected_close_head, expected_close_count)
        if pin is None:
            return result  # pin agrees (or vouches for nothing) — defer to the derivation
        cause, reason = pin
        return {"latched": True, "reason": reason, "cause": cause,
                "cursor": None, "last_close_seq": None}

    def close_records_snapshot(self):
        """One consistent read of the close-record gate state AND the raw history rows together --
        mirrors :meth:`seal_state_snapshot` exactly, for the same reason (F3, security redteam
        2026-07-15 on the seal side): a caller needing both the derived state and the underlying
        rows (a future ``close-status`` render, an anchor pin) must reason about ONE snapshot, not
        two separate queries a concurrent writer could land between.

        Returns ``(state, rows)`` where ``state`` is exactly what :meth:`close_gate_state` would
        return for these same rows, and ``rows`` is the raw
        ``(seq, ts, action, period_since, period_until, attested_head, gapped, reason, source,
        hmac)`` tuples, oldest first."""
        rows = self._close_rows()
        return self._derive_close_gate_state(rows), rows

    def close_head(self):
        """The latest ``close_records`` row's ``hmac`` (pin this off-box), or ``None`` for an
        empty table. Mirrors :meth:`seal_head` exactly: a plain accessor, no key needed (the
        ``hmac`` column value is read as stored, not recomputed), available on the same
        least-exposure keyless path.

        Paired with :meth:`close_count`, this is the (head, count) an operator records off-box —
        somewhere this box's own writer cannot reach — and later supplies back into
        :meth:`close_gate_state` as ``expected_close_head``/``expected_close_count`` to detect a
        tail rollback (the silent cursor rollback). Audit-time detection, not prevention — see
        :meth:`close_gate_state`'s honest-tamper-ceiling paragraph for the exact guarantee.

        Unlike ``seal_events`` (genesis-seeded on first keyed open) this table legitimately has
        zero rows — "no period has ever closed yet" — so ``(None, 0)`` is a REAL pin value, and
        :func:`_check_close_pin` accepts it verbatim (a count of 0 vouches for nothing on
        replay, but "nothing closed as of ts" is still a claim worth recording)."""
        row = self._conn.execute(
            "SELECT hmac FROM close_records ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def close_count(self):
        """The number of rows currently in ``close_records`` — the count half of the off-box
        pin pair (:meth:`close_head` is the head half). A plain ``COUNT(*)``, no key needed,
        same least-exposure posture as :meth:`close_head`/:meth:`seal_count`."""
        return self._conn.execute("SELECT COUNT(*) FROM close_records").fetchone()[0]

    def _close_rows(self):
        return self._conn.execute(
            "SELECT seq, ts, action, period_since, period_until, attested_head, gapped, reason,"
            " source, hmac FROM close_records ORDER BY seq"
        ).fetchall()

    def _derive_close_gate_state(self, rows):
        """The deny-biased derivation behind :meth:`close_gate_state` — every branch that cannot
        fully vouch for the history answers ``latched=True`` rather than guessing open, with ONE
        deliberate exception (the first bullet below) pinned by the design doc's Global
        constraint 4.

        - **Keyless open** (``self._key is None``) → ``latched=True, cause="keyless"``. There is
          no key in reach to verify any row's HMAC at all, so this refuses to even inspect row
          content — checked FIRST, before touching ``rows``.
        - **Zero rows** → ``latched=False, cause=None``. Deliberately the OPPOSITE of the seal's
          zero-rows=SEALED: absence of close-record history is the honest genesis state for a
          *cursor* ("no period has ever closed yet"), not a fail-closed case — the first
          ``--advance`` against an empty table is legitimate.
        - **A gap in ``seq`` 1..N** → ``latched=True, cause="gap"`` (``seq`` is ``AUTOINCREMENT``,
          never reused — see ``_SCHEMA`` — so a missing number means a row was deleted).
        - **Any row's HMAC does not verify** → ``latched=True, cause="unverifiable"`` — every row
          is recomputed and checked, not only the latest's, same F1(a) discipline as the seal.
        - **Otherwise**: find the latest ``close``-action row (if any). If it has ``gapped=1`` AND
          no LATER ``attest`` row exists → ``latched=True,
          cause="gapped_awaiting_attestation"``. Any other outcome (no close rows yet, latest
          close not gapped, or a later attest already covers it) → ``latched=False, cause=None``.

        ``cursor``/``last_close_seq`` are nulled on every INTEGRITY-failure branch (keyless, gap,
        unverifiable) — the row content itself is not trustworthy there, so nothing derived from it
        is surfaced, same "don't smuggle possibly-doctored content next to a fail-closed verdict"
        rule the seal's ``since``/``reason``/``source`` nulling follows. The
        ``gapped_awaiting_attestation`` case is different in kind — the content IS verified/
        contiguous, only the WORKFLOW gate is up — so ``cursor``/``last_close_seq`` stay honestly
        populated there (an operator checking ``close-status`` while latched still needs to see
        which period is stuck).
        """
        if self._key is None:
            return {"latched": True,
                    "reason": "no seal key available -- close-record integrity cannot be "
                              "verified keyless",
                    "cause": "keyless", "cursor": None, "last_close_seq": None}

        if not rows:
            return {"latched": False, "reason": "", "cause": None, "cursor": None,
                    "last_close_seq": None}

        seqs = [row[0] for row in rows]
        if seqs != list(range(1, len(seqs) + 1)):
            return {"latched": True, "reason": "close-record history gap (rollback?)",
                    "cause": "gap", "cursor": None, "last_close_seq": None}

        if not all(self._verify_close_row(row) for row in rows):
            return {"latched": True,
                    "reason": "close-record history is unverifiable (hmac mismatch)",
                    "cause": "unverifiable", "cursor": None, "last_close_seq": None}

        close_rows = [row for row in rows if row[2] == "close"]
        if not close_rows:
            return {"latched": False, "reason": "", "cause": None, "cursor": None,
                    "last_close_seq": None}

        latest_close = close_rows[-1]
        latest_close_seq = latest_close[0]
        cursor = latest_close[4]  # period_until
        gapped = latest_close[6]

        if gapped:
            later_attest = any(row[2] == "attest" and row[0] > latest_close_seq for row in rows)
            if not later_attest:
                return {"latched": True,
                        "reason": f"close at seq {latest_close_seq} closed over a gap "
                                  "(gate_required) and has not been attested",
                        "cause": "gapped_awaiting_attestation",
                        "cursor": cursor, "last_close_seq": latest_close_seq}

        return {"latched": False, "reason": "", "cause": None, "cursor": cursor,
                "last_close_seq": latest_close_seq}

    def _verify_close_row(self, row):
        """Recompute one ``close_records`` row's HMAC under this store's key and compare it
        (``hmac.compare_digest``) against the stored value. Caller must already know
        ``self._key`` is not ``None``. Never raises: any failure evaluating the HMAC folds into
        ``False`` — a content problem, not a connection problem — same posture as
        :meth:`_verify_seal_row`."""
        seq, ts, action, period_since, period_until, attested_head, gapped, reason, source, mac = row
        try:
            return hmac.compare_digest(
                mac,
                _close_record_hmac(self._key, seq, ts, action, period_since, period_until,
                                    attested_head, gapped, reason, source),
            )
        except Exception:
            return False

    def _append_close_record(self, action, period_since, period_until, attested_head, gapped,
                              reason, source):
        """Insert one row into ``close_records`` and return it as a dict. Caller must already hold
        the write lock (:meth:`_immediate`) — same two-step insert-then-correct-the-hmac pattern as
        :meth:`_append_seal_event` (see its docstring for why the two steps are necessary: the
        HMAC's canonical tuple includes ``seq``, and only SQLite's own AUTOINCREMENT bookkeeping
        guarantees a deleted row's number is never reused)."""
        ts = self._now_iso()
        cur = self._conn.execute(
            "INSERT INTO close_records(ts, action, period_since, period_until, attested_head,"
            " gapped, reason, source, hmac) VALUES(?,?,?,?,?,?,?,?,?)",
            (ts, action, period_since, period_until, attested_head, gapped, reason, source, ""),
        )
        seq = cur.lastrowid
        mac = _close_record_hmac(self._key, seq, ts, action, period_since, period_until,
                                  attested_head, gapped, reason, source)
        self._conn.execute("UPDATE close_records SET hmac = ? WHERE seq = ?", (mac, seq))
        return {"seq": seq, "ts": ts, "action": action, "period_since": period_since,
                "period_until": period_until, "attested_head": attested_head, "gapped": gapped,
                "reason": reason, "source": source}

    # --- internals ---------------------------------------------------------------
    @contextmanager
    def _immediate(self):
        """A ``BEGIN IMMEDIATE`` transaction: grabs the write lock up front so the whole
        read-then-write critical section is serialised. Commits on success, rolls back on any error."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _head_receipt(self):
        row = self._conn.execute(
            "SELECT seq, prev_hash, kind, body, ts, hmac FROM receipts ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return self._row_to_receipt(row) if row else None

    def _row_to_receipt(self, row):
        seq, prev_hash, kind, body, ts, mac = row
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, TypeError) as exc:
            # A receipt row SQLite hands back cleanly but whose body no longer parses is store
            # corruption (a tail-truncation deep enough to garble a body while leaving the file
            # otherwise openable — redteam verify pass). Surface it as the SAME StoreCorruptError a
            # torn FILE raises, so it flows through dispatch()'s structured store-deny and the CLI's
            # catches, never a raw json error crashing past every handler.
            raise StoreCorruptError(
                f"receipt seq {seq} has a corrupt (unparseable) body — the store is damaged; "
                f"investigate against backups / the off-box anchor pin before trusting it ({exc})"
            ) from exc
        return Receipt(seq=seq, prev_hash=prev_hash, kind=kind, body=parsed, ts=ts, hmac=mac)

    def _insert(self, r):
        self._conn.execute(
            "INSERT INTO receipts(seq, prev_hash, kind, body, ts, hmac) VALUES(?,?,?,?,?,?)",
            (r.seq, r.prev_hash, r.kind, json.dumps(r.body, sort_keys=True), r.ts, r.hmac),
        )


class SubmitEffects:
    """The ``effects`` the spine needs, assembled from a :class:`BrokerStore` (persistence) and an
    ``execute`` callable (the bench submit, supplied by the ERPNext glue). Keeps persistence and the
    bench call cleanly separate — the store never imports anything bench-facing.

    ``readback`` (transport taxonomy, docs/plans/2026-07-07-transport-taxonomy.md) is an optional
    zero-arg callable the glue wires to a governed re-read of the document's real docstatus
    (``client.get_document(doctype, name).get("docstatus")``) — used only on the spine's "no
    answer" branch, when ``execute`` raised an exception the transport layer could not classify as
    an answered refusal. It may raise; ``spine.governed_submit`` owns degrading that to
    ``readback_error`` rather than letting it crash the flow. Left unwired (``None``) by callers
    that never exercise that branch (e.g. the happy-path store tests)."""

    def __init__(self, store, execute, readback=None):
        self._store = store
        self._execute = execute
        self._readback = readback

    def claim_marker(self, reserved):
        return self._store.claim_marker(reserved)

    def record_intent(self, body):
        return self._store.record_intent(body)

    def execute(self):
        return self._execute()

    def readback(self):
        if self._readback is None:
            raise RuntimeError("no readback capability was wired for this effects instance")
        return self._readback()

    def record_outcome(self, intent, status, result, final_marker):
        return self._store.record_outcome(intent, status, result, final_marker)
