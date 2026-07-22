# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — PLAN: the memorandum, TOCTOU freshness, and the closed-books refusal (pure core).

PLAN is the memorandum (Pacioli's *memoriale*): everything written down first, before any formal
entry. It records a draft's projected GL impact (from ERPNext's native preview) *before* any submit,
so a submit can't fire without a plan built first. Two guards live here:

  * **TOCTOU freshness** — the plan is bound to the draft's ``modified`` version; if the draft changed
    after planning, the plan (and its GL preview) no longer describe it, so ``submit`` must fail closed
    and demand a re-plan. Honest limit: this detects changes to *this document* only, not to linked
    master data (customer credit limit, item tax template, live FX) that can shift the real GL without
    bumping the doc's ``modified``. Documented, not silently assumed away.

  * **The closed books** — where ERPNext has closed the books (a closed Accounting Period, a Period-
    Closing-Voucher boundary, a company frozen-till date), the broker *refuses and says so* rather than
    discover it at submit or, worse, write in a closed book. It must **never** set ``adv_adj=True`` and **never**
    rewrite ``posting_date`` — all three date-gates are pure ``posting_date``-range checks against a
    *stored* date, so a future ``posting_date`` silently escapes them; refusing that is the whole point.

Dates are ISO ``YYYY-MM-DD`` strings (lexicographic compare = chronological). No frappe, no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# The period-lock boundaries the glue reads off the company/site and passes in. Each is an ISO date
# (a posting on or before it is inside a locked window) or absent.
_LOCK_KEYS = ("closed_period_until", "pcv_until", "frozen_until")

# Lexicographic date comparison is chronological ONLY for strictly zero-padded ISO dates:
# "2026-3-15" sorts *after* "2026-12-31", so a non-padded date would silently escape a lock. Every
# date is validated to this shape before any comparison; a malformed date is refused, never compared.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# BOM breadth (2026-07-21) — the declared-dateless sentinel. BOM is the first supported doctype
# that carries NO date field at all (neither ``posting_date`` nor ``transaction_date`` nor any
# other Date/Datetime field — zero across all 94 fields of ``bom.json``, v16 source), yet is fully
# submittable. The glue puts THIS value in the Plan's ``posting_date`` channel for such a doctype —
# and ONLY when :data:`pacioli.erpnext.SUPPORTED_DOCTYPES` explicitly declares
# ``"date_field": None`` (a source-verified pin), NEVER because a date read came back empty: an
# empty/missing date on a doctype that DOES declare a date field remains exactly as unverifiable —
# and exactly as refused — as before. Three properties are load-bearing:
#
#   * it is not a valid ISO date (fails ``_is_iso_date``), so a bug that leaks it into any other
#     date slot REFUSES rather than passes — the same fail-closed shape as a malformed date;
#   * it compares lexicographically GREATER than every ISO date (``_`` > ``9``), so any call site
#     that forgot to branch would fire its "in the future" refusal/flag loudly rather than let the
#     sentinel slide through a range check unnoticed — a wrong-but-loud failure mode, never a
#     silent pass (the glue's own call sites all branch explicitly; this is the backstop);
#   * it is compared by EQUALITY, never identity — the Plan round-trips through the store's
#     ``posting_date TEXT`` column, so an ``is`` check would silently break after rehydration.
NO_DATE_FIELD = "__no_date_field__"


def _is_iso_date(d):
    return isinstance(d, str) and bool(_ISO_DATE.match(d))


@dataclass(frozen=True)
class Plan:
    """A recorded dry-run. Built by the glue from the native GL preview; consumed by ``submit``.

    :param plan_id: opaque id the glue mints; the marker binds to it.
    :param target: the routed ``site/company`` the plan (and the eventual submit) is pinned to.
    :param docname: the ONE document this plan describes — ``submit`` refuses a name mismatch
        (``doc_version`` equality alone is coincidence-prone: two drafts can share a ``modified``).
    :param doc_version: the draft's ``modified`` at plan time — the TOCTOU anchor.
    :param posting_date: the draft's posting date (ISO) — checked against period locks. An
        internal, doctype-agnostic vocabulary name: the glue resolves which ERPNext field it came
        from (``posting_date``/``transaction_date``/``from_date`` — see
        :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`' ``date_field``); this core treats it as an
        opaque ISO-date value. For a doctype DECLARED dateless (``date_field: None`` — BOM, the
        first) the glue stores :data:`NO_DATE_FIELD` here instead, and :func:`check_red_line`
        passes it by its own explicit branch (see there for why that equals ERPNext, not
        weakens it).
    :param projected_gl: the preview rows (opaque to this core).
    :param risk_flags: advisory flags surfaced to the caller.
    :param ts: plan timestamp (supplied; this core has no clock).
    :param op: the ONE operation this plan (and any marker minted against it) authorizes —
        ``"submit"`` or ``"cancel"``. The execute tools refuse a cross-op plan: a human who
        consented to *cancelling* a posting has not consented to *making* one, and vice versa.
    :param doctype: the ONE ERPNext DocType this plan describes — ``"Sales Invoice"`` or
        ``"Purchase Invoice"``. Mirrors ``op``: the marker binds to a plan, the plan binds to ONE
        doctype, and ``check_doctype`` refuses a plan replayed against a different doctype's
        submit/cancel (a Sales Invoice plan must never authorize posting a Purchase Invoice, or
        vice versa). Defaults to ``"Sales Invoice"`` so pre-breadth plan constructions/tests are
        unaffected.
    :param graph: the ordered list of node dicts (doctype/docname/doc_version/posting_date/
        company/coverage/projected_gl) a ``cascade_cancel`` plan pins — the dependent-document
        chain the human consented to cancel, in cancel order. For a ``reconcile`` plan (F-R2),
        this instead carries the pinned ALLOCATION graph (one node per payment/invoice tuple —
        see ``pacioli.reconcile``'s module docstring for the node shape); empty for single-op
        (submit/cancel) plans.
    :param party_type: (``reconcile`` plans only) the ERPNext party type
        (``"Customer"``/``"Supplier"``) the reconcile call is scoped to. Blank for every other op.
    :param party: (``reconcile`` plans only) the party name. Blank for every other op.
    :param receivable_payable_account: (``reconcile`` plans only) the GL account the reconcile
        call is scoped to. Blank for every other op.
    :param company: (``reconcile`` plans only) the ERPNext company every allocation in ``graph``
        was verified to belong to — carried here (not just on the target pin) so
        ``_tool_reconcile`` can rebuild ``erpnext.reconcile()``'s call from the PINNED plan alone,
        never from execute-time args. Blank for every other op.
    """

    plan_id: str
    target: str
    doc_version: str
    posting_date: str
    projected_gl: list = field(default_factory=list)
    risk_flags: list = field(default_factory=list)
    ts: str = ""
    docname: str = ""
    op: str = "submit"
    doctype: str = "Sales Invoice"
    graph: list = field(default_factory=list)
    party_type: str = ""
    party: str = ""
    receivable_payable_account: str = ""
    company: str = ""


def new_plan(plan_id, target, doc_version, posting_date, projected_gl=None, risk_flags=None,
             ts="", docname="", op="submit", doctype="Sales Invoice", graph=None,
             party_type="", party="", receivable_payable_account="", company=""):
    """Pure constructor for a :class:`Plan`. ``graph`` is the ordered node list for a
    cascade_cancel or reconcile plan (empty for single-op plans). ``party_type``/``party``/
    ``receivable_payable_account``/``company`` are reconcile-only (F-R2); blank for every other op."""
    return Plan(
        plan_id=plan_id,
        target=target,
        doc_version=doc_version,
        posting_date=posting_date,
        projected_gl=list(projected_gl or []),
        risk_flags=list(risk_flags or []),
        ts=ts,
        docname=docname,
        op=op,
        doctype=doctype,
        graph=list(graph or []),
        party_type=party_type,
        party=party,
        receivable_payable_account=receivable_payable_account,
        company=company,
    )


def check_docname(plan, docname):
    """Refuse a plan presented for a different (or unbound/blank) document. ``(ok, reason)``."""
    if plan is None:
        return (False, "no plan")
    if not plan.docname or not isinstance(docname, str) or not docname.strip():
        return (False, "cannot verify the document: the plan or the request has no document name")
    if plan.docname != docname:
        return (False, "plan is for a different document; re-plan before submitting")
    return (True, None)


def check_op(plan, op):
    """Refuse a plan presented for a different operation. ``(ok, reason)``.

    The marker binds to a plan_id and the plan binds to ONE operation — this is what stops a
    consent grant from being laundered across the duality (a marker minted to cancel a posting
    must never authorize submitting one, and vice versa). A blank/missing ``op`` on either side
    is a refusal, never a pass-through."""
    if plan is None:
        return (False, "no plan")
    if not getattr(plan, "op", "") or not isinstance(op, str) or not op.strip():
        return (False, "cannot verify the operation: the plan or the request has no operation")
    if plan.op != op:
        return (False, f"plan {plan.plan_id!r} authorizes {plan.op!r}, not {op!r}; "
                       "a consent grant does not transfer between operations — re-plan")
    return (True, None)


def check_doctype(plan, doctype):
    """Refuse a plan presented for a different (or unbound/blank) ERPNext DocType. ``(ok, reason)``.

    The security headline of the breadth increment: a plan for ``(Sales Invoice, ACC-X)`` must
    NEVER authorize a submit/cancel of ``(Purchase Invoice, ACC-X)`` even if the docname happens
    to collide. Mirrors :func:`check_op` exactly — the executor and the plan must agree on
    doctype, and a blank/missing doctype on either side is a refusal, never a pass-through
    (``None == None`` must not read as verified)."""
    if plan is None:
        return (False, "no plan")
    if not getattr(plan, "doctype", "") or not isinstance(doctype, str) or not doctype.strip():
        return (False, "cannot verify the document type: the plan or the request has no doctype")
    if plan.doctype != doctype:
        return (False, f"plan {plan.plan_id!r} is for a different document type "
                       f"({plan.doctype!r}, not {doctype!r}); a consent grant does not transfer "
                       "between document types — re-plan for the correct doctype")
    return (True, None)


def check_fresh(plan, current_doc_version):
    """TOCTOU guard. ``(True, None)`` iff the draft's ``modified`` is unchanged since the plan was
    built; otherwise ``(False, reason)`` so ``submit`` fails closed and demands a re-plan.

    A missing version on *either* side is a refusal (an unverifiable version must never read as
    verified via ``None == None``)."""
    if plan is None:
        return (False, "no plan")
    if not current_doc_version or not plan.doc_version:
        return (False, "cannot verify document version: a version is missing; re-plan before submitting")
    if current_doc_version != plan.doc_version:
        return (False, "plan is stale: the document changed after planning; re-plan before submitting")
    return (True, None)


def check_red_line(posting_date, now_date, locks):
    """The closed-books refusal. ``(True, None)`` to allow, ``(False, reason)`` to refuse.

    ``now_date`` is the broker's own "as-of" ISO date (an *independent* pin, never the caller's).
    Refuses — deny-biased — a missing/malformed posting date; a posting date on/before any live
    period-lock boundary (closed period / PCV / frozen-till); a malformed lock boundary; and a
    *future* posting date while a lock is live (the silent-escape vector). When a lock is live and
    ``now_date`` can't be read, it **refuses** (does not skip the check). Refusal is the only safe
    answer at a lock — it never returns "bypass".

    **The declared-dateless pass (BOM breadth, 2026-07-21).** :data:`NO_DATE_FIELD` — the value
    the glue stores ONLY for a doctype whose :data:`pacioli.erpnext.SUPPORTED_DOCTYPES` entry
    explicitly declares ``"date_field": None`` (a source-verified pin, never an empty read) —
    passes, first branch, regardless of ``locks``. This is EQUAL to ERPNext, not weaker,
    verified from v16 source three ways: (1) the doctype carries no date field at all, so there
    is no date to range-check and no period for a posting to fall inside; (2) ERPNext's own
    accounting-period validation runs only for the 18 doctypes in ``hooks.py``'s
    ``period_closing_doctypes`` list — BOM is not one of them, so ERPNext never period-checks a
    BOM save; (3) ``check_freezing_date`` fires only inside ``general_ledger.py``'s GL-posting
    paths, and a dateless doctype in this broker's set posts no GL (``BOM(WebsiteGenerator)``
    has no ``make_gl_entries`` in its MRO). Refusing here would deny every submit/cancel of the
    doctype forever, protecting nothing ERPNext protects. The pass is BEFORE lock validation
    deliberately: the glue never reads locks for a dateless doctype (there is no date to build
    the Accounting-Period range query on — see ``tools.py``'s ``_locks_for``), so ``locks`` here
    is ``{}`` in every real flow; validating a hypothetical non-empty ``locks`` first would make
    this core's answer depend on data no caller can supply. An empty or malformed date that is
    NOT the sentinel refuses exactly as before — datelessness is a declared property of the
    doctype, never an inference from a missing value.
    """
    if posting_date == NO_DATE_FIELD:
        return (True, None)
    if not posting_date:
        return (False, "no posting_date: refusing to submit an undated posting")
    if not _is_iso_date(posting_date):
        return (False, f"posting_date {posting_date!r} is not a valid ISO (YYYY-MM-DD) date")
    # None / absent = genuinely no lock. A key present with ANY other value (incl. falsy "" / 0 /
    # False) must be validated, not skipped — else a glue bug that emits a falsy sentinel for a
    # live lock would silently no-op the closed-books check (the deny-bias this module holds everywhere).
    boundaries = {k: locks[k] for k in _LOCK_KEYS if locks and locks.get(k) is not None}
    for name, boundary in boundaries.items():
        if not _is_iso_date(boundary):
            return (False, f"period lock {name} has a malformed date {boundary!r}; refusing")
        if posting_date <= boundary:
            return (False, f"posting_date {posting_date} is within a locked period ({name} {boundary})")
    if boundaries:
        # A lock is live: we MUST be able to check for future-dating around it. No valid "now" → refuse.
        if not _is_iso_date(now_date):
            return (False, "cannot verify posting_date against live period locks: no valid current date")
        if posting_date > now_date:
            return (
                False,
                f"posting_date {posting_date} is in the future while period locks are live: "
                "refusing to date around period controls",
            )
    return (True, None)
