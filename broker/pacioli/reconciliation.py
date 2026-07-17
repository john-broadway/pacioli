# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — THE CLOSE (Half 2: the Reconciliation). Pure core, no I/O, no clock, no seal.

Half 1 (``close.py``) builds the period Statement from Pacioli's OWN receipt store — it attests only
to what passed *through* Pacioli. Its honest limit is that movement through any other door (desk
users, other integrations, the DB) is invisible to it. **This half closes that gap by the only means
a self-referential ledger honestly can:** it JOINS Pacioli's governed acts against a snapshot of the
real ERPNext General Ledger movement for the period, and sorts every voucher into three buckets:

  * ``governed``  — every GL row of the voucher lines up with a governed act (same doctype+docname,
    and — when the act's outcome carried a server ``modified`` stamp — the row's ``creation`` is
    within tolerance of it). Pacioli did this, and the movement corroborates the receipt.
  * ``ungoverned`` — no governed act references this voucher at all. It moved through another door.
    This is stated, never accused: Pacioli can see a movement *did not pass through it*; it cannot
    see *why*, and renders **no verdict** (a desk posting is normal and legitimate).
  * ``governed_ungoverned_generation`` — a voucher a governed act DID touch, but which also carries
    rows that don't line up (off-tolerance, or a different owner than the seat): a "second
    generation" of movement under a governed voucher — most often an ERPNext **repost** of the
    ledger. Attributed to the naming repost when the snapshot shows one.

**The join clock discipline (load-bearing):** a GL row's ``creation`` is ERPNext's SERVER (frappe)
clock, format ``YYYY-MM-DD HH:MM:SS.ffffff``; a governed act's ``expected_time`` is the finalizing
outcome's ``result["modified"]`` — ALSO the frappe clock. These two are compared. A Pacioli receipt
``.ts`` is a DIFFERENT clock and is **never** compared against a frappe stamp. Parsing here is
``datetime.strptime`` (parsing a supplied string, not reading a wall clock) so the core stays pure.

**Two ceilings this reconciliation cannot close, carried in the ``scope_note`` so no render drops
them:** (a) *governs-vs-detects* — Pacioli detects that a movement did not come through it, but
cannot judge why; (b) *tamper* — an actor with ERPNext server-side code execution can forge the very
``creation``/``owner``/``modified`` stamps this join reads, and can erase GL rows with no ERPNext-side
trail. This reconciliation reads the front-door truth; it cannot close that ceiling.

**Disclosed residuals (safe direction — never a false 'governed', by design):**
  * *System-generated side documents.* A governed submit/cancel can auto-author an Exchange
    Gain-Or-Loss Journal Entry (a multicurrency side effect) with its OWN ``voucher_no``. This
    slice does NOT 2-hop-attribute it to its parent act (the ``voucher_subtype`` + JE-Account
    ``reference_type``/``reference_name`` join scouted in Fork III of the 2026-07-10 forks doc was
    not built), so its GL rows surface in the ``ungoverned`` bucket — over-reported, never falsely
    governed. The 2-hop attribution is a documented future refinement.
  * *The tolerance is a window, not a point.* The ``tolerance_seconds`` gate (default 120s) is how
    a row's ``creation`` is matched to an act's ``modified``; a repost whose rows land WITHIN that
    window of the governing act's stamp reads as governed (masked). Inherent to any clock-skew
    tolerance — the gate is time-bounded, not absolute — and exposed as a parameter.
  * *Structural-only governance.* When a committed act's outcome carries no server ``modified``
    stamp (the cascade readback-confirmed path), its voucher is governed by a structural match with
    NO time gate; a second generation of those rows cannot be distinguished, and a ``flags`` entry
    says so per run.

Pure by the same discipline as ``close.py``/``prove.py``: no bench, no store, no ``datetime.now``, no
key. The glue supplies the receipts and the movement snapshot (the read layer's frozen contract);
this core only joins and classifies, so the whole thing is unit-testable without a bench.
"""
from __future__ import annotations

from datetime import datetime

from pacioli.prove import INTENT, OUTCOME

_COMMITTED = "committed"

# Governed-act intents whose doc identity is NOT a top-level (doctype, docname): a reconcile settles
# payments against invoices, so each allocation row names its own invoice/payment doc.
_RECONCILE = "reconcile"

_FRAPPE_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


def _norm_posture(v):
    """A posture flag normalized: a real ``bool`` as-is; an ``int`` (ERPNext's 0/1 checkbox wire
    form) coerced to ``bool``; ``None`` (absent) or ANY other type to ``None`` = unreadable. Keeps
    the caller's `is False`/`is True`/`is None` checks honest regardless of the wire type, and
    deny-biased — a surprise value (a stray string from a proxy) reads as unreadable, never as a
    falsely-safe truthy 'on'."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    return None


def _parse_frappe(s):
    """Parse a frappe SERVER-clock stamp (``YYYY-MM-DD HH:MM:SS.ffffff``, also without microseconds)
    to a ``datetime``, or ``None`` if it is absent / not that shape. Deny-biased: an unparseable
    stamp returns ``None`` (NOT a silently-accepted match), so a row whose time cannot be read is
    never quietly treated as time-corroborated. NEVER call this on a Pacioli ``.ts`` (different
    clock, different format)."""
    if not isinstance(s, str):
        return None
    for fmt in _FRAPPE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _governed_transitions(receipts):
    """Build the list of governed transitions from the receipt chain, reusing PROVE/close.py's exact
    rule: only a **committed** outcome finalizes an intent. Each transition carries the doc identity
    it governs plus ``expected_time`` (the finalizing outcome's ``result["modified"]``, the frappe
    server stamp) — ``None`` when absent/malformed (structural match only, no time gate)."""
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

    transitions = []
    for r in receipts:
        if r.kind != INTENT or r.seq not in finalized:
            continue
        body = r.body if isinstance(r.body, dict) else {}
        outcome = outcome_by_intent.get(r.seq)
        result = outcome.body.get("result") if (outcome is not None
                                                and isinstance(outcome.body, dict)) else None
        expected_time = result.get("modified") if isinstance(result, dict) else None
        tool = body.get("tool")

        if tool == _RECONCILE:
            # A reconcile settles a payment against invoices by writing/rebuilding the PAYMENT
            # LEDGER (PLE); it writes NO GL Entry rows (erpnext `reconcile_against_document` calls
            # `create_payment_ledger_entry` WITHOUT `make_gl_entries`/`save_entries` — scout-b,
            # erpnext version-16). This slice reconciles the GL sweep, so a reconcile has no GL
            # footprint to govern. Emitting a per-allocation (invoice/payment) transition here
            # would structurally match the invoice's/payment's OWN submit-time GL rows and wrongly
            # mark them governed — MASKING a later repost's second generation, and (worse) marking
            # a DESK-submitted invoice's GL rows "governed" merely because Pacioli settled it: a
            # false-clean the accounting model forbids (never read as all-governed when it isn't).
            # Those GL rows are governed by their SUBMIT act (if any), not by the reconcile. The
            # PLE reconciliation (the design's GL<->PLE divergence axis) is the deferred Half-2
            # refinement; when it lands, a reconcile act governs PLE movement THERE.
            continue

        # submit / cancel / amend / workflow_transition / cascade_cancel — top-level identity.
        doctype, docname = body.get("doctype"), body.get("docname")
        if doctype is None or docname is None:
            # A governing act with no doc identity cannot match a real voucher; emitting a
            # (None, None) transition would structural-match the (None, None) sentinel group that
            # coerced non-dict / identity-less GL rows collapse into — reading garbage as governed
            # (the unsafe direction). No live writer emits this; a belt for a forged/garbage receipt.
            continue
        transitions.append({"receipt_seq": r.seq, "tool": tool, "expected_time": expected_time,
                            "doctype": doctype, "docname": docname,
                            "transition": body.get("transition") or tool})
    return transitions


def _row_governed(row, matching, tolerance_seconds, seat_owner):
    """Is one GL ``row`` governed, given the transitions that already match its voucher identity?

    Returns ``(governed, reason, structural_only)``. ``reason`` (``"owner"`` / ``"time"``) names why
    a non-governed row failed — for the generation-bucket note. ``structural_only`` is True when the
    row IS governed but ONLY via a matching transition that carried no ``expected_time`` (no server
    ``modified`` stamp — e.g. a cascade cancel confirmed by readback, ``cascade.py``): the time gate
    could not be applied, so a later second generation of this row cannot be distinguished, and the
    caller flags it. A time-corroborated match is PREFERRED over a structural one — a row that fails
    every time-gated transition is not silently rescued by a co-present structural one — but a
    genuinely stamp-less act still governs its own rows (never over-accused), just flagged.
    Deny-biased throughout: an unreadable time is NOT a match.
    """
    # Seat corroboration (Fork III): when a seat owner is asserted, a row stamped by a different
    # owner cannot be governed no matter which act matches — it downgrades the voucher.
    if seat_owner is not None and row.get("owner") != seat_owner:
        return (False, "owner", False)
    row_dt = _parse_frappe(row.get("creation"))
    structural = False
    time_reason = None
    for t in matching:
        if t["expected_time"] is None:
            structural = True  # remember it, but keep looking for a time-corroborated match first
            continue
        exp_dt = _parse_frappe(t["expected_time"])
        if row_dt is not None and exp_dt is not None \
                and abs((row_dt - exp_dt).total_seconds()) <= tolerance_seconds:
            return (True, None, False)  # time-corroborated wins over any structural match
        time_reason = "time"
    if structural:
        return (True, None, True)  # governed only structurally — no time gate could apply; flagged
    return (False, time_reason or "time", False)


def _repost_ref(vt, vn, reposts):
    """The name of the snapshot repost whose ``vouchers`` names ``(vt, vn)``, or ``None``."""
    for rp in reposts:
        if not isinstance(rp, dict):
            continue
        for v in rp.get("vouchers") or []:
            if isinstance(v, dict) and v.get("voucher_type") == vt and v.get("voucher_no") == vn:
                return rp.get("name")
    return None


def build_reconciliation(receipts, snapshot, *, target, company=None, since=None, until=None,
                         seat_owner=None, tolerance_seconds=120):
    """Join Pacioli's governed acts (``receipts``) against a movement ``snapshot`` for a period and
    sort every GL voucher into governed / ungoverned / second-generation. Returns a JSON-native dict.

    ``snapshot`` is the read layer's frozen contract: ``{"gl_rows":[...], "reposts":[...],
    "posture":{...}}`` (see the module docstring and ``docs``). ``receipts`` are NOT re-windowed —
    the read layer already scoped the snapshot to the period, and a receipt's ``.ts`` is a different
    clock than the frappe stamps on the rows, so windowing receipts here would false-negative real
    governance; ``since``/``until`` are recorded for display only. ``seat_owner`` (optional): when
    given, a governed classification additionally requires each row's ``owner`` to equal it.

    ``complete`` is true only when the posture is fully readable (neither posture flag is ``None``)
    and the core detects no internal inconsistency. Ungoverned movement does NOT make a period
    incomplete — in accounting mode that is normal, presented not failed. An unreadable posture flips
    ``complete`` false and is flagged, so the caller refuses.
    """
    gl_rows = snapshot.get("gl_rows") or []
    reposts = snapshot.get("reposts") or []
    posture_in = snapshot.get("posture") or {}
    # Normalize posture defensively: ERPNext checkbox fields read back as int 0/1, not bool, and the
    # danger checks below are `is False`/`is True` identity tests (`0 is False` is False in Python).
    # A real bool passes through; an int coerces; None (absent) and ANY surprise type -> None =
    # unreadable (deny-biased, refuses `complete`), never a falsely-safe truthy. The glue coerces
    # too, but the core must not depend on a caller remembering to (sec-E / F1).
    immutable = _norm_posture(posture_in.get("enable_immutable_ledger"))
    delete_linked = _norm_posture(posture_in.get("delete_linked_ledger_entries"))

    transitions = _governed_transitions(receipts)

    # Group rows by voucher, preserving first-seen order for deterministic output. EVERY row lands in
    # exactly one group and every group in exactly one bucket — no row is ever silently dropped.
    groups = {}  # (voucher_type, voucher_no) -> [rows]
    for row in gl_rows:
        row = row if isinstance(row, dict) else {}
        key = (row.get("voucher_type"), row.get("voucher_no"))
        groups.setdefault(key, []).append(row)

    governed, ungoverned, generation = [], [], []
    flags = []
    time_uncorroborated = False
    structural_only_count = 0  # governed vouchers whose match had no time gate (F2 blind spot)

    for (vt, vn), rows in groups.items():
        matching = [t for t in transitions if t["doctype"] == vt and t["docname"] == vn]
        first = rows[0]
        if not matching:
            ungoverned.append({
                "voucher_type": vt, "voucher_no": vn,
                "owner": first.get("owner"),
                "posting_date": first.get("posting_date"),
                "creation": first.get("creation"),
                "gl_row_count": len(rows),
                "note": "no governed act references this voucher — it did not pass through Pacioli",
            })
            continue

        results = [_row_governed(row, matching, tolerance_seconds, seat_owner) for row in rows]
        if all(g for g, _, _ in results):
            rep = matching[0]
            if any(structural for _, _, structural in results):
                structural_only_count += 1
            governed.append({
                "voucher_type": vt, "voucher_no": vn,
                "doctype": vt, "docname": vn,
                "receipt_seq": rep["receipt_seq"],
                "transition": rep["transition"],
                "gl_row_count": len(rows),
                "owner": first.get("owner"),
            })
        else:
            # Second generation: the act touched this voucher, but some rows don't line up.
            off = next(row for row, (g, _, _) in zip(rows, results) if not g)
            reasons = {reason for g, reason, _ in results if not g}
            note = _generation_note(reasons)
            if "time" in reasons:
                time_uncorroborated = True
            generation.append({
                "voucher_type": vt, "voucher_no": vn,
                "doctype": vt, "docname": vn,
                "governed_transitions": [
                    {"receipt_seq": t["receipt_seq"], "transition": t["transition"]}
                    for t in matching
                ],
                "ungoverned_creation": off.get("creation"),
                "ungoverned_owner": off.get("owner"),
                "gl_row_count": len(rows),
                "repost_ref": _repost_ref(vt, vn, reposts),
                "note": note,
            })

    reposts_in_window = [
        {"name": rp.get("name"), "owner": rp.get("owner"), "creation": rp.get("creation"),
         "docstatus": rp.get("docstatus"),
         "voucher_count": len(rp.get("vouchers") or [])}
        for rp in reposts if isinstance(rp, dict)
    ]

    # --- posture + completeness --------------------------------------------------------------
    if immutable is None:
        flags.append("posture unreadable: enable_immutable_ledger is None — cannot confirm the GL is "
                     "immutable; refusing to call this reconciliation complete")
    elif immutable is False:
        flags.append("posture: enable_immutable_ledger is off — GL rows can be deleted with no "
                     "ERPNext-side trail (the tamper ceiling; see scope)")
    if delete_linked is None:
        flags.append("posture unreadable: delete_linked_ledger_entries is None — cannot confirm "
                     "linked GL entries are protected; refusing to call this reconciliation complete")
    elif delete_linked is True:
        flags.append("posture: delete_linked_ledger_entries is on — cancelling a voucher can delete "
                     "its linked GL rows (movement can vanish from this snapshot)")
    if time_uncorroborated:
        flags.append("one or more rows under a governed voucher could not be time-corroborated "
                     "(off-tolerance or an unreadable creation stamp) — surfaced as second generation")
    if structural_only_count:
        flags.append(f"{structural_only_count} voucher(s) were governed by a structural match only "
                     "(the act's outcome carried no server `modified` stamp — e.g. a cascade cancel "
                     "confirmed by readback); the time gate could not be applied, so a second "
                     "generation of those vouchers' rows cannot be distinguished from the governed one")

    posture_readable = immutable is not None and delete_linked is not None

    gl_rows_total = len(gl_rows)
    covered = (sum(e["gl_row_count"] for e in governed)
               + sum(e["gl_row_count"] for e in ungoverned)
               + sum(e["gl_row_count"] for e in generation))
    internal_consistent = covered == gl_rows_total
    if not internal_consistent:  # defensive — must be impossible; never silently continue
        flags.append(f"internal inconsistency: {covered} rows bucketed but snapshot has "
                     f"{gl_rows_total} — refusing to call this reconciliation complete")

    complete = posture_readable and internal_consistent

    return {
        "target": target,
        "company": company,
        "period": {"since": since, "until": until},
        "governed": governed,
        "ungoverned": ungoverned,
        "governed_ungoverned_generation": generation,
        "reposts_in_window": reposts_in_window,
        "summary": {
            "gl_rows_total": gl_rows_total,
            "vouchers_total": len(groups),
            "governed": len(governed),
            "ungoverned": len(ungoverned),
            "governed_ungoverned_generation": len(generation),
        },
        "posture": {
            "enable_immutable_ledger": immutable,
            "delete_linked_ledger_entries": delete_linked,
        },
        "complete": complete,
        "flags": flags,
        "scope_note": (
            f"This reconciliation joins Pacioli's governed acts against the ERPNext General Ledger "
            f"movement for target {target!r}. It GOVERNS-vs-DETECTS: Pacioli can see that a voucher "
            "did not pass through it (the 'ungoverned' bucket), but it cannot see WHY and renders no "
            "verdict — a desk posting is a normal, legitimate movement, not an accusation. TAMPER "
            "CEILING: an actor with ERPNext server-side code execution can forge the creation/owner/"
            "modified stamps this join reads, and can erase GL rows with no ERPNext-side trail; this "
            "reconciliation reads the front-door truth and cannot close that ceiling. Confirm the "
            "posture (immutable ledger on, linked-entry deletion off) and pin the Pacioli chain head "
            "off-box to narrow it."
        ),
    }


def _generation_note(reasons):
    """A human note for a second-generation voucher, from the set of per-row failure reasons."""
    if reasons == {"owner"}:
        return ("a governed act references this voucher, but one or more of its rows are stamped by "
                "a different owner than the seat")
    if reasons == {"time"}:
        return ("a governed act references this voucher, but one or more of its rows fall outside "
                "the time tolerance of the act (a second generation, e.g. a ledger repost)")
    return ("a governed act references this voucher, but one or more of its rows do not line up with "
            "the act (owner and/or time) — a second generation, e.g. a ledger repost")


def render_reconciliation(result):
    """Render a Reconciliation as human-legible text (the join made readable). The scope caveat is
    NEVER dropped, and the ungoverned bucket is stated, NEVER accused — "did not pass through
    Pacioli", never "unauthorized"/"breach"/"intrusion"."""
    p = result["period"]
    window = f"{p['since'] or 'genesis'} .. {p['until'] or 'now'}"
    s = result["summary"]
    company = f" / company {result['company']!r}" if result.get("company") else ""
    lines = [
        f"PACIOLI CLOSE — reconciliation for target {result['target']!r}{company}",
        f"  period:      {window}",
        f"  gl rows:     {s['gl_rows_total']} across {s['vouchers_total']} voucher(s)",
        f"  governed:    {s['governed']}  (every row lines up with a governed act)",
        f"  ungoverned:  {s['ungoverned']}  (did not pass through Pacioli — stated, not judged)",
        f"  2nd-gen:     {s['governed_ungoverned_generation']}  (governed voucher, rows that do not "
        "line up — e.g. a ledger repost)",
    ]
    pos = result["posture"]
    lines.append(
        "  posture:     immutable-ledger="
        + _posture_word(pos["enable_immutable_ledger"])
        + ", delete-linked-entries=" + _posture_word(pos["delete_linked_ledger_entries"])
    )
    for e in result["ungoverned"]:
        lines.append(f"     ungoverned: {e['voucher_type']} {e['voucher_no']} "
                     f"(owner {e['owner']}, {e['gl_row_count']} row(s), posted {e['posting_date']}) "
                     "— did not pass through Pacioli.")
    for e in result["governed_ungoverned_generation"]:
        ref = e["repost_ref"]
        attributed = f" attributed to repost {ref}" if ref else " (no repost names it in-window)"
        lines.append(f"     2nd-gen:    {e['voucher_type']} {e['voucher_no']} "
                     f"({e['gl_row_count']} row(s); off-row by {e['ungoverned_owner']} "
                     f"at {e['ungoverned_creation']}){attributed} — {e['note']}.")
    for rp in result["reposts_in_window"]:
        lines.append(f"     repost:     {rp['name']} by {rp['owner']} ({rp['voucher_count']} "
                     f"voucher(s), docstatus {rp['docstatus']}, created {rp['creation']}).")
    for f in result["flags"]:
        lines.append(f"  ! flag:      {f}")
    lines.append("  RESULT:      "
                 + ("complete — posture readable and every GL row is accounted for."
                    if result["complete"]
                    else "NOT complete — see flags (posture unreadable or an internal "
                         "inconsistency); the caller refuses."))
    lines.append("  scope:       " + result["scope_note"])
    return "\n".join(lines) + "\n"


def _posture_word(v):
    return {True: "on", False: "off", None: "UNREADABLE"}[v]
