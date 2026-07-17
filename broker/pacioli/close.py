# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — THE CLOSE (Half 1: the period Statement). Pure core, no I/O, no clock, no seal.

Double-entry's power in 1494 was not recording — it was the CLOSE: at period end the books balance
or they confess a gap. PROVE records every governed act (the *giornale*); the Close is the *bilancio
di verificazione* — the trial balance — for a period: every governed act, balanced (committed) or
confessed (an orphan intent, a debit with no credit).

**What this half does and does NOT claim** (docs/plans/2026-07-09-the-close.md): the Statement is
built from Pacioli's OWN receipt store, so it attests only to what passed *through Pacioli*. It is
self-referential — Pacioli attesting to Pacioli — NOT a statement about the whole ERPNext ledger.
Movement through any other door (desk users, other integrations, the DB) is invisible here until
the Reconciliation (Half 2, forthcoming). That honest limit is carried IN the statement's own
``scope_note`` so no render can drop it.

Pure by the same discipline as ``prove.py``: this core neither seals nor reads a store nor knows the
clock. The glue (``cli.py``) supplies the receipts, the verify result, the head, and any off-box
anchor; this core only classifies and summarises. So the whole period-attestation logic is
unit-testable without a bench or a key.
"""
from __future__ import annotations

from pacioli.prove import INTENT, OUTCOME

_COMMITTED = "committed"
_RECORDED_OPEN = "recorded_open"
_ORPHAN = "orphan"
_UNKNOWN = "(unknown)"


def _in_window(ts, since, until):
    """ISO ts strings are sortable (``YYYY-MM-DDTHH:MM:SSZ``), so window bounds are plain string
    comparisons, inclusive on both ends. A receipt with NO ts is never silently dropped from a
    windowed statement — it CANNOT be excluded (we don't know when it happened), so it is included
    and flagged by the caller-visible path (deny-biased: an attestation never quietly omits an act)."""
    if not ts:
        return True
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def build_statement(receipts, *, target, since=None, until=None,
                    head=None, verified=None, anchor=None):
    """Build the period Statement from a receipt list. Returns a JSON-native dict.

    An **act** is anchored by its ``intent`` receipt and windowed by that intent's ``ts`` (the act
    belongs to the period it was initiated in). Its outcome is resolved against the WHOLE chain, not
    just the window, so an outcome landing just past ``until`` never misreports a committed act as an
    orphan. Classification reuses PROVE's own rule — only a ``committed`` outcome finalizes:

      * ``committed``      — intent + committed outcome (the balanced entry).
      * ``recorded_open``  — intent + a terminal non-committed outcome. A ``failed`` outcome (the
                             bench answered and refused — nothing landed) is a KNOWN non-event; an
                             ``unconfirmed`` one (no answer — the write MAY have posted) is a
                             suspense item that must confess. Both are surfaced, never hidden.
      * ``orphan``         — intent with no outcome at all: the unbalanced entry, the confession.

    ``verified`` is the ``(ok, reason)`` the glue got from ``verify_chain`` (this core does not
    seal). ``head`` is the live chain head hmac; ``anchor`` the off-box-pinned head, if any.
    ``balanced`` is true ONLY when there are no orphans, no ``unconfirmed`` acts, the chain verifies,
    AND (if an anchor was given) the live head matches it. An ``unconfirmed`` act blocks a clean
    close exactly like an orphan — it may have posted server-side and is unresolved, the one status
    ``prove``'s own orphan sweep also refuses to clear — while a ``failed`` act (definitely nothing
    landed) does not, so governance correctly refusing a write never reads as an unbalanced period.
    """
    ok, reason = verified if verified is not None else (None, None)

    # Resolve outcomes against the FULL chain (not the window) — see docstring.
    finalized = {
        r.body.get("finalizes")
        for r in receipts
        if r.kind == OUTCOME and isinstance(r.body, dict) and r.body.get("status") == _COMMITTED
    }
    outcome_by_intent = {
        r.body.get("finalizes"): r
        for r in receipts
        if r.kind == OUTCOME and isinstance(r.body, dict) and "finalizes" in r.body
    }

    acts, flags = [], []
    by_class = {_COMMITTED: 0, _RECORDED_OPEN: 0, _ORPHAN: 0}
    unconfirmed_count = 0  # recorded_open acts whose outcome is `unconfirmed` — they block a clean close
    by_tool, by_target, by_doctype = {}, {}, {}

    for r in receipts:
        if r.kind != INTENT:
            continue
        if not _in_window(r.ts, since, until):
            continue
        if not r.ts:
            flags.append(f"receipt seq {r.seq} has no timestamp — included in the window it "
                         "cannot be excluded from (deny-biased)")
        body = r.body if isinstance(r.body, dict) else {}
        if r.seq in finalized:
            cls = _COMMITTED
        elif r.seq in outcome_by_intent:
            cls = _RECORDED_OPEN
        else:
            cls = _ORPHAN
        by_class[cls] += 1
        outcome = outcome_by_intent.get(r.seq)
        outcome_status = (outcome.body.get("status")
                          if outcome is not None and isinstance(outcome.body, dict) else None)
        # A `failed` outcome is a KNOWN non-event (bench answered and refused; nothing landed) and
        # does not block a clean close. An `unconfirmed` one got no answer and MAY have posted — a
        # suspense item that must confess, exactly like an orphan.
        if cls == _RECORDED_OPEN and outcome_status == "unconfirmed":
            unconfirmed_count += 1
        tool = body.get("tool") or _UNKNOWN
        tgt = body.get("target") or _UNKNOWN
        doctype = body.get("doctype") or _UNKNOWN
        by_tool[tool] = by_tool.get(tool, 0) + 1
        by_target[tgt] = by_target.get(tgt, 0) + 1
        by_doctype[doctype] = by_doctype.get(doctype, 0) + 1
        acts.append({
            "seq": r.seq,
            "ts": r.ts,
            "tool": body.get("tool"),
            "target": body.get("target"),
            "doctype": body.get("doctype"),
            "docname": body.get("docname"),
            "transition": body.get("transition"),
            "plan_id": body.get("plan_id"),
            "class": cls,
            "outcome_status": outcome_status,
        })

    anchor_matches = None if anchor is None else (head is not None and head == anchor)
    balanced = (
        by_class[_ORPHAN] == 0
        and unconfirmed_count == 0
        and ok is True
        and (anchor_matches is not False)
    )
    return {
        "target": target,
        "period": {"since": since, "until": until},
        "acts": acts,
        "summary": {
            "total_acts": len(acts),
            "by_class": by_class,
            "unconfirmed": unconfirmed_count,
            "by_tool": by_tool,
            "by_target": by_target,
            "by_doctype": by_doctype,
        },
        "chain": {
            "head": head,
            "count": len(receipts),
            "verified": ok,
            "verify_reason": reason,
            "anchor_head": anchor,
            "anchor_matches": anchor_matches,
        },
        "balanced": balanced,
        "flags": flags,
        "scope_note": (
            f"This attests ONLY to activity that passed through Pacioli on target {target!r}. "
            "It is NOT a statement about the whole ERPNext ledger — movement through any other "
            "door (desk users, other integrations, direct database access) is invisible here "
            "until the Reconciliation (forthcoming). Verify this statement against the off-box "
            "anchor head."
        ),
    }


def render_statement(st):
    """Render a Statement as human-legible text (the proof made readable). The scope
    caveat and — when unbalanced — the confession are never omitted."""
    p = st["period"]
    window = (f"{p['since'] or 'genesis'} .. {p['until'] or 'now'}")
    s = st["summary"]
    by = s["by_class"]
    lines = [
        f"PACIOLI CLOSE — period statement for target {st['target']!r}",
        f"  period:      {window}",
        f"  governed acts: {s['total_acts']}  "
        f"(committed {by['committed']}, recorded-open {by['recorded_open']}"
        + (f" incl. {s['unconfirmed']} unconfirmed" if s.get("unconfirmed") else "")
        + f", orphan {by['orphan']})",
    ]
    if s["by_tool"]:
        lines.append("  by tool:     " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_tool"].items())))
    if s["by_doctype"]:
        lines.append("  by doctype:  " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_doctype"].items())))
    c = st["chain"]
    verified = {True: "verifies", False: f"FAILS ({c['verify_reason']})", None: "not checked"}[c["verified"]]
    lines.append(f"  chain:       {c['count']} receipts, {verified}; head {c['head']}")
    if c["anchor_head"] is not None:
        lines.append(f"  anchor:      pinned {c['anchor_head']} — "
                     + ("matches" if c["anchor_matches"] else "DOES NOT MATCH the live head"))
    for f in st["flags"]:
        lines.append(f"  ! flag:      {f}")
    if st["balanced"]:
        lines.append("  RESULT:      balanced — every governed act in this period is accounted for "
                     "on a verified chain.")
    else:
        lines.append("  RESULT:      NOT balanced — this period does not close clean:")
        orphans = [a for a in st["acts"] if a["class"] == _ORPHAN]
        for a in orphans:
            lines.append(f"                 orphan: seq {a['seq']} {a['tool']} "
                         f"{a['doctype']} {a['docname']} ({a['ts']}) — intent with no committed "
                         "outcome; reconcile against the real docstatus.")
        unconfirmed = [a for a in st["acts"]
                       if a["class"] == _RECORDED_OPEN and a["outcome_status"] == "unconfirmed"]
        for a in unconfirmed:
            lines.append(f"                 unconfirmed: seq {a['seq']} {a['tool']} "
                         f"{a['doctype']} {a['docname']} ({a['ts']}) — the write got no answer and "
                         "MAY have posted; reconcile against the real docstatus.")
        if c["verified"] is not True:
            lines.append(f"                 chain does not verify: {c['verify_reason']}")
        if c["anchor_matches"] is False:
            lines.append("                 live head disagrees with the off-box anchor.")
    lines.append("  scope:       " + st["scope_note"])
    return "\n".join(lines) + "\n"
