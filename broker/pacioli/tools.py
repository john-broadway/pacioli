# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — TOOLS: the MCP tool surface (glue, SDK-free).

The tool schemas and the dispatcher live here, with **no MCP SDK import**, so the whole surface is
unit-testable; ``server.py`` is a thin stdio adapter over :class:`PacioliBroker`.

The surface is deny-by-default (SPEC §4): three reads, the PLAN/execute pairs for both directions of
the duality (submit and cancel, each behind a plan + human marker), the amend re-draft, the
workflow transition (CONSENT's second gate), and two PROVE reads. **Minting a marker is
deliberately NOT a tool** — consent must come from outside the agent's own reach (the human runs
``pacioli mint`` in a terminal; the agent only ever *presents* a token it was handed). **Amend and
``request_workflow_transition`` alone take no marker**: each creates a reversible change (a
deletable draft; a workflow-state move that never touches ``docstatus``) — the irreversible act
remains submit, which demands its own plan + marker, and demanding consent for a reversible act
would dilute what the marker means. Both still write the intent+outcome receipt pair so the book
shows the full arc. Every dispatch returns a JSON-native dict with an ``ok`` flag — a governed
denial is a structured answer (stage + reason), never a traceback.

**CONSENT's second gate — Workflow-SoD (knowledge-pinned, not live-verified; see
``pacioli/workflow.py``).** When a company has configured an active ERPNext Workflow on the
document's doctype, that workflow becomes the company's own separation-of-duties law: the submit
tool is refused outright (the approving transition belongs to a human with the named role), and
the cancel tool is refused too if the company's own workflow configuration maps a state to
``doc_status "2"``. The broker may still perform non-approving transitions on the agent's behalf
via ``request_workflow_transition``. No workflow configured on the doctype = these two gates pass
silently and submit/cancel stay marker-governed exactly as before.

Two wrong-books guards live here because this is where routing meets documents: a plan records the
target it was built against and ``submit`` refuses a plan replayed at a different target; a target
with a ``company`` pin refuses to plan a document from any other company.

**Breadth (Purchase Invoice) — doctype-generic handlers, doctype-named tools.** The five
SI-baked handlers (get/list/submit/cancel/amend) are now ONE generic implementation each
(``_get_document``/``_list_documents``/``_submit_document``/``_cancel_document``/
``_amend_document``), each doctype-named tool (``*_sales_invoice`` / ``*_purchase_invoice``) a
thin wrapper pinning the doctype. The five existing ``*_sales_invoice`` tools keep their name,
schema, and behaviour — every test written against them still holds; the only edit is one clause in
the ``submit``/``cancel`` *descriptions* noting the new cross-doctype refusal. The four generically-named doc-scoped tools (``plan_submit``, ``plan_cancel``,
``workflow_status``, ``request_workflow_transition``) gain an OPTIONAL ``pacioli_doctype``
property (default ``"Sales Invoice"``, today's behaviour) resolved through
:func:`_resolve_doctype`, which refuses (structured deny, ``stage="request"``) any doctype outside
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES` — this is the broker's own "I've been built and tested
for these" allowlist, belt-and-suspenders alongside (never instead of) ``pacioli_guard``'s
per-credential ``resource_doctypes`` grant; either layer denying is enough to refuse the call.

**The security-critical gate — doctype-bind the plan.** :func:`pacioli.plan.check_doctype` is
wired into :meth:`PacioliBroker._governed_write` alongside ``check_docname``/``check_op``, BEFORE
the workflow gate and the spine: a plan built for Sales Invoice can never authorize a Purchase
Invoice submit/cancel, or vice versa, even if the two share a plan store and a docname were to
collide. Mirrors the existing cross-op guard (a cancel marker cannot authorize a submit) exactly.

**Every receipt carries ``doctype``.** The submit/cancel intent body is built inside
``spine.governed_submit``, which records ``plan.doctype`` on the intent (a one-line, doctype-agnostic
addition — the spine just records another field the plan already carries, exactly like
``docname``/``target``); the OUTCOME's ``result`` (built in this module's ``execute()`` closure)
carries it too, and the two receipts this module builds directly (``amend``, ``workflow_transition``)
carry it on both intent and outcome. So the ledger is self-describing on both sides for every
doctype. The doctype is *also* recoverable from the persisted (doctype-columned) plan via
``plan_id`` — belt and suspenders. None of this is the gate: enforcement is ``check_doctype`` above,
independent of what any receipt records; PROVE is audit, not the gate.

**Breadth (Payment Entry) — a third doctype, same generic handlers.** Five more doctype-named
tools (``*_payment_entry``) wrap the same generic handlers, pinned to Payment Entry
(``party_field="party"``). Two Payment-Entry-specific disclosures are added at THIS layer (never
in ``erpnext.py``, which stays doctype-blind): ``_payment_entry_risk_flags`` flags, at
``plan_submit`` time, any ``references`` row with a nonzero ``exchange_gain_loss`` (ERPNext's own
ledger preview creates AND SUBMITS a real, separate Exchange Gain/Loss Journal Entry mid-preview,
whose GL rows the projection never shows — projection-incomplete, disclosed) and any row already
at zero/negative ``outstanding_amount`` (ERPNext only ``frappe.msgprint``s this over REST — HTTP
200, no exception — so a governed PLAN surfaces what the bench itself only warns about); and
``_tool_plan_cancel`` discloses the blast radius for a Payment Entry cancel — a single voucher can
unwind N invoices at once, unlike SI/PI's one-document cancel — by listing every reference
(doctype, name, allocated_amount) the plan response's ``references`` key. Both read the draft's
OWN cached child-row fields (no extra bench call); neither is a gate — PLAN is disclosure, the
gates above are unchanged and untouched for this addition.

**Breadth (Journal Entry) — a fourth doctype, the first with no header-level party and the first
with its OWN gate (not just a disclosure).** Five more doctype-named tools (``*_journal_entry``)
wrap the same generic handlers, pinned to Journal Entry (``party_field=None`` — see
``pacioli/erpnext.py``). Two genuinely new things land here, both because Journal Entry's ERPNext
controller carves out a real bypass of Pacioli's founding law ("no debit without a credit") that
Sales/Purchase Invoice and Payment Entry never could (scout-je.md §5, headline risk #1):

* **A REFUSAL, not a disclosure.** ``voucher_type == "Exchange Gain Or Loss"`` is refused outright
  at BOTH ``plan_submit`` and ``submit_journal_entry``
  (:func:`_journal_entry_reserved_voucher_type_deny`) —
  this voucher type is meant to be produced only by ERPNext's own FX-revaluation tooling, and two
  independent ERPNext gates skip the debit==credit check for exactly this value. Cancel is NOT
  refused for it — ERPNext's own machinery routinely auto-cancels these (see the standing
  plan_cancel flag below), and refusing a legitimate cleanup would be perverse.
* **An INDEPENDENT balance check** (:func:`_journal_entry_balance_check`) — the broker sums the
  draft's own ``accounts`` child-row ``debit``/``credit`` fields itself and refuses a mismatch,
  at BOTH ``plan_submit`` (before any marker can be minted — the plan itself never records) and
  ``submit_journal_entry`` (the actual write, belt-and-suspenders even though ``check_fresh``
  already makes the second check logically redundant for an unmodified draft — this codebase's
  standing pattern is to re-verify every gate at the moment of the write, never trust only the
  plan-time pass). This is Pacioli's own founding law, enforced independently of whatever ERPNext's
  native preview or on-submit validation would have caught — never a replacement for either.

Two more Journal-Entry-specific disclosures, both advisory (never a gate), mirroring how the
Payment Entry ones above are layered on:

* ``plan_submit`` (:func:`_journal_entry_submit_risk_flags`) carries a STANDING note that Journal
  Entry's ``on_submit``-only checks (cheque info, credit limit, invoice-discounting status) are
  invisible to the native preview — a clean-looking plan can still fail at real submit (scout-je.md
  §3) — plus a CONDITIONAL flag when a Bank Entry draft is missing ``cheque_no``/``cheque_date``
  (ERPNext's own ``validate_cheque_info`` requires both for ``voucher_type == "Bank Entry"``,
  checked only at on_submit — confirmed from source, ``journal_entry.py:649-658``). A Cash Entry
  draft missing the same fields is flagged too, worded as the broker's OWN precaution — ERPNext's
  source does not actually enforce this for Cash Entry, only Bank Entry, and the flag says so
  rather than overclaiming what the bench itself checks.
* ``plan_cancel`` (:func:`_journal_entry_cancel_flags_for_settings`) reads
  ``Accounts Settings.unlink_payment_on_cancellation_of_invoice`` (a read,
  :meth:`pacioli.erpnext.ErpnextClient.get_accounts_settings` — an unreadable settings doc raises,
  refusing the whole plan, the same deny-bias as every other lock-adjacent read in this codebase)
  and flags it ON: turning cancel's blast radius from "refused by the generic backlink check" into
  "a silent raw-SQL unlink of other submitted Journal Entries/Payment Entries that reference this
  one, with no doc event firing on either" (scout-je.md §2, §5). A SECOND flag is always present,
  unconditionally: cancelling this Journal Entry auto-cancels any system-generated Exchange Gain Or
  Loss Journal Entries that reference it, with no separate consent (ERPNext's own
  ``cancel_exchange_gain_loss_journal``, called from both ``accounts_controller.on_cancel`` and
  ``JournalEntry.make_gl_entries(1)``).

**F-R1 — the settling-PE disclosure on cancel, widened to EVERY supported doctype** (pin sheet
``docs/plans/2026-07-07-fr1-settling-pe-disclosure.md``). The Journal-Entry-specific unlink flag
above was never the whole gap: ANY supported doctype's cancel can silently unlink a settling
voucher (a Payment Entry, most commonly — ``auto_cancel_exempted_doctypes``) that ERPNext's own
blast-radius check (``get_submitted_linked_docs``, the refusal ``_tool_plan_cancel`` already makes
above) structurally cannot surface, since the exempt list removes it from that traversal's
allowed-source set. ``_tool_plan_cancel`` and ``_tool_plan_cascade_cancel`` now read
:meth:`pacioli.erpnext.ErpnextClient.get_settling_references` for every doctype and flag each
settling voucher found via :func:`_settling_reference_risk_flags`, in one of two exact voices
depending on the (now doctype-generic) Accounts Settings read above: ON — "cancelling will
SILENTLY UNLINK ``<voucher_type>`` ``<voucher_no>``'s allocation of ``<amount>`` against this
document…"; OFF — "ERPNext will REFUSE this cancel (LinkExistsError) while ``<voucher_no>``
references it". No settling rows = no new flags (the control case). An unreadable settling-
reference read OR an unreadable Accounts Settings read both refuse the WHOLE plan (deny-biased,
same as every other lock-adjacent read); the Accounts Settings read itself is now made ONCE per
plan (single-op) or ONCE per graph (cascade — generalized from the prior JE-only ``je_settings``
memo variable), while the settling-reference read is made once per plan and once PER NODE in a
cascade (each node has its own settlement blast radius). The Journal-Entry-specific EG-auto-cancel
note and unlink flag above are unchanged, byte-for-byte — this is additional disclosure, not a
replacement. **Guard implication: a NEW required grant** — ``Payment Ledger Entry`` read — is
BREAKING for an existing scoped credential (see CHANGELOG/README; ``pacioli doctor`` gains
``probe_payment_ledger_read``).

**The seal gate (CONTAIN's teeth, Task 2 — docs/plans/2026-07-14-close-half3-seal-slice.md).**
:data:`READ_ONLY_TOOLS` classifies every get_*/list_* tool plus ``workflow_status``,
``prove_verify``, and ``prove_orphans`` as reads; every OTHER dispatched tool is a **governed
surface** and is seal-gated in :meth:`PacioliBroker.dispatch`, via :meth:`PacioliBroker._seal_gate`,
BEFORE its handler is even looked up — a sealed store (an operator's own ``pacioli seal``, or a
future ``close --respond --envelope``-escalated CONTAIN) refuses the write outright, the handler
never runs, nothing is claimed, no marker is spent (the F-C2 invariant, extended: consent is spent
by commitment, never by refusal). New tools are **born gated** — the classification is an
allowlist of reads, not of writes, so a tool nobody consciously classified read-only is seal-gated
by construction, with zero further code change. An unreadable seal state — ``BrokerStore.
seal_state`` itself raising once the store has resolved cleanly — denies too, the same deny-bias
as every other lock-adjacent read in this codebase; it is never treated as "probably unsealed". A
failure resolving the TARGET or STORE (an unknown/ambiguous ``pacioli_target``, a torn/corrupt
store file) is a DIFFERENT thing — the pre-existing failure taxonomy (review F1, Task 2 review):
:meth:`PacioliBroker._seal_gate` re-raises it rather than swallowing it into ``stage="seal"``, so
:meth:`PacioliBroker.dispatch`'s own pre-existing exception clauses produce the SAME stage
(``"request"``/``"store"``) a read-only tool's own ``_route`` call would have produced for the
identical failure — resolution precedes seal knowledge, so it is never reported as "the seal did
this". Read-only tools skip this path entirely, so sealing — even a CORRUPT seal state — never
breaks a read; the confession has to stay legible, or the seal would hide the very books that
explain it. The mint CLI (``pacioli.cli.cmd_mint``) carries an independent, keyless, content-only
pre-check (:func:`format_seal_refusal` renders both refusals identically) — defense-in-depth only;
this dispatch-time gate is the authoritative one. The gate and the write it guards each open the
store independently, so a seal landing between the two is not re-checked at write time — see
:meth:`PacioliBroker._seal_gate`'s docstring for the full TOCTOU ruling (F2, Task 2 review).
"""
from __future__ import annotations

import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from pacioli import consent  # noqa: F401 — re-exported for the CLI's mint path
from pacioli import workflow
from pacioli.amend import seat_conflict
from pacioli.cascade import build_cascade, run_cascade
from pacioli.erpnext import (ErpnextError, JOURNAL_ENTRY, PAYMENT_ENTRY, PURCHASE_INVOICE,
                             SALES_INVOICE, SUPPORTED_DOCTYPES)
from pacioli.plan import check_doctype, check_docname, check_op, check_red_line, new_plan
from pacioli.reconcile import check_allocation, run_reconcile
from pacioli.registry import RegistryError
from pacioli.spine import governed_submit
from pacioli.store import StoreCorruptError, SubmitEffects

_TARGET_PROP = {
    "pacioli_target": {
        "type": "string",
        "description": "Registry target to route to (omit to use the default target).",
    }
}

_DOCTYPE_PROP = {
    "pacioli_doctype": {
        "type": "string",
        "description": "ERPNext DocType to operate on: 'Sales Invoice', 'Purchase Invoice', "
                       "'Payment Entry', or 'Journal Entry' (omit for 'Sales Invoice', today's "
                       "default). A doctype this broker is not built and tested for is refused.",
    }
}

# Journal Entry's system-reserved voucher_type (scout-je.md §5, headline risk #1): ERPNext's OWN
# debit==credit invariant is bypassable for this exact voucher_type at TWO independent gates
# (validate_total_debit_and_credit, journal_entry.py:937-944; general_ledger.process_debit_
# credit_difference, general_ledger.py:485-503) — it is meant to be produced ONLY by ERPNext's own
# FX-revaluation tooling, never authored via the API. Refused outright at plan AND submit — never
# a value this broker will plan or post, balanced or not.
_JE_RESERVED_VOUCHER_TYPE = "Exchange Gain Or Loss"

# --- doctype-descriptor spine: the mechanical tool surface generates from these rows ------------
# Adding a supported doctype = ONE row here (+ walk its hazards in the disclosure layer, which is
# deliberately NOT generated). The transport verbs are already doctype-parametric (erpnext.py
# client + the _<verb>_document helpers); this lifts that parametricity to tool REGISTRATION so a
# doctype's get/list/submit/cancel/amend surface appears from its descriptor instead of being
# hand-enumerated. Every mechanical verb's inputSchema is uniform across doctypes (verified
# 2026-07-08); only the doctype label in the description and the doctype constant in the wrapper
# vary. See docs/plans/2026-07-08-doctype-descriptor-spine.md.


@dataclass(frozen=True)
class DoctypeDescriptor:
    """One supported ERPNext doctype's mechanical tool surface, by construction."""
    doctype: str        # ERPNext label, e.g. "Sales Invoice"
    slug: str           # singular tool slug, e.g. "sales_invoice"
    plural: str         # list-tool slug, e.g. "sales_invoices"
    label_plural: str   # human plural for the list description, e.g. "Sales Invoices"


DESCRIPTORS = (
    DoctypeDescriptor(SALES_INVOICE, "sales_invoice", "sales_invoices", "Sales Invoices"),
    DoctypeDescriptor(PURCHASE_INVOICE, "purchase_invoice", "purchase_invoices", "Purchase Invoices"),
    DoctypeDescriptor(PAYMENT_ENTRY, "payment_entry", "payment_entries", "Payment Entries"),
    DoctypeDescriptor(JOURNAL_ENTRY, "journal_entry", "journal_entries", "Journal Entries"),
)


def _schema_get():
    return {"type": "object",
            "properties": {"name": {"type": "string", "description": "Document name."},
                           **_TARGET_PROP},
            "required": ["name"]}


def _schema_list():
    return {"type": "object",
            "properties": {"filters": {"type": "array", "items": {"type": "array"},
                                       "description": "Frappe filter triples."},
                           "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                                     "default": 20},
                           **_TARGET_PROP}}


def _schema_write(name_desc, plan_desc):
    """The submit/cancel schema. Structurally identical for both; only the name/plan_id hint text
    differs (submit wants a DRAFT + a plan_submit id, cancel wants a SUBMITTED doc + a plan_cancel
    id) — preserved verbatim from the hand-written surface."""
    return {"type": "object",
            "properties": {"name": {"type": "string", "description": name_desc},
                           "plan_id": {"type": "string", "description": plan_desc},
                           "marker": {"type": "string",
                                      "description": "The single-use consent token a human handed you."},
                           **_TARGET_PROP},
            "required": ["name", "plan_id", "marker"]}


def _schema_amend():
    return {"type": "object",
            "properties": {"name": {"type": "string", "description": "Cancelled document name."},
                           **_TARGET_PROP},
            "required": ["name"]}


# verb -> (tool-name builder, schema builder, description template, generic helper method name).
# The name builder and label differ per verb; the schema/helper are the doctype-blind spine.
_MECHANICAL_VERBS = {
    "get": (lambda d: f"get_{d.slug}", _schema_get,
            "Read one {label} (permission-scoped, read-only).", "_get_document"),
    "list": (lambda d: f"list_{d.plural}", _schema_list,
             'List {label_plural} (read-only). Optional Frappe filters, e.g. '
             '[["status","=","Draft"]].', "_list_documents"),
    "submit": (lambda d: f"submit_{d.slug}",
               lambda: _schema_write("Draft document name.", "From plan_submit."),
               "Submit a {label} under a recorded plan and a live human-minted consent marker. "
               "Refuses a stale plan, a period-lock violation, a plan bound to a different document "
               "or document type, or a missing/spent/mismatched marker. This is one of the two "
               "state-changing tools.", "_submit_document"),
    "cancel": (lambda d: f"cancel_{d.slug}",
               lambda: _schema_write("Submitted document name.", "From plan_cancel."),
               "Cancel a submitted {label} under a recorded cancel-plan and a live human-minted "
               "consent marker (UNDO \u2014 docstatus 1 \u2192 2). Refuses a stale plan, a "
               "period-lock violation, a missing/spent/mismatched marker, a plan bound to a "
               "different document or document type, or a marker minted for any other operation. "
               "ERPNext's own cancel-blocks are honored, never bypassed.", "_cancel_document"),
    "amend": (lambda d: f"amend_{d.slug}", _schema_amend,
              "Create the corrected re-draft of a CANCELLED {label} (UNDO's second half): a new "
              "DRAFT copied from it with amended_from set. Reversible \u2014 nothing posts, "
              "deleting the draft undoes it \u2014 so no consent marker is required; submitting "
              "the corrected draft is the irreversible step and demands its own plan_submit + "
              "human-minted marker. Under an active ERPNext Workflow the draft is seated at the "
              "workflow's initial state (disclosed as workflow_seat), so it is born with legal "
              "transitions. Refuses an uncancelled source, a source that already has an "
              "amendment (any docstatus), a wrong-books company, and ambiguous/malformed/"
              "unseatable workflow configuration. Writes the intent+outcome receipt pair.",
              "_amend_document"),
}


def _generate_mechanical_tools():
    """The 20 (verb x doctype) mechanical tools, generated from DESCRIPTORS. Every call to a schema
    builder yields a fresh ``properties`` dict, so no two tools share the mutable outer schema. (The
    ``_TARGET_PROP`` leaf value is spread into each — one shared leaf object — exactly as the
    pre-refactor hand-written surface did; TOOLS is built once at import and never mutated.)"""
    out = []
    for d in DESCRIPTORS:
        for verb, (name_of, schema_of, desc_tmpl, _helper) in _MECHANICAL_VERBS.items():
            out.append({
                "name": name_of(d),
                "description": desc_tmpl.format(label=d.doctype, label_plural=d.label_plural),
                "inputSchema": schema_of(),
            })
    return out


_GENERIC_TOOLS = [
    {
        "name": "workflow_status",
        "description": "Read the ERPNext Workflow (if any) governing this document: whether one "
                       "is active, the document's current workflow state, the legal transitions "
                       "from here (each flagged approving vs non-approving, with its role and "
                       "self-approval setting), and an honest separation-of-duties risk note. "
                       "Read-only; unrelated to the plan/marker flow. No active workflow returns "
                       "workflow_active=false, not an error. pacioli_doctype selects the "
                       "document's DocType (default Sales Invoice).",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Document name."},
                           **_TARGET_PROP, **_DOCTYPE_PROP},
            "required": ["name"],
        },
    },
    {
        "name": "plan_submit",
        "description": "PLAN a document submit: dry-run the draft via ERPNext's native ledger "
                       "preview and record the plan. Returns plan_id, the projected GL impact, "
                       "and risk flags. Nothing is posted. A human then mints a consent marker "
                       "for the plan_id, out of band. pacioli_doctype selects the document's "
                       "DocType (default Sales Invoice) — the plan is BOUND to it, and the "
                       "matching submit_<doctype> tool must be used to consume it.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Draft document name."},
                           **_TARGET_PROP, **_DOCTYPE_PROP},
            "required": ["name"],
        },
    },
    {
        "name": "plan_cancel",
        "description": "PLAN a document cancel (UNDO): read the posting's live GL rows (what "
                       "the cancel unwinds), check the linked-submitted-documents blast radius, "
                       "and record the plan. Refuses when other submitted documents link to this "
                       "one (use plan_cascade_cancel to govern the whole graph in one consent). "
                       "Nothing is cancelled. A human then mints a consent marker for the "
                       "plan_id, out of band — a cancel marker never authorizes a submit, or "
                       "vice versa. pacioli_doctype selects the document's DocType (default "
                       "Sales Invoice) — the plan is BOUND to it.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Submitted document name."},
                           **_TARGET_PROP, **_DOCTYPE_PROP},
            "required": ["name"],
        },
    },
    {
        "name": "plan_cascade_cancel",
        "description": "PLAN a CASCADE cancel: discover the full submitted-dependent graph of a "
                       "document, order it (dependents first, the target last), and record the plan "
                       "for the WHOLE ordered graph. Refuses a cycle or a graph over the cap. Nothing "
                       "is cancelled. A human then mints ONE consent marker for the plan_id that "
                       "authorizes exactly this enumerated graph. pacioli_doctype selects the "
                       "target's DocType (default Sales Invoice). Any doctype may appear in the graph; "
                       "each node is labeled 'modeled' (Sales/Purchase Invoice) or 'generic'.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Submitted target document name."},
                           **_TARGET_PROP, **_DOCTYPE_PROP},
            "required": ["name"],
        },
    },
    {
        "name": "cascade_cancel",
        "description": "Execute a recorded cascade-cancel plan under a live human-minted marker: "
                       "cancel every document in the frozen graph in order (dependents first, target "
                       "last, docstatus 1 -> 2). Re-checks the graph is unchanged since planning and "
                       "each node's freshness + period-lock before its cancel. Fail-stop: on the first "
                       "failure it STOPS and returns exactly what was cancelled and where it stopped "
                       "(ok is true only if ALL cancelled). The marker is spent if any document was "
                       "cancelled. ERPNext's own cancel-blocks are honored, never bypassed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Submitted target document name."},
                "plan_id": {"type": "string", "description": "From plan_cascade_cancel."},
                "marker": {"type": "string",
                           "description": "The single-use consent token a human handed you."},
                **_TARGET_PROP,
            },
            "required": ["name", "plan_id", "marker"],
        },
    },
    {
        "name": "plan_reconcile",
        "description": "PLAN a governed reconciliation (settling payments against invoices): "
                       "the agent proposes specific (payment, invoice, "
                       "amount) tuples; the broker reads each named invoice and payment FRESH, "
                       "discloses the settlement (per row: which payment settles which invoice, "
                       "the amount, live pre-outstanding -> projected post-outstanding), a "
                       "standing note that reconciliation bypasses ERPNext's own freeze belts "
                       "(the broker enforces the closed-books refusal itself, ERPNext will not), "
                       "and a note naming the possible system Journal Entry side-effects "
                       "(exchange gain/loss; credit/debit note). Nothing is posted. A human then "
                       "mints a consent marker for the plan_id, out of band — it binds to "
                       "exactly this pinned allocation set; reconcile never re-reads the "
                       "allocation from the caller.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "party_type": {"type": "string",
                               "description": "ERPNext party type, e.g. 'Customer' or 'Supplier'."},
                "party": {"type": "string", "description": "The party name."},
                "company": {"type": "string",
                            "description": "The ERPNext company every allocation must belong to."},
                "receivable_payable_account": {
                    "type": "string",
                    "description": "The GL account (e.g. Debtors/Creditors) reconciliation is "
                                   "scoped to."},
                "allocations": {
                    "type": "array",
                    "description": "The specific settlements proposed: each row names one "
                                   "payment and one invoice to settle it "
                                   "against, and the amount. The broker validates and owns the "
                                   "final allocation graph — it does not auto-match.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "payment_type": {"type": "string",
                                             "description": "'Payment Entry'. Journal-Entry "
                                                            "payments are a deferred extension, "
                                                            "refused at plan and execute."},
                            "payment_no": {"type": "string",
                                          "description": "The Payment Entry name."},
                            "invoice_type": {"type": "string",
                                             "description": "'Sales Invoice' or 'Purchase Invoice'."},
                            "invoice_no": {"type": "string", "description": "The invoice name."},
                            "allocated_amount": {"type": "number",
                                                 "description": "The amount to settle."},
                        },
                        "required": ["payment_type", "payment_no", "invoice_type", "invoice_no",
                                    "allocated_amount"],
                    },
                },
                **_TARGET_PROP,
            },
            "required": ["party_type", "party", "company", "receivable_payable_account",
                        "allocations"],
        },
    },
    {
        "name": "reconcile",
        "description": "Execute a recorded reconcile plan under a live human-minted marker: "
                       "settle exactly the pinned allocation set from plan_reconcile. The "
                       "allocation is NEVER re-supplied here — only plan_id and marker travel — "
                       "it comes only from the pinned plan graph (the broker's safety control: "
                       "the agent proposes at plan time, the broker owns and constructs the "
                       "write). Re-checks per-row freshness, closed-books, and the allocation "
                       "ceiling against a FRESH read (never the plan's disclosed numbers) before "
                       "any write; one reconcile call across the whole set; confirms via a "
                       "readback of each invoice's real post-write outstanding, never the call's "
                       "own result body.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "From plan_reconcile."},
                "marker": {"type": "string",
                           "description": "The single-use consent token a human handed you."},
                **_TARGET_PROP,
            },
            "required": ["plan_id", "marker"],
        },
    },
    {
        "name": "request_workflow_transition",
        "description": "Perform a NON-APPROVING ERPNext Workflow transition (e.g. Draft -> "
                       "Pending Approval) on a document governed by an active Workflow. "
                       "Refuses any transition whose next state carries docstatus 1 or 2 — that "
                       "approving transition belongs to a human with the named role, never the "
                       "broker — and refuses an action that isn't legal from the document's "
                       "current state, naming the legal ones. Reversible (a workflow-state move, "
                       "never a docstatus change), so it takes no consent marker, but writes the "
                       "intent+outcome receipt pair like every other mutation. pacioli_doctype "
                       "selects the document's DocType (default Sales Invoice).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Document name."},
                "action": {"type": "string",
                           "description": "The workflow transition's action name."},
                **_TARGET_PROP, **_DOCTYPE_PROP,
            },
            "required": ["name", "action"],
        },
    },
    {
        "name": "prove_verify",
        "description": "Verify the receipt chain (PROVE). Optionally check the head against an "
                       "off-box-recorded anchor.",
        "inputSchema": {
            "type": "object",
            "properties": {"expected_head": {"type": "string",
                                             "description": "Off-box-recorded head hmac."},
                           **_TARGET_PROP},
        },
    },
    {
        "name": "prove_orphans",
        "description": "Intent receipts with no committed outcome — mutations whose success was "
                       "never proven; reconcile each against the document's real docstatus.",
        "inputSchema": {"type": "object", "properties": {**_TARGET_PROP}},
    },
]

TOOLS = _generate_mechanical_tools() + _GENERIC_TOOLS


def tool_names():
    return [t["name"] for t in TOOLS]


# --- the seal gate's classification (deny-biased, Task 2 — docs/plans/2026-07-14-close-half3-
# seal-slice.md) --------------------------------------------------------------------------------
# Every dispatched tool NOT in this set is a GOVERNED SURFACE and is seal-gated in dispatch(),
# below: a new tool is born gated the moment it exists — nobody has to remember to add it to a
# gate list, because the gate list IS "everything else". Someone has to consciously classify a
# tool read-only by adding it here; the alternative (an allowlist of governed tools that a new
# write-tool might simply never get added to) is exactly the drift this shape refuses to allow.
# Derived from the same descriptor/verb tables the tool surface itself is generated from — never
# hand-listed — so this can never silently drift from the real get_*/list_* names as doctypes are
# added (a 5th DESCRIPTOR row automatically grows this set, no second edit required).
READ_ONLY_TOOLS = frozenset(
    {_MECHANICAL_VERBS["get"][0](d) for d in DESCRIPTORS}
    | {_MECHANICAL_VERBS["list"][0](d) for d in DESCRIPTORS}
    | {"workflow_status", "prove_verify", "prove_orphans"}
)


def _now_epoch():
    return time.time()


def _now_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _deny(reason, stage="request"):
    return {"ok": False, "stage": stage, "reason": str(reason)}


def format_seal_refusal(state):
    """Render the shared refusal text for a sealed broker, from a
    :meth:`pacioli.store.BrokerStore.seal_state` dict (``{"sealed", "since", "reason", "source",
    "seq", "cause"}``). Shared by :meth:`PacioliBroker._seal_gate` (the AUTHORITATIVE, keyed
    dispatch-time gate every governed tool refuses through) and the mint CLI's keyless pre-check
    (``pacioli.cli.cmd_mint`` — defense-in-depth only, HMAC-unverified) so the two refusals never
    drift apart in wording — only in which surface a caller can actually reach. Names since/
    reason/source always, and ``cause`` only when the state carries one (a genuinely fail-closed
    verdict — zero rows, a rollback gap, an unverifiable HMAC — rather than a clean operator
    seal), plus the exact command that clears it."""
    parts = [f"since {state['since']}", f"reason {state['reason']!r}",
             f"source {state['source']!r}"]
    if state.get("cause"):
        parts.append(f"cause {state['cause']!r}")
    return ("broker is SEALED (" + ", ".join(parts) + ") — every governed write is refused while "
            "sealed; a human operator can clear it: pacioli unseal --reason <why>")


def _record_committed_or_confess(store, intent, result, landed_name, tool):
    """Record a markerless ``"committed"`` outcome for an act that ALREADY LANDED on the bench
    (``result`` in hand — the amend draft was created / the workflow transition applied). On a
    store-write failure, retry once; if it still cannot record, return a deny-biased structured
    result rather than let the exception crash past ``dispatch()`` as a raw traceback, and rather
    than claim a clean success the receipt chain does not back (John's ruling 2026-07-10): ``ok:
    False``, name the landed doc, tell the caller NOT to retry — it already succeeded, and a repeat
    is denied anyway (``find_amendments`` for amend, frappe's own state machine for a workflow
    move) — and note the intent is now an orphan for hand-reconciliation (``prove.orphans``). The
    act is NOT lost: it happened on the bench and the recorded intent surfaces it. Returns ``None``
    on a clean record (the caller returns its own ``ok:True``), else the deny dict to return as-is.
    Markerless-only: there is no consent grant to settle, so ``final_marker`` is always ``None`` —
    the marker cores use :func:`pacioli.spine._settle` instead (which also degrades status)."""
    last = None
    for _ in (1, 2):  # the original attempt + one retry (a transient store error may clear)
        try:
            store.record_outcome(intent, "committed", result, final_marker=None)
            return None
        except Exception as exc:  # noqa: BLE001 — a post-landing outcome write must never crash past here
            last = exc
    return {"ok": False, "stage": "store", "result": result,
            "reason": (f"the {tool} LANDED on the bench ({landed_name}) but its outcome receipt "
                       f"could not be durably recorded ({last}); do NOT retry — it already "
                       "succeeded (a repeat is refused anyway); the intent is an orphan, reconcile "
                       "it by hand (prove_orphans)")}


def _resolve_doctype(args):
    """Resolve+validate the optional ``pacioli_doctype`` arg for the four generically-named
    doc-scoped tools (``plan_submit``, ``plan_cancel``, ``workflow_status``,
    ``request_workflow_transition``). Missing/blank defaults to :data:`SALES_INVOICE` — today's
    behaviour, unchanged. Anything outside :data:`SUPPORTED_DOCTYPES` is a structured deny
    (``stage="request"``) naming the supported set — the broker's own "I've been built and tested
    for these" allowlist, belt-and-suspenders alongside (never instead of) ``pacioli_guard``'s
    per-credential ``resource_doctypes`` grant. Returns ``(doctype, None)`` or ``(None, deny)``.

    A missing or blank ``pacioli_doctype`` defaults to Sales Invoice. A *present-but-non-string*
    value (a number, list, dict — a malformed or hostile MCP argument) is a structured deny, not a
    silent default: the same deny-bias :func:`pacioli.plan.check_doctype` applies to a request-side
    doctype, so a client that fails to serialise the field as a string gets a named refusal, never a
    quietly-substituted Sales Invoice plan it did not ask for."""
    raw = args.get("pacioli_doctype")
    if raw is not None and not isinstance(raw, str):
        return None, _deny(
            f"pacioli_doctype must be a string (got {type(raw).__name__}); omit it for the default "
            f"({SALES_INVOICE!r}) or name a supported doctype", stage="request")
    doctype = raw.strip() if isinstance(raw, str) and raw.strip() else SALES_INVOICE
    if doctype not in SUPPORTED_DOCTYPES:
        return None, _deny(
            f"unsupported pacioli_doctype {doctype!r}; this broker is built and tested for: "
            f"{', '.join(sorted(SUPPORTED_DOCTYPES))} — a doctype outside this set is refused "
            "here even if a credential's own resource grant would otherwise allow it",
            stage="request")
    return doctype, None


def _coerce_float(val):
    """Deny-biased numeric coercion for a bench-read amount field (F-R2): a real number (never a
    bool — ``isinstance(True, int)`` is ``True`` in Python, the same trap ``reconcile.py``'s own
    ``check_allocation`` guards against) coerces; anything missing, malformed, or the wrong shape
    returns ``None`` so the caller refuses rather than silently defaulting to ``0.0`` or crashing
    on an uncaught ``TypeError``/``ValueError``. Shared by ``_tool_plan_reconcile``'s fresh reads
    and ``_tool_reconcile``'s live/readback effects — one coercion rule, not two copies that could
    drift apart on what counts as "unverifiable"."""
    if isinstance(val, bool) or not isinstance(val, (int, float, str)):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _ambiguous_workflow_reason(ambiguous, doctype):
    """Shared refusal text for a :class:`pacioli.workflow.Ambiguous` sentinel — used wherever a
    tool reads workflow config directly (``workflow_status``, ``request_workflow_transition``);
    ``check_submit_gate`` builds its own equivalent for the ``_governed_write`` gate."""
    names = ", ".join(repr(n) for n in ambiguous.names)
    return (f"{len(ambiguous.names)} active Workflows govern {doctype!r} "
           f"({names}) — ambiguous configuration, refusing rather than guessing")


def _malformed_workflow_reason(malformed, doctype):
    """Shared refusal text for a :class:`pacioli.workflow.Malformed` sentinel — same consumers
    as :func:`_ambiguous_workflow_reason`. A malformed workflow body is an unverifiable gate
    source: it refuses, it never reads as 'no workflow configured'."""
    return (f"workflow configuration for {doctype!r} is malformed ({malformed.detail}) — "
            "an unverifiable gate source refuses, it never reads as 'no workflow'")


def _resolve_active_workflow(client, doctype):
    """The shared read+sentinel gate for every tool that consumes workflow config DIRECTLY
    (``workflow_status``, ``_amend_document``, ``request_workflow_transition``): fetch the active
    workflows, resolve through :func:`pacioli.workflow.find_active`, and turn the two refusal
    sentinels into the one shared deny. Returns ``(active, deny)`` — ``deny`` is the structured
    refusal to return as-is (non-None ONLY for Ambiguous/Malformed); ``active`` is the workflow
    dict or ``None`` (no workflow configured), whose meaning stays each caller's decision
    (status reports inactive, amend proceeds unseated, transition refuses). Extracted (review
    finding [7]) so a future refusal-taxonomy change lands ONCE — a hand-replicated site that
    misses it would diverge silently. ``_plan_workflow_risk`` deliberately does NOT use this:
    planning is a read, so it flags instead of denying."""
    active = workflow.find_active(client.get_active_workflows(doctype))
    if isinstance(active, workflow.Malformed):
        return (None, _deny(_malformed_workflow_reason(active, doctype), stage="workflow"))
    if isinstance(active, workflow.Ambiguous):
        return (None, _deny(_ambiguous_workflow_reason(active, doctype), stage="workflow"))
    return (active, None)


def _payment_entry_risk_flags(doc):
    """Payment Entry-specific PLAN-time disclosure (scout-pe.md §3, §5). ERPNext's own preview
    (:meth:`pacioli.erpnext.ErpnextClient.ledger_preview`) is fully doctype-generic and reused
    unchanged for Payment Entry, but two things it does NOT surface must be disclosed here:

    * A ``references`` row with a nonzero ``exchange_gain_loss`` makes ERPNext's OWN preview call
      ``make_gl_entries()``, which creates AND SUBMITS a real, separate Exchange Gain/Loss
      Journal Entry mid-preview (rolled back only at the very end,
      ``accounts_controller.make_exchange_gain_loss_journal``) — a side-effecting "dry run". The
      preview response is also filtered to this doctype/docname, so that JE's own GL rows never
      appear in ``projected_gl``: the projection is incomplete for exactly this case, not merely
      side-effecting.
    * A ``references`` row already at zero/negative ``outstanding_amount`` is something ERPNext
      itself only ``frappe.msgprint``s about (``validate_paid_invoices``) — non-blocking over
      REST (HTTP 200, no exception) — so a governed PLAN discloses what the bench only warns
      about.

    Reads the draft's OWN cached fields on each references row (no extra bench call) — the same
    fields ERPNext's own ``validate_allocated_amount`` falls back to for non-Customer/Supplier
    party types. Advisory only: these are ``risk_flags``, never a refusal — PLAN is a read."""
    flags = []
    for row in doc.get("references") or []:
        if not isinstance(row, dict):
            continue
        ref = f"{row.get('reference_doctype') or '?'} {row.get('reference_name') or '?'}"
        gain_loss = row.get("exchange_gain_loss")
        if isinstance(gain_loss, (int, float)) and not isinstance(gain_loss, bool) \
                and gain_loss != 0:
            flags.append(
                f"reference {ref!r}: nonzero exchange_gain_loss ({gain_loss}) — ERPNext's own "
                "preview creates AND SUBMITS a real Exchange Gain/Loss Journal Entry mid-preview "
                "(rolled back only at the very end) whose GL rows this projection does not "
                "include — projection-incomplete, disclosed")
        outstanding = row.get("outstanding_amount")
        if isinstance(outstanding, (int, float)) and not isinstance(outstanding, bool) \
                and outstanding <= 0:
            flags.append(
                f"reference {ref!r}: already at zero/negative outstanding ({outstanding}) — "
                "ERPNext only warns (frappe.msgprint, HTTP 200) and does not refuse this over "
                "REST")
    return flags


def _payment_entry_cancel_references(doc):
    """Payment Entry cancel blast-radius disclosure (the task's second PE requirement): list
    every reference row (doctype, name, allocated_amount) this Payment Entry settles, so a human
    reviewing a cancel plan sees which invoices/orders will have their outstanding_amount
    reverted — a single Payment Entry cancel can touch N of them at once, unlike SI/PI's
    one-document cancel. Reads the draft's own cached references (no extra bench call);
    disclosure only, not a gate — the blast-radius REFUSAL (other submitted docs linking to this
    one) is the existing linked-docs check above, unchanged."""
    return [
        {"reference_doctype": row.get("reference_doctype"),
         "reference_name": row.get("reference_name"),
         "allocated_amount": row.get("allocated_amount")}
        for row in (doc.get("references") or []) if isinstance(row, dict)
    ]


# Float-sum tolerance for _journal_entry_balance_check — a fixed-point currency amount summed as
# a Python float can pick up sub-cent noise across many rows; this is NOT an accounting allowance
# (unlike ERPNext's own JE-specific 5.0/10**precision round-off tolerance) — it exists purely to
# absorb float arithmetic, so it stays far tighter than any real currency's smallest unit.
_JE_BALANCE_EPSILON = 0.005


def _journal_entry_reserved_voucher_type_deny(doc):
    """Refuse ``voucher_type == 'Exchange Gain Or Loss'`` outright (see the module-level
    ``_JE_RESERVED_VOUCHER_TYPE`` comment for why) — called at BOTH ``plan_submit`` and the
    governed submit write, never at cancel (ERPNext's own machinery routinely auto-cancels these
    as a side effect of cancelling whatever they reference — see
    ``_journal_entry_cancel_flags_for_settings`` — so refusing a cancel of one would block a
    legitimate, ERPNext-driven cleanup). Returns a structured deny, or ``None`` when the
    voucher_type is fine."""
    if doc.get("voucher_type") == _JE_RESERVED_VOUCHER_TYPE:
        return _deny(
            f"voucher_type {_JE_RESERVED_VOUCHER_TYPE!r} is system-reserved for ERPNext's own "
            "FX-revaluation tooling — two independent ERPNext gates (validate_total_debit_and_"
            "credit and general_ledger.process_debit_credit_difference) skip the debit==credit "
            "invariant for exactly this value, so a caller-authored draft with this voucher_type "
            "could post unbalanced with ERPNext's own blessing; this broker refuses it rather "
            "than trust either gate", stage="plan")
    return None


def _journal_entry_balance_check(doc):
    """Pacioli's founding law ('no debit without a credit'), enforced INDEPENDENTLY of ERPNext
    (scout-je.md §4/§5): sum the draft's OWN ``accounts`` child-row ``debit``/``credit`` fields
    directly — never trust ERPNext's own cached ``total_debit``/``total_credit`` parent fields,
    recompute from the primitive rows every time — and refuse a mismatch beyond a small
    float-arithmetic tolerance (:data:`_JE_BALANCE_EPSILON`). Called at BOTH ``plan_submit``
    (before any marker can even be minted — a plan that fails this never records) and
    ``submit_journal_entry`` (the actual write; belt-and-suspenders even though ``check_fresh``
    already makes the second check logically redundant for an unmodified draft — this codebase's
    standing pattern is to re-verify every gate at the moment of the write). Never a replacement
    for ERPNext's own preview/on-submit checks — an independent addition alongside them. Returns a
    structured deny, or ``None`` when balanced."""
    total_debit = total_credit = 0.0
    for row in doc.get("accounts") or []:
        if not isinstance(row, dict):
            continue
        sides = {}
        for field in ("debit", "credit"):
            value = row.get(field)
            if value is None:
                sides[field] = 0.0  # an unused side — ERPNext rows legitimately carry only one
                continue
            # A PRESENT value that is not a finite, non-bool number cannot be summed: silently
            # skipping it (the prior behavior) treats it as 0, so an unreadable amount reads as
            # "balanced" (or, for NaN, DEFEATS the abs()>epsilon check entirely — the WG-2a class).
            # Refuse rather than bless a balance we could not independently verify — same
            # math.isfinite discipline as get_gl_entries / reconcile.check_allocation.
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not math.isfinite(value):
                return _deny(
                    f"Journal Entry accounts row for account {row.get('account')!r} has a "
                    f"malformed {field} ({value!r}) — the debit==credit balance cannot be "
                    "independently verified from an unreadable amount; refusing", stage="plan")
            sides[field] = value
        total_debit += sides["debit"]
        total_credit += sides["credit"]
    # Every per-row value was finite (guarded above), but summing enough large finite amounts can
    # overflow float64 to inf — and a non-finite TOTAL defeats the abs()>epsilon comparison the same
    # way a NaN input does (abs(inf - inf) is NaN, NaN > epsilon is False). Refuse an unverifiable
    # total rather than bless it — the accumulated-value companion to the per-value isfinite guard.
    if not (math.isfinite(total_debit) and math.isfinite(total_credit)):
        return _deny(
            f"Journal Entry debit/credit totals are not finite (debit={total_debit}, "
            f"credit={total_credit}) — the balance cannot be independently verified; refusing",
            stage="plan")
    if abs(total_debit - total_credit) > _JE_BALANCE_EPSILON:
        return _deny(
            f"total_debit ({total_debit}) != total_credit ({total_credit}), summed independently "
            "from the draft's own accounts rows (never ERPNext's own cached totals) — no debit "
            "without a credit; refusing", stage="plan")
    return None


def _update_stock_risk_flags(doc, op):
    """Envelope E2 disclosure: a doc with truthy ``update_stock`` moves PHYSICAL stock — the stock
    ledger is written alongside the GL on submit (and reversed on cancel). The projected GL shows
    the valuation rows only when perpetual inventory is enabled, so the movement can be invisible
    in the GL preview. Summary is read from the draft's OWN items rows (never a new bench read —
    the same source discipline as the independent JE balance check). Doctype-agnostic by
    construction: a doc without ``update_stock`` (JE, PE, a plain billing SI/PI) is a no-op.

    Envelope E4 fix (source-confirmed defect): a negative ``qty`` row is a RETURN receipt — stock
    comes IN on submit, not out — so the cancel-branch text can no longer hard-code "return to
    their warehouses" (backwards for a return: the reversal sends that stock back OUT). Both
    directions are now sign-aware: submit names the inbound movement explicitly when any row is
    negative, and cancel names the reversal's real direction instead of assuming outbound-then-back.
    A doc whose rows are all positive qty (the common case) gets byte-identical wording to before
    this fix — the sign-aware text only appears when a negative-qty row is actually present."""
    if not doc.get("update_stock"):
        return []
    items = [r for r in (doc.get("items") or []) if isinstance(r, dict)]
    shown = ", ".join(
        f"{r.get('qty')} {r.get('uom') or ''} of {r.get('item_code')} @ {r.get('warehouse')}".strip()
        for r in items[:5])
    if len(items) > 5:
        shown += f", and {len(items) - 5} more"
    inbound = [r for r in items if isinstance(r.get("qty"), (int, float))
              and not isinstance(r.get("qty"), bool) and r.get("qty") < 0]
    inbound_shown = ", ".join(
        f"{r.get('qty')} {r.get('uom') or ''} of {r.get('item_code')} @ {r.get('warehouse')}".strip()
        for r in inbound[:5])
    if len(inbound) > 5:
        inbound_shown += f", and {len(inbound) - 5} more"
    if op == "submit":
        flags = [f"this document moves PHYSICAL STOCK on submit (update_stock is set): "
                f"{len(items)} item row(s) — {shown}. The stock ledger is written alongside the "
                f"GL; the projected GL shows the valuation rows only when perpetual inventory is "
                f"enabled, so the stock movement can be invisible in the GL preview above"]
        if inbound:
            flags.append(
                f"{len(inbound)} of these row(s) carry a NEGATIVE qty — {inbound_shown} — this is "
                "a return receipt: stock comes IN to the named warehouse(s) on submit, not out")
        return flags
    if inbound:
        return [f"cancelling this document REVERSES its physical stock movement (update_stock was "
                f"set): {len(items)} item row(s) — {shown}; this reverses the ORIGINAL movement "
                f"whichever direction it ran — {len(inbound)} row(s) moved stock IN on submit "
                f"(negative qty: {inbound_shown}), so cancelling moves that stock back OUT rather "
                f"than returning it to a warehouse; the stock ledger entries are cancelled "
                f"alongside the GL reversal"]
    return [f"cancelling this document REVERSES its physical stock movement (update_stock was "
            f"set): {len(items)} item row(s) — {shown} — return to their warehouses; the stock "
            f"ledger entries are cancelled alongside the GL reversal"]


def _return_risk_flags(doc):
    """Envelope E4 disclosure: ``is_return`` (credit note), doctype-agnostic by construction — SI
    and PI both carry the field. Read entirely from the draft's OWN header/items fields (no new
    bench read, the same source discipline as :func:`_update_stock_risk_flags`). Advisory only,
    never a gate — a no-op for any doc without a truthy ``is_return``.

    * ALWAYS, when ``is_return`` is truthy: names the document as a return/credit note — money
      moves opposite a normal sale/purchase, and the projected rows already shown are that
      reversal.
    * A free-standing flag when ``return_against`` is falsy (leads with the fact, not a volume
      label — plain-function rule): a free-standing credit note is a first-class,
      tested ERPNext shape, but ERPNext's return-consistency checks — over-return protection,
      exchange-rate match, receivable-account match, posting-date ordering — all gate on
      ``return_against`` being truthy (``sales_and_purchase_return.py``), so none of them run here.
    * A mixed-sign flag when the items rows carry BOTH a negative and a positive qty: only the
      negative rows receive ERPNext's return checks; the positive rows are treated as an ordinary
      sale/purchase line.
    * A settlement flag when ``return_against`` IS set (envelope E4, found live PHASE Q): whether
      this credit note actually settles its original depends on ``update_outstanding_for_self`` —
      ERPNext's return mapper sets it by default, and then the return's receivable rows post
      against the RETURN ITSELF (the original's outstanding does not move; the credit sits on the
      return until a separate reconciliation allocates them). Consent to "a credit note against X"
      is not consent to "X is settled" — the memorandum must say which one this is."""
    if not doc.get("is_return"):
        return []
    flags = [
        "this document is a RETURN (credit note) — money moves opposite a normal sale/purchase; "
        "the projected rows above are that reversal"
    ]
    if doc.get("return_against"):
        if doc.get("update_outstanding_for_self"):
            flags.append(
                f"this credit note does NOT settle {doc.get('return_against')}: "
                "update_outstanding_for_self is set (ERPNext's return mapper sets it by default), "
                "so the receivable rows post against THIS return — the original's outstanding is "
                "unchanged, and the credit sits on this return until a separate payment "
                "reconciliation allocates them")
        else:
            flags.append(
                f"this credit note posts against {doc.get('return_against')} — the original's "
                "outstanding is reduced by this reversal")
    if not doc.get("return_against"):
        flags.append(
            "FREE-STANDING credit note (is_return is set, return_against is not) — ERPNext's "
            "return-consistency checks do NOT run for it: over-return protection, "
            "exchange-rate match, receivable-account match, and posting-date ordering are all "
            "gated on return_against being truthy, so none of them fire for this document")
    items = [r for r in (doc.get("items") or []) if isinstance(r, dict)]
    qtys = [r.get("qty") for r in items
            if isinstance(r.get("qty"), (int, float)) and not isinstance(r.get("qty"), bool)]
    if any(q < 0 for q in qtys) and any(q > 0 for q in qtys):
        flags.append(
            "this return has MIXED-SIGN item rows (both negative and positive qty) — only the "
            "negative-qty rows receive ERPNext's return checks; the positive-qty rows are treated "
            "as an ordinary sale/purchase line")
    return flags


def _pos_risk_flags(doc, op):
    """Envelope E4 disclosure: ``is_pos``, doctype-agnostic — read entirely from the draft's own
    ``payments`` child rows and header fields (no new bench read, same discipline as
    :func:`_update_stock_risk_flags`, whose ``op`` parameter this mirrors: the cancel direction
    gains one extra sentence the submit direction doesn't need). Advisory only, never a gate — a
    no-op for any doc without a truthy ``is_pos``.

    * ALWAYS: summarizes the ``payments`` rows (mode_of_payment + amount, first 5 then an honest
      "and N more"); when payments is empty and grand_total > 0, names the coming ERPNext refusal
      (``validate_pos_paid_amount`` requires at least one mode of payment at submit) — the plan
      discloses the refusal before it happens. When grand_total is not positive and there are no
      payments, names the zero-total waiver instead (ERPNext does not require a payment row here).
    * A PARTIAL-PAYMENT flag when the doc is NOT ``is_created_using_pos`` and the summed payments
      fall short of ``grand_total`` — ERPNext only enforces full payment for docs created via the
      POS flow (``is_created_using_pos``); a bare ``is_pos=1`` doc can post with the shortfall left
      outstanding.
    * On cancel: one more sentence — cancelling reverses the inline payment GL legs too (the
      payments rows were posted inline with the sale, not as a separate Payment Entry)."""
    if not doc.get("is_pos"):
        return []
    payments = [r for r in (doc.get("payments") or []) if isinstance(r, dict)]
    grand_total = doc.get("grand_total")
    if not (isinstance(grand_total, (int, float)) and not isinstance(grand_total, bool)):
        grand_total = 0
    flags = []
    if not payments:
        if grand_total > 0:
            flags.append(
                f"this is a POS document (is_pos is set) with NO payments rows and grand_total "
                f"{grand_total} — ERPNext will refuse this at submit (at least one mode of "
                "payment is required); this plan discloses the coming refusal")
        elif grand_total == 0:
            flags.append(
                "this is a POS document (is_pos is set) with grand_total 0 and no payments rows "
                "— ERPNext waives the payment requirement for a zero-total document")
        else:
            flags.append(
                f"this is a POS document (is_pos is set) with NO payments rows and grand_total "
                f"{grand_total} (not positive) — no ERPNext-confirmed gate applies to this "
                "combination; disclosed plainly rather than guessed")
    else:
        shown = ", ".join(
            f"{r.get('amount') if r.get('amount') is not None else '?'} via "
            f"{r.get('mode_of_payment') or '?'}" for r in payments[:5])
        if len(payments) > 5:
            shown += f", and {len(payments) - 5} more"
        flags.append(f"POS document (is_pos is set): {len(payments)} payment row(s) — {shown}")
        total_paid = sum(
            r.get("amount") for r in payments
            if isinstance(r.get("amount"), (int, float)) and not isinstance(r.get("amount"), bool))
        # tolerance 0.005 (the JE balance-check precedent): float dust on an intended-full
        # payment must not read as a shortfall — this is a disclosure, but a false disclosure
        # still misleads.
        if not doc.get("is_created_using_pos") and (grand_total - total_paid) > 0.005:
            flags.append(
                f"PARTIAL-PAYMENT: payments total {total_paid} is less than grand_total "
                f"{grand_total}, and this document is NOT is_created_using_pos — ERPNext only "
                "enforces full payment for docs created via the POS flow, so this one can post "
                "with the shortfall left outstanding")
    if op == "cancel":
        flags.append(
            "cancelling this POS document reverses the inline payment GL legs too (the payments "
            "rows above were posted inline with the sale, not as a separate Payment Entry)")
    return flags


def _journal_entry_submit_risk_flags(doc):
    """Journal Entry-specific ``plan_submit`` disclosures (scout-je.md §3, §5) — both advisory
    (``risk_flags``, never a gate: the plan still records so a human can see it even though a
    footgun below may bite only at real submit):

    * A STANDING note, always present: Journal Entry's ``on_submit``-only checks (cheque info for
      Bank Entry, credit limit, invoice-discounting status) are invisible to the native ledger
      preview — a clean-looking plan can still fail after a marker was minted against it.
    * A CONDITIONAL flag when a Bank Entry draft is missing ``cheque_no``/``cheque_date`` —
      ERPNext's own ``validate_cheque_info`` requires both for ``voucher_type == "Bank Entry"``
      specifically (confirmed from source, ``journal_entry.py:649-658``), checked only at
      on_submit.
    * The SAME disclosure for a Cash Entry draft missing the same fields, worded as the broker's
      OWN precaution — ERPNext's source does not actually enforce cheque_no/cheque_date for Cash
      Entry, only Bank Entry; the flag says so rather than overclaiming what the bench checks."""
    flags = [
        "Journal Entry's on_submit-only checks (cheque info for Bank Entry, credit limit, "
        "invoice-discounting status) are invisible to this preview — a clean-looking plan can "
        "still fail at real submit"
    ]
    if len(doc.get("accounts") or []) > 100:
        flags.append(
            "this Journal Entry has more than 100 accounts rows — ERPNext queues its submit (and "
            "any later cancel) to a background worker, so the broker will report the write as "
            "'unconfirmed' (accepted, not yet shown committed); confirm it afterwards against the "
            "document's real docstatus")
    voucher_type = doc.get("voucher_type")
    missing_cheque = not doc.get("cheque_no") or not doc.get("cheque_date")
    if voucher_type == "Bank Entry" and missing_cheque:
        flags.append(
            "Bank Entry draft is missing cheque_no/cheque_date — ERPNext's validate_cheque_info "
            "requires both for this voucher_type, checked only at on_submit (invisible to this "
            "preview); the real submit will fail after consent was minted on a clean-looking plan")
    elif voucher_type == "Cash Entry" and missing_cheque:
        flags.append(
            "Cash Entry draft is missing cheque_no/cheque_date — ERPNext itself does not enforce "
            "this for Cash Entry (only Bank Entry), but the broker flags it defensively")
    return flags


def _journal_entry_cancel_flags_for_settings(settings):
    """Journal Entry's own STANDING ``plan_cancel`` disclosure (scout-je.md §2, §5), taking an
    already-fetched ``Accounts Settings`` dict — no bench read of its own (the read is now made
    ONCE per plan/graph regardless of doctype, see :func:`_settling_reference_risk_flags` and
    ``PacioliBroker._tool_plan_cancel``/``_tool_plan_cascade_cancel``, F-R1). Unchanged text,
    byte-for-byte, from before F-R1 — the pin sheet keeps this flag as-is and adds the
    doctype-generic settling-reference disclosure alongside it, never replacing it:

    * ALWAYS present: cancelling this Journal Entry auto-cancels any system-generated Exchange
      Gain Or Loss Journal Entry that references it, with no separate consent (ERPNext's own
      ``cancel_exchange_gain_loss_journal``, called from BOTH ``accounts_controller.on_cancel`` and
      ``JournalEntry.make_gl_entries(1)``).
    * When the setting is ON: a second flag that this cancel can silently unlink (raw SQL, no doc
      event) other submitted Journal Entries/Payment Entries that reference this one, instead of
      being refused by the generic backlink check — Journal Entry's own, pre-F-R1 wording of the
      same underlying mechanism :func:`_settling_reference_risk_flags` now also names precisely
      (voucher + amount) for every supported doctype, JE included."""
    flags = [
        "cancelling this Journal Entry auto-cancels any system-generated Exchange Gain Or Loss "
        "Journal Entry that references it, with no separate consent (ERPNext's own "
        "cancel_exchange_gain_loss_journal runs unconditionally on cancel)"
    ]
    if settings.get("unlink_payment_on_cancellation_of_invoice"):
        flags.append(
            "Accounts Settings.unlink_payment_on_cancellation_of_invoice is ON — this cancel can "
            "silently unlink (raw SQL, no doc event) other submitted Journal Entries/Payment "
            "Entries that reference this one, instead of being refused by the generic backlink "
            "check")
    return flags


def _settling_reference_risk_flags(references, settings):
    """F-R1 — the settling-PE disclosure, doctype-generic (pin sheet
    docs/plans/2026-07-07-fr1-settling-pe-disclosure.md): for every settling ``references`` row
    (:meth:`pacioli.erpnext.ErpnextClient.get_settling_references` — Payment Ledger Entry rows
    whose ``against_voucher_type``/``against_voucher_no`` point at the document being cancelled),
    one flag naming exactly what happens on cancel, in one of TWO exact voices depending on
    whether ``Accounts Settings.unlink_payment_on_cancellation_of_invoice`` (``settings``, an
    already-fetched dict — no bench read of its own, same split as
    :func:`_journal_entry_cancel_flags_for_settings`) is ON or OFF:

    * ON — the unlink happens SILENTLY (no doc event, no separate consent): the settling voucher's
      allocation against this document is severed, the payment itself stays posted, and its
      unallocated amount goes back up by that amount (ERPNext's own
      ``auto_cancel_exempted_doctypes`` hook, ``unlink_ref_doc_from_payment_entries``).
    * OFF — ERPNext's own generic backlink check (``check_no_back_links_exist``) refuses the
      cancel outright (``LinkExistsError``) while the settling voucher still references it — the
      broker's cancel will fail at execute even though this plan itself still records (disclosure,
      not a gate).

    No rows = no flags (the control case — an unsettled document gets no noise). This applies to
    EVERY supported doctype (the prior JE-only unlink flag —
    :func:`_journal_entry_cancel_flags_for_settings` — stays, unchanged, alongside this one; the
    settling-PE gap this closes was never JE-specific, only the disclosure surface was)."""
    flags = []
    unlink_on = bool(settings.get("unlink_payment_on_cancellation_of_invoice"))
    for row in references:
        voucher_type = row.get("voucher_type")
        voucher_no = row.get("voucher_no")
        amount = row.get("amount")
        if unlink_on:
            flags.append(
                f"cancelling will SILENTLY UNLINK {voucher_type} {voucher_no}'s allocation of "
                f"{amount} against this document — the payment stays posted but the settlement "
                "link is severed and its unallocated amount increases "
                "(auto_cancel_exempted_doctypes)")
        else:
            flags.append(
                f"ERPNext will REFUSE this cancel (LinkExistsError) while {voucher_no} "
                "references it")
    return flags


def _require(args, *names):
    """Blank-or-missing required-argument check for the four generic doc-scoped tools: returns a
    structured deny (``stage="request"``) naming the first missing arg, or ``None`` when all are
    present. Runs BEFORE any network call — a schema-required arg the MCP client failed to send
    must be a named refusal, never a silent success or a downstream 404. (Deliberately applied
    only to ``workflow_status``/``request_workflow_transition``; the doctype-named
    get/list/submit/cancel/amend tools keep their existing behaviour untouched — a blank name
    surfaces as ERPNext's own 404/refusal, as it always has.)"""
    for name in names:
        value = args.get(name)
        if not isinstance(value, str) or not value.strip():
            return _deny(f"{name!r} is required", stage="request")
    return None


class PacioliBroker:
    """The assembled broker: registry + per-target stores + per-target clients.

    :param registry: a loaded :class:`pacioli.registry.Registry`.
    :param store_provider: ``target_name -> BrokerStore`` (one store per target — one set of
        books, one ledger; the same store must back the human's mint CLI).
    :param client_provider: ``Target -> ErpnextClient``.
    :param now_epoch/now_date: clocks, injected for testability. ``now_date`` is UTC — the
        closed-books compare is date-granular and deny-biased, so a bench-timezone skew can only ever
        refuse a same-day edge, never allow one.
    :param cascade_max: the node-count cap ``plan_cascade_cancel``/``cascade_cancel`` enforce via
        :func:`pacioli.cascade.build_cascade` (default 25; set via ``PACIOLI_CASCADE_MAX`` in
        ``runtime.assemble``).
    """

    def __init__(self, registry, store_provider, client_provider,
                 now_epoch=_now_epoch, now_date=_now_date, cascade_max=25):
        self._registry = registry
        self._store = store_provider
        self._client = client_provider
        self._now_epoch = now_epoch
        self._now_date = now_date
        self._cascade_max = cascade_max

    # --- dispatch ------------------------------------------------------------------
    def dispatch(self, name, arguments):
        handler = getattr(self, f"_tool_{name}", None)
        if name not in tool_names() or handler is None:
            return _deny(f"unknown tool {name!r}")
        args = arguments or {}
        try:
            if name not in READ_ONLY_TOOLS:
                # The choke point (CONTAIN's teeth, Task 2): every governed write dispatches
                # through here, so this is the ONE place a seal refuses all of them at once,
                # BEFORE the handler is even invoked — nothing is claimed, nothing is spent, no
                # marker is consumed. Read-only tools (READ_ONLY_TOOLS) skip this branch and reach
                # their own routing untouched: a sealed OR CORRUPT seal state must never make a
                # read fail — the confession has to stay readable, or the seal would hide the very
                # books that explain it (plan Global constraint #6).
                #
                # This call sits INSIDE this same try (review F1, Task 2 review): `_seal_gate`
                # itself only guards its `seal_state()` read — a failure RESOLVING the target or
                # its store (RegistryError, StoreCorruptError) is deliberately left to propagate
                # out of `_seal_gate`, so it lands here and is caught by the SAME except clauses
                # below that a read-only tool's own `_route` call would hit — the pre-existing
                # taxonomy (stage="request"/"store"), never stage="seal", for a failure that has
                # nothing to do with the seal itself.
                deny = self._seal_gate(args)
                if deny is not None:
                    return deny
            return handler(args)
        except (ErpnextError, RegistryError) as exc:
            return _deny(exc)
        except sqlite3.OperationalError as exc:
            # Write-lock contention beyond busy_timeout: fail closed with a structured deny, never
            # a traceback. If it fires after the marker is claimed, the marker stays reserved
            # (dead) and the human re-mints — safe, never a silent posting (spine ordering, SPEC §5).
            return _deny(f"store busy, could not complete safely: {exc}", stage="store")
        except StoreCorruptError as exc:
            # A torn/sub-header store file caught at open (refuse_if_torn). On the agent-facing
            # server path this must land as the house structured deny, the same envelope every
            # other refusal uses — not escape to the MCP SDK's generic error channel (redteam,
            # ledger-integrity lens Gap #3; the CLI's six open sites catch it separately). Reached
            # from either the seal gate's own resolution (a governed tool) or a handler's `_route`
            # (a read-only tool) — one clause, one stage, regardless of which side of the gate hit it.
            return _deny(str(exc), stage="store")

    def _seal_gate(self, args):
        """The seal check for every GOVERNED tool, run from :meth:`dispatch` before its handler is
        even looked up. Resolves the target the same way :meth:`_route` does (registry lookup,
        then ``store_provider``) but never constructs a client — a governed tool's own handler is
        the only thing that ever talks to ERPNext, and this gate's entire job is to make sure that
        never happens while sealed.

        Returns ``None`` when the write may proceed; otherwise a structured deny. Three distinct
        outcomes (review F1, Task 2 review — the pre-existing failure taxonomy, restored):

        * **Resolution itself fails** — an unknown/ambiguous ``pacioli_target``
          (:class:`~pacioli.registry.RegistryError`) or a torn/corrupt store file
          (:class:`~pacioli.store.StoreCorruptError`, or anything else the registry/store
          machinery raises). Deliberately done OUTSIDE any ``try`` here, so it is NOT caught by
          this method at all — it propagates to :meth:`dispatch`'s own pre-existing exception
          clauses and lands at their PRE-EXISTING stage (``"request"`` for a registry miss,
          ``"store"`` for a torn store), the exact stage a read-only tool's own ``_route`` call
          would have produced for the same failure. Resolving a target/store is not part of the
          seal check — it precedes seal knowledge entirely (an unknown target has no store to even
          ask whether it is sealed) — so it must never be reported as ``stage="seal"``: that stage
          is reserved for the two cases below, or a caller reading ``stage="seal"`` as "the seal
          did this" would be told something false.
        * **The store resolved fine, but reading seal state itself raised** —
          :meth:`pacioli.store.BrokerStore.seal_state` raised (e.g. a genuine ``sqlite3.Error`` off
          a damaged ``seal_events`` table or connection). THIS is the one case this method itself
          catches, denied at ``stage="seal"`` — the plan's Global constraint #4 ("Unreadable seal
          state on a write path → refuse the write"): if this gate cannot positively confirm
          UNsealed, it must not let a governed write through on the hope that it probably is. The
          reason names the raised cause and says plainly that this is NOT a confirmed seal event —
          an operator investigating should fix the underlying error, then retry, not go hunting for
          a seal that was never actually recorded.
        * **A verified ``sealed=True``** — ``seal_state()`` cleanly derived the current state (an
          operator's own ``pacioli seal``, or an envelope-escalated CONTAIN) and it is sealed.
          :func:`format_seal_refusal` names since/reason/source (and ``cause`` when the fail-closed
          machinery itself supplied one — a gap, zero rows, an unverifiable HMAC) plus the exact
          command that clears it. Also ``stage="seal"`` — this one really is the seal.

        **Gate-entry semantics — the TOCTOU window (stated contract, not a bug).** This gate and
        the write it guards each open the store INDEPENDENTLY — a fresh connection per call in
        production (:func:`pacioli.runtime.assemble`'s ``store_provider`` closure calls
        :func:`pacioli.runtime.open_store` fresh on every dispatch). A seal that lands in
        the gap between this gate's read and the handler's own write is not re-checked at write
        time: this gate answers "may dispatch ENTER this tool right now", not "is the store still
        unsealed at the exact instant the write lands". That is acceptable because CONTAIN here is
        a reversible refuse-every-FURTHER-write control, not an abort-in-flight-work control — the
        seal's job is to stop every governed write it can still catch at the gate, not to reach
        back into one that already cleared it. A write that lands still gets its own recorded
        intent/outcome receipt pair either way (README.md's "no lone entry" law — nothing here
        claims a write that lands is unaccounted for, only that it was not re-asked "still okay?"
        mid-flight). The authoritative fix for closing this window completely — one shared
        connection spanning gate-check-through-write for a single dispatch call — would mean
        restructuring :meth:`_route` (and every handler that calls it) to accept an already-open
        target/store instead of re-resolving one; that is a bigger, cross-cutting change
        deliberately deferred, not attempted here. The recorded future tightening is narrower: an
        execute-time re-check of seal state inside the spine itself
        (:func:`pacioli.spine.governed_submit`), immediately before the write is actually issued —
        closing the window without restructuring every call site.
        """
        target = self._registry.get(args.get("pacioli_target"))
        store = self._store(target.name)
        try:
            state = store.seal_state()
        except Exception as exc:  # noqa: BLE001 — deny-biased: cannot verify unsealed, refuse
            return _deny(
                "could not determine seal state before this governed write "
                f"({exc}) — refusing rather than risk an unverifiable seal letting a write "
                "through (this is NOT a confirmed seal event; investigate the underlying error, "
                "then retry)", stage="seal")
        if not state["sealed"]:
            return None
        return _deny(format_seal_refusal(state), stage="seal")

    def _route(self, args):
        target = self._registry.get(args.get("pacioli_target"))
        return target, self._client(target), self._store(target.name)

    # --- read tier (doctype-generic handlers) ---------------------------------------
    def _get_document(self, doctype, args):
        _, client, _ = self._route(args)
        return {"ok": True, "doc": client.get_document(doctype, args.get("name"))}





    def _list_documents(self, doctype, args):
        _, client, _ = self._route(args)
        party_field = SUPPORTED_DOCTYPES[doctype]["party_field"]
        rows = client.list_documents(doctype, filters=args.get("filters"),
                                     limit=int(args.get("limit", 20)), party_field=party_field)
        return {"ok": True, "rows": rows}





    # --- WORKFLOW (CONSENT's second gate, read side) --------------------------------
    def _tool_workflow_status(self, args):
        missing = _require(args, "name")
        if missing:
            return missing
        doctype, deny = _resolve_doctype(args)
        if deny:
            return deny
        _, client, _ = self._route(args)
        name = args.get("name")
        active, deny = _resolve_active_workflow(client, doctype)
        if deny:
            return deny
        if active is None:
            return {"ok": True, "workflow_active": False}
        state_field = (str(active.get("workflow_state_field") or "workflow_state").strip()
                       or "workflow_state")  # stripped: the seat writes the stripped name
        current_state = client.get_workflow_state(doctype, name, state_field)
        states = active.get("states") or []
        matched_row = next(
            (s for s in states if isinstance(s, dict) and s.get("state") == current_state), None)
        current_doc_status = workflow.doc_status(matched_row) if matched_row else None
        transitions_out = [
            {"action": t.get("action"), "next_state": t.get("next_state"),
             "approving": workflow.classify_transition(states, t) == "approving",
             "allowed_role": t.get("allowed"),
             # frappe's field default is ON — a transition LACKING the key reads as risky
             "allow_self_approval": workflow.self_approval_allowed(t)}
            for t in (active.get("transitions") or [])
            if isinstance(t, dict) and t.get("state") == current_state
        ]
        report = workflow.sod_report(active)
        return {"ok": True, "workflow_active": True, "workflow_name": active.get("name"),
                "current_state": current_state, "current_state_doc_status": current_doc_status,
                "transitions": transitions_out, "sod": report["sod"],
                "note": report["risk"] or "no self-approval risk found on approving transitions"}

    # --- PLAN ----------------------------------------------------------------------
    def _tool_plan_submit(self, args):
        doctype, deny = _resolve_doctype(args)
        if deny:
            return deny
        target, client, store = self._route(args)
        name = args.get("name")
        doc = client.get_document(doctype, name)
        if doc.get("docstatus") != 0:
            return _deny(f"{name} is not a draft (docstatus={doc.get('docstatus')}); "
                         "only a draft can be planned for submit", stage="plan")
        company = doc.get("company")
        if target.company and company != target.company:
            return _deny(f"document belongs to company {company!r} but target "
                         f"{target.name!r} is pinned to {target.company!r} — wrong books",
                         stage="plan")
        # Journal Entry breadth: two cheap, LOCAL checks (no network call) run BEFORE the preview's
        # network round-trip — a refusal here means the preview is never even requested. Neither
        # applies to any other doctype (doctype-scoped by construction).
        if doctype == JOURNAL_ENTRY:
            deny = _journal_entry_reserved_voucher_type_deny(doc)
            if deny:
                return deny
            deny = _journal_entry_balance_check(doc)
            if deny:
                return deny
        preview = client.ledger_preview(company=company, doctype=doctype, docname=name)
        risk_flags = []
        posting_date = str(doc.get("posting_date") or "")
        if posting_date > self._now_date():
            risk_flags.append("posting_date is in the future")
        # Payment Entry breadth: disclose what ERPNext's own preview doesn't (see
        # _payment_entry_risk_flags). Doctype-scoped by construction — SI/PI docs carry no
        # `references` child table, so this is a no-op for them, never a branch on shape alone.
        if doctype == PAYMENT_ENTRY:
            risk_flags.extend(_payment_entry_risk_flags(doc))
        # Journal Entry breadth: disclose the on_submit-only fidelity gap + the Bank/Cash cheque
        # footgun (see _journal_entry_submit_risk_flags). Doctype-scoped — a no-op for every other
        # doctype, never a branch on shape alone.
        if doctype == JOURNAL_ENTRY:
            risk_flags.extend(_journal_entry_submit_risk_flags(doc))
        # Envelope E2: physical-stock disclosure — fires only when the doc itself carries a
        # truthy update_stock (SI/PI); a no-op for every other doctype by construction.
        risk_flags.extend(_update_stock_risk_flags(doc, "submit"))
        # Envelope E4: is_return / is_pos disclosures — doctype-agnostic (SI/PI both carry
        # is_return; is_pos is free-standing), read from the doc's own fields, no-op unless set.
        risk_flags.extend(_return_risk_flags(doc))
        risk_flags.extend(_pos_risk_flags(doc, "submit"))
        # Envelope E6: closed-books disclosure — a locked posting still plans (ok: True); the
        # actual refusal remains execute-time only (see _plan_closed_books_risk's docstring).
        self._plan_closed_books_risk(client, risk_flags, company, doctype, posting_date)
        # Workflow-SoD is surfaced here as a risk flag, never a refusal — PLAN is a read, and the
        # actual gate (the doctype's submit tool refusing outright) runs at execute time. Ambiguous
        # config is flagged the same way: the plan still records; only the write is refused.
        workflow_info = self._plan_workflow_risk(client, risk_flags, doctype)
        plan = new_plan(
            plan_id=uuid.uuid4().hex,
            target=target.name,
            doc_version=str(doc.get("modified") or ""),
            posting_date=posting_date,
            projected_gl=preview.get("gl_data") or [],
            risk_flags=risk_flags,
            ts=self._now_date(),
            docname=name,
            op="submit",
            doctype=doctype,
        )
        store.record_plan(plan)
        return {"ok": True, "plan_id": plan.plan_id, "docname": name, "target": target.name,
                "doctype": doctype,
                "doc_version": plan.doc_version, "posting_date": plan.posting_date,
                "projected_gl": plan.projected_gl, "risk_flags": plan.risk_flags,
                "workflow": workflow_info,
                "next": "have a human mint a consent marker for this plan_id (pacioli mint), "
                        "then call the matching submit_<doctype> tool with it"}

    def _plan_workflow_risk(self, client, risk_flags, doctype):
        """Append workflow-governance risk flags to ``risk_flags`` (mutated in place) and return
        the workflow info block for the plan response (``None`` when no active workflow governs
        this doctype). Ambiguous AND malformed config are flagged, not refused — planning is a
        read; the actual write is where the refusal lands (see caller)."""
        active = workflow.find_active(client.get_active_workflows(doctype))
        if active is None:
            return None
        if isinstance(active, workflow.Malformed):
            risk_flags.append(f"malformed workflow configuration for {doctype!r} "
                              f"({active.detail}) — submission will be refused until this is "
                              "resolved")
            return {"workflow_name": None, "malformed": active.detail, "sod": False}
        if isinstance(active, workflow.Ambiguous):
            names = ", ".join(active.names)
            risk_flags.append(f"ambiguous workflow configuration for {doctype!r}: "
                              f"{names} — submission will be refused until this is resolved")
            return {"workflow_name": None, "ambiguous": list(active.names), "sod": False}
        report = workflow.sod_report(active)
        roles = sorted({a["allowed_role"] for a in report["approving_transitions"]
                       if a.get("allowed_role")})
        role_text = ", ".join(roles) if roles else "an approving role"
        risk_flags.append(f"workflow-governed: submission requires human approval via Workflow "
                          f"{active.get('name')!r} (role {role_text})")
        if report["risk"]:
            risk_flags.append(report["risk"])
        return {"workflow_name": active.get("name"), "ambiguous": None, "sod": report["sod"]}

    def _plan_closed_books_risk(self, client, risk_flags, company, doctype, posting_date):
        """Envelope E6: plan-time closed-books DISCLOSURE, never a gate. Reads the same
        ``get_period_locks`` the execute-time spine (``_governed_write`` -> ``spine.
        governed_submit`` -> ``check_red_line``) already enforces, and runs the SAME
        ``check_red_line`` core against them — reused, not re-implemented, so the disclosure can
        never drift from the gate it's warning about. A locked posting still returns a plan
        (``ok: True``) with this flag appended; the actual refusal still happens only at execute
        — belt-and-suspenders, TOCTOU-fresh (a lock can appear between plan and execute; the
        execute-time check is what actually protects the books either way).

        F-S1: ``doctype`` is now threaded through to ``get_period_locks`` alongside
        ``posting_date`` (both required — see ``erpnext.py``) — the SAME (company, doctype,
        posting_date) triple ``_governed_write`` passes at execute, so the plan-time disclosure and
        the execute-time gate can never read a different Accounting Period shape for the same
        posting; "never drifts" is a property of calling the identical function with identical
        arguments, not just reusing ``check_red_line``.

        An unreadable lock source raises here exactly as it does at execute (``get_period_locks``'s
        own deny-bias, ``pacioli/erpnext.py``) — ``dispatch()`` turns that into a plan-stage deny,
        the same "can't verify, must refuse" posture as every other lock read in this codebase.
        This is a NEW refusal path for ``plan_submit``/``plan_cancel`` (they never called
        ``get_period_locks`` before), but it only ever adds a refusal for a source that was already
        deny-on-unreadable at execute — it never loosens anything."""
        locks = client.get_period_locks(company, doctype, posting_date)
        ok, reason = check_red_line(posting_date, self._now_date(), locks)
        if not ok:
            risk_flags.append(f"closed-books: {reason} — this posting will be refused at execute "
                              "unless it changes before then")

    # --- PLAN (UNDO direction) ---------------------------------------------------------
    def _tool_plan_cancel(self, args):
        doctype, deny = _resolve_doctype(args)
        if deny:
            return deny
        target, client, store = self._route(args)
        name = args.get("name")
        doc = client.get_document(doctype, name)
        if doc.get("docstatus") != 1:
            return _deny(f"{name} is not a submitted document (docstatus={doc.get('docstatus')}); "
                         "only a submitted document can be planned for cancel", stage="plan")
        company = doc.get("company")
        if target.company and company != target.company:
            return _deny(f"document belongs to company {company!r} but target "
                         f"{target.name!r} is pinned to {target.company!r} — wrong books",
                         stage="plan")
        # Blast radius FIRST: cancelling under other submitted documents breaks them. This slice
        # governs the leaf-node cancel only; a non-empty graph is a refusal that names the links,
        # never a cascade taken silently. An unreadable graph raised above (deny, not empty).
        linked = client.get_submitted_linked_docs(doctype, name)
        if linked:
            shown = ", ".join(
                f"{d.get('doctype')} {d.get('name')}" for d in linked[:5] if isinstance(d, dict))
            more = f" (+{len(linked) - 5} more)" if len(linked) > 5 else ""
            return _deny(f"{len(linked)} submitted document(s) link to {name}: {shown}{more} — "
                         "cancelling would break them; cancel those first, or use "
                         "plan_cascade_cancel to govern the whole graph in one consent",
                         stage="plan")
        reversal = client.get_gl_entries(doctype, name)
        risk_flags = []
        posting_date = str(doc.get("posting_date") or "")
        if posting_date > self._now_date():
            risk_flags.append("posting_date is in the future")
        if not reversal:
            risk_flags.append("no live GL rows found for this voucher — nothing visible to unwind")
        # Payment Entry breadth: disclose the blast radius the linked-docs check above doesn't
        # cover — WHICH invoices this payment settles (a single voucher can revert N of them at
        # once). Disclosure only, computed from the draft's own cached references, never a gate.
        references = _payment_entry_cancel_references(doc) if doctype == PAYMENT_ENTRY else None
        # F-R1: the settling-PE disclosure, now made for EVERY supported doctype (widened beyond
        # the prior JE-only gate) — the Accounts Settings unlink read is made ONCE per plan
        # regardless of doctype, then reused both for the doctype-generic settling-reference
        # flags below AND (JE only) the standing EG-note/unlink flag, unchanged text
        # (_journal_entry_cancel_flags_for_settings). An unreadable Accounts Settings doc OR an
        # unreadable Payment Ledger Entry read both raise, caught by dispatch() and returned as a
        # structured deny (the WHOLE plan refuses, never just this flag) — the same deny-bias as
        # every other lock-adjacent read in this codebase.
        unlink_settings = client.get_accounts_settings(
            ["unlink_payment_on_cancellation_of_invoice"])
        if doctype == JOURNAL_ENTRY:
            risk_flags.extend(_journal_entry_cancel_flags_for_settings(unlink_settings))
        settling_references = client.get_settling_references(doctype, name)
        risk_flags.extend(_settling_reference_risk_flags(settling_references, unlink_settings))
        # Envelope E2: cancelling an update_stock doc reverses its physical stock movement —
        # disclosed from the doc's own items rows, doctype-agnostic by construction.
        risk_flags.extend(_update_stock_risk_flags(doc, "cancel"))
        # Envelope E4: is_return / is_pos disclosures, same fields, cancel direction.
        risk_flags.extend(_return_risk_flags(doc))
        risk_flags.extend(_pos_risk_flags(doc, "cancel"))
        # Envelope E6: closed-books disclosure — a locked posting still plans (ok: True); the
        # actual refusal remains execute-time only (see _plan_closed_books_risk's docstring).
        self._plan_closed_books_risk(client, risk_flags, company, doctype, posting_date)
        plan = new_plan(
            plan_id=uuid.uuid4().hex,
            target=target.name,
            doc_version=str(doc.get("modified") or ""),
            posting_date=posting_date,
            projected_gl=reversal,
            risk_flags=risk_flags,
            ts=self._now_date(),
            docname=name,
            op="cancel",
            doctype=doctype,
        )
        store.record_plan(plan)
        return {"ok": True, "plan_id": plan.plan_id, "docname": name, "target": target.name,
                "doctype": doctype,
                "doc_version": plan.doc_version, "posting_date": plan.posting_date,
                "projected_reversal": plan.projected_gl, "risk_flags": plan.risk_flags,
                "references": references,
                "next": "have a human mint a consent marker for this plan_id (pacioli mint), "
                        "then call the matching cancel_<doctype> tool with it"}

    # --- PLAN (CASCADE cancel: the whole submitted-dependent graph, ordered) -----------
    def _tool_plan_cascade_cancel(self, args):
        doctype, deny = _resolve_doctype(args)
        if deny:
            return deny
        target, client, store = self._route(args)
        name = args.get("name")
        doc = client.get_document(doctype, name)
        if doc.get("docstatus") != 1:
            return _deny(f"{name} is not a submitted document (docstatus={doc.get('docstatus')}); "
                         "only a submitted document can be cascade-cancelled", stage="plan")
        if target.company and doc.get("company") != target.company:
            return _deny(f"document belongs to company {doc.get('company')!r} but target "
                         f"{target.name!r} is pinned to {target.company!r} — wrong books",
                         stage="plan")

        built = build_cascade(
            {"doctype": doctype, "docname": name},
            fetch_linked=self._cascade_fetch_linked(client),
            node_meta=self._cascade_node_meta(client),
            supported_doctypes=set(SUPPORTED_DOCTYPES),
            max_nodes=self._cascade_max)
        if not built["ok"]:
            return _deny(built["reason"], stage=built.get("stage", "plan"))

        graph = built["graph"]
        ok, reason = self._cascade_workflow_gate(client, graph)
        if not ok:
            return _deny(reason, stage="workflow")
        # Wrong-books pin (C1, final-review fix): the target-only check above is not enough — a
        # dependent can legitimately live in a different company, so every node must be pinned.
        ok, reason = self._cascade_books_gate(target, graph)
        if not ok:
            return _deny(reason, stage="plan")

        # Per-node risk disclosure, mirroring _tool_plan_cancel's flags but qualified by docname so
        # a multi-node graph doesn't blur which node the flag is about. Disclosure only — the gates
        # above/below are what actually refuse. Every node gets the SAME doctype-appropriate
        # disclosures the single-op plan would have given it (envelope E3, Gap B; F-R1): the
        # Journal Entry EG-auto-cancel note + unlink flag, the update_stock reversal flag, and the
        # settling-PE disclosure (F-R1, every node) — reusing those exact helpers rather than
        # duplicating their text. The Accounts Settings unlink read happens at most ONCE for the
        # whole graph (generalized from the prior JE-only "je_settings" memo — F-R1 widens it to
        # every node's doctype, exactly like the single-op plan_cancel); the Payment Ledger Entry
        # settling-reference read happens PER NODE — each node has its own settlement blast radius.
        risk_flags = []
        unlink_settings = None
        for node in graph:
            if node["posting_date"] > self._now_date():
                risk_flags.append(f"{node['docname']}: posting_date is in the future")
            if not node["projected_gl"]:
                risk_flags.append(f"{node['docname']}: no live GL rows found")
            if unlink_settings is None:
                unlink_settings = client.get_accounts_settings(
                    ["unlink_payment_on_cancellation_of_invoice"])
            if node["doctype"] == JOURNAL_ENTRY:
                risk_flags.extend(f"{node['docname']}: {flag}" for flag in
                                  _journal_entry_cancel_flags_for_settings(unlink_settings))
            settling = client.get_settling_references(node["doctype"], node["docname"])
            risk_flags.extend(f"{node['docname']}: {flag}" for flag in
                              _settling_reference_risk_flags(settling, unlink_settings))
            # Deliberate duplicate read vs _cascade_node_meta's own get_document (redteam: LOW,
            # keeping): this read feeds disclosure only — widening the core node shape to carry
            # the whole doc just to save it isn't worth coupling cascade.py to doc internals.
            node_doc = client.get_document(node["doctype"], node["docname"])
            if node["doctype"] == PAYMENT_ENTRY:
                # The single-op plan_cancel discloses a PE's settled references via the top-level
                # `references` key; per-node, each reference becomes its own prefixed flag —
                # reusing _payment_entry_cancel_references' data, never re-querying.
                risk_flags.extend(
                    f"{node['docname']}: cancelling this Payment Entry reverts the settled "
                    f"outstanding_amount of {r['reference_doctype']} {r['reference_name']} "
                    f"(allocated {r['allocated_amount']}) — that document is unlinked from this "
                    "payment and shows as unpaid again"
                    for r in _payment_entry_cancel_references(node_doc))
            risk_flags.extend(f"{node['docname']}: {flag}"
                              for flag in _update_stock_risk_flags(node_doc, "cancel"))
            # Envelope E4: same is_return / is_pos disclosures, per-node, docname-prefixed —
            # reusing node_doc already fetched above, no extra bench read.
            risk_flags.extend(f"{node['docname']}: {flag}"
                              for flag in _return_risk_flags(node_doc))
            risk_flags.extend(f"{node['docname']}: {flag}"
                              for flag in _pos_risk_flags(node_doc, "cancel"))
        plan = new_plan(
            plan_id=uuid.uuid4().hex, target=target.name,
            doc_version=graph[-1]["doc_version"], posting_date=graph[-1]["posting_date"],
            projected_gl=[row for n in graph for row in n["projected_gl"]],
            risk_flags=risk_flags, ts=self._now_date(), docname=name, op="cascade_cancel",
            doctype=doctype, graph=graph)
        store.record_plan(plan)
        return {"ok": True, "plan_id": plan.plan_id, "docname": name, "target": target.name,
                "doctype": doctype, "op": "cascade_cancel", "total": len(graph), "graph": graph,
                "risk_flags": plan.risk_flags,
                "preflight_limit": "ERPNext has no dry-cancel: freshness + period-lock are checked "
                                   "before any cancel, but internal cancel-blocks (e.g. a reconciled "
                                   "payment) can only surface at execute and will fail-stop the run",
                "next": "have a human mint a consent marker for this plan_id (pacioli mint), then "
                        "call cascade_cancel with it — it authorizes exactly this ordered graph"}

    # --- the governed writes (both directions of the duality) ---------------------------
    def _governed_write(self, args, *, op, transition, plan_tool, doctype):
        """The shared spine glue for submit and cancel. The two verbs differ ONLY in the executor,
        the op label, and the docstatus transition — every gate (wrong-books, docname, cross-op,
        cross-doctype, freshness, closed-books, marker) is identical by construction, which is the
        point: UNDO gets the exact same governance as DO, and Purchase Invoice gets the exact same
        governance as Sales Invoice."""
        target, client, store = self._route(args)
        name, plan_id, token = args.get("name"), args.get("plan_id"), args.get("marker")

        plan = store.get_plan(plan_id)
        if plan is None:
            return _deny(f"no recorded plan {plan_id!r}; call {plan_tool} first", stage="plan")
        if plan.target != target.name:
            return _deny(f"plan {plan_id!r} was built for target {plan.target!r}, "
                         f"not {target.name!r} — wrong books", stage="plan")
        ok, reason = check_docname(plan, name)
        if not ok:
            return _deny(reason, stage="plan")
        ok, reason = check_op(plan, op)
        if not ok:
            return _deny(reason, stage="plan")
        # The security headline of the breadth increment: a plan for one doctype must never
        # authorize a submit/cancel of another — mirrors check_op exactly, same tier, same place.
        ok, reason = check_doctype(plan, doctype)
        if not ok:
            return _deny(reason, stage="plan")

        # Workflow-SoD (CONSENT's second gate): a caller-side gate, same tier as check_docname/
        # check_op/check_doctype above — cheap local checks first, THEN this network read, THEN
        # the doc/lock reads below. Independent of doc state: submit is refused whenever ANY
        # active workflow governs this doctype; cancel only when the company's own config maps a
        # state to doc_status "2" (governs_op). No workflow configured = this passes silently.
        active_workflow = workflow.find_active(client.get_active_workflows(doctype))
        ok, reason = workflow.check_submit_gate(active_workflow, op)
        if not ok:
            return _deny(reason, stage="workflow")

        doc = client.get_document(doctype, name)
        # Wrong-books TOCTOU belt (F-C2): re-check the FRESH doc's company against the pinned
        # target here, at execute — the gate `_tool_plan_submit`/`_tool_plan_cancel` already apply
        # at plan time (above), but this glue never re-ran it, and check_fresh's `modified`-
        # equality is only an IMPLICIT protection: it holds only if changing a doc's `company`
        # always bumps `modified`, which a `db_set(update_modified=False)`/raw-SQL patch does not.
        # Mirrors `_cascade_books_gate` (this module, ~1653) — the sibling that already closed this
        # exact class for cascade cancel — same stage label ("plan": the violation is "this write
        # is not what was planned", not a new tier of its own), same wrong-books wording. Placed
        # FIRST among the execute-time belts below (JE balance/voucher, closed-books): a document
        # posting to the wrong company's books is the most fundamental mismatch a plan can carry,
        # so it is checked before any doctype-specific or date-range belt gets a chance to fire on
        # a write that must be refused regardless of its balance or its posting date. Runs BEFORE
        # `marker = store.get_marker(token)` below and everything `governed_submit` does with it
        # (reserve/claim) — same tier as check_docname/check_op/check_doctype above, so a refusal
        # here never touches the marker at all (it stays `live`, exactly like those). No pin on the
        # target (`target.company` unset) = nothing to check — the documented unpinned posture
        # (registry.py: the `company` field is optional, recorded but not enforced when absent).
        if target.company and doc.get("company") != target.company:
            return _deny(f"document belongs to company {doc.get('company')!r} but target "
                         f"{target.name!r} is pinned to {target.company!r} — wrong books",
                         stage="plan")
        # Journal Entry breadth: the same two gates plan_submit(JE) already ran, re-verified here
        # at the actual moment of the write (belt-and-suspenders — governed_submit's own
        # check_fresh, just below, already makes this logically redundant for an unmodified draft,
        # since an unchanged doc_version means an unchanged balance/voucher_type too; but this
        # codebase's standing pattern is to re-check every gate at the write, never trust only the
        # plan-time pass). Cancel is deliberately NOT gated on the reserved voucher_type (see
        # _journal_entry_reserved_voucher_type_deny's docstring for why).
        if doctype == JOURNAL_ENTRY and op == "submit":
            deny = _journal_entry_reserved_voucher_type_deny(doc)
            if deny:
                return deny
            deny = _journal_entry_balance_check(doc)
            if deny:
                return deny
        # F-S1: doctype + posting_date are REQUIRED by get_period_locks now — posting_date comes
        # from the SAME already-fetched `doc` above (never a new network read; it's the identical
        # snapshot every other gate here just validated against). Cancel gets the identical
        # doctype-aware check as submit (F6 — ERPNext itself blocks cancelling into a closed
        # period via general_ledger.make_reverse_gl_entries; this matches, not exceeds, that).
        locks = client.get_period_locks(
            doc.get("company"), doctype, str(doc.get("posting_date") or ""))  # unreadable → deny
        marker = store.get_marker(token)

        def execute():
            # submit_document grows a `doc` kwarg for the override-doctype client_rpc transport
            # (Journal Entry — frappe.client.submit reconstructs the document from the body rather
            # than re-fetching it). It MUST be this SAME already-fetched `doc` — the identical
            # snapshot current_doc_version/the closed-books/JE-balance checks above just validated —
            # never a fresh re-read, or a write could land against a doc state those gates never
            # saw. cancel_document takes no doc (frappe.client.cancel loads fresh from the DB
            # itself), so its call shape is unchanged.
            if op == "submit":
                result = client.submit_document(doctype, name, doc=doc)
            else:
                result = client.cancel_document(doctype, name)
            return {"name": str(result.get("name") or name),
                    "docstatus": int(result.get("docstatus", -1)),
                    "modified": str(result.get("modified") or ""),
                    "doctype": doctype}

        def readback():
            # Transport taxonomy (docs/plans/2026-07-07-transport-taxonomy.md): only reached when
            # `execute()` raised something the transport layer could not classify as an answered
            # refusal — the mutating call may already be in motion server-side, so the spine needs
            # the document's REAL docstatus rather than assuming no progress. Same read path as
            # everywhere else in this module (`client.get_document`), never a new surface. May
            # raise (a transient failure reading it back) — `spine.governed_submit` owns degrading
            # that to `readback_error`, never letting it crash this flow.
            return client.get_document(doctype, name).get("docstatus")

        outcome = governed_submit(
            plan=plan, marker=marker, token=token,
            current_doc_version=str(doc.get("modified") or ""),
            now_epoch=self._now_epoch(), now_date=self._now_date(), locks=locks,
            effects=SubmitEffects(store, execute, readback), op=op, transition=transition,
        )
        out = {"ok": outcome.ok, "stage": outcome.stage, "reason": outcome.reason}
        if outcome.ok:
            out["result"] = outcome.result
        return out

    def _submit_document(self, doctype, args):
        return self._governed_write(args, op="submit", transition="0->1",
                                    plan_tool="plan_submit", doctype=doctype)





    def _cancel_document(self, doctype, args):
        return self._governed_write(args, op="cancel", transition="1->2",
                                    plan_tool="plan_cancel", doctype=doctype)





    def _cascade_node_meta(self, client):
        """Build the ``node_meta(doctype, docname)`` callback ``build_cascade`` needs, bound to
        ``client``. One shared definition — plan and re-plan (execute-time re-discovery) must read
        a node exactly the same way, so this is not duplicated per call site."""
        def node_meta(dt, dn):
            d = client.get_document(dt, dn)
            return {"doc_version": str(d.get("modified") or ""),
                    "posting_date": str(d.get("posting_date") or ""),
                    "company": d.get("company"),
                    "projected_gl": client.get_gl_entries(dt, dn)}
        return node_meta

    def _cascade_fetch_linked(self, client):
        """Build the ``fetch_linked(doctype, docname)`` callback ``build_cascade`` needs, bound to
        ``client`` — and NORMALIZE the seam. ERPNext's ``get_submitted_linked_docs`` returns each
        dependent in frappe's native shape (``{"doctype", "name", ...}``), but ``build_cascade``
        keys every node on ``docname`` (the broker's internal convention, matching the target node
        the tool constructs). Map ``name`` → ``docname`` here so the pure core never sees frappe's
        wire shape. One shared definition — plan and execute-time re-discovery must walk the graph
        identically. (The pure-core tests fed fakes already in ``docname`` shape, so this exact
        adapter boundary had no live-shape coverage until the bench hit ``KeyError: 'docname'`` on
        the first real dependent — see ``test_tools.py`` for the regression that pins the real
        shape.)"""
        def fetch_linked(dt, dn):
            return [{"doctype": d["doctype"], "docname": d.get("docname") or d.get("name")}
                    for d in client.get_submitted_linked_docs(dt, dn)]
        return fetch_linked

    def _cascade_books_gate(self, target, graph):
        """Refuse a cascade if ANY node's company differs from the pinned target's company — the
        same wrong-books pin ``_tool_plan_cancel``/``_governed_write`` apply to their one document,
        applied across the whole graph so a cascade can never launder a cross-company cancel under
        one marker (a dependent graph can legitimately span companies — inter-company Sales/
        Purchase Invoice pairs, inter-company Journal Entries — so this must be checked per node,
        not just on the target). No pin configured on the target = nothing to check."""
        if not target.company:
            return (True, None)
        for node in graph:
            if node["company"] != target.company:
                return (False, f"cascade node {node['doctype']} {node['docname']} belongs to "
                               f"company {node['company']!r} but target {target.name!r} is pinned "
                               f"to {target.company!r} — wrong books")
        return (True, None)

    def _cascade_workflow_gate(self, client, graph):
        """Refuse a cascade if ANY node's cancel is governed by an active ERPNext Workflow — the
        same Workflow-SoD gate `_governed_write` applies to a single cancel, applied across the whole
        graph so a cascade can never launder a governed cancel. Deny-biased: an ambiguous, malformed,
        or unreadable workflow config refuses. Cached per doctype (the gate governs a doctype)."""
        seen = {}
        for node in graph:
            dt = node["doctype"]
            if dt not in seen:
                try:
                    seen[dt] = workflow.check_submit_gate(
                        workflow.find_active(client.get_active_workflows(dt)), "cancel")
                except ErpnextError as exc:
                    seen[dt] = (False, f"could not read Workflow config for {dt}: {exc}")
            ok, reason = seen[dt]
            if not ok:
                return (False, f"{dt} {node['docname']}: {reason}")
        return (True, None)

    # --- CASCADE cancel (execute the frozen ordered graph under one marker) -------------
    def _tool_cascade_cancel(self, args):
        target, client, store = self._route(args)
        name, plan_id, token = args.get("name"), args.get("plan_id"), args.get("marker")
        plan = store.get_plan(plan_id)
        if plan is None:
            return _deny(f"no recorded plan {plan_id!r}; call plan_cascade_cancel first", stage="plan")
        if plan.target != target.name:
            return _deny(f"plan {plan_id!r} was built for target {plan.target!r}, "
                         f"not {target.name!r} — wrong books", stage="plan")
        ok, reason = check_docname(plan, name)
        if not ok:
            return _deny(reason, stage="plan")
        ok, reason = check_op(plan, "cascade_cancel")
        if not ok:
            return _deny(reason, stage="plan")

        # re-discover: the frozen graph the human consented to must still be the whole set.
        rebuilt = build_cascade({"doctype": plan.doctype, "docname": plan.docname},
                                fetch_linked=self._cascade_fetch_linked(client),
                                node_meta=self._cascade_node_meta(client),
                                supported_doctypes=set(SUPPORTED_DOCTYPES), max_nodes=self._cascade_max)
        if not rebuilt["ok"]:
            return _deny(rebuilt["reason"], stage=rebuilt.get("stage", "plan"))
        planned_keys = [(n["doctype"], n["docname"]) for n in plan.graph]
        live_keys = [(n["doctype"], n["docname"]) for n in rebuilt["graph"]]
        if planned_keys != live_keys:
            return _deny("the dependent graph changed since planning (a link was added or removed); "
                         "re-plan before cascade-cancelling", stage="plan")

        ok, reason = self._cascade_workflow_gate(client, plan.graph)
        if not ok:
            return _deny(reason, stage="workflow")
        # Wrong-books TOCTOU belt (C1, final-review fix): re-check against the freshly rebuilt
        # graph (live company data), not plan.graph — a company change between plan and execute
        # doesn't necessarily bump a doc's `modified`, so per-node freshness can't be relied on to
        # catch it; this is the only gate that reads live company. Symmetric with the Workflow-SoD
        # belt above.
        ok, reason = self._cascade_books_gate(target, rebuilt["graph"])
        if not ok:
            return _deny(reason, stage="plan")

        marker = store.get_marker(token)

        class _Effects:
            def claim_marker(self, reserved):
                return store.claim_marker(reserved)
            def current_version(self, dt, dn):
                return str(client.get_document(dt, dn).get("modified") or "")
            def locks_for(self, company, doctype, posting_date):
                # F-S1: doctype + posting_date threaded through per node — cascade.run_cascade
                # passes the SAME node["doctype"]/node["posting_date"] it already uses for that
                # node's check_fresh/check_red_line call, never a fresh read.
                return client.get_period_locks(company, doctype, posting_date)
            def record_intent(self, body):
                return store.record_intent(body)
            def cancel(self, dt, dn):
                # Gap A (envelope E3): the cancel response alone may not carry a usable docstatus
                # (frappe.client.cancel's client_rpc envelope vs a run_method body — see
                # erpnext.py's cancel_document docstring) — run_cascade needs one to confirm the
                # transition, the same discipline spine.governed_submit already applies. When the
                # response doesn't carry one, read the document back through the SAME path already
                # used everywhere else in this module (client.get_document) — never a new read
                # surface — rather than assume a transition the response never showed.
                result = client.cancel_document(dt, dn)
                raw = result if isinstance(result, dict) else {}
                docstatus = raw.get("docstatus")
                readback_error = None
                if docstatus is None:
                    # The readback must NEVER raise past this point (redteam, critical): the
                    # mutating cancel above already went through, and an exception here would land
                    # in run_cascade's generic failure path — which on a first node RELEASES the
                    # marker as if nothing happened. A released grant for an act in flight is the
                    # exact inversion the unconfirmed rule exists to prevent, so a failed readback
                    # degrades to docstatus None → the unconfirmed branch (marker spent,
                    # fail-stop), with the readback error carried on the result so the reconciler
                    # sees the readback itself failed rather than a queued write. (A throw from
                    # cancel_document itself is the transport taxonomy's OWN no-answer branch —
                    # docs/plans/2026-07-07-transport-taxonomy.md — handled by `run_cascade` itself
                    # via this same class's `readback` method below, never here.)
                    try:
                        docstatus = client.get_document(dt, dn).get("docstatus")
                    except Exception as exc:  # noqa: BLE001 — degrade, never release-in-flight
                        readback_error = str(exc)
                try:
                    docstatus = int(docstatus)
                except (TypeError, ValueError):
                    docstatus = None
                confirmed = {**raw, "docstatus": docstatus}
                if readback_error is not None:
                    confirmed["readback_error"] = readback_error
                return confirmed
            def readback(self, dt, dn):
                # Transport taxonomy: only reached when `cancel` above raised something the
                # transport layer could not classify as an answered refusal — the cancel may
                # already be in motion server-side, so `run_cascade` needs the document's REAL
                # docstatus rather than assuming no progress. Same read path as everywhere else in
                # this module. May raise — `run_cascade` owns degrading that to `readback_error`,
                # never letting it crash this flow (mirrors `cancel`'s own internal readback above).
                return client.get_document(dt, dn).get("docstatus")
            def record_outcome(self, intent, status, result, final_marker):
                return store.record_outcome(intent, status, result, final_marker)

        return run_cascade(plan=plan, marker=marker, token=token, now_epoch=self._now_epoch(),
                           now_date=self._now_date(), effects=_Effects())

    # --- PLAN (RECONCILE: F-R2 govern Payment Reconciliation) --------------------------
    def _tool_plan_reconcile(self, args):
        """PLAN a governed reconciliation. Modeled on ``_tool_plan_cascade_cancel``, not the
        single-doc submit shape (docs/plans/2026-07-09-fr2-govern-reconciliation.md): the agent
        proposes specific ``(payment, invoice, amount)`` tuples; the broker reads each named
        invoice + payment FRESH, refuses deny-biased on any unreadable doc / missing field / wrong
        company, and builds the pinned allocation graph ``run_reconcile`` will later execute
        UNCHANGED — this is the only place that graph is ever constructed from agent input."""
        target, client, store = self._route(args)
        # First slice: reconcile Sales/Purchase Invoices against a Payment Entry. Journal-Entry
        # payments are a DEFERRED extension — a JE's available amount is not a simple
        # `unallocated_amount` field (the read this tool does off the payment), so admitting one
        # would deny-bias-refuse on a missing field or need its own JE-available read path. This
        # allowlist mirrors the rest of the broker's "built and tested for these doctypes" gate
        # (`_resolve_doctype`) — the defense-in-depth layer above pacioli_guard's credential scope.
        reconcile_invoice_types = ("Sales Invoice", "Purchase Invoice")
        reconcile_payment_types = ("Payment Entry",)
        missing = _require(args, "party_type", "party", "company", "receivable_payable_account")
        if missing:
            return missing
        party_type = args.get("party_type")
        party = args.get("party")
        company = args.get("company")
        receivable_payable_account = args.get("receivable_payable_account")
        # Wrong-books: the same pinned-target guard _tool_plan_submit applies to a document's own
        # company, applied here to the reconcile's requested company.
        if target.company and company != target.company:
            return _deny(f"requested company {company!r} but target {target.name!r} is pinned "
                         f"to {target.company!r} — wrong books", stage="plan")

        allocations = args.get("allocations")
        if not isinstance(allocations, list) or not allocations:
            return _deny("allocations must be a non-empty list of {payment_type, payment_no, "
                         "invoice_type, invoice_no, allocated_amount}", stage="request")

        graph = []
        memorandum = []
        # FIX 1 (plan-time honesty): the memorandum's post-outstanding must be CUMULATIVE per
        # invoice — two rows settling the same invoice must show the real aggregate effect
        # (100 -> 60 -> 20), never each row's own outstanding-minus-its-own-amount (which would
        # misreport BOTH rows as "100 -> 60"). Seeded per invoice from that invoice's own first
        # fresh read, then carried forward as rows for that invoice are built.
        invoice_running_outstanding = {}
        for i, row in enumerate(allocations):
            if not isinstance(row, dict):
                return _deny(f"allocations[{i}] must be an object", stage="request")
            fields = {}
            for key in ("payment_type", "payment_no", "invoice_type", "invoice_no"):
                val = row.get(key)
                if not isinstance(val, str) or not val.strip():
                    return _deny(f"allocations[{i}].{key} is required", stage="request")
                fields[key] = val
            if fields["invoice_type"] not in reconcile_invoice_types:
                return _deny(f"allocations[{i}].invoice_type {fields['invoice_type']!r} is not "
                             f"reconcilable ({', '.join(reconcile_invoice_types)}) — refused",
                             stage="request")
            if fields["payment_type"] not in reconcile_payment_types:
                return _deny(f"allocations[{i}].payment_type {fields['payment_type']!r} is not "
                             f"reconcilable ({', '.join(reconcile_payment_types)}); Journal-Entry "
                             "payments are a deferred extension — refused", stage="request")
            raw_amount = row.get("allocated_amount")
            amount = _coerce_float(raw_amount)
            if amount is None:
                return _deny(f"allocations[{i}].allocated_amount must be a number",
                             stage="request")

            # The invoice: fresh read, deny-biased on an unreadable doc (ErpnextError propagates
            # to dispatch()'s structured deny) or a missing/malformed field (explicit refusal
            # here — never an uncaught crash on a bad coercion).
            invoice_doc = client.get_document(fields["invoice_type"], fields["invoice_no"])
            invoice_company = invoice_doc.get("company")
            if invoice_company != company:
                return _deny(
                    f"allocations[{i}]: {fields['invoice_type']} {fields['invoice_no']} belongs "
                    f"to company {invoice_company!r}, not {company!r} — cross-company allocation "
                    "refused", stage="plan")
            invoice_version = str(invoice_doc.get("modified") or "")
            invoice_date = str(invoice_doc.get("posting_date") or "")
            invoice_outstanding = _coerce_float(invoice_doc.get("outstanding_amount"))
            if not invoice_version or not invoice_date or invoice_outstanding is None:
                return _deny(
                    f"allocations[{i}]: {fields['invoice_type']} {fields['invoice_no']} is "
                    "missing modified/posting_date/outstanding_amount — cannot verify, refusing",
                    stage="plan")
            # FIX 4 (audit-trail self-consistency): cross-check the declared party against the
            # invoice's OWN party field (customer for Sales Invoice, supplier for Purchase
            # Invoice) — a wrong party name must never land in the permanent receipt chain.
            # Deny-biased: a missing party field on the invoice refuses, same as any other
            # unverifiable gate source in this tool.
            invoice_party_field = "customer" if fields["invoice_type"] == "Sales Invoice" \
                else "supplier"
            invoice_party = invoice_doc.get(invoice_party_field)
            if not invoice_party or invoice_party != party:
                return _deny(
                    f"allocations[{i}]: {fields['invoice_type']} {fields['invoice_no']}'s "
                    f"{invoice_party_field} {invoice_party!r} does not match the declared party "
                    f"{party!r} — refusing rather than risk a mismatched-party allocation "
                    "landing in the receipt chain", stage="plan")

            # The payment (or Journal Entry): same discipline, mirrored field for field.
            payment_doc = client.get_document(fields["payment_type"], fields["payment_no"])
            payment_company = payment_doc.get("company")
            if payment_company != company:
                return _deny(
                    f"allocations[{i}]: {fields['payment_type']} {fields['payment_no']} belongs "
                    f"to company {payment_company!r}, not {company!r} — cross-company allocation "
                    "refused", stage="plan")
            payment_version = str(payment_doc.get("modified") or "")
            payment_date = str(payment_doc.get("posting_date") or "")
            payment_unallocated = _coerce_float(payment_doc.get("unallocated_amount"))
            if not payment_version or not payment_date or payment_unallocated is None:
                return _deny(
                    f"allocations[{i}]: {fields['payment_type']} {fields['payment_no']} is "
                    "missing modified/posting_date/unallocated_amount — cannot verify, refusing",
                    stage="plan")
            # FIX 4, mirrored on the payment side.
            payment_party = payment_doc.get("party")
            if not payment_party or payment_party != party:
                return _deny(
                    f"allocations[{i}]: {fields['payment_type']} {fields['payment_no']}'s party "
                    f"{payment_party!r} does not match the declared party {party!r} — refusing "
                    "rather than risk a mismatched-party allocation landing in the receipt "
                    "chain", stage="plan")

            node = {"payment_type": fields["payment_type"], "payment_no": fields["payment_no"],
                   "payment_version": payment_version, "payment_date": payment_date,
                   "invoice_type": fields["invoice_type"], "invoice_no": fields["invoice_no"],
                   "invoice_version": invoice_version, "invoice_date": invoice_date,
                   "allocated_amount": amount, "invoice_outstanding": invoice_outstanding,
                   "payment_unallocated": payment_unallocated, "company": company}
            graph.append(node)
            inv_key = (fields["invoice_type"], fields["invoice_no"])
            if inv_key not in invoice_running_outstanding:
                invoice_running_outstanding[inv_key] = invoice_outstanding
            running_before = invoice_running_outstanding[inv_key]
            running_after = running_before - amount
            invoice_running_outstanding[inv_key] = running_after
            memorandum.append(
                f"settle {fields['payment_type']} {fields['payment_no']} against "
                f"{fields['invoice_type']} {fields['invoice_no']} for {amount}; invoice "
                f"outstanding {running_before} -> {running_after}")

        risk_flags = list(memorandum)
        # Standing notes (pin sheet hazard list): the freeze belts ERPNext bypasses for
        # reconciliation, and the system JEs reconcile() can spawn that this broker discloses but
        # does not separately govern (they ride the same marker as the reconcile that spawns them).
        # FIX 2 (honesty correction): the prior wording claimed the broker enforces BOTH freeze
        # belts ERPNext bypasses. It only independently enforces the COMPANY/PERIOD half
        # (get_period_locks/check_red_line read only company-wide locks — closed Accounting
        # Period, Period Closing Voucher, company frozen-till). It does NOT read
        # Account.freeze_account, so the PER-ACCOUNT frozen-account check ERPNext bypasses
        # (adv_adj=1 skips validate_frozen_account) is disclosed here but NOT independently
        # enforced — a recorded next increment (needs its own Account read grant + bench
        # verification of which accounts a reconcile touches), never implemented this slice.
        risk_flags.append(
            "reconciliation bypasses ERPNext's own company/period-freeze belt for this write "
            "(closed Accounting Period, Period Closing Voucher, company frozen-till) — the "
            "broker independently enforces that closed-books refusal itself here; ERPNext will "
            "not block it")
        risk_flags.append(
            "reconciliation ALSO bypasses ERPNext's PER-ACCOUNT frozen-account check "
            "(Account.freeze_account — adv_adj=1 always set for reconciliation skips "
            "validate_frozen_account) — this is DISCLOSED here but NOT yet independently "
            "enforced by the broker; a recorded next increment, not covered by this slice")
        risk_flags.append(
            "reconcile may spawn system Journal Entries this broker does not separately govern: "
            "an exchange gain/loss JE (multicurrency) and/or a credit/debit-note JE, both riding "
            "this same consent marker")

        # Plan-time check_allocation + check_red_line pass — early disclosure only, never a
        # refusal here (PLAN is a read); the authoritative check is at execute, against whatever
        # is live THEN, not what was true at plan time.
        for node in graph:
            ok, reason = check_allocation(node["allocated_amount"], node["invoice_outstanding"],
                                          node["payment_unallocated"])
            if not ok:
                risk_flags.append(f"{node['invoice_no']}: {reason} — this reconcile will be "
                                  "refused at execute unless it changes before then")
            for dt, date in ((node["invoice_type"], node["invoice_date"]),
                             (node["payment_type"], node["payment_date"])):
                locks = client.get_period_locks(company, dt, date)
                ok, reason = check_red_line(date, self._now_date(), locks)
                if not ok:
                    risk_flags.append(
                        f"{node['invoice_no']}/{node['payment_no']}: {reason} — this reconcile "
                        "will be refused at execute unless it changes before then")

        plan_id = uuid.uuid4().hex
        plan = new_plan(
            plan_id=plan_id, target=target.name, doc_version="", posting_date="",
            risk_flags=risk_flags, ts=self._now_date(), docname=plan_id, op="reconcile",
            doctype="Payment Reconciliation", graph=graph, party_type=party_type, party=party,
            receivable_payable_account=receivable_payable_account, company=company)
        store.record_plan(plan)
        return {"ok": True, "plan_id": plan.plan_id, "target": target.name, "total": len(graph),
                "memorandum": memorandum, "risk_flags": plan.risk_flags,
                "next": "have a human mint a consent marker for this plan_id (pacioli mint), "
                        "then call reconcile with it — it authorizes exactly this pinned "
                        "allocation set"}

    # --- RECONCILE (execute the frozen pinned allocation graph under one marker) -------
    def _tool_reconcile(self, args):
        """Execute a recorded reconcile plan. Deliberately takes ONLY ``plan_id``/``marker`` — no
        ``name``, no ``allocations`` — because the allocation comes from the PINNED plan graph
        alone (THE safety control the pin sheet names: the broker owns and constructs the
        reconcile payload; execute-time args can never influence it)."""
        target, client, store = self._route(args)
        plan_id, token = args.get("plan_id"), args.get("marker")
        plan = store.get_plan(plan_id)
        if plan is None:
            return _deny(f"no recorded plan {plan_id!r}; call plan_reconcile first", stage="plan")
        if plan.target != target.name:
            return _deny(f"plan {plan_id!r} was built for target {plan.target!r}, "
                         f"not {target.name!r} — wrong books", stage="plan")
        ok, reason = check_op(plan, "reconcile")
        if not ok:
            return _deny(reason, stage="plan")

        marker = store.get_marker(token)

        class _Effects:
            def claim_marker(self, reserved):
                return store.claim_marker(reserved)

            def current_version(self, dt, dn):
                return str(client.get_document(dt, dn).get("modified") or "")

            def live_company(self, dt, dn):
                # The wrong-books TOCTOU belt's live read (mirrors _governed_write's fresh-company
                # re-read). An unreadable doc raises (ErpnextError -> dispatch structured deny); a
                # missing `company` returns None, which the core refuses against the pinned company.
                return client.get_document(dt, dn).get("company")

            def locks_for(self, company, doctype, posting_date):
                return client.get_period_locks(company, doctype, posting_date)

            def live_outstanding(self, invoice_type, invoice_no):
                # Deny-biased: an unreadable doc raises (ErpnextError, propagates to dispatch()'s
                # structured deny); a missing/non-numeric field coerces to None, which
                # check_allocation itself treats as an unverifiable ceiling — refused, never
                # defaulted.
                doc = client.get_document(invoice_type, invoice_no)
                return _coerce_float(doc.get("outstanding_amount"))

            def live_unallocated(self, payment_type, payment_no):
                doc = client.get_document(payment_type, payment_no)
                return _coerce_float(doc.get("unallocated_amount"))

            def record_intent(self, body):
                return store.record_intent(body)

            def reconcile(self, rows):
                # Built from the PINNED plan fields alone (plan.company/party_type/party/
                # receivable_payable_account) plus `rows`, which run_reconcile itself builds ONLY
                # from plan.graph — never from this tool's own args.
                return client.reconcile(plan.company, plan.party_type, plan.party,
                                        plan.receivable_payable_account, rows)

            def readback_outstanding(self, invoice_type, invoice_no):
                # run_reconcile wraps this call itself (try/except degrading to an unconfirmed
                # readback_error row) — a missing/non-numeric field raising here is exactly the
                # deny-biased behavior that wrapper exists for, so this coerces directly rather
                # than swallowing the error into a silent None.
                doc = client.get_document(invoice_type, invoice_no)
                return float(doc.get("outstanding_amount"))

            def record_outcome(self, intent, status, result, final_marker):
                return store.record_outcome(intent, status, result, final_marker)

        return run_reconcile(plan=plan, marker=marker, token=token, now_epoch=self._now_epoch(),
                             now_date=self._now_date(), effects=_Effects())

    # --- AMEND (UNDO's second half: the corrected re-draft, receipts but no marker) ------
    def _amend_document(self, doctype, args):
        target, client, store = self._route(args)
        name = args.get("name")
        doc = client.get_doc_for_amend(doctype, name)
        if doc.get("docstatus") != 2:
            return _deny(f"{name} is not a cancelled document (docstatus="
                         f"{doc.get('docstatus')}); only a cancelled document can be amended",
                         stage="amend")
        company = doc.get("company")
        if target.company and company != target.company:
            return _deny(f"document belongs to company {company!r} but target "
                         f"{target.name!r} is pinned to {target.company!r} — wrong books",
                         stage="amend")
        # A second amendment of the same cancelled document is almost always a mistake — deny
        # and NAME the existing one, at ANY docstatus. An unreadable search raised above (deny,
        # never reads as "no amendments").
        existing = client.find_amendments(doctype, name)
        if existing:
            shown = ", ".join(str(d.get("name")) for d in existing[:5] if isinstance(d, dict))
            more = f" (+{len(existing) - 5} more)" if len(existing) > 5 else ""
            return _deny(f"{name} already has {len(existing)} amendment(s): {shown}{more} — "
                         "a second amendment of the same cancelled document is almost always a "
                         "mistake; work with the existing one (or delete it first)",
                         stage="amend")
        # The F1 fix (2026-07-17, found by the first dogfood drive): an amendment born under an
        # ACTIVE workflow used to arrive with NO workflow state — stuck, because
        # request_workflow_transition (rightly) refuses a null state and the bench's REST insert
        # does not backfill one (live-observed). Seat the draft at the workflow's initial state
        # (frappe's own states[0] convention — workflow.initial_seat). Deny-biased, gated BEFORE
        # the intent so a refusal never leaves an orphan: ambiguous/malformed config refuses with
        # the same reasons as every other workflow gate; a workflow that CANNOT seat a draft
        # refuses too — creating it unseated would silently recreate the stuck F1 shape. No
        # workflow configured = no seat, byte-identical behaviour to before.
        active, wf_deny = _resolve_active_workflow(client, doctype)
        if wf_deny:
            return wf_deny
        seat = None
        if active is not None:
            field, state, why = workflow.initial_seat(active)
            if why is None:
                # The strip-surface guard (review finding [0]), BEFORE the intent: a
                # workflow_state_field naming a protected key (amended_from, docstatus, a child
                # table, …) must refuse, never overwrite — amend_payload's own raise is the belt
                # under this suspender, but by then an intent would already be recorded.
                why = seat_conflict(field, doc)
            if why is not None:
                return _deny(f"Workflow {active.get('name')!r} cannot seat a fresh draft "
                             f"({why}); an amendment created without a workflow state would be "
                             "stuck — fix the workflow configuration first", stage="workflow")
            seat = (field, state)
        # Receipts without a marker: the intent is recorded DURABLY before the insert (a crash
        # in between leaves an orphan to reconcile, never a silent draft), the outcome after.
        # final_marker=None — there is no consent grant to settle (see pacioli.amend for why).
        # Built directly here (not via the spine), so — unlike submit/cancel — doctype travels
        # in BOTH the intent and the outcome body.
        intent_body = {"tool": "amend", "target": target.name, "docname": name,
                       "doctype": doctype, "doc_version": str(doc.get("modified") or ""),
                       "transition": "2->0(draft)"}
        if seat is not None:
            intent_body["workflow_seat"] = {"field": seat[0], "state": seat[1]}
        try:
            intent = store.record_intent(intent_body)
        except Exception as exc:  # noqa: BLE001 — pre-wire: nothing was created on the bench yet
            return _deny(f"could not durably record the amend intent ({exc}); nothing was sent to "
                         "the bench — no draft was created; safe to retry once the store is healthy",
                         stage="amend")
        try:
            created = client.create_amended_draft(doctype, doc, seat=seat)
        except Exception as exc:  # noqa: BLE001 — any insert failure becomes a recorded outcome
            try:
                store.record_outcome(intent, "failed", {"error": str(exc), "doctype": doctype},
                                     final_marker=None)
            except Exception as rexc:  # noqa: BLE001 — double-fault: never crash; intent is an orphan
                return _deny(f"amend failed ({exc}) and the failure outcome could not be recorded "
                             f"either ({rexc}); nothing landed on the bench; the intent is an "
                             "orphan, reconcile by hand (prove_orphans)", stage="execute")
            return _deny(f"amend failed: {exc}", stage="execute")
        result = {"name": str(created.get("name") or ""),
                  "docstatus": int(created.get("docstatus", -1)),
                  "amended_from": str(created.get("amended_from") or name),
                  "modified": str(created.get("modified") or ""),
                  "doctype": doctype}
        if seat is not None:
            # Disclose the seat — in the result AND (via this dict, recorded below) the outcome
            # receipt — CONFIRMED against the bench's ANSWER, never just the request (the E1
            # rule, review finding [8]): a bench that silently drops the seated field (read-only
            # field, permission-stripped) must never leave a committed receipt asserting the
            # draft was seated when it wasn't. `confirmed: false` = the draft exists but is in
            # the stuck shape — the operator seats it from the desk (one save re-seats it).
            result["workflow_seat"] = {"field": seat[0], "state": seat[1],
                                       "confirmed": created.get(seat[0]) == seat[1]}
        confess = _record_committed_or_confess(store, intent, result, result["name"], "amend")
        if confess is not None:
            return confess
        return {"ok": True, "result": result,
                "next": f"correct the draft {result['name']}, then plan_submit it — submitting "
                        "the amendment is a governed write and needs its own plan + "
                        "human-minted marker"}





    # --- WORKFLOW (the non-approving transition, receipts but no marker) ----------------
    def _tool_request_workflow_transition(self, args):
        """The non-approving half of CONSENT's second gate — the amend precedent applied to a
        workflow move: no marker (reversible: a workflow-state change, never a docstatus change),
        but the intent+outcome receipt pair is still written durably around the call, and a
        failure never leaves a silent state. Every approving transition is refused by
        :func:`pacioli.workflow.check_transition` before ``apply_workflow`` is ever called.
        ``pacioli_doctype`` selects the document's DocType (default Sales Invoice).

        Honest residual (mirrors amend's, ``pacioli/amend.py``): no CAS claim guards this path —
        two concurrent requests for the same transition may both pass ``check_transition`` and
        both call ``apply_workflow``. The blast radius is bounded: frappe's own state machine
        refuses the second call once the first has moved the document off ``current_state``
        (the transition it matched no longer applies), so this is a duplicate-attempt race, never
        a double-posting — reconciled the same way an orphan intent is, by hand."""
        missing = _require(args, "name", "action")
        if missing:
            return missing
        doctype, deny = _resolve_doctype(args)
        if deny:
            return deny
        target, client, store = self._route(args)
        name, action = args.get("name"), args.get("action")
        doc = client.get_document(doctype, name)
        company = doc.get("company")
        if target.company and company != target.company:
            return _deny(f"document belongs to company {company!r} but target "
                         f"{target.name!r} is pinned to {target.company!r} — wrong books",
                         stage="workflow")
        active, deny = _resolve_active_workflow(client, doctype)
        if deny:
            return deny
        if active is None:
            return _deny(f"{name} has no active Workflow governing {doctype!r}; "
                         "there is no transition to request", stage="workflow")
        state_field = (str(active.get("workflow_state_field") or "workflow_state").strip()
                       or "workflow_state")  # stripped: the seat writes the stripped name
        current_state = client.get_workflow_state(doctype, name, state_field)
        ok, reason, transition = workflow.check_transition(active, current_state, action)
        if not ok:
            return _deny(reason, stage="workflow")
        next_state = transition.get("next_state")
        # Receipts without a marker (amend precedent): intent recorded DURABLY before the call —
        # a crash in between leaves an orphan to reconcile, never a silent state move. Built
        # directly here (not via the spine), so doctype travels in both intent and outcome.
        try:
            intent = store.record_intent(
                {"tool": "workflow_transition", "target": target.name, "docname": name,
                 "doctype": doctype, "doc_version": str(doc.get("modified") or ""),
                 "transition": f"state:{current_state}->{next_state}"})
        except Exception as exc:  # noqa: BLE001 — pre-wire: apply_workflow was never called
            return _deny(f"could not durably record the workflow-transition intent ({exc}); nothing "
                         "was sent to the bench — no state was moved; safe to retry once the store "
                         "is healthy", stage="workflow")
        try:
            result = client.apply_workflow(doctype, name, action)
        except Exception as exc:  # noqa: BLE001 — any apply failure becomes a recorded outcome
            try:
                store.record_outcome(intent, "failed", {"error": str(exc), "doctype": doctype},
                                     final_marker=None)
            except Exception as rexc:  # noqa: BLE001 — double-fault: never crash; intent is an orphan
                return _deny(f"workflow transition failed ({exc}) and the failure outcome could not "
                             f"be recorded either ({rexc}); no state was moved on the bench; the "
                             "intent is an orphan, reconcile by hand (prove_orphans)", stage="execute")
            return _deny(f"workflow transition failed: {exc}", stage="execute")
        out_result = {"name": str(result.get("name") or name),
                      state_field: str(result.get(state_field) or next_state or ""),
                      "modified": str(result.get("modified") or ""),
                      "doctype": doctype}
        confess = _record_committed_or_confess(store, intent, out_result, out_result["name"],
                                               "workflow transition")
        if confess is not None:
            return confess
        return {"ok": True, "result": out_result,
                "next": "call workflow_status to see the legal next transitions from here"}

    # --- PROVE reads -----------------------------------------------------------------
    def _tool_prove_verify(self, args):
        _, _, store = self._route(args)
        ok, reason = store.verify(expected_head=args.get("expected_head"))
        return {"ok": ok, "reason": reason, "head": store.head(),
                "count": len(store.receipts())}

    def _tool_prove_orphans(self, args):
        _, _, store = self._route(args)
        return {"ok": True, "orphans": [
            {"seq": r.seq, "ts": r.ts, "body": r.body} for r in store.orphans()
        ]}


# --- generate the 20 mechanical wrapper methods onto PacioliBroker -----------------------------
# dispatch() resolves a tool via getattr(self, f"_tool_{name}") \u2014 unchanged. These wrappers are
# the per-(verb, doctype) one-liners the descriptor makes unnecessary to hand-write. `doctype` is
# bound as a default arg inside the factory (NOT captured from the loop) so each wrapper reaches its
# OWN doctype, never the loop's last (the classic late-binding-closure trap).
def _make_mechanical_wrapper(helper_name, doctype):
    def _wrapper(self, args, _helper=helper_name, _doctype=doctype):
        return getattr(self, _helper)(_doctype, args)
    return _wrapper


for _desc in DESCRIPTORS:
    for _verb, (_name_of, _schema_of, _desc_tmpl, _helper_name) in _MECHANICAL_VERBS.items():
        setattr(PacioliBroker, f"_tool_{_name_of(_desc)}",
                _make_mechanical_wrapper(_helper_name, _desc.doctype))
