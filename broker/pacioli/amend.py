# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — AMEND: the corrected re-draft after a governed cancel (pure core).

After a governed cancel the human's story continues with the *corrected* entry. ERPNext has no
native ``amend()`` server method — the documented flow is client-side: copy the cancelled document,
strip what must not carry over, set ``amended_from``, insert as a fresh draft. This module builds
that payload, and **the strip-list is the security surface**: every field that survives the copy
is data the new draft asserts, and every field wrongly carried over is either a forged identity
(``name``, audit stamps), a smuggled state (``docstatus``, ``status``), or stale settlement
residue. So the copy is deny-biased the way everything here is — identity, audit, state, and
frappe runtime metadata are stripped by name (and every ``_``-prefixed key by rule); document
*data* — including the child tables (items, taxes) — survives intact.

What is stripped, and why (mirrors what ``frappe.copy_doc`` + the desk amend flow drop):

* **Identity** — ``name`` (the bench names the draft from the series; a supplied name would be a
  forged identity), ``doctype`` (the resource URL is the authority; a body ``doctype`` that could
  disagree with it is a confusion vector, so it never travels).
* **Audit stamps** — ``owner``, ``creation``, ``modified``, ``modified_by``, ``idx`` (the bench
  stamps the new document; copying them would forge provenance).
* **State** — ``docstatus`` (SET to 0 explicitly: the amendment is a DRAFT by construction),
  ``status`` / ``workflow_state`` (the cancelled state must not leak into the draft),
  ``outstanding_amount`` (settlement residue; the bench recomputes it at validate).
* **Cancelled linkage** — ``amended_from`` is never copied; it is **SET** to the source's name
  (so a chain SI → SI-1 → SI-2 always points one hop back, never to the original twice).
* **Frappe runtime metadata** — every ``_``-prefixed key (``_comments``, ``_assign``,
  ``_user_tags``, ``_liked_by``, ``__onload``, …) by rule: underscore keys are desk/runtime
  state, never document data.
* **Child-row identity** — per row of every child table: ``name``, ``parent``, ``parentfield``,
  ``parenttype``, ``owner``, ``creation``, ``modified``, ``modified_by``, ``docstatus`` (the rows
  are new rows of a new document); the row's *data* (item_code, qty, rate, accounts, ``idx``
  ordering, child ``doctype``) survives.

Parity note (F-C3 investigated and REFUTED, 2026-07-07): the strip-list deliberately does NOT
walk ERPNext's per-field ``no_copy`` meta — and that is native-amend PARITY, not a gap. The desk
amend flow is client-side ``frappe.model.copy_doc(doc, from_amend)`` (``create_new.js``,
verbatim-identical in version-15 and version-16), and its strip condition —
``is_no_copy = !from_amend && cint(df.no_copy) == 1`` — explicitly disables the ``no_copy``
strip on amend; only ``name``/``amended_from``/``amendment_date``/``cancel_reason``,
``Password``-type fields, and ``__``-internal keys are withheld. It is "Duplicate" (from_amend
falsy), not amend, that strips ``no_copy``. The strip set above is a strict superset of native
amend's withheld set for every supported doctype (none defines ``amendment_date``,
``cancel_reason``, or a ``Password`` field). Live-confirmed PHASE V (P7): a ``no_copy=1`` custom
field copied through — matching native amend. Honest residual: a site-added *custom* field of
fieldtype ``Password`` would be withheld by native amend but copied here (rare; recorded, not
silently assumed away).

The consent decision (recorded here because this module is why it holds): **amend takes no
marker.** It creates a reversible DRAFT — nothing posts, deleting the draft undoes it. The
irreversible act remains *submit*, which already demands its own plan + human-minted marker;
demanding consent for a reversible act would dilute what the marker means. Amend still writes
the intent+outcome receipt pair (op ``"amend"``, transition ``"2->0(draft)"``) so the book shows
the full arc cancel → amend → submit. Pure: no frappe, no I/O.

**Honest residual (the missing marker's one cost).** ``amend_sales_invoice`` refuses a source that
already has an amendment, but that check is a read (``find_amendments``) with no atomic claim
before the create — submit/cancel close the equivalent race with the marker CAS, and amend has no
marker. So two *concurrent* amends of the same cancelled document can both see "no amendment yet"
and both create a draft. The blast radius is bounded to a **duplicate reversible draft**: neither
posts anything (each still needs its own plan + human-minted marker to submit), both are visible,
and either can simply be deleted — it is never a double-posting of money. Not silently assumed
away; the operator reconciles the extra draft by hand, the same shape as an orphan intent.
"""
from __future__ import annotations

# Top-level fields that must not carry into the amendment (see module docstring for the why).
_STRIP_DOC = frozenset({
    "name", "doctype",                                       # identity
    "owner", "creation", "modified", "modified_by", "idx",   # audit stamps
    "docstatus", "status", "workflow_state",                 # state (docstatus is re-SET to 0)
    "outstanding_amount",                                    # settlement residue (recomputed)
    "amended_from",                                          # never copied — re-SET to the source
})

# Per-row fields that must not carry: the rows are NEW rows of a NEW document.
_STRIP_ROW = frozenset({
    "name", "parent", "parentfield", "parenttype",
    "owner", "creation", "modified", "modified_by", "docstatus",
})


def _strip_row(row):
    return {k: v for k, v in row.items() if not k.startswith("_") and k not in _STRIP_ROW}


# The one strip-list key a seat MAY write: ``workflow_state`` is stripped precisely so the
# cancelled state never leaks — the seat setting it FRESH (to the workflow's initial state) is
# the design, not a collision. Everything else on the strip-list stays reserved: identity,
# audit, linkage, ``docstatus``, and ``status`` (ERPNext-derived; a workflow pointed at it is
# exactly the confusing configuration a deny-biased broker refuses rather than guesses about).
_SEAT_RESERVED = frozenset(_STRIP_DOC - {"workflow_state"})


def seat_conflict(field, source_doc):
    """Reason (str) iff seating ``field`` would re-enter the strip-list's security surface —
    ``None`` when the field is safe to seat. The strip-list exists to keep identity, audit,
    state, and settlement residue out of the amendment; a (mis)configured — or maliciously
    configured — ``workflow_state_field`` naming one of those keys would overwrite the very
    value the strip protects (``amended_from`` = a forged chain, ``docstatus`` = a smuggled
    state). Underscore keys are runtime metadata by rule, and a field whose source value is a
    LIST is a child table — a seat there would replace document rows with a string. Deny-biased:
    the caller refuses (tool layer) or raises (:func:`amend_payload`), never overwrites.
    Pure; shared by both layers so the two refusals cannot drift apart."""
    if not isinstance(field, str) or not field.strip():
        return "seat field is not a non-empty string"
    if field.startswith("_"):
        return f"seat field {field!r} is frappe runtime metadata (underscore rule)"
    if field in _SEAT_RESERVED:
        return (f"seat field {field!r} is on the amend strip-list — seating it would overwrite "
                "a protected identity/audit/state field")
    if isinstance(source_doc, dict) and isinstance(source_doc.get(field), list):
        return f"seat field {field!r} names a child table — a seat would replace its rows"
    return None


def amend_payload(source_doc, seat=None):
    """Build the insert payload for the amended draft of ``source_doc``. Pure; never mutates
    the source. Raises ``ValueError`` (deny-biased, belt-and-suspenders under the tool's own
    docstatus gate) on a non-dict source, a nameless source, or an uncancelled source —
    only a cancelled document (docstatus 2) has anything to amend.

    ``seat`` (the F1 fix, 2026-07-17 — found by the first dogfood drive): the
    ``(state_field, state)`` pair from :func:`pacioli.workflow.initial_seat`, applied AFTER the
    strip. The strip correctly drops the cancelled document's ``workflow_state``, but a draft
    born under an ACTIVE workflow with no state is stuck — the transition gate (rightly) refuses
    a null state, and the bench's REST insert does not backfill it (live-observed). Setting the
    field by name also overwrites a CUSTOM ``workflow_state_field`` value the copy carried over
    (a custom field is not in the literal strip-list). ``None`` — the ungoverned case — keeps the
    payload byte-identical to before. A malformed seat raises (deny-biased): the caller computed
    it from workflow config, and garbage here means that config read went wrong."""
    if seat is not None:
        if (not isinstance(seat, tuple) or len(seat) != 2
                or not isinstance(seat[0], str) or not seat[0].strip()
                or not isinstance(seat[1], str) or not seat[1].strip()):
            raise ValueError("workflow seat must be a (state_field, state) pair of "
                             "non-empty strings")
        conflict = seat_conflict(seat[0], source_doc if isinstance(source_doc, dict) else None)
        if conflict is not None:
            raise ValueError(conflict)
    if not isinstance(source_doc, dict):
        raise ValueError("amend needs the full source document (a dict)")
    source_name = source_doc.get("name")
    if not isinstance(source_name, str) or not source_name.strip():
        raise ValueError("amend needs the source document's name")
    if source_doc.get("docstatus") != 2:
        raise ValueError(
            f"only a cancelled document (docstatus 2) can be amended; "
            f"{source_name} has docstatus {source_doc.get('docstatus')!r}")
    payload = {}
    for key, value in source_doc.items():
        if key.startswith("_") or key in _STRIP_DOC:
            continue
        if isinstance(value, list) and all(isinstance(r, dict) for r in value):
            payload[key] = [_strip_row(r) for r in value]  # a child table (possibly empty)
        else:
            payload[key] = value
    payload["amended_from"] = source_name
    payload["docstatus"] = 0
    if seat is not None:
        payload[seat[0]] = seat[1]
    return payload
