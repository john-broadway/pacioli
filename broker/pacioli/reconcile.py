# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — RECONCILE: governed payment reconciliation (pure core).

A pure core, no bench imports (I/O is injected, exactly like ``cascade.py``/``spine.py``): one
allocation set — a payment (or Journal Entry) settled against an invoice — governed through
PLAN(fresh + closed-books + allocation ceiling) -> CONSENT -> EXECUTE(one call, readback-driven
outcome). Design context: ``docs/plans/2026-07-09-fr2-govern-reconciliation.md``.

**Why the broker owns the safety here, not the grant.** ERPNext's ``reconcile()`` trusts the
caller's own numbers (``validate_allocation`` checks only the client-submitted ``outstanding_
amount``, never re-fetched) and writes with ``ignore_permissions=True``, bypassing the freeze
belts (frozen account, company period-freeze) that block every other posting. Once the guard
grants the doctype-method pair, it cannot see or bound *which* invoices/payments/amounts the
request names — the guard sees the pair, not the content. So this core supplies everything
ERPNext skips:

  * **Its own over-allocation ceiling from a FRESH read** (:func:`check_allocation`) — never the
    agent's declared ``invoice_outstanding``/``payment_unallocated``; those plan-time fields are
    disclosure-only, not a source of truth for what the invoice/payment holds *now*.
  * **Its own freshness + closed-books checks**, on BOTH the invoice's and the payment's posting
    date — the belt ERPNext bypasses entirely for reconciliation (``adv_adj=1`` skips
    ``validate_frozen_account``; the relink write never reaches ``save_entries``, so company
    period-freeze never applies).
  * **The reconcile body itself, constructed from the pinned plan graph** — never forwarded from
    any agent-supplied allocation list. Without this, the grant would just be a narrower door onto
    arbitrary-content writes.

``run_reconcile`` mirrors ``run_cascade``'s ordering discipline (preflight everything before any
consent or write; a gate failure records nothing and leaves the marker untouched) but its EXECUTE
step is shaped differently: reconciliation is ONE call across the whole allocation set (ERPNext
saves each voucher inside it separately, so a mid-call throw can leave earlier vouchers saved), and
there is no docstatus transition to confirm — the outcome is decided by a READBACK of each
invoice's real post-write outstanding, never by trusting the call's result body.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

from pacioli.consent import commit, release, reserve
from pacioli.plan import check_fresh, check_red_line


def check_allocation(amount, live_outstanding, live_unallocated, eps=0.005):
    """Pure allocation-ceiling check. ``(ok, reason)``, deny-biased.

    Refuses a non-positive/non-numeric ``amount``; a non-numeric ``live_outstanding`` or
    ``live_unallocated`` (an unverifiable ceiling is a refusal, never a pass-through); an
    ``amount`` over either live ceiling (past ``eps`` slack for float noise). ``live_outstanding``
    and ``live_unallocated`` are meant to be FRESH reads taken at call time — this function does
    not care where they came from, but the caller (:func:`run_reconcile`) must never pass a
    plan-time disclosure field here.
    """
    # `math.isfinite` guards are load-bearing, not cosmetic: every comparison against a NaN is
    # False, so a NaN `amount` slips past `amount <= 0` and both `amount > ceiling` checks — the
    # ceiling would silently pass. `consent.py`/`prove.py` guard finiteness the same way; this is
    # the one financial gate that used to omit it (a `plan_reconcile` arg of "nan" reaches here).
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) \
            or not math.isfinite(amount) or amount <= 0:
        return (False, f"allocated amount must be a positive finite number, got {amount!r}")
    if not isinstance(live_outstanding, (int, float)) or isinstance(live_outstanding, bool) \
            or not math.isfinite(live_outstanding):
        return (False, "cannot verify allocation: the invoice's live outstanding is unavailable")
    if amount > live_outstanding + eps:
        return (False, f"allocated {amount} exceeds the invoice's live outstanding {live_outstanding}")
    if not isinstance(live_unallocated, (int, float)) or isinstance(live_unallocated, bool) \
            or not math.isfinite(live_unallocated):
        return (False, "cannot verify allocation: the payment's live unallocated is unavailable")
    if amount > live_unallocated + eps:
        return (False, f"allocated {amount} exceeds the payment's live unallocated {live_unallocated}")
    return (True, None)


def _node_ids(node, seq):
    return {"invoice_type": node["invoice_type"], "invoice_no": node["invoice_no"],
            "payment_type": node["payment_type"], "payment_no": node["payment_no"], "seq": seq}


def _preflight_refusal(stage, total, invoice_no, payment_no, seq, reason):
    stopped_at = {"invoice_no": invoice_no, "payment_no": payment_no, "seq": seq, "reason": reason}
    return {"ok": False, "stage": stage, "confirmed": [], "unconfirmed": [], "total": total,
            "reason": reason, "stopped_at": stopped_at}


def _settle(effects, intent, status, result, final_marker):
    """Record the reconcile outcome; if the store itself refuses the write (a non-finite float or
    any other value that slipped past every upstream check — belt-and-suspenders for
    ``prove.append``'s JSON-native guard), degrade to a minimal always-safe body rather than let
    the raw exception crash past ``dispatch()``'s structured deny (WG-2b, reconcile-path: the
    residual this closes — reconcile.py was the original locus of the concrete WG-2a NaN crash).
    A ``"failed"`` status (pre-wire-truth — an answered refusal the readback proves landed nothing)
    is preserved on retry; every other status is POST-WIRE and is recorded on the degraded retry as
    ``"unconfirmed"`` and ONLY ``"unconfirmed"`` — deny-biased, never letting a recording failure
    make a possibly-in-motion reconcile look like a clean ``"failed"``, and never claiming
    ``"committed"`` through a body the store just refused. ``final_marker`` is reused as-is: the
    first attempt's transaction rolls back cleanly on any exception (``BrokerStore._immediate``), so
    nothing was partially written and retrying is safe. Semantics identical to spine._settle.

    Returns ``(recorded, retry_exc)``: ``recorded`` is ``True`` iff some outcome is now durably
    persisted; ``retry_exc`` is ``None`` on a clean first try, else the first attempt's exception
    (whether or not the retry recovered) — the caller uses it to tell a clean settle from a degraded
    one and downgrade its own returned result so the caller never sees a false clean success."""
    try:
        effects.record_outcome(intent, status, result, final_marker)
        return True, None
    except Exception as exc:  # noqa: BLE001 — the outcome write itself must never crash past here
        safe_status = status if status == "failed" else "unconfirmed"
        safe_result = {"error": f"the original outcome could not be durably recorded ({exc}); "
                                "this is a degraded, sanitized record", "attempted_status": status}
        try:
            effects.record_outcome(intent, safe_status, safe_result, final_marker)
            return True, exc
        except Exception as exc2:  # noqa: BLE001 — genuinely unrecoverable; never raise past here
            return False, exc2


def run_reconcile(*, plan, marker, token, now_epoch, now_date, effects):
    """Execute a governed reconciliation. See module docstring for the safety architecture.

    Order is PREFLIGHT-ALL -> CONSENT -> EXECUTE, ``run_cascade``'s approved shape:

      1. **Preflight every node** (freshness on both the invoice and the payment, closed-books on
         both dates, the allocation ceiling against a fresh read) BEFORE consent and BEFORE any
         write. A gate failure records nothing and leaves the marker untouched — a clean no-op.
      2. **Consent** — reserve + CAS-claim the ONE marker, only once the whole graph preflights
         clean.
      3. **Execute** — ONE ``effects.reconcile(rows)`` call across the whole PINNED allocation set
         (``rows`` is built from ``plan.graph`` alone, never from any other argument this function
         accepts), then a READBACK of every node's real post-write outstanding — the call's result
         body is never trusted, only the readback is. The marker is settled once, deny-biased:
         released only on an answered refusal the readback proves changed nothing; committed
         (spent) in every other case — partial apply, any unconfirmed row, a no-answer/ambiguous
         exception, or a readback failure — because a released grant for a write that may already
         be in motion or partially landed could let one grant initiate a second act.
    """
    graph = list(plan.graph or [])
    total = len(graph)
    if total == 0:
        return {"ok": False, "stage": "plan", "confirmed": [], "unconfirmed": [], "total": 0,
                "reason": "reconcile plan has no allocations", "stopped_at": None}
    companies = {n["company"] for n in graph}
    if len(companies) > 1:
        reason = (f"reconcile plan spans {len(companies)} companies ({sorted(companies)}); "
                 "cross-company reconciliation must be an explicit refusal, not incidental")
        return {"ok": False, "stage": "plan", "confirmed": [], "unconfirmed": [], "total": total,
                "reason": reason, "stopped_at": None}

    # 1. PREFLIGHT every node before ANY consent or write; records nothing, marker untouched.
    # THE FIX (safety-critical): the over-allocation ceiling must be CUMULATIVE per invoice and
    # per payment across the WHOLE graph, never checked per-row-independent. Two rows against the
    # same invoice (or the same payment) can each individually pass a fresh-looking check yet,
    # summed, overshoot what the doc actually holds — every row's fresh read sees the same
    # pre-write state (a split settlement of one invoice across two payments is a real, common
    # pattern, not an edge case). live_outstanding(inv)/live_unallocated(pay) are the same value
    # across every row against the same doc (pre-write), so each unique doc's live value is read
    # ONCE and cached, and the RUNNING total for that doc — not any single row's own amount — is
    # what gets compared to the ceiling.
    invoice_live = {}     # (invoice_type, invoice_no) -> live_outstanding, read once per doc
    payment_live = {}     # (payment_type, payment_no) -> live_unallocated, read once per doc
    invoice_totals = {}   # (invoice_type, invoice_no) -> running cumulative allocated so far
    payment_totals = {}   # (payment_type, payment_no) -> running cumulative allocated so far
    for i, node in enumerate(graph):
        inv_dt, inv_no = node["invoice_type"], node["invoice_no"]
        pay_dt, pay_no = node["payment_type"], node["payment_no"]
        ok, reason = check_fresh(SimpleNamespace(doc_version=node["invoice_version"]),
                                 effects.current_version(inv_dt, inv_no))
        if not ok:
            return _preflight_refusal("fresh", total, inv_no, pay_no, i, reason)
        ok, reason = check_fresh(SimpleNamespace(doc_version=node["payment_version"]),
                                 effects.current_version(pay_dt, pay_no))
        if not ok:
            return _preflight_refusal("fresh", total, inv_no, pay_no, i, reason)
        # Wrong-books TOCTOU belt (mirrors _governed_write's F-C2 / _cascade_books_gate's C1): a
        # company swapped under a live plan via db_set(update_modified=False) leaves `modified`
        # untouched, so the freshness checks above cannot catch it — and every closed-books lock
        # below is read against node["company"], so a drifted invoice/payment would be checked
        # against the WRONG (planned) company's locks and could land against a different company's
        # closed books. Re-read each doc's LIVE company here and refuse any drift from the pinned
        # plan company; an unreadable company (raises to dispatch's structured deny) or a missing
        # one (None != planned) refuses too, deny-biased. Stage "plan": the violation is "this write
        # is not what was planned", exactly the sibling belts' framing.
        for dt, dn in ((inv_dt, inv_no), (pay_dt, pay_no)):
            live_company = effects.live_company(dt, dn)
            if live_company != node["company"]:
                reason = (f"{dt} {dn} now belongs to company {live_company!r}, not the plan's "
                         f"{node['company']!r} — wrong books (company changed under the plan)")
                return _preflight_refusal("plan", total, inv_no, pay_no, i, reason)
        # Closed-books on BOTH dates — the belt ERPNext bypasses for reconciliation entirely
        # (adv_adj=1 skips validate_frozen_account; the relink write never reaches save_entries,
        # so company period-freeze never applies): check the invoice's and the payment's posting
        # date independently, each against the SAME node fields already read above.
        for dt, date in ((inv_dt, node["invoice_date"]), (pay_dt, node["payment_date"])):
            ok, reason = check_red_line(date, now_date, effects.locks_for(node["company"], dt, date))
            if not ok:
                return _preflight_refusal("red_line", total, inv_no, pay_no, i, reason)

        inv_key, pay_key = (inv_dt, inv_no), (pay_dt, pay_no)
        if inv_key not in invoice_live:
            invoice_live[inv_key] = effects.live_outstanding(inv_dt, inv_no)
        if pay_key not in payment_live:
            payment_live[pay_key] = effects.live_unallocated(pay_dt, pay_no)
        # Per-row positivity + non-numeric-live checks (unchanged): the broker's OWN ceiling from
        # a FRESH read — never the agent's declared invoice_outstanding/payment_unallocated; those
        # fields are disclosure-only (the plan-time snapshot), not a source of truth for what the
        # invoice/payment actually holds now.
        ok, reason = check_allocation(node["allocated_amount"], invoice_live[inv_key],
                                      payment_live[pay_key])
        if not ok:
            return _preflight_refusal("allocation", total, inv_no, pay_no, i, reason)

        # THE cumulative ceiling itself: compare the RUNNING total for this doc, not this row's
        # own amount, against the doc's live value read above.
        invoice_totals[inv_key] = invoice_totals.get(inv_key, 0.0) + node["allocated_amount"]
        if invoice_totals[inv_key] > invoice_live[inv_key] + 0.005:
            reason = (f"cumulative allocated {invoice_totals[inv_key]} across this reconcile "
                     f"exceeds {inv_dt} {inv_no}'s live outstanding {invoice_live[inv_key]}")
            return _preflight_refusal("allocation", total, inv_no, pay_no, i, reason)
        payment_totals[pay_key] = payment_totals.get(pay_key, 0.0) + node["allocated_amount"]
        if payment_totals[pay_key] > payment_live[pay_key] + 0.005:
            reason = (f"cumulative allocated {payment_totals[pay_key]} across this reconcile "
                     f"exceeds {pay_dt} {pay_no}'s live unallocated {payment_live[pay_key]}")
            return _preflight_refusal("allocation", total, inv_no, pay_no, i, reason)

    # 2. CONSENT: reserve + CAS-claim the ONE marker now that the whole graph preflights clean.
    ok, reason, reserved = reserve(marker, token, plan.plan_id, now_epoch)
    if not ok:
        return {"ok": False, "stage": "consent", "confirmed": [], "unconfirmed": [], "total": total,
                "reason": reason, "stopped_at": None}
    if not effects.claim_marker(reserved):
        return {"ok": False, "stage": "consent", "confirmed": [], "unconfirmed": [], "total": total,
                "reason": "marker is already in use (concurrent reconcile)", "stopped_at": None}

    # 3. EXECUTE — one reconcile call across the whole pinned graph; readback-driven outcome.
    # `rows` is built from the PINNED plan.graph alone — this function takes no other
    # allocation-shaped argument, so there is no seam for a separate agent-supplied row list.
    # SEMANTIC keys (P7, bench-proven 2026-07-09): `payment_unallocated` (the payment's plan-time
    # available) and `invoice_outstanding` (the invoice's plan-time outstanding) travel with each
    # row; the GLUE (erpnext.py reconcile()) owns the translation to ERPNext's wire field names
    # (`amount`/`unreconciled_amount` are BOTH the payment's unallocated; the invoices[] pool
    # carries the outstanding). Pinned-not-live is deliberate: ERPNext's own
    # check_if_advance_entry_modified compares the payment echo to the LIVE DB, so a doc that
    # moved since consent (past check_fresh — e.g. db_set with update_modified=False) gets an
    # ANSWERED pre-write refusal from the bench itself — a free second TOCTOU belt that keeps the
    # act exactly what the human saw disclosed, instead of silently landing a drifted version.
    rows = [{"payment_type": n["payment_type"], "payment_no": n["payment_no"],
             "invoice_type": n["invoice_type"], "invoice_no": n["invoice_no"],
             "allocated_amount": n["allocated_amount"],
             "payment_unallocated": n["payment_unallocated"],
             "invoice_outstanding": n["invoice_outstanding"]} for n in graph]
    try:
        intent = effects.record_intent({"tool": "reconcile", "target": plan.target,
                                        "plan_id": plan.plan_id, "allocations": rows})
    except Exception as iexc:  # noqa: BLE001 — WG-2b (reconcile-path): post-claim, pre-wire.
        # effects.reconcile() is never reached, so nothing was sent to the bench — but there is no
        # intent receipt to link an outcome to (store.record_outcome requires one), so none can be
        # recorded. The marker is left exactly as claim_marker left it: claimed, dead, not
        # spendable. This is now a structured deny, never a raw exception past dispatch()'s catch —
        # identical posture to spine.governed_submit's intent wrap.
        return {"ok": False, "stage": "execute", "confirmed": [], "unconfirmed": [], "total": total,
                "reason": (f"reconcile: could not durably record the intent ({iexc}); nothing was "
                           "sent to the bench; the consent marker remains claimed (not spendable) "
                           "for manual review"),
                "stopped_at": None}
    exc = None
    answered = False
    try:
        effects.reconcile(rows)  # result body is NOT trusted — the readback below is the truth
    except Exception as e:  # noqa: BLE001 — the broker records ANY reconcile failure as an
        # outcome and never lets it crash the flow; the transport taxonomy (answered vs no-answer,
        # docs/plans/2026-07-07-transport-taxonomy.md) is resolved below via the duck-typed
        # `answered` attribute, exactly like cascade.py/spine.py.
        exc = e
        answered = getattr(e, "answered", False)

    # THE readback baseline: `expected` is each unique invoice's post-write outstanding — its FRESH
    # pre-write outstanding (`invoice_live`, read once at preflight: the same source of truth the
    # ceiling used, NOT the plan-time `invoice_outstanding` disclosure snapshot, which can be stale
    # if outstanding legitimately moved between plan and execute and would then false-negative a
    # correctly-bounded settle) minus the CUMULATIVE sum of every row's allocated_amount against it
    # (never a single row's own amount — a 400+400-against-1000 split must read as ONE confirmed
    # invoice, not two false negatives). Read back each unique invoice's outstanding ONCE, mark its
    # rows together. (The wire still carries the pinned snapshot by design — see the execute comment
    # above; only this internal success-baseline uses the fresh read.)
    invoice_alloc_sum = {}
    for node in graph:
        key = (node["invoice_type"], node["invoice_no"])
        invoice_alloc_sum[key] = invoice_alloc_sum.get(key, 0.0) + node["allocated_amount"]

    readback_failed = False
    readback_cache = {}  # (invoice_type, invoice_no) -> (new_outstanding, error_str_or_None)
    for key in invoice_alloc_sum:
        inv_type, inv_no = key
        try:
            readback_cache[key] = (effects.readback_outstanding(inv_type, inv_no), None)
        except Exception as rexc:  # noqa: BLE001 — a readback must never crash the flow; degrade
            # to an unconfirmed row (for every row sharing this invoice) carrying the readback
            # error instead of propagating.
            readback_failed = True
            readback_cache[key] = (None, str(rexc))

    confirmed = []
    unconfirmed = []
    for i, node in enumerate(graph):
        ids = _node_ids(node, i)
        key = (node["invoice_type"], node["invoice_no"])
        new_out, rexc_str = readback_cache[key]
        if rexc_str is not None:
            unconfirmed.append({**ids, "readback_error": rexc_str})
            continue
        expected = invoice_live[key] - invoice_alloc_sum[key]
        if isinstance(new_out, (int, float)) and not isinstance(new_out, bool) \
                and abs(new_out - expected) <= 0.005:
            confirmed.append({**ids, "outstanding": new_out})
        else:
            unconfirmed.append({**ids, "outstanding": new_out, "expected": expected})

    if not confirmed and answered and not readback_failed:
        # The reconcile cleanly refused (bench definitely saw and refused it) and the readback
        # PROVES nothing landed: spare the grant. Mirrors run_cascade's "release only on an
        # answered refusal with zero readback progress."
        final_marker = release(reserved)
        status = "failed"
        ok = False
        reason = f"reconcile refused ({exc}); the readback confirms nothing landed"
    elif confirmed and not unconfirmed and exc is None:
        # Every row landed, and the call itself never raised — a clean, unambiguous success.
        final_marker = commit(reserved)
        status = "committed"
        ok = True
        reason = None
    else:
        # Partial apply, any unconfirmed row, a no-answer/ambiguous exception (even one the
        # readback later shows fully landed), a readback failure, or an answered refusal that still
        # shows progress: an act is in motion or partially landed, so spending the grant is the
        # deny-biased choice — a released grant for a partially-applied write could initiate a
        # second act. The intent stays open (only "committed" finalizes, like the rest of the spine).
        #
        # DELIBERATE DIVERGENCE FROM spine.py (documented for the redteam): governed_submit
        # PROMOTES a no-answer-but-readback-confirmed submit to "committed" (confirmed_via
        # post_failure_readback), because a docstatus 0->1 readback is UNAMBIGUOUS proof the one
        # transition happened. Reconciliation is stricter on purpose: "committed" here requires BOTH
        # a clean return AND a readback-confirm (`exc is None` above), because (a) reconcile's
        # readback signal is weaker than a docstatus — "outstanding moved by the allocated amount"
        # does not verify the side-effect gain/loss or credit/debit-note JEs and can be confounded
        # by a concurrent change to the same invoice — and (b) an exception removes the clean-return
        # signal, leaving only that weaker readback. One weak signal is "unconfirmed" (keep the
        # intent open for the sweep), never a clean "committed".
        final_marker = commit(reserved)
        status = "unconfirmed"
        ok = False
        reason = ("reconcile did not confirm every allocation landed as expected; the consent "
                 "marker is spent; the intent stays open until reconciled"
                 + (f" ({exc})" if exc else ""))

    recorded, settle_exc = _settle(
        effects, intent, status,
        {"confirmed": confirmed, "unconfirmed": unconfirmed,
         "error": str(exc) if exc else None},
        final_marker)
    if settle_exc is not None:
        # The outcome write raised at least once (see _settle). Three cases the caller-visible
        # result must not paper over (mirrors spine.governed_submit's not-recorded reporting):
        if not recorded:
            # Both the original AND the sanitized retry failed: NO outcome receipt exists for this
            # reconcile. The intent is a bare orphan and the marker's settle rolled back with the
            # failed write (it stays reserved/dead, not the release/commit the code intended).
            ok = False
            reason = ((reason + "; " if reason else "")
                      + "the outcome could not be durably recorded at all (neither the original nor "
                        "the sanitized retry landed); the consent marker's real state is uncertain "
                        "— treat it as unspendable and inspect manually")
        elif status != "failed":
            # A would-be commit/unconfirmed whose durable record degraded to 'unconfirmed' — must
            # not still claim a clean success. ("failed" is a proven-clean refusal, correctly
            # preserved by _settle on the retry; nothing to downgrade or flag.)
            ok = False
            reason = ((reason + "; " if reason else "")
                      + f"the outcome could not be durably recorded as {status!r} and was degraded "
                        "to 'unconfirmed'")
    return {"ok": ok, "stage": "done" if ok else "execute", "confirmed": confirmed,
            "unconfirmed": unconfirmed, "total": total, "reason": reason, "stopped_at": None}
