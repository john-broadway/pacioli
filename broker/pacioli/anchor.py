# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — ANCHOR: the registered book (pure off-box head-pin record for the PROVE chain
and, since format v2, the seal history alongside it; since format v3, the close-record history
too — one pin, all three chains).

The books take their authority from a mark held outside the bookkeeper's own hand: a registration
the book cannot rewrite, so a truncated or swapped book confesses against it.

An on-box receipt chain has one blind spot ``verify_chain`` cannot close from the inside: a
tail-truncation or a full wipe leaves a chain that is still internally self-consistent. The fix is
a **pin recorded somewhere this host cannot touch**: the chain head hmac + the receipt count at a
moment in time. The seal history (``seal_events``) has the exact same blind spot (``store.py``'s
``seal_state`` docstring) — a keyless attacker with DB-file write access can delete the newest
seal-event row(s) and a genuine earlier row becomes "latest", undetectable from content alone.
Format v2 pins the seal head+count in the SAME record, so one off-box pin covers both. This module
is the pure half of that fix — the record format and the deny-biased comparison. It has no I/O, no
clock, and **never sees the seal key**: an anchor is made from already-computed hmacs, so nothing
in an anchor record is secret (a head hmac is a public pin; possessing it forges nothing).

**Audit-time DETECTION, not real-time prevention** — for the seal head exactly as for the receipt
head (see the honest-guarantee paragraph in ``docs/plans/2026-07-15-seal-anchor-slice.md`` and
``store.py``'s own ``seal_state`` docstring). Nothing here blocks a write, on-box, ever; what a
pin buys is that a rollback stops being silent to an operator who checks it.

**What comparing an anchor against the live chain proves (and what it does not):**

* ``count`` went DOWN → tampering (tail truncated or wiped). Deny.
* same ``count``, different head → tampering (the chain was rewritten since the pin). Deny.
* ``count`` grew → the anchored head must still sit on the chain at its pinned position
  (``chain_hmacs[count-1]``). Because each hmac commits to its entire prefix through
  ``prev_hash`` chaining, a matching hmac at that position fixes the whole pre-pin history —
  **even against a seal-key holder** (rewriting the prefix while preserving the pinned hmac would
  need an HMAC-SHA256 collision). If it moved or changed → deny.
* What is **not** detectable: anything appended, rewritten, or truncated **after the last pin** —
  the window between pins is unprotected, which is why the honest claim is "tamper-evident since
  the last pin," never more. And ``compare`` checks positions and values only; it does NOT verify
  seals — the caller must pair it with a keyed ``verify_chain``/``store.verify()`` or a forged
  suffix would sail through.

Parsing is strict and fails closed: wrong format marker, a missing/extra/mistyped field, a head
that is not 64 lowercase hex, a negative count, or a count/head combination that is internally
inconsistent (count 0 must pin ``GENESIS``; a non-empty chain never has a ``GENESIS`` head) are
all refused with a reason, never coerced. A malformed anchor must read as "cannot check," not "ok."

**Format v2 — the seal head rides alongside the receipt head.** A v1 record (``pacioli_anchor``
== 1) has exactly the five receipt-side fields above and never carries ``seal_head``/
``seal_count``; parsing it is not an error — it is simply a pin that predates seal anchoring (the
CLI's ``anchor check`` warns of this, it does not refuse it). A v2 record (``pacioli_anchor`` ==
2) has those five fields PLUS ``seal_head`` (the seal table's head hmac, mirrors
:meth:`~pacioli.store.BrokerStore.seal_head`) and ``seal_count`` (mirrors
:meth:`~pacioli.store.BrokerStore.seal_count`) — both required together, never one without the
other, and both subject to the exact same shape rules the receipt head/count already have: 64
lowercase hex, a non-negative non-bool int, and the same ``(count == 0) == (head == GENESIS)``
invariant (the seal table has no chain of its own, so ``GENESIS`` is reused here purely as the
"nothing pinned yet" sentinel — never a claim the seal history is hash-chained the way receipts
are). A record that names a version it does not have the matching fields for — v2 missing either
seal field, v1 carrying a stray one — is refused exactly like every other shape violation: never
coerced into the other version, never half-read.

**Format v3 — the close-record head rides too (the count-anchor slice, 2026-07-16).**
``close_records`` (the period-close cursor + attestation gate) has the same tail-deletion blind
spot the seal table had — deleting the newest close row(s) silently ROLLS THE CURSOR BACK to an
earlier period — and it closes the same way: a v3 record (``pacioli_anchor`` == 3) is a v2 record
PLUS ``close_head`` (mirrors :meth:`~pacioli.store.BrokerStore.close_head`) and ``close_count``
(mirrors :meth:`~pacioli.store.BrokerStore.close_count`), both required together, same shape
rules, same ``GENESIS``-as-sentinel reuse (``close_records`` is per-row HMAC'd, not chained; a
count of 0 pins the honest "no period has ever closed yet" genesis state). v3 is a strict
superset of v2 — close fields without seal fields are refused (a keyed ``anchor write`` always
has both pairs in reach, so that shape never legitimately exists). The close-side comparison,
like the seal's, is not done here: it is :meth:`~pacioli.store.BrokerStore.close_gate_state`'s
``expected_close_head``/``expected_close_count`` kwargs, run by ``anchor check`` against a keyed
store.
"""
from __future__ import annotations

import hmac
import json
import re

from pacioli.prove import GENESIS

FORMAT_KEY = "pacioli_anchor"
FORMAT_VERSION = 3  # the current/preferred format this module emits when close fields are given

_FIELDS_V1 = (FORMAT_KEY, "target", "head", "count", "ts")
_SEAL_FIELDS = ("seal_head", "seal_count")
_CLOSE_FIELDS = ("close_head", "close_count")
_FIELDS_V2 = _FIELDS_V1 + _SEAL_FIELDS
_FIELDS_V3 = _FIELDS_V2 + _CLOSE_FIELDS
_SUPPORTED_VERSIONS = (1, 2, FORMAT_VERSION)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _validate_head_count_pair(record, head_field, count_field, label):
    """Shared shape check for a (head, count) pair — used for both the receipt fields (``head``/
    ``count``) and, in a v2 record, the seal fields (``seal_head``/``seal_count``). ``label``
    names the pair in error text so a v2 failure is never mistaken for a receipt-side one."""
    if not isinstance(record[head_field], str) or not record[head_field]:
        raise ValueError(f"anchor field {head_field!r} must be a non-empty string")
    count = record[count_field]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"anchor field {count_field!r} must be a non-negative integer")
    if not _HEX64.match(record[head_field]):
        raise ValueError(f"anchor field {head_field!r} must be 64 lowercase hex characters")
    if (count == 0) != (record[head_field] == GENESIS):
        raise ValueError(f"anchor is inconsistent: {label} count 0 must pin the GENESIS "
                         f"{label} head, and a non-zero {label} count never does")


def _validate(record):
    """Strict shape check. Raises ``ValueError`` with a reason; returns the record unchanged.

    Deny-biased on purpose: unknown keys are refused (a v1 record has exactly its five fields, a
    v2 record exactly its seven — a record from an unsupported format, or a version/field
    mismatch, must be rejected, not half-read), and bools are refused as counts (``bool`` is an
    ``int`` subclass Python would happily accept)."""
    if not isinstance(record, dict):
        raise ValueError(f"anchor must be a JSON object, got {type(record).__name__}")
    version = record.get(FORMAT_KEY)
    # F4 (correctness redteam 2026-07-15): ``bool`` is an ``int`` subclass, so JSON ``true``
    # parses to Python ``True`` and ``True == 1`` / ``True in _SUPPORTED_VERSIONS`` are both
    # true — without this guard, ``{"pacioli_anchor": true, ...}`` would silently pass as a v1
    # record. This module's own docstring already calls out bool-exclusion for the head/count
    # pairs (:func:`_validate_head_count_pair`); the version marker had the identical gap.
    if isinstance(version, bool) or version not in _SUPPORTED_VERSIONS:
        raise ValueError(f"not a pacioli anchor (expected {FORMAT_KEY} to be one of "
                         f"{_SUPPORTED_VERSIONS!r}, got {version!r})")
    fields = {1: _FIELDS_V1, 2: _FIELDS_V2, 3: _FIELDS_V3}[version]
    missing = [f for f in fields if f not in record]
    if missing:
        raise ValueError(f"anchor is missing field(s): {', '.join(sorted(missing))}")
    extra = [k for k in record if k not in fields]
    if extra:
        raise ValueError(f"anchor has unknown field(s): {', '.join(sorted(extra))}")
    for f in ("target", "head", "ts"):
        if not isinstance(record[f], str) or not record[f]:
            raise ValueError(f"anchor field {f!r} must be a non-empty string")
    _validate_head_count_pair(record, "head", "count", "receipt")
    if version >= 2:
        _validate_head_count_pair(record, "seal_head", "seal_count", "seal")
    if version >= 3:
        _validate_head_count_pair(record, "close_head", "close_count", "close")
    return record


def make_anchor(target, head, count, ts, *, seal_head=None, seal_count=None,
                close_head=None, close_count=None):
    """Build a validated anchor record for a chain head; when both ``seal_head`` and
    ``seal_count`` are supplied, the seal head/count ride alongside it (format v2); when
    ``close_head``/``close_count`` are ALSO supplied, the close-record head/count ride too
    (format v3 — the count-anchor slice, 2026-07-16). ``head`` is the chain-head hmac
    (``GENESIS`` for an empty chain), ``count`` the receipt count, ``ts`` a caller-supplied
    timestamp (this core has no clock).

    All four extra fields are keyword-only and default to ``None``: omitting a pair emits the
    older format — **existing callers that never pass them are byte-identical to before that
    format existed** (the seal slice's Global Constraint 4, inherited by the close pair).
    Supplying only one of a pair is refused (the same "both or neither" discipline the store
    side's pin checks already enforce) — a partial pin is never silently coerced into either
    shape. The close pair additionally REQUIRES the seal pair: v3 is a superset of v2 (``anchor
    write`` always has both pairs in reach on a keyed store), so close-without-seal is a shape
    violation, not a real pin.

    Raises ``ValueError`` on anything malformed — the producer side is validated as strictly as
    the consumer side, so a bad pin is refused at write time, not discovered at check time."""
    if (seal_head is None) != (seal_count is None):
        raise ValueError("anchor seal fields must be supplied together: both seal_head and "
                         "seal_count, or neither")
    if (close_head is None) != (close_count is None):
        raise ValueError("anchor close fields must be supplied together: both close_head and "
                         "close_count, or neither")
    has_seal = seal_head is not None or seal_count is not None
    has_close = close_head is not None or close_count is not None
    if has_close and not has_seal:
        raise ValueError("anchor close fields require the seal fields (v3 is a superset of "
                         "v2): a pin that covers the close chain but not the seal is not a "
                         "shape this format has")
    record = {FORMAT_KEY: 1, "target": target, "head": head, "count": count, "ts": ts}
    if has_seal:
        record[FORMAT_KEY] = 2
        record["seal_head"] = seal_head
        record["seal_count"] = seal_count
    if has_close:
        record[FORMAT_KEY] = 3
        record["close_head"] = close_head
        record["close_count"] = close_count
    return _validate(record)


def render_anchor(record):
    """The canonical one-line JSON text of an anchor (sorted keys, trailing newline) — stable
    bytes so an off-box copy can be compared, hashed, or committed verbatim."""
    return json.dumps(_validate(record), sort_keys=True, separators=(",", ":")) + "\n"


def parse_anchor(text):
    """Parse and strictly validate anchor text. Raises ``ValueError`` (never returns a partial
    record) on non-JSON input or any shape violation — see :func:`_validate`."""
    try:
        record = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"anchor is not valid JSON: {exc}") from exc
    return _validate(record)


def compare(record, target, chain_hmacs):
    """Deny-biased comparison of a previously recorded anchor against the live RECEIPT chain
    state. Returns ``(True, None)`` or ``(False, reason)``.

    This is the receipt-chain half only — it never looks at ``seal_head``/``seal_count`` even
    when the record is v2. The seal-side comparison is not done here: it is
    :meth:`~pacioli.store.BrokerStore.seal_state`'s ``expected_seal_head``/``expected_seal_count``
    kwargs, which the caller (``pacioli anchor check``) runs separately against a keyed store —
    ``compare`` has no store to query and no key in reach, so it could not run that check even in
    principle.

    :param record: a parsed anchor (re-validated here — garbage fails closed, never passes).
    :param target: the resolved name of the target being checked; an anchor recorded for any
        other target is refused (a "prod" pin must never vouch for "staging").
    :param chain_hmacs: the ordered list of receipt hmacs of the live chain. Positions and
        values only — the caller MUST separately run a keyed verify of the same chain, or a
        forged/inconsistent suffix would not be seen here.
    """
    try:
        _validate(record)
    except ValueError as exc:
        return (False, str(exc))
    if record["target"] != target:
        return (False, f"anchor was recorded for target {record['target']!r}, "
                       f"not {target!r} — refusing the cross-target check")
    a_count, live_count = record["count"], len(chain_hmacs)
    if live_count < a_count:
        return (False, f"receipt count regressed: anchor pinned {a_count}, live chain has "
                       f"{live_count} — tail truncated or wiped since the pin")
    if a_count == 0:
        return (True, None)  # the pin says "empty then"; the chain can only have grown
    if not hmac.compare_digest(chain_hmacs[a_count - 1], record["head"]):
        if live_count == a_count:
            return (False, "head mismatch at the pinned count — the chain was rewritten "
                           "since the pin")
        return (False, "the anchored head is no longer on the chain at its pinned position — "
                       "history before the pin was rewritten")
    return (True, None)
