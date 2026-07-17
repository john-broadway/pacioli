# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — CLI: the human-run commands (glue, stdlib-only).

The half of CONSENT the agent must never touch. ``pacioli mint`` generates a **high-entropy token
itself** (the human never types one — a typed token is low-entropy and re-derivable), writes only
its hash to the same store the server reads, and prints the raw token **once**. The human hands
that token to the agent through a channel the agent's shell can't read; the agent presents it to
``submit_sales_invoice``.

Minting is a CLI, not an MCP tool, on purpose: if the agent could mint, consent would be
self-granted and the marker is theatre (SPEC §2). The mint path opens the store **keyless** — the
seal key is never in reach of the consent step (least exposure).
"""
from __future__ import annotations

import argparse
import secrets
import sys

from pacioli import __version__
from pacioli.runtime import RuntimeError_, open_store
from pacioli.store import (
    AttestStaleError,
    CloseGateLatchedError,
    CloseRecordStaleError,
    NoAttestationPendingError,
    StoreCorruptError,
)
from pacioli.tools import format_seal_refusal

_TOKEN_BYTES = 24  # 192 bits, url-safe -> ~32 chars


def cmd_mint(env, plan_id, target, ttl):
    """Mint a consent marker for a recorded plan. Returns a process exit code; prints the token."""
    import time

    try:
        store = open_store(env, _target_name(env, target), with_key=False)
    except (RuntimeError_, StoreCorruptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Seal check — DEFENSE-IN-DEPTH, never the authoritative gate (docs/plans/2026-07-14-close-
    # half3-seal-slice.md, Task 2). This store is opened KEYLESS (mint's own least-exposure
    # posture — the seal key never comes near the human's consent step), so `seal_state()` here
    # trusts row CONTENT only: it still catches a genuinely sealed history (zero rows, a
    # confirmed `seal` as the latest action, a rollback gap) but cannot detect an in-place edit to
    # the latest row's content the way a keyed reader can (BrokerStore.seal_state's own
    # docstring). The AUTHORITATIVE gate is the keyed, dispatch-time check
    # (PacioliBroker._seal_gate, tools.py) that refuses every governed write tool while sealed
    # regardless of what this check sees; this one exists purely so a human does not even get as
    # far as printing a marker for a broker that has already confessed CONTAIN. A failure to even
    # read the seal state is deny-biased the same way as the dispatch gate: refuse rather than
    # mint blind.
    try:
        seal = store.seal_state()
    except Exception as exc:  # noqa: BLE001 — deny-biased: an unreadable seal state refuses mint
        print(f"error: could not determine seal state before minting ({exc}); refusing rather "
              "than mint a consent grant while the seal cannot be verified", file=sys.stderr)
        return 1
    if seal["sealed"]:
        print(f"error: {format_seal_refusal(seal)}", file=sys.stderr)
        return 1
    plan = store.get_plan(plan_id)
    if plan is None:
        print(f"error: no recorded plan {plan_id!r} — the agent must call plan_submit first, "
              "and you mint against the plan_id it returns", file=sys.stderr)
        return 1
    if not 1 <= ttl <= 86_400:
        print(f"error: --ttl {ttl} out of range; use 1..86400 seconds (a consent grant is "
              "meant to be short-lived)", file=sys.stderr)
        return 1
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    try:
        store.mint_marker(token, plan_id, expires_at=time.time() + ttl)
    except Exception as exc:  # noqa: BLE001 — a UNIQUE clash = a live marker already exists
        print(f"error: could not mint (a live marker for this plan may already exist): {exc}",
              file=sys.stderr)
        return 1
    print(f"plan:   {plan_id}  (document {plan.docname} on target {plan.target})")
    print(f"ttl:    {ttl}s")
    print(f"marker: {token}")
    print("hand this token to the agent out of band; it is shown once and stored only as a hash.")
    return 0


def cmd_verify(env, target, expected_head):
    """Verify a target's receipt chain from the operator's side."""
    try:
        store = open_store(env, _target_name(env, target), with_key=True)
    except (RuntimeError_, StoreCorruptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ok, reason = store.verify(expected_head=expected_head)
    try:
        head = store.head()
        count = len(store.receipts())
    except StoreCorruptError as exc:
        # verify() already reported the corruption in `reason`; the head/count reads can't run on a
        # damaged chain. Report the failure cleanly rather than crash with a raw json error.
        print(f"FAILED: {reason or exc}", file=sys.stderr)
        return 1
    if ok:
        print(f"ok: chain verifies ({count} receipts). head: {head}")
        return 0
    print(f"FAILED: {reason} ({count} receipts). head: {head}", file=sys.stderr)
    return 1


def cmd_close(env, target, since, until, expected_head, as_json, reconcile=False, respond=False,
              transport=None, envelope=None, advance=False):
    """The Close — produce the period Statement (docs/plans/2026-07-09-the-close.md), and,
    with ``--reconcile``, ALSO join it to Half 2 (the Reconciliation, ``pacioli.reconciliation``).

    A read-only period attestation from Pacioli's OWN ledger: every governed act, balanced
    (committed) or confessed (orphan), on a chain that verifies. Opens WITH key (the statement must
    say whether the chain it reports even holds). Exit 0 only when the period closes clean; non-zero
    when the chain fails to verify, orphans remain, or the live head disagrees with an
    ``--expected-head`` off-box pin — the same it's-not-done-if-it-doesn't-balance discipline as
    ``verify``/``orphans``. v1 is stateless: it attests a window, it does not lock or advance a
    close cursor. That statefulness is now built (Half 3, Fork A1) but stays strictly opt-in — see
    ``--advance`` below; without it, ``close``/``close --respond`` are exactly this: stateless,
    byte-identical to v1. Plain ``close`` (``reconcile=False``, the
    default) is UNCHANGED — offline, store-only, no bench call — behavior above this point is
    identical to before Half 2 existed.

    ``--reconcile`` additionally resolves the target's registry entry, builds a live
    :class:`~pacioli.erpnext.ErpnextClient` (mirroring :func:`pacioli.runtime.assemble`:
    :func:`~pacioli.registry.resolve_auth` + ``ErpnextClient``), sweeps the GL Entry
    creation-window movement and reads the accounts-settings posture (BOTH required — an
    unreadable audit source refuses the whole reconciliation, deny-biased), and separately reads
    Repost Accounting Ledger (non-fatal — an unreadable repost source degrades to a flag, the
    reconciliation still runs without second-generation attribution). Refuses up front, before any
    bench call, when the target carries no ``company`` pin (the GL sweep is company-scoped; an
    unpinned reconciliation would silently sweep cross-company) or when either ``--since``/
    ``--until`` is missing (a period reconciliation needs a bounded window — an unbounded
    creation-window sweep of the whole bench is not a close).

    **Owner corroboration (Fork III):** ``seat_owner`` is passed from the target's optional
    ``seat_user`` registry field — the ERPNext username this credential authenticates as. When set,
    a governed voucher's GL rows must ALSO carry that ``owner`` or the voucher downgrades to
    second-generation (a repost that rewrote a governed voucher's rows under another user's name is
    then surfaced, not passed by the name+time match alone). When ``seat_user`` is unset the
    corroboration is off — name+time match only — so the field is purely tightening and opt-in
    (the seat's username is not carried in any Pacioli receipt, so the operator supplies it once in
    the registry).

    Exit code with ``--reconcile``: 0 iff the Statement is balanced AND the reconciliation is
    complete. The mere PRESENCE of ungoverned movement never flips it — accounting-not-police,
    ungoverned is presented, not judged. Non-zero on: an unbalanced statement, an incomplete
    reconciliation (e.g. an unreadable posture flag), an unreadable GL/Accounts-Settings audit
    source, a missing company pin, or a missing --since/--until bound.

    **``--respond`` (Half 3, response-to-gap):** applies the operator's posture and response
    envelope (:func:`pacioli.response.build_response`) to the findings and emits the reaction —
    record / alert / attestation-gate / CONTAIN. Over a Statement alone (no ``--reconcile``) it
    responds to the statement-side findings (orphans, unconfirmed acts) at the default ``mixed_door``
    posture; adding ``--reconcile`` also weighs the reconciliation-side findings (ungoverned,
    second-generation, blind read). This slice uses the DEFAULT posture/envelope — a per-target
    registry posture field is a staged refinement (Fork C). Exit is non-zero when the aggregate
    response rises above ``record`` (a finding the operator asked to be told about — which includes
    a second-generation voucher the balanced/complete checks alone would miss).

    **``--envelope CLASS=LEVEL`` (Half 3, CONTAIN's teeth):** repeatable, only meaningful with
    ``--respond`` (present without it is a usage error, exit 2 — parsed at THIS boundary, before
    any store I/O, deny-biased: a malformed entry refuses the whole close rather than silently
    running with a weaker envelope). CLASS is one of the six finding classes, LEVEL one of the
    four response levels (:mod:`pacioli.response`'s own floor semantics decide the actual
    escalation — this boundary only validates the strings, it never re-implements the floor).
    When the resulting response's ``seal_required`` is True, this command seals the already-open
    keyed store itself (``source="response"``). Two ways to get there: an operator-escalated
    CONTAIN (an explicit ``--envelope ...=contain`` — opt-in only, nothing in the six escalatable
    classes reaches it any other way), or — since broker 0.22.0, narrowed 2026-07-15 — a
    ``chain_broken`` finding: the statement's own receipt chain FAILED TO VERIFY
    (:func:`pacioli.response.build_response`'s sole default-CONTAIN class, floor ``contain``
    unconditionally, not one of the six ``--envelope``-escalatable classes and not silenceable by
    one either). This is never an "escalation" — the seal reason names it truthfully as reached, not
    escalated (see :func:`_seal_reason`). It does NOT fire on an off-box anchor mismatch alone
    (dropped 2026-07-15: that naive head==anchor equality false-sealed a legitimately-grown chain;
    receipt-rollback detection is count-aware and lives in `pacioli anchor check`/`seal-status
    --anchor` instead). A seal-WRITE failure is reported loudly on stderr (manual ``pacioli seal``
    required) but is never silently treated as "handled" — the exit code stays non-zero either way
    via the existing response-above-record rule. ``seal_required`` False (a plain or alert-level
    ``--respond`` with a clean chain) makes zero writes to ``seal_events`` on this path.

    **``--advance`` (Half 3, Fork A1 — the close-record and the attestation gate):** everything
    ``--respond`` does, PLUS: after the full render, writes the close record itself
    (:meth:`~pacioli.store.BrokerStore.record_close`), advancing the period cursor. Requires
    ``--respond`` (a usage error, exit 2, without it — you cannot advance past a window you didn't
    examine). When ``--since`` is omitted, it defaults from the verified close-record cursor
    (:meth:`~pacioli.store.BrokerStore.close_gate_state`'s own ``cursor``/``last_close_seq`` pair,
    read exactly ONCE) rather than genesis — "the next close covers only new activity" — UNLESS no
    close has ever been recorded, in which case genesis (today's default) stands; an explicit
    ``--since`` from the caller is never overridden. Refuses the write (exit 1; the read above never
    refuses — reads are never gated) when the PRE-EXISTING gate is already LATCHED: either a prior
    close closed over a gap (``gate_required``) with no later attestation — the render names the
    stuck period/seq and the ``pacioli attest`` command that clears it — or the close-record history
    itself fails integrity verification (a ``seq`` gap, an unverifiable HMAC) — the render names the
    failure and refuses to guess a repair. When the gate is open, the write always records THIS
    close's own ``gate_required`` as ``gapped`` — a close that itself closes over a gap still gets
    recorded (that is what latches the NEXT advance: "the close that finds the gap records itself").
    A same-run auto-seal (above) and this write are independent mechanisms — one stops the pen, the
    other stops the page-turn; a CONTAIN reached this run does not stop the close record from
    landing. Plain ``close``/``close --respond`` (no ``--advance``) make ZERO writes to
    ``close_records`` and render no cursor/gate lines — byte-identical to before this flag existed.
    Operator footgun (bench lab, 2026-07-16): ``--advance`` with an ``--envelope`` but WITHOUT
    ``--reconcile`` records a CLEAN close — the envelope has no reconciliation-side finding to
    escalate, so a posture gap that only ``--reconcile`` can see never latches the gate. Correct
    by construction, but if the envelope names a reconciliation-side class (e.g.
    ``adverse_posture``), pair it with ``--reconcile`` or it is a no-op.

    **The clock domain (T1 ruling, docs/plans/2026-07-16-clock-domain-ruling.md):** with the
    target's optional registry ``site_tz`` declared, ``--since``/``--until`` mean SITE time — the
    books' own calendar — converted once at this boundary to the store's UTC domain (statement
    filter, future-``--until`` compare, cursor); the GL sweep keeps the operator's original
    site-domain strings (its ``creation`` axis IS the site clock). A declared-but-unresolvable
    zone refuses a bounded close (a skipped conversion is the very defect this fixes); absent
    ``site_tz``, bounds pass verbatim to both clock domains (pre-0.24.0 filtering, unchanged)
    and ``--reconcile`` ADDS an honest clock note saying so — the one output addition on the
    undeclared path.

    ``transport`` is a pure testing seam (forwarded to ``ErpnextClient``, defaulting to
    :func:`pacioli.erpnext.default_transport` when ``None``) — real CLI dispatch never sets it."""
    from pacioli.close import build_statement, render_statement
    from pacioli.prove import GENESIS

    # --envelope is a STRICTER boundary than response.py's floor semantics (which it feeds
    # unmodified) — a malformed entry refuses the WHOLE close, before any I/O, rather than
    # silently degrading to a weaker envelope. Validate first; nothing below this point runs on
    # a bad entry.
    envelope_dict, envelope_err = _parse_envelope(envelope, respond)
    if envelope_err:
        print(f"error: {envelope_err}", file=sys.stderr)
        return 2
    # --advance is a STRICTER boundary too, same reasoning as --envelope above: without --respond
    # there is no response to advance past, so this refuses the whole close before any I/O rather
    # than silently running a stateless close with a flag that would do nothing.
    if advance and not respond:
        print("error: --advance only makes sense with --respond (it writes the close record for "
              "the window --respond just examined) — you cannot advance past a window you didn't "
              "examine; add --respond or drop --advance", file=sys.stderr)
        return 2
    # --- The clock domain (T1 ruling, docs/plans/2026-07-16-clock-domain-ruling.md) ---------
    # With `site_tz` declared on the target, caller window bounds mean SITE time — the books'
    # own calendar — converted ONCE here to the store's UTC domain (the canonical internal
    # domain: statement filter, future-until compare, cursor). The ORIGINAL site-domain strings
    # are kept for the GL sweep (whose `creation` axis IS the site clock); a bound the operator
    # did NOT supply (an --advance-materialized until, a cursor-defaulted since) has no
    # original, and the sweep derives its site-domain form from the store-domain canonical
    # (store_to_site — review finding 0). Declared-but-unresolvable zone REFUSES a bounded
    # close (a skipped conversion is the original defect); an unbounded close has nothing to
    # convert and is never bricked by a zone typo. Absent site_tz → bounds pass verbatim
    # (pre-0.24.0 behavior, both domains read the same string) — `close --reconcile` then
    # carries an honest clock note saying so (the one deliberate output addition).
    site_tz, registry_readable = _resolve_site_tz(env, target)
    site_window = None  # (since, until) as the operator typed them, site domain
    if site_tz is not None and (since is not None or until is not None):
        from pacioli.clock import ClockDomainError, site_to_store
        try:
            since_utc = site_to_store(since, site_tz) if since is not None else None
            until_utc = site_to_store(until, site_tz, end_of_day=True) if until is not None else None
        except ClockDomainError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        site_window = (since, until)
        since, until = since_utc, until_utc
    elif not registry_readable and (since is not None or until is not None):
        # An unreadable registry can hide a DECLARED site_tz — say so instead of silently
        # reading the window in the store domain. Non-fatal: a store-only operator with no
        # registry at all keeps today's behavior. (A readable registry whose TARGET fails to
        # resolve is NOT this case — _resolve_site_tz keeps the two apart, review finding 3.)
        print("warning: registry unreadable — window bounds read in the store clock domain "
              "(a declared site_tz, if any, was not applied)", file=sys.stderr)

    # Reversed window, caller's own bounds (redteam finding 2, case a): both supplied and
    # since > until is pure caller input — a usage error refused before any I/O, same boundary
    # discipline as --envelope/--advance above. Runs AFTER the clock conversion so both bounds
    # share one shape (review finding 1: on raw strings, 'T' sorts above ' ' and a valid
    # mixed-separator window read as reversed); the message shows the operator's own strings.
    # (A CURSOR-defaulted since ahead of --until is a different, state-dependent case —
    # refused with a render below, exit 1.)
    if advance and since is not None and until is not None and since > until:
        shown_since, shown_until = site_window if site_window is not None else (since, until)
        print(f"error: --since {shown_since} is after --until {shown_until} — a reversed window "
              "closes nothing (it excludes every act and would record a trivially 'balanced' "
              "period); swap or fix the bounds", file=sys.stderr)
        return 2

    name = _target_name(env, target)
    try:
        store = open_store(env, name, with_key=True)
    except (RuntimeError_, StoreCorruptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    gate_state = None
    if advance:
        # ONE verified read gives latch + cursor + existence together (Task 1's own contract,
        # store.py's close_gate_state docstring): `last_close_seq is None` means no close row
        # exists yet (honest genesis — --since stays whatever the caller passed, None by default);
        # a seq number means a close exists and `cursor` is its verified `period_until` (`None`
        # there = that close was legitimately open-ended, so --since correctly defaults to genesis
        # too — not an integrity gap, just an open window). Never `close_cursor()` alone here — it
        # raises on an integrity failure, and this path must still be able to RENDER (reads are
        # never gated) even while refusing the WRITE.
        gate_state = store.close_gate_state()
        since_from_cursor = False
        if since is None and gate_state["last_close_seq"] is not None:
            since = gate_state["cursor"]
            since_from_cursor = since is not None

        # THE STORE's clock, not a second datetime.now() of our own (redteam finding 1: the
        # future-until comparison and the materialized until must both use the exact source
        # record_close stamps rows with).
        now_ts = store.clock_now()
        advance_refusal = None  # (cause, reason) — decided here, rendered/written below
        if until is not None and until > now_ts:
            # Redteam finding 1 (CRITICAL): a future --until would poison the cursor — every
            # later default-since advance would silently see zero acts, forever. Advance only;
            # plain close still accepts any window (it writes nothing).
            advance_refusal = (
                "future_until",
                f"--until {until} is in the future (store clock {now_ts}) — an advance may not "
                "close a period that has not happened yet; a future cursor would make every "
                "later default-since advance silently see zero acts",
            )
        elif until is None:
            # Redteam finding 3: materialize the effective until as the store's now — the cursor
            # is always concrete, so consecutive no-until advances close contiguous periods
            # instead of overlapping genesis..now windows. The statement below uses the SAME
            # materialized bound, so the examined window and the recorded period are identical.
            until = now_ts
        if advance_refusal is None and since is not None and since > until:
            # Redteam finding 2 (case b): the cursor-defaulted (or materialized-window) since is
            # ahead of the effective until — the requested period is already closed. This also
            # un-poisons a legacy future cursor (written before finding 1's refusal existed):
            # instead of the silent zero-act close the poison used to buy, this refuses loudly.
            if since_from_cursor:
                detail = (f"the close cursor is already at {since}, past the requested "
                          f"--until {until} — that window is already closed; nothing to advance")
                if since > now_ts:
                    detail += (" (note: the recorded cursor is in the FUTURE vs the store clock "
                               f"{now_ts} — a previously recorded future --until poisoned it; "
                               "inspect `pacioli close-status`)")
                advance_refusal = ("cursor_past_until", detail)
            else:
                advance_refusal = (
                    "reversed_window",
                    f"--since {since} is after the effective until {until} — a reversed window "
                    "closes nothing",
                )

    # ONE snapshot backs both the verify and the statement built from it — same reason the anchor
    # reads once: a concurrent host-level writer must not change the chain between verify and report.
    ok, reason, receipts = store.verify_snapshot()
    head = receipts[-1].hmac if receipts else GENESIS
    st = build_statement(receipts, target=name, since=since, until=until,
                         head=head, verified=(ok, reason), anchor=expected_head)

    reconciliation = None
    if reconcile:
        outcome = _build_reconciliation_for_close(env, target, name, receipts, since, until,
                                                   site_tz=site_tz, site_window=site_window,
                                                   transport=transport)
        if isinstance(outcome, str):  # a refusal message — never built, never rendered
            print(f"error: {outcome}", file=sys.stderr)
            return 2
        reconciliation = outcome

    response = None
    seal_block = None
    if respond:
        from pacioli.response import build_response
        # reconciliation is None without --reconcile — build_response handles that (statement-side
        # findings only). Posture comes from the target's registry field (Fork C); envelope_dict is
        # {} (all floors) unless --envelope escalated a class — build_response's own floor
        # semantics decide the actual response, this call never re-implements them.
        response = build_response(st, reconciliation, target=name,
                                  posture=_resolve_posture(env, target), envelope=envelope_dict)

        # Auto-seal — CONTAIN's teeth. Only fires when the response ITSELF says seal_required
        # (top finding reached contain) — either an explicit operator --envelope ...=contain, or
        # (since broker 0.22.0) a `chain_broken` finding, response.py's sole default-contain class
        # (Pacioli's own ledger unverifiable — not a party verdict, so it needs no operator
        # opt-in; see response.py's "Invariant 1, REVISED"). Reuses the store already opened WITH
        # KEY above (line ~164) — no second open. A plain/alert close on a clean chain makes ZERO
        # writes to seal_events on this path.
        if response["seal_required"]:
            seal_reason_text = _seal_reason(response, since, until)
            try:
                state = store.seal(seal_reason_text, source="response")
            except Exception as exc:  # noqa: BLE001 — fail-closed: a seal WRITE failure must be
                # LOUD and must never be folded into a success shape — CONTAIN was DECIDED but the
                # broker is NOT confirmed sealed until this append lands. The exit code still goes
                # non-zero via the ordinary response-above-record rule below; this stderr line is
                # what tells the operator the seal itself did not take and needs a manual hand.
                print(f"error: CONTAIN was reached but the seal WRITE FAILED ({exc}) — this "
                      "broker is NOT confirmed sealed; run `pacioli seal --reason <why>` "
                      "manually right now", file=sys.stderr)
            else:
                seal_block = {"sealed": True, "seq": state["seq"], "action": "sealed by response"}

    # --advance's own write — independent of auto-seal above (seal stops the pen, this stops the
    # page-turn). `gate_state` is the ONE read from before the statement was even built; a
    # PRE-EXISTING latch (a prior gapped close awaiting attestation, or a close-record integrity
    # failure) refuses the write outright, with no second store hit needed to know why. When it is
    # open, `record_close` re-derives the gate itself, fresh, inside its own `BEGIN IMMEDIATE` (no
    # TOCTOU) — the `except` below is a defensive net for the vanishingly rare race where a
    # concurrent writer latches the gate between our early read and this call, never a path this
    # process's own writes can trigger.
    advance_result = None
    if advance:
        # Pre-existing seal, confessed (redteam finding 7, render-only): the page may turn while
        # the pen is confiscated — but never silently. One read; no mechanism coupling.
        broker_sealed = bool(store.seal_state()["sealed"])
        if gate_state["latched"]:
            advance_result = {"written": False, "cause": gate_state["cause"],
                              "reason": gate_state["reason"], "period_since": since,
                              "period_until": until, "cursor": gate_state["cursor"],
                              "gapped": None, "seq": None, "broker_sealed": broker_sealed}
        elif advance_refusal is not None:
            cause, refusal_reason = advance_refusal
            advance_result = {"written": False, "cause": cause, "reason": refusal_reason,
                              "period_since": since, "period_until": until,
                              "cursor": gate_state["cursor"], "gapped": None, "seq": None,
                              "broker_sealed": broker_sealed}
        else:
            gapped_flag = bool(response["gate_required"])
            try:
                # Compare-and-append: `gate_state` is the ONE read this run planned its period
                # bounds against (the --since default above came from it); its last_close_seq is
                # what the store must still see at write time, inside record_close's own
                # BEGIN IMMEDIATE. A concurrent advance landing between our read and this write
                # raises CloseRecordStaleError below -- the period math is stale, not the store.
                new_state = store.record_close(period_since=since, period_until=until,
                                               attested_head=head, gapped=gapped_flag, reason="",
                                               expected_last_close_seq=gate_state["last_close_seq"])
            except CloseGateLatchedError:
                fresh = store.close_gate_state()
                advance_result = {"written": False, "cause": fresh["cause"],
                                  "reason": fresh["reason"], "period_since": since,
                                  "period_until": until, "cursor": fresh["cursor"],
                                  "gapped": None, "seq": None, "broker_sealed": broker_sealed}
            except CloseRecordStaleError as exc:
                advance_result = {"written": False, "cause": "stale_cursor",
                                  "reason": str(exc), "period_since": since,
                                  "period_until": until, "cursor": gate_state["cursor"],
                                  "gapped": None, "seq": None, "broker_sealed": broker_sealed}
            else:
                # The gate verdict comes from record_close's RETURNED fresh derivation, never
                # re-asserted from the input gapped flag (redteam finding 6, honesty F2).
                advance_result = {"written": True, "cause": None, "reason": "",
                                  "period_since": since, "period_until": until,
                                  "cursor": new_state["cursor"], "gapped": gapped_flag,
                                  "seq": new_state["last_close_seq"],
                                  "gate_latched": new_state["latched"],
                                  "gate_reason": new_state["reason"],
                                  "broker_sealed": broker_sealed}

    # The clock disclosure (T1 ruling + bench lab lesson 1): with a declared site_tz and a
    # bounded window, name the zone and both faces of the window; on --reconcile WITHOUT one,
    # say the window crossed two clock domains unconverted. None otherwise — an unbounded or
    # store-only close renders byte-identically to before.
    clock_block = None
    if site_window is not None:
        clock_block = {"site_tz": site_tz,
                       "window_site": {"since": site_window[0], "until": site_window[1]},
                       "window_store": {"since": since, "until": until}}
    elif reconcile and site_tz is None:
        clock_block = {"site_tz": None,
                       "note": "window applied in two clock domains (store-UTC receipt stamps "
                               "vs site wall-clock GL creation) — declare site_tz on the "
                               "target to align them"}

    if as_json:
        import json
        if reconcile or respond:
            doc = {"statement": st}
            if clock_block:
                doc["clock"] = clock_block
            if reconcile:
                doc["reconciliation"] = reconciliation
            if respond:
                doc["response"] = response
                if seal_block:
                    doc["seal"] = seal_block
                if advance:
                    doc["advance"] = advance_result
        else:
            # The clock disclosure holds on the PLAIN machine-readable surface too (review
            # finding 2: scripts must never see silently-shifted period bounds) — an additive
            # top-level key beside the statement's own, present only when there is a
            # conversion to disclose; the undeclared unbounded shape is byte-identical.
            doc = dict(st, clock=clock_block) if clock_block else st
        print(json.dumps(doc, indent=1, default=str))
    else:
        sys.stdout.write(render_statement(st))
        if clock_block:
            if clock_block["site_tz"] is not None:
                sys.stdout.write(
                    f"  clock:       window declared in site time ({clock_block['site_tz']}) — "
                    f"receipts filtered at the store-clock equivalents above\n")
            else:
                sys.stdout.write(f"  clock:       {clock_block['note']}\n")
        if reconcile:
            from pacioli.reconciliation import render_reconciliation
            sys.stdout.write(render_reconciliation(reconciliation))
        if respond:
            from pacioli.response import render_response
            sys.stdout.write(render_response(response))
            if seal_block:
                sys.stdout.write(f"  seal:        SEALED (seq {seal_block['seq']}) — sealed by "
                                 "response\n")
                sys.stdout.write("               to open it back up: `pacioli unseal --reason "
                                 "<why>` (see `pacioli seal-status` for the seal history)\n")
            if advance:
                sys.stdout.write(_render_advance(advance_result, name))

    if not st["balanced"]:
        return 1
    if reconcile and not reconciliation["complete"]:
        return 1
    if respond and response["response"] != "record":  # a finding rises above record
        return 1
    if advance and not advance_result["written"]:  # the pre-existing gate refused the write
        return 1
    return 0


def _render_advance(result, target_name):
    """Render the ``--advance`` cursor/gate lines appended to a close's rendered output — printed
    ONLY when ``--advance`` was passed (constraint 6: plain ``close``/``close --respond`` stay
    byte-identical without it). Two shapes:

      * **Refused** — no write happened THIS run. ``gapped_awaiting_attestation`` names the stuck
        period/seq and the ``pacioli attest`` command that clears it; ``stale_cursor`` means the
        cursor at write time did not match the one this run planned against — USUALLY a
        concurrent advance, but the check cannot distinguish that from history alteration (e.g.
        a deleted tail row), and the render says exactly that (redteam finding 5: truth over
        implication); ``future_until`` refuses to close a period that has not happened yet
        (finding 1 — a future cursor poisons every later default-since advance);
        ``cursor_past_until``/``reversed_window`` refuse a window that is already closed or
        excludes everything (finding 2); any other cause (``gap``/``unverifiable``/``keyless``)
        is an integrity failure attest cannot repair — the render says so and refuses to guess a
        fix.
      * **Written** — the close row landed. The gate line renders from ``record_close``'s
        RETURNED fresh state (``gate_latched``/``gate_reason``), never re-asserted from the input
        gapped flag (redteam finding 6).

    Either shape appends a one-line confession when the broker is SEALED (``broker_sealed``,
    redteam finding 7): the cursor advancing does not clear containment, and the render never
    lets the page-turn imply the pen is back.
    """
    sealed_note = ""
    if result.get("broker_sealed"):
        sealed_note = ("  note:        broker is SEALED — advancing the cursor does not clear "
                       "containment (`pacioli seal-status` for the history, `pacioli unseal "
                       "--reason <why>` is the human hand that lifts it)\n")
    if not result["written"]:
        if result["cause"] == "gapped_awaiting_attestation":
            period_end = result["cursor"] if result["cursor"] is not None else "now (open-ended)"
            return (f"  advance:     REFUSED — {result['reason']} (stuck period ended "
                    f"{period_end})\n"
                    f"  fix:         pacioli attest --target {target_name} --reason <why> — that "
                    "clears the gap before the cursor can advance\n" + sealed_note)
        if result["cause"] == "stale_cursor":
            return (f"  advance:     REFUSED — {result['reason']}\n"
                    "  fix:         re-run this exact command — the period bounds must be "
                    "recomputed against the current cursor. Usually a concurrent advance landed "
                    "first, but this check cannot distinguish that from history alteration "
                    "(e.g. a deleted tail row); if the mismatch recurs with no concurrent "
                    "operator, inspect `pacioli close-status` before retrying\n" + sealed_note)
        if result["cause"] == "future_until":
            return (f"  advance:     REFUSED — {result['reason']}\n"
                    "  fix:         drop --until (the advance will close the period at the "
                    "store's own now) or pass a bound that has already passed\n" + sealed_note)
        if result["cause"] in ("cursor_past_until", "reversed_window"):
            return (f"  advance:     REFUSED — {result['reason']}\n"
                    "  fix:         nothing was written; pick an --until after the current "
                    "cursor (see `pacioli close-status`)\n" + sealed_note)
        return (f"  advance:     REFUSED — close-record history integrity failure "
                f"({result['cause']}): {result['reason']}\n"
                "  fix:         investigate before advancing any period — this is not something "
                "`pacioli attest` can repair\n" + sealed_note)
    period = f"{result['period_since'] or 'genesis'}..{result['period_until'] or 'now'}"
    lines = [f"  advance:     cursor recorded for period {period} (seq {result['seq']})"]
    if result["gate_latched"]:
        lines.append(
            f"  gate:        LATCHED — {result['gate_reason']}; the NEXT advance is refused "
            f"until `pacioli attest --target {target_name} --reason <why>`"
        )
    else:
        lines.append("  gate:        OPEN — the next advance is not blocked")
    return "\n".join(lines) + "\n" + sealed_note


# The six finding classes and four response levels, verbatim (docs/plans/2026-07-14-close-half3-
# seal-slice.md, Task 4). Independent from response.py's own private ``_LEVELS``/``_FLOOR`` —
# this is a DIFFERENT, stricter check (string membership at the CLI boundary), not a
# reimplementation of the floor-escalation math response.py already owns.
_FINDING_CLASSES = frozenset({
    "orphan", "unconfirmed", "second_generation", "blind_read", "adverse_posture", "ungoverned",
})
_RESPONSE_LEVELS = frozenset({"record", "alert", "attestation_gate", "contain"})


def _parse_envelope(entries, respond):
    """Parse repeated ``--envelope CLASS=LEVEL`` strings (argparse's raw ``action="append"``
    list, ``None`` if the flag was never given) into the ``{class: level}`` dict
    :func:`~pacioli.response.build_response` expects. Deny-biased: an operator who asked for an
    escalation must never silently get a weaker one, so ANY malformed entry refuses the dict
    entirely rather than dropping just that entry.

    Returns ``(dict, None)`` on success — ``{}`` when ``entries`` is empty/``None`` (no
    envelope; every class stays at its floor) — or ``(None, error_message)`` on a bad entry or
    on ``entries`` being present without ``respond`` (an envelope only means something paired
    with ``--respond``; presenting it alone is very likely a forgotten flag, not an intentional
    no-op, so it refuses rather than silently doing nothing).

    A CLASS repeated across two ``--envelope`` entries is ALSO refused, malformed-entry-style —
    even when both entries agree. Letting the last one silently win (``orphan=contain`` then
    ``orphan=alert`` quietly landing as ``alert``) is a silent weakening of exactly the kind this
    boundary exists to refuse; an ambiguous envelope is not an envelope, so it refuses the whole
    command rather than picking a reading for the operator.
    """
    if not entries:
        return {}, None
    if not respond:
        return None, ("--envelope only makes sense with --respond (it escalates a "
                       "response-to-gap finding; without --respond nothing computes one) — add "
                       "--respond or drop --envelope")
    result = {}
    for entry in entries:
        if "=" not in entry:
            return None, (f"--envelope entry {entry!r} is not CLASS=LEVEL "
                           "(expected e.g. orphan=contain)")
        cls, _, level = entry.partition("=")
        if cls not in _FINDING_CLASSES:
            return None, (f"--envelope entry {entry!r}: unknown class {cls!r} — must be one of "
                           f"{sorted(_FINDING_CLASSES)}")
        if level not in _RESPONSE_LEVELS:
            return None, (f"--envelope entry {entry!r}: unknown level {level!r} — must be one "
                           f"of {sorted(_RESPONSE_LEVELS)}")
        if cls in result:
            return None, (f"--envelope class {cls!r} given more than once — an ambiguous "
                           "envelope is not an envelope; state each class once")
        result[cls] = level
    return result, None


def _seal_reason(response, since, until):
    """Build the ``store.seal`` reason for a CONTAIN reached via ``close --respond``: names every
    finding class that reached contain, plus the period this close attested — a confession naming
    neither what tripped it nor when is not a legible entry.

    **C1 (redteam fix wave, 2026-07-15): never says "escalated".** ``contain`` is reached one of
    two ways — an operator's explicit ``--envelope CLASS=contain`` (a genuine escalation), or
    ``chain_broken`` firing at its unconditional default floor (never an escalation; it is not one
    of the six ``--envelope``-eligible classes at all). The old wording ("escalated {names} to
    CONTAIN") was a lie on the second path — a chain_broken-only seal never involved an operator
    escalating anything, so writing "escalated" into the permanent ``seal_events`` reason
    misrepresented how CONTAIN was reached. This phrasing is neutral and true on both paths."""
    classes = []
    for f in response["findings"]:
        if f["response"] == "contain" and f["class"] not in classes:
            classes.append(f["class"])
    names = ", ".join(classes) if classes else "an unnamed finding"
    period = f"{since or 'genesis'}..{until or 'now'}"
    return f"close --respond reached CONTAIN via {names} for the period {period}."


def _registry_target(env, target):
    """Best-effort registry-target resolution for cmd_close's optional per-target fields
    (posture, site_tz). Returns ``(target_or_None, registry_readable)`` and never raises —
    the two failure kinds are kept apart (review finding 3): the registry file itself
    unreadable/unparseable → ``(None, False)``; a readable registry whose TARGET cannot be
    resolved (typo'd name, ambiguous default) → ``(None, True)`` — that is a target problem,
    not a readability problem, and callers must not report it as one."""
    from pacioli.registry import RegistryError
    from pacioli.runtime import _load_registry_from_env
    try:
        reg = _load_registry_from_env(env)
    except (RuntimeError_, RegistryError):
        return None, False
    try:
        return reg.get(target), True
    except RegistryError:
        return None, True


def _resolve_posture(env, target):
    """Read the target's optional registry ``posture`` for ``close --respond`` (Fork C). ANY
    resolution failure is deny-biased to ``sole_door`` — an unreadable posture must never
    silently fall to the quieter ``mixed_door`` and hide ungoverned movement. An absent field
    stays ``None`` → ``build_response``'s documented ``mixed_door`` default; a typo string is
    carried through and ``build_response`` deny-biases it (single validator)."""
    t, _readable = _registry_target(env, target)
    return t.posture if t is not None else "sole_door"


def _resolve_site_tz(env, target):
    """Read the target's optional registry ``site_tz`` (the clock-domain ruling, T1). Returns
    ``(site_tz_or_None, registry_readable)`` — the second element lets the caller WARN only
    when the registry FILE may be hiding a declared zone (a silently-unconverted window is the
    original defect), never for a mere target-resolution miss. Zone VALIDITY is not checked
    here — ``pacioli.clock`` is the single zone validator, refusing at use with the zone named
    (mirrors the posture split: type errors at load, typos at use)."""
    t, readable = _registry_target(env, target)
    return (t.site_tz if t is not None else None), readable


def _build_reconciliation_for_close(env, target, name, receipts, since, until, *,
                                    site_tz=None, site_window=None, transport=None):
    """The Half-2 glue for ``close --reconcile``: resolve the target, sweep the live bench, and
    build the Reconciliation. Returns the reconciliation dict on success, or a plain error STRING
    the caller reports and refuses on — this function never raises; :func:`cmd_close` is its only
    caller and always branches on the return type.

    ``site_window`` is the operator's ORIGINAL (site-domain) since/until pair when the target
    declares a ``site_tz`` — used for the sweep bounds in place of the converted store-UTC
    ``since``/``until`` (the GL ``creation`` axis is the site clock; T1 ruling). ``None`` means
    no conversion happened and the canonical bounds are the caller's own strings.

    Deny-biased refusal order: company pin, then bounded window (--since/--until), BEFORE any
    bench call is attempted — cheap, local checks first. Only then does it resolve credentials and
    call the bench. ``sweep_gl_entries``/``get_accounts_settings`` failing is FATAL (the audit
    source itself is unreadable); ``get_reposts`` failing is NON-FATAL (corroboration only — the
    reconciliation still builds, with a flag appended)."""
    from pacioli.erpnext import ErpnextClient, ErpnextError, default_transport
    from pacioli.reconciliation import build_reconciliation
    from pacioli.registry import RegistryError, resolve_auth
    from pacioli.runtime import _load_registry_from_env, _read_file

    try:
        t = _load_registry_from_env(env).get(target)
    except (RuntimeError_, RegistryError) as exc:
        return f"cannot resolve target for reconciliation: {exc}"

    if not t.company:
        return (f"--reconcile requires a company-pinned target (target {t.name!r} has no "
                "`company` set in the registry) — the GL sweep is company-scoped; reconciling "
                "without a company pin would silently sweep cross-company movement")
    if not since or not until:
        return ("--reconcile requires both --since and --until (a bounded period) — an "
                "unbounded creation-window sweep of the entire bench is not a close")

    # The GL `creation` axis is the SITE clock: with a declared site_tz the operator's ORIGINAL
    # site-domain strings (site_window) go to the bench — never the converted store-UTC bounds
    # (T1 ruling). A bound the operator did NOT supply has no original — an --advance can
    # materialize `until` as the store's now and cursor-default `since` (both store-domain
    # stamps) AFTER the boundary conversion, so the both-bounds check above sees them as
    # present while site_window holds None (review finding 0: this crashed as
    # `_to_frappe_clock(None)`). Those derive their site-domain form from the canonical via
    # store_to_site — the one honest back-conversion (store stamps are store-shaped by
    # construction; a failure here refuses the reconciliation, deny-biased, never a raw
    # traceback). Without site_tz the canonical bounds ARE the caller's/materialized strings —
    # pre-0.24.0 behavior, byte-identical.
    from pacioli.clock import ClockDomainError, store_to_site

    def _sweep_bound(original, canonical):
        if original is not None:
            return original
        if site_tz is not None:
            return store_to_site(canonical, site_tz)
        return canonical

    try:
        raw_since = _sweep_bound(site_window[0] if site_window else since, since)
        raw_until = _sweep_bound(site_window[1] if site_window else until, until)
    except ClockDomainError as exc:  # keep the glue's never-raises contract
        return f"cannot derive site-domain sweep bounds: {exc}"
    since_frappe = _to_frappe_clock(raw_since)
    until_frappe = _to_frappe_clock(raw_until, end_of_day=True)

    try:
        key, secret = resolve_auth(t, env=env, read_file=_read_file)
    except RegistryError as exc:
        return f"cannot resolve credential for reconciliation: {exc}"

    client = ErpnextClient(base_url=t.base_url, api_key=key, api_secret=secret,
                           transport=transport or default_transport)

    try:
        gl_rows = client.sweep_gl_entries(t.company, since_frappe, until_frappe)
        settings = client.get_accounts_settings(
            ["enable_immutable_ledger", "delete_linked_ledger_entries"])
        if not isinstance(settings, dict):
            # A proxy-shaped {"data": null}/list body — posture unreadable. Refuse INSIDE the try so
            # the glue keeps its "never raises past its boundary" contract: the later settings.get()
            # would otherwise AttributeError out of cmd_close as a raw traceback (sec-A).
            raise ErpnextError("accounts-settings read returned a non-dict body; posture "
                               "unreadable, refusing")
    except ErpnextError as exc:
        return f"reconciliation audit source unreadable: {exc}"

    extra_flags = []
    try:
        reposts = client.get_reposts(t.company, since_frappe, until_frappe)
    except ErpnextError as exc:
        reposts = []
        extra_flags.append(
            f"repost source unreadable — second-generation attribution unavailable ({exc})")

    posture = {"enable_immutable_ledger": _posture_bool(settings.get("enable_immutable_ledger")),
              "delete_linked_ledger_entries": _posture_bool(
                  settings.get("delete_linked_ledger_entries"))}
    snapshot = {"gl_rows": gl_rows, "reposts": reposts, "posture": posture}

    result = build_reconciliation(receipts, snapshot, target=name, company=t.company,
                                  since=since, until=until, seat_owner=t.seat_user)
    if extra_flags:
        result["flags"] = result["flags"] + extra_flags
    return result


def _to_frappe_clock(value, *, end_of_day=False):
    """Convert a Pacioli-CLI-style ISO timestamp (``2026-06-01`` or ``2026-06-01T00:00:00Z``) to
    the frappe SERVER-clock format ``sweep_gl_entries``/``get_reposts`` filter on: swap the ``T``
    separator for a space and strip a trailing ``Z``. A bare date normally reads as midnight — fine
    for the inclusive LOWER bound. As the inclusive UPPER bound (``end_of_day``), a bare date would
    drop every row created DURING that last day (MariaDB reads ``2026-06-30`` as ``00:00:00``), so a
    bare-date upper bound expands to end-of-day (F3, one implementation —
    ``pacioli.clock.expand_bare_date_end_of_day``). A bound that already carries a time is left
    alone."""
    from pacioli.clock import expand_bare_date_end_of_day
    s = value.replace("T", " ")
    if s.endswith("Z"):
        s = s[:-1]
    if end_of_day:
        s = expand_bare_date_end_of_day(s)
    return s


def _posture_bool(value):
    """``None`` (a missing field) stays ``None`` — ``build_reconciliation``'s "unreadable" signal.
    A real ``bool`` passes through; an ``int`` (ERPNext's 0/1 checkbox wire form) coerces to bool
    (``reconciliation.py``'s ``is False``/``is None`` identity checks need an actual bool, not a
    truthy int — ``0 is False`` is ``False`` in Python). ANY other type (a stray string from a
    proxy) becomes ``None`` = unreadable — deny-biased, never truthy-coerced to a falsely-safe 'on'
    (``bool("0")`` would be ``True``). The core normalizes too; this is defense in depth (sec-E)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return None


def _print_seal_state(state):
    """Human-legible render of one :meth:`~pacioli.store.BrokerStore.seal_state` dict — shared by
    ``seal``, ``unseal`` (the state the append just produced), and ``seal-status`` (the state as
    read). ``cause`` is printed only when present (a genuinely fail-closed verdict — zero rows, a
    rollback gap, an unverifiable HMAC — never a clean operator seal/unseal)."""
    print(f"sealed: {state['sealed']}")
    print(f"since:  {state['since']}")
    print(f"reason: {state['reason']!r}")
    print(f"source: {state['source']!r}")
    print(f"seq:    {state['seq']}")
    if state.get("cause"):
        print(f"cause:  {state['cause']!r}")


def _print_repin_reminder():
    """F2 (security redteam 2026-07-15): "re-pin after every seal/unseal" is the load-bearing
    operator discipline behind the off-box seal anchor — a seal or unseal taken after the last
    `pacioli anchor write` is not covered by that pin, so a rollback back to the pre-seal/
    pre-unseal state is silently indistinguishable, at the next `anchor check`, from a normal
    unseal/re-seal. Before this, that discipline lived only in an abstract limits paragraph
    (README's "Honest ceiling"), never at the point of action — an operator who pinned once at
    install and never again gets ZERO seal-rollback protection. Printed after every successful
    `seal`/`unseal` append (stderr — the state render above stays the authoritative stdout
    output); mirrors the trailing guidance `cmd_anchor_write` already prints."""
    print("note: this changed the seal history — the off-box seal anchor (if any) no longer "
          "covers the CURRENT state. Run `pacioli anchor write` and move the new pin off this "
          "host; until you do, a rollback to what the state was just before this command would "
          "check clean against the stale pin.", file=sys.stderr)


def _print_seal_status(name, state, events):
    print(f"target: {name}")
    _print_seal_state(state)
    print(f"events: {len(events)} total")
    for e in events[-5:]:
        v = e["verified"]
        vtxt = "verified" if v is True else ("HMAC MISMATCH" if v is False else "unverified (keyless)")
        print(f"  seq {e['seq']:>3}  {e['ts']}  {e['action']:<7} source={e['source']!r} "
              f"reason={e['reason']!r}  [{vtxt}]")


def cmd_seal(env, reason, target):
    """Operator CONTAIN: append a ``seal`` event (``source="operator"``) and print the resulting
    state. ``--reason`` is required at the argparse boundary (a confession without a reason is
    not an entry).

    Exit 0 whenever the append itself succeeds — appending a ``seal`` event can only ever MAKE
    the state sealed (:meth:`~pacioli.store.BrokerStore.seal`'s own contract), so there is no
    "succeeded but somehow not sealed" branch to defend against here the way ``unseal`` must.
    A failure to even resolve the target/store, or to append the event, is reported plainly and
    exits 1 — never a raw traceback."""
    from pacioli.registry import RegistryError
    try:
        name = _target_name(env, target)
        store = open_store(env, name, with_key=True)
    except (RuntimeError_, RegistryError, StoreCorruptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        state = store.seal(reason, source="operator")
    except Exception as exc:  # noqa: BLE001 — a seal-append failure must be reported, never crash raw
        print(f"error: could not append seal event: {exc}", file=sys.stderr)
        return 1
    print(f"target: {name}")
    _print_seal_state(state)
    _print_repin_reminder()
    return 0


def cmd_unseal(env, reason, target):
    """Clear the seal: append an ``unseal`` event (always ``source="operator"`` —
    :meth:`~pacioli.store.BrokerStore.unseal` never accepts another source; unsealing is never
    automatic). ``--reason`` is required at the argparse boundary.

    Exit 0 when the resulting state reads unsealed. Exit 1 (defensive, but reachable — not merely
    hypothetical) when it still reads sealed after the append: an ``unseal`` event heals nothing
    about a PRE-EXISTING fail-closed cause in the history (e.g. a seq gap from an earlier
    tampered/deleted row) — appending after it does not restore contiguity, so the state can
    legitimately still read sealed. The truthful post-append state is always printed either way;
    the exit code and the loud stderr line are what tell an operator (or a script) the clear did
    NOT actually take."""
    from pacioli.registry import RegistryError
    try:
        name = _target_name(env, target)
        store = open_store(env, name, with_key=True)
    except (RuntimeError_, RegistryError, StoreCorruptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        state = store.unseal(reason)
    except Exception as exc:  # noqa: BLE001 — an unseal-append failure must be reported, never crash raw
        print(f"error: could not append unseal event: {exc}", file=sys.stderr)
        return 1
    print(f"target: {name}")
    _print_seal_state(state)
    _print_repin_reminder()  # an unseal event was appended either way — the pin below is stale
    if state["sealed"]:
        print(f"error: state remains SEALED after unseal (cause: {state['cause']!r}) — the "
              "unseal event was still recorded (the history stays honest), but the underlying "
              "history cannot be cleared by an append alone; investigate before assuming this "
              "broker is open again", file=sys.stderr)
        return 1
    return 0


def cmd_seal_status(env, target, as_json, anchor=None):
    """Read-only: render the broker's current CONTAIN state plus a tail of its seal history.

    Never crashes on a fail-closed state — :meth:`~pacioli.store.BrokerStore.seal_state` already
    turns a zero-row/gapped/unverifiable history into ``sealed=True`` with a ``cause``; this only
    renders it (the confession must stay readable even while sealed). Works keyed (the
    authoritative read).

    Exit 2 when sealed, 0 when unsealed — deliberately distinct from the generic error exit (1),
    so a script can tell "sealed" apart from "could not even check" (a resolve/read failure).

    ``anchor`` (optional, since 0.21.0 — the seal-anchor slice): a path to a previously recorded
    ``pacioli anchor write`` pin (or ``"-"`` for stdin). When given, its ``seal_head``/
    ``seal_count`` are threaded into :meth:`~pacioli.store.BrokerStore.seal_state` exactly as
    ``pacioli anchor check`` already does, so a keyless tail-rollback against the OFF-BOX pin
    renders SEALED here too — the everyday status command, not only the dedicated check. ``None``
    (the default): byte-identical to the pre-0.21.0 behavior (Global Constraint 4 — no pin
    supplied, ``seal_state()`` runs its plain content-only derivation).

    A v1 pin (predates seal anchoring, no ``seal_head``/``seal_count`` fields) is not an error:
    a WARNING is printed to stderr that it does not cover the seal, and the on-box state is
    rendered exactly as with no ``--anchor`` at all — never silently claimed as covered. A pin
    recorded for a different target, or one that is unreadable/malformed (bad JSON, a shape
    violation, a partial seal-field pair), is refused loudly (exit 1, nothing rendered) — this is
    audit-time DETECTION machinery; a caller must never be told "unanchored" when what actually
    happened is "the anchor could not be trusted," or a real rollback could hide behind a broken
    pin path.

    This is audit-time DETECTION, gated by the operator's own check cadence — not real-time
    prevention; nothing here blocks any write, on-box, ever (the same honest guarantee
    ``pacioli anchor check`` and :meth:`~pacioli.store.BrokerStore.seal_state` already carry)."""
    from pacioli.registry import RegistryError
    try:
        name = _target_name(env, target)
        store = open_store(env, name, with_key=True)
    except (RuntimeError_, RegistryError, StoreCorruptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    expected_seal_head = expected_seal_count = None
    if anchor is not None:
        from pacioli.anchor import parse_anchor

        if anchor == "-":
            text = sys.stdin.read()
        else:
            from pathlib import Path
            try:
                text = Path(anchor).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"error: cannot read anchor: {exc}", file=sys.stderr)
                return 1
        try:
            record = parse_anchor(text)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if record["target"] != name:
            print(f"error: anchor was recorded for target {record['target']!r}, not {name!r} — "
                  "refusing the cross-target check", file=sys.stderr)
            return 1
        if "seal_head" not in record:
            # v1 pin: predates seal anchoring. Deny-biased in the other direction too — never
            # claim the seal is covered just because nothing here contradicts it (mirrors
            # cmd_anchor_check's identical warning, verbatim).
            print("warning: this pin predates seal anchoring (v1); a seal rollback is NOT "
                  "covered by it — re-pin with 0.21.0 to cover the seal.", file=sys.stderr)
        else:
            expected_seal_head = record["seal_head"]
            expected_seal_count = record["seal_count"]

    try:
        state = store.seal_state(expected_seal_head=expected_seal_head,
                                 expected_seal_count=expected_seal_count)
        events = store.seal_events()
    except Exception as exc:  # noqa: BLE001 — an unreadable seal state must be reported, never crash raw
        print(f"error: could not read seal state: {exc}", file=sys.stderr)
        return 1
    if as_json:
        import json
        print(json.dumps(
            {"target": name, "state": state, "event_count": len(events),
             "recent_events": events[-5:]},
            indent=1, default=str))
    else:
        _print_seal_status(name, state, events)
    return 2 if state["sealed"] else 0


def cmd_attest(env, reason, target, seq=None):
    """Operator ceremony (Half 3, Fork A1, Task 3): append an ``attest`` row that clears a gapped
    close awaiting attestation, mirroring ``cmd_unseal``'s shape exactly — ``--reason`` required at
    the argparse boundary, always ``source="operator"`` (:meth:`~pacioli.store.BrokerStore.attest`
    never accepts another source), append-only history (a correction is a new row, never an edit).

    Refuses (:class:`~pacioli.store.NoAttestationPendingError`, exit 2) when there is nothing
    gapped currently pending to clear — an attestation must attest *something*, it never fires
    speculatively (Q2 of the design doc). That same exception ALSO fires when the gate is LATCHED
    for an INTEGRITY reason (a close-record ``seq`` gap or an unverifiable hmac) rather than a
    real pending workflow gap — :meth:`~pacioli.store.BrokerStore.attest`'s own contract refuses
    both cases identically, so this command re-reads :meth:`~pacioli.store.BrokerStore.close_gate_state`
    once (the write already failed; this second read is for the RENDER only, naming which of the
    two happened) and renders the two refusals in different words: a real pending gap says so
    plainly; an integrity failure names the failure and explicitly does NOT claim attesting can
    repair it (the same discipline ``close --advance``'s own integrity-refusal render already
    carries — see ``_render_advance``).

    A resolve/open failure or any other append failure (a keyless store's ``ValueError``, a raw
    sqlite error) is reported plainly and exits 1 — never a raw traceback, never conflated with the
    exit-2 refusal above (a script needs to tell "nothing to attest" apart from "could not even
    try").

    **``--seq N`` + compare-and-append (redteam 2026-07-15, model-fidelity F1):** the pending
    gapped close's seq is read from the gate state ONCE (one snapshot backs both the pre-check
    and the render data) and threaded into :meth:`~pacioli.store.BrokerStore.attest`'s required
    ``expected_seq`` — so an attest can never clear a DIFFERENT gap than the one this invocation
    saw pending. When the operator supplies ``--seq`` explicitly it must equal the pending seq or
    this refuses (exit 1) naming the ACTUAL pending gap — "review THAT gap before attesting". A
    race that changes the pending gap between this read and the store's own in-transaction
    re-check surfaces as :class:`~pacioli.store.AttestStaleError` (exit 1) — same shape, caught
    at the store instead.
    """
    from pacioli.registry import RegistryError
    try:
        name = _target_name(env, target)
        store = open_store(env, name, with_key=True)
    except (RuntimeError_, RegistryError, StoreCorruptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # ONE snapshot: the pending-seq pre-check and the period lookup for the success render both
    # come from these same rows (mirrors close_records_snapshot's own reason for existing).
    pre_state, pre_rows = store.close_records_snapshot()
    pending_seq = (pre_state["last_close_seq"]
                   if pre_state["cause"] == "gapped_awaiting_attestation" else None)
    if seq is not None and pending_seq is not None and seq != pending_seq:
        pending_row = next((r for r in pre_rows if r[0] == pending_seq and r[2] == "close"), None)
        period = "?"
        if pending_row is not None:
            period = f"{pending_row[3] or 'genesis'}..{pending_row[4] or 'now (open-ended)'}"
        print(f"error: the pending gap is seq {pending_seq} (period {period}), not seq {seq} — "
              "review THAT gap before attesting (an attest must describe the gap it actually "
              "clears; see `pacioli close-status`)", file=sys.stderr)
        return 1
    expected = seq if seq is not None else pending_seq
    try:
        state = store.attest(reason, expected_seq=expected)
    except NoAttestationPendingError:
        current = store.close_gate_state()
        if current["cause"] in ("keyless", "gap", "unverifiable"):
            print(f"error: cannot attest — the close-record history itself failed integrity "
                  f"verification ({current['cause']}): {current['reason']} — an attestation "
                  "cannot repair a corrupt history; investigate before advancing any period "
                  "(this is the same failure `close --advance` refuses on)", file=sys.stderr)
        else:
            print("error: nothing to attest — the close gate is OPEN (no gapped close is "
                  "currently awaiting attestation); an attestation must attest something, it "
                  "never fires speculatively", file=sys.stderr)
        return 2
    except AttestStaleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — an append failure must be reported, never crash raw
        print(f"error: could not append attest event: {exc}", file=sys.stderr)
        return 1
    print(f"target:   {name}")
    _, rows = store.close_records_snapshot()
    closed = next((r for r in rows if r[0] == state["last_close_seq"] and r[2] == "close"), None)
    if closed is not None:
        since, until = closed[3], closed[4]
        period = f"{since or 'genesis'}..{until or 'now (open-ended)'}"
        print(f"attested: close seq {state['last_close_seq']} (period {period})")
    print(f"reason:   {reason!r}")
    # From the state attest actually RETURNED — never a hardcoded "OPEN" (redteam finding 6): if
    # the freshly-derived gate is somehow still latched, say so honestly.
    if state["latched"]:
        print(f"gate:     STILL LATCHED ({state['cause']}) — {state['reason']}")
    else:
        print("gate:     OPEN — the next `close --advance` is not blocked")
    return 0


def _cursor_words(state):
    """The genesis/open-ended/unknown distinction, rendered in words (Task 3's own requirement —
    never leave an operator to infer these from a bare ``None``)."""
    if state["cause"] in ("keyless", "gap", "unverifiable"):
        return "UNKNOWN — close-record history integrity failure, cannot be trusted"
    if state["last_close_seq"] is None:
        return "no period ever closed (genesis — the first `close --advance` is legitimate)"
    if state["cursor"] is None:
        # Only HISTORICAL rows can look like this since the redteam wave: record_close refuses
        # period_until=None and the CLI materializes a concrete until from the store clock.
        return ("latest close is open-ended (no period_until was set — a historical row; "
                "the write path now always records a concrete cursor)")
    return state["cursor"]


def _close_row_to_dict(row):
    seq, ts, action, period_since, period_until, attested_head, gapped, reason, source, mac = row
    return {"seq": seq, "ts": ts, "action": action, "period_since": period_since,
            "period_until": period_until, "gapped": bool(gapped), "reason": reason,
            "source": source}


def _print_close_status(name, state, tail, total):
    print(f"target: {name}")
    print(f"gate:   {'LATCHED' if state['latched'] else 'OPEN'}")
    if state["cause"]:
        print(f"cause:  {state['cause']!r}")
    if state["reason"]:
        print(f"reason: {state['reason']}")
    print(f"cursor: {_cursor_words(state)}")
    if state["cause"] == "gapped_awaiting_attestation":
        print(f"fix:    pacioli attest --target {name} --reason <why> — clears the gap so the "
              "next `close --advance` can proceed")
    print(f"history: {total} total")
    for row in tail:
        seq, ts, action, since, until, head, gapped, reason, source, mac = row
        period = f"{since or 'genesis'}..{until or 'now'}"
        # reason printed always (redteam finding 8): the attest rows carry the ceremony's whole
        # point; a close row's empty reason stays honestly empty rather than being hidden.
        print(f"  seq {seq:>3}  {ts}  {action:<6} period={period}  gapped={bool(gapped)}  "
              f"source={source!r}  reason={reason!r}")


def cmd_close_status(env, target, as_json):
    """Read-only (Half 3, Fork A1, Task 3): render the close-record cursor + attestation-gate
    state, plus a history tail — mirrors ``cmd_seal_status``'s shape and exit contract exactly.

    Never gated (Global constraint 5): opens the store WITH KEY (the authoritative read, same as
    ``seal-status``) and renders whatever :meth:`~pacioli.store.BrokerStore.close_records_snapshot`
    hands back, including every fail-closed cause ``close_gate_state`` itself already turns
    content into rather than raising — a keyless open (``cause="keyless"``), a ``seq`` gap, an
    unverifiable hmac, or a genuine workflow latch (``gapped_awaiting_attestation``, the only cause
    this command's render also names the ``pacioli attest`` command for — an integrity cause never
    gets that suggestion, since attesting cannot repair a corrupt history).

    The cursor is rendered in WORDS for the two cases a bare value can't distinguish on its own
    (:meth:`~pacioli.store.BrokerStore.close_cursor`'s own docstring, Task 1's adversarial review
    finding 2): ``last_close_seq is None`` → "no period ever closed" (honest genesis); a real close
    whose ``period_until`` was never set → "latest close is open-ended".

    Exit 0 when the gate is open, 2 when LATCHED for ANY cause — scriptable, mirrors
    ``seal-status``. ``--json`` emits the raw :meth:`~pacioli.store.BrokerStore.close_gate_state`
    dict plus the same history tail as structured data.
    """
    from pacioli.registry import RegistryError
    try:
        name = _target_name(env, target)
        store = open_store(env, name, with_key=True)
    except (RuntimeError_, RegistryError, StoreCorruptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    state, rows = store.close_records_snapshot()
    tail = rows[-5:]
    if as_json:
        import json
        doc = {"target": name, "state": state, "history_count": len(rows),
               "recent_history": [_close_row_to_dict(r) for r in tail]}
        print(json.dumps(doc, indent=1, default=str))
    else:
        _print_close_status(name, state, tail, len(rows))
    return 2 if state["latched"] else 0


def cmd_orphans(env, target):
    try:
        store = open_store(env, _target_name(env, target), with_key=False)
    except (RuntimeError_, StoreCorruptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        orphans = store.orphans()
    except StoreCorruptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not orphans:
        print("ok: no orphan intents.")
        return 0
    print(f"{len(orphans)} orphan intent(s) — reconcile each against the real docstatus:",
          file=sys.stderr)
    for r in orphans:
        print(f"  seq {r.seq} {r.ts} {r.body}", file=sys.stderr)
    return 1


def cmd_anchor_write(env, target, out):
    """Emit the current chain head — and, since 0.21.0, the seal head alongside it; since
    0.26.0, the close-record head too (format v3) — as an anchor record: the pin the OPERATOR
    carries off-box.

    Refuses to pin a chain that does not verify (pinning a tampered chain would launder it as
    truth), and — the same reasoning applied to the seal table — refuses to pin a seal history
    that is not itself fully verifiable (a zero-row/gapped/unverifiable ``seal_state()``; see its
    own docstring): witnessing an already-broken seal history as a trustworthy off-box pin would
    be recording a lie, not a fact. A genuinely, verifiably SEALED broker (a real operator seal,
    ``cause is None``) is not "broken" in this sense and is pinned normally — this check is about
    whether the history can be vouched for, not about the broker's current CONTAIN status. The
    close-record history follows the identical rule: any cause other than the workflow latch
    (``gapped_awaiting_attestation`` — a fully verified, contiguous history whose gate is up
    awaiting an operator's attest) refuses; the workflow latch itself pins normally, exactly as
    the SEALED broker does. An EMPTY close table is not a refusal either — "no period has ever
    closed yet" is the honest genesis state for a cursor, pinned as (``GENESIS``, 0): the COUNT
    is the claim.

    Emits to stdout by default so the operator decides where the pin goes; a local file
    (``--out``) is a convenience only — a copy on THIS host proves nothing. This is audit-time
    DETECTION machinery, not real-time prevention: nothing here blocks any write, on-box, ever."""
    from datetime import datetime, timezone

    from pacioli.anchor import make_anchor, render_anchor
    from pacioli.prove import GENESIS

    name = _target_name(env, target)
    try:
        store = open_store(env, name, with_key=True)
    except (RuntimeError_, StoreCorruptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # ONE snapshot for both the verify and the pin we build from it — reading twice would let a
    # concurrent host-level writer pin a head the verify never covered (the anchor's own threat).
    ok, reason, receipts = store.verify_snapshot()
    if not ok:
        print(f"FAILED: refusing to anchor a chain that does not verify: {reason}",
              file=sys.stderr)
        return 1
    # Same discipline for the seal history: a fail-closed (cause-carrying) seal_state means the
    # history itself is not fully vouched for (zero rows / a seq gap / an unverifiable latest
    # row) — don't witness that as an off-box pin. `seal_state_snapshot()` is ONE `SELECT` for
    # state/head/count together (F3 fix, correctness redteam 2026-07-15) — the prior code here
    # read these three separately (`seal_state()` / `seal_head()` / `seal_count()`), so a
    # concurrent seal/unseal (another CLI invocation, or an auto-CONTAIN `close --respond`)
    # landing between any two of those reads could pair a stale derivation with a fresh
    # head/count (or vice versa) and emit a self-inconsistent pin — one that later false-alarms
    # "diverges from the off-box anchor" against a history that was never actually tampered
    # with. One fetch closes that window the same way `verify_snapshot()` already does for
    # receipts, above.
    seal_state, seal_head, seal_count = store.seal_state_snapshot()
    if seal_state["cause"] is not None:
        print("FAILED: refusing to anchor a seal history that is not verifiable: "
              f"{seal_state['cause']}", file=sys.stderr)
        return 1
    # Close-record history, same one-snapshot discipline (`close_records_snapshot()` reads
    # state AND rows in one SELECT — its docstring names "an anchor pin" as an intended
    # caller): deny-biased refusal on ANY cause except the documented workflow-latch tag
    # (`gapped_awaiting_attestation` — verified, contiguous, gate up awaiting attest — pins
    # normally, exactly as a genuinely SEALED broker does). This is the store contract's
    # stable machine-checkable tag, not prose-matching.
    close_state, close_rows = store.close_records_snapshot()
    if close_state["cause"] is not None \
            and close_state["cause"] != "gapped_awaiting_attestation":
        print("FAILED: refusing to anchor a close-record history that is not verifiable: "
              f"{close_state['cause']}", file=sys.stderr)
        return 1
    # head/count derived from the SAME rows the state derivation covered — never re-queried.
    close_head = close_rows[-1][9] if close_rows else GENESIS
    close_count = len(close_rows)
    head = receipts[-1].hmac if receipts else GENESIS
    if seal_head is None:
        seal_head = GENESIS  # mirrors the receipt side's own empty-chain sentinel (anchor.py)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = render_anchor(make_anchor(name, head, len(receipts), ts,
                                     seal_head=seal_head, seal_count=seal_count,
                                     close_head=close_head, close_count=close_count))
    if out in (None, "-"):
        sys.stdout.write(text)
    else:
        from pathlib import Path
        Path(out).write_text(text, encoding="utf-8")
        print(f"anchor written to {out}", file=sys.stderr)
    print("move this anchor OFF this host (another machine, a git remote, paper) — a copy the "
          "broker host can touch proves nothing. Run `pacioli anchor check` against the old pin "
          "BEFORE rotating to this one. Re-pin again after every future `seal`/`unseal` and "
          "every `close --advance`/`attest` — a row appended after this pin is not covered by "
          "it until the next `pacioli anchor write`.", file=sys.stderr)
    return 0


def cmd_anchor_check(env, target, infile):
    """Verify the live chain against a previously recorded anchor — and, when the pin carries
    seal fields (format v2+), the live seal history against the pin's ``seal_head``/
    ``seal_count``, and, when it carries close fields (format v3), the live close-record
    history against ``close_head``/``close_count`` too. Exit 0 only when every applicable
    check passes; any one check alone proves too little.

    An OLDER pin is NOT an error: everything it covers is still checked exactly as before, but
    a WARNING is printed to stderr for each table it does not cover (v1: seal AND close; v2:
    close) — never silently treated as "fine" just because nothing contradicted it. Re-pinning
    with ``pacioli anchor write`` on the current version closes the gap.

    This is audit-time DETECTION, gated by the operator's own check cadence — not real-time
    prevention; a rollback followed by a re-forward before the next check is invisible here, the
    same disclosed limit the receipt-side anchor has always carried."""
    from pacioli.anchor import compare, parse_anchor

    if infile == "-":
        text = sys.stdin.read()
    else:
        try:
            from pathlib import Path
            text = Path(infile).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read anchor: {exc}", file=sys.stderr)
            return 2
    try:
        record = parse_anchor(text)
    except ValueError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    name = _target_name(env, target)
    try:
        store = open_store(env, name, with_key=True)
    except (RuntimeError_, StoreCorruptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # ONE snapshot: compare() must run against the SAME receipts the keyed verify covered, or a
    # concurrent host-level writer could slip a chain past compare that verify never checked
    # (compare does no HMAC recompute — it trusts that its input was the verified input).
    ok, reason, receipts = store.verify_snapshot()
    if not ok:
        print(f"FAILED: {reason}", file=sys.stderr)
        return 1
    ok, reason = compare(record, name, [r.hmac for r in receipts])
    if not ok:
        print(f"FAILED: {reason}", file=sys.stderr)
        return 1
    if "seal_head" not in record:
        # v1 pin: receipts-only coverage. Deny-biased in the other direction too — never claim
        # the seal is covered just because nothing here contradicts it.
        print("warning: this pin predates seal anchoring (v1); a seal rollback is NOT covered "
              "by it — re-pin with `pacioli anchor write` to cover the seal.", file=sys.stderr)
    else:
        # `seal_state()`'s own contract (store.py) is the whole test: `cause` is `None` iff the
        # seal history is genuinely verified/contiguous with an agreeing pin, and non-`None` on
        # ANY fail-closed branch — a pin mismatch (tail-behind, diverges) same as a content-only
        # break (a gap, an unverifiable latest row) that the pin position itself never disagreed
        # with. `sealed` is dead weight here (always `True` whenever `cause` is set); don't
        # re-derive the condition by pattern-matching the cause TEXT — that's what silently let
        # a gap/unverifiable seal history past this check before (Task 2 review, Critical).
        seal_result = store.seal_state(expected_seal_head=record["seal_head"],
                                       expected_seal_count=record["seal_count"])
        seal_cause = seal_result["cause"]
        if seal_cause is not None:
            print(f"FAILED: {seal_cause}", file=sys.stderr)
            return 1
    if "close_head" not in record:
        # a pre-v3 pin (v1 or v2): the close chain is honestly uncovered. Same deny-biased
        # posture as the seal's own warning — never claim coverage nothing contradicted.
        print("warning: this pin predates close anchoring (pre-0.26.0); a close-record "
              "rollback is NOT covered by it — re-pin with `pacioli anchor write` to cover "
              "the close history.", file=sys.stderr)
    else:
        # `close_gate_state`'s contract is the whole test, with ONE documented exemption:
        # `cause` is None on a verified/contiguous history with an agreeing pin, and the
        # stable workflow-latch tag `gapped_awaiting_attestation` is a LEGITIMATE state (fully
        # verified rows, gate up awaiting an operator's attest — the pin itself agreed, or the
        # pin's own cause would have replaced it). Every other cause — integrity (`keyless`/
        # `gap`/`unverifiable`) or pin (`anchor_behind`/`anchor_diverged`/`anchor_malformed`) —
        # fails the check. Stable tags from the store contract, never prose-matching (the seal
        # slice's Task-2 Critical, inherited as a rule).
        close_result = store.close_gate_state(
            expected_close_head=record["close_head"],
            expected_close_count=record["close_count"])
        close_cause = close_result["cause"]
        if close_cause is not None and close_cause != "gapped_awaiting_attestation":
            print(f"FAILED: {close_result['reason']}", file=sys.stderr)
            return 1
    print(f"ok: chain matches the anchor (pinned {record['count']} receipt(s) at "
          f"{record['ts']}; live chain has {len(receipts)}, verifies).")
    if len(receipts) > record["count"]:
        print(f"note: {len(receipts) - record['count']} receipt(s) appended since this pin are "
              "not yet covered by any pin — rotate with `pacioli anchor write` when checked.",
              file=sys.stderr)
    return 0


def cmd_doctor(env, target, offline):
    """Read-only config & readiness report. Exit 0 only when zero checks fail."""
    from pacioli.doctor import run_doctor

    code, lines = run_doctor(env, target_name=target, offline=offline)
    out = sys.stdout if code == 0 else sys.stderr
    for line in lines:
        print(line, file=out)
    return code


def _target_name(env, target):
    if target:
        return target
    from pacioli.runtime import _load_registry_from_env
    return _load_registry_from_env(env).get(None).name


def build_parser():
    p = argparse.ArgumentParser(
        prog="pacioli",
        description="Pacioli Broker — governed ERPNext writes. This CLI is the HUMAN side: "
                    "the merchant's hand (mint consent) and the trial balance (verify the "
                    "books). The agent-facing MCP server is `pacioli serve`.")
    p.add_argument("--version", action="version", version=f"pacioli {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("mint", help="mint a single-use consent marker for a recorded plan")
    m.add_argument("plan_id", help="the plan_id the agent returned from plan_submit")
    m.add_argument("--target", help="registry target (default: the registry default)")
    m.add_argument("--ttl", type=int, default=900, help="seconds the marker stays live (default 900)")

    v = sub.add_parser("verify", help="the trial balance: verify a target's PROVE receipt chain "
                                      "(don't go to sleep until the debits equal the credits)")
    v.add_argument("--target")
    v.add_argument("--expected-head", help="off-box-recorded head hmac (catches truncation/wipe)")

    o = sub.add_parser("orphans", help="list intent receipts with no committed outcome — "
                                       "a debit with no credit; reconcile each against the "
                                       "real docstatus")
    o.add_argument("--target")

    cl = sub.add_parser("close", help="the close: a period statement — every governed act, "
                                      "balanced or confessed, on a verified chain (the proof made "
                                      "readable). Attests only to what passed "
                                      "through Pacioli — not the whole ledger")
    cl.add_argument("--target")
    cl.add_argument("--since", help="lower bound, inclusive (default: genesis). With a registry "
                                    "site_tz declared, bounds mean SITE time (the books' own "
                                    "calendar), converted once at this boundary; without one, the "
                                    "statement windows receipts by Pacioli's UTC clock while "
                                    "--reconcile windows GL rows by ERPNext's server CREATION "
                                    "clock — the same string, two clocks (declare site_tz to "
                                    "align them)")
    cl.add_argument("--until", help="upper bound, inclusive (default: now). A bare date covers the "
                                    "whole day (in site time when site_tz is declared). For "
                                    "--reconcile, see its note on backdated postings")
    cl.add_argument("--expected-head", help="off-box-recorded head hmac — unbalances the statement "
                                            "if the live head disagrees (truncation/wipe)")
    cl.add_argument("--json", action="store_true", dest="as_json",
                    help="emit the structured statement instead of the rendered document")
    cl.add_argument("--reconcile", action="store_true",
                    help="ALSO run the Close's Half 2 (the Reconciliation): join the Statement "
                         "against a live ERPNext GL Entry sweep for the same period and classify "
                         "every voucher governed / ungoverned / second-generation. Requires a "
                         "company-pinned target and both --since/--until. The sweep windows GL rows "
                         "by CREATION time (when a row was actually written) — so to catch a "
                         "posting BACKDATED into a closed period, set --until past the period-end "
                         "(e.g. to now), not to the period's last posting date")
    cl.add_argument("--respond", action="store_true",
                    help="ALSO run the Close's Half 3 (response-to-gap): apply the operator's "
                         "posture and response envelope to the findings and emit the reaction "
                         "(record / alert / attestation-gate / CONTAIN). Over a statement alone it "
                         "responds to orphans/unconfirmed at the default mixed-door posture; add "
                         "--reconcile to also weigh ungoverned/second-generation movement. Exit is "
                         "non-zero when any finding rises above 'record'")
    cl.add_argument("--envelope", action="append", metavar="CLASS=LEVEL",
                    help="escalate one finding class's response floor for --respond (repeatable), "
                         "e.g. --envelope orphan=contain. CLASS: orphan|unconfirmed|"
                         "second_generation|blind_read|adverse_posture|ungoverned. LEVEL: "
                         "record|alert|attestation_gate|contain. Only ESCALATES above a class's "
                         "deny-biased floor, never silences below it. Only meaningful with "
                         "--respond (present without it is a usage error, exit 2). Reaching "
                         "'contain' seals the broker itself (source=response) — for every class "
                         "listed here, CONTAIN is never a default, only ever this explicit "
                         "opt-in. (Since broker 0.22.0: 'chain_broken' — Pacioli's own receipt "
                         "chain failing to verify — is the one exception; it is not a CLASS "
                         "accepted here, its floor is 'contain' unconditionally, and it can reach "
                         "seal_required on a plain --respond with no --envelope at all. It does "
                         "NOT fire on an off-box anchor mismatch alone — that naive check was "
                         "dropped 2026-07-15; use `pacioli anchor check`/`seal-status --anchor` "
                         "for count-aware receipt-rollback detection)")
    cl.add_argument("--advance", action="store_true",
                    help="ALSO write the close record (the period-loop cursor + attestation gate) "
                         "after the render. Requires --respond (a usage error, exit 2, without it). "
                         "--since defaults from the verified cursor when omitted (the next close "
                         "covers only new activity), unless no close has ever been recorded. "
                         "Refuses the write (exit 1) — never the read above it — when a prior close "
                         "closed over a gap awaiting attestation (`pacioli attest` clears it) or the "
                         "close-record history itself fails integrity verification. Plain close/"
                         "close --respond (no --advance) write nothing to the close-record table")

    a = sub.add_parser("anchor", help="record/check the off-box head pin that makes PROVE "
                                      "(and, since 0.21.0, the seal history) tamper-evident "
                                      "against truncation since the last pin")
    asub = a.add_subparsers(dest="anchor_command", required=True)
    aw = asub.add_parser("write", help="emit the current chain head (and seal head/count) as "
                                       "an anchor record. This tool cannot make the pin "
                                       "off-box — YOU carry it off this host (another machine, "
                                       "a git remote, paper)")
    aw.add_argument("--target")
    aw.add_argument("--out", default="-",
                    help="where to write the record; '-' = stdout (default). A file on this "
                         "host is NOT off-box — move it off this machine yourself")
    ac = asub.add_parser("check", help="verify the live chain (and, for a v2 pin, the seal "
                                       "history) against a previously recorded anchor; exit 0 "
                                       "only if every applicable check holds")
    ac.add_argument("--in", dest="infile", required=True,
                    help="path to the recorded anchor, or '-' for stdin")
    ac.add_argument("--target")

    d = sub.add_parser("doctor", help="the inventory: read-only config & readiness checks (is "
                                      "this install actually ready, as the right principal?)")
    d.add_argument("--target", help="check one registry target (default: all)")
    d.add_argument("--offline", action="store_true",
                   help="skip the live bench probe (config checks only)")

    se = sub.add_parser("seal", help="operator CONTAIN: seal the broker — every governed write "
                                     "(including mint) is refused until `pacioli unseal`. A "
                                     "confession without a reason is not an entry")
    se.add_argument("--reason", required=True, help="why you are sealing (required)")
    se.add_argument("--target")

    un = sub.add_parser("unseal", help="clear the seal: append an unseal event, resuming "
                                       "governed writes (never automatic — always the human's "
                                       "hand)")
    un.add_argument("--reason", required=True, help="why you are unsealing (required)")
    un.add_argument("--target")

    sst = sub.add_parser("seal-status", help="read-only: is this broker sealed right now? exit "
                                             "2 if sealed, 0 if not (scriptable) — renders even a "
                                             "fail-closed (zero-row/gap/unverifiable) history "
                                             "rather than crashing on it")
    sst.add_argument("--target")
    sst.add_argument("--json", action="store_true", dest="as_json",
                     help="emit the structured state + recent-events tail instead of the "
                          "rendered text")
    sst.add_argument("--anchor", default=None,
                     help="path to a recorded `pacioli anchor write` pin (or '-' for stdin), "
                          "since 0.21.0: threads its seal_head/seal_count into the read so a "
                          "tail-rollback against the OFF-BOX pin renders SEALED here too, not "
                          "only via `pacioli anchor check`. A v1 pin (predates seal anchoring) "
                          "warns and renders the on-box state instead. Audit-time DETECTION, "
                          "gated by your own check cadence — not real-time prevention")

    at = sub.add_parser("attest", help="operator ceremony: attest a gapped close, clearing the "
                                       "attestation gate so the next `close --advance` can "
                                       "proceed. A confession without a reason is not an entry. "
                                       "Refuses (exit 2) when nothing gapped is currently "
                                       "pending — including when the gate is latched for an "
                                       "integrity failure attesting cannot repair")
    at.add_argument("--reason", required=True, help="why you are attesting (required)")
    at.add_argument("--target")
    at.add_argument("--seq", type=int, default=None,
                    help="the seq of the gapped close you reviewed (see `pacioli close-status`); "
                         "when supplied it must equal the currently-pending gap or the attest "
                         "refuses (exit 1) — an attest must describe the gap it actually clears")

    cst = sub.add_parser("close-status", help="read-only: the close-record cursor + "
                                               "attestation-gate state, plus a history tail. "
                                               "Exit 2 when LATCHED (any cause), 0 when open — "
                                               "scriptable, mirrors seal-status. Never gated: "
                                               "works in every state, including keyless and a "
                                               "fail-closed integrity failure")
    cst.add_argument("--target")
    cst.add_argument("--json", action="store_true", dest="as_json",
                     help="emit the gate-state dict + history tail instead of the rendered text")

    sv = sub.add_parser("serve", help="run the agent-facing server (MCP stdio by default; "
                                      "--http opts into MCP streamable HTTP, --a2a into the "
                                      "A2A door)")
    door = sv.add_mutually_exclusive_group()
    door.add_argument("--http", action="store_true",
                      help="serve MCP over streamable HTTP instead of stdio (OFF by default — "
                           "a deliberate opt-in, the doors ruling)")
    door.add_argument("--a2a", action="store_true",
                      help="serve the A2A door (Agent2Agent JSON-RPC; agent card at "
                           "/.well-known/agent-card.json) instead of stdio — OFF by default, "
                           "a deliberate opt-in; needs pip install 'pacioli[a2a]'")
    sv.add_argument("--bind", default="127.0.0.1",
                    help="bind address (default 127.0.0.1; any non-loopback bind "
                         "refuses to start without --auth)")
    sv.add_argument("--port", type=int, default=None,
                    help="port (default: 8791 for --http, 8792 for --a2a)")
    sv.add_argument("--auth", default=None,
                    help="bearer token BY REFERENCE — env:VAR or file:/path, never inline; "
                         "required for any non-loopback bind, honored on loopback too")
    sv.add_argument("--allowed-hosts", default=None, dest="allowed_hosts",
                    help="in-door Host allowlist (comma-separated) for the DNS-rebind guard; "
                         "default = the bind host + loopback forms. '*' disables the guard "
                         "(only safe behind a Host-validating reverse proxy)")

    kg = sub.add_parser("a2a-keygen",
                        help="mint an EC P-256 signing key for the A2A agent card (opt-in card "
                             "signing; point PACIOLI_A2A_SIGNING_KEY_FILE at it)")
    kg.add_argument("--out", default=None,
                    help="key file path (default $PACIOLI_STATE_DIR/a2a-signing.key); written "
                         "0600, refuses to overwrite an existing key")
    return p


def main(argv=None, env=None):
    import os

    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    if args.command == "mint":
        return cmd_mint(env, args.plan_id, args.target, args.ttl)
    if args.command == "verify":
        return cmd_verify(env, args.target, args.expected_head)
    if args.command == "orphans":
        return cmd_orphans(env, args.target)
    if args.command == "close":
        return cmd_close(env, args.target, args.since, args.until,
                         args.expected_head, args.as_json, reconcile=args.reconcile,
                         respond=args.respond, envelope=args.envelope, advance=args.advance)
    if args.command == "anchor":
        if args.anchor_command == "write":
            return cmd_anchor_write(env, args.target, args.out)
        return cmd_anchor_check(env, args.target, args.infile)
    if args.command == "doctor":
        return cmd_doctor(env, args.target, args.offline)
    if args.command == "seal":
        return cmd_seal(env, args.reason, args.target)
    if args.command == "unseal":
        return cmd_unseal(env, args.reason, args.target)
    if args.command == "seal-status":
        return cmd_seal_status(env, args.target, args.as_json, args.anchor)
    if args.command == "attest":
        return cmd_attest(env, args.reason, args.target, seq=args.seq)
    if args.command == "close-status":
        return cmd_close_status(env, args.target, args.as_json)
    if args.command == "serve":
        allowed_hosts = None
        if getattr(args, "allowed_hosts", None):
            allowed_hosts = [h.strip() for h in args.allowed_hosts.split(",") if h.strip()]
        if getattr(args, "http", False):
            from pacioli.server import serve_http
            return serve_http(env, bind=args.bind, port=args.port or 8791, auth=args.auth,
                              allowed_hosts=allowed_hosts)
        if getattr(args, "a2a", False):
            from pacioli.a2a import DEFAULT_PORT, serve_a2a
            return serve_a2a(env, bind=args.bind, port=args.port or DEFAULT_PORT,
                             auth=args.auth, allowed_hosts=allowed_hosts)
        from pacioli.server import serve
        return serve(env)
    if args.command == "a2a-keygen":
        return cmd_a2a_keygen(env, args.out)
    return 2


def cmd_a2a_keygen(env, out):
    """Mint an A2A card-signing key (0600, refuse-to-overwrite) and print its path + kid."""
    from pathlib import Path

    out_path = out
    if not out_path:
        state_dir = env.get("PACIOLI_STATE_DIR")
        if not state_dir:
            print("error: --out is required (or set PACIOLI_STATE_DIR for the default path)",
                  file=sys.stderr)
            return 2
        out_path = str(Path(state_dir) / "a2a-signing.key")
    try:
        from pacioli.a2a import SIGNING_KEY_ENV, keygen
        key = keygen(out_path)
    except ImportError:
        print("error: signing needs 'pacioli[a2a]' (a2a-sdk[signing] + cryptography). "
              "Install with: pip install 'pacioli[a2a]'", file=sys.stderr)
        return 2
    except RuntimeError_ as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"minted A2A signing key at {out_path} (0600, ES256/P-256; kid {key.kid})")
    print(f"point the door at it: export {SIGNING_KEY_ENV}={out_path}", file=sys.stderr)
    print("keep it in the OPERATOR's vault, outside the agent's own write reach — a key the "
          "agent can rewrite proves nothing (a compromised broker re-signs a forged card).",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
