# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — THE CLOSE (Half 3: response-to-gap). Pure core, no I/O, no clock, no seal, no store.

Halves 1 and 2 DETECT: ``close.py`` attests the period from Pacioli's own receipt store (the
Statement), ``reconciliation.py`` joins it against ERPNext's real General Ledger (the Reconciliation).
Detection without response is three-quarters of a loop. This half is the response: it takes the two
dicts those halves already return, applies the operator's per-target **posture** and response
**envelope**, and decides — per finding and in aggregate — what reaction fires (record / alert /
attestation-gate / CONTAIN). The store/CLI/seal layer ACTS on that decision; this core only decides,
so the whole policy layer is unit-testable with no bench.

**The model, load-bearing (docs/plans/2026-07-11-close-half3-response-to-gap.md):**

  * *Accounting, not police — preserved.* This core renders NO verdict of its own. ``ungoverned``
    movement (a desk posting, another integration) is a normal, legitimate part of real books; it is
    **recorded, never accused**, and elevated to an alert ONLY under a ``sole_door`` posture the
    operator EXPLICITLY declared. Half 2 partitions; Half 3 is the only place a posture is applied,
    and only because the operator opted in.
  * *The envelope is a FLOOR, not an override.* Each finding class has a deny-biased minimum response
    (an orphan or a blind read always at least ALERTs). The operator can ESCALATE a class (e.g. orphan
    -> CONTAIN) but can never SILENCE one below its floor.
  * *Deny-biased everywhere.* An unparseable posture -> the most conservative live posture
    (``sole_door``, surface ungoverned). An unparseable envelope level -> the floor, never the loosest.
    A blind reconciliation (``complete`` not exactly ``True``) -> a finding, never an all-clear.
  * *Autonomy envelope, not per-gap prompting.* The response is what the operator authorised in advance
    for each finding class; the machine acts within that envelope, it does not ask on every gap.

**Invariant 1, REVISED 2026-07-15 (docs/plans/2026-07-15-chain-broken-finding.md; do not read the
original wording below as still-current — kept for the record, not silently dropped):**

  * *Original (through 0.21.0):* "Since no floor is CONTAIN, CONTAIN can only ever appear because the
    operator opted in — 'never a default' is a property of the data."
  * *Revised (0.22.0), why it changed:* ``chain_broken`` (below) is now the SOLE finding class whose
    floor is CONTAIN by default, no operator opt-in required. The distinction that makes this a
    principled narrowing, not a break of the rule — restated 2026-07-15 to the real categorical line,
    not a two-way "own vs external" axis (that axis was wrong: ``orphan``/``unconfirmed`` are ALSO
    Pacioli's own writes and stay ``alert``, so "own" alone cannot be what earns CONTAIN):
    ``chain_broken`` is CONTAIN because the attestation apparatus is **provably broken** — the HMAC
    chain cannot verify itself, so you can no longer trust ANY record in it. Every other class,
    including Pacioli's own ``orphan``/``unconfirmed``, stays at ``alert`` or below because the ledger
    **verifiably records a known uncertainty** — the apparatus works and is honestly confessing an
    open item, which is a fundamentally different thing from the apparatus itself being unable to
    vouch for anything. ``ungoverned``/``blind_read`` are additionally about another party's movement
    or an external read going dark, but that is not the load-bearing axis — apparatus-broken vs.
    apparatus-honestly-confesses is. **Restated:** CONTAIN is never a default for a working ledger
    honestly recording an open item — every such class stays at alert or below unless the operator
    explicitly escalates it. ``chain_broken`` is the one and only exception, and no other class may
    ever gain a CONTAIN floor.
  * *2026-07-15, redteam fix wave (D1 — John ruled, drop the anchor branch):* ``chain_broken`` no
    longer inspects ``anchor_matches`` at all. The receipt-anchor branch was dropped because
    ``anchor_matches`` (``close.py``'s ``build_statement``) is a NAIVE ``head == anchor`` equality
    that false-positives on a legitimately-grown chain fed a stale ``--expected-head`` — it would
    auto-seal a perfectly untampered ledger. Receipt-rollback detection is COUNT-AWARE and already
    lives in ``pacioli anchor check`` / ``seal-status --anchor`` (the 0.21.0 seal-anchor tooling),
    which correctly reads a grown chain as clean. ``chain_broken`` is now provable OWN-chain-integrity
    failure (``verified is False``) and nothing else.

**Composition, not coupling:** CONTAIN here is a *decision* the broker acts on locally (a fail-closed
seal, in the store/CLI layer). This core never writes into ``guard`` — floor-level credential
revocation stays the operator's hand. This module reads no new ERPNext surface and needs no new grant.
"""
from __future__ import annotations

# Response levels, ordered. The aggregate response is the max level across all findings.
_LEVELS = {"record": 0, "alert": 1, "attestation_gate": 2, "contain": 3}
_LEVEL_NAME = {v: k for k, v in _LEVELS.items()}

# The conservative default matrix — each finding class's deny-biased FLOOR. The operator's envelope
# can raise a class above its floor but never below it. Through 0.21.0, no floor was CONTAIN(3):
# CONTAIN was opt-in only. As of 0.22.0, `chain_broken` (below) is the SOLE exception — its own
# comment explains why this is a principled narrowing, not a loosening, of that rule.
_FLOOR = {
    "orphan": 1,             # alert — Pacioli's own unbalanced entry; the confession
    "unconfirmed": 1,        # alert — a write that got no answer and MAY have posted (suspense item)
    "second_generation": 1,  # alert — rows rewritten off-seat under a voucher Pacioli governs
    "blind_read": 1,         # alert — the reconciliation refused; cannot even attest
    "adverse_posture": 0,    # record — a standing condition (mutable/scrubbable books), not an event
    "ungoverned": 0,         # record under mixed_door; raised to alert under sole_door (see below)
    "chain_broken": 3,       # contain — the SOLE default-contain: the attestation apparatus is
                             # PROVABLY BROKEN (`verified is False` — the HMAC chain cannot verify
                             # itself, so no record in it can be trusted). NOT the receipt-anchor
                             # branch (dropped 2026-07-15, D1) — that naive head==anchor equality
                             # false-sealed a legitimately-grown chain; rollback detection is
                             # count-aware and lives in `anchor check`/`seal-status --anchor`
                             # instead. See invariant 1 (revised) for the apparatus-broken-vs-
                             # records-a-known-uncertainty axis this floor is drawn on.
}

_POSTURES = ("mixed_door", "sole_door")


def _parse_posture(v):
    """Resolve the operator's per-target posture. ``None`` (not configured) -> the documented default
    ``mixed_door`` (accounting: real books have other legitimate doors; do not cry wolf). A configured
    but UNPARSEABLE value -> deny-biased ``sole_door`` (a broken config must never silently hide
    ungoverned movement) plus a caller-visible flag. Returns ``(posture, flag_or_None)``."""
    if v is None:
        return "mixed_door", None
    if isinstance(v, str) and v in _POSTURES:
        return v, None
    return ("sole_door",
            f"posture {v!r} is not one of {_POSTURES} — deny-biased to 'sole_door' "
            "(a broken posture config never silently hides ungoverned movement)")


def _envelope_level(envelope, cls, floor):
    """The resolved response level for a finding class: the operator's envelope value if it parses to a
    known level, floored at ``floor`` (deny-biased — escalate above the floor, never silence below it).
    An unparseable or absent entry -> the floor."""
    if isinstance(envelope, dict):
        raw = envelope.get(cls)
        if isinstance(raw, str) and raw in _LEVELS:
            return max(floor, _LEVELS[raw])
    return floor


def build_response(statement, reconciliation, *, target=None, posture=None, envelope=None):
    """Decide the period's response from the Statement and Reconciliation dicts. Returns a JSON-native
    dict. ``posture`` is the operator's per-target posture string (or ``None``); ``envelope`` the
    operator's ``{finding_class: level_name}`` matrix (or ``None`` for all-floors).

    **Chain integrity — SHIPPED 2026-07-15, broker 0.22.0, narrowed same-day by the redteam fix wave
    (D1 — John ruled: drop the anchor branch; closes the disclosed gap the 2026-07-14 correctness
    redteam found; was previously described here as a staged next increment — that note is now
    stale, replaced by this one):** this DOES inspect ``statement["chain"]["verified"]``. A verify
    failure (``verified is False`` — the keyed PROVE-chain verify actively failed, tampered/corrupt
    receipts) emits a ``chain_broken`` finding at floor ``contain`` — see the module docstring's
    "Invariant 1, REVISED" for why this is the sole default-CONTAIN class. **This no longer inspects
    ``anchor_matches`` at all** (dropped 2026-07-15): ``close.py``'s ``anchor_matches`` is a NAIVE
    ``head == anchor`` equality that false-positives on a legitimately-grown chain fed a stale
    ``--expected-head``, which would auto-seal an untampered ledger; the ``anchor_matches`` field
    still feeds ``build_statement``'s own ``balanced`` check and the human render (unchanged there),
    it just no longer drives this default-CONTAIN floor. Receipt-rollback detection is COUNT-AWARE
    and lives in ``pacioli anchor check`` / ``seal-status --anchor`` (the 0.21.0 seal-anchor
    tooling), which correctly reads a grown chain as clean. The false-seal guard: an absent
    ``chain`` dict, a clean verify (``verified is True``), or ``verified is None`` (verify not run)
    all emit NOTHING — an ordinary clean close never seals itself. This function only DECIDES; the
    actual seal is still performed by ``cmd_close``'s pre-existing 0.20.0 auto-seal path when
    ``seal_required`` comes back ``True`` — this change makes that path reachable via a broken
    chain, it does not change how the seal itself is performed."""
    flags = []
    posture_word, posture_flag = _parse_posture(posture)
    if posture_flag:
        flags.append(posture_flag)

    st = statement if isinstance(statement, dict) else None
    rc = reconciliation if isinstance(reconciliation, dict) else None

    # target: explicit wins; else agree across the two inputs; a disagreement is flagged (they should
    # attest to the same target) and the statement's is taken.
    st_target = st.get("target") if st else None
    rc_target = rc.get("target") if rc else None
    if target is None:
        target = st_target if st_target is not None else rc_target
    if st_target is not None and rc_target is not None and st_target != rc_target:
        flags.append(f"target mismatch: statement {st_target!r} vs reconciliation {rc_target!r}")

    findings = []

    def _emit(cls, floor, detail):
        level = _envelope_level(envelope, cls, floor)
        findings.append({"class": cls, "response": _LEVEL_NAME[level],
                         "response_level": level, "detail": detail})

    # --- Statement findings ---------------------------------------------------------------------
    if st is not None:
        acts = st.get("acts")
        if isinstance(acts, list):
            for a in acts:
                if not isinstance(a, dict):
                    flags.append("could not classify a statement act (malformed, not a dict) — skipped")
                    continue
                cls = a.get("class")
                detail = {k: a.get(k) for k in ("seq", "tool", "doctype", "docname", "ts")}
                if cls == "orphan":
                    _emit("orphan", _FLOOR["orphan"], detail)
                elif cls == "recorded_open" and a.get("outcome_status") == "unconfirmed":
                    # a `failed` recorded-open is a KNOWN non-event (close.py) and is deliberately NOT a
                    # finding; only `unconfirmed` (no answer — may have posted) is a suspense item.
                    _emit("unconfirmed", _FLOOR["unconfirmed"], detail)
        elif acts is not None:
            flags.append("statement 'acts' is not a list — no acts classified (deny-biased)")

        # Chain integrity: fires ONLY on a provable failure of Pacioli's OWN receipt chain to
        # verify itself (D1, 2026-07-15: the anchor_matches branch was dropped — see the module
        # docstring's "Invariant 1, REVISED" for why; anchor_matches keeps feeding
        # build_statement's `balanced` and the human render, it just no longer drives this floor).
        # Absent `chain` is no signal (emit nothing). `is False` is an identity check, never
        # falsiness — `None` (verify not run) MUST NOT fire; that is the guard that keeps an
        # ordinary pinless clean close from sealing itself. A `chain` present but not a dict is
        # flagged (deny-biased, mirrors the `acts` check above) — chain integrity could not be
        # evaluated, but nothing is emitted on a malformed shape.
        chain = st.get("chain")
        if isinstance(chain, dict):
            if chain.get("verified") is False:
                verify_reason = chain.get("verify_reason")
                detail = {"reason": f"the receipt chain failed to verify ({verify_reason})"
                          if verify_reason else "the receipt chain failed to verify"}
                _emit("chain_broken", _FLOOR["chain_broken"], detail)
        elif chain is not None:
            flags.append("statement 'chain' is not a dict — chain integrity not evaluated "
                         "(deny-biased)")

    # --- Reconciliation findings ----------------------------------------------------------------
    if rc is not None:
        # Blind read: `complete` must be EXACTLY True to clear. False/None/absent -> a finding.
        if rc.get("complete") is not True:
            _emit("blind_read", _FLOOR["blind_read"],
                  {"reason": "reconciliation did not complete (posture unreadable or internally "
                             "inconsistent)", "flags": rc.get("flags")})

        # Second generation: rows rewritten off-seat under a governed voucher.
        gen = rc.get("governed_ungoverned_generation")
        if isinstance(gen, list):
            for e in gen:
                d = e if isinstance(e, dict) else {"malformed": True}
                _emit("second_generation", _FLOOR["second_generation"], d)

        # Ungoverned: recorded under mixed_door, ALERT under sole_door. The floor is raised by posture,
        # never by an accusation — a desk posting is legitimate; the operator's posture is the verdict.
        ungoverned_floor = 1 if posture_word == "sole_door" else _FLOOR["ungoverned"]
        ung = rc.get("ungoverned")
        if isinstance(ung, list):
            for e in ung:
                d = e if isinstance(e, dict) else {"malformed": True}
                _emit("ungoverned", ungoverned_floor, d)

        # Adverse posture: a standing condition (mutable ledger / linked-entry deletion), deny-biased
        # so an UNREADABLE (None) posture reads adverse. Assessed only when the reconciliation ran.
        pos = rc.get("posture")
        if isinstance(pos, dict):
            immutable = pos.get("enable_immutable_ledger")
            delete_linked = pos.get("delete_linked_ledger_entries")
            if immutable is not True or delete_linked is not False:
                _emit("adverse_posture", _FLOOR["adverse_posture"],
                      {"enable_immutable_ledger": immutable,
                       "delete_linked_ledger_entries": delete_linked})
        else:
            _emit("adverse_posture", _FLOOR["adverse_posture"],
                  {"reason": "posture unreadable — deny-biased to adverse"})

    # --- Aggregate ------------------------------------------------------------------------------
    by_class, by_response = {}, {k: 0 for k in _LEVELS}
    top = 0
    for f in findings:
        by_class[f["class"]] = by_class.get(f["class"], 0) + 1
        by_response[f["response"]] += 1
        top = max(top, f["response_level"])

    return {
        "target": target,
        "posture": posture_word,
        "findings": findings,
        "actionable": [f for f in findings if f["response_level"] >= _LEVELS["alert"]],
        "summary": {
            "total_findings": len(findings),
            "by_class": by_class,
            "by_response": by_response,
        },
        "response": _LEVEL_NAME[top],
        "response_level": top,
        "gate_required": top >= _LEVELS["attestation_gate"],
        "seal_required": top >= _LEVELS["contain"],
        "flags": flags,
        "scope_note": (
            "This is Pacioli's response layer: it applies the operator's configured posture and "
            "response envelope to the findings from the period Statement and Reconciliation. It "
            "renders NO verdict on another party — 'ungoverned' movement is a legitimate, normal "
            "part of real books and is RECORDED, not accused; it is elevated to an alert ONLY under "
            "a sole-door posture the operator explicitly declared. CONTAIN is never a default for a "
            "working ledger honestly recording a known uncertainty — every such finding, including "
            "Pacioli's own 'orphan'/'unconfirmed', stays at alert or below unless the operator "
            "explicitly escalates it. The sole exception is 'chain_broken': the attestation "
            "apparatus itself is provably broken (the HMAC chain cannot verify itself, so no "
            "record in it can be trusted) — a fundamentally different thing from the apparatus "
            "working and confessing an open item, and the accounting system halts on its own "
            "unprovable books rather than a verdict on a party (invariant 1, revised 2026-07-15). "
            "Otherwise, the response is the autonomy envelope the operator set in advance, not a "
            "per-gap judgment."
        ),
    }


_CLASS_WORD = {
    "orphan": "orphan (intent with no committed outcome — reconcile against the real docstatus)",
    "unconfirmed": "unconfirmed (the write got no answer and MAY have posted)",
    "second_generation": "second-generation (rows rewritten under a governed voucher — e.g. a repost)",
    "blind_read": "blind reconciliation (could not complete — cannot attest)",
    "ungoverned": "ungoverned (did not pass through Pacioli)",
    "adverse_posture": "adverse posture (the books are more mutable/scrubbable than ideal)",
    "chain_broken": "chain broken (the receipt chain failed to verify — the attestation apparatus "
                     "itself is broken, not a verdict on a party)",
}


def _finding_line(f):
    d = f["detail"] if isinstance(f.get("detail"), dict) else {}
    cls = f["class"]
    if cls in ("orphan", "unconfirmed"):
        who = f"seq {d.get('seq')} {d.get('tool')} {d.get('doctype')} {d.get('docname')} ({d.get('ts')})"
    elif cls in ("ungoverned", "second_generation"):
        who = f"{d.get('voucher_type')} {d.get('voucher_no')}"
        if cls == "ungoverned":
            who += (f" (owner {d.get('owner')}, posted {d.get('posting_date')})"
                    " — did not pass through Pacioli")
    else:
        who = _CLASS_WORD.get(cls, cls)
    return f"     [{f['response']}] {cls}: {who}"


def render_response(result):
    """Render a Response as human-legible text. The scope caveat is NEVER dropped; a finding is stated
    or flagged-for-a-reaction, NEVER accused — 'ungoverned' is 'did not pass through Pacioli', never
    'unauthorized'/'breach'/'intrusion'. The operator's posture is the only verdict, and it is named."""
    lines = [
        f"PACIOLI CLOSE — response for target {result['target']!r}",
        f"  posture:     {result['posture']}"
        + ("  (ungoverned movement is recorded, not alerted)" if result["posture"] == "mixed_door"
           else "  (operator-declared sole-door: ungoverned movement is alerted)"),
        f"  findings:    {result['summary']['total_findings']}",
    ]
    for f in result["findings"]:
        lines.append(_finding_line(f))
    for f in result["flags"]:
        lines.append(f"  ! flag:      {f}")
    resp = result["response"]
    verdict = {
        "record": "record — every finding is accounted for; nothing rises to a reaction.",
        "alert": "ALERT — one or more findings the operator asked to be told about.",
        "attestation_gate": "ATTESTATION-GATE — a fresh human attestation is required before the "
                            "next period opens.",
        "contain": "CONTAIN — the broker is sealing itself: either the operator authorised this "
                   "(direct `pacioli seal` or an --envelope escalation), or 'chain_broken' fired "
                   "(Pacioli's own ledger failed to verify — the sole default-CONTAIN, not an "
                   "operator opt-in and not a verdict on a party).",
    }[resp]
    lines.append(f"  RESULT:      {verdict}")
    lines.append("  scope:       " + result["scope_note"])
    return "\n".join(lines) + "\n"
