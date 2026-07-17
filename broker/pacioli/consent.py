# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — CONSENT: the marker (pure core).

Two hands on every entry: the clerk writes, the merchant grants — the agent must not approve its
own posting. A *marker* is a single-use,
out-of-band, human-minted grant that the agent **cannot derive**: a human mints the raw token and
writes it somewhere the agent's own execution context can't read; the broker only ever sees the
grant record. Submit requires a live marker **bound to this exact plan** and within its TTL.

**Lifecycle — a state machine, not a bool.** ``live → reserved → consumed`` (or ``reserved → live``
if the submit fails). The glue CAS-claims ``live → reserved`` **before** the irreversible submit, so
a second racing call finds the marker already reserved and is refused — this closes the concurrency
window a verify-now/consume-later design leaves open (two concurrent submits both passing the gate).
``verify`` only ever passes a ``live`` marker.

Two properties remain the glue's job (SPEC §2) and this core cannot provide them:
  * **out-of-band minting / storage** — the raw token and the grant must live where the agent shell
    can't reach; if the agent can write the grant, this is cosmetic (Proximo's "honest limit").
  * **atomic claim** — the glue persists the ``live → reserved`` transition via a compare-and-swap so
    only one racing caller wins. This core computes the reserved marker; it does not serialise.

No frappe, no I/O, no clock: ``now`` and ``expires_at`` are epoch-second floats passed in.
"""
from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass, replace

_TRUE = (True, None)

LIVE = "live"
RESERVED = "reserved"
CONSUMED = "consumed"


def hash_token(token):
    """SHA-256 hex of a raw token. The grant stores only this — a leaked marker store never reveals
    a usable token. Returns ``None`` for a blank/non-string token (which can never match).

    The raw token is assumed high-entropy and machine-minted (out of band), so a plain SHA-256 is
    the right primitive — this is not a low-entropy password, so no KDF/salt is needed.
    """
    if not isinstance(token, str) or not token.strip():
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Marker:
    """A minted grant. ``token_hash`` is ``sha256(raw token)``; the raw token is never stored.

    :param token_hash: the hash the presented token is checked against.
    :param plan_id: the plan this marker authorises — and only this one.
    :param expires_at: epoch-second float; the marker is dead at/after this ``now``.
    :param state: ``"live"`` | ``"reserved"`` | ``"consumed"`` — only ``"live"`` ever authorises.
    """

    token_hash: str
    plan_id: str
    expires_at: float
    state: str = LIVE


def new_marker(token, plan_id, expires_at):
    """Pure constructor the glue uses after minting a raw ``token`` out of band and computing
    ``expires_at = now + ttl``. Stores only the token hash; starts ``live``."""
    return Marker(token_hash=hash_token(token), plan_id=plan_id, expires_at=expires_at, state=LIVE)


def verify(marker, token, plan_id, now):
    """Deny-biased check. Returns ``(True, None)`` or ``(False, reason)``.

    Denies — in order — a missing marker, a non-``live`` marker (reserved/consumed never re-authorise),
    a non-finite/non-numeric ``now`` (a NaN clock would make the expiry compare silently false), an
    expired marker (``now`` at/past ``expires_at``), a missing-or-mismatched plan binding (a ``None``
    binding on either side is a denial, never a match), and a token that does not hash to
    ``token_hash`` (constant-time compare). Anything that isn't an affirmative match is a denial.
    """
    if marker is None:
        return (False, "no marker presented")
    if marker.state != LIVE:
        return (False, f"marker not available (state: {marker.state})")
    if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
        return (False, "invalid current time")
    try:
        if now >= marker.expires_at:
            return (False, "marker expired")
    except (TypeError, ValueError):
        return (False, "marker has no valid expiry")
    if not plan_id or not marker.plan_id or plan_id != marker.plan_id:
        return (False, "marker is bound to a different plan")
    presented = hash_token(token)
    if presented is None or marker.token_hash is None:
        return (False, "invalid token")
    if not hmac.compare_digest(presented, marker.token_hash):
        return (False, "token does not match marker")
    return _TRUE


def reserve(marker, token, plan_id, now):
    """Verify then return the marker in the ``reserved`` state. ``(ok, reason, reserved|None)``.

    The glue persists the ``live → reserved`` transition via CAS **before** executing the submit, so
    only one concurrent caller can claim a given marker. A failed verify returns ``None`` — a bad
    presentation never reserves.
    """
    ok, reason = verify(marker, token, plan_id, now)
    if not ok:
        return (False, reason, None)
    return (True, None, replace(marker, state=RESERVED))


def commit(marker):
    """``reserved → consumed`` — call after a successful submit. Single-use is now spent."""
    return replace(marker, state=CONSUMED)


def release(marker):
    """``reserved → live`` — call after a *failed* submit, so the human's grant isn't burned."""
    return replace(marker, state=LIVE)
