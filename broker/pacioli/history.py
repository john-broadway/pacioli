"""Party history — the READ half of PLAN-time contextual disclosure.

Named ``history``, not ``memory``: increment 1 stores nothing, so calling it memory would be the
module lying about itself in its own filename. Every plan reads live. There is no cache, no
staleness, and nothing inside the agent's write reach to tamper with.

Resolves per-doctype config, performs ONE bounded read, and converts any failure into a STATED
unavailability rather than silence. The decision of what to say belongs to
:mod:`pacioli.baseline`; this module only gets the numbers and hands them over.

ADVISORY ONLY. Nothing here can permit, refuse, or alter a write.

Design: docs/plans/internal/2026-07-30-party-baselines-design.md
"""
import os

from pacioli import baseline
from pacioli.erpnext import SUPPORTED_DOCTYPES

#: Re-exported from the pure core, which owns the single definition. Not redefined here: the number
#: appears in emitted text, and two defaults for one number is how the message and the query drift.
WINDOW_DEFAULT = baseline.WINDOW_DEFAULT

_ENV_FLAG = "PACIOLI_PARTY_HISTORY"


def enabled(env=None):
    """Opt-in, and exactly ``"1"``. A permissive truthy check would let a stray value switch on a
    feature that adds a read to every governed plan."""
    return (env if env is not None else os.environ).get(_ENV_FLAG) == "1"


def _inert():
    """Returned when the feature has nothing to say about this doctype at all. Distinct from
    :func:`baseline.unavailable`: no claim is being made, so ``context`` carries no line — but the
    shape still carries all four keys ``baseline.assess``/``baseline.unavailable`` return, because
    this is not the rare path: Sales Order and Journal Entry both still lack ``amount_field``
    (Sales Invoice, formerly a third example named here, gained one via ``base_grand_total`` once
    a live-bench probe found real submitted volume on it and none on Purchase Invoice or Payment
    Entry), and most of :data:`SUPPORTED_DOCTYPES`'s other configured doctypes lack ``amount_field``
    too, so a caller reading ``out["source"]`` unconditionally must not KeyError on it. Builds a
    FRESH dict with fresh lists on every call: a shared module-level literal would hand every
    caller the same list objects, and one caller's ``.append`` would poison every later inert
    return, process-wide.
    """
    return {"context": [], "flags": [], "source": "none", "as_of": None}


def _config(doctype):
    cfg = SUPPORTED_DOCTYPES.get(doctype) or {}
    party_field = cfg.get("party_field")
    amount_field = cfg.get("amount_field")
    date_field = cfg.get("date_field")
    if not (party_field and amount_field and date_field):
        return None
    return party_field, amount_field, date_field


def party_context(client, doctype, company, doc, window=WINDOW_DEFAULT, k=None):
    """One bounded read, then the pure core's verdict.

    Fails LOUD, never quiet: every failure path returns a stated line. The plan must never fail
    because this failed, so everything downstream of doctype-config resolution — reading the
    document's own fields, the read itself, summarizing it, and the assess call — sits inside one
    exception boundary below, not just the read call.

    A read that returns something other than a list (``None`` included) is a FAILED read, not a
    finding of zero prior documents, and is reported as unavailable rather than as novelty. A read
    that returns rows but none of them carry a usable amount is reported plainly as such — it must
    not be misreported as either a genuine first document or a real outside-band amount, since
    either misreading fabricates a claim the read never actually supported.
    """
    resolved = _config(doctype)
    if resolved is None:
        return _inert()
    party_field, amount_field, date_field = resolved

    try:
        party = (doc or {}).get(party_field)
        if not party:
            return baseline.unavailable(f"no {party_field} on this document")

        raw_amount = (doc or {}).get(amount_field)
        amount = baseline.numeric(raw_amount)
        if amount is None:
            if raw_amount is None:
                return baseline.unavailable(f"no {amount_field} on this document")
            return baseline.unavailable(
                f"{amount_field} on this document is not a number: {raw_amount!r}")

        rows = client.party_amount_history(
            doctype=doctype, company=company, party_field=party_field, party=party,
            amount_field=amount_field, date_field=date_field, limit=window)

        if not isinstance(rows, list):
            kind = "None" if rows is None else type(rows).__name__
            return baseline.unavailable(f"history read returned {kind}, not a list")

        n_rows = len(rows)
        usable_amounts = [r.get(amount_field) for r in rows if isinstance(r, dict)]
        summary = baseline.summarize(usable_amounts)

        if n_rows and summary["n"] == 0:
            # Rows came back — this is NOT a genuine zero-prior-documents finding — but not one of
            # them carried a usable amount. Neither novelty nor a real outside-band amount was
            # ever established, so neither may be claimed.
            return baseline.unavailable(
                f"{n_rows} prior document(s) for {party} returned but none carried a usable "
                f"{amount_field}")

        last_seen = None
        for row in rows:
            if isinstance(row, dict) and row.get(date_field):
                last_seen = row.get(date_field)
                break

        kwargs = {} if k is None else {"k": k}
        return baseline.assess(party=str(party), amount=amount, summary=summary, doctype=doctype,
                               last_seen=last_seen, window_hit=n_rows >= window,
                               window=window, **kwargs)
    except Exception as exc:  # noqa: BLE001 — a disclosure read must never break the plan
        return baseline.unavailable(f"{type(exc).__name__}: {exc}")
