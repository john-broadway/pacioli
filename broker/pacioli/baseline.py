"""Party baselines — the PURE decision core for PLAN-time contextual disclosure.

No I/O, no clock, no frappe, no config lookups. Given a history summary and a candidate amount,
decide what to SAY. Split out for the same reason ``pacioli_guard.scope`` is split out in guard: the
judgement is the part that must be testable without a bench, and the part a reviewer should be able
to read in one sitting.

ADVISORY ONLY. Nothing in this module can permit, refuse, or alter a write. It returns strings.

Design: docs/plans/internal/2026-07-30-party-baselines-design.md
"""

#: The band multiple. An amount fires only when it exceeds the prior maximum by this factor.
#: John's number, 2026-07-30. Legible in every message it produces, so the rule never has to be
#: inferred from source.
K_DEFAULT = 2.0

#: The read window. Lives HERE, in the pure core, and ``history`` imports it, so there is exactly one
#: definition of the number that appears in the emitted text. Two defaults for one number is how the
#: message and the query drift apart.
WINDOW_DEFAULT = 100

#: Below this many prior documents, a median invites more confidence than it earns, so it is
#: withheld. The range and n are stated at every n >= 1 regardless.
_P50_FLOOR = 3


def numeric(value):
    """A real number, or None. ``bool`` is excluded deliberately: it is an int subclass, and a
    stray True would otherwise summarize as the amount 1.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def summarize(amounts):
    """n / min / max / p50 over company-currency amounts. Non-numeric entries are SKIPPED, never
    coerced: a missing ``base_`` amount read as zero would silently drag the range down and make an
    outside-band amount look in-band."""
    clean = sorted(v for v in (numeric(a) for a in amounts or []) if v is not None)
    n = len(clean)
    if not n:
        return {"n": 0, "min": None, "max": None, "p50": None}
    p50 = None
    if n >= _P50_FLOOR:
        mid = n // 2
        p50 = clean[mid] if n % 2 else (clean[mid - 1] + clean[mid]) / 2.0
    return {"n": n, "min": clean[0], "max": clean[-1], "p50": p50}


def _amount(value):
    return f"{value:,.2f}"


def unavailable(reason):
    """History could not be read. States it. NEVER returns silence, because a plan with nothing
    added reads as 'normal', and the human cannot tell a clean result from an absent one."""
    return {
        "context": [f"history unavailable: {reason}"],
        "flags": [],
        "source": "none",
        "as_of": None,
    }


def assess(*, party, amount, summary, doctype, last_seen=None, window_hit=False,
           window=WINDOW_DEFAULT, k=K_DEFAULT, source="books", as_of=None):
    """Context always. A flag only on novelty or outside-band.

    Returns ``{"context": [...], "flags": [...], "source": ..., "as_of": ...}``. ``context`` is
    always non-empty when this is called; ``flags`` is empty unless something fired. Exactly two
    flag kinds exist.

    ``doctype`` is REQUIRED and has no default, deliberately. The read that produces ``summary`` is
    doctype-scoped (``party_amount_history`` filters by doctype, party and ``docstatus=1``), so every
    sentence built here is a claim about ONE doctype and must say which. Before 2026-07-30 this
    function was never told, and said "FIRST {party} document in these books" on a Payment Entry
    plan for a party with 26 submitted Sales Invoices — a claim the read never supported. A default
    would make not-knowing silent again, which is the whole defect.
    """
    n = summary.get("n") or 0
    if n == 0:
        return {
            # "submitted" is load-bearing, not padding. The read behind this is `docstatus = 1`
            # ONLY (see erpnext.party_amount_history), so a party with forty DRAFT documents of
            # this doctype produces n == 0 here. Without the qualifier the sentence claims to have
            # looked at everything and found nothing, which is a claim the read never supported.
            # This is the SAME overclaim as the doctype one, on the docstatus axis; it was left
            # behind when the doctype axis was fixed, and a second lens found it nine lines from
            # the sibling that had the qualifier all along.
            "context": [f"FIRST submitted {party} {doctype} in these books. "
                        f"No prior submitted {doctype} to compare against."],
            # `flags` is part of this function's public contract even though the only production
            # caller bars it down to kind+party before anyone reads it. It carried the original
            # unscoped "this book's history" wording after the context lines were fixed.
            "flags": [f"NOVEL COUNTERPARTY: no prior submitted {doctype} for {party} to compare "
                      f"{_amount(amount)} against."],
            "source": source,
            "as_of": as_of,
        }

    window_note = f" (over the last {window} submitted)" if window_hit else ""
    context = [
        f"{n} prior submitted {doctype} document(s) for {party}{window_note}.",
        f"prior range {_amount(summary['min'])} to {_amount(summary['max'])}.",
    ]
    if summary.get("p50") is not None:
        context.append(f"prior median {_amount(summary['p50'])}.")
    if last_seen:
        context.append(f"most recent {last_seen}.")
    context.append(f"this one {_amount(amount)}.")

    flags = []
    prior_max = summary["max"]
    # John's ruling, 2026-07-30: ERPNext credit notes/returns carry a NEGATIVE base_grand_total,
    # real data this meets. A multiple computed against a non-positive prior max produces a flag
    # that contradicts its own text (a sub-1.0 "multiple" that still fires, or "infx" at zero). A
    # disclosure that contradicts itself is worse than one that admits its limit, so below zero and
    # at zero alike: no multiple is computed and no band flag fires. The range is still stated.
    if prior_max > 0:
        threshold = prior_max * k
        if amount > threshold:
            multiple = amount / prior_max
            # The SAME parameters, stated on the human's side of the split. `risk_flags` is echoed
            # to the calling agent, so `_bare_baseline_flag` bars this text out of the flag — and
            # before 2026-07-30 it existed ONLY in the flag, so barring deleted it. The human was
            # told an amount was out of band and never told by how much, against what, or which
            # rule fired. `context` never reaches the agent (verified: no tool returns it; the
            # three `get_plan` callers are execution paths), so stating it here costs no oracle.
            context.append(
                f"{multiple:.1f}x the largest prior amount (prior max {_amount(prior_max)}, "
                f"threshold K={k}, n={n})."
            )
            flags.append(
                f"AMOUNT OUTSIDE RANGE: {multiple:.1f}x the largest prior amount for {party} "
                f"(prior max {_amount(prior_max)}, threshold K={k}, n={n})."
            )
    else:
        context.append("no band computed: prior maximum is not positive.")
    return {"context": context, "flags": flags, "source": source, "as_of": as_of}
