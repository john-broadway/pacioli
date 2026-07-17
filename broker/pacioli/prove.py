# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — PROVE: the journal (pure hash-chained, HMAC-keyed receipt book).

This is the *giornale*: every mutation in fixed form, chronological, permanent — the intent receipt
is the entry's *per* (debit), the outcome its *a* (credit), and an intent with no outcome is a debit
with no credit, surfaced exactly the way an unbalanced trial balance surfaces a bad book.

Intended to be **append-only** — that immutability is enforced by the glue's storage layer (and,
against a host-level rewrite, only by the off-box head anchor — ``anchor.py`` + the
``pacioli anchor`` CLI); this pure core
provides no storage and no append-only mechanism itself, only the chaining/sealing. Every mutation
the broker makes leaves two chained receipts: an ``intent`` (written
*before* the execute, so a crash between execute and finalize still leaves a durable "we were about
to post X" record) and an ``outcome`` (the result, referencing the intent it finalizes). An intent
with no matching outcome is an ``orphan`` — a crash gap to reconcile against the real ``docstatus``.

No frappe, no I/O, no clock: ``append`` takes the timestamp and the sealing ``key`` as arguments, so
the security-critical chaining logic is unit-testable without a bench. The glue (``erpnext.py``)
supplies the store and the clock; the **key lives off-box / not colocated with the agent** (see
SPEC §2, §5) — this core only computes with it.

**Honesty (SPEC §5):** an on-box chain is tamper-evident against *API users* but NOT against anyone
with file access on this host, including the agent whose actions this ledger records. It is only
tamper-evident against a rewrite once the head is pinned **off-box** — ``pacioli anchor write``
emits the pin, but carrying it off this host is the operator's discipline. Do not call it
"tamper-evident" without the "since the last pin" qualification.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass

GENESIS = "0" * 64  # the prev_hash of the first receipt (seq 0)

INTENT = "intent"
OUTCOME = "outcome"


@dataclass(frozen=True)
class Receipt:
    """One sealed link in the receipt chain.

    :param seq: 0-based position in the chain.
    :param prev_hash: the ``hmac`` of the preceding receipt (``GENESIS`` for seq 0).
    :param kind: ``"intent"`` (pre-execute) or ``"outcome"`` (post-execute result).
    :param body: the mutation details (actor, tool, target, transition, plan id) for an intent, or
        ``{"finalizes": <intent seq>, "status": ...}`` for an outcome. Any JSON-serialisable mapping.
    :param ts: caller-supplied timestamp string (this core has no clock).
    :param hmac: the chained seal — ``HMAC-SHA256(key, canonical(seq, prev_hash, kind, body, ts))``.
    """

    seq: int
    prev_hash: str
    kind: str
    body: dict
    ts: str
    hmac: str


_NATIVE = (str, int, float)  # bool is a subclass of int; None handled explicitly


def _ensure_json_native(obj, path="body"):
    """Reject anything that isn't strictly JSON-native, with string dict keys. Fails closed.

    ``json.dumps(default=str)`` would silently collapse a ``Decimal("10.50")`` (or any object) to the
    string ``"10.50"`` — indistinguishable, under the seal, from the literal string ``"10.50"`` — so
    two semantically different receipts could hash identically. Non-string keys and non-native values
    are refused before sealing rather than coerced."""
    if isinstance(obj, float) and not math.isfinite(obj):
        # NaN/Infinity are not RFC-8259 JSON and would break the off-box anchor's strict parser;
        # in a financial ledger they are also almost certainly a corrupt amount. Refuse them.
        raise ValueError(f"{path}: non-finite float ({obj!r}) cannot be sealed into a receipt")
    if obj is None or isinstance(obj, _NATIVE):
        return
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            _ensure_json_native(v, f"{path}[{i}]")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError(f"{path}: receipt keys must be strings, got {type(k).__name__}")
            _ensure_json_native(v, f"{path}.{k}")
        return
    raise ValueError(
        f"{path}: receipt body must be JSON-native (str/int/float/bool/None/list/dict), "
        f"got {type(obj).__name__} — normalise it in the glue before sealing"
    )


def _canonical(seq, prev_hash, kind, body, ts):
    """Deterministic bytes for the sealed fields. ``sort_keys`` + no whitespace so the same logical
    content always seals identically regardless of dict ordering. No ``default`` — non-native values
    raise (they are rejected up front by :func:`_ensure_json_native`) rather than lossily stringify."""
    return json.dumps(
        {"seq": seq, "prev_hash": prev_hash, "kind": kind, "body": body, "ts": ts},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _seal(key, seq, prev_hash, kind, body, ts):
    return hmac.new(key, _canonical(seq, prev_hash, kind, body, ts), hashlib.sha256).hexdigest()


def append(key, prev, kind, body, ts):
    """Seal a new receipt onto the chain. ``prev`` is the last :class:`Receipt` or ``None`` (genesis).

    Pure: derives ``seq`` and ``prev_hash`` from ``prev`` and returns a new frozen :class:`Receipt`.
    Raises ``ValueError`` if ``body`` is not strictly JSON-native (see :func:`_ensure_json_native`).
    """
    _ensure_json_native(body)
    if prev is None:
        seq, prev_hash = 0, GENESIS
    else:
        seq, prev_hash = prev.seq + 1, prev.hmac
    return Receipt(
        seq=seq,
        prev_hash=prev_hash,
        kind=kind,
        body=body,
        ts=ts,
        hmac=_seal(key, seq, prev_hash, kind, body, ts),
    )


def verify_chain(key, receipts, expected_head=None):
    """Recompute every seal and check linkage. Returns ``(True, None)`` or ``(False, reason)``.

    Detects, *within the given list*: a tampered field (the recomputed HMAC won't match), the wrong
    key (same), and a dropped / reordered / inserted receipt in the *middle* (``seq`` / ``prev_hash``
    linkage breaks). Fails closed on the first bad link, naming its index.

    **What it does NOT detect on its own:** truncation of the *tail* or a fully-replaced/empty list —
    a shortened chain is still internally self-consistent, and dropping the newest receipt hides the
    most-likely-fraudulent transaction. That gap closes only with the **off-box head anchor**: pass
    ``expected_head`` (the last-seen head ``hmac`` recorded off-box) and this checks the actual head
    matches it, catching tail-truncation and full wipes. ``anchor.py`` carries the full pin record
    (head + count) and the position-aware comparison; without a pin actually kept off-box, an
    on-box chain is not tamper-evident against a host-level rewrite (SPEC §5).
    """
    prev = None
    for i, r in enumerate(receipts):
        expected_seq = 0 if prev is None else prev.seq + 1
        expected_prev = GENESIS if prev is None else prev.hmac
        if r.seq != expected_seq:
            return (False, f"receipt {i}: seq {r.seq} != expected {expected_seq}")
        if r.prev_hash != expected_prev:
            return (False, f"receipt {i}: prev_hash does not chain to previous receipt")
        if not hmac.compare_digest(r.hmac, _seal(key, r.seq, r.prev_hash, r.kind, r.body, r.ts)):
            return (False, f"receipt {i}: seal does not verify (tampered body or wrong key)")
        prev = r
    if expected_head is not None:
        actual = prev.hmac if prev is not None else GENESIS
        if not hmac.compare_digest(actual, expected_head):
            return (False, "chain head does not match the off-box anchor (tail truncated or wiped)")
    return (True, None)


def orphans(receipts):
    """Intent receipts not finalized by a **committed** outcome — a mutation whose success was never
    recorded. Reconcile each against the doc's real ``docstatus``.

    Only a ``"committed"`` outcome finalizes an intent. A ``"failed"``/uncertain outcome (e.g. a
    timeout that may actually have landed server-side) does **not** clear the intent — it stays an
    orphan so the sweep still verifies whether a real posting happened."""
    finalized = {
        r.body.get("finalizes")
        for r in receipts
        if r.kind == OUTCOME and isinstance(r.body, dict) and r.body.get("status") == "committed"
    }
    return [r for r in receipts if r.kind == INTENT and r.seq not in finalized]


def head(receipts):
    """The last receipt (the chain head to pin off-box), or ``None`` for an empty chain."""
    return receipts[-1] if receipts else None
