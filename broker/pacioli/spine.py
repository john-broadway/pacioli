# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — SPINE: the governed-submit orchestration (pure core).

Ties the pillars into one ordered flow: ``PLAN(fresh) → closed-books → CONSENT → claim → execute
→ PROVE``. The security ordering is the whole point and is enforced here:

  1. **Gates first, deny-biased.** TOCTOU freshness, the closed-books check, and the marker are all
     checked *before* anything happens. Any failure returns immediately — nothing recorded, nothing
     executed. Two clocks are threaded separately, by contract: ``now_date`` (ISO) for the date-range
     closed-books check, ``now_epoch`` (float) for the marker TTL — they are different types and must not be
     conflated.
  2. **Atomic claim before the irreversible.** On passing gates the marker is *reserved* and the glue
     CAS-claims ``live → reserved`` (``effects.claim_marker``) BEFORE ``execute``. A concurrent submit
     sharing the marker loses the CAS and is refused — closing the double-execute race a
     verify-now/consume-later design leaves open.
  3. **Durable intent before execute.** A PROVE *intent* is recorded (durably) before ``execute``, so a
     crash between execute and outcome leaves an orphan intent to reconcile — never a silent posting.
  4. **Execute, then settle — and the marker is only released on an ANSWERED refusal.**
     Success → the marker is committed (single-use spent) and a ``committed`` outcome recorded,
     together (glue = one transaction).

     Failure splits, and the split is the deny-biased rule this file exists to enforce:

     * **The bench ANSWERED with a refusal** (``exc.answered`` is truthy — a converted, attributable
       error) → nothing was posted, so the marker is *released* back to ``live`` and a ``failed``
       outcome is recorded. A refused submit must not burn the human's grant.
     * **NO answer** (a raw exception, a connection failure, an ambiguous proxy response) → the
       mutating call may already be **in motion server-side**, so the marker is **SPENT**, not
       released, and the outcome resolves through a governed readback of the document's real
       ``docstatus`` (:func:`_resolve_no_answer`). Releasing here would let one human grant initiate
       a second act, which is the exact inversion consent exists to prevent.

     Because only a ``committed`` outcome finalizes an intent, an unresolved intent stays an orphan
     for the reconciliation sweep — a timeout that may actually have landed is never treated as a
     clean failure.

     ⚠️ **This paragraph said "Failure → the marker is *released*", flat, until 2026-08-11.** That
     was true before 0.10.4 and the flip is deliberate and documented — the comment at
     :func:`submit_governed`'s own exception branch says *"this used to release on the never-verified
     assumption that an exception meant no progress; it no longer does"*, and ``broker/README.md``
     records it. Only this header was never updated, so the **governance spine's stated invariant
     was the opposite of the rule it implements**, in the paragraph a reader trusts most. Found by
     an independent review of the docstring-claim pass that had just declared this module clean.
     ``test_spine_docstring_claims.py`` now holds it.

Side effects are injected via ``effects`` — **five** members, all called by this core:
``claim_marker(reserved) -> bool``, ``record_intent(body) -> intent``, ``execute() -> result``,
``record_outcome(intent, status, result, final_marker)``, and ``readback() -> result`` (used only
on the no-answer path above, to ask the bench what actually happened). So this core is pure and
unit-testable; the glue owns the store, the clocks, the seal key, the atomic CAS, and the
single-transaction settle. ``readback`` was omitted from this list until 2026-08-11, so anyone
implementing the protocol as documented got an ``AttributeError`` on the deny-biased path that
matters most.

**``record_outcome`` MUST BE ALL-OR-NOTHING**, and this contract did not say so until 2026-08-11.
The refusal messages now assert a persisted fact — that after a double failure the marker row
still reads ``reserved`` — and that is only true if a failed ``record_outcome`` leaves nothing
behind. Both shipped implementations satisfy it (``store.BrokerStore.record_outcome`` is one
``BEGIN IMMEDIATE`` with rollback on any error, and the marker update is state-guarded
``AND state='reserved'``), and ``_settle``'s retry already depends on it by name. But a
conforming-looking implementation without that property would make an operator-facing sentence
false with no test noticing, because every test here uses an atomic one. Raised as a SUSPECTED
design fragility by an independent review; closed by stating the requirement rather than by
adding a guard the pure core has no way to enforce.

Honest residuals (SPEC §5): a crash after ``claim`` but before ``execute``/intent leaves the marker
stuck ``reserved`` (dead → fail-closed; a human re-mints — safe, not silent). A crash after a
successful ``execute`` but before the outcome persists leaves an orphan intent (caught by the sweep)
and the marker un-consumed but claimed (dead). Neither produces a posting without a trail.

WG-2b (2026-07-10 readiness audit): both ``effects.record_intent`` and ``effects.record_outcome``
are themselves fallible (the store's ``prove.append`` refuses a non-finite float or any other
non-JSON-native value — the concrete NaN trigger closed at ``reconcile.check_allocation``, WG-2a —
and any other unexpected local bug). Left unguarded, that exception used to propagate straight past
``dispatch()``'s narrow ``except (ErpnextError, RegistryError)`` catch as an unstructured crash,
while the marker sat claimed with no outcome recorded. Both calls are now guarded: a
``record_intent`` failure (post-claim, pre-wire — nothing was ever sent) returns a structured
result and leaves the marker exactly as ``claim_marker`` left it (claimed, dead, not spendable) —
no outcome can be linked to an intent that was never durably recorded, the same honest residual
named above. A ``record_outcome`` failure (post-wire — the intent already exists) retries once with
a sanitized, deny-biased body (see :func:`_settle`): a pre-wire "failed" stays "failed"; every
other status downgrades to "unconfirmed" on the retry, NEVER to "failed" and never silently kept as
"committed" through a write that itself just failed. If even that retry fails, the caller still gets
a structured result, never a raw exception.
"""
from __future__ import annotations

from dataclasses import dataclass

from pacioli.consent import commit, release, reserve
from pacioli.plan import check_fresh, check_red_line

# The one true statement about the marker whenever ``_settle`` returns ``recorded=False``, shared
# by every such return so the five cannot drift apart again.
#
# ``commit()``/``release()`` are PURE — they return a new dataclass; only ``record_outcome``
# persists one. So when both the original write and the sanitized retry fail, no marker write
# happened at all and the row is left exactly as ``store.claim_marker``'s CAS left it before
# execute: ``reserved``. Not ``live``, and NOT ``consumed``.
#
# ⚠️ Two of these returns said "the consent marker is spent" until 2026-08-11 — a durable fact the
# immediately-preceding failed write means did not happen, asserted at the exact moment the
# operator most needs the truth. Verified against a real ``BrokerStore``: the row reads
# ``reserved`` while the message claimed spent. "Spent" tells an operator the ceremony completed
# and there is nothing to clean up; the reality is a marker stranded in ``reserved``, which no
# sweep collects (``prove.orphans`` sweeps open INTENTS, not markers) and which reads as neither
# a live grant nor a spent one. Pinned by ``test_spine.TestDoubleFailureMessagesTellTheTruth`` and
# by ``TestUnsettledMarkerIsReallyReservedInTheStore``, which reads the row rather than the fake.
_UNSETTLED = ("the marker remains CLAIMED — state 'reserved': not spendable, and NOT recorded as "
              "spent")

# The other half of the same truth, and it must appear wherever `_UNSETTLED` does. `record_intent`
# succeeds before every one of these returns, so an intent receipt EXISTS with no committed
# outcome — which is exactly what `prove.orphans` collects. Naming the sweep is the only thing in
# these messages that tells the operator where the act will resurface.
#
# ⚠️ An independent review caught this missing from the answered-refusal return, where it is not
# merely true but MOST explainable ("the bench refused; nothing posted"). It also caught the
# pointer being pinned at only one of its four sites, so three could drift silently — hence one
# constant, and `test_spine.TestEveryDoubleFailureCarriesBothClauses`, which drives all five paths
# rather than sampling one.
_OPEN_INTENT = "the intent receipt stays open (sweep prove_orphans)"


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of a governed submit.

    :param ok: did the posting complete and get proven?
    :param reason: denial/failure reason (``None`` on success).
    :param stage: where it ended — ``"fresh"`` | ``"red_line"`` | ``"consent"`` | ``"execute"`` |
        ``"done"`` | ``"unconfirmed"``.

        ⚠️ ``"unconfirmed"`` was missing from this list until 2026-08-11, and it is the one a
        caller can least afford to miss: it means **the marker is spent and the document's real
        state is unknown — reconcile before treating this as final.** It is also the most
        frequently constructed stage in the module. A caller switching on ``stage`` per the old
        list would have fallen through on exactly the outcome that needs a human.
        ``test_spine_docstring_claims.py`` pins this list against the literals the module actually
        constructs.
    :param result: the execute result when there is one. Present on success, and ALSO on the
        no-answer failures that carry what the readback saw (``"unconfirmed"``) — it is not a
        success-only field, which is what this line claimed until 2026-08-11.
    """

    ok: bool
    reason: str | None
    stage: str
    result: dict | None = None


def governed_submit(*, plan, marker, token, current_doc_version, now_epoch, now_date, locks, effects,
                    op="submit", transition="0->1"):
    """Run the governed irreversible-op flow. See module docstring for the ordering guarantees.

    ``op``/``transition`` label the intent receipt (``"submit"``/``"0->1"``, ``"cancel"``/``"1->2"``)
    — the same ordered spine governs both directions of the duality. Cross-op plan/marker binding is
    the caller's gate (``plan.check_op``), enforced BEFORE this flow runs."""
    ok, reason = check_fresh(plan, current_doc_version)
    if not ok:
        return SubmitResult(False, reason, "fresh")

    ok, reason = check_red_line(plan.posting_date, now_date, locks)
    if not ok:
        return SubmitResult(False, reason, "red_line")

    ok, reason, reserved = reserve(marker, token, plan.plan_id, now_epoch)
    if not ok:
        return SubmitResult(False, reason, "consent")

    # Atomic claim (CAS live->reserved) BEFORE execute — a concurrent submit loses the race here.
    if not effects.claim_marker(reserved):
        return SubmitResult(False, "marker is already in use (concurrent submit)", "consent")

    # Record intent DURABLY before the irreversible submit. ``doctype`` makes the intent receipt
    # self-describing (an auditor reads the doctype off the receipt itself, not only via a join to
    # the plan store, which may be pruned); it is read straight off the plan, exactly like the
    # other fields here — this core stays doctype-agnostic, it just records what the plan carries.
    try:
        intent = effects.record_intent(
            {"tool": op, "target": plan.target, "plan_id": plan.plan_id,
             "docname": plan.docname, "doctype": getattr(plan, "doctype", "Sales Invoice"),
             "doc_version": plan.doc_version, "transition": transition}
        )
    except Exception as exc:  # noqa: BLE001 — WG-2b: post-claim, pre-wire. execute() is never
        # reached, so nothing was sent to the bench — but there is no intent receipt to link an
        # outcome to (store.record_outcome requires one), so none can be recorded. The marker is
        # left exactly as claim_marker left it: claimed, dead, not spendable — the same honest
        # residual this module's docstring already names for a claim -> intent crash. The fix is
        # that this is now a structured deny, never a raw exception past dispatch()'s narrow catch.
        return SubmitResult(
            False,
            f"{op}: could not durably record the intent ({exc}); nothing was sent to the bench; "
            "the consent marker remains claimed (not spendable) for manual review",
            "execute",
        )

    try:
        result = effects.execute()
    except Exception as exc:  # noqa: BLE001 — the broker records ANY execute failure as an outcome
        # Transport taxonomy (docs/plans/2026-07-07-transport-taxonomy.md): an ANSWERED refusal
        # (the bench definitely saw and refused the call — `ErpnextError.answered`, duck-typed via
        # getattr so this core never imports the glue's exception type) keeps today's
        # byte-identical behavior: spare the grant (release -> live), record "failed". Everything
        # else — a raw/unconverted exception, a connection failure, a proxy-shaped ambiguous
        # response — is "no answer": the mutating call may already be IN MOTION server-side, so
        # releasing would let one grant initiate a second act. THE FLIP (deliberate, deny-biased):
        # this used to release on the never-verified assumption that an exception meant no
        # progress; it no longer does.
        if getattr(exc, "answered", False):
            recorded, rexc = _settle(effects, intent, "failed", {"error": str(exc)},
                                     release(reserved))
            if not recorded:
                return SubmitResult(
                    False,
                    f"{op} failed ({exc}) and the outcome could not be durably recorded either "
                    f"({rexc}); {_UNSETTLED}, and the grant was never returned to the human — "
                    f"inspect manually; {_OPEN_INTENT}",
                    "execute",
                )
            return SubmitResult(False, f"{op} failed: {exc}", "execute")
        return _resolve_no_answer(effects, intent, reserved, exc, op, transition)

    # Execute returning is NOT proof the transition happened — the response must CONFIRM it.
    # ERPNext queues some writes to a background worker (``JournalEntry.submit``/``.cancel``
    # override the base method and queue past 100 accounts rows), and frappe then answers 200 with
    # the doc still at its pre-transition docstatus. The book must never record ``committed`` for
    # a write the response did not show (envelope E1, found live 2026-07-07). The grant is still
    # SPENT: consent initiated an irreversible act now in motion server-side; releasing the marker
    # would let one grant initiate a second act. The ``unconfirmed`` outcome does not finalize the
    # intent (only ``committed`` does — ``prove.orphans``), so the write stays in the reconcile
    # sweep until checked against the real docstatus.
    expected = _transition_end(transition)
    got = result.get("docstatus") if isinstance(result, dict) else None
    if expected is not None and got != expected:
        recorded, rexc = _settle(effects, intent, "unconfirmed", result, commit(reserved))
        if not recorded:
            return SubmitResult(
                False,
                f"{op} was accepted but NOT confirmed (docstatus {got}, expected {expected}), "
                f"and the outcome could not be durably recorded either ({rexc}); {_UNSETTLED} — "
                f"reconcile against the document's real docstatus manually; {_OPEN_INTENT}",
                "execute",
            )
        return SubmitResult(
            False,
            f"{op} was accepted but NOT confirmed: the response shows docstatus {got}, expected "
            f"{expected}. ERPNext queues some writes to a background worker (e.g. a >100-row "
            f"Journal Entry submit/cancel) — the write may still land after this reply. The "
            f"consent marker is spent; the intent receipt stays open until reconciled against "
            f"the document's real docstatus (fetch the document, or sweep prove_orphans).",
            "unconfirmed",
        )

    recorded, rexc = _settle(effects, intent, "committed", result, commit(reserved))
    if not recorded:
        return SubmitResult(
            False,
            f"{op} succeeded at the bench (docstatus confirmed) but the outcome could not be "
            f"durably recorded ({rexc}); {_UNSETTLED} — verify the document's real state before "
            f"assuming this is final; {_OPEN_INTENT}",
            "execute",
        )
    if rexc is not None:
        # The FIRST attempt to record "committed" failed and had to degrade to a sanitized
        # "unconfirmed" retry (see _settle) — the bench genuinely confirmed the transition, but
        # the book itself could not durably say "committed" on the first write. Never report a
        # clean "done" through a ledger that had to degrade: the caller must reconcile.
        return SubmitResult(
            False,
            f"{op} succeeded at the bench (docstatus confirmed) but the outcome record itself "
            f"hit an unexpected error and was recorded degraded as 'unconfirmed' ({rexc}); the "
            "consent marker is spent; reconcile against the document's real docstatus before "
            "treating this as final",
            "unconfirmed",
            result,
        )
    return SubmitResult(True, None, "done", result)


def _settle(effects, intent, status, result, final_marker):
    """Record the outcome; if the store itself refuses the write (e.g. a non-finite float or any
    other unexpected value that slipped past every upstream check — belt-and-suspenders for
    ``prove.append``'s JSON-native guard), degrade to a minimal, always-safe body rather than let
    the raw exception crash past ``dispatch()``'s structured deny (WG-2b: the residual this
    closes — a post-claim exception stranding the marker with an UNRECORDED outcome and an
    unstructured crash). A ``"failed"`` status (pre-wire — nothing was ever sent to the bench) is
    preserved on retry; every other status is POST-WIRE and is recorded on the degraded retry as
    ``"unconfirmed"`` and ONLY ``"unconfirmed"`` — deny-biased, per the transport taxonomy this
    module already carries: never let a recording failure make a possibly-in-motion act look like
    a clean "failed", and never claim "committed" through a body the store itself just refused.
    ``final_marker`` is reused as-is on the retry: the first attempt's transaction rolls back
    cleanly on any exception (``BrokerStore._immediate``), so nothing was partially written and
    retrying is safe.

    Returns ``(recorded, retry_exc)``: ``recorded`` is ``True`` iff some outcome (the original or
    the degraded retry) is now durably persisted; ``retry_exc`` is ``None`` on a clean first-try
    success, else the exception the first attempt raised (whether or not the retry then
    recovered) — the caller uses it to tell a clean success from a degraded one."""
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


def _resolve_no_answer(effects, intent, reserved, exc, op, transition):
    """The "no answer"/ambiguous half of the transport taxonomy: the mutating call raised but the
    exception carries no proof the bench refused it (see ``governed_submit``'s docstring). The
    marker is ALWAYS spent here (never released — a released grant for an act possibly in flight is
    the exact inversion this rule exists to prevent) and the outcome is resolved by a governed
    readback of the document's real docstatus, the same discipline ``tools.py``'s existing
    cascade-cancel readback already applies (``_Effects.cancel`` in ``cascade_cancel``; cited by
    NAME rather than by line, because this citation read ``tools.py:1744-1779`` until 2026-08-11
    and had drifted ~8,000 lines onto unrelated prose): the readback
    itself must never be allowed to crash this flow, so any exception IT raises degrades to a
    ``readback_error`` rather than propagating."""
    committed_marker = commit(reserved)

    def _settle_or_deny(status, result, detail=""):
        # WG-2b: every settle in this function is already deny-biased ("unconfirmed"/"committed",
        # never "failed" — nothing here is a known-clean refusal); _settle's own downgrade rule
        # leaves that untouched. Only a genuinely unrecoverable double failure needs a distinct
        # message: the marker is spent either way (committed_marker, computed above), so the only
        # thing degrading is whether the ledger itself could say so.
        #
        # ``detail`` exists because this one return is shared by BOTH no-answer sub-branches, and
        # without it they rendered BYTE-IDENTICALLY: a readback that RAISED and a readback that
        # ANSWERED "the transition did not happen" produced the same sentence, and that sentence
        # says "with no answer from the bench". In the second case the bench DID answer — it said
        # no — and an operator told otherwise at 2am will go looking for a posting that is not
        # there. Both siblings that are not double failures carry their detail (``{rexc}`` and
        # ``docstatus {got!r}``); this one dropped it, and because the ledger write also failed,
        # the ``readback_error``/``docstatus`` in ``result`` is not persisted either — so the
        # detail was lost everywhere, not merely from the message. Found by an independent review.
        recorded, oexc = _settle(effects, intent, status, result, committed_marker)
        if not recorded:
            return SubmitResult(
                False,
                f"{op} raised ({exc}) with no answer from the bench, and the outcome could not "
                f"be durably recorded either ({oexc}); {detail}{_UNSETTLED} — inspect manually; "
                f"{_OPEN_INTENT}",
                "execute",
            )
        return None  # recorded (possibly degraded) — caller builds its own SubmitResult

    try:
        got = effects.readback()
    except Exception as rexc:  # noqa: BLE001 — the readback must never raise past this point
        result = {"error": str(exc), "readback_error": str(rexc)}
        deny = _settle_or_deny("unconfirmed", result,
                               detail=f"the confirmatory readback ALSO failed ({rexc}), so the "
                                      f"document's real state is unknown; ")
        if deny is not None:
            return deny
        return SubmitResult(
            False,
            f"{op} raised ({exc}) with no answer from the bench, and the confirmatory readback "
            f"itself failed ({rexc}); the document's real state is unknown. The consent marker is "
            "spent; the intent receipt stays open until reconciled against the document's real "
            "docstatus.",
            "unconfirmed",
        )
    expected = _transition_end(transition)
    if expected is not None and got == expected:
        result = {"error": str(exc), "docstatus": got, "confirmed_via": "post_failure_readback"}
        recorded, oexc = _settle(effects, intent, "committed", result, committed_marker)
        if not recorded:
            return SubmitResult(
                False,
                f"{op} raised ({exc}) with no answer from the bench, but a readback confirms it "
                f"DID complete — yet the outcome could not be durably recorded ({oexc}); "
                f"{_UNSETTLED} — inspect manually; {_OPEN_INTENT}",
                "execute",
            )
        if oexc is not None:
            # The bench genuinely confirmed the transition via readback, but the FIRST attempt to
            # record "committed" failed and had to degrade to "unconfirmed" — never report a
            # clean "done" through a ledger that had to degrade.
            return SubmitResult(
                False,
                f"{op} raised ({exc}) with no answer from the bench, and a readback confirms it "
                f"DID complete, but the outcome record itself hit an unexpected error and was "
                f"recorded degraded as 'unconfirmed' ({oexc}); the consent marker is spent; "
                "reconcile before treating this as final",
                "unconfirmed",
                result,
            )
        return SubmitResult(True, None, "done", result)
    result = {"error": str(exc), "docstatus": got}
    deny = _settle_or_deny("unconfirmed", result,
                           detail=f"a readback DID answer and shows the document at docstatus "
                                  f"{got!r}, not the expected {expected!r}; ")
    if deny is not None:
        return deny
    return SubmitResult(
        False,
        f"{op} raised ({exc}) with no answer from the bench; a readback shows the document at "
        f"docstatus {got!r}, not confirmed at the expected end state. The consent marker is spent; "
        "the intent receipt stays open until reconciled against the document's real docstatus.",
        "unconfirmed",
    )


def _transition_end(transition):
    """The docstatus a transition label claims to end at (``"0->1"`` → ``1``), or ``None`` if the
    label doesn't carry one. Unparseable = ``None`` = no confirmation check (never a crash)."""
    try:
        return int(str(transition).split("->", 1)[1].strip()[:1])
    except (IndexError, ValueError):
        return None
