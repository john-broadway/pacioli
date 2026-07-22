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

**Breadth (Sales Order) — Wave 1 of the breadth campaign, a fifth doctype
(docs/plans/2026-07-20-breadth-campaign-past-120.md).** Five more doctype-named tools
(``*_sales_order``) wrap the same generic handlers, pinned to Sales Order (``party_field=
"customer"``, ``submit_via`` = the proven run_method surface — Sales Order overrides neither
``submit()`` nor ``cancel()``, confirmed from ``sales_order.py`` version-16 source, so it needs no
Journal-Entry-style client_rpc exception). Full semantics pinned in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block. The one genuinely new mechanical
branch this doctype forces: **Sales Order carries no ``posting_date`` field at all** (confirmed
absent from ``sales_order.json``; its own date field is ``transaction_date``) — every call site
that used to hardcode ``doc.get("posting_date")`` for the broker's own closed-books check now
resolves the fieldname through :func:`_date_field_for` first, which reads the new
``SUPPORTED_DOCTYPES[doctype]["date_field"]`` (defaulting to ``"posting_date"`` for every other
doctype, unchanged, and for any doctype outside ``SUPPORTED_DOCTYPES`` too — a 'generic' cascade
node this broker has no descriptor for gets the same best-effort default it always did). This
never renames the internal Plan vocabulary itself: ``plan.posting_date`` stays the stored/response
key for every doctype including Sales Order — :func:`pacioli.plan.check_red_line` already treats
it as an opaque ISO-date value, blind to which ERPNext field it was read from, so only the
EXTRACTION site changes, never the contract downstream of it. Four call sites needed the swap:
``_tool_plan_submit``, ``_tool_plan_cancel``, ``_governed_write`` (the execute-time gate), and
``_cascade_node_meta`` (so a Sales Order node inside a cascade graph gets the same fix, with zero
change to ``cascade.py`` itself — see below). ``_list_documents``'s ``SUPPORTED_DOCTYPES[doctype]``
read grows the same ``date_field`` lookup, threaded to :meth:`pacioli.erpnext.ErpnextClient.
list_documents` exactly like ``party_field`` already is, so ``list_sales_orders`` asks the bench
for ``transaction_date`` in its field list rather than a column Sales Order's schema doesn't carry
(the same unknown-column failure class ``get_period_locks``'s own docstring documents for a stale
``filters`` column — a real functional fix, not a cosmetic rename).

**Cascade (``pacioli/cascade.py``) needed NO changes.** ``build_cascade``/``run_cascade`` are
fully doctype-blind — they consume whatever ``node_meta(doctype, docname)`` hands back under the
internal ``"posting_date"`` KEY (never the ERPNext field name), so fixing that closure
(``_cascade_node_meta`` above) is the whole fix; Sales Order participates in a cascade graph as a
'modeled' node (now that it is in ``SUPPORTED_DOCTYPES``) with no cascade.py edit at all. Sales
Order is typically the PARENT a Delivery Note / Sales Invoice is built FROM — ERPNext's own
``get_submitted_linked_docs`` (the call ``_cascade_fetch_linked`` wraps) already walks the live
Link-field graph generically, so a submitted Delivery Note or Sales Invoice referencing a Sales
Order is discovered and ordered ahead of it in cancel order with zero doctype-specific cascade
code, exactly like every other doctype's dependents already are. The one edge NOT covered by that
walk (by design, unchanged by this increment): ``get_submitted_linked_docs`` only surfaces
SUBMITTED (docstatus 1) dependents — a DRAFT Sales Invoice referencing a Sales Order is invisible
to it, yet ERPNext's own ``on_cancel`` (``check_nextdoc_docstatus``, sales_order.py:580-594)
throws on exactly that case ("Sales Invoice {0} must be deleted before cancelling this Sales
Order"). This is not a new gap Sales Order introduces — it is the same "a draft dependent is
invisible to the submitted-only blast-radius read" shape every other doctype already has — and it
needs no new broker code: the answered refusal from ERPNext surfaces through the same generic
exception handling every other doctype's cancel already relies on ("ERPNext's own cancel-blocks
are honored, never bypassed").

**Breadth (Purchase Order) — Wave 1, a sixth doctype, mechanically identical to Sales Order.**
Five more doctype-named tools (``*_purchase_order``) wrap the same generic handlers, pinned to
Purchase Order (``party_field="supplier"``, ``submit_via`` = the proven run_method surface —
Purchase Order overrides neither ``submit()`` nor ``cancel()``, confirmed from
``purchase_order.py`` version-16 source). Full semantics pinned in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block; dossier at
``docs/plans/dossiers/purchase_order.md``. **This doctype forces NO new mechanical branch** —
Purchase Order carries no ``posting_date`` field either (confirmed absent from
``purchase_order.json``; its own date field is ``transaction_date``, exactly like Sales Order), so
it rides the ``date_field``/:func:`_date_field_for` mechanism Sales Order's landing already built
and exhaustively tested (``TestSalesOrderDateField``, ``test_erpnext.py``'s
``TestSupportedDoctypesConfig``/``TestList``) — the four call sites
(``_tool_plan_submit``/``_tool_plan_cancel``/``_governed_write``/``_cascade_node_meta``) and
``_list_documents``'s threading into :meth:`pacioli.erpnext.ErpnextClient.list_documents` are
unchanged; adding ``PURCHASE_ORDER: {..., "date_field": "transaction_date"}`` to
``SUPPORTED_DOCTYPES`` is the whole fix.

**Cascade needed NO changes**, same finding as Sales Order and for the same reason: ``cascade.py``
is fully doctype-blind. Purchase Order is typically the PARENT a Purchase Receipt / Purchase
Invoice is built FROM (plus subcontracting's Stock Entry / Subcontracting Order edges) —
ERPNext's own ``get_submitted_linked_docs`` already walks the live Link-field graph generically,
so a submitted Purchase Receipt/Purchase Invoice/Subcontracting Order (or a drop-ship Sales Order)
referencing a Purchase Order is discovered and ordered ahead of it in cancel order with zero
doctype-specific cascade code. Unlike Sales Order, Purchase Order's own ``on_cancel`` has NO
``check_nextdoc_docstatus``-style draft-dependent refusal (confirmed absent from
``purchase_order.py`` by grep) — but it does carry an analogous disclosure gap of its own shape:
``check_for_on_hold_or_closed_status("Material Request", "material_request")`` refuses the cancel
if the source Material Request is On Hold/Closed, a STATUS check (not the submitted-docstatus
graph ``get_submitted_linked_docs`` walks), so ``plan_cancel``'s disclosure cannot preview it in
advance either. No new broker code needed here — the answered refusal from ERPNext surfaces
through the same generic exception handling every other doctype's cancel already relies on.

**Breadth (Material Request) — Wave 1, a seventh doctype, and the first with a genuinely NEW
list-tier shape.** Five more doctype-named tools (``*_material_request``) wrap the same generic
handlers, pinned to Material Request (``party_field=None`` — the header-level ``customer`` field
is present but type-gated to ``material_request_type=="Customer Provided"`` and forcibly cleared
by the controller otherwise, never a stable counterparty; ``submit_via`` = the proven run_method
surface — Material Request overrides neither ``submit()`` nor ``cancel()``, confirmed from
``material_request.py`` version-16 source). Full semantics pinned in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block; dossier at
``docs/plans/dossiers/material_request.md``. **This doctype forces exactly ONE new mechanical
branch, and it lives entirely in ``erpnext.py``'s ``_list_fields`` — nothing here in ``tools.py``
changes.** Material Request is the first doctype to carry ``status`` while lacking
``grand_total``; :func:`pacioli.erpnext._list_fields` grows a third branch alongside Journal
Entry's for that combination (party_field=None already threads through unchanged — ``tools.py``'s
``_list_documents`` has passed ``cfg["party_field"]`` straight through since Journal Entry's own
``None`` landed, no new conditional needed here). ``date_field="transaction_date"`` reuses the
mechanism Sales Order's landing built and Purchase Order's already reused (the four call sites —
``_tool_plan_submit``/``_tool_plan_cancel``/``_governed_write``/``_cascade_node_meta`` — and
``_list_documents``'s threading into :meth:`pacioli.erpnext.ErpnextClient.list_documents` are
unchanged for the third time running).

**Cascade needed NO changes**, same finding as Sales Order and Purchase Order, for the same
reason: ``cascade.py`` is fully doctype-blind. Material Request is typically the PARENT a Purchase
Order / Stock Entry / Work Order is built FROM (plus Pick List, Delivery Note, and the
manufacturing-planning back-links) — ERPNext's own ``get_submitted_linked_docs`` already walks the
live Link-field graph generically across the 14 external doctypes carrying a Link to Material
Request (see :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s comment block for the full list), so a
submitted dependent referencing a Material Request is discovered and ordered ahead of it in cancel
order with zero doctype-specific cascade code. Material Request's own ``before_cancel`` carries an
analogous disclosure gap to Purchase Order's MR-status check, seen from the other side: it refuses
the cancel outright if the Material Request's OWN status is On Hold/Closed
(``check_on_hold_or_closed_status``), a status read ``plan_cancel``'s disclosure cannot preview in
advance either — no new broker code needed, the answered refusal surfaces through the same generic
exception handling every other doctype's cancel already relies on. The six-way
``material_request_type`` fulfillment-mode fork (Purchase/Material Transfer/Material Issue/
Manufacture/Subcontracting/Customer Provided) is documented in ``erpnext.py``'s comment block but
deliberately NOT built into a plan-tier disclosure column here — the same treatment the Stock
Entry dossier proposes for its own ``purpose`` polymorphism is owed to both stock rows together at
whatever point that machinery gets built, not invented ad hoc for Material Request alone (keeping
this landing mechanical, per the campaign's per-doctype DoD).

**Breadth (Delivery Note) — Wave 1, the eighth doctype, and the first STOCK-PRIMARY row (it posts
real Stock Ledger AND (conditionally) GL rows on its own submit, unlike the pre-accounting SO/PO/MR
trio).** Five more doctype-named tools (``*_delivery_note``) wrap the same generic handlers, pinned
to Delivery Note (``party_field="customer"``, ``submit_via`` = the proven run_method surface —
Delivery Note overrides neither ``submit()`` nor ``cancel()``, confirmed from ``delivery_note.py``
version-16 source). Full semantics pinned in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own
comment block (and the module docstring there); dossier at ``docs/plans/dossiers/delivery_note.md``.
**This doctype forces NO new mechanical branch** — Delivery Note carries a real ``posting_date``
field (confirmed present, unlike SO/PO/MR), so it rides the plain PRE-EXISTING default path of
:func:`_date_field_for` (the same one SI/PI/PE/JE already use), never the ``transaction_date``
branch. The genuinely new finding is semantic, not mechanical: ``ledger_preview``'s ``gl_data`` is
REAL and non-empty for Delivery Note (ERPNext's own preview RPC explicitly seeds an in-memory
``update_stock_ledger()`` for Delivery Note before calling ``make_gl_entries()`` — see
``erpnext.py``'s citation), so ``plan_submit``'s ``projected_gl`` genuinely discloses the
Inventory/COGS rows a submit would post, unlike Sales/Purchase Order's near-always-empty preview.

**Cascade needed NO changes**, same finding as every prior breadth increment, for the same reason:
``cascade.py`` is fully doctype-blind. Delivery Note's own real Link-field dependents (Sales
Invoice Item, Purchase Receipt, Stock Entry, Shipment Delivery Note, Packing Slip, Delivery Stop,
POS Invoice Item — see ``erpnext.py``'s comment block for the full grep-confirmed list) are
discovered by ERPNext's own ``get_submitted_linked_docs`` with zero doctype-specific cascade code.
**One PARTIAL disclosure gap, sharper than any prior doctype's:** ERPNext's own
``check_next_docstatus`` refuses a Delivery Note's cancel if a submitted Sales Invoice OR a
submitted Installation Note references it. The Sales Invoice half is covered by the generic
blast-radius walk above (a real Link field); the Installation Note half is NOT — its
back-reference (``Installation Note Item.prevdoc_docname``) is a plain Data field in ERPNext's own
schema, not a Link, so the generic walker has no edge to find it at any docstatus. This is recorded
honestly in ``erpnext.py`` rather than silently claimed covered; no new broker gate is built,
because the answered ERPNext refusal still surfaces safely at the real cancel call regardless.

**A real code fix lands alongside this doctype, in the shared ``is_return`` disclosure below**
(:func:`_return_risk_flags`, now doctype-aware) — see its own docstring and ``erpnext.py``'s
SUPPORTED_DOCTYPES comment block for the full source-cited finding: Delivery Note is the first
doctype where ``is_return`` is set but the doctype carries no ``update_outstanding_for_self`` field
and no receivable/payable balance of its own at all, so the prior unconditional "the original's
outstanding is reduced" wording was a FALSE claim waiting to happen the moment a stock-only return
doctype landed. Fixed by keying the branch on the field's PRESENCE on the fetched document, not its
truthiness.

**Breadth (Purchase Receipt) — Wave 1, the ninth doctype, and the SECOND STOCK-PRIMARY row (with
Delivery Note — the received-goods mirror of Delivery Note's shipped-goods shape).** Five more
doctype-named tools (``*_purchase_receipt``) wrap the same generic handlers, pinned to Purchase
Receipt (``party_field="supplier"``, ``submit_via`` = the proven run_method surface — Purchase
Receipt overrides neither ``submit()`` nor ``cancel()``, confirmed from ``purchase_receipt.py``
version-16 source). Full semantics pinned in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own
comment block (and the module docstring there); dossier at
``docs/plans/dossiers/purchase_receipt.md``. **This doctype forces NO new mechanical branch** —
Purchase Receipt carries a real ``posting_date`` field (confirmed present, the same shape Delivery
Note already rides), so it needs no ``date_field`` branch, and ``status``/``grand_total`` are both
present (the generic branch, same as Delivery Note). The genuinely new findings are semantic:
``ledger_preview``'s ``gl_data`` is REAL and non-empty for Purchase Receipt too — confirmed at the
identical ``stock_controller.py:2109-2110`` whitelist line Delivery Note's landing cited (Purchase
Receipt is in fact the FIRST-named doctype there) — so ``plan_submit``'s ``projected_gl`` discloses
the Stock/Asset-vs-SRBNB rows a submit would post, mirroring Delivery Note's disclosure exactly
(``TestPurchaseReceiptLedgerDisclosure``).

**Cascade needed NO changes**, same finding as every prior breadth increment: ``cascade.py`` is
fully doctype-blind. Purchase Receipt's own real Link-field dependents (Stock Entry, Stock Entry
Detail, Delivery Note, Asset, Purchase Invoice Item — see ``erpnext.py``'s comment block for the
full grep-confirmed list, including a dossier correction on Stock Entry's own field name) are
discovered by ERPNext's own ``get_submitted_linked_docs`` with zero doctype-specific cascade code.
**Purchase Receipt's own cancel-refusal disclosure is FULLY covered — a genuine divergence from
Delivery Note's partial gap:** ERPNext's ``check_next_docstatus``/``on_cancel`` refuses only on a
submitted Purchase Invoice, via a real ``Purchase Invoice Item.purchase_receipt`` Link field, so
the generic blast-radius walk sees it in full; there is no Installation-Note-shaped blind spot
here. **A WIDER gap Delivery Note did not have, instead:** ``check_for_on_hold_or_closed_status(
"Purchase Order", ...)`` fires on BOTH submit (from ``validate()``) and cancel, not cancel-only —
a status read on a different document, invisible to ``plan_submit``'s AND ``plan_cancel``'s
disclosure alike. No new broker gate: the answered ERPNext refusal surfaces safely at the real
call either way, recorded honestly as wider than the cancel-only shape every prior doctype had.
**A genuine orphan hazard, different in kind from a cancel-refusal gap:** Landed Cost Voucher's
own back-reference to a Purchase Receipt (``Landed Cost Purchase Receipt.receipt_document``) is a
Dynamic Link, not a real Link — invisible to ``get_submitted_linked_docs`` at any docstatus, and
ERPNext's own ``on_cancel`` never checks for a linked LCV at all (no refusal exists to preview),
so cancelling a Purchase Receipt can silently stale an LCV's distributed cost amounts — documented
here as a disclosure note, per the dossier's own envelope-campaign hazard list, not built into a
gate.

**No further code fix needed in the shared ``is_return`` disclosure** (:func:`_return_risk_flags`)
— Purchase Receipt confirms absent ``update_outstanding_for_self`` (148-field list), the identical
stock-only-return shape Delivery Note's landing already fixed the function for; this doctype
exercises that existing branch live (``TestPurchaseReceiptReturnDisclosure``) rather than needing
a new one.

**Breadth (Stock Entry) — Wave 1, the tenth and LAST doctype, and the hardest: a third stock-
primary row, the first genuinely polymorphic one (13-way ``purpose``), and the first whose party
field is client-JS-gated rather than server-enforced.** Five more doctype-named tools
(``*_stock_entry``) wrap the same generic handlers, pinned to Stock Entry (``party_field=None`` —
weaker-grounded than Material Request's own ``None`` decision, since Stock Entry's ``supplier``
field is never read or cleared server-side at all, only hidden by a client-side JS eval;
``submit_via`` = the proven run_method surface — confirmed by reading all 4875 lines of
``stock_entry.py``, no ``submit()``/``cancel()`` override). Full semantics pinned in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block (and the module docstring there);
dossier at ``docs/plans/dossiers/stock_entry.md``. **This doctype forces a genuinely NEW
mechanical branch** — the fourth ``_list_fields`` shape (``status`` absent, ``grand_total``
absent, AND ``party_field=None`` all at once, a combination no prior doctype exercised;
``purpose`` + the three ``total_*``/``value_difference`` fields stand in). ``date_field`` needs no
branch (a real ``posting_date`` field, the same default path Delivery Note/Purchase Receipt ride).

``ledger_preview``'s ``gl_data`` is REAL and non-empty for Stock Entry too — the THIRD-named
doctype in the same ``stock_controller.py:2109-2110`` whitelist Delivery Note's and Purchase
Receipt's own landings cited, closing the set all three stock-primary doctypes now occupy
(``TestStockEntryLedgerDisclosure``). **Cascade needed NO changes**, same finding as every prior
breadth increment: ``cascade.py`` is fully doctype-blind, and Stock Entry has only ONE external
Link-field dependent in the whole v16 checkout (Journal Entry's own ``stock_entry`` field,
Credit/Debit Note vouchers only) — every other doctype in its own purpose-driven cascade map
(Work Order, Subcontracting Order, Quality Inspection, Project, Material Request) is a target
Stock Entry writes INTO via a PUSH, never a source carrying a Link back for
``get_submitted_linked_docs`` to discover; a genuine divergence from Delivery Note's/Purchase
Receipt's own PULL-shaped ``check_next_docstatus`` refusal — Stock Entry's own cancel names no
downstream-submitted-document refusal at all. The widest disclosure gap of the ten doctypes landed
so far: ``validate_closed_subcontracting_order`` fires from ``validate()`` itself (i.e. on every
save/submit) AND ``on_cancel``, a status read on a linked Subcontracting Order invisible to either
disclosure — the same shape Purchase Receipt's own PO-hold check widened, now on both directions
for the SAME doctype axis. The 13-way ``purpose`` polymorphism is documented in ``erpnext.py``'s
comment block (which purposes touch Work Order/Subcontracting Order/Quality Inspection on
submit/cancel, cited to the controller methods) but deliberately NOT built into a plan-tier
disclosure column here — the OWED treatment the campaign has named since Material Request's own
landing, kept mechanical rather than invented ad hoc for the last Wave 1 row.

**No code change needed in the shared ``is_return`` disclosure** (:func:`_return_risk_flags`) —
Stock Entry carries ``is_return`` but confirmed NO ``return_against`` field at all (a third
is_return shape, traced to ``work_order.py``'s ``make_stock_return_entry``: a raw-material-
direction flag on an ordinary Work Order transfer, not a credit-note concept), so the function's
``return_against``-gated settlement branch structurally never fires for this doctype — no false
claim is possible, only the top-line RETURN + FREE-STANDING flags apply
(``TestStockEntryReturnDisclosure``). The top-line "credit note... sale/purchase" wording is
imprecise for a pure inventory redirection — a documented imprecision, not a false claim, and
deliberately not reworded without a fourth real-world is_return shape to design the fix against.

**Breadth (Supplier Quotation) — Wave 2, the eleventh doctype, and the first byte-for-byte
mechanical twin of an already-landed doctype (Purchase Order).** Five more doctype-named tools
(``*_supplier_quotation``) wrap the same generic handlers, pinned to Supplier Quotation
(``party_field="supplier"``, ``submit_via`` = the proven run_method surface — Supplier Quotation
overrides neither ``submit()`` nor ``cancel()``, confirmed from ``supplier_quotation.py``
version-16 source). Full semantics pinned in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own
comment block (and the module docstring there); dossier at
``docs/plans/dossiers/supplier_quotation.md``. **This doctype forces NO new mechanical branch at
all** — Supplier Quotation carries no ``posting_date`` field either (confirmed absent from
``supplier_quotation.json``; its own date field is ``transaction_date``, the fourth doctype on
that pattern with Sales Order/Purchase Order/Material Request), so it rides the
``date_field``/:func:`_date_field_for` mechanism Sales Order's landing built and Purchase
Order/Material Request already reused unchanged; ``status``/``grand_total`` are both present, so
it rides the generic ``_list_fields`` branch too, the same shape Purchase Order/Delivery
Note/Purchase Receipt already ride. Its :data:`SUPPORTED_DOCTYPES` entry is **identical** to
Purchase Order's on every axis (``party_field``, ``submit_via``, ``date_field`` all match) —
adding ``SUPPLIER_QUOTATION: {"party_field": "supplier", "submit_via": SUBMIT_VIA_RUN_METHOD,
"date_field": "transaction_date"}`` is the whole functional change; no call site in this module
(``_tool_plan_submit``/``_tool_plan_cancel``/``_governed_write``/``_cascade_node_meta``/
``_list_documents``) needed to change.

**Cascade needed NO changes**, same finding as every prior breadth increment: ``cascade.py`` is
fully doctype-blind. Supplier Quotation is a LEAF in the cancel dependency graph — the only real
``Link`` fields anywhere in the v16 checkout pointing AT it are Purchase Order's own ``ref_sq``
and Purchase Order Item's ``supplier_quotation``, both optional (no ``reqd``) read-only
back-references that ERPNext's own Purchase Order cancel never even checks; there is no
dependent that must be discovered and ordered ahead of a Supplier Quotation cancel. Supplier
Quotation's own submit/cancel carries exactly ONE side-effect outside itself:
``update_rfq_supplier_status()`` writes ``Request for Quotation Supplier.quote_status`` on the
linked RFQ — a status write on a sibling document, reversed between submit (``include_me=1``)
and cancel (``include_me=0``), never a docstatus change on the RFQ and never a cascade. No
ERPNext-native cancel refusal comparable to Sales Order's draft-SI check or Purchase
Order/Purchase Receipt's on-hold/closed status reads exists on Supplier Quotation's own cancel —
confirmed absent by reading ``supplier_quotation.py`` in full (362 lines); a cancel succeeds
unless blocked by a period-freeze or permission check, the same generic exception handling every
other doctype's cancel tool already relies on for its own gaps.

**Breadth (Quotation) — Wave 2, the twelfth doctype, and the FIRST DYNAMIC-PARTY doctype landed —
the judgment call of Wave 2.** Five more doctype-named tools (``*_quotation``) wrap the same
generic handlers, pinned to Quotation. Full semantics pinned in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block (and the module docstring there);
dossier at ``docs/plans/dossiers/quotation.md``. **The key decision, made at the erpnext.py layer
and inherited here unchanged: ``party_field=None``, not a forced single Dynamic Link.** Quotation
carries no static ``customer``-shaped header field at all — only ``quotation_to`` (which DocType)
paired with ``party_name`` (a Dynamic Link keyed on it). The broker's ``party_field`` config names
ONE static fieldname; a Dynamic Link does not fit that shape, and splicing ``party_name`` alone
into a list row would disclose a bare record name with no doctype to interpret it by — a worse
disclosure than Journal Entry's/Material Request's own ``None``. Following Material Request's
None-precedent (a doctype-specific pair of CONTEXT columns standing in for the missing party slot,
never invented ad hoc) rather than reshaping ``party_field`` itself to accept a two-field pair —
that would ripple every consumer (``_list_documents``, ``_cascade_node_meta``, the cascade
disclosure) for a shape only ONE doctype needs, when the existing list-tier context-column pattern
already covers it with zero new plumbing beyond ``erpnext.py``'s own ``_list_fields``.

**This doctype forces a genuinely NEW mechanical branch, but it lives entirely in ``erpnext.py``'s
``_list_fields`` — nothing else in this module changes.** Quotation is the FIFTH ``_list_fields``
branch: ``party_field=None`` (like Journal Entry/Material Request/Stock Entry) combined with
``status`` AND ``grand_total`` BOTH present (unlike any of those three) — a combination no prior
doctype exercised. ``date_field="transaction_date"`` reuses the mechanism Sales Order's landing
built and Purchase Order/Material Request/Supplier Quotation already reused (the four call sites —
``_tool_plan_submit``/``_tool_plan_cancel``/``_governed_write``/``_cascade_node_meta`` — and
``_list_documents``'s threading into :meth:`pacioli.erpnext.ErpnextClient.list_documents` are
unchanged for the fourth time running); ``party_field=None`` itself needed no new conditional here
either — ``_list_documents`` has passed ``cfg["party_field"]`` straight through unchanged since
Journal Entry's own ``None`` landed.

**Cascade needed NO changes**, same finding as every prior breadth increment: ``cascade.py`` is
fully doctype-blind. Grepping the full v16 checkout for a real ``Link`` field naming Quotation
returns exactly two hits: Quotation's own self-referencing ``amended_from`` and Sales Order Item's
``prevdoc_docname`` (the field ``make_sales_order()``'s own field_map populates when a Sales Order
is mapped from a Quotation) — ERPNext's own ``get_submitted_linked_docs`` walks this Link-field
graph generically (including child-table fields), so a submitted Sales Order built from a
Quotation is discovered and ordered ahead of it in cancel order with zero doctype-specific cascade
code. Quotation's own ``on_cancel`` carries NO downstream-submitted-document refusal of its own
(unlike Delivery Note/Purchase Receipt's ``check_next_docstatus``-shaped calls) — a Quotation can
be cancelled even with Sales Orders already built from it, ERPNext's own design choice, not a
broker gap; its real side-effects are Opportunity/Lead status resets (``update_opportunity``/
``update_lead``), never a ledger reversal, since Quotation posts no GL/SL of its own to begin with.

**Quotation's own landing repoints two exemplars.** ``TestUnsupportedDoctypeDenied`` and
``TestCascadeCancel`` (``test_tools.py``) both used ``"Quotation"`` as the standing example of an
UNsupported doctype — that repoint chain (Payment Entry -> Journal Entry -> Delivery Note ->
Purchase Receipt -> Quotation) now ends, because Quotation itself lands here. Repointed to
``"Transaction Deletion Record"`` (TRIAGE.md's REFUSE bucket — a bulk data-destruction instrument,
``docs/plans/dossiers/TRIAGE.md``) rather than the next Wave-2/3 doctype in line: every prior
repoint target eventually landed and needed repointing again; a REFUSE doctype never will, by
design (data-destruction, ledger-repost/maintenance machinery, or internal process logs this
broker will never build a tool for) — the most durable exemplar available, closing the repoint
churn for good.

**Breadth (POS Invoice) — Wave 2, the thirteenth doctype, and the one that forces a REAL CODE FIX,
not just documentation — the campaign's Delivery-Note-shaped precedent, but sharper.** Five more
doctype-named tools (``*_pos_invoice``) wrap the same generic handlers, pinned to POS Invoice
(``party_field="customer"``, ``submit_via``/``date_field`` byte-identical to Sales Invoice's own
entry — confirmed by reading ``pos_invoice.py`` version-16 in full: no ``submit()``/``cancel()``
override). Full semantics pinned in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block
(and the module docstring there, which carries the full source-cited finding); dossier at
``docs/plans/dossiers/pos_invoice.md`` — **the dossier itself is WRONG on its central claim** (it
says GL/SL posting is "inherited from SalesInvoice.on_submit"); ``erpnext.py``'s own docstring has
the correction, proven by a full-file grep of ``pos_invoice.py`` showing zero references to
``make_gl_entries``/``update_stock_ledger`` anywhere: ``POSInvoice.on_submit()`` fully overrides
``SalesInvoice.on_submit()`` without calling it, so a real POS Invoice submit posts NEITHER a GL
Entry NOR a Stock Ledger Entry of its own — that accounting is deferred entirely to a later,
separate, genuinely-submitted Sales Invoice a POS Closing Entry builds at consolidation.

**The real code fix, in three parts, all landing alongside this doctype:**

1. :func:`_pos_invoice_ledger_deferral_flag` (new) — because ERPNext's own ``ledger_preview`` RPC
   calls ``doc.make_gl_entries()`` DIRECTLY (bypassing ``on_submit`` entirely), and POS Invoice
   never overrides that method (only ``on_submit``), the preview genuinely computes and returns
   non-empty GL rows via the INHERITED ``SalesInvoice.make_gl_entries`` — rows that will never
   actually post for this voucher. Without this flag, ``plan_submit``'s own "no debit without a
   credit" promise (projected_gl reflects what a submit will do) silently breaks for this one
   doctype. The flag fires unconditionally for ``doctype == POS_INVOICE`` (not gated on any doc
   field — the divergence is a property of the DOCTYPE's own on_submit override, true for every
   POS Invoice regardless of its contents), submit-direction only: ``plan_cancel``'s own
   ``projected_reversal`` is a REAL bench read (``get_gl_entries``) that already comes back empty
   honestly, covered by the pre-existing generic "no live GL rows found" flag with no change.
2. :func:`_update_stock_risk_flags` gains an optional ``doctype`` parameter (the same shape
   :func:`_return_risk_flags` already carries) and a POS-Invoice-gated branch: the doctype-agnostic
   wording ("the stock ledger is written alongside the GL on submit") would be a FALSE claim for
   POS Invoice specifically — ``update_stock_ledger()`` is never called anywhere in
   ``pos_invoice.py`` either, the same on_submit-override finding as the GL side. Both submit and
   cancel directions get corrected wording naming the deferral explicitly.
3. :func:`_return_risk_flags` gains a THIRD ``"update_outstanding_for_self" not in doc`` sub-branch,
   keyed explicitly on ``doctype == POS_INVOICE`` (not field-presence alone, unlike the DN/PR
   branch it sits beside — this is a VERIFIED, doctype-specific behavioral fact, not a shape
   inferable from schema alone, so it is named explicitly rather than guessed at from
   ``debit_to``'s presence). POS Invoice carries no ``update_outstanding_for_self`` field (confirmed
   absent, the same finding shape as Delivery Note/Purchase Receipt) — but unlike those two
   doctypes, POS Invoice DOES carry ``debit_to`` (a real receivable field); the DN/PR wording
   ("STOCK reversal only... Inventory/COGS GL only") would therefore be doubly wrong for POS
   Invoice: not just because it undersells a receivable balance that exists, but because — per the
   central finding above — POS Invoice posts NO GL of ANY kind on its own submit, stock or
   receivable, return or not. The new branch says so plainly rather than reusing either existing
   sentence.

**No code change needed for two already-generic mechanisms, documented as USAGE findings, not new
mechanism:** :func:`_pos_risk_flags` (the ``is_pos`` disclosure, built for Sales Invoice where the
field is optional) fires unconditionally for every real POS Invoice — ``validate()``
(``pos_invoice.py:203-206``) throws if ``is_pos`` is falsy, so this doctype is the first where the
flag is never a no-op. :func:`_return_risk_flags`'s top-line RETURN/FREE-STANDING/mixed-sign flags
apply unchanged (POS Invoice carries real ``is_return``/``return_against`` fields, confirmed
present).

**Cascade needed NO changes**, the same finding as every prior breadth increment — ``cascade.py``
carries zero doctype-specific string literals. **A genuine, VERIFIED CORRECTION to the pinned
dossier's own cancel-block disclosure claim, more generous than the dossier states:** the dossier
claims neither the consolidated Sales Invoice's downstream reach nor a submitted POS Closing
Entry/POS Invoice Merge Log is visible to this broker's ``get_submitted_linked_docs`` blast-radius
walk. Traced directly against frappe's own ``linked_with.py`` source (version-16, not assumed):
BOTH are discoverable. ``Sales Invoice Item.pos_invoice`` (a real Link field, set on every merged
Sales Invoice Item row by ``merge_pos_invoice_into()`` for both the return-consolidation and the
ordinary batch-merge paths — not return-only, a correction beyond what the dossier even
attempted) surfaces a submitted consolidated Sales Invoice the same way ``Sales Invoice
Item.delivery_note`` already surfaces one for Delivery Note. ``POS Invoice Reference.pos_invoice``
(a real, required Link field on a CHILD table embedded by BOTH ``POS Closing Entry`` and ``POS
Invoice Merge Log``, both ``is_submittable: 1`` and neither exempted from frappe's own
``auto_cancel_exempted_doctypes``) surfaces a submitted POS Closing Entry/Merge Log too — frappe's
own child-table-to-parent resolution (``get_referencing_documents``'s ``parenttype``-groupby logic,
``frappe/desk/form/linked_with.py:356-363``) is the documented mechanism for exactly this shape,
not an edge case. This broker's PRE-EXISTING generic refusal (any non-empty
``get_submitted_linked_docs`` result denies the plan) already covers both halves with zero new
code — see ``TestPOSInvoiceConsolidationCancelDisclosure``, ``test_tools.py``. **The consolidation
boundary itself is unchanged and honored:** this landing adds no tools for POS Invoice Merge Log
(a TRIAGE.md REFUSE doctype) and does not drive POS Closing Entry's own batch-consolidation
machinery — a governed POS Invoice submit/cancel, disclosed blast radius, nothing more.

**Breadth (Dunning) — Wave 2, the fourteenth doctype, and the FIRST that forces this broker to
SKIP its own native preview RPC rather than merely annotate its result.** Five more doctype-named
tools (``*_dunning``) wrap the same generic handlers, pinned to Dunning (``party_field="customer"``,
``submit_via``/``date_field`` identical shape to Sales Invoice's own entry — confirmed by reading
all 276 lines of ``dunning.py``: no ``submit()``/``cancel()``/``on_submit()`` override). Full
semantics pinned in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block (and the module
docstring there, full source-cited finding); dossier at ``docs/plans/dossiers/dunning.md`` — the
dossier is right about ``customer``'s ``reqd: 1`` and about ``status``/``grand_total`` both being
present with no GL posting on submit, but WRONG about the MECHANISM behind the empty
``projected_gl`` it predicts: it assumes Dunning behaves like Sales Order/Purchase Order/Material
Request (a real, callable, conditionally-no-op ``make_gl_entries`` inherited from
``StockController``). Dunning inherits from ``AccountsController`` directly (never
``StockController``) and defines no ``make_gl_entries`` of its own — the method does not exist
ANYWHERE in its MRO. See ``erpnext.py``'s own "Breadth (Dunning)" module-docstring section for the
full source-cited finding (the MRO grep, the bare unguarded call site in
``get_accounting_ledger_preview``, and why this produces a live-bench ``AttributeError`` rather
than an empty result if the RPC is ever actually called).

**The real code fix, in two parts:**

1. :func:`_dunning_ledger_preview_unavailable_flag` (new) — fires unconditionally for
   ``doctype == DUNNING`` (a property of the doctype itself, true regardless of the document's own
   contents), submit-direction only, naming plainly why ``projected_gl`` is empty: not "nothing to
   post" (SO/PO/MR/SQ/Q's shape) but "the preview mechanism itself does not apply to this
   doctype — calling it would raise a server-side AttributeError, so this broker does not call it".
2. ``_tool_plan_submit`` gains a doctype-gated branch, the same shape as its existing Journal Entry
   local-checks branch immediately above it: for ``doctype == DUNNING``, the
   ``client.ledger_preview(...)`` network call is skipped ENTIRELY (never sent — this is a
   correctness fix, not just a disclosure one; letting the call through would 500 on a live bench
   and refuse every single Dunning ``plan_submit``, which would make Dunning ungoverned in
   practice despite being "added"), and ``preview`` is built locally as ``{"gl_data": []}``. Every
   other doctype's ``plan_submit`` path is byte-for-byte unchanged.

**No code change needed for two already-generic mechanisms, documented as USAGE findings, not new
mechanism:** ``plan_cancel``'s ``projected_reversal`` (``get_gl_entries``, a real bench read filtered
on ``voucher_type``/``voucher_no``) naturally, safely returns empty for Dunning — no AttributeError
risk, since it never calls ``make_gl_entries``, only queries the ``GL Entry`` table directly; the
pre-existing generic "no live GL rows found for this voucher — nothing visible to unwind" flag
already covers it honestly, the same shape POS Invoice's ``plan_cancel`` already established.
``_cascade_node_meta`` (cascade's own per-node GL projection) uses the same safe ``get_gl_entries``
read, never ``ledger_preview`` — a Dunning node inside a cascade graph needs no special-casing
either.

**Cascade needed NO changes**, the same finding as every prior breadth increment — ``cascade.py``
carries zero doctype-specific string literals. **A genuine, VERIFIED CORRECTION to the pinned
dossier's own §5 "disclosure gap" claim.** The dossier states Payment Entry's reference to a
Dunning (via ``Payment Entry Reference.reference_name``) is invisible to this broker's
``get_submitted_linked_docs`` blast-radius walk because that field is "a Data/String field, not a
Link field." Dumping the raw field dict directly shows this is WRONG: ``reference_name`` is
fieldtype ``Dynamic Link`` (``options: "reference_doctype"``), and frappe's own generic
blast-radius walker (``get_references_across_doctypes``, ``linked_with.py``) explicitly resolves
Dynamic Link fields with the identical child-table-to-parent promotion static Links get — see
``erpnext.py``'s own module docstring for the full source-cited finding (the exact function names
and line ranges in frappe's ``linked_with.py``). This broker's PRE-EXISTING generic refusal (any
non-empty ``get_submitted_linked_docs`` result denies the plan) already covers a submitted Payment
Entry referencing a Dunning, with zero new code — there is no disclosure gap here.

**Breadth (Stock Reconciliation) — Wave 2, the fifteenth doctype, and the doctype that forces TWO
real code changes at once, not one: a new transport (the second ``SUBMIT_VIA_CLIENT_RPC`` doctype)
AND a new ledger-preview disclosure (a FIFTH shape).** Five more doctype-named tools
(``*_stock_reconciliation``) wrap the same generic handlers, pinned to Stock Reconciliation
(``party_field=None`` — no header-level party field of any kind, confirmed absent from the
doctype's complete 17-field schema). Full semantics pinned in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block (and the module docstring there,
full source-cited finding); dossier at ``docs/plans/dossiers/stock_reconciliation.md`` — correct on
every claim it makes (party_field, status/grand_total absence, posting_date, the reserved-stock
disclosure gap, cascade), but SILENT on submit transport, which turns out to be the doctype's
biggest mechanical surprise.

**Finding one: ``submit_via=SUBMIT_VIA_CLIENT_RPC``, the second doctype ever to need it.**
``StockReconciliation`` overrides ``submit()``/``cancel()`` themselves (``stock_reconciliation.py:
1107-1127``, not merely the ``on_submit``/``on_cancel`` hooks every prior non-JE landing's own
verification stopped at), and neither override carries ``@frappe.whitelist()`` — the identical
Journal Entry mechanism (``journal_entry.py:186,195``): frappe's ``run_method`` REST dispatch calls
``doc.is_whitelisted(method)`` before invoking it, and an undecorated subclass override is not in
frappe's global whitelisted-function set, so the ``run_method=submit``/``=cancel`` vector 403s for
this doctype exactly as it does for Journal Entry. **Zero new transport code was needed** —
:meth:`pacioli.erpnext.ErpnextClient.submit_document`/``.cancel_document`` already branch generically
on ``SUPPORTED_DOCTYPES[doctype]["submit_via"]``, and ``_governed_write``'s ``execute()`` closure
(below) already threads the SAME already-fetched ``doc`` into ``client.submit_document(doctype,
name, doc=doc)`` unconditionally for EVERY doctype (never gated on ``doctype == JOURNAL_ENTRY`` —
built generically the first time this codebase needed it); ``pacioli_guard``'s
``body_scoped_target`` is likewise already doctype-generic. This landing is therefore the first
LIVE PROOF that the client_rpc transport generalizes beyond the one doctype it was built for.

**Finding two, the real code change:** :func:`_stock_reconciliation_ledger_preview_incomplete_flag`
(new) — Stock Reconciliation IS a ``StockController`` subclass (``make_gl_entries`` is inherited
and callable, unlike Dunning's total absence), so ERPNext's own preview RPC does not raise. But it
is confirmed ABSENT from ``get_accounting_ledger_preview``'s own SLE-seeding whitelist
(``stock_controller.py:2109-2110``, names only Purchase Receipt/Delivery Note/Stock Entry) and
carries no ``update_stock`` field, so the preview never seeds the voucher's own Stock Ledger Entry
rows before calling ``make_gl_entries()`` — which then queries the REAL ``Stock Ledger Entry``
table, finds nothing (no rows exist yet), and returns an empty ``projected_gl`` with no exception.
**This is DIFFERENT from SO/PO/MR/SQ/Q's own honestly-empty preview**: those doctypes' real submit
ALSO posts no GL (the emptiness matches reality); Stock Reconciliation's real submit ALWAYS writes
Stock Ledger Entry rows (unconditional, and REFUSES the submit outright if it would write none at
all) and, whenever perpetual inventory is enabled, ALSO writes real GL Entry rows from those same
rows — none of which the empty preview discloses. A FALSE NEGATIVE, the mirror image of POS
Invoice's own FALSE POSITIVE (a non-empty preview for a posting that never happens). The flag fires
unconditionally for ``doctype == STOCK_RECONCILIATION`` (a property of the doctype's own
preview-vs-submit mismatch), submit-direction only — the native preview call is NOT skipped (unlike
Dunning: it is callable and harmless here, never raises, so there is no correctness reason to avoid
sending it, only a disclosure reason to annotate its result). ``plan_cancel`` needs no equivalent
fix: its own ``projected_reversal`` already reads real, actually-posted ``GL Entry`` rows.

**No code change needed for two already-generic mechanisms, documented as USAGE findings:**
:func:`_return_risk_flags` (Stock Reconciliation carries no ``is_return`` field at all — confirmed
absent from the 17-field schema, a stock-correction doctype has no return concept), and
:func:`_update_stock_risk_flags`/:func:`_pos_risk_flags` (no ``update_stock``/``is_pos`` field
either) are all structural no-ops for this doctype by construction — no doctype-gated branch is
needed in any of the three, unlike POS Invoice's own landing.

**Cascade needed NO changes**, the same finding as every prior breadth increment — ``cascade.py``
carries zero doctype-specific string literals. Grepping the full v16 checkout for a real ``Link``
field naming Stock Reconciliation returns exactly one hit: its own self-referencing
``amended_from``. Zero external dependents; Stock Reconciliation can never be a dependent in any
cancel cascade.

**Breadth (Landed Cost Voucher) — Wave 2, the sixteenth doctype and LAST, and the sharpest
disclosure case in the campaign: the FIRST supported doctype whose own submit/cancel rewrites
OTHER documents' posted ledger rows rather than only its own.** Five more doctype-named tools
(``*_landed_cost_voucher``) wrap the same generic handlers, pinned to Landed Cost Voucher
(``party_field=None`` — no header-level party field of any kind, confirmed absent from the
doctype's complete 15-field schema; ``submit_via``/``date_field`` byte-for-byte identical to Stock
Entry's own entry — confirmed by reading all 522 lines of ``landed_cost_voucher.py``: no
``submit()``/``cancel()`` override). Full semantics pinned in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block (and the module docstring there,
full source-cited finding); dossier at ``docs/plans/dossiers/landed_cost_voucher.md`` — correct on
party_field/status/grand_total/posting_date, WRONG on the Dynamic Link's discoverability (§6), the
same class of correction Dunning's own landing (above) made for a different doctype.

**Finding one: the native ``ledger_preview`` RPC is uncallable, the Dunning shape — but with a
sharper reason even a working preview would still be the wrong document.**
``LandedCostVoucher(Document)`` (``landed_cost_voucher.py:22``) descends directly from frappe's
base ``Document`` — never ``AccountsController`` (Dunning's own ancestor), never
``StockController`` — and defines no ``make_gl_entries`` of its own; a full-file grep confirms it
is absent everywhere in the file. ERPNext's own ``get_accounting_ledger_preview`` would call
``doc.make_gl_entries()`` as a bare method call and raise ``AttributeError`` on a live bench, so
``_tool_plan_submit`` skips the ``client.ledger_preview()`` network call entirely for this
doctype too — the SAME skip branch Dunning already built, generalized to a second doctype with
zero new transport code. **The reason this is a SHARPER shape than Dunning's own, not merely a
repeat:** Dunning's real submit genuinely posts nothing under ANY voucher_type — an empty
``projected_gl`` matches ground truth, full stop. A Landed Cost Voucher's real submit ALSO never
posts under its OWN voucher_type, but it very much posts real rows — under the NAMED RECEIPTS'
voucher_type instead (see finding two). An empty ``projected_gl`` here is accurate about the LCV
and silent about the posting it is about to cause elsewhere; :func:`_landed_cost_voucher_ledger_
preview_unavailable_flag` (new) says so plainly, submit-direction only.

**Finding two: ``on_submit``/``on_cancel`` both call the same ``update_landed_cost()`` — a real
revaluation of the Purchase Receipt/Purchase Invoice/Stock Entry/Subcontracting Receipt documents
named in the LCV's own ``purchase_receipts`` child table.** Confirmed from source
(``landed_cost_voucher.py:289-350``): for each named receipt, the item valuation is recalculated
and its existing Stock Ledger Entry + GL Entry rows are reversed and reposted at the new rate — on
SUBMIT this raises the receipt's cost (the LCV's charges land on it); on CANCEL the identical
method call reverses that raise. Neither ``plan_submit``'s ``projected_gl`` nor ``plan_cancel``'s
``projected_reversal`` (``get_gl_entries("Landed Cost Voucher", name)``, honestly empty — no GL
row is EVER posted under this voucher_type) can show any of this, because both reads are scoped to
the LCV's own voucher, and the real posting happens under a DIFFERENT voucher entirely.
:func:`_landed_cost_voucher_cancel_revaluation_flag` (new) names this on the cancel side — unlike
Dunning's own landing (which found its pre-existing "no live GL rows found — nothing visible to
unwind" flag was already honest and needed no addition), this doctype's otherwise-identical empty
read is TRUE but MISLEADING (a reader could reasonably infer "nothing to unwind" when in fact a
real repost is about to run on other documents), so a new flag is added rather than left silent —
a deliberate divergence from Dunning's own precedent, not an oversight.

**Finding three, the sharpest of the landing: the Dynamic Link edge to the named receipts IS
generically discoverable, contra both the dossier and Purchase Receipt's own earlier landing
(b2d06a9).** Traced directly against frappe's ``linked_with.py`` (version-16) rather than assumed:
``get_references_across_doctypes_by_dynamic_link_field`` runs a LIVE query for the distinct values
actually in use in the sibling type-selector column, not a static schema scan — the exact
mechanism Dunning's own landing already proved for Payment Entry Reference's Dynamic Link
(``reference_name``/``reference_doctype``). ``Landed Cost Purchase Receipt.receipt_document``
(a Dynamic Link, ``options: "receipt_document_type"``) rides the identical mechanism: a submitted
Landed Cost Voucher referencing a Purchase Receipt (or Purchase Invoice/Stock Entry/Subcontracting
Receipt) genuinely surfaces in ``get_submitted_linked_docs(<receipt doctype>, <receipt name>)``'s
response, and this broker's PRE-EXISTING generic refusal (any non-empty result denies
``plan_cancel``) already blocks cancelling a receipt with a submitted LCV against it — zero new
code, and MORE protective than raw ERPNext's own ``check_next_docstatus`` (which, per Purchase
Receipt's own landing, checks only for a submitted Purchase Invoice, never an LCV). The dossier's
"structurally invisible" framing and Purchase Receipt's own "orphan hazard, invisible to this
broker's walk" comment are both corrected here; what survives as true is narrower and still real:
ERPNext's OWN native cancel check does not itself look for a linked LCV, so an operator working
DIRECTLY against ERPNext (not through this broker) can still orphan one — this broker's disclosure
layer is the thing standing between that native gap and the user. ``Landed Cost Vendor Invoice.
vendor_invoice`` (a REAL, plain ``Link`` to Purchase Invoice, missed by the dossier entirely) is a
second, independent edge riding the ordinary Link-field walk, no Dynamic Link reasoning needed.

**The reverse direction needs its own honest framing, not a fix.** Cancelling the LCV itself finds
NO inbound dependents (nothing links to Landed Cost Voucher except its own ``amended_from``,
skipped by the tree walk by construction) — ``get_submitted_linked_docs("Landed Cost Voucher",
name)`` is genuinely, correctly empty. This is the right answer to the WRONG question for this
doctype: the blast-radius walk asks "what depends on this LCV existing", never "what does this
LCV's own on_cancel hook go rewrite" — a structurally different hazard shape the Link-graph
mechanism was never built to see, which is exactly why finding two's new cancel flag exists
independent of this check.

**No code change needed for three already-generic mechanisms, documented as USAGE findings:**
:func:`_return_risk_flags`/:func:`_update_stock_risk_flags`/:func:`_pos_risk_flags` (Landed Cost
Voucher carries no ``is_return``/``update_stock``/``is_pos`` field at all — confirmed absent from
the 15-field schema) are all structural no-ops for this doctype by construction, the same shape
Stock Reconciliation's own landing already established.

**Cascade needed NO changes**, the same finding as every prior breadth increment — ``cascade.py``
carries zero doctype-specific string literals; the corrected Dynamic Link/Link discovery above is
entirely frappe's own generic mechanism, already exercised through the existing blast-radius
refusal.

**Breadth (Request for Quotation) — Wave 3, the seventeenth doctype and first row.** Five more
doctype-named tools (``*_request_for_quotation``) wrap the same generic handlers, pinned to
Request for Quotation. Full semantics pinned in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own
comment block (and the module docstring there, full source-cited finding); dossier at
``docs/plans/dossiers/request_for_quotation.md``.

**The ledger-preview finding needed direct verification, not a repeat of the dossier's own hedge:
RFQ is the SO/PO/MR/SQ/Q "honest-empty" category, not the Dunning/LCV "uncallable" one.**
``RequestforQuotation(BuyingController)`` shares the identical controller chain
(``BuyingController`` -> ``SubcontractingController`` -> ``StockController``) Purchase Order/
Material Request/Supplier Quotation already ride — a REAL, callable ``make_gl_entries`` is
inherited from ``StockController``, unlike Dunning (``AccountsController`` only) or Landed Cost
Voucher (bare ``Document``). No new risk-flag function is needed and RFQ is NOT added to
``tools.py``'s existing ``(DUNNING, LANDED_COST_VOUCHER)`` ledger_preview skip tuple — the native
preview call is safely callable and honestly returns empty (RFQ never writes a Stock Ledger Entry,
so ``make_gl_entries``'s own query finds nothing to post), the same mechanism SO/PO/MR/SQ/Q's own
landings already established and confirmed rather than assumed here.

**``on_submit`` dispatches real email to every supplier in the RFQ's child table — documented as a
plan-tier disclosure note, not built as a new risk-flag function.** This is the first supported
doctype whose submit causes an external-communication side effect rather than a ledger one. No new
code models it: like Material Request's own on-hold/closed cancel-refusal gap (documented in prose,
no new gate), the email dispatch is ERPNext's own designed behavior for this doctype (an RFQ's
entire purpose is reaching suppliers) — not previewable (depends on live Contact/User/Email Account
state this broker never reads) and not something a refusal should block (blocking it would defeat
the doctype). ``plan_submit``'s disclosure names it informationally.

**Cascade needed NO changes**, the same finding as every prior breadth increment — ``cascade.py``
carries zero doctype-specific string literals. The one real external Link naming RFQ
(``supplier_quotation_item.request_for_quotation``) is walked generically by ERPNext's own
``get_submitted_linked_docs``; a submitted Supplier Quotation built from this RFQ is discovered and
ordered ahead of it on cancel with zero new code.

**Breadth (Blanket Order) — Wave 3, the eighteenth doctype and second row.** Five more
doctype-named tools (``*_blanket_order``) wrap the same generic handlers, pinned to Blanket Order
(``party_field=None``, ``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field="from_date"``). Full
semantics pinned in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block (and the module
docstring there, full source-cited finding); dossier at ``docs/plans/dossiers/blanket_order.md`` —
the ninth landing in a row to find at least one dossier error rather than trust it blind (a real
cascade edge the dossier missed, not a semantics defect).

**THE LEDGER-PREVIEW FINDING: Blanket Order is the Dunning/Landed Cost Voucher "uncallable"
category, NOT the SO/PO/MR/SQ/Q/RFQ "honest-empty" one.** ``class BlanketOrder(Document)`` — never
``AccountsController``, never ``StockController`` — defines no ``make_gl_entries`` anywhere in its
MRO (the same full-tree grep Dunning's and Landed Cost Voucher's own landings ran). ERPNext's own
preview would call ``doc.make_gl_entries()`` as a bare method and raise ``AttributeError`` on a live
bench if invoked, refusing every ``plan_submit`` outright. **The real code fix, mirroring Dunning's
own (the simpler shape, not LCV's dual-flag one — Blanket Order has no revaluation-elsewhere side
effect, see below):**

1. :func:`_blanket_order_ledger_preview_unavailable_flag` (new) — fires unconditionally for
   ``doctype == BLANKET_ORDER``, submit-direction only, naming plainly why ``projected_gl`` is
   empty by construction rather than by an honest no-op.
2. ``_tool_plan_submit``'s existing ``(DUNNING, LANDED_COST_VOUCHER)`` skip tuple grows to
   ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER)`` — the native ``client.ledger_preview()`` call
   is skipped entirely for this doctype too.

``plan_cancel`` needs no equivalent new flag (the Dunning shape, not the LCV one): ``get_gl_entries``
is a real, safe bench read that naturally, honestly returns empty for Blanket Order in both
directions — unlike Landed Cost Voucher, a Blanket Order's own cancel never re-triggers a
revaluation of some OTHER document's ledger. Its one real side effect —
``StockController.update_blanket_order()`` recomputing this Blanket Order's own ``ordered_qty``
counters — is triggered by a REFERENCING Sales/Purchase Order's own submit/cancel (upstream, into
this document), never by this Blanket Order's own lifecycle, and is a Bin-style quantity counter,
never a ledger row — documented in ``erpnext.py``'s own module docstring, not built into new
machinery (a dossier correction: the effect fires on the referencing SO/PO's submit AND cancel,
not "cancel only" as the dossier's own §6 heading claims).

**Cascade needed NO changes**, the same finding as every prior breadth increment — ``cascade.py``
carries zero doctype-specific string literals. **A genuine, VERIFIED CORRECTION to the pinned
dossier's own §7 claim.** The dossier names two real Link fields pointing at Blanket Order
(``sales_order_item.blanket_order``, ``purchase_order_item.blanket_order``) and dismisses a third
(``quotation_item.blanket_order``) as "not a Link reference — child-table membership only."
Dumping the raw field dict directly shows this is WRONG: ``quotation_item.json``'s own
``blanket_order`` field is ``fieldtype: "Link"``, byte-for-byte the same shape as the SO/PO Item
fields the dossier got right. This does not change the cascade conclusion (``get_submitted_linked_
docs`` already walks all three generically, zero new code either way) but widens the real
disclosure: a submitted Quotation referencing this Blanket Order is also discoverable by the
blast-radius check, not just a Sales Order or Purchase Order — the same "wider than claimed, never
a gap" shape Dunning's own Payment Entry correction and RFQ's own supplier_quotation_item
correction already established.

**Breadth (Job Card) — Wave 3, the nineteenth doctype and third row.** Five more doctype-named
tools (``*_job_card``) wrap the same generic handlers, pinned to Job Card (``party_field=None``,
``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field="posting_date"``). Full semantics pinned in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block (and the module docstring there,
full source-cited finding); dossier at ``docs/plans/dossiers/job_card.md`` — the tenth landing in a
row to find at least one dossier imprecision rather than trust it blind (Material Request's own
cascade edge undersold, not a semantics defect).

**THE LEDGER-PREVIEW FINDING: Job Card is the Dunning/Landed Cost Voucher/Blanket Order
"uncallable" category, NOT the SO/PO/MR/SQ/Q/RFQ "honest-empty" one.** ``class JobCard(Document)``
— never ``AccountsController``, never ``StockController`` (the import block pulls only two
EXCEPTION classes from ``stock_controller``, never the class itself) — defines no
``make_gl_entries`` anywhere in its MRO (the same full-tree grep every prior "uncallable" landing
ran). ERPNext's own preview would call ``doc.make_gl_entries()`` as a bare method and raise
``AttributeError`` on a live bench if invoked, refusing every ``plan_submit`` outright. **The real
code fix, mirroring Blanket Order's own (the simpler shape, not LCV's dual-flag one — Job Card has
no revaluation-elsewhere side effect, see below):**

1. :func:`_job_card_ledger_preview_unavailable_flag` (new) — fires unconditionally for
   ``doctype == JOB_CARD``, submit-direction only, naming plainly why ``projected_gl`` is empty by
   construction rather than by an honest no-op.
2. ``_tool_plan_submit``'s existing ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER)`` skip tuple
   grows to ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD)`` — the native
   ``client.ledger_preview()`` call is skipped entirely for this doctype too.

``plan_cancel`` needs no equivalent new flag (the Dunning/Blanket-Order shape, not the LCV one):
``get_gl_entries`` is a real, safe bench read that naturally, honestly returns empty for Job Card.
Its own real side effects on cancel — ``update_work_order()``/``set_transferred_qty()`` — reset the
linked Work Order's operation quantities and this Job Card's own ``transferred_qty``/``status``,
never a ledger row, never a revaluation of some OTHER document's GL entries — documented in
``erpnext.py``'s own module docstring, not built into new machinery.

**Cascade needed NO changes**, the same finding as every prior breadth increment — ``cascade.py``
carries zero doctype-specific string literals. **A genuine dossier CLARIFICATION** (not an outright
error, unlike Dunning's/Blanket Order's own corrections): the dossier's own §7 names the same five
external Link-carrying doctypes this landing confirms (Stock Entry, Material Request, Subcontracting
Receipt Item, Subcontracting Order Item, Purchase Order Item), but describes Material Request's own
edge only as "may be created from Job Card" rather than plainly "carries a ``job_card`` Link field"
like the other four — dumping ``material_request.json``'s raw field dict shows it is the identical
shape (``fieldtype: "Link"``, ``read_only: 1``). This does not change the cascade conclusion
(``get_submitted_linked_docs`` already walks all five generically, zero new code either way) but
states plainly that Material Request stands on equal cascade footing with the other four.

**THE SIDE-SURFACE CAVEAT — the largest of this campaign so far.** Job Card exposes 14 separate
``@frappe.whitelist()`` callables outside this broker's submit/cancel/amend 5-verb surface (6
instance methods including ``pause_job``/``resume_job``/``start_timer``/``complete_job_card``/
``make_stock_entry_for_semi_fg_item``, 8 module-level functions including ``make_stock_entry``/
``make_subcontracting_po``/``make_material_request``/``make_corrective_job_card``). The
``SUPPORTED_DOCTYPES`` entry above governs ONLY submit/cancel (plus the generic amend/get/list
wrappers) — it grants NOTHING toward any of these 14 methods. Several mutate state outside
``docstatus`` entirely (``pause_job``/``resume_job``/``start_timer`` flip ``is_paused``/append time
logs without touching ``docstatus`` — Work Order's own ``stop_unstop()`` shape from
``docs/plans/dossiers/work_order.md`` §8, generalized to a whole family rather than one method) or
create new documents of a different doctype entirely (a Stock Entry, Purchase Order, Material
Request, or corrective Job Card). No tool is built for any of them this landing, matching Pick
List's own reservation-RPC caveat in ``docs/plans/dossiers/TRIAGE.md``.

**Breadth (BOM) — Wave 3's fourth row, the twentieth doctype, THE FIRST DATELESS ONE.** Five more
doctype-named tools (``*_bom``/``list_boms``) wrap the same generic handlers, pinned to BOM
(``party_field=None``, ``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field=None``). Full semantics
in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s comment block + the module docstring there
(dossier at ``docs/plans/dossiers/bom.md`` — three corrections this landing, the eleventh in a
row to find at least one: the 94-vs-"157" field count, the phantom Supplier Quotation cascade
edge, and ``update_cost``'s submitted-BOM mutation undersold as "client RPC support").

**THE REAL CODE CHANGE — the declared-dateless shape, this campaign's first since Sales Order
forced ``date_field`` itself into existence.** BOM carries NO date field at all (zero
Date/Datetime fields across ``bom.json``'s 94), yet the whole closed-books chain was keyed on
one: ``check_red_line`` refuses an empty date, and ``get_period_locks`` refuses a non-ISO date —
riding any default would have hard-denied every BOM write forever, at plan (the E6 disclosure's
own lock read raises) AND at execute. The fix threads a single explicit state end to end,
branching ONLY on the declared pin, never on an empty read:

1. ``SUPPORTED_DOCTYPES``' ``"date_field": None`` — the source-verified pin (a third state,
   distinct from a named field and from the absent-key ``"posting_date"`` default).
2. :func:`_posting_date_of` (new; the four former inline ``doc.get(_date_field_for(...))`` sites
   — plan_submit, plan_cancel, ``_governed_write``, ``_cascade_node_meta`` — now share it) maps
   the declared ``None`` to :data:`pacioli.plan.NO_DATE_FIELD`, a sentinel that is deliberately
   NOT a valid ISO date (a leak into any other date slot refuses loudly, never passes).
3. :func:`_locks_for` (new; wraps ``get_period_locks`` at ``_governed_write`` + the cascade
   effects) returns ``{}`` for the sentinel — there is no date to build the Accounting-Period
   range query on — and passes everything else through unchanged, empty string included (which
   still refuses there: an unreadable date on a DATED doctype stays denied).
4. ``check_red_line`` (plan.py) passes the sentinel by its own first branch — EQUAL to ERPNext,
   proven three ways from v16 source (BOM absent from ``hooks.py``'s ``period_closing_doctypes``;
   ``check_freezing_date`` fires only in GL-posting paths and BOM posts no GL; no date exists to
   range-check). The plan-time E6 disclosure names "not applicable" plainly instead of reading
   locks; the three future-date flag sites (plan_submit/plan_cancel/plan_cascade_cancel) branch
   explicitly because the sentinel sorts AFTER every ISO date — unbranched, every dateless doc
   would falsely flag "posting_date is in the future" (wrong-but-loud by sentinel design, fixed
   by branching, tested both ways).

The reconcile paths (``plan_reconcile``/``reconcile``) are deliberately NOT routed through the
new helpers: their nodes are invoices/payments by construction (never a dateless doctype), and
their date reads come off named per-node fields, not ``_date_field_for`` — nothing there can see
the sentinel.

**Ledger preview: the Dunning/Blanket-Order/Job-Card "uncallable" category again** —
``BOM(WebsiteGenerator)``, no ``make_gl_entries`` in the MRO, so the skip tuple grows to
``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM)`` with
:func:`_bom_ledger_preview_unavailable_flag` naming it honestly; ``plan_cancel`` needs no new
flag (``get_gl_entries`` honestly returns empty — no GL row ever exists under a BOM's name).

**Cancel blast radius — the widest fan-in landed so far:** 29 ``Link → BOM`` fields across 25
doctypes (Work Order, Stock Entry + Detail, Sales Order Item, Purchase Invoice/Order/Receipt
Item, the whole planning/subcontracting spine, and BOM Item's own self-referencing sub-assembly
edge), all walked generically by ``get_submitted_linked_docs`` — zero cascade code changes, the
same finding every landing has made. ERPNext's own cancel gate (``validate_bom_links``: refuses
while another submitted+active BOM uses this one) rides the standing answered-ErpnextError path.

**Side surface NOT granted:** 10 whitelist callables, sharpest of them ``update_cost`` — which
rewrites a SUBMITTED BOM's stored cost fields in place (``ignore_validate_update_after_submit``
+ ``db_update()``) and then recursively does the same to every submitted parent BOM — and
``make_variant_bom`` (creates a new BOM). The 5-verb surface grants nothing toward any of them;
no tool is built for any of them this landing.

**Breadth (Work Order) — Wave 3's fifth row, the twenty-first doctype, THE FIRST DATETIME-DATED
ONE.** Five more doctype-named tools (``*_work_order``) wrap the same generic handlers, pinned to
Work Order (``party_field=None``, ``submit_via=SUBMIT_VIA_RUN_METHOD``,
``date_field="planned_start_date"``). Full semantics in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s comment block + the module docstring there (dossier
at ``docs/plans/dossiers/work_order.md`` — FOUR corrections this landing, the twelfth in a row:
its §9 "cancel does NOT refuse on linked submitted documents" is an outright inversion
(``validate_cancel`` throws on any submitted Stock Entry); its §3 missed that
``planned_start_date`` is a DATETIME; its §7 ``parent_work_order`` self-link is a phantom while
the real ``Serial No.work_order`` edge went unmentioned; its §2 misread ``produced_qty`` as
list-view-flagged).

**THE REAL CODE CHANGE — the datetime→date projection, one reader, one rule.**
``planned_start_date`` reads back as ``"YYYY-MM-DD HH:MM:SS[.ffffff]"``, which fails the strict
ISO-date shape ``check_red_line``/``get_period_locks`` enforce — the dossier's own "ride the
transaction_date pattern unchanged" would have hard-denied every Work Order write at plan AND
execute. :func:`_posting_date_of` (already the single date reader since BOM's landing) now
projects a well-formed datetime to its date part — truncating ONLY when a valid ISO date is
immediately followed by a ``" "``/``"T"`` separator; anything malformed keeps its raw shape and
stays refused downstream. Inert for every Date-typed field and for the dateless sentinel; the
closed-books chain, the future-date flags, and the Plan's channel all see a plain ISO date.
``allow_on_submit: 1`` on the field means the date can move without a docstatus change — it
still bumps ``modified``, so ``check_fresh`` catches plan/execute drift, nothing new needed.

**Ledger preview: the "uncallable" category again** — ``WorkOrder(Document)``, no
``make_gl_entries`` in 3114 lines (the dossier's §5 hedged "empty OR AttributeError"; the MRO
settles it), so the skip tuple grows to ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER,
JOB_CARD, BOM, WORK_ORDER)`` with :func:`_work_order_ledger_preview_unavailable_flag`;
``plan_cancel`` needs no new flag (``get_gl_entries`` honestly returns empty).

**Cancel: TWO real bench gates, one visible to plan, one not.** ``validate_cancel`` refuses (a)
while ``status == "Stopped"`` — a STATUS condition, not docstatus, invisible to plan-time
disclosure, honored at execute via the standing answered-ErpnextError path — and (b) while any
submitted Stock Entry references this Work Order — the same condition the broker's own
blast-radius gate refuses on FIRST, so (b) rarely reaches the bench. Fan-in: 5 real
``Link → Work Order`` doctypes (Stock Entry, Job Card, Material Request, Pick List, Serial No —
the last a non-submittable master that can never appear in a submitted-links read, disclosed
not pretended) + ``amended_from``; zero cascade code changes as always.

**Side surface NOT granted — TWO ungated status mutators in a 19-callable whitelist:**
``stop_unstop`` (flips a submitted Work Order between Stopped and live with base write
permission — the dossier's own §8 centerpiece, confirmed verbatim) and ``close_work_order``
(drives the terminal "Closed" status; the dossier never mentioned it) — both move the very
status ``validate_cancel`` keys on, entirely outside docstatus and this broker's sight — plus
seven document factories (``make_stock_entry``/``make_job_card``/``make_material_request``/
``create_pick_list``/``make_stock_return_entry``/``make_work_order``/``make_bom``). The 5-verb
surface grants nothing toward any of the 19; no tool is built for any of them this landing.

**Breadth (Asset) — Wave 3's sixth and FINAL row, the twenty-second doctype: the first with
scheduled ASYNC GL posting, the first Wave-3 row with a CALLABLE native preview, and the first
whose leaf-node cancel is structurally unreachable.** Five more doctype-named tools (``*_asset``)
wrap the same generic handlers, pinned to Asset (``party_field=None``,
``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field="available_for_use_date"`` — the GL posting
date, and Asset IS in ``period_closing_doctypes``, so the closed-books check is natively EQUAL
to ERPNext here, the first Wave-3 row where it is not merely equal-or-stricter). Full semantics
in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s comment block + the module docstring there
(dossier at ``docs/plans/dossiers/asset.md`` — corrections: 18 not 9 cascade edges; the
structural cancel consequence of its own §4 finding unstated; the second async channel —
``make_post_gl_entry``, the daily deferred-CWIP scheduler — missed entirely).

**No skip-tuple change:** Asset has a real ``make_gl_entries`` (``asset.py:924``), so
``plan_submit`` calls the native preview like SI/PI — what is new is the DISCLOSURE layer,
two data-driven helpers in the Payment-Entry/Journal-Entry family:

1. :func:`_asset_submit_risk_flags` — the auto-created-and-submitted sibling Asset Movement
   (always; it is also why leaf cancel refuses later); the depreciation channel (only when the
   draft's ``calculate_depreciation`` is set: submit ARMS the schedules, the JEs post via
   ERPNext's daily ``post_depreciation_entries``, outside any marker this broker minted); the
   deferred-CWIP channel (only for a future ``available_for_use_date``: submit posts nothing
   NOW, ``make_post_gl_entry`` posts on the day the date arrives); the no-purchase-document and
   Composite-Component no-GL cases (``validate_make_gl_entry``'s own gates, pre-disclosed so an
   empty ``projected_gl`` is never ambiguous).
2. :func:`_asset_cancel_risk_flags` — the status gate readable on the draft
   (``validate_cancellation`` refuses In Maintenance/Out of Order and everything outside
   Submitted/Partially Depreciated/Fully Depreciated — a doomed cancel is named at plan time,
   the refusal itself stays ERPNext's answered throw at execute) + the multi-document-unwind
   disclosure. Fired on BOTH cancel plans — the single-op ``plan_cancel`` (rarely reached: see
   below) and per-node in ``plan_cascade_cancel`` (the real path).

**The structural cancel finding:** every submitted Asset carries a submitted Asset Movement
from birth (``on_submit`` creates and submits it), so the leaf-node blast-radius gate refuses
``plan_cancel`` for every Asset, by construction — ``plan_cascade_cancel`` is the governed
cancel path. Equal in effect to raw ERPNext (frappe runs ``on_cancel`` BEFORE
``check_no_back_links_exist``, so Asset's own hooks silently cancel the movements, schedules,
and depreciation JEs first — one bench cancel unwinds N documents invisibly); STRICTER in
consent (the cascade graph names every document — depreciation JEs included, discovered through
frappe's own dynamic-link walk — before a human mints the marker). Tests pin both halves.

**Side surface NOT granted:** 15 whitelist callables — the document-factory family
(``make_sales_invoice``, ``create_asset_repair``/``_maintenance``/``_capitalization``/
``_value_adjustment``, ``transfer_asset``, ``make_journal_entry``, ``make_asset_movement``,
``split_asset``) plus reads. Nothing granted, no tool built.

**Breadth (Packing Slip) — Wave 4's first row, the twenty-third doctype, THE SECOND DATELESS
ONE.** Five more doctype-named tools (``*_packing_slip``/``list_packing_slips``) wrap the same
generic handlers, pinned to Packing Slip (``party_field=None``, ``submit_via=
SUBMIT_VIA_RUN_METHOD``, ``date_field=None``). Full semantics in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s comment block + the module docstring there (dossier
at ``docs/plans/dossiers/packing_slip.md`` — correct on every axis it checked).

**The dateless axis is a pure REUSE — zero new code in ``plan.py`` or this module.** Packing Slip
declares ``"date_field": None`` exactly like BOM; ``_posting_date_of``/``_locks_for``/
``check_red_line``'s existing sentinel branch handle it unchanged. The only thing this landing
adds on that axis is a SECOND doctype independently proving its own datelessness from source (see
``test_erpnext.py``'s widened two-member exclusivity test).

**A genuinely new finding this landing does NOT build machinery for: Packing Slip also carries no
``company`` field.** All nine existing "wrong books" call sites (``_tool_plan_submit``,
``_tool_plan_cancel``, ``_tool_plan_cascade_cancel``, ``_governed_write``'s TOCTOU belt,
``_amend_document``) read ``doc.get("company")`` and compare it against the target's optional
company pin; a companyless document reads back ``None`` and is therefore refused, correctly and
unchanged, by any company-PINNED target ("wrong books": ``None`` can never match a real pin) —
and passes cleanly under the documented UNPINNED posture (``registry.py``; ``REG_UNPINNED`` in
the test suite). This is the SAME deny-on-unverifiable posture the codebase already applies to an
unreadable lock source or a malformed date — refusing what cannot be proven safe, never inventing
a bypass unasked. No ``company_field`` pin, no new sentinel, and no change to any of the nine
call sites are built here; a dedicated bypass (e.g. inferring company from the linked Delivery
Note) is a real design decision left open, not invented solo — see the module docstring in
``erpnext.py`` for the full argument. Practically: this doctype's reads (``get``/``list``) are
unaffected (no company check there at all); only submit/cancel/amend under a pinned target are
structurally out of reach — test coverage pins both the refusal (pinned) and the clean path
(unpinned) explicitly, rather than leaving the pinned case untested.

**Ledger preview: the Dunning/Blanket-Order/Job-Card/BOM/Work-Order "uncallable" category again**
— ``PackingSlip(StatusUpdater)``, and ``StatusUpdater`` is a bare ``Document`` subclass with no
``make_gl_entries`` anywhere, so the skip tuple grows to ``(DUNNING, LANDED_COST_VOUCHER,
BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP)`` with
:func:`_packing_slip_ledger_preview_unavailable_flag` naming it honestly; ``plan_cancel`` needs
no new flag (``get_gl_entries`` honestly returns empty — Packing Slip's own ``on_submit``/
``on_cancel`` rewrite a QUANTITY counter on another document via a raw SQL ``UPDATE``, never a
GL/Stock Ledger row).

**Cascade: a CASCADE LEAF, the narrowest fan-in this campaign has found.** A full-tree scan finds
exactly ONE ``Link → Packing Slip`` field anywhere in v16 — Packing Slip's own self-referencing
``amended_from`` — so ``cascade.py`` needed no changes (as always) and this landing builds no
external blast-radius test, because there is no real external edge for one to exercise.

**Side surface NOT granted:** ONE read-only whitelist callable (``item_details``, an item-picker
search filter that mutates nothing) — the smallest side-surface caveat this campaign has found;
nothing to withhold beyond documenting it for completeness of the house pattern.

**Breadth (Cost Center Allocation) — Wave 4's second row, the twenty-fourth doctype, and A
DOSSIER CORRECTION settled from source (see ``erpnext.py``'s own module docstring for the full
argument).** Five more doctype-named tools (``*_cost_center_allocation``/
``list_cost_center_allocations``) wrap the same generic handlers, pinned to Cost Center
Allocation (``party_field=None``, ``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field=
"valid_from"``).

**The dossier claimed this doctype was BOTH dated AND "dateless" — settled here as an ORDINARY
dated doctype, never the sentinel.** ``valid_from`` is a real, required Date field (default
"Today") — the sixth distinct date-fieldname pattern this campaign has found. It rides the
existing generic :func:`_date_field_for`/:func:`_posting_date_of`/:func:`_locks_for`/
:func:`pacioli.plan.check_red_line` machinery unchanged (the same fieldname-splice shape
Blanket Order's ``from_date`` and Asset's ``available_for_use_date`` already proved
generalizes) — zero new code for this axis, and the closed-books check runs the NORMAL,
equal-or-stricter path against a real read value, never :data:`pacioli.plan.NO_DATE_FIELD`.

**``company`` IS present (unlike Packing Slip's own landing immediately above) — the standing
"wrong books" belt applies in its ordinary form, no new machinery needed.** ``company`` is
fetched automatically from the chosen ``main_cost_center`` (``fetch_from:
"main_cost_center.company"``) and is therefore always populated on a valid document; a
company-PINNED target governs this doctype exactly as it governs any other company-bearing
doctype.

**Ledger preview: the Dunning/Blanket-Order/Job-Card/BOM/Work-Order/Packing-Slip "uncallable"
category again** — ``CostCenterAllocation(Document)`` is a direct ``Document`` subclass with no
``make_gl_entries`` anywhere, so the skip tuple grows to ``(DUNNING, LANDED_COST_VOUCHER,
BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION)`` with
:func:`_cost_center_allocation_ledger_preview_unavailable_flag` naming it honestly; ``plan_cancel``
needs no new flag (``get_gl_entries`` honestly returns empty — this doctype defines no
``on_submit``/``on_cancel`` hook of ANY kind, the simplest submit/cancel lifecycle this campaign
has found, never even a counter rewrite on another document).

**Cascade: a CASCADE LEAF, the same narrow shape as Packing Slip.** A full-tree scan finds
exactly ONE ``Link → Cost Center Allocation`` field anywhere in v16 — its own self-referencing
``amended_from`` — so ``cascade.py`` needed no changes and this landing builds no external
blast-radius test, because there is no real external edge for one to exercise.

**Side surface NOT granted:** ZERO whitelist callables — a full grep of
``cost_center_allocation.py`` finds none at all, the smallest side surface this campaign has
found (smaller even than Packing Slip's one read-only search filter); nothing to withhold
because nothing exists.

**Breadth (Supplier Scorecard Period) — Wave 4's third row, the twenty-fifth doctype, and Wave
4's FIRST ROW WITH A REAL PARTY FIELD** (see ``erpnext.py``'s own module docstring for the full
argument). Five more doctype-named tools (``*_supplier_scorecard_period``/
``list_supplier_scorecard_periods``) wrap the same generic handlers, pinned to Supplier
Scorecard Period (``party_field="supplier"``, ``submit_via=SUBMIT_VIA_RUN_METHOD``,
``date_field="start_date"``).

**``company`` is confirmed absent — the SECOND companyless doctype after Packing Slip, a dossier
omission its own summary table never checked for.** The same nine existing "wrong books" call
sites handle it correctly and unchanged: a company-PINNED target refuses every governed write
(``None`` never matches a real pin), while the documented UNPINNED posture
(``registry.py``/``REG_UNPINNED``) governs it cleanly. Unlike Packing Slip, this doctype is
genuinely DATED (``start_date``) — the first companyless doctype where the closed-books
disclosure still reads a real period-lock call (``client.get_period_locks(None, ...)``) even
under the unpinned target that alone can govern it; no new machinery, just a new combination of
two previously-proven mechanisms.

**CORRECTION (2026-07-21 live-prove batch):** the claim above is wrong — it never read a real
period-lock call at all; it CRASHED. ``get_period_locks``'s own first line
(``self._doc_path("Company", company)``) raises ``ErpnextError("a document name is required")``
for ``company=None``, so every ``plan_submit`` for this doctype under the only registry posture
that can govern it hard-failed on a live bench, unit-tested green only because the test double
tolerated ``company=None`` where the real client refuses. Fixed by a shape-driven guard in
:meth:`PacioliBroker._plan_closed_books_risk`/:func:`_locks_for` (falsy ``company`` short-circuits
to a plain disclosure before any lock read, the same early-return shape the dateless sentinel
already used) — see their own docstrings for the full finding and the doctype-blind fix (Bank
Guarantee/Project Update/Shipment/Asset Maintenance Log carry the identical shape and were
identically broken, not just this one row).

**Ledger preview: the Dunning/Blanket-Order/Job-Card/BOM/Work-Order/Packing-Slip/Cost-Center-
Allocation "uncallable" category again** — ``SupplierScorecardPeriod(Document)`` is a direct
``Document`` subclass with no ``make_gl_entries`` anywhere, so the skip tuple grows to
``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD)`` with
:func:`_supplier_scorecard_period_ledger_preview_unavailable_flag` naming it honestly;
``plan_cancel`` needs no new flag (``get_gl_entries`` honestly returns empty — this doctype
defines no ``on_submit``/``on_cancel`` hook of any kind, the same simplest-lifecycle shape Cost
Center Allocation's own landing established).

**Cascade: a CASCADE LEAF, the same narrow shape as Packing Slip/Cost Center Allocation.** A
full-tree scan finds exactly ONE ``Link → Supplier Scorecard Period`` field anywhere in v16 —
its own self-referencing ``amended_from`` — so ``cascade.py`` needed no changes and this landing
builds no external blast-radius test, because there is no real external edge for one to
exercise. (The relationship with its own parent Supplier Scorecard doctype runs the OTHER way —
this doctype's own ``scorecard`` field is a Link TO Supplier Scorecard, never the reverse.)

**Side surface NOT granted:** ZERO whitelist callables — a full grep of
``supplier_scorecard_period.py`` finds none at all; the one module-level mapper helper
(``make_supplier_scorecard``) is undecorated, never browser-RPC-reachable on its own.

**A caveat load-bearing to the whole doctype, not this broker's own scope:** Supplier Scorecard
Period is ordinarily MACHINE-GENERATED AND MACHINE-SUBMITTED by its own parent Supplier
Scorecard doctype — on every one of the parent's own saves, and once daily via the
``refresh_scorecards`` scheduled job (``erpnext/hooks.py:469``) — never by this broker, which
only ever governs an EXISTING document through its own plan/consent/execute path. See
``erpnext.py``'s own module docstring for the full source-cited argument (a dossier §11
correction: its RED FLAGS scan was correctly scoped to ``supplier_scorecard_period.py`` alone,
but the parent doctype's own scheduler-driven behavior is real operational context worth
disclosing).

**Breadth (Quality Inspection) — Wave 4's fourth row, the twenty-sixth doctype, the FIRST DOCTYPE
ON A DYNAMIC LINK PAIR SINCE QUOTATION** (see ``erpnext.py``'s own module docstring for the full
argument). Five more doctype-named tools (``*_quality_inspection``/``list_quality_inspections``)
wrap the same generic handlers, pinned to Quality Inspection (``party_field=None``,
``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field="report_date"``).

**Ledger preview: the Dunning/Blanket-Order/Job-Card/BOM/Work-Order/Packing-Slip/Cost-Center-
Allocation/Supplier-Scorecard-Period "uncallable" category again** — ``QualityInspection
(Document)`` is a direct ``Document`` subclass with no ``make_gl_entries`` anywhere in either the
erpnext-16 OR frappe-16 checkouts (the widest MRO sweep this campaign has run), so the skip tuple
grows to ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION)`` with
:func:`_quality_inspection_ledger_preview_unavailable_flag` naming it honestly.

**Two NEW data-driven risk-flag functions, both source-verified off the draft's own fields, the
Asset precedent (never a blanket warning):**

1. :func:`_quality_inspection_submit_risk_flags` — a genuine dossier omission: ``before_submit``
   (``quality_inspection.py:152-153``) calls ``validate_readings_status_mandatory`` (210-213),
   which THROWS the first time it finds a ``readings`` row with no ``status`` value — a
   doomed-submit gate readable off the draft's own child rows before a marker is ever minted, the
   same "status gate readable on the draft" shape Asset's own ``validate_cancellation``
   disclosure established, here on the submit side.
2. :func:`_quality_inspection_cancel_risk_flags` — THE central dossier correction: ``on_cancel``
   unconditionally calls ``update_qc_reference()``, which writes this document's own name into
   the reference document via raw SQL/query-builder, bypassing that document's own validate/
   on_update/version history entirely. For ``reference_type == "Job Card"`` this hits Job Card's
   own TOP-LEVEL row directly (``UPDATE tabJob Card ...`` — no child table exists for that case);
   for every other ``reference_type`` it hits a child-table row instead (the shape the dossier's
   own §7 describes, but wrongly generalizes to Job Card too). The standing blast-radius check
   already refuses this cancel first whenever the reference document is CURRENTLY submitted — the
   same "rarely reached" shape Asset's own multi-document unwind carries — but an
   ALREADY-CANCELLED reference document is not protected the same way, and this flag names that
   plainly, branching its own wording on the draft's own ``reference_type``.

**Cascade: the FIRST real external blast-radius partner since Cost Center Allocation/Packing
Slip/Supplier Scorecard Period's own cascade-leaf shape.** A full-tree scan finds NINE ``Link ->
Quality Inspection`` fields: seven non-submittable child-table rows (``Delivery Note Item``,
``POS Invoice Item``, ``Purchase Invoice Item``, ``Purchase Receipt Item``, ``Sales Invoice
Item``, ``Stock Entry Detail``, ``Subcontracting Receipt Item``) + the self-referencing
``amended_from`` + **``Job Card`` — the one genuine external submittable edge** (``is_submittable:
1``, confirmed). ``cascade.py`` needed no changes (doctype-blind, as always); this landing DOES
build a real external blast-radius test, using a submitted Job Card as the realistic linked
document — the same shape Work Order's own Stock-Entry/Job-Card blast-radius test already
established, not a fabricated scenario.

**Side surface NOT granted:** FIVE ``@frappe.whitelist()`` callables (a SECOND dossier
correction — the dossier claimed only four, treating ``make_quality_inspection`` as undecorated;
verified ``@frappe.whitelist()`` at ``quality_inspection.py:487``) — two instance methods
(``get_item_specification_details``/``get_quality_inspection_template``) and three module-level
functions (``item_query``/``quality_inspection_query``/``make_quality_inspection``). This entry
grants NOTHING toward any of the five.

**Breadth (Installation Note) — Wave 4's fifth row, the twenty-seventh doctype, and Wave 4's
SECOND ROW WITH A REAL PARTY FIELD** (see ``erpnext.py``'s own module docstring for the full
argument). Five more doctype-named tools (``*_installation_note``/``list_installation_notes``)
wrap the same generic handlers, pinned to Installation Note (``party_field="customer"``,
``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field="inst_date"``).

**Ledger preview: the Dunning/Blanket-Order/Job-Card/BOM/Work-Order/Packing-Slip/Cost-Center-
Allocation/Supplier-Scorecard-Period/Quality-Inspection "uncallable" category again, reached
through a DEEPER MRO than any prior member** — ``InstallationNote(TransactionBase)``, and
``TransactionBase(StatusUpdater)`` — no ``make_gl_entries`` anywhere in either
``transaction_base.py`` or ``status_updater.py``, so the skip tuple grows to ``(DUNNING,
LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE)`` with
:func:`_installation_note_ledger_preview_unavailable_flag` naming it honestly.

**No new risk-flag function for ``on_submit``'s own ``validate_serial_no()`` gate — a deliberate
absence, not an oversight.** Every one of that gate's own throw conditions (a serialized Item
missing its serial, a serial that doesn't exist, a serial that doesn't match the linked Delivery
Note Item's own serial numbers) requires reading a DIFFERENT doctype (``Item``, ``Serial No``,
the referenced ``Delivery Note Item``) — none is derivable from this draft's own fields alone, the
same reason every existing risk-flag function in this module reads only the draft's own fields and
never fetches a second doctype. Disclosed instead in ``erpnext.py``'s own module docstring as an
ERPNext-native doomed-submit path this broker's ``plan_submit`` structurally cannot preview — the
same class of gap Sales Order's own ``check_nextdoc_docstatus`` and Purchase Order's own
``check_for_on_hold_or_closed_status`` already carry, surfacing as an ordinary answered
``ErpnextError`` at execute time, never bypassed. ``on_cancel`` and the shared
``update_prevdoc_status()`` mechanism (both submit and cancel) carry no new risk-flag function
either — the same shared ``update_qty()``/``validate_qty()`` StatusUpdater pair Packing Slip's own
landing already named and left unmodeled (a quantity-counter rewrite plus ERPNext's own
over-allowance guard, never a flag this broker fabricates).

**Cascade: a CASCADE LEAF, the same narrow shape as Packing Slip/Cost Center Allocation/Supplier
Scorecard Period.** A full-tree scan finds exactly ONE ``Link → Installation Note`` field anywhere
in v16 — its own self-referencing ``amended_from`` — so ``cascade.py`` needed no changes and this
landing builds no external blast-radius test, because there is no real external edge to exercise
(the child table's own references to Delivery Note are plain ``Data`` fields, never ``Link``).

**Side surface NOT granted:** ZERO whitelist callables — a full grep of ``installation_note.py``
finds none at all, the same smallest side-surface shape Cost Center Allocation's own landing
established.

**Breadth (Shipment) — Wave 4's sixth row, the twenty-eighth doctype, and the FIRST doctype with
TWO independent dynamic-selector pairs** (see ``erpnext.py``'s own module docstring for the full
argument). Five more doctype-named tools (``*_shipment``/``list_shipments``) wrap the same generic
handlers, pinned to Shipment (``party_field=None``, ``submit_via=SUBMIT_VIA_RUN_METHOD``,
``date_field="pickup_date"``).

**Ledger preview: the Dunning/Blanket-Order/Job-Card/BOM/Work-Order/Packing-Slip/Cost-Center-
Allocation/Supplier-Scorecard-Period/Quality-Inspection/Installation-Note "uncallable" category
again, this time the SIMPLEST MRO in the category** — ``Shipment(Document)`` directly, no
``make_gl_entries`` anywhere in ``shipment.py`` or ``Document`` itself, so the skip tuple grows to
``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
SHIPMENT)`` with :func:`_shipment_ledger_preview_unavailable_flag` naming it honestly.

**No new risk-flag function for ``on_submit``'s own two throws — a deliberate absence, not an
oversight.** Both (an empty ``shipment_parcel`` table; ``value_of_goods == 0``) are doc-readable,
but they are ordinary reqd/business-rule guards, not a hidden or async mechanism — consistent with
every other "simple row" landing in this campaign (Job Card/BOM/Work Order/Packing Slip/Cost Center
Allocation/Supplier Scorecard Period), so they surface as an ordinary answered ``ErpnextError`` at
execute time, never pre-flagged. ``on_cancel`` carries no throw of its own at all.

**Cascade: a CASCADE LEAF, the same narrow shape as Packing Slip/Cost Center Allocation/Supplier
Scorecard Period/Installation Note.** A full-tree scan finds exactly ONE ``Link → Shipment`` field
anywhere in v16 — its own self-referencing ``amended_from`` — so ``cascade.py`` needed no changes
and this landing builds no external blast-radius test, because there is no real external edge to
exercise.

**Side surface NOT granted:** THREE read-only ``@frappe.whitelist()`` callables
(``get_address_name``/``get_contact_name``/``get_company_contact``) — the WIDEST all-read-only
surface this campaign has found (Packing Slip's own precedent was ONE) — confirmed, each one, to
mutate nothing; nothing granted toward any of them.

**Breadth (Sales Forecast) — Wave 4's seventh row, the twenty-ninth doctype** (see ``erpnext.py``'s
own module docstring for the full argument). Five more doctype-named tools (``*_sales_forecast``/
``list_sales_forecasts``) wrap the same generic handlers, pinned to Sales Forecast
(``party_field=None``, ``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field="posting_date"``).

**Ledger preview: the same "uncallable" category again, this time the CLEANEST MRO in it** —
``SalesForecast(Document)`` directly, and its own import block pulls in ZERO accounting/stock-
controller-related names at all (not even an exception class, unlike Job Card's/Shipment's own
bare-``Document`` shape). No ``make_gl_entries`` anywhere, so the skip tuple grows to ``(DUNNING,
LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
SHIPMENT, SALES_FORECAST)`` with :func:`_sales_forecast_ledger_preview_unavailable_flag` naming it
honestly.

**No new risk-flag function for anything else — there is nothing else: ``on_submit`` and
``on_cancel`` are BOTH confirmed absent entirely** (the THIRD doctype after Cost Center
Allocation/Supplier Scorecard Period), so a governed cancel changes ``docstatus`` alone; the
doctype's own ``status`` field is never touched by this broker's own cancel path (only a
draft-only ``on_discard`` writes ``"Cancelled"`` into it, and that hook is unreachable from a
submitted document — see ``erpnext.py``'s own module docstring for the full ``document.py``
citation). This is disclosed in prose, not as a new flag, because it changes no risk surface this
broker's own plan/execute contract needs to warn about beyond the standard uncallable-preview
flag above.

**Cascade needed no changes.** A full-tree scan finds exactly one real external ``Link ->
Sales Forecast`` field (``Master Production Schedule.sales_forecast``), but that doctype's own
``is_submittable`` is ``None`` (falsy) — it can never reach ``docstatus == 1``, so this edge can
never actually block a cancel on a real bench. This landing's own blast-radius test pins the
doctype-blind MECHANISM against a synthetic stub, naming plainly that the specific scenario it
exercises cannot occur on a real bench.

**Side surface NOT granted:** two whitelisted callables (``generate_demand`` — an in-memory
``items`` child-table rewrite only; ``create_mps`` — returns an UNSAVED Master Production Schedule
draft, caller must save/submit separately). Neither is a submit/cancel override; this entry grants
NOTHING toward either.

**Breadth (Project Update) — Wave 4's eighth row, the thirtieth doctype** (see ``erpnext.py``'s own
module docstring for the full argument). Five more doctype-named tools (``*_project_update``/
``list_project_updates``) wrap the same generic handlers, pinned to Project Update
(``party_field=None``, ``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field="date"``).

**Ledger preview: the same "uncallable" category again** — ``ProjectUpdate(Document)`` directly,
import block pulls in ONLY ``frappe``/``Document``, zero accounting/stock-controller names. No
``make_gl_entries`` anywhere, so the skip tuple grows to ``(DUNNING, LANDED_COST_VOUCHER,
BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION,
SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST,
PROJECT_UPDATE)`` with :func:`_project_update_ledger_preview_unavailable_flag` naming it honestly.

**No new risk-flag function for anything else beyond the standard closed-books disclosure —
``on_submit``/``on_cancel`` are BOTH confirmed absent entirely (the FOURTH doctype after Cost
Center Allocation/Supplier Scorecard Period/Sales Forecast), and the class body is a bare
``pass``.** A governed submit or cancel through this broker changes ``docstatus`` alone.

**A GENUINELY NEW combination on the date axis, needing zero new plan.py/tools.py code:** ``date``
is a REAL, declared field (``date_field="date"``, never the ``date_field=None`` dateless pin) that
carries ``reqd: 0`` and no schema default — the first dated doctype in this campaign where BOTH
are absent, so an API-authored draft can carry a genuinely blank ``date``. The EXISTING
:func:`_posting_date_of`/:func:`pacioli.plan.check_red_line` machinery already governs this
correctly: a blank read comes back ``""``, which denies at both the ``plan_submit`` disclosure
(Envelope E6, ``ok: True`` with the risk flag named) and the real execute-time gate
(``pacioli.spine.governed_submit`` checks the plan's own stored ``posting_date``) — deny-biased,
never a crash, never a silent bypass.

**Company: the FOURTH companyless doctype** (after Packing Slip/Supplier Scorecard
Period/Shipment) — confirmed absent from all 9 enumerated fields. ``REG_UNPINNED`` is the only
registry shape this doctype can ever govern through; the existing nine "wrong books" call sites
need no change.

**Cascade needed no changes.** A full-tree scan finds exactly ONE ``Link -> Project Update`` field
anywhere in v16 — its own self-referencing ``amended_from`` — so this landing builds no external
blast-radius test, because there is no real external edge to exercise.

**Side surface, two layers — the second genuinely new, not surfaced by the dossier's own
file-scoped read (the same "scoped to the wrong file" shape Supplier Scorecard Period's own
landing established):** (1) ``project_update.py``'s own ``daily_reminder()`` whitelist, manually
triggered only (confirmed absent from ``hooks.py``'s ``scheduler_events``) — calls the
un-whitelisted ``email_sending()``, a synchronous ``frappe.sendmail()``. (2) Project's OWN module
(``project.py``, a different doctype) auto-creates Project Update drafts on a schedule
(``send_project_update_email_to_users``, via ``hourly``/``hourly_maintenance``) and later mutates
them from outside (``collect_project_status`` appends ``users`` rows via a plain save, draft-only
in practice; ``send_project_status_email_to_users`` flips ``sent`` via a raw ``db_set`` that
bypasses docstatus entirely) — load-bearing operational context, not a gap this entry grants or
withholds.

**Breadth (Maintenance Visit) — Wave 4, the thirty-first doctype and ninth row.** Five more
doctype-named tools (``*_maintenance_visit``) wrap the same generic handlers, pinned to
Maintenance Visit (``party_field="customer"``, ``submit_via=SUBMIT_VIA_RUN_METHOD``,
``date_field="mntc_date"``). Full semantics pinned in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s
own comment block (and the module docstring there, full source-cited finding); dossier at
``docs/plans/dossiers/maintenance_visit.md``.

**Ledger preview: the same "uncallable" category again, the SAME MRO depth Installation Note
established** — ``MaintenanceVisit(TransactionBase)``, and ``TransactionBase(StatusUpdater)`` —
no ``make_gl_entries`` anywhere in any of the three files (``maintenance_visit.py``,
``transaction_base.py``, ``status_updater.py``). The skip tuple grows to ``(DUNNING,
LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT)`` with
:func:`_maintenance_visit_ledger_preview_unavailable_flag` naming it honestly.

**TWO genuine data-driven risk-flag functions, the Asset-precedent shape — each fired doctype-
scoped at the call site, each computed entirely from the draft's own fields, never a second-
document read:**

1. :func:`_maintenance_visit_submit_risk_flags` — ``on_submit`` calls
   ``update_customer_issue(1)``, gated on ``not doc.maintenance_schedule``: for every ``purposes``
   row naming a Warranty Claim (``prevdoc_doctype == "Warranty Claim"`` with a truthy
   ``prevdoc_docname``), it directly rewrites that Warranty Claim's ``resolution_date``/
   ``resolved_by``/``resolution_details``/``status`` via ``wc_doc.db_update()`` — ONE independent
   write per matching row, so a single submit can touch more than one Warranty Claim. Warranty
   Claim carries no ``is_submittable`` key at all (confirmed absent, not ``0``) — it is NOT a
   submittable doctype, so there is no docstatus lifecycle for this write to bypass; ``db_update()``
   still skips ``validate()``/``on_update`` hooks, permission checks, and version history — the same
   bypass-the-normal-save-path shape Quality Inspection's own ``update_qc_reference()`` carries
   against a submittable reference document. Both the gating condition and the affected Warranty
   Claim docname(s) are fully readable from this draft's own ``maintenance_schedule``/``purposes``
   fields — no second-document read needed.
2. :func:`_maintenance_visit_cancel_risk_flags` — the cancel-direction counterpart, two parts:
   (a) ``check_if_last_visit()``'s own temporal-ordering peer constraint (see below) is disclosed
   in PROSE only, matching Installation Note's own ``validate_serial_no`` precedent — its throw
   condition needs a sibling read (a raw SQL join across OTHER Maintenance Visit rows) this plan
   cannot perform, so no flag computes whether it will actually fire; (b) if that gate does NOT
   throw, ``update_customer_issue(0)`` runs the SAME Warranty Claim write(s) with reset values
   computed from a second sibling query — the CANDIDATE Warranty Claim(s) are named (same
   draft-readable condition as submit), but the exact reset values are not, for the same reason.

**THE TEMPORAL-ORDERING CANCEL GATE, read line by line — a genuine dossier correction of its own
§7 framing.** ``check_if_last_visit()`` throws ``"Cancel Material Visits {0} before cancelling this
Maintenance Visit"`` when another SUBMITTED Maintenance Visit shares the SAME ``prevdoc_docname``
with a LATER ``mntc_date`` (or same date, later ``mntc_time``). The dossier calls this "the SAME
Warranty Claim" — but the raw SQL match is on ``prevdoc_docname`` STRING equality alone, with NO
``prevdoc_doctype`` filter at all (the source even carries a commented-out
``# check_for_doctype = d.prevdoc_doctype``, a filter ERPNext's own authors considered and left
unimplemented) — the gate is not schema-restricted to Warranty Claim. This is a SAME-DOCTYPE PEER
constraint, invisible to this broker's own blast-radius/cascade machinery: ``prevdoc_docname`` is a
Dynamic Link FROM the ``Maintenance Visit Purpose`` child table pointing OUT to an external
document, never a Link TO Maintenance Visit, so ``cascade.py``'s generic Link walk cannot see it —
and it is checked against OTHER Maintenance Visit rows, not documents linking to this one. No new
sibling-query machinery is built for it; it is disclosed here as a real, doomed-cancel path this
broker's ``plan_cancel`` structurally cannot preview, the same class of gap Installation Note's own
``validate_serial_no``/Sales Order's own ``check_nextdoc_docstatus`` already carry.

**Cascade needed no changes.** A full-tree scan finds exactly ONE ``Link -> Maintenance Visit``
field anywhere in v16 — its own self-referencing ``amended_from`` (matching the dossier's own §8
count) — so this landing builds no fabricated external blast-radius test, because there is no real
external edge to exercise; the temporal-ordering peer constraint above is a separate mechanism,
never a cascade edge.

**Side surface: ZERO** ``@frappe.whitelist()`` **callables** (confirmed by a full grep of
``maintenance_visit.py``, matching the dossier's own §9 count) — nothing to withhold because
nothing exists.

**Breadth (Maintenance Schedule) — Wave 4's tenth and last row, the thirty-second doctype.** Five
more doctype-named tools (``*_maintenance_schedule``) wrap the same generic handlers, pinned to
Maintenance Schedule (``party_field="customer"`` — the first party field in this campaign that is
genuinely optional, not ``reqd`` — ``submit_via=SUBMIT_VIA_RUN_METHOD``,
``date_field="transaction_date"``, rejoining the standing SO/PO/MR/SQ/Q/RFQ set with zero new date
plumbing). Full semantics pinned in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block
(and the module docstring there, full source-cited finding); dossier at
``docs/plans/dossiers/maintenance_schedule.md``.

**Ledger preview: the same "uncallable" category again, the SAME MRO depth Installation
Note/Maintenance Visit established** — ``MaintenanceSchedule(TransactionBase)``, and
``TransactionBase(StatusUpdater)`` — no ``make_gl_entries`` anywhere in any of the three files. The
skip tuple grows to ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER,
PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT,
MAINTENANCE_SCHEDULE)`` with :func:`_maintenance_schedule_ledger_preview_unavailable_flag` naming
it honestly.

**TWO genuine data-driven risk-flag functions, computed entirely from the draft's own fields, never
a second-document read:**

1. :func:`_maintenance_schedule_submit_risk_flags` — if ``schedules`` is empty, submit is doomed
   (ERPNext's own "Please click on 'Generate Schedule'" throw); otherwise, discloses (a) the exact
   count of Event documents ``on_submit`` will auto-create (one per ``schedules`` row, inserted
   directly via ``ignore_permissions=1`` — Event is not itself submittable) and (b) which ``items``
   rows carry a ``serial_and_batch_bundle`` (submit will run ``serial_no_doc.save()`` against every
   Serial No the bundle resolves to — a FULL document save, never ``db_set``/raw SQL, the first
   cross-document mutation this campaign has found that is not a bypass).
2. :func:`_maintenance_schedule_cancel_risk_flags` — names which ``items`` rows will have that same
   field cleared back to ``None`` via the identical ``.save()`` mechanism, and discloses, as
   standing prose (every submitted Maintenance Schedule has non-empty ``schedules`` by
   construction), that cancel permanently DELETES every Event this document's own submit created
   (``frappe.delete_doc``, not orphaned) — and that ``on_cancel`` itself carries no gate of any
   kind against a linked Maintenance Visit, verified plainly by a full-file grep.

**Cascade — genuinely NOT a leaf, the first Wave 4 row since Quality Inspection to carry a real
external blast-radius partner.** A full-tree scan finds Maintenance Visit's own real, static
``maintenance_schedule`` Link (``is_submittable=1``) pointing at this doctype — fully visible to
``get_submitted_linked_docs`` with zero ``cascade.py`` changes. A submitted Maintenance Visit
therefore refuses this broker's own leaf-node ``plan_cancel`` outright, even though ERPNext's own
``on_cancel`` enforces no such gate — this broker is stricter than ERPNext here, pinned by a real
(not fabricated) blast-radius test.

**Side surface: FIVE** ``@frappe.whitelist()`` **callables** (confirmed by a full grep of
``maintenance_schedule.py``, matching the dossier's own §9 count) — draft-only or read-only
throughout; nothing here mutates already-submitted state, so nothing is granted.

**Breadth (Asset Maintenance Log) — Wave 5's first row, the thirty-third doctype.** Five more
doctype-named tools (``*_asset_maintenance_log``) wrap the same generic handlers, pinned to Asset
Maintenance Log (``party_field=None``, ``submit_via=SUBMIT_VIA_RUN_METHOD``,
``date_field="completion_date"``, the FIFTH companyless doctype). Full semantics pinned in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block (and the module docstring there,
full source-cited finding); dossier at ``docs/plans/dossiers/asset_maintenance_log.md``.

**Ledger preview: the same "uncallable" category, the SIMPLEST bare-``Document`` MRO** —
``AssetMaintenanceLog(Document)`` directly, no ``make_gl_entries`` anywhere. The skip tuple grows
to ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT, MAINTENANCE_SCHEDULE,
ASSET_MAINTENANCE_LOG)`` with :func:`_asset_maintenance_log_ledger_preview_unavailable_flag`
naming it honestly.

**TWO genuine risk-flag functions:**

1. :func:`_asset_maintenance_log_submit_risk_flags` — deterministic from the draft's own
   ``maintenance_status``/``completion_date`` fields: doomed (naming ERPNext's own exact throw
   text) unless the combination is (``"Completed"`` + a set ``completion_date``) or
   (``"Cancelled"`` + a blank one); when not doomed, discloses the cross-document ``.save()``
   cascade ``update_maintenance_task()`` performs (the linked Task, conditionally, AND the parent
   Asset Maintenance record, unconditionally — which re-triggers ITS OWN ``on_update`` and can
   touch sibling Asset Maintenance Log documents), plus standing prose naming the two post-submit
   status-rewrite bypasses (the scheduler's raw SQL — draft-only in practice — and the parent's own
   ungated ``db_set``, which genuinely can reach a submitted, Completed log).
2. :func:`_asset_maintenance_log_cancel_risk_flags` — standing prose: ``on_cancel`` is CONFIRMED
   ABSENT ENTIRELY, so cancelling never reverses either mutation ``on_submit`` performed.

**Cascade: a CASCADE LEAF** — the only ``Link -> Asset Maintenance Log`` field in the v16 tree is
its own self-referencing ``amended_from``; no fabricated external blast-radius test.

**Side surface: ONE** ``@frappe.whitelist()`` **callable** (``get_maintenance_tasks``, confirmed
read-only, matching the dossier's own §9 count) — nothing to withhold.

**Breadth (Bank Guarantee) — Wave 5's second row, the thirty-fourth doctype.** Five more
doctype-named tools (``*_bank_guarantee``) wrap the same generic handlers, pinned to Bank
Guarantee (``party_field=None`` — a genuine DUAL CONDITIONAL customer/supplier pair, never "no
party concept at all" — ``submit_via=SUBMIT_VIA_RUN_METHOD`` (a DOSSIER CORRECTION: NOT
``client_rpc`` — ``on_submit`` is a hook, never a ``submit()``/``cancel()`` override),
``date_field="start_date"``, the SIXTH companyless doctype). Full semantics pinned in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block (and the module docstring there,
full source-cited finding); dossier at ``docs/plans/dossiers/bank_guarantee.md``.

**Ledger preview: the same "uncallable" category, the SIMPLEST bare-``Document`` MRO** —
``BankGuarantee(Document)`` directly, no ``make_gl_entries`` anywhere. The skip tuple grows to
``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT, MAINTENANCE_SCHEDULE,
ASSET_MAINTENANCE_LOG, BANK_GUARANTEE)`` with
:func:`_bank_guarantee_ledger_preview_unavailable_flag` naming it honestly.

**ONE genuine risk-flag function:** :func:`_bank_guarantee_submit_risk_flags` — deterministic
from the draft's own ``bank_guarantee_number``/``name_of_beneficiary``/``bank`` fields (none
``reqd`` at the schema level): doomed, naming ERPNext's own exact throw text, for the FIRST of the
three that is blank (the checks run in strict source order and stop at the first failure); when
all three are present, submit succeeds with NO further side effect of any kind — ``on_submit``
here performs validation only, the plainest "doomed-or-nothing" submit story this campaign has
found. **No cancel risk-flag function**: ``on_cancel`` is CONFIRMED ABSENT, but with nothing for
it to reverse either (unlike Asset Maintenance Log's own cross-document cascade), a bare
``docstatus`` flip here is the unremarkable case every simple doctype in this campaign already
carries un-disclosed (Stock Reconciliation/LCV/Blanket Order/BOM/Job Card/Work
Order/Packing Slip/Cost Center Allocation/Supplier Scorecard Period's own precedent).

**Cascade: a CASCADE LEAF** — the only ``Link -> Bank Guarantee`` field in the v16 tree (both
erpnext and frappe checkouts) is its own self-referencing ``amended_from``; no fabricated
external blast-radius test.

**Side surface: ONE** ``@frappe.whitelist()`` **callable** (``get_voucher_details``, confirmed
read-only, matching the dossier's own count) — nothing to withhold.

**Breadth (Asset Movement) — Wave 5's third row, the thirty-fifth doctype, and the SECOND
Datetime-dated doctype in this campaign (after Work Order).** Five more doctype-named tools
(``*_asset_movement``) wrap the same generic handlers, pinned to Asset Movement
(``party_field=None`` — a Dynamic Link provenance pair, never a GL party —
``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field="transaction_date"`` — the SAME literal
fieldname seven Date-typed doctypes already use, but Asset Movement's own copy is Datetime-typed,
needing the SAME projection Work Order's landing built, reused unchanged). Full semantics pinned
in :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block (and the module docstring there,
full source-cited finding); dossier at ``docs/plans/dossiers/asset_movement.md``.

**Ledger preview: the same "uncallable" category, bare** ``Document`` **MRO** —
``AssetMovement(Document)`` directly, no ``make_gl_entries`` anywhere. The skip tuple grows to its
NINETEENTH member: ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER,
PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT,
MAINTENANCE_SCHEDULE, ASSET_MAINTENANCE_LOG, BANK_GUARANTEE, ASSET_MOVEMENT)`` with
:func:`_asset_movement_ledger_preview_unavailable_flag` naming it honestly.

**ONE genuine risk-flag function, fired on BOTH directions:** :func:`_asset_movement_write_risk_flags`
— THE central finding of this landing: ``on_submit`` and ``on_cancel`` call the EXACT SAME method
(``set_latest_location_and_custodian_in_asset``), the first doctype this campaign has found whose
submit and cancel hooks are textually identical, so its own disclosure is unified behind an ``op``
parameter (the :func:`_pos_risk_flags` shape) rather than two near-duplicate functions. Names,
data-driven off the draft's own ``assets`` child rows, every referenced Asset whose
``location``/``custodian`` this write recomputes and overwrites via a raw ``frappe.db.set_value``
bypass (no ``validate()``, no hooks, no version history on the Asset — the Maintenance Visit
``update_status_and_actual_date`` grade). On cancel, an UNCONDITIONAL prose addition names the
asymmetric truthy guard (``custodian`` clears to empty on a full rollback; ``location`` does not)
— a genuine dossier correction (its own §7 claimed both clear symmetrically).

**Cascade: a LEAF for INCOMING links** — the only ``Link -> Asset Movement`` field in the v16 tree
(both erpnext and frappe checkouts) is its own self-referencing ``amended_from`` — **but Asset
Movement itself is one of Asset's own 18 ``Link -> Asset`` edges** (via Asset Movement Item's own
``asset`` field), so a submitted Asset Movement DOES appear as a dependent node inside an Asset's
own ``plan_cascade_cancel`` graph; :func:`_tool_plan_cascade_cancel`'s per-node loop fires the same
write-mechanism disclosure for any ``ASSET_MOVEMENT`` node, docname-qualified.

**Side surface: ZERO** ``@frappe.whitelist()`` **callables** (confirmed by a full grep of
``asset_movement.py``, matching the dossier's own count) — nothing to withhold because nothing
exists.

**Breadth (Delivery Trip) — the thirty-sixth doctype, the THIRD Datetime-dated doctype in this
campaign, and the first row whose own cancel side effect collides with this broker's own cascade
order.** Five more doctype-named tools (``*_delivery_trip``) wrap the same generic handlers,
pinned to Delivery Trip (``party_field=None`` — ``company`` IS present, NOT companyless —
``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field="departure_time"`` — a brand-new fourteenth
date-fieldname pattern, no collision with any prior member). Full semantics pinned in
:data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block (and the module docstring there,
full source-cited finding); dossier at ``docs/plans/dossiers/delivery_trip.md``.

**Ledger preview: the same "uncallable" category, bare** ``Document`` **MRO** —
``DeliveryTrip(Document)`` directly, no ``make_gl_entries`` anywhere. The skip tuple grows to its
TWENTIETH member: ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER,
PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT,
MAINTENANCE_SCHEDULE, ASSET_MAINTENANCE_LOG, BANK_GUARANTEE, ASSET_MOVEMENT, DELIVERY_TRIP)`` with
:func:`_delivery_trip_ledger_preview_unavailable_flag` naming it honestly.

**TWO genuine risk-flag functions.** :func:`_delivery_trip_submit_risk_flags` — the "driver must be
set to submit" doomed-submit gate, deterministic off the draft's own ``driver`` field (the
Maintenance Schedule "submit will be REFUSED" shape), plus an unconditional prose addition naming
every linked Delivery Note from the draft's own ``delivery_stops`` rows that submit will ALSO
refuse against if still draft (a live read this plan cannot perform — the Asset Movement
"monotonic validate needs other movements' state" shape). :func:`_delivery_trip_cancel_risk_flags`
— THE central finding of this landing: naming, data-driven off the SAME ``delivery_stops`` rows,
every Delivery Note that ``update_delivery_notes(delete=True)`` force-clears via ``note_doc.
save()`` with ``ignore_validate_update_after_submit=True``. Read against ``frappe/model/
document.py``'s own ``_save``/``check_docstatus_transition``/``validate_update_after_submit``:
this is the SANCTIONED ``update_after_submit`` save path — permission checks, freshness, link
validation, ``before``/``on_update_after_submit`` hooks, and version history all still run; only
the doctype's own custom ``validate()`` (a property of the action itself, not a discretionary
skip) and the ONE ``allow_on_submit`` field-lock check (the flag's entire, narrow purpose) are
skipped — placed ABOVE every bypass grade this campaign has named (Maintenance Visit's
``db_update``, Asset Movement's/Asset Maintenance Log's ``frappe.db.set_value``, Quality
Inspection's raw SQL), not a bypass at all. THE SHARPEST FINDING, CORRECTED 2026-07-21 by
supervisor verification (the original landing's claim that this Delivery Trip's OWN cancel step
raises was UNREACHABLE — see :func:`_delivery_trip_cancel_risk_flags`'s own docstring for the
full source-cited correction): ``run_post_save_methods`` runs ``on_cancel`` BEFORE
``check_no_back_links_exist`` (``document.py:1450-1452``), so a LEAF cancel of this Delivery Trip
NATIVELY SUCCEEDS as a self-unlinking cancel — ``update_delivery_notes(delete=True)`` clears the
linked notes' fields via the sanctioned ``note_doc.save()`` before any back-link check runs. The
real collision lands one step earlier, on the DEPENDENT: cancelling a Delivery Note while its
Delivery Trip is still submitted hits frappe's own ``check_if_doc_is_linked(method="Cancel")``
(``delete_doc.py``), which walks child-table Link fields too — ``Delivery Stop.delivery_note``
(``istable=1``) still points the submitted trip at that note, and Delivery Note's own
``on_cancel`` exemption tuple (``delivery_note.py:500-505``) does not cover Delivery Trip — so
this broker's OWN dependents-first cascade order (:func:`pacioli.cascade.build_cascade`) makes
the cascade raise ``frappe.LinkExistsError`` at the NOTE step, before this Delivery Trip's own
cancel step is ever reached. Net (at landing time): this broker had NO WORKING CANCEL PATH for a
submitted Delivery Trip with a submitted linked Delivery Note — the leaf ``plan_cancel`` was
refused outright by the standing blast-radius check, and the only remaining path,
``plan_cascade_cancel``, fails at execute at the note step above. Two structural fixes were named
without choosing either — an open design decision for John. The originally-claimed
``ValidationError("Cannot edit cancelled document")`` (``document.py:1149-1150``) survives as a
weaker SECOND lock, reachable only if the note step were somehow already past.

**RULED 2026-07-21 — John's ruling 1** (``docs/plans/2026-07-21-cancel-truth-rulings.md``): option
**(a)**, belted. Delivery Trip is registered in :data:`pacioli.erpnext.SELF_UNLINKING_DOCTYPES`;
the leaf ``plan_cancel`` blast-radius gate (:meth:`PacioliBroker._tool_plan_cancel`) now treats a
registered doctype's own submitted incoming links as non-blocking instead of refusing outright —
one surgical condition, byte-identical for every other doctype. The belt (the full note-side
disclosure above) stays; the suspenders is new: a post-execute readback
(:func:`_delivery_trip_self_unlink_readback`, wired into :meth:`PacioliBroker._governed_write`)
re-reads every Delivery Note the pre-cancel draft named and attests the self-unlink actually
happened, reporting any note still linked (``self_unlink_readback`` in the outcome payload) rather
than silently passing — the cancel already happened by the time this runs, so a still-linked note
is truth-telling, never a retro-refusal. Option (b) — a cascade-order exception — was NOT taken:
``plan_cascade_cancel`` (:meth:`PacioliBroker._tool_plan_cascade_cancel`'s per-node loop) is
UNCHANGED, still structurally dead at the Delivery Note step, disclosed exactly as before.

**Cascade: GENUINELY NOT A LEAF for graph-BUILDING purposes, unchanged by the ruling** — a
full-tree grep for ``"options": "Delivery Trip"`` over both the erpnext-16 and frappe-16 checkouts
finds exactly two hits: this doctype's own self-referencing ``amended_from``, and Delivery Note's
own real, static, submittable ``delivery_trip`` Link, so ``plan_cascade_cancel`` still orders a
submitted Delivery Note ahead of it and still fails at that note step (the RULED note above).
ERPNext's own ``on_cancel`` enforces NO gate whatsoever against a linked Delivery Note's docstatus
— this broker's own leaf-cancel blast-radius check was, PRE-RULING, genuinely STRICTER than
ERPNext's own self-unlinking posture here; RULED 2026-07-21 to match it for this ONE registered
doctype only (the Maintenance Schedule precedent — a real external-partner Link with no
self-unlinking receipt — still holds for every OTHER doctype's leaf cancel, unchanged; zero
``cascade.py`` changes needed either way, the same generic walk every prior edge rides).

**Side surface: FIVE** ``@frappe.whitelist()`` **callables** (confirmed by a full grep of
``delivery_trip.py``, matching the dossier's own count) — ``process_route`` and
``notify_customers`` both carry a genuine submitted-state mutation surface (no docstatus check on
either); nothing granted.

**Breadth (Asset Value Adjustment) — Wave 5's fifth row, the thirty-seventh doctype: the
campaign's FIRST sibling-document FACTORY.** Five more doctype-named tools (``*_asset_value_
adjustment``) wrap the same generic handlers, pinned to Asset Value Adjustment
(``party_field=None``, ``submit_via=SUBMIT_VIA_RUN_METHOD``, ``date_field="date"`` — REJOINING
Project Update's own eleventh date pattern). Full semantics in :data:`pacioli.erpnext.
SUPPORTED_DOCTYPES`'s comment block + the module docstring there (dossier at ``docs/plans/
dossiers/asset_value_adjustment.md`` — corrections: ``company`` present-but-optional is the
THIRD such row (after Purchase Invoice/Quality Inspection), never before consequential; the
dossier's own §10 excerpt drops the ``ignore_permissions`` line on the submit side, naming it
only on cancel; ``update_asset()`` arms a THIRD sibling-document channel via
``reschedule_depreciation``, entirely unnamed by the dossier's one-line summary).

**No skip-tuple growth in kind, but the tuple itself grows to its TWENTY-FIRST member:**
:func:`_asset_value_adjustment_ledger_preview_unavailable_flag` — the same bare-``Document`` MRO
this campaign already knows, with a twist worth naming: this doctype's own preview really is
empty (no GL posts under ITS name), yet submitting it DOES post real GL — through the
synchronously-created sibling Journal Entry. Two data-driven disclosure functions, the Payment-
Entry/Asset family:

1. :func:`_asset_value_adjustment_submit_risk_flags` — the sibling Journal Entry (unconditional,
   SYNCHRONOUS — a materially weaker disclosure need than Asset's own SCHEDULED depreciation
   channel, since this broker sees the whole thing inside the call it already governs); the
   doomed-submit company-blank gate (data-driven off the draft's own ``company`` field — the
   sibling JE's own ``company`` is ``reqd``, so a blank AVA company dooms the submit even though
   AVA's own schema never requires it); the closed-books scope gap (unconditional prose: this
   doctype can never itself be locked via ``period_closing_doctypes``, but the sibling JE — a
   genuine member — is natively gated for real, on the identical date value, a real execute-time
   failure this plan cannot see); the cross-document ``validate_date`` gate (unconditional prose —
   needs the linked Asset's own ``purchase_date``); the depreciation-reschedule sibling-document
   channel (unconditional prose — needs the linked Asset's own ``calculate_depreciation``/
   ``finance_books`` state: may CANCEL an existing submitted Asset Depreciation Schedule,
   preserving its already-posted depreciation JEs via ``should_not_cancel_depreciation_entries``,
   and CREATE+SUBMIT a new one); the raw-write bypass on the linked Asset itself
   (``row.db_update()``/``asset.db_update()`` — the Maintenance Visit grade — then
   ``asset.set_status()``'s own ``db_set`` — the Asset Movement/AML grade).
2. :func:`_asset_value_adjustment_cancel_risk_flags` — THE PERMISSION-ONLY BYPASS on the sibling
   JE's cancel (data-driven off the draft's own ``journal_entry`` field): ``ignore_permissions=
   True`` short-circuits ``Document.has_permission`` for ANY permission type
   (``document.py:407-408``), skipping ONLY the ACL/authorization gate — every hook, validation,
   ``check_no_back_links_exist``, and version-history write on the JE's own full ``.cancel()``
   lifecycle runs exactly as an ordinary cancel would. A bypass grade distinct in KIND from every
   data-integrity bypass this campaign has named (Maintenance Visit's ``db_update``, Asset
   Movement's/AML's ``db_set``, Quality Inspection's raw SQL) and narrower than Delivery Trip's
   own sanctioned ``update_after_submit`` save (which still waives ``validate()`` + one field
   lock while running permission checks normally) — this waives ONLY authorization, nothing about
   the sibling's own data-integrity checks. Plus the SAME depreciation-reschedule and raw-write
   disclosures the submit direction carries (sign-flipped, per ``update_asset()``'s own single
   shared implementation).

**Cascade, both directions of one relationship (the Asset Movement precedent):** CONFIRMED ZERO
incoming edges (a leaf for its own target scans — ``plan_cancel``/the leaf half of
``plan_cascade_cancel`` are never blocked by anything pointing IN), while its own ``asset`` field
is one of Asset's own 18 ``Link -> Asset`` edges, so a submitted Asset Value Adjustment appears as
a DEPENDENT NODE inside an Asset's own ``plan_cascade_cancel`` graph — the per-node cancel-risk
disclosure is wired into :meth:`PacioliBroker._tool_plan_cascade_cancel`'s own loop too, alongside
the leaf-path ``plan_cancel`` wiring in :meth:`PacioliBroker._tool_plan_cancel`.

**Side surface NOT granted:** ONE whitelist callable — ``get_value_of_accounting_dimensions``
(read-only, reads Asset field values). Nothing granted, no tool built.

**Breadth (Payment Order)** — Wave 5's sixth row, the thirty-eighth doctype. Full source-cited
finding in ``erpnext.py``'s ``SUPPORTED_DOCTYPES`` comment block + the module docstring there
(dossier at ``docs/plans/dossiers/payment_order.md`` — THE central correction: ``party_field`` is
``None``, not ``"party"`` — the dossier's own §1 calls ``party`` a static header field, but it
carries a real ``depends_on`` and is never read or written anywhere in ``payment_order.py``'s own
code, in either direction; also corrected: ``company`` present+reqd, never mentioned by the
dossier at all; "Override present: YES" repeats the Bank Guarantee dossier-error class — plain
hooks, no ``validate()`` at all, zero throws anywhere in the class; the ``period_closing_doctypes``
citation is a stale line number, 326-345 not 117-133).

**No skip-tuple growth in kind, the tuple grows to its TWENTY-SECOND member:**
:func:`_payment_order_ledger_preview_unavailable_flag` — the same bare-``Document`` MRO. ONE
data-driven disclosure function, fired on BOTH directions (the Asset Movement ``op``-parameter
shape, though NOT textually identical calls the way Asset Movement's own precedent is — ``on_cancel``
passes ``cancel=True`` to the SAME shared method, close but not byte-identical):

:func:`_payment_order_write_risk_flags` — THE central finding: ``update_payment_status`` writes a
status value directly onto EVERY referenced Payment Request/Payment Entry via raw
``frappe.db.set_value`` (no validate/hooks/version-history/permission check on the target — the
Asset Movement/AML bypass grade), the target doctype/field/child-column chosen by
``payment_order_type``. Data-driven off the draft's own ``references``/``payment_order_type``
fields — no live read needed to name WHICH documents get touched. The sharpest disclosure fires
cancel-direction only, and only for Payment-Request-typed rows: the write is UNCONDITIONAL (never
reads the target's current value first), so a Payment Request whose own status has since moved
past "Initiated" (to "Paid"/"Partially Paid"/etc. — six further values exist on that field) gets
stomped back to "Initiated" by this cancel regardless; Payment Entry's own mirror field carries
only two values total, so no equivalent loss exists on that branch.

**Cascade — genuinely NOT A LEAF, joining Delivery Trip (not the first Wave-5 row on this side —
Asset Movement/Asset Value Adjustment are the OTHER shape: leaves for their own target scans that
appear as dependents elsewhere; Payment Order is a real TARGET with real dependents, the same
side of the relationship Delivery Trip's own landing already established, here with THREE
incoming edges rather than Delivery Trip's one):** three submittable doctypes (Journal Entry,
Payment Entry, Payment Request) carry a genuine ``payment_order`` Link back to this doctype,
discovered by the standing generic ``get_submitted_linked_docs`` mechanism, zero ``cascade.py``
code needed. The write-risk disclosure is wired into
:meth:`PacioliBroker._tool_plan_submit`, the leaf-path :meth:`PacioliBroker._tool_plan_cancel`, AND
:meth:`PacioliBroker._tool_plan_cascade_cancel`'s own per-node loop — the last of these is how a
cascade-cancelled Payment Order (the common real case once a submitted JE/PE/PR dependent exists)
gets its own cancel-direction disclosure, since the TARGET is itself the last node in its own
graph.

**Side surface NOT granted:** THREE whitelist callables —
``get_mop_query``/``get_supplier_query`` (read-only search queries) and ``make_payment_records``
(MUTATES: builds and saves, never submits, a draft Journal Entry linked back via
``payment_order``, ``ignore_mandatory=True``, no dedup against a repeat call). Nothing granted, no
tool built toward any of the three.

**Breadth (Share Transfer)** — a full-attention landing (not a numbered wave row), the
thirty-ninth doctype. Full source-cited finding in ``erpnext.py``'s ``SUPPORTED_DOCTYPES``
comment block + the module docstring there (dossier at ``docs/plans/dossiers/share_transfer.md``,
pre-verified at ``docs/plans/dossiers/share_transfer.verify.md`` — the addendum's own two
corrections re-verified here: ``party_field``'s conditional directions were SWAPPED in the
original dossier, and Shareholder is NOT submittable at all; plus one finding beyond the
addendum's own scope — see below).

**No skip-tuple growth in kind, the tuple grows to its TWENTY-THIRD member:**
:func:`_share_transfer_ledger_preview_unavailable_flag` — the same bare-``Document`` MRO.

**Cascade — a genuinely ISOLATED LEAF, the strictest shape this campaign has found.** No other
doctype anywhere links to Share Transfer (one hit across both checkouts, its own self-referencing
``amended_from``), AND Share Transfer's own outgoing Link fields all point at non-submittable or
non-transactional doctypes (Shareholder, Share Type, Account, Company) — it can never appear as a
node in ANY ``plan_cascade_cancel`` graph, its own or anyone else's. No ``cascade.py`` wiring of
any kind; the write-risk disclosures below are wired only into :meth:`PacioliBroker._tool_plan_submit`
and the leaf path of :meth:`PacioliBroker._tool_plan_cancel`.

:func:`_share_transfer_submit_risk_flags` — THE central findings, submit direction, entirely
data-driven off the draft's own ``transfer_type``/``from_folio_no``/``to_folio_no`` fields:

  1. **THE SHARPEST FINDING, beyond the pre-verification addendum's own scope: a doomed-submit
     gate.** ``basic_validations()``'s Purchase branch (``share_transfer.py:170-177``) blanks
     ``self.to_shareholder`` to ``""`` at line 171, then — if ``self.from_folio_no`` is blank —
     calls ``self.autoname_folio(self.to_shareholder)`` at lines 174-175, passing the JUST-BLANKED
     field instead of ``self.from_shareholder``. Traced end to end through both checkouts (see
     ``erpnext.py``'s own module docstring for the full six-step chain, ``document.py:294-297``'s
     ``frappe.throw`` the terminus): any Purchase-type Share Transfer with a populated
     ``from_shareholder`` but a blank ``from_folio_no`` THROWS ``frappe.DoesNotExistError``
     ("Shareholder None not found") before any share_balance mutation ever runs — a real,
     reachable case (``from_folio_no``'s only auto-fill is a client-side ``fetch_from``, which
     never fires for a document authored via this broker's own REST channel).
  2. **``folio_no_validation()`` (share_transfer.py:222-240) writes a Shareholder's ``folio_no``
     on EVERY save, not only submit** — the addendum's own settled finding, disclosed as prose
     (needs the target Shareholder's live state, not readable from the draft alone).
  3. **The ``on_submit`` share_balance mutation itself**, branch by branch — every write a plain,
     hook-respecting ``.save()``/``.insert()`` on a non-submittable Shareholder (the Maintenance
     Schedule clean-``.save()`` honesty grade, never a bypass).

:func:`_share_transfer_cancel_risk_flags` — the cancel-direction mirror, plus the unguarded
``remove_shares()``/``get_shareholder_doc()`` risk (data-dependent, prose-disclosed, not gated —
the addendum's own landing risk #6): ``on_cancel`` re-resolves the same Shareholder names
``on_submit`` touched, throwing ``frappe.DoesNotExistError`` if any has since been deleted or
renamed.

**Side surface NOT granted:** ONE whitelist callable — ``make_jv_entry`` (builds an unsaved
Journal Entry dict, never inserted or submitted). Nothing granted, no tool built.
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
from pacioli.erpnext import (ASSET, ASSET_CAPITALIZATION, ASSET_MAINTENANCE_LOG, ASSET_MOVEMENT,
                             ASSET_REPAIR,
                             ASSET_VALUE_ADJUSTMENT, BANK_GUARANTEE,
                             BLANKET_ORDER, BOM, BOM_CREATOR, BUDGET, CONTRACT,
                             COST_CENTER_ALLOCATION,
                             DELIVERY_NOTE,
                             DELIVERY_TRIP,
                             DUNNING, ENQUEUE_ON_SUBMIT_CHANNELS, ENQUEUE_ON_SUBMIT_DOCTYPES,
                             ErpnextError, INSTALLATION_NOTE, INVOICE_DISCOUNTING, JOB_CARD,
                             JOURNAL_ENTRY,
                             LANDED_COST_VOUCHER, MAINTENANCE_SCHEDULE, MAINTENANCE_VISIT,
                             MATERIAL_REQUEST, PACKING_SLIP, PAYMENT_ENTRY, PAYMENT_ORDER,
                             PICK_LIST, POS_INVOICE, PRODUCTION_PLAN,
                             PROJECT_UPDATE, PURCHASE_INVOICE, PURCHASE_ORDER, PURCHASE_RECEIPT,
                             QUALITY_INSPECTION, QUOTATION, REQUEST_FOR_QUOTATION, SALES_FORECAST,
                             SALES_INVOICE, SALES_ORDER, SELF_UNLINKING_DOCTYPES, SHARE_TRANSFER,
                             SHIPMENT, STOCK_ENTRY,
                             STOCK_RECONCILIATION, SUBCONTRACTING_INWARD_ORDER,
                             SUBCONTRACTING_ORDER, SUBCONTRACTING_RECEIPT, SUPPLIER_QUOTATION,
                             SUPPLIER_SCORECARD_PERIOD, SUPPORTED_DOCTYPES, TIMESHEET, WORK_ORDER)
# _is_iso_date is package-private, reused here the same way erpnext.py already reuses it (one
# ISO-shape rule, never a second copy that could drift) — _posting_date_of's datetime→date
# projection must decide "is this prefix a real ISO date" with the SAME rule check_red_line and
# get_period_locks enforce downstream.
from pacioli.plan import (NO_DATE_FIELD, _is_iso_date, check_doctype, check_docname, check_op,
                          check_red_line, new_plan)
from pacioli.prove import INTENT, OUTCOME
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
    DoctypeDescriptor(SALES_ORDER, "sales_order", "sales_orders", "Sales Orders"),
    DoctypeDescriptor(PURCHASE_ORDER, "purchase_order", "purchase_orders", "Purchase Orders"),
    DoctypeDescriptor(MATERIAL_REQUEST, "material_request", "material_requests",
                      "Material Requests"),
    DoctypeDescriptor(DELIVERY_NOTE, "delivery_note", "delivery_notes", "Delivery Notes"),
    DoctypeDescriptor(PURCHASE_RECEIPT, "purchase_receipt", "purchase_receipts",
                      "Purchase Receipts"),
    DoctypeDescriptor(STOCK_ENTRY, "stock_entry", "stock_entries", "Stock Entries"),
    DoctypeDescriptor(SUPPLIER_QUOTATION, "supplier_quotation", "supplier_quotations",
                      "Supplier Quotations"),
    DoctypeDescriptor(QUOTATION, "quotation", "quotations", "Quotations"),
    DoctypeDescriptor(POS_INVOICE, "pos_invoice", "pos_invoices", "POS Invoices"),
    DoctypeDescriptor(DUNNING, "dunning", "dunnings", "Dunnings"),
    DoctypeDescriptor(STOCK_RECONCILIATION, "stock_reconciliation", "stock_reconciliations",
                      "Stock Reconciliations"),
    DoctypeDescriptor(LANDED_COST_VOUCHER, "landed_cost_voucher", "landed_cost_vouchers",
                      "Landed Cost Vouchers"),
    DoctypeDescriptor(REQUEST_FOR_QUOTATION, "request_for_quotation", "request_for_quotations",
                      "Request for Quotations"),
    DoctypeDescriptor(BLANKET_ORDER, "blanket_order", "blanket_orders", "Blanket Orders"),
    DoctypeDescriptor(JOB_CARD, "job_card", "job_cards", "Job Cards"),
    DoctypeDescriptor(BOM, "bom", "boms", "BOMs"),
    DoctypeDescriptor(WORK_ORDER, "work_order", "work_orders", "Work Orders"),
    DoctypeDescriptor(ASSET, "asset", "assets", "Assets"),
    DoctypeDescriptor(PACKING_SLIP, "packing_slip", "packing_slips", "Packing Slips"),
    DoctypeDescriptor(COST_CENTER_ALLOCATION, "cost_center_allocation",
                      "cost_center_allocations", "Cost Center Allocations"),
    DoctypeDescriptor(SUPPLIER_SCORECARD_PERIOD, "supplier_scorecard_period",
                      "supplier_scorecard_periods", "Supplier Scorecard Periods"),
    DoctypeDescriptor(QUALITY_INSPECTION, "quality_inspection",
                      "quality_inspections", "Quality Inspections"),
    DoctypeDescriptor(INSTALLATION_NOTE, "installation_note",
                      "installation_notes", "Installation Notes"),
    DoctypeDescriptor(SHIPMENT, "shipment", "shipments", "Shipments"),
    DoctypeDescriptor(SALES_FORECAST, "sales_forecast", "sales_forecasts", "Sales Forecasts"),
    DoctypeDescriptor(PROJECT_UPDATE, "project_update", "project_updates", "Project Updates"),
    DoctypeDescriptor(MAINTENANCE_VISIT, "maintenance_visit", "maintenance_visits",
                      "Maintenance Visits"),
    DoctypeDescriptor(MAINTENANCE_SCHEDULE, "maintenance_schedule", "maintenance_schedules",
                      "Maintenance Schedules"),
    DoctypeDescriptor(ASSET_MAINTENANCE_LOG, "asset_maintenance_log", "asset_maintenance_logs",
                      "Asset Maintenance Logs"),
    DoctypeDescriptor(BANK_GUARANTEE, "bank_guarantee", "bank_guarantees", "Bank Guarantees"),
    DoctypeDescriptor(ASSET_MOVEMENT, "asset_movement", "asset_movements", "Asset Movements"),
    DoctypeDescriptor(DELIVERY_TRIP, "delivery_trip", "delivery_trips", "Delivery Trips"),
    DoctypeDescriptor(ASSET_VALUE_ADJUSTMENT, "asset_value_adjustment",
                      "asset_value_adjustments", "Asset Value Adjustments"),
    DoctypeDescriptor(PAYMENT_ORDER, "payment_order", "payment_orders", "Payment Orders"),
    DoctypeDescriptor(SHARE_TRANSFER, "share_transfer", "share_transfers", "Share Transfers"),
    DoctypeDescriptor(BOM_CREATOR, "bom_creator", "bom_creators", "BOM Creators"),
    DoctypeDescriptor(BUDGET, "budget", "budgets", "Budgets"),
    DoctypeDescriptor(TIMESHEET, "timesheet", "timesheets", "Timesheets"),
    DoctypeDescriptor(CONTRACT, "contract", "contracts", "Contracts"),
    DoctypeDescriptor(PICK_LIST, "pick_list", "pick_lists", "Pick Lists"),
    DoctypeDescriptor(ASSET_REPAIR, "asset_repair", "asset_repairs", "Asset Repairs"),
    DoctypeDescriptor(INVOICE_DISCOUNTING, "invoice_discounting", "invoice_discountings",
                     "Invoice Discountings"),
    DoctypeDescriptor(ASSET_CAPITALIZATION, "asset_capitalization", "asset_capitalizations",
                     "Asset Capitalizations"),
    DoctypeDescriptor(PRODUCTION_PLAN, "production_plan", "production_plans",
                     "Production Plans"),
    DoctypeDescriptor(SUBCONTRACTING_ORDER, "subcontracting_order", "subcontracting_orders",
                     "Subcontracting Orders"),
    DoctypeDescriptor(SUBCONTRACTING_INWARD_ORDER, "subcontracting_inward_order",
                     "subcontracting_inward_orders", "Subcontracting Inward Orders"),
    DoctypeDescriptor(SUBCONTRACTING_RECEIPT, "subcontracting_receipt", "subcontracting_receipts",
                     "Subcontracting Receipts"),
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
                       "off-box-recorded anchor. Also sweeps any queued-consequence submit "
                       "(e.g. BOM Creator, whose BOM tree builds on a background queue after "
                       "submit answers) still marked committed_pending_async, attesting the "
                       "real Completed/Failed result when the worker has landed it, or "
                       "reporting it still pending.",
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


# Source-verified date-field pins for doctypes this broker does NOT govern but which ride a
# plan_cascade_cancel graph as dependent nodes (see _date_field_for's graph-participant
# paragraph for the receipts and the discovery story). Only a declared-dateless (None) pin
# exists today; a dated graph participant would carry its real fieldname here the same way.
GRAPH_NODE_DATE_FIELDS = {
    # Asset's auto-created sibling (asset.py's make_depreciation_schedule): zero Date/Datetime
    # fields in its own schema, not in period_closing_doctypes, posts no GL under its own name.
    "Asset Depreciation Schedule": None,
}


def _date_field_for(doctype):
    """The doctype's own transaction-date fieldname — the raw ERPNext field the broker's own
    closed-books check (:meth:`pacioli.erpnext.ErpnextClient.get_period_locks`'s ``posting_date``
    parameter) reads its value from, BEFORE that value is stored under the Plan's own
    ``posting_date`` attribute (an internal, doctype-agnostic name that never changes — see
    :func:`pacioli.plan.check_red_line`, which treats it as an opaque ISO-date value regardless of
    source field). SI/PI/PE/JE all carry a literal ``posting_date`` field. Sales Order does not —
    confirmed absent from ``sales_order.json`` (see :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s
    own comment block for the full breadth-increment finding) — its date field is
    ``transaction_date``. Defaults to ``"posting_date"`` for any doctype not explicitly configured
    in :data:`SUPPORTED_DOCTYPES`, including a 'generic' cascade node this broker has no
    descriptor for — ERPNext's overwhelmingly common convention for GL-posting doctypes, a
    best-effort default for the unmodeled case, not a verified pin like the explicit rows in
    :data:`SUPPORTED_DOCTYPES` (a count deliberately not restated here — it drifts every landing;
    see that dict itself).

    **May return ``None`` (BOM breadth, 2026-07-21):** an explicitly declared ``"date_field":
    None`` — the source-verified pin that the doctype carries NO date field at all (BOM, the
    first) — passes through as ``None``, distinct from the absent-key default above. Callers
    that need a document's date value go through :func:`_posting_date_of` (which maps the
    declared ``None`` to :data:`pacioli.plan.NO_DATE_FIELD`); callers that need a period-lock
    read go through :func:`_locks_for` (which skips the read for the sentinel). No call site
    feeds a ``None`` fieldname into ``doc.get`` or a bench query.

    **Graph-participant pins (Asset cascade live-prove, 2026-07-22).** A doctype this broker
    does NOT govern can still ride a ``plan_cascade_cancel`` graph as a dependent node — Asset
    Depreciation Schedule is auto-created and auto-submitted by Asset's own submit (the
    disclosed sibling factory), so EVERY submitted Asset with depreciation carries one, and the
    landed Asset row names cascade as its only governed cancel path. Under the unmodeled
    default above, that node read ``doc.get("posting_date")`` → ``""`` and the deny-biased
    non-ISO refusal killed every Asset cascade at preflight — the documented governed path
    could structurally never run (found live, lab CT 31340). :data:`GRAPH_NODE_DATE_FIELDS`
    carries the same source-verified declared-dateless pin for such nodes, checked AFTER the
    governed table (a governed row's own declaration always wins) and BEFORE the unmodeled
    default. The pin's three receipts match BOM's own dateless proof: absent from ERPNext's
    ``period_closing_doctypes`` (hooks.py's 18-entry list), no ``make_gl_entries`` anywhere in
    its class hierarchy (DepreciationScheduleController is calculation mixins, never
    AccountsController — the depreciation JEs are posted by the scheduler/depreciation path
    under their OWN names, never by the schedule), and ZERO Date/Datetime fields in its own
    schema (asset_depreciation_schedule.json — stronger than BOM, which at least carries
    non-posting dates). Deny-bias unchanged for every OTHER unmodeled node: no pin, no pass."""
    if doctype in SUPPORTED_DOCTYPES:
        return SUPPORTED_DOCTYPES[doctype].get("date_field", "posting_date")
    if doctype in GRAPH_NODE_DATE_FIELDS:
        return GRAPH_NODE_DATE_FIELDS[doctype]
    return "posting_date"


def _posting_date_of(doc, doctype):
    """The ONE way glue code reads a document's closed-books date (BOM breadth, 2026-07-21 —
    extracted so the four former inline ``doc.get(_date_field_for(doctype))`` sites cannot drift
    on the dateless branch). For a doctype whose :data:`SUPPORTED_DOCTYPES` entry explicitly
    declares ``"date_field": None`` (BOM — the declared-dateless pin, source-verified), returns
    :data:`pacioli.plan.NO_DATE_FIELD`: the sentinel ``check_red_line`` passes by its own branch
    and :func:`_locks_for` skips the period-lock read for. For every other doctype, exactly the
    prior behavior: the declared (or defaulted) field read off the doc, empty string when
    missing/falsy — which downstream stays REFUSED (``check_red_line``'s "no posting_date"
    refusal, ``get_period_locks``' non-ISO refusal): an empty read on a doctype that DOES declare
    a date field is unverifiable, never dateless. Datelessness is a property of the declared
    shape, not of the data.

    **The datetime→date projection (Work Order breadth, 2026-07-21).** ``planned_start_date`` is
    the first declared date_field whose fieldtype is **Datetime** — a frappe REST read returns
    ``"YYYY-MM-DD HH:MM:SS[.ffffff]"``, which fails the strict ISO-date shape every downstream
    consumer validates and would hard-deny every Work Order write if passed through raw. This
    ONE reader projects it to its date part — the same date-part semantics ERPNext's own
    ``getdate()`` applies wherever it needs a date from a datetime — under a deliberately narrow
    rule: truncate ONLY when the first 10 characters are a valid ISO date immediately followed
    by a ``" "`` or ``"T"`` separator. Anything else (a non-padded date, a mangled timestamp, a
    stray string) keeps its raw shape and stays REFUSED downstream with the actual value in the
    refusal message — the projection is a declared type's read rule, never a repair of malformed
    data. Inert for every Date-typed field (a 10-char read has no index 10) and for the
    sentinel."""
    date_field = _date_field_for(doctype)
    if date_field is None:
        return NO_DATE_FIELD
    value = str(doc.get(date_field) or "")
    if len(value) > 10 and value[10] in (" ", "T") and _is_iso_date(value[:10]):
        return value[:10]
    return value


def _locks_for(client, company, doctype, posting_date):
    """The ONE way glue code reads period locks (BOM breadth, 2026-07-21; the companyless-doctype
    guard below, 2026-07-21 live-prove fix). For the declared-dateless sentinel there is nothing
    to read: :meth:`get_period_locks`' Accounting Period query is RANGE-KEYED on the posting date
    (``start_date <= posting_date <= end_date``) — with no date there is no containing period to
    look for, and the frozen-till/PCV boundaries are date comparisons with the same nothing to
    compare. Returns ``{}`` (no locks apply), matching ERPNext exactly: a dateless doctype in
    this broker's set is never period-checked by ERPNext either (not in
    ``period_closing_doctypes``, posts no GL — the full three-way source proof lives on
    ``check_red_line``'s own dateless branch).

    **The companyless guard (shape-driven off ``company``, never a doctype list — the SAME
    architecture as the dateless guard above).** A falsy ``company`` (``None`` — the field is
    absent from the doctype's own schema entirely, e.g. Bank Guarantee/Project Update/Supplier
    Scorecard Period/Shipment/Asset Maintenance Log — OR ``""`` — a schema-present but non-``reqd``
    ``company`` field left blank on the draft, e.g. Asset Value Adjustment) also returns ``{}``
    before any network call: :meth:`get_period_locks`' own first line is
    ``self._doc_path("Company", company)``, which raises ``ErpnextError("a document name is
    required")`` for anything that is not a non-empty string (``erpnext.py``'s ``_doc_path``) — a
    **REAL crash on a live bench**, not a defensive guess (found by the 2026-07-21 live-prove
    batch: ``_tool_plan_submit -> _plan_closed_books_risk -> get_period_locks -> _doc_path("Company",
    None)``). Returning ``{}`` matches ERPNext exactly here too: every companyless doctype in this
    broker's set is confirmed absent from ``period_closing_doctypes`` (the same 18-entry list;
    none of them post GL either), so ERPNext itself never period-checks them regardless of company
    — there is no company to look up a lock against, and none would apply even if there were.
    **Reachability:** only under the documented UNPINNED registry posture — a company-PINNED
    target already refuses a companyless/blank-company document as "wrong books"
    (:meth:`PacioliBroker._tool_plan_submit`/``_tool_plan_cancel``/``_governed_write``'s TOCTOU
    belt, all nine call sites) before this function is ever reached with a falsy ``company``.

    Every other value goes straight through to :meth:`get_period_locks` unchanged, including the
    empty string ``posting_date`` (which refuses there, non-ISO — the standing deny-bias for an
    unreadable date on a dated doctype)."""
    if posting_date == NO_DATE_FIELD:
        return {}
    if not company:
        return {}
    return client.get_period_locks(company, doctype, posting_date)


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


def _update_stock_risk_flags(doc, op, doctype=None):
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
    this fix — the sign-aware text only appears when a negative-qty row is actually present.

    ``doctype`` (POS Invoice breadth, 2026-07-21) is OPTIONAL, mirroring :func:`_return_risk_flags`'s
    own precedent — ``None`` degrades to the doctype-agnostic wording above, never a crash. Gated
    explicitly on ``doctype == POS_INVOICE`` (a VERIFIED behavioral fact — a full-file grep of
    ``pos_invoice.py`` shows zero references to ``update_stock_ledger`` anywhere — not a shape
    inferred from field presence): the doctype-agnostic claim above ("the stock ledger is written
    alongside the GL on submit") is FALSE for POS Invoice specifically, whose own
    ``on_submit``/``on_cancel`` fully override ``SalesInvoice``'s without calling it. The real
    stock movement, if any, posts only later on the separate, consolidated Sales Invoice."""
    if not doc.get("update_stock"):
        return []
    if doctype == POS_INVOICE:
        if op == "submit":
            return [
                "update_stock is set on this POS Invoice, but its OWN submit does not write a "
                "Stock Ledger Entry — POSInvoice.on_submit fully overrides SalesInvoice.on_submit "
                "without calling it, and update_stock_ledger() is never called anywhere in "
                "pos_invoice.py; the physical stock movement, if any, is posted only later, when "
                "a POS Closing Entry consolidates this invoice into a separate Sales Invoice"]
        return [
            "update_stock was set on this POS Invoice, but cancelling it reverses no Stock Ledger "
            "Entry of its own — none was ever written at submit (see plan_submit's own disclosure "
            "for why); any real stock movement lives on the consolidated Sales Invoice, if one "
            "exists, and reverses only when THAT document is cancelled"]
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


def _return_risk_flags(doc, doctype=None):
    """Envelope E4 disclosure: ``is_return`` (credit note), doctype-agnostic by construction — SI
    and PI both carry the field, and (Delivery Note breadth, 2026-07-21; Purchase Receipt breadth,
    same day) so do both STOCK-PRIMARY doctypes. Read entirely from the draft's OWN header/items
    fields (no new bench
    read, the same source discipline as :func:`_update_stock_risk_flags`). Advisory only, never a
    gate — a no-op for any doc without a truthy ``is_return``. ``doctype`` is OPTIONAL (every
    existing call site now passes it; ``None`` degrades to the pre-Delivery-Note settlement wording
    below, never a crash, so a future caller that forgets it fails soft, not loud, on this one axis
    — deliberate, since the alternative is a hard requirement on a purely advisory helper).

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
    * A settlement flag when ``return_against`` IS set — THREE-WAY, not two (Delivery Note breadth
      widened this from the original two-way SI/PI-only shape):

      - ``"update_outstanding_for_self" not in doc`` (Delivery Note breadth, 2026-07-21, source-
        confirmed absent from ``delivery_note.json``'s full field list — a STOCK-ONLY return
        doctype with no receivable/payable balance of its own at all, Inventory/COGS GL only, per
        ``erpnext.py``'s SUPPORTED_DOCTYPES comment block; Purchase Receipt breadth, same day,
        confirmed absent from ``purchase_receipt.json``'s 148-field list too — the identical
        Stock/Asset-only shape, mirrored for the receiving side): the settlement sentences below are
        MEANINGLESS for it — there is no "outstanding" on this document to reduce or preserve. This
        branch is keyed on FIELD PRESENCE, never mere truthiness, because a real bench document
        omits a field its doctype's schema doesn't carry at all — ``.get()`` alone cannot tell
        "this doctype has no such concept" apart from "this doctype has the field, unset" (SI/PI's
        own default-off shape), and conflating the two would have silently kept emitting the FALSE
        "outstanding is reduced" claim below for Delivery Note the moment it landed. Names WHAT
        this return actually does instead (a stock reversal) and where a real credit note, if any,
        comes from (a separately auto-created, auto-submitted Sales Invoice).
      - ``doc.get("update_outstanding_for_self")`` truthy (SI/PI, the field present and set):
        "does NOT settle" — ERPNext's return mapper sets it by default, and then the return's
        receivable rows post against the RETURN ITSELF (the original's outstanding does not move;
        the credit sits on the return until a separate reconciliation allocates them).
      - Field present and falsy (SI/PI, the field present but cleared): "posts against X — the
        original's outstanding is reduced by this reversal".

      **A FOURTH shape (POS Invoice breadth, 2026-07-21), keyed explicitly on
      ``doctype == POS_INVOICE`` rather than field-presence alone** (unlike the DN/PR branch it
      sits beside, this is a VERIFIED, doctype-specific behavioral fact — POS Invoice DOES carry
      ``debit_to``, so inferring "no receivable field present" from schema shape alone would
      wrongly route it into the DN/PR branch): POS Invoice has no ``update_outstanding_for_self``
      field either, but — per the central finding in ``erpnext.py``'s own module docstring — posts
      NO GL entries of ANY kind on its own submit, return or not (``POSInvoice.on_submit`` fully
      overrides ``SalesInvoice.on_submit`` without calling it). The DN/PR wording ("STOCK reversal
      only... Inventory/COGS GL only") would be doubly wrong here: it both undersells a receivable
      field that genuinely exists AND implies an inventory GL posting that, per the central
      finding, never happens either. Named plainly instead.

      Consent to "a credit note against X" is not consent to "X is settled" — the memorandum must
      say which one this is, and (now) whether "settled" even applies to this doctype at all."""
    if not doc.get("is_return"):
        return []
    flags = [
        "this document is a RETURN (credit note) — money moves opposite a normal sale/purchase; "
        "the projected rows above are that reversal"
    ]
    if doc.get("return_against"):
        if "update_outstanding_for_self" not in doc and doctype == POS_INVOICE:
            flags.append(
                f"this return names {doc.get('return_against')} as return_against, but POS "
                "Invoice posts NO GL entries of its own on ANY submit (return or not) — "
                "POSInvoice.on_submit fully overrides SalesInvoice.on_submit without calling it, "
                "confirmed by zero make_gl_entries/update_stock_ledger references anywhere in "
                "pos_invoice.py; there is no update_outstanding_for_self field and nothing to "
                "settle on THIS document — the real accounting, and any actual settlement, "
                "happens only when a POS Closing Entry consolidates this invoice into a separate, "
                "genuinely-submitted Sales Invoice")
        elif "update_outstanding_for_self" not in doc:
            flags.append(
                f"this return posts against {doc.get('return_against')} as a STOCK reversal only "
                f"— {doctype or 'this doctype'} carries no receivable/payable balance of its own "
                "(Inventory/COGS GL only, when perpetual inventory is enabled) and has no "
                "update_outstanding_for_self field at all, so 'outstanding' does not apply to it; "
                "if a credit note is also needed, ERPNext creates and submits a SEPARATE Sales "
                "Invoice for it rather than settling anything on this document")
        elif doc.get("update_outstanding_for_self"):
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


def _pos_invoice_ledger_deferral_flag(doctype):
    """POS Invoice breadth (2026-07-21) — a genuinely NEW disclosure, not a doctype-agnostic
    envelope: fires unconditionally for ``doctype == POS_INVOICE`` (no doc field gates it — the
    divergence is a property of the DOCTYPE's own ``on_submit`` override, true for every POS
    Invoice regardless of its contents), submit-direction only.

    ``POSInvoice.on_submit()`` (``pos_invoice.py:240-263``) is a COMPLETE override of
    ``SalesInvoice.on_submit()`` that never calls ``super()`` — confirmed by a full-file grep of
    ``pos_invoice.py`` (1119 lines): zero references to ``make_gl_entries`` or
    ``update_stock_ledger`` anywhere. A real POS Invoice submit therefore posts NEITHER a GL Entry
    NOR a Stock Ledger Entry of its own; that accounting happens only later, on a separate,
    genuinely-submitted Sales Invoice a POS Closing Entry builds at consolidation.

    ERPNext's own ``ledger_preview`` RPC (``get_accounting_ledger_preview``,
    stock_controller.py:2090-2119) does NOT go through ``on_submit`` — it calls
    ``doc.make_gl_entries()`` as a bare method call. Since ``POSInvoice`` never overrides that
    method (only ``on_submit``), the call resolves via ordinary Python MRO to the INHERITED
    ``SalesInvoice.make_gl_entries``, which genuinely runs and returns real-shaped GL rows. So
    this broker's own ``plan_submit`` for a POS Invoice carries a non-empty ``projected_gl`` that
    will NEVER actually post for this voucher — a real simulation of a posting that is not coming.
    Without this flag, the plan's own "no debit without a credit" promise (projected_gl reflects
    what a submit will do) silently breaks for this one doctype.

    ``plan_cancel`` needs no equivalent flag: its ``projected_reversal`` comes from
    ``get_gl_entries(doctype, name)``, a REAL bench read of actually-posted rows, which correctly
    comes back empty for POS Invoice (nothing was ever posted to reverse) — the pre-existing
    generic "no live GL rows found for this voucher — nothing visible to unwind" flag already says
    so honestly, unchanged."""
    if doctype != POS_INVOICE:
        return []
    return [
        "PROJECTED GL ABOVE WILL NOT POST: POS Invoice's own submit never calls make_gl_entries() "
        "or update_stock_ledger() — on_submit fully overrides SalesInvoice.on_submit without "
        "calling it, so no GL Entry or Stock Ledger Entry is created for THIS document at all; the "
        "projected_gl shown here comes only from ERPNext's preview RPC calling make_gl_entries() "
        "directly (bypassing on_submit) — a real simulation, but of a posting that will never "
        "happen for this voucher. GL/SL truth is deferred until a POS Closing Entry consolidates "
        "this invoice into a separate, genuinely-submitted Sales Invoice — govern THAT document's "
        "own submit to see real postings"]


def _dunning_ledger_preview_unavailable_flag(doctype):
    """Dunning breadth (2026-07-21) — the opposite shape from
    :func:`_pos_invoice_ledger_deferral_flag`: POS Invoice's native preview SUCCEEDS but is
    misleading (non-empty projected_gl for a posting that will never happen); Dunning's native
    preview is UNCALLABLE outright. Fires unconditionally for ``doctype == DUNNING`` (a property of
    the doctype itself — true regardless of the document's own contents), submit-direction only.

    ``Dunning`` has no ``make_gl_entries`` method anywhere in its class hierarchy (``Dunning ->
    AccountsController -> TransactionBase -> StatusUpdater -> Document`` — none of these define
    it; the method exists only on ``StockController``, NOT an ancestor of ``AccountsController``,
    and on a short closed list of individual doctype controllers Dunning shares no ancestry with —
    see ``erpnext.py``'s own "Breadth (Dunning)" module-docstring section for the full MRO grep).
    ERPNext's own ``ledger_preview`` RPC (``get_accounting_ledger_preview``,
    ``stock_controller.py:2090-2119``) calls ``doc.make_gl_entries()`` as a BARE, unguarded method
    call — for a real Dunning this raises ``AttributeError`` on a live bench, which this broker's
    own transport taxonomy classifies as an answered refusal (``ErpnextError``, propagating to
    ``dispatch()``'s structured deny). Calling it would refuse EVERY Dunning ``plan_submit``
    outright, never actually planning one — so ``_tool_plan_submit`` skips the network call
    entirely for this doctype (see its own comment at the call site) rather than let a live bench
    500 stand in for a plan. ``projected_gl`` is therefore ``[]`` BY CONSTRUCTION, not because a
    preview ran and found nothing to post (the SO/PO/MR/SQ/Q shape, whose inherited
    ``StockController.make_gl_entries`` genuinely runs and conditionally no-ops) — a materially
    different reason this flag names plainly rather than leaving silent.

    ``plan_cancel`` needs no equivalent flag: its ``projected_reversal`` comes from
    ``get_gl_entries(doctype, name)``, a REAL, safe bench read of the ``GL Entry`` table (never
    calls ``make_gl_entries``), which correctly, safely comes back empty for Dunning (no GL row was
    ever written under this voucher_type) — the pre-existing generic "no live GL rows found for
    this voucher — nothing visible to unwind" flag already says so honestly, unchanged."""
    if doctype != DUNNING:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Dunning has no make_gl_entries method anywhere in its class hierarchy (it inherits "
        "from AccountsController, never StockController) — ERPNext's own preview RPC would call "
        "doc.make_gl_entries() as a bare method call and raise AttributeError on a live bench, so "
        "this broker does not call it for a Dunning at all. This is different from Sales "
        "Order/Purchase Order/Material Request/Supplier Quotation/Quotation, whose own inherited "
        "make_gl_entries genuinely runs and correctly finds nothing to post. Dunning's own submit "
        "posts no GL Entry regardless (grand_total is a computed display field, never a posting "
        "target) — any real GL impact from dunning fees/interest is posted later by a separate "
        "Payment Entry when this dunning is paid; govern THAT document's own submit to see real "
        "postings"]


def _stock_reconciliation_ledger_preview_incomplete_flag(doctype):
    """Stock Reconciliation breadth (2026-07-21) — a FIFTH ledger_preview shape, distinct from all
    four already landed: Dunning's preview is UNCALLABLE (AttributeError); POS Invoice's preview is
    callable and returns a real but MISLEADING NON-EMPTY projected_gl (a posting that will never
    happen); Sales/Purchase Order/Material Request/Supplier Quotation/Quotation's preview is
    callable and returns an HONESTLY empty projected_gl (a real submit of THOSE doctypes posts no
    GL either, so the emptiness matches reality); Delivery Note/Purchase Receipt/Stock Entry's
    preview is callable and returns a real, HONEST non-empty projected_gl. Stock Reconciliation's
    preview is callable, raises nothing, and returns an EMPTY projected_gl too — but the emptiness
    is DISHONEST: unlike SO/PO/MR/SQ/Q, a real Stock Reconciliation submit ALWAYS writes Stock
    Ledger Entry rows (``update_stock_ledger()`` is unconditional in ``on_submit``,
    ``stock_reconciliation.py:109-114`` — and the submit itself REFUSES outright,
    ``stock_reconciliation.py:811-816``, if no item row carries any difference at all, so a
    document that successfully plans a submit necessarily has real rows to write) and, whenever
    perpetual inventory is enabled for the company, ALSO writes real GL Entry rows from those same
    rows (the inherited ``StockController.make_gl_entries``, sourced via ``get_gl_entries``/
    ``get_stock_ledger_details``, a live query against the ``Stock Ledger Entry`` table filtered on
    THIS voucher).

    The reason the preview cannot see any of this: ERPNext's own ``get_accounting_ledger_preview``
    (``stock_controller.py:2090-2119``) only pre-seeds an in-memory ``update_stock_ledger()`` call
    for the THREE doctypes explicitly named in its whitelist tuple — ``("Purchase Receipt",
    "Delivery Note", "Stock Entry")``, line 2109 — before it calls ``doc.make_gl_entries()``. Stock
    Reconciliation is confirmed ABSENT from that tuple (direct string comparison) and carries no
    ``update_stock`` field either (confirmed absent from ``stock_reconciliation.json``'s 17-field
    list), so neither half of that line's ``or`` condition fires for it. ``doc.make_gl_entries()``
    is still called (line 2112, unconditionally, unguarded) — and IS callable, unlike Dunning
    (``StockReconciliation(StockController)``, ``stock_reconciliation.py:31`` — ``make_gl_entries``
    is inherited straight from ``StockController``, never overridden) — but with no SLE rows seeded
    for this voucher in the savepoint, ``StockReconciliation.get_gl_entries``
    (``stock_reconciliation.py:961-965``, which delegates to ``StockController.get_gl_entries``
    after checking ``self.cost_center``) queries ``get_stock_ledger_details()`` against the REAL
    ``Stock Ledger Entry`` table (``stock_controller.py:923+``, filtered by
    ``voucher_type``/``voucher_no``) and finds nothing — the voucher's own SLE rows do not exist
    yet, because they are only ever created by a REAL submit's ``update_stock_ledger()`` call, which
    the preview never makes for this doctype. ``get_voucher_details``
    (``stock_controller.py:871-887``) builds ITS OWN return value by iterating that (empty) SLE map
    for Stock Reconciliation specifically, so the loop in ``get_gl_entries`` never executes and
    ``process_gl_map([])`` returns ``[]`` cleanly — no exception, just a quiet false negative.

    This flag fires unconditionally for ``doctype == STOCK_RECONCILIATION`` (a property of the
    doctype's own preview-vs-submit mismatch, true regardless of the document's own contents),
    submit-direction only: ``plan_cancel``'s own ``projected_reversal`` comes from
    ``get_gl_entries(doctype, name)``, a REAL bench read of ACTUALLY-POSTED GL Entry rows (never
    ``make_gl_entries``), which is accurate for Stock Reconciliation exactly as it is for every
    other doctype — no equivalent fix needed there."""
    if doctype != STOCK_RECONCILIATION:
        return []
    return [
        "PROJECTED GL ABOVE MAY BE INCOMPLETE, NOT BECAUSE NOTHING WOULD POST: Stock "
        "Reconciliation is absent from ERPNext's own ledger-preview SLE-seeding whitelist "
        "(unlike Delivery Note/Purchase Receipt/Stock Entry), so this broker's preview never "
        "seeds the Stock Ledger Entry rows a real submit would write, and the resulting empty "
        "projected_gl here does NOT mean this submit posts nothing. A real submit of this "
        "document ALWAYS writes Stock Ledger Entry rows (unconditional — and refuses outright "
        "if no item carries any difference at all) and, whenever perpetual inventory is enabled "
        "for this company, ALSO writes real GL Entry rows sourced from those same rows — none "
        "of that appears above. This is different from Sales Order/Purchase Order/Material "
        "Request/Supplier Quotation/Quotation, whose own empty preview honestly reflects that a "
        "real submit posts nothing either"]


def _landed_cost_voucher_ledger_preview_unavailable_flag(doctype):
    """Landed Cost Voucher breadth (2026-07-21) — the Dunning "skip" shape (the native preview is
    UNCALLABLE, not merely non-posting), for a different structural reason and with a sharper
    consequence. Fires unconditionally for ``doctype == LANDED_COST_VOUCHER`` (a property of the
    doctype itself, true regardless of the document's own contents), submit-direction only.

    ``LandedCostVoucher`` (``landed_cost_voucher.py:22``) descends directly from frappe's base
    ``Document`` — never ``AccountsController`` (Dunning's own ancestor), never ``StockController``
    (every other "posts no GL of its own" doctype's ancestor) — and defines no ``make_gl_entries``
    anywhere (confirmed by a full-file grep). ERPNext's own ``ledger_preview`` RPC
    (``get_accounting_ledger_preview``, ``stock_controller.py:2090-2119``) calls
    ``doc.make_gl_entries()`` as a BARE, unguarded method call — for a real Landed Cost Voucher
    this raises ``AttributeError`` on a live bench, the identical mechanical finding Dunning's own
    landing made, so ``_tool_plan_submit`` skips the ``client.ledger_preview()`` network call
    entirely for this doctype too rather than let a live bench 500 refuse every plan.

    **The sharper point, genuinely new here: even a HYPOTHETICALLY callable preview would describe
    the WRONG document.** A Landed Cost Voucher never posts a GL Entry or Stock Ledger Entry under
    its own ``voucher_type`` — not because of an ``AttributeError``, but because its real economic
    effect is a REVALUATION of the receipt documents named in its own ``purchase_receipts`` table
    (see ``update_landed_cost()``, ``landed_cost_voucher.py:307-350``, and
    :func:`_landed_cost_voucher_cancel_revaluation_flag`'s own docstring for the cancel-side half).
    So unlike Dunning (whose empty ``projected_gl`` matches ground truth — Dunning posts nothing,
    period), this doctype's empty ``projected_gl`` is accurate about the LCV's own voucher and
    silent about the very real posting this submit is about to trigger on OTHER documents' ledgers.

    ``plan_cancel`` needs its OWN new flag, not the "no equivalent fix needed" treatment Dunning's
    landing gave — see :func:`_landed_cost_voucher_cancel_revaluation_flag`."""
    if doctype != LANDED_COST_VOUCHER:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN — AND EVEN A WORKING PREVIEW "
        "WOULD BE THE WRONG DOCUMENT: Landed Cost Voucher has no make_gl_entries method anywhere "
        "in its class hierarchy (it inherits directly from Document, never AccountsController or "
        "StockController) — ERPNext's own preview RPC would call doc.make_gl_entries() as a bare "
        "method call and raise AttributeError on a live bench, so this broker does not call it "
        "for a Landed Cost Voucher at all. But even a callable preview would only ever describe "
        "THIS voucher's own ledger, which is permanently empty by design — a Landed Cost Voucher "
        "never posts a GL Entry or Stock Ledger Entry under its own voucher_type. Its real "
        "effect on submit is a REVALUATION of the Purchase Receipt/Purchase Invoice/Stock Entry/"
        "Subcontracting Receipt documents named in its own purchase_receipts table: their item "
        "valuation_rate is recalculated upward and their existing Stock Ledger Entry + General "
        "Ledger Entry rows are reversed and reposted at the new, higher rate. Nothing about that "
        "reposting shows up here — govern the target receipt documents' own get/list calls "
        "before and after this submit to see the real ledger impact"]


def _landed_cost_voucher_cancel_revaluation_flag(doctype):
    """Landed Cost Voucher breadth (2026-07-21) — the cancel-side half of
    :func:`_landed_cost_voucher_ledger_preview_unavailable_flag`'s finding, and a deliberate
    divergence from Dunning's own precedent (which found its pre-existing "no live GL rows found —
    nothing visible to unwind" flag was already honest for the cancel direction and needed no
    addition). Fires unconditionally for ``doctype == LANDED_COST_VOUCHER`` (a property of the
    doctype itself), cancel-direction only.

    ``plan_cancel``'s ``projected_reversal`` comes from ``get_gl_entries("Landed Cost Voucher",
    name)`` — a REAL bench read of the ``GL Entry`` table, honestly empty, because a Landed Cost
    Voucher NEVER posts a row under its own voucher_type (see the submit-side flag's own
    docstring). For Dunning, that same honest emptiness is accurate in BOTH directions — cancelling
    a Dunning genuinely touches nothing else either. For a Landed Cost Voucher, it is NOT: cancelling
    THIS document re-triggers the identical ``update_landed_cost()`` routine its own submit ran
    (``landed_cost_voucher.py:294-296`` calls it again, with ``self.docstatus == 2``), which
    reverses the valuation it added to the Purchase Receipt/Purchase Invoice/Stock Entry/
    Subcontracting Receipt documents named in its own ``purchase_receipts`` table and reposts
    THEIR Stock Ledger Entry and General Ledger Entry rows at the lower, pre-LCV rate — a real,
    consequential rewrite of other documents' ledgers, invisible to a read scoped to this voucher's
    own name.

    This hazard is ALSO invisible to the submitted-linked-docs blast-radius check that already ran
    above in ``_tool_plan_cancel``: that walk finds documents that REFERENCE this Landed Cost
    Voucher (there are none — nothing carries a real Link to Landed Cost Voucher except its own
    amendment chain, confirmed by grepping the full v16 checkout), never documents THIS one is
    about to rewrite — a structurally different kind of relationship the Link-graph mechanism was
    never built to see, in either direction, for a doctype whose economic effect is outbound
    revaluation rather than inbound reference."""
    if doctype != LANDED_COST_VOUCHER:
        return []
    return [
        "NO LIVE GL ROWS UNDER THIS VOUCHER DOES NOT MEAN A SAFE CANCEL: a Landed Cost Voucher "
        "never posts a GL Entry or Stock Ledger Entry of its own (see the submit-side disclosure "
        "on this same doctype), so this read is honestly empty and always will be — but "
        "cancelling THIS document re-triggers the SAME revaluation routine its submit ran, which "
        "reverses the valuation it added to the Purchase Receipt/Purchase Invoice/Stock Entry/"
        "Subcontracting Receipt documents named in its own purchase_receipts table and reposts "
        "THEIR Stock Ledger Entry and General Ledger Entry rows at the lower, pre-LCV rate. This "
        "blast radius is also invisible to the submitted-linked-docs check above: that walk finds "
        "documents that REFERENCE this Landed Cost Voucher (there are none), not documents THIS "
        "one is about to rewrite. Govern the target receipt documents' own get/list calls before "
        "and after this cancel to see the real reversal"]


def _blanket_order_ledger_preview_unavailable_flag(doctype):
    """Blanket Order breadth (2026-07-21) — the Dunning "skip" shape (the native preview is
    UNCALLABLE, not merely non-posting), the SIMPLER of the two shapes this campaign has built for
    an uncallable preview (Dunning's own, not Landed Cost Voucher's dual-flag one — see below for
    why). Fires unconditionally for ``doctype == BLANKET_ORDER`` (a property of the doctype itself,
    true regardless of the document's own contents), submit-direction only.

    ``BlanketOrder`` (``blanket_order.py:15``) descends directly from frappe's base ``Document`` —
    never ``AccountsController`` (Dunning's own ancestor), never ``StockController`` (every
    "honest-empty" doctype's ancestor) — and defines no ``make_gl_entries`` anywhere (confirmed by a
    full-file grep of all 201 lines, and by the same full-tree ``def make_gl_entries`` grep
    Dunning's/Landed Cost Voucher's own landings ran: the method lives only on ``StockController``
    and a short, closed list of individual doctype controllers Blanket Order shares no ancestry
    with). ERPNext's own ``ledger_preview`` RPC (``get_accounting_ledger_preview``,
    ``stock_controller.py:2090-2119``) calls ``doc.make_gl_entries()`` as a BARE, unguarded method
    call — for a real Blanket Order this raises ``AttributeError`` on a live bench, so
    ``_tool_plan_submit`` skips the network call entirely for this doctype too (joining the skip
    tuple, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER)``) rather than let a live bench 500
    refuse every plan.

    **Unlike Landed Cost Voucher, this is the END of the finding, not the start of a second one:**
    Blanket Order has no ``update_landed_cost()``-shaped side effect that rewrites some OTHER
    submitted document's ledger — its own submit posts nothing, anywhere, ever, full stop. The one
    real thing a Blanket Order's own lifecycle touches (a REFERENCING Sales/Purchase Order's own
    submit/cancel recomputing THIS document's ``ordered_qty`` counters via inherited
    ``StockController.update_blanket_order()``) is an UPSTREAM effect, never triggered by this
    document's own cancel, and a Bin-style quantity counter, never a ledger row — so ``plan_cancel``
    needs no equivalent new flag, the same "no addition needed, the existing empty read is already
    honest" finding Dunning's own landing made (unlike Landed Cost Voucher, whose own cancel DOES
    re-trigger a real elsewhere-reversal and therefore needed
    :func:`_landed_cost_voucher_cancel_revaluation_flag`)."""
    if doctype != BLANKET_ORDER:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Blanket Order has no make_gl_entries method anywhere in its class hierarchy (it "
        "inherits directly from Document, never AccountsController or StockController) — "
        "ERPNext's own preview RPC would call doc.make_gl_entries() as a bare method call and "
        "raise AttributeError on a live bench, so this broker does not call it for a Blanket "
        "Order at all. Blanket Order's own submit posts no GL Entry or Stock Ledger Entry "
        "regardless — it is a cost-blind rate/qty template (no grand_total field exists on this "
        "doctype at all); the actual revenue/cost posting happens later, when a Sales Order or "
        "Purchase Order created FROM this Blanket Order is itself submitted, or when an invoice "
        "is raised against that order — govern THOSE documents' own submit to see real "
        "postings"]


def _job_card_ledger_preview_unavailable_flag(doctype):
    """Job Card breadth (2026-07-21) — the Dunning/Blanket Order "skip" shape (the native preview
    is UNCALLABLE, not merely non-posting), the SIMPLER of the two shapes this campaign has built
    for an uncallable preview (not Landed Cost Voucher's dual-flag one — see below for why). Fires
    unconditionally for ``doctype == JOB_CARD`` (a property of the doctype itself, true regardless
    of the document's own contents), submit-direction only.

    ``JobCard`` (``job_card.py:60``) descends directly from frappe's base ``Document`` — never
    ``AccountsController``, never ``StockController`` (the import block pulls only two EXCEPTION
    classes, ``QualityInspectionNotSubmittedError``/``QualityInspectionRejectedError``, from
    ``erpnext.controllers.stock_controller``, never the ``StockController`` class itself) — and
    defines no ``make_gl_entries`` anywhere (confirmed by a full-file grep of all 1875 lines, and
    by the same full-tree ``def make_gl_entries`` grep Dunning's/Landed Cost Voucher's/Blanket
    Order's own landings ran: the method lives only on ``StockController`` and a short, closed
    list of individual doctype controllers Job Card shares no ancestry with). ERPNext's own
    ``ledger_preview`` RPC (``get_accounting_ledger_preview``, ``stock_controller.py:2090-2119``)
    calls ``doc.make_gl_entries()`` as a BARE, unguarded method call — for a real Job Card this
    raises ``AttributeError`` on a live bench, so ``_tool_plan_submit`` skips the network call
    entirely for this doctype too (joining the skip tuple, now ``(DUNNING, LANDED_COST_VOUCHER,
    BLANKET_ORDER, JOB_CARD)``) rather than let a live bench 500 refuse every plan.

    **Like Blanket Order, this is the END of the finding, not the start of a second one:** Job Card
    has no ``update_landed_cost()``-shaped side effect that rewrites some OTHER submitted
    document's ledger — its own submit posts nothing, anywhere, ever, full stop. The two real
    things a Job Card's own lifecycle touches on EITHER submit or cancel (``update_work_order()``,
    recomputing the linked Work Order Operation's ``completed_qty``/``process_loss_qty``/
    ``pending_qty``; ``set_transferred_qty()``, recomputing this Job Card's own ``transferred_qty``/
    ``status`` from submitted Material-Transfer Stock Entries) are quantity/status counters, never
    a ledger row — so ``plan_cancel`` needs no equivalent new flag, the same "no addition needed,
    the existing empty read is already honest" finding Dunning's/Blanket Order's own landings made
    (unlike Landed Cost Voucher, whose own cancel DOES re-trigger a real elsewhere-reversal and
    therefore needed :func:`_landed_cost_voucher_cancel_revaluation_flag`)."""
    if doctype != JOB_CARD:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Job Card has no make_gl_entries method anywhere in its class hierarchy (it "
        "inherits directly from Document, never AccountsController or StockController) — "
        "ERPNext's own preview RPC would call doc.make_gl_entries() as a bare method call and "
        "raise AttributeError on a live bench, so this broker does not call it for a Job Card "
        "at all. Job Card's own submit posts no GL Entry or Stock Ledger Entry regardless — it "
        "is a shop-floor time/operation record (no grand_total field exists on this doctype at "
        "all); the actual cost/stock posting happens later, when a Stock Entry this Job Card's "
        "own make_stock_entry/make_stock_entry_for_semi_fg_item whitelist methods create is "
        "itself submitted — govern THAT document's own submit to see real postings"]


def _bom_ledger_preview_unavailable_flag(doctype):
    """BOM breadth (2026-07-21) — the Dunning/Blanket Order/Job Card "skip" shape (the native
    preview is UNCALLABLE, not merely non-posting), the simpler single-flag one, never LCV's
    dual-flag one (BOM has no side effect that rewrites some OTHER document's ledger — its
    ``update_cost`` whitelist method does, but that is an ungoverned side surface this broker
    builds no tool for, not a submit/cancel lifecycle effect — see the erpnext.py module
    docstring's side-surface caveat). Fires unconditionally for ``doctype == BOM`` (a property
    of the doctype itself), submit-direction only.

    ``BOM`` (``bom.py:104``) descends from frappe's ``WebsiteGenerator``
    (``website_generator.py:11``) — itself a direct ``Document`` subclass, a website-route mixin
    with nothing accounting-shaped — never ``AccountsController``, never ``StockController``,
    and no ``make_gl_entries`` exists anywhere in frappe itself (full-tree grep) or in
    ``bom.py``. ERPNext's own ``get_accounting_ledger_preview``
    (``stock_controller.py:2090-2119``) calls ``doc.make_gl_entries()`` as a BARE, unguarded
    method call — ``AttributeError`` on a live bench for a real BOM, so ``_tool_plan_submit``
    skips the network call entirely for this doctype too (joining the skip tuple, now
    ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM)``) rather than let a live
    bench 500 refuse every plan.

    Like Blanket Order and Job Card, this is the END of the finding: BOM's own submit posts
    nothing, anywhere, ever — ``total_cost`` is a read-only valuation snapshot stored on the
    document by ``calculate_cost()`` at validate time, never a GL or Stock Ledger row — so
    ``plan_cancel`` needs no equivalent new flag; ``get_gl_entries`` is a real, safe bench read
    that naturally, honestly returns empty for a BOM."""
    if doctype != BOM:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: BOM has no make_gl_entries method anywhere in its class hierarchy (it inherits "
        "from WebsiteGenerator, a bare Document subclass — never AccountsController or "
        "StockController) — ERPNext's own preview RPC would call doc.make_gl_entries() as a "
        "bare method call and raise AttributeError on a live bench, so this broker does not "
        "call it for a BOM at all. BOM's own submit posts no GL Entry or Stock Ledger Entry "
        "regardless — it is a product recipe/structure, not a transaction (total_cost is a "
        "stored valuation snapshot, not a posting); submitting it activates the recipe "
        "(is_active/is_default bookkeeping on the BOM and its Item's default_bom pointer), and "
        "the real cost/stock postings happen later, on the Work Orders / Stock Entries built "
        "FROM this BOM — govern those documents' own submits to see real postings"]


def _work_order_ledger_preview_unavailable_flag(doctype):
    """Work Order breadth (2026-07-21) — the Dunning/Blanket Order/Job Card/BOM "skip" shape
    (the native preview is UNCALLABLE, not merely non-posting), the simpler single-flag one.
    Fires unconditionally for ``doctype == WORK_ORDER``, submit-direction only.

    ``WorkOrder`` (``work_order.py:70``) descends directly from frappe's base ``Document`` —
    never ``AccountsController``, never ``StockController`` — and defines no ``make_gl_entries``
    anywhere in its 3114 lines (grep; the dossier's own §5 hedged "empty list OR AttributeError"
    — the MRO settles it as ``AttributeError``, the skip category). ERPNext's own
    ``get_accounting_ledger_preview`` (``stock_controller.py:2090-2119``) calls
    ``doc.make_gl_entries()`` bare and unguarded — a live-bench 500 for a Work Order, so
    ``_tool_plan_submit`` skips the network call entirely (joining the skip tuple, now
    ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER)``).

    Like every prior member but LCV, this is the END of the finding: a Work Order's own
    submit/cancel lifecycle posts nothing anywhere (the on_submit chain is fulfillment counters,
    reservations, and an auto-created Job Card — never a ledger row), so ``plan_cancel`` needs
    no equivalent new flag; ``get_gl_entries`` honestly returns empty for a Work Order."""
    if doctype != WORK_ORDER:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Work Order has no make_gl_entries method anywhere in its class hierarchy (it "
        "inherits directly from Document, never AccountsController or StockController) — "
        "ERPNext's own preview RPC would call doc.make_gl_entries() as a bare method call and "
        "raise AttributeError on a live bench, so this broker does not call it for a Work "
        "Order at all. Work Order's own submit posts no GL Entry or Stock Ledger Entry "
        "regardless — it is a manufacturing order (no grand_total field exists on this doctype "
        "at all); the real cost/stock postings happen later, on the Stock Entries and Job "
        "Cards executed against this order — govern THOSE documents' own submits to see real "
        "postings"]


def _asset_submit_risk_flags(doc, now_date):
    """Asset breadth (2026-07-21) — the submit-direction disclosures for the first doctype with
    SCHEDULED, ASYNCHRONOUS GL posting. Doctype-scoping lives at the call site (fired only for
    ``doctype == ASSET``); every flag below is data-driven off the draft's own fields, never a
    blanket warning. Source pins for each condition live in ``erpnext.py``'s module docstring
    (asset.py's ``on_submit``/``make_gl_entries``/``validate_make_gl_entry``, plus ``hooks.py``'s
    daily scheduler list — ``post_depreciation_entries`` and ``make_post_gl_entry``).

    Unlike Dunning/LCV/Blanket Order/Job Card/BOM/Work Order, Asset's native ledger preview is
    CALLABLE (a real ``make_gl_entries``, ``asset.py:924``) — Asset does NOT join the skip
    tuple, so ``projected_gl`` here is ERPNext's own draft rows. What these flags add is the
    part no preview can show: the postings that happen LATER, outside any consent marker this
    broker ever minted, and the conditions under which the preview's emptiness is a deferral
    rather than a genuine no-op."""
    flags = []
    # (1) The auto-created sibling — unconditional: on_submit's make_asset_movement() creates
    # AND SUBMITS an Asset Movement capturing location/custodian. This is also WHY a later
    # plan_cancel on this asset refuses at the blast-radius gate and routes through
    # plan_cascade_cancel (see _asset_cancel_risk_flags).
    flags.append(
        "submitting this Asset auto-creates AND submits an Asset Movement (the receipt "
        "movement, capturing location/custodian) — a sibling submitted document riding this "
        "same consent; it will also appear as a submitted linked document blocking any later "
        "leaf-node plan_cancel of this asset, making plan_cascade_cancel the governed cancel "
        "path")
    # (2) The depreciation channel — fires only when the draft opts in.
    if doc.get("calculate_depreciation"):
        flags.append(
            "calculate_depreciation is set: submit ACTIVATES this asset's depreciation "
            "schedules, and the depreciation Journal Entries are then created and auto-"
            "submitted by ERPNext's own daily scheduler (post_depreciation_entries) — hours or "
            "days later, outside this broker's consent entirely. This broker governs the "
            "submit that arms the schedule, never the scheduled postings themselves — a scope "
            "boundary, disclosed not hidden")
    # (3) The deferred-CWIP channel — fires only for a future available_for_use_date. The
    # generic "posting_date is in the future" flag fires alongside from the same comparison;
    # this one names what that future date MEANS for an Asset specifically.
    available = str(doc.get("available_for_use_date") or "")
    if available and available > now_date:
        flags.append(
            "available_for_use_date is in the future: submit will post NO GL now "
            "(make_gl_entries' own date condition) — for a CWIP-enabled asset category, "
            "ERPNext's daily make_post_gl_entry scheduler posts the CWIP-to-fixed-asset "
            "transfer on the day the date arrives, again outside this broker's consent; the "
            "projected_gl above is what WOULD post, not what will post at this submit")
    # (4) The no-purchase-document case — validate_make_gl_entry returns False at submit, so
    # nothing posts even though the native preview (which skips that gate) may have drafted
    # rows. Composite Assets are exempt from the purchase-document requirement (their GL comes
    # from capitalization); Composite Components never post directly.
    if (doc.get("asset_type") not in ("Composite Asset", "Composite Component")
            and not doc.get("purchase_receipt") and not doc.get("purchase_invoice")):
        flags.append(
            "no purchase document (Purchase Receipt/Purchase Invoice) is linked: ERPNext's "
            "validate_make_gl_entry returns False at submit, so this submit posts NO GL "
            "regardless of the projected rows above — the asset is recorded without a "
            "CWIP-to-fixed-asset transfer")
    if doc.get("asset_type") == "Composite Component":
        flags.append(
            "asset_type is Composite Component: this asset never posts GL directly on submit "
            "(the parent Composite Asset's capitalization carries the posting)")
    return flags


def _asset_cancel_risk_flags(doc):
    """Asset breadth (2026-07-21) — the cancel-direction disclosures. Doctype-scoped at the call
    site; data-driven off the draft's own ``status``/``calculate_depreciation`` fields.

    The status gate is disclosed HERE, at plan time, because unlike Work Order's Stopped (a
    state reachable only via an out-of-surface RPC, invisible until execute), an Asset's
    ``status`` is on the document being planned — ``validate_cancellation``
    (``asset.py:728-736``) refuses In Maintenance/Out of Order outright and refuses everything
    outside Submitted/Partially Depreciated/Fully Depreciated (a Sold/Scrapped/Capitalized
    asset can never be cancelled), so a doomed cancel can be named before a marker is ever
    minted. Disclosure only — the refusal itself stays ERPNext's own answered throw at execute,
    honored never bypassed (and TOCTOU-honest: status can change between plan and execute; the
    bench gate is what actually protects)."""
    flags = []
    status = str(doc.get("status") or "")
    if status in ("In Maintenance", "Out of Order"):
        flags.append(
            f"status is {status!r}: ERPNext's validate_cancellation will REFUSE this cancel "
            "(active maintenance/repairs must be completed first) — this plan can be minted "
            "but the cancel will be refused at the bench unless the status changes")
    elif status and status not in ("Submitted", "Partially Depreciated", "Fully Depreciated"):
        flags.append(
            f"status is {status!r}: ERPNext's validate_cancellation only allows cancelling an "
            "asset whose status is Submitted, Partially Depreciated, or Fully Depreciated — "
            "this cancel will be refused at the bench unless the status changes")
    flags.append(
        "cancelling an Asset is a multi-document unwind riding one consent: ERPNext's own "
        "on_cancel auto-cancels this asset's Asset Movements and depreciation schedules, "
        "cancels every depreciation Journal Entry posted for it, and reverses its GL — the "
        "leaf-node plan_cancel will refuse on those same submitted links first (the asset's "
        "own receipt movement exists from the moment it was submitted), so "
        "plan_cascade_cancel is the governed path: the whole unwind graph, named and "
        "consented, instead of a single-document consent silently cancelling N documents")
    return flags


def _packing_slip_ledger_preview_unavailable_flag(doctype):
    """Packing Slip breadth (2026-07-21) — the Dunning/Blanket Order/Job Card/BOM/Work Order
    "skip" shape (the native preview is UNCALLABLE, not merely non-posting), the simpler
    single-flag one. Fires unconditionally for ``doctype == PACKING_SLIP``, submit-direction
    only.

    ``PackingSlip`` (``packing_slip.py:13``) descends from ``StatusUpdater``
    (``status_updater.py:181``), itself a direct ``frappe.model.document.Document`` subclass
    (confirmed from its own import line) — never ``AccountsController``, never
    ``StockController`` — and a full-file grep of ``status_updater.py`` finds no
    ``make_gl_entries``/``make_sl_entries``/``GLEntry`` reference anywhere. ERPNext's own
    ``get_accounting_ledger_preview`` calls ``doc.make_gl_entries()`` bare and unguarded —
    ``AttributeError`` on a live bench for a real Packing Slip, so ``_tool_plan_submit`` skips
    the network call entirely for this doctype too (joining the skip tuple, now ``(DUNNING,
    LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP)``).

    Like every prior member but LCV, this is the END of the finding: Packing Slip's own
    ``on_submit``/``on_cancel`` both call ``update_prevdoc_status()``
    (``status_updater.py:193-195``), which recomputes ``packed_qty`` on the linked Delivery Note
    Item / Packed Item via a raw ``frappe.db.sql`` ``UPDATE`` (``status_updater.py:549-602``) —
    a quantity COUNTER on another document, never a GL or Stock Ledger row — so ``plan_cancel``
    needs no equivalent new flag; ``get_gl_entries`` honestly returns empty for a Packing Slip."""
    if doctype != PACKING_SLIP:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Packing Slip has no make_gl_entries method anywhere in its class hierarchy (it "
        "inherits from StatusUpdater, itself a bare Document subclass — never "
        "AccountsController or StockController) — ERPNext's own preview RPC would call "
        "doc.make_gl_entries() as a bare method call and raise AttributeError on a live bench, "
        "so this broker does not call it for a Packing Slip at all. Packing Slip's own submit "
        "posts no GL Entry or Stock Ledger Entry regardless — it is a shipment-packing record "
        "(no grand_total field exists on this doctype at all); on_submit/on_cancel instead "
        "rewrite the packed_qty COUNTER on the linked Delivery Note Item / Packed Item via a "
        "raw SQL update — govern the Delivery Note's own submit to see real postings"]


def _cost_center_allocation_ledger_preview_unavailable_flag(doctype):
    """Cost Center Allocation breadth (2026-07-21) — the Dunning/Blanket Order/Job Card/BOM/Work
    Order/Packing Slip "skip" shape (the native preview is UNCALLABLE, not merely non-posting),
    the simpler single-flag one. Fires unconditionally for ``doctype ==
    COST_CENTER_ALLOCATION``, submit-direction only.

    ``CostCenterAllocation`` (``cost_center_allocation.py:30``) is a **direct**
    ``frappe.model.document.Document`` subclass (confirmed from its own import line and class
    statement) — never ``AccountsController``, never ``StockController`` — and a full-file grep
    of ``cost_center_allocation.py`` finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry``
    reference anywhere. ERPNext's own ``get_accounting_ledger_preview`` calls
    ``doc.make_gl_entries()`` bare and unguarded — ``AttributeError`` on a live bench for a real
    Cost Center Allocation, so ``_tool_plan_submit`` skips the network call entirely for this
    doctype too (joining the skip tuple, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER,
    JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION)``).

    Unlike every prior member of this skip category (each of which carries SOME on_submit/
    on_cancel side effect, even if never a ledger row), this is the simplest lifecycle this
    campaign has found: Cost Center Allocation defines NO ``on_submit``/``on_cancel`` hook of any
    kind (confirmed by reading all 160 lines of ``cost_center_allocation.py`` — only
    ``__init__``/``validate``/``clear_cache`` exist), so ``plan_cancel`` needs no equivalent new
    flag either; ``get_gl_entries`` honestly returns empty for a Cost Center Allocation."""
    if doctype != COST_CENTER_ALLOCATION:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Cost Center Allocation has no make_gl_entries method anywhere in its class "
        "hierarchy (it is a direct Document subclass — never AccountsController or "
        "StockController) — ERPNext's own preview RPC would call doc.make_gl_entries() as a "
        "bare method call and raise AttributeError on a live bench, so this broker does not "
        "call it for a Cost Center Allocation at all. This doctype's own submit posts no GL "
        "Entry or Stock Ledger Entry regardless — it is a recurring-allocation RULE, not a "
        "transaction (no grand_total field exists on this doctype at all); submit/cancel define "
        "no on_submit/on_cancel hook of any kind, so nothing beyond docstatus itself changes — "
        "govern the transactions that reference this cost center to see real postings"]


def _supplier_scorecard_period_ledger_preview_unavailable_flag(doctype):
    """Supplier Scorecard Period breadth (2026-07-21) — the Dunning/Blanket Order/Job Card/BOM/
    Work Order/Packing Slip/Cost Center Allocation "skip" shape (the native preview is
    UNCALLABLE, not merely non-posting), the simpler single-flag one. Fires unconditionally for
    ``doctype == SUPPLIER_SCORECARD_PERIOD``, submit-direction only.

    ``SupplierScorecardPeriod`` (``supplier_scorecard_period.py:16``) is a **direct**
    ``frappe.model.document.Document`` subclass (confirmed from its own import line and class
    statement) — never ``AccountsController``, never ``StockController`` — and a full-file grep
    of ``supplier_scorecard_period.py`` finds no ``make_gl_entries``/``make_sl_entries``/
    ``GLEntry`` reference anywhere. ERPNext's own ``get_accounting_ledger_preview`` calls
    ``doc.make_gl_entries()`` bare and unguarded — ``AttributeError`` on a live bench for a real
    Supplier Scorecard Period, so ``_tool_plan_submit`` skips the network call entirely for this
    doctype too (joining the skip tuple, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER,
    JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION,
    SUPPLIER_SCORECARD_PERIOD)``).

    Like Cost Center Allocation, this is the simplest lifecycle shape this campaign has found:
    Supplier Scorecard Period defines NO ``on_submit``/``on_cancel`` hook of any kind (confirmed
    by reading all 161 lines of ``supplier_scorecard_period.py`` — only ``validate`` and its own
    six helpers exist), so ``plan_cancel`` needs no equivalent new flag either;
    ``get_gl_entries`` honestly returns empty for a Supplier Scorecard Period."""
    if doctype != SUPPLIER_SCORECARD_PERIOD:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Supplier Scorecard Period has no make_gl_entries method anywhere in its class "
        "hierarchy (it is a direct Document subclass — never AccountsController or "
        "StockController) — ERPNext's own preview RPC would call doc.make_gl_entries() as a "
        "bare method call and raise AttributeError on a live bench, so this broker does not "
        "call it for a Supplier Scorecard Period at all. This doctype's own submit posts no GL "
        "Entry or Stock Ledger Entry regardless — it is a scored supplier-evaluation record, not "
        "a transaction (no grand_total field exists on this doctype at all, only a Percent "
        "total_score); submit/cancel define no on_submit/on_cancel hook of any kind, so nothing "
        "beyond docstatus itself changes — this doctype is also ordinarily machine-generated "
        "and machine-submitted by its own parent Supplier Scorecard doctype, never by this "
        "broker (see the module docstring's own caveat)"]


def _quality_inspection_ledger_preview_unavailable_flag(doctype):
    """Quality Inspection breadth (2026-07-21) — the Dunning/Blanket Order/Job Card/BOM/Work
    Order/Packing Slip/Cost Center Allocation/Supplier Scorecard Period "skip" shape (the native
    preview is UNCALLABLE, not merely non-posting), the simpler single-flag one. Fires
    unconditionally for ``doctype == QUALITY_INSPECTION``, submit-direction only.

    ``QualityInspection`` (``quality_inspection.py:20``) is a **direct**
    ``frappe.model.document.Document`` subclass (confirmed from its own import line and class
    statement) — never ``AccountsController``, never ``StockController`` — and a full-tree grep
    for ``def make_gl_entries`` across BOTH the erpnext-16 AND frappe-16 checkouts (the widest MRO
    sweep this campaign has run, not merely erpnext's own tree) finds it defined only on
    ``StockController`` and nine individual controllers, none of which ``QualityInspection``
    shares ancestry with. ERPNext's own ``get_accounting_ledger_preview`` calls
    ``doc.make_gl_entries()`` bare and unguarded — ``AttributeError`` on a live bench for a real
    Quality Inspection, so ``_tool_plan_submit`` skips the network call entirely for this doctype
    too (joining the skip tuple, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD,
    BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
    QUALITY_INSPECTION)``).

    Unlike Cost Center Allocation/Supplier Scorecard Period, Quality Inspection is NOT the
    simplest lifecycle shape — it defines real ``on_submit``/``on_cancel``/``before_submit``
    hooks (see :func:`_quality_inspection_submit_risk_flags` and
    :func:`_quality_inspection_cancel_risk_flags`, both doctype-scoped and data-driven, for what
    those hooks actually do). ``plan_cancel`` needs no equivalent PREVIEW flag beyond the cancel
    risk-flags function above — ``get_gl_entries`` honestly returns empty for a Quality
    Inspection (it never posts GL of any kind, regardless of what its own cancel writes into a
    reference document)."""
    if doctype != QUALITY_INSPECTION:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Quality Inspection has no make_gl_entries method anywhere in its class hierarchy "
        "(it is a direct Document subclass — never AccountsController or StockController) — "
        "ERPNext's own preview RPC would call doc.make_gl_entries() as a bare method call and "
        "raise AttributeError on a live bench, so this broker does not call it for a Quality "
        "Inspection at all. This doctype's own submit posts no GL Entry or Stock Ledger Entry "
        "regardless — it is a pure control/quality record (no grand_total field exists on this "
        "doctype at all); on_submit/on_cancel instead read/write a reference document's own "
        "quality_inspection link field — see this plan's other risk flags for what that write "
        "actually touches"]


def _quality_inspection_submit_risk_flags(doc):
    """Quality Inspection breadth (2026-07-21) — the submit-direction disclosure for
    ``before_submit``'s own gate (``quality_inspection.py:152-153`` ->
    ``validate_readings_status_mandatory``, 210-213), a genuine dossier omission: the dossier's
    own §4 ("Submit via") and §6 ("on_submit side effects") both describe only the run_method/
    on_submit shape and never mention this EARLIER hook. ``before_submit`` runs before
    ``on_submit`` on every real submit and throws (``"Row #{idx}: Status is mandatory"``) the
    first time it finds a ``readings`` row with no ``status`` value set — a doomed submit,
    readable off the draft's own ``readings`` child rows before a marker is ever minted, the same
    "status gate readable on the draft" shape Asset's own ``validate_cancellation`` disclosure
    established (there on cancel; here on submit). Doctype-scoped at the call site; data-driven
    off ``doc["readings"]``, never a new bench read — the same source discipline as
    :func:`_update_stock_risk_flags`'s own ``items`` read."""
    rows = [r for r in (doc.get("readings") or []) if isinstance(r, dict)]
    missing = [r.get("idx") for r in rows if not r.get("status")]
    if not missing:
        return []
    shown = ", ".join(str(i) for i in missing[:5] if i is not None)
    if not shown:
        shown = f"{len(missing)} row(s)"
    more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
    noun = "row" if len(missing) == 1 else "rows"
    return [
        f"submit will be REFUSED: readings {noun} #{shown}{more} with no status value — "
        "before_submit's validate_readings_status_mandatory throws 'Row #N: Status is "
        "mandatory' the first time it finds one, before on_submit ever runs"]


def _quality_inspection_cancel_risk_flags(doc):
    """Quality Inspection breadth (2026-07-21) — the cancel-direction disclosure for
    ``update_qc_reference()`` (``quality_inspection.py:215-262``), called UNCONDITIONALLY from
    ``on_cancel`` (202-205). Doctype-scoped at the call site; data-driven off the draft's own
    ``reference_type``/``reference_name`` fields (both ``reqd: 1`` on any real Quality
    Inspection, so this fires for every real draft reaching cancel).

    THE central dossier correction (§7), read line by line: ``update_qc_reference``'s own
    ``reference_type == "Job Card"`` branch (218-227) runs a raw ``frappe.db.sql`` ``UPDATE``
    directly against the Job Card doctype's OWN TOP-LEVEL row — Job Card carries no child-item
    structure for this field, so there is no child row to touch at all, unlike the dossier's own
    "removes the QI link from the reference document's child row" framing, which is right for the
    other six ``reference_type`` options (a ``frappe.qb`` ``UPDATE`` against a child table row —
    Purchase Receipt Item/Delivery Note Item/Stock Entry Detail/etc.) but wrong for this one.
    Either shape bypasses the reference document's own ``validate``/``on_update`` hooks and
    version history entirely (never ``doc.save()``) and checks neither this Quality Inspection's
    own docstatus meaning nor the reference document's.

    The standing blast-radius check (``get_submitted_linked_docs``, run before this function is
    ever reached) already refuses this cancel first whenever the reference document is CURRENTLY
    submitted (Job Card's own ``quality_inspection`` Link field, or a child-table Link promoted to
    its submitted parent for the other six reference types) — the same "rarely reached through
    this tool" shape Asset's own multi-document unwind carries (see
    :func:`_asset_cancel_risk_flags`). An ALREADY-CANCELLED reference document (docstatus 2,
    invisible to that same blast-radius check) is NOT protected the same way — this flag names
    that plainly rather than let the write pass silently."""
    reference_type = str(doc.get("reference_type") or "")
    reference_name = str(doc.get("reference_name") or "")
    if not reference_type or not reference_name:
        return []
    if reference_type == "Job Card":
        return [
            f"cancelling this Quality Inspection calls update_qc_reference(), which runs a raw "
            f"SQL UPDATE directly against the TOP-LEVEL row of Job Card {reference_name!r} (no "
            "child table exists for this reference_type) — bypassing that Job Card's own "
            "validate/on_update hooks and version history entirely. The standing blast-radius "
            "check above already refuses this cancel if that Job Card is currently submitted; "
            "an ALREADY-CANCELLED Job Card is not protected the same way and will still be "
            "silently rewritten"]
    return [
        f"cancelling this Quality Inspection calls update_qc_reference(), which runs a raw SQL "
        f"UPDATE clearing the quality_inspection link on the {reference_type} Item (or Stock "
        f"Entry Detail) child row under {reference_name!r} — bypassing that document's own "
        "validate/on_update hooks and version history entirely. The standing blast-radius check "
        "above already refuses this cancel if that document is currently submitted; an "
        "ALREADY-CANCELLED reference document is not protected the same way and will still be "
        "silently rewritten"]


def _installation_note_ledger_preview_unavailable_flag(doctype):
    """Installation Note breadth (2026-07-21) — the Dunning/Blanket Order/Job Card/BOM/Work
    Order/Packing Slip/Cost Center Allocation/Supplier Scorecard Period/Quality Inspection "skip"
    shape (the native preview is UNCALLABLE, not merely non-posting), the simpler single-flag
    one. Fires unconditionally for ``doctype == INSTALLATION_NOTE``, submit-direction only.

    ``InstallationNote`` (``installation_note.py:13``) descends from ``TransactionBase``
    (``transaction_base.py:20``), which itself descends from ``StatusUpdater``
    (``status_updater.py:181``) — a DEEPER MRO than any prior skip-tuple member (Packing Slip's
    own ``StatusUpdater(Document)`` is only ONE level above ``Document``; this is two), but the
    conclusion is identical: a full-file grep of BOTH ``transaction_base.py`` and
    ``status_updater.py`` finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry`` reference
    anywhere in either file. ERPNext's own ``get_accounting_ledger_preview`` calls
    ``doc.make_gl_entries()`` bare and unguarded — ``AttributeError`` on a live bench for a real
    Installation Note, so ``_tool_plan_submit`` skips the network call entirely for this doctype
    too (joining the skip tuple, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD,
    BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
    QUALITY_INSPECTION, INSTALLATION_NOTE)``).

    Like Packing Slip, this is the END of the finding: Installation Note's own ``on_submit``/
    ``on_cancel`` both call ``update_prevdoc_status()`` (``status_updater.py:193-195``), which
    recomputes ``installed_qty``/``per_installed`` on the linked Delivery Note Item/Delivery Note
    via a raw ``frappe.db.sql`` ``UPDATE`` (``status_updater.py:549-602``) — a quantity COUNTER on
    another document, never a GL or Stock Ledger row — so ``plan_cancel`` needs no equivalent new
    flag; ``get_gl_entries`` honestly returns empty for an Installation Note."""
    if doctype != INSTALLATION_NOTE:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Installation Note has no make_gl_entries method anywhere in its class hierarchy "
        "(InstallationNote -> TransactionBase -> StatusUpdater -> Document — never "
        "AccountsController or StockController) — ERPNext's own preview RPC would call "
        "doc.make_gl_entries() as a bare method call and raise AttributeError on a live bench, "
        "so this broker does not call it for an Installation Note at all. Installation Note's "
        "own submit posts no GL Entry or Stock Ledger Entry regardless — it is a fulfillment "
        "record (no grand_total field exists on this doctype at all); on_submit/on_cancel "
        "instead rewrite the installed_qty/per_installed COUNTER on the linked Delivery Note "
        "Item/Delivery Note via a raw SQL update — govern the Delivery Note's own submit to see "
        "real postings"]


def _shipment_ledger_preview_unavailable_flag(doctype):
    """Shipment breadth (2026-07-21) — the Dunning/Blanket Order/Job Card/BOM/Work Order/Packing
    Slip/Cost Center Allocation/Supplier Scorecard Period/Quality Inspection/Installation Note
    "skip" shape (the native preview is UNCALLABLE, not merely non-posting), this time the
    SIMPLEST MRO in the category. Fires unconditionally for ``doctype == SHIPMENT``,
    submit-direction only.

    ``class Shipment(Document)`` (``shipment.py:14``) — a direct ``Document`` subclass, tied with
    Job Card's/Work Order's/Blanket Order's own bare-``Document`` shape. A full-file grep of
    ``shipment.py`` finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry`` reference
    anywhere, and ``Document`` itself carries none either. ERPNext's own
    ``get_accounting_ledger_preview`` calls ``doc.make_gl_entries()`` bare and unguarded —
    ``AttributeError`` on a live bench for a real Shipment, so ``_tool_plan_submit`` skips the
    network call entirely for this doctype too (joining the skip tuple, now ``(DUNNING,
    LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
    SHIPMENT)``).

    This is the END of the finding: Shipment's own ``on_submit``/``on_cancel`` post no GL/SL row
    of any kind — ``on_submit`` only validates (parcel table non-empty, ``value_of_goods`` non-
    zero) then ``db_set``s ``status``; ``on_cancel`` only ``db_set``s ``status`` — so
    ``plan_cancel`` needs no equivalent new flag; ``get_gl_entries`` honestly returns empty for a
    Shipment."""
    if doctype != SHIPMENT:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Shipment has no make_gl_entries method anywhere in its class hierarchy "
        "(Shipment -> Document — never AccountsController or StockController) — ERPNext's own "
        "preview RPC would call doc.make_gl_entries() as a bare method call and raise "
        "AttributeError on a live bench, so this broker does not call it for a Shipment at all. "
        "Shipment's own submit posts no GL Entry or Stock Ledger Entry regardless — it is a pure "
        "logistics/carrier record (no grand_total field exists on this doctype at all; "
        "shipment_amount is a Currency field but is never referenced anywhere in the doctype's "
        "own code) — govern the linked Delivery Note's own submit to see real postings"]


def _sales_forecast_ledger_preview_unavailable_flag(doctype):
    """Sales Forecast breadth (2026-07-21) — the Dunning/Blanket Order/Job Card/BOM/Work Order/
    Packing Slip/Cost Center Allocation/Supplier Scorecard Period/Quality Inspection/Installation
    Note/Shipment "skip" shape (the native preview is UNCALLABLE, not merely non-posting), and the
    CLEANEST case in it yet. Fires unconditionally for ``doctype == SALES_FORECAST``,
    submit-direction only.

    ``class SalesForecast(Document):`` (``sales_forecast.py:11``) — a direct ``Document``
    subclass. Unlike every prior "uncallable" member (even Job Card's/Shipment's own bare-
    ``Document`` shape, which still import at least an exception class from ``stock_controller``),
    Sales Forecast's own import block (``:4-8``) pulls in ONLY ``frappe``, ``frappe._``,
    ``frappe.model.document.Document``, ``frappe.model.mapper.get_mapped_doc``, and
    ``frappe.utils.add_to_date`` — zero accounting/stock-controller-related imports of any kind. A
    full-file grep finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry``/
    ``StockLedgerEntry`` reference anywhere. ERPNext's own ``get_accounting_ledger_preview`` calls
    ``doc.make_gl_entries()`` bare and unguarded — ``AttributeError`` on a live bench for a real
    Sales Forecast, so ``_tool_plan_submit`` skips the network call entirely for this doctype too
    (joining the skip tuple, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM,
    WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
    QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST)``).

    This is the END of the finding: Sales Forecast defines NEITHER ``on_submit`` NOR ``on_cancel``
    at all (confirmed by grep — the THIRD doctype after Cost Center Allocation/Supplier Scorecard
    Period) — a governed cancel through this broker flips ``docstatus`` alone and posts no GL/SL
    row of any kind, so ``plan_cancel`` needs no equivalent new flag; ``get_gl_entries`` honestly
    returns empty for a Sales Forecast."""
    if doctype != SALES_FORECAST:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Sales Forecast has no make_gl_entries method anywhere in its class hierarchy "
        "(SalesForecast -> Document — never AccountsController or StockController; its own "
        "import block pulls in no accounting/stock-controller-related name at all) — ERPNext's "
        "own preview RPC would call doc.make_gl_entries() as a bare method call and raise "
        "AttributeError on a live bench, so this broker does not call it for a Sales Forecast at "
        "all. Sales Forecast's own submit posts no GL Entry or Stock Ledger Entry regardless — "
        "it is a demand-planning fixture (no grand_total field exists on this doctype at all, and "
        "neither on_submit nor on_cancel is even defined) — govern the Master Production "
        "Schedule this Sales Forecast's own create_mps whitelist method produces, and THAT "
        "document's own downstream submits, to see real postings"]


def _project_update_ledger_preview_unavailable_flag(doctype):
    """Project Update breadth (2026-07-21) — the Dunning/Blanket Order/Job Card/BOM/Work
    Order/Packing Slip/Cost Center Allocation/Supplier Scorecard Period/Quality Inspection/
    Installation Note/Shipment/Sales Forecast "skip" shape (the native preview is UNCALLABLE, not
    merely non-posting), the SAME cleanest import shape as Sales Forecast. Fires unconditionally
    for ``doctype == PROJECT_UPDATE``, submit-direction only.

    ``class ProjectUpdate(Document):`` (``project_update.py:9``) — a direct ``Document``
    subclass. Its own import block (``:5-6``) pulls in ONLY ``frappe`` and
    ``frappe.model.document.Document`` — zero accounting/stock-controller-related imports of any
    kind, the same cleanest shape Sales Forecast's own landing established. A full-file grep finds
    no ``make_gl_entries``/``make_sl_entries``/``GLEntry``/``StockLedgerEntry`` reference anywhere.
    ERPNext's own ``get_accounting_ledger_preview`` calls ``doc.make_gl_entries()`` bare and
    unguarded — ``AttributeError`` on a live bench for a real Project Update, so
    ``_tool_plan_submit`` skips the network call entirely for this doctype too (joining the skip
    tuple, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER,
    PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE)``).

    This is the END of the finding: ``project_update.py``'s own class body is a bare ``pass`` —
    neither ``on_submit`` NOR ``on_cancel`` is defined at all (confirmed by grep — the FOURTH
    doctype after Cost Center Allocation/Supplier Scorecard Period/Sales Forecast), so a governed
    cancel through this broker flips ``docstatus`` alone and posts no GL/SL row of any kind;
    ``plan_cancel`` needs no equivalent new flag — ``get_gl_entries`` honestly returns empty for a
    Project Update."""
    if doctype != PROJECT_UPDATE:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Project Update has no make_gl_entries method anywhere in its class hierarchy "
        "(ProjectUpdate -> Document — never AccountsController or StockController; its own "
        "import block pulls in no accounting/stock-controller-related name at all) — ERPNext's "
        "own preview RPC would call doc.make_gl_entries() as a bare method call and raise "
        "AttributeError on a live bench, so this broker does not call it for a Project Update at "
        "all. Project Update's own submit posts no GL Entry or Stock Ledger Entry regardless — "
        "it is a project-status snapshot with no grand_total field of any kind, and neither "
        "on_submit nor on_cancel is even defined (a bare 'pass' class body) — govern the linked "
        "Project's own downstream documents to see real postings"]


def _maintenance_visit_ledger_preview_unavailable_flag(doctype):
    """Maintenance Visit breadth (2026-07-21) — the Dunning/.../Project Update "skip" shape (the
    native preview is UNCALLABLE, not merely non-posting), the SAME MRO DEPTH Installation Note
    established (two levels above ``Document``, not Packing Slip's one). Fires unconditionally for
    ``doctype == MAINTENANCE_VISIT``, submit-direction only.

    ``class MaintenanceVisit(TransactionBase):`` (``maintenance_visit.py:12``), and ``class
    TransactionBase(StatusUpdater):`` (``transaction_base.py:20``) — a full-file grep of
    ``maintenance_visit.py``, ``transaction_base.py``, AND ``status_updater.py`` finds no
    ``make_gl_entries``/``make_sl_entries``/``GLEntry``/``StockLedgerEntry`` reference anywhere in
    any of the three. ERPNext's own ``get_accounting_ledger_preview`` calls ``doc.make_gl_
    entries()`` bare and unguarded — ``AttributeError`` on a live bench for a real Maintenance
    Visit, so ``_tool_plan_submit`` skips the network call entirely for this doctype too (joining
    the skip tuple, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER,
    PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT)``).

    Unlike Dunning/Blanket Order/Job Card, this is NOT the end of the finding: Maintenance Visit's
    own ``on_submit``/``on_cancel`` DO reach into another document's row via ``db_update()`` (the
    Warranty Claim mutation — see :func:`_maintenance_visit_submit_risk_flags`/
    :func:`_maintenance_visit_cancel_risk_flags`), but that write is never a GL/Stock Ledger
    posting of any kind — ``plan_cancel`` needs no equivalent ledger-preview flag; ``get_gl_
    entries`` honestly returns empty for a Maintenance Visit."""
    if doctype != MAINTENANCE_VISIT:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Maintenance Visit has no make_gl_entries method anywhere in its class hierarchy "
        "(MaintenanceVisit -> TransactionBase -> StatusUpdater -> Document — never "
        "AccountsController or StockController) — ERPNext's own preview RPC would call "
        "doc.make_gl_entries() as a bare method call and raise AttributeError on a live bench, "
        "so this broker does not call it for a Maintenance Visit at all. Maintenance Visit's own "
        "submit posts no GL Entry or Stock Ledger Entry regardless — it is a service-visit "
        "fulfillment record with no grand_total field of any kind; its real side effects are "
        "direct writes into a linked Warranty Claim and Maintenance Schedule Detail, disclosed "
        "separately (see the risk flags on this same plan), never a ledger posting"]


def _maintenance_visit_submit_risk_flags(doc):
    """Maintenance Visit breadth (2026-07-21) — the submit-direction disclosure for
    ``update_customer_issue(1)`` (``maintenance_visit.py:132-172``), called UNCONDITIONALLY from
    ``on_submit`` (198-201) — but its OWN body is gated on ``if not self.maintenance_schedule:``,
    so it never runs at all for a schedule-driven visit. Doctype-scoping lives at the call site
    (fired only for ``doctype == MAINTENANCE_VISIT``); fully data-driven off the draft's own
    ``maintenance_schedule``/``purposes`` fields — no second-document read needed.

    For EVERY ``purposes`` child row where ``prevdoc_docname`` is truthy AND
    ``prevdoc_doctype == "Warranty Claim"``: the source loads that Warranty Claim
    (``frappe.get_doc``), sets ``resolution_date``/``resolved_by``/``resolution_details``/
    ``status`` from this document's own ``mntc_date``/``service_person``/``work_done``/
    ``completion_status``, and calls ``wc_doc.db_update()`` — ONE INDEPENDENT WRITE PER MATCHING
    ROW, so a single submit can mutate more than one Warranty Claim if more than one purpose row
    names one.

    **The docstatus-guard question settled from source:** ``warranty_claim.json`` carries no
    ``is_submittable`` key at all (confirmed absent, not ``0``) — Warranty Claim is NOT a
    submittable doctype, so there is no docstatus lifecycle for this write to bypass. What
    ``db_update()`` DOES bypass, regardless of that: Warranty Claim's own ``validate()``/
    ``on_update`` hooks, permission checks, and version history — a raw column UPDATE against an
    already-loaded document, never a ``doc.save()`` — the same bypass-the-normal-save-path shape
    Quality Inspection's own ``update_qc_reference()`` carries against a submittable reference
    document (see :func:`_quality_inspection_cancel_risk_flags`)."""
    if doc.get("maintenance_schedule"):
        return []
    rows = [r for r in (doc.get("purposes") or []) if isinstance(r, dict)]
    claims = [r.get("prevdoc_docname") for r in rows
             if r.get("prevdoc_doctype") == "Warranty Claim" and r.get("prevdoc_docname")]
    if not claims:
        return []
    named = ", ".join(repr(c) for c in claims)
    plural = "s" if len(claims) > 1 else ""
    return [
        f"submitting this Maintenance Visit calls update_customer_issue(1), which directly "
        f"rewrites Warranty Claim{plural} {named}'s resolution_date/resolved_by/"
        "resolution_details/status via wc_doc.db_update() — one independent write per purpose "
        "row naming a Warranty Claim (a single submit can touch more than one). Warranty Claim "
        "is not a submittable doctype (is_submittable is unset — no docstatus lifecycle exists "
        "on it to bypass), but db_update() still skips its own validate()/on_update hooks, "
        "permission checks, and version history entirely — the same bypass-the-normal-save-path "
        "shape Quality Inspection's own update_qc_reference() carries against a submittable "
        "reference document"]


def _maintenance_visit_cancel_risk_flags(doc):
    """Maintenance Visit breadth (2026-07-21) — the cancel-direction disclosure, two parts.
    Doctype-scoped at the call site; data-driven off the draft's own ``purposes``/
    ``maintenance_schedule`` fields (part 2) and standing prose (part 1, which needs a sibling
    read this function cannot perform).

    (1) ``on_cancel`` calls ``check_if_last_visit()`` (``maintenance_visit.py:174-196``) FIRST: a
    raw SQL peer query for OTHER SUBMITTED Maintenance Visit rows sharing the SAME
    ``prevdoc_docname`` with a LATER ``mntc_date`` (or same date, later ``mntc_time``) — if any
    exist, ERPNext throws ``"Cancel Material Visits {0} before cancelling this Maintenance
    Visit"`` and refuses the cancel outright. **A dossier correction:** its own §7 calls this "the
    SAME Warranty Claim", but the SQL match is on ``prevdoc_docname`` STRING equality alone, with
    NO ``prevdoc_doctype`` filter (the source even carries a commented-out
    ``# check_for_doctype = d.prevdoc_doctype``) — the gate is not schema-restricted to Warranty
    Claim. This is a SAME-DOCTYPE PEER constraint, invisible to this broker's own
    blast-radius/cascade machinery (``prevdoc_docname`` is a Dynamic Link FROM this child table
    pointing OUT, never a Link TO Maintenance Visit) and needs a sibling read this plan cannot
    perform — disclosed here in PROSE ONLY, the same undisclosable-without-a-new-read shape
    Installation Note's own ``validate_serial_no`` already established; no new sibling-query
    machinery is invented for it.

    (2) If that gate does NOT throw, ``self.update_customer_issue(0)`` runs the SAME Warranty
    Claim write(s) :func:`_maintenance_visit_submit_risk_flags` describes, but with reset values
    computed from a SECOND sibling query (the latest other ``Partially Completed`` submitted
    Maintenance Visit against the same ``prevdoc_docname``, or a plain reset to ``"Open"``/blank
    if none exists). The CANDIDATE Warranty Claim(s) are still readable from this draft's own
    fields alone (the same ``maintenance_schedule``/``purposes`` condition as submit) — named
    here — but the exact reset values are not, for the same sibling-read reason as part 1."""
    flags = [
        "cancelling a Maintenance Visit first runs check_if_last_visit(): a raw SQL peer query "
        "for OTHER submitted Maintenance Visit rows sharing the SAME prevdoc_docname with a "
        "LATER mntc_date (or same date, later mntc_time) — if any exist, ERPNext throws 'Cancel "
        "Material Visits {0} before cancelling this Maintenance Visit' and this cancel is "
        "refused outright. This is a same-doctype PEER constraint, invisible to this broker's "
        "own blast-radius/cascade check (prevdoc_docname is a Dynamic Link FROM this child table "
        "to an external document, never a Link TO Maintenance Visit — cascade.py's generic Link "
        "walk cannot see it), and requires a sibling read this plan cannot perform — disclosed "
        "here as an ERPNext-native doomed-cancel path, never bypassed"]
    if not doc.get("maintenance_schedule"):
        rows = [r for r in (doc.get("purposes") or []) if isinstance(r, dict)]
        claims = [r.get("prevdoc_docname") for r in rows
                 if r.get("prevdoc_doctype") == "Warranty Claim" and r.get("prevdoc_docname")]
        if claims:
            named = ", ".join(repr(c) for c in claims)
            flags.append(
                f"if the ordering check above does not refuse this cancel: "
                f"update_customer_issue(0) will ALSO mutate Warranty Claim {named} via the same "
                "db_update bypass described on submit — reusing another qualifying Maintenance "
                "Visit's own resolution values if one exists, or resetting status to Open/"
                "blanking resolution_date/resolved_by/resolution_details if none does; the exact "
                "outcome needs the same sibling read as the ordering check above, so only the "
                "fact of the touch is disclosed here")
    return flags


def _maintenance_schedule_ledger_preview_unavailable_flag(doctype):
    """Maintenance Schedule breadth (2026-07-21) — the Dunning/.../Maintenance Visit "skip" shape
    (the native preview is UNCALLABLE, not merely non-posting), the SAME MRO DEPTH Installation
    Note/Maintenance Visit established. Fires unconditionally for
    ``doctype == MAINTENANCE_SCHEDULE``, submit-direction only.

    ``class MaintenanceSchedule(TransactionBase):`` (``maintenance_schedule.py:13``) — a full-file
    grep of ``maintenance_schedule.py``, ``transaction_base.py``, AND ``status_updater.py`` finds
    no ``make_gl_entries``/``make_sl_entries``/``GLEntry``/``StockLedgerEntry`` reference anywhere
    in any of the three. ERPNext's own ``get_accounting_ledger_preview`` calls ``doc.make_gl_
    entries()`` bare and unguarded — ``AttributeError`` on a live bench for a real Maintenance
    Schedule, so ``_tool_plan_submit`` skips the network call entirely for this doctype too
    (joining the skip tuple, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM,
    WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
    QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE,
    MAINTENANCE_VISIT, MAINTENANCE_SCHEDULE)``).

    Maintenance Schedule's own real side effects — the Serial No field mutation and the auto-
    created/auto-deleted Event documents (see :func:`_maintenance_schedule_submit_risk_flags`/
    :func:`_maintenance_schedule_cancel_risk_flags`) — are never a GL/Stock Ledger posting of any
    kind; ``get_gl_entries`` honestly returns empty for a Maintenance Schedule."""
    if doctype != MAINTENANCE_SCHEDULE:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Maintenance Schedule has no make_gl_entries method anywhere in its class "
        "hierarchy (MaintenanceSchedule -> TransactionBase -> StatusUpdater -> Document — never "
        "AccountsController or StockController) — ERPNext's own preview RPC would call "
        "doc.make_gl_entries() as a bare method call and raise AttributeError on a live bench, "
        "so this broker does not call it for a Maintenance Schedule at all. Its real side "
        "effects are a Serial No field write and auto-created/auto-deleted Event documents, "
        "disclosed separately (see the risk flags on this same plan), never a ledger posting"]


def _maintenance_schedule_submit_risk_flags(doc):
    """Maintenance Schedule breadth (2026-07-21) — the submit-direction disclosure, entirely
    data-driven off the draft's own ``schedules``/``items`` child rows; doctype-scoped at the call
    site (fired only for ``doctype == MAINTENANCE_SCHEDULE``); no second-document read needed.

    (1) ``on_submit`` (``maintenance_schedule.py:102-158``) throws immediately
    (``"Please click on 'Generate Schedule' to get schedule"``) if ``schedules`` is empty — a
    doomed-submit gate readable off the draft before a marker is ever minted, the same "status
    gate readable on the draft" shape Quality Inspection's own ``before_submit`` disclosure
    established.

    (2) If ``schedules`` is non-empty, on_submit auto-creates ONE Event document per ``schedules``
    row (``frappe.get_doc({"doctype": "Event", ...}).insert(ignore_permissions=1)``, then
    ``event.add_participant(...)``) — Event carries no ``is_submittable`` key at all, so these are
    ordinary Desk records, never themselves submitted. The exact count is read straight off the
    draft's own ``schedules`` rows.

    (3) For every ``items`` row carrying a ``serial_and_batch_bundle``: submit will run
    ``validate_serial_no()`` (can THROW — under warranty, under an existing maintenance contract,
    or delivered after this row's own ``start_date``) then, if it passes,
    ``update_amc_date(serial_nos, d.end_date)`` — settled from source to a FULL
    ``serial_no_doc.save()`` (validate/hooks/versioning all run), never ``db_set()``/raw SQL — the
    first cross-document mutation this campaign has found that is not a bypass. The item row and
    bundle id are named; the actual resolved Serial Nos are not (that needs a live ``Serial and
    Batch Bundle`` read this disclosure cannot perform)."""
    schedules = [r for r in (doc.get("schedules") or []) if isinstance(r, dict)]
    if not schedules:
        return [
            "submit will be REFUSED: the schedules table is empty — on_submit throws 'Please "
            "click on 'Generate Schedule' to get schedule' before any Event is created or any "
            "Serial No is touched"]
    flags = [
        f"submitting will auto-create {len(schedules)} Event document(s), one per schedules "
        "row (frappe.get_doc({'doctype': 'Event', ...}).insert(ignore_permissions=1)) — Event "
        "is not itself a submittable doctype, so these are ordinary Desk records, never "
        "themselves submitted"]
    items = [r for r in (doc.get("items") or []) if isinstance(r, dict)]
    bundled = [(r.get("item_code"), r.get("serial_and_batch_bundle")) for r in items
              if r.get("serial_and_batch_bundle")]
    if bundled:
        named = ", ".join(f"{code!r} (bundle {bundle!r})" for code, bundle in bundled[:5])
        more = f" (+{len(bundled) - 5} more)" if len(bundled) > 5 else ""
        flags.append(
            f"item row(s) {named}{more} carry a serial_and_batch_bundle — submit will run "
            "validate_serial_no() (can THROW if any resolved Serial No is under warranty, "
            "under an existing maintenance contract, or delivered after this row's own "
            "start_date) then call serial_no_doc.save() (a FULL document save, not db_set/raw "
            "SQL) setting each resolved Serial No's amc_expiry_date to this row's end_date")
    return flags


def _maintenance_schedule_cancel_risk_flags(doc):
    """Maintenance Schedule breadth (2026-07-21) — the cancel-direction disclosure. Part (1) is
    data-driven off the draft's own ``items`` rows; part (2) is standing prose (every submitted
    Maintenance Schedule reached this doctype's ``on_cancel`` has a non-empty ``schedules`` table
    by construction — the submit-time throw above guarantees it — so the Event-deletion fact is
    unconditional, not read off a second document). Doctype-scoped at the call site.

    (1) ``on_cancel`` (391-402): for every ``items`` row carrying a ``serial_and_batch_bundle``,
    ``update_amc_date(serial_nos)`` runs with NO date argument — the SAME full ``.save()``
    mechanism as submit, this time clearing ``amc_expiry_date`` back to ``None``.

    (2) ``on_cancel`` then calls ``delete_events(self.doctype, self.name)``
    (``transaction_base.py:582-599``) — a raw SQL join over ``tabEvent``/``tabEvent Participants``
    followed by ``frappe.delete_doc("Event", events, for_reload=True)`` — a REAL, PERMANENT delete
    of every Event this document's own submit created, never a soft-delete or an orphaning. The
    SAME cleanup fires a third time from ``on_trash`` (404-405), a lifecycle hook the dossier's
    own §7 never named.

    (3) ``on_cancel`` carries ZERO reference to Maintenance Visit anywhere (verified by a full
    grep of ``maintenance_schedule.py``) — no ``ignore_linked_doctypes``, no throw of any kind.
    This broker's OWN blast-radius gate (``get_submitted_linked_docs``, checked before this
    function ever runs — see ``_tool_plan_cancel``) is what actually protects a linked, submitted
    Maintenance Visit here, not anything ERPNext's own ``on_cancel`` does."""
    flags = [
        "cancelling permanently DELETES (frappe.delete_doc, not orphaned) every Event this "
        "Maintenance Schedule's own submit created, via delete_events() — a raw SQL lookup "
        "against Event Participants, not readable from this draft's own fields alone",
        "ERPNext's own on_cancel carries NO gate against a linked, submitted Maintenance Visit "
        "(verified by a full grep — no ignore_linked_doctypes, no throw of any kind); this "
        "broker's own blast-radius check is what actually refuses that case, not this doctype's "
        "native cancel path"]
    items = [r for r in (doc.get("items") or []) if isinstance(r, dict)]
    bundled = [(r.get("item_code"), r.get("serial_and_batch_bundle")) for r in items
              if r.get("serial_and_batch_bundle")]
    if bundled:
        named = ", ".join(f"{code!r} (bundle {bundle!r})" for code, bundle in bundled[:5])
        more = f" (+{len(bundled) - 5} more)" if len(bundled) > 5 else ""
        flags.append(
            f"item row(s) {named}{more} carry a serial_and_batch_bundle — cancelling will call "
            "update_amc_date(serial_nos) with no date argument, clearing each resolved Serial "
            "No's amc_expiry_date back to None via the same full serial_no_doc.save() mechanism "
            "as submit")
    return flags


def _asset_maintenance_log_ledger_preview_unavailable_flag(doctype):
    """Asset Maintenance Log breadth (2026-07-21) — the same "uncallable" shape, the SIMPLEST MRO
    in the category. Fires unconditionally for ``doctype == ASSET_MAINTENANCE_LOG``, submit-
    direction only.

    ``class AssetMaintenanceLog(Document):`` (``asset_maintenance_log.py:14``) — a full-file grep
    finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry``/``StockLedgerEntry`` reference
    anywhere. ERPNext's own ``get_accounting_ledger_preview`` calls ``doc.make_gl_entries()`` bare
    and unguarded — ``AttributeError`` on a live bench for a real Asset Maintenance Log, so
    ``_tool_plan_submit`` skips the network call entirely for this doctype too (joining the skip
    tuple, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER,
    PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT,
    MAINTENANCE_SCHEDULE, ASSET_MAINTENANCE_LOG)``).

    This doctype's own real side effects — the Task/parent-Asset-Maintenance ``.save()`` cascade
    (see :func:`_asset_maintenance_log_submit_risk_flags`) — are never a GL/Stock Ledger posting of
    any kind; ``get_gl_entries`` honestly returns empty for an Asset Maintenance Log."""
    if doctype != ASSET_MAINTENANCE_LOG:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Asset Maintenance Log has no make_gl_entries method anywhere in its class "
        "hierarchy (AssetMaintenanceLog -> Document directly — never AccountsController or "
        "StockController) — ERPNext's own preview RPC would call doc.make_gl_entries() as a "
        "bare method call and raise AttributeError on a live bench, so this broker does not "
        "call it for an Asset Maintenance Log at all. Its real side effects are cross-document "
        "saves against the linked Task and the parent Asset Maintenance record, disclosed "
        "separately (see the risk flags on this same plan), never a ledger posting"]


def _asset_maintenance_log_submit_risk_flags(doc):
    """Asset Maintenance Log breadth (2026-07-21) — the submit-direction disclosure, entirely
    data-driven off the draft's own ``maintenance_status``/``completion_date`` fields; doctype-
    scoped at the call site (fired only for ``doctype == ASSET_MAINTENANCE_LOG``); no second-
    document read needed for the doom determination itself.

    ``validate()`` (``asset_maintenance_log.py:44-55``) runs on the submit path too (frappe's own
    ``run_before_save_methods`` calls ``validate`` for both the ``"save"`` and ``"submit"``
    actions — confirmed, ``frappe/model/document.py:1402-1407``) — BEFORE ``on_submit`` — so all
    three throws below are readable off the draft before a marker is ever minted:

    (1) ``maintenance_status == "Completed"`` and ``completion_date`` blank -> validate() throws
    "Please select Completion Date for Completed Asset Maintenance Log" (51-52).
    (2) ``maintenance_status != "Completed"`` and ``completion_date`` set -> validate() throws
    "Please select Maintenance Status as Completed or remove Completion Date" (54-55).
    (3) Neither of the above, but ``maintenance_status`` is not exactly ``"Completed"`` or
    ``"Cancelled"`` -> ``on_submit`` throws "Maintenance Status has to be Cancelled or Completed
    to Submit" (58-59) — validate()'s own auto-flip to "Overdue" when ``due_date`` is already past
    (45-49) does not help, Overdue is not in the allowed set either.

    Only (``"Completed"`` + a set ``completion_date``) or (``"Cancelled"`` + a blank one) survive
    all three gates. When they do, ``on_submit`` calls ``update_maintenance_task()`` (62-77): the
    linked Asset Maintenance Task is saved in full (validate/hooks/versioning all run) ONLY when
    status is Completed and the Task's own ``last_completion_date`` actually differs, or
    unconditionally (status set to Cancelled) when status is Cancelled — and, regardless of
    either branch, the PARENT Asset Maintenance record (``self.asset_maintenance``) is ALWAYS
    saved in full too, re-triggering that parent's own ``on_update`` (task reassignment plus a
    re-sync capable of creating/updating OTHER sibling Asset Maintenance Log documents —
    ``asset_maintenance.py:43-46/53-68``). The actual sibling rows touched need a live read of the
    parent's own task table this static disclosure cannot perform (the Maintenance Schedule Serial
    No precedent for an honesty limit).

    Standing prose (unconditional, whenever submit is not doomed): names the TWO status-rewrite
    bypasses that remain live against this document AFTER submission — the scheduler's raw SQL
    (draft-only in practice) and the parent's own ungated ``db_set`` (which genuinely reaches a
    submitted, Completed log) — see :data:`pacioli.erpnext.SUPPORTED_DOCTYPES`'s own comment block
    for the full source-cited finding on both mechanisms."""
    status = doc.get("maintenance_status")
    completion_date = doc.get("completion_date")
    if status == "Completed" and not completion_date:
        return [
            "submit will be REFUSED: validate() throws 'Please select Completion Date for "
            "Completed Asset Maintenance Log' — maintenance_status is 'Completed' but "
            "completion_date is blank (asset_maintenance_log.py:51-52)"]
    if status != "Completed" and completion_date:
        return [
            "submit will be REFUSED: validate() throws 'Please select Maintenance Status as "
            f"Completed or remove Completion Date' — completion_date is set but "
            f"maintenance_status is {status!r}, not Completed (asset_maintenance_log.py:54-55)"]
    if status not in ("Completed", "Cancelled"):
        return [
            "submit will be REFUSED: on_submit throws 'Maintenance Status has to be Cancelled "
            f"or Completed to Submit' — maintenance_status is {status!r} (validate()'s own "
            "auto-flip to 'Overdue' when due_date is already past does not help; Overdue is not "
            "in the allowed set either) (asset_maintenance_log.py:58-59)"]
    # Only (Completed + a set completion_date) or (Cancelled + a blank one) reach here.
    flags = [
        "submit succeeds; on_submit's update_maintenance_task() ALWAYS saves the PARENT Asset "
        f"Maintenance record ({doc.get('asset_maintenance')!r}) in full — re-triggering that "
        "parent's own on_update (task reassignment + a re-sync across ALL of its own "
        "maintenance tasks, capable of creating or updating OTHER sibling Asset Maintenance "
        "Log documents) — a real cascading side effect beyond this single document "
        "(asset_maintenance_log.py:76-77, asset_maintenance.py:43-46/53-68)"]
    if status == "Completed":
        flags.append(
            f"the linked Asset Maintenance Task ({doc.get('task')!r}) will ALSO be saved in "
            "full (validate/hooks/versioning all run) if its own last_completion_date differs "
            "from this log's completion_date — setting last_completion_date, a recalculated "
            "next_due_date, and flipping ITS OWN maintenance_status to 'Planned' "
            "(asset_maintenance_log.py:64-72)")
    else:
        flags.append(
            f"the linked Asset Maintenance Task ({doc.get('task')!r}) will ALSO be saved in "
            "full with its own maintenance_status set to 'Cancelled' "
            "(asset_maintenance_log.py:73-75)")
    flags.append(
        "AFTER submission, maintenance_status remains mutable OUTSIDE this broker's own audit "
        "trail by TWO ERPNext-native mechanisms, neither gated by docstatus in its own filter: "
        "(1) the 'daily_maintenance' scheduler job update_asset_maintenance_log_status() runs a "
        "raw frappe.qb UPDATE ... SET maintenance_status='Overdue' WHERE "
        "maintenance_status='Planned' AND due_date < today() (asset_maintenance_log.py:80-88, "
        "registered erpnext/hooks.py:485) — draft-only in practice, since on_submit's own gate "
        "means a submitted document can never read back 'Planned'; (2) editing the PARENT Asset "
        "Maintenance record's own task table fires its on_update -> sync_maintenance_tasks() "
        "(asset_maintenance.py:53-68), which calls "
        "maintenance_log.db_set('maintenance_status', 'Cancelled') with NO status/docstatus "
        "filter at all — db_set() skips validate()/on_update()/versioning entirely (frappe/"
        "model/document.py's own docstring), so a SUBMITTED, Completed log CAN be silently "
        "flipped to Cancelled this way")
    return flags


def _asset_maintenance_log_cancel_risk_flags(doc):
    """Asset Maintenance Log breadth (2026-07-21) — the cancel-direction disclosure. Standing
    prose only (unconditional whenever ``doctype == ASSET_MAINTENANCE_LOG``): ``on_cancel`` is
    CONFIRMED ABSENT ENTIRELY (a full-file grep of ``asset_maintenance_log.py`` finds no
    ``on_cancel`` method of any kind), and frappe's own cancel path SKIPS the doctype's
    ``validate()`` hook for the ``"cancel"`` action entirely (``run_before_save_methods`` calls
    only ``before_cancel``, never defined here either — confirmed, ``frappe/model/document.py:
    1408-1409``). Cancelling this doctype is therefore a PURE ``docstatus`` flip plus frappe's own
    generic ``check_no_back_links_exist()`` — it never reverses the Task/parent-Asset-Maintenance
    ``.save()`` cascade ``on_submit`` performed (:func:`_asset_maintenance_log_submit_risk_flags`).
    Doctype-scoped at the call site."""
    return [
        "on_cancel is CONFIRMED ABSENT for Asset Maintenance Log — cancelling is a pure "
        "docstatus flip (frappe's own generic check_no_back_links_exist() only); it does NOT "
        "reverse the Task/parent Asset Maintenance .save() cascade this document's own submit "
        "performed — there is no code path that undoes it"]


def _bank_guarantee_ledger_preview_unavailable_flag(doctype):
    """Bank Guarantee breadth (2026-07-21) — the same "uncallable" shape, the SIMPLEST bare-
    ``Document`` MRO in the category (tied with Job Card's/Work Order's/Blanket Order's/
    Shipment's/BOM's/Asset Maintenance Log's own shape). Fires unconditionally for
    ``doctype == BANK_GUARANTEE``, submit-direction only.

    ``class BankGuarantee(Document):`` (``bank_guarantee.py:10``) — a DIRECT
    ``frappe.model.document.Document`` subclass (confirmed from its own import line and class
    statement) — never ``AccountsController``, never ``StockController``. A full-file grep of
    ``bank_guarantee.py`` finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry``/
    ``StockLedgerEntry`` reference anywhere. ERPNext's own ``get_accounting_ledger_preview`` calls
    ``doc.make_gl_entries()`` bare and unguarded — ``AttributeError`` on a live bench for a real
    Bank Guarantee, so ``_tool_plan_submit`` skips the network call entirely for this doctype too
    (joining the skip tuple, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM,
    WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
    QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE,
    MAINTENANCE_VISIT, MAINTENANCE_SCHEDULE, ASSET_MAINTENANCE_LOG, BANK_GUARANTEE)``).

    This doctype's own ``on_submit`` performs validation only — no document creation, no other
    document's write, no GL/Stock Ledger posting of any kind (see
    :func:`_bank_guarantee_submit_risk_flags`); ``get_gl_entries`` honestly returns empty for a
    Bank Guarantee."""
    if doctype != BANK_GUARANTEE:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Bank Guarantee has no make_gl_entries method anywhere in its class hierarchy "
        "(BankGuarantee -> Document directly — never AccountsController or StockController) — "
        "ERPNext's own preview RPC would call doc.make_gl_entries() as a bare method call and "
        "raise AttributeError on a live bench, so this broker does not call it for a Bank "
        "Guarantee at all. Its own submit performs validation only (three field-presence "
        "checks, each stopping at the first that fails) — no document creation, no other "
        "document's write, no ledger posting of any kind"]


def _bank_guarantee_submit_risk_flags(doc):
    """Bank Guarantee breadth (2026-07-21) — the submit-direction disclosure for ``on_submit``'s
    own three-field gate (``bank_guarantee.py:49-55``), a genuine doomed-submit story readable off
    the draft's own fields before a marker is ever minted (the Quality Inspection
    ``before_submit``/Asset Maintenance Log ``on_submit`` precedent). Doctype-scoped at the call
    site; data-driven off the draft's own fields, no new bench read.

    None of ``bank_guarantee_number``, ``name_of_beneficiary``, or ``bank`` is ``reqd`` at the
    schema level (confirmed absent from all three field definitions in ``bank_guarantee.json``),
    so a draft can be saved and read back with any or all of them blank — only ``on_submit``'s own
    three throws enforce them. The checks run in STRICT SOURCE ORDER and stop at the first failure
    (``frappe.throw`` raises immediately, the same "first one wins" shape Quality Inspection's own
    ``before_submit`` readings-loop already established): ``bank_guarantee_number`` first
    (50-51), then ``name_of_beneficiary`` (52-53), then ``bank`` (54-55) — a document missing two
    or three of them still surfaces only the FIRST throw on a real submit.

    ``validate()``'s own separate customer-or-supplier gate (``bank_guarantee.py:46-47``) is NOT
    re-disclosed here: it runs on every save (frappe's own ``run_before_save_methods`` calls
    ``validate`` for both the ``"save"`` and ``"submit"`` actions, the same Asset Maintenance
    Log/Quality Inspection precedent), so an EXISTING draft this broker can already read back has
    necessarily satisfied it at its own last save — it cannot newly doom a submit this flag needs
    to preview.

    When all three ``on_submit`` fields are present, submit succeeds and performs NO further side
    effect of any kind (``on_submit`` here is validation-only — no document creation, no other
    document's write, no enqueue — confirmed by a full 74-line read of ``bank_guarantee.py``), so
    this function returns nothing further to disclose — the plainest "doomed-or-nothing" submit
    story this campaign has found."""
    if not doc.get("bank_guarantee_number"):
        return [
            "submit will be REFUSED: on_submit throws 'Enter the Bank Guarantee Number before "
            "submitting.' — bank_guarantee_number is blank (bank_guarantee.py:50-51)"]
    if not doc.get("name_of_beneficiary"):
        return [
            "submit will be REFUSED: on_submit throws 'Enter the name of the Beneficiary before "
            "submitting.' — name_of_beneficiary is blank (bank_guarantee.py:52-53)"]
    if not doc.get("bank"):
        return [
            "submit will be REFUSED: on_submit throws 'Enter the name of the bank or lending "
            "institution before submitting.' — bank is blank (bank_guarantee.py:54-55)"]
    return []


def _asset_movement_ledger_preview_unavailable_flag(doctype):
    """Asset Movement breadth (2026-07-21) — the same "uncallable" shape, a bare ``Document`` MRO.
    Fires unconditionally for ``doctype == ASSET_MOVEMENT``, submit-direction only.

    ``class AssetMovement(Document):`` (``asset_movement.py:13``) — a DIRECT
    ``frappe.model.document.Document`` subclass — never ``AccountsController``, never
    ``StockController``. A full-file grep of ``asset_movement.py`` finds no ``make_gl_entries``/
    ``make_sl_entries``/``GLEntry``/``StockLedgerEntry`` reference anywhere. ERPNext's own
    ``get_accounting_ledger_preview`` calls ``doc.make_gl_entries()`` bare and unguarded —
    ``AttributeError`` on a live bench for a real Asset Movement, so ``_tool_plan_submit`` skips
    the network call entirely for this doctype too (joining the skip tuple, now its NINETEENTH
    member: ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER,
    PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT,
    MAINTENANCE_SCHEDULE, ASSET_MAINTENANCE_LOG, BANK_GUARANTEE, ASSET_MOVEMENT)``).

    Asset Movement posts no GL/Stock Ledger row of any kind under its own name — it is a pure
    state-capture trail for an Asset's location/custodian (see
    :func:`_asset_movement_write_risk_flags`); ``get_gl_entries`` honestly returns empty for it."""
    if doctype != ASSET_MOVEMENT:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Asset Movement has no make_gl_entries method anywhere in its class hierarchy "
        "(AssetMovement -> Document directly — never AccountsController or StockController) — "
        "ERPNext's own preview RPC would call doc.make_gl_entries() as a bare method call and "
        "raise AttributeError on a live bench, so this broker does not call it for an Asset "
        "Movement at all. Asset Movement posts no GL or Stock Ledger entry of any kind under "
        "its own name regardless — it is a pure state-capture trail for an Asset's location "
        "and custodian, never an accounting document"]


def _asset_movement_write_risk_flags(doc, op):
    """Asset Movement breadth (2026-07-21) — THE central finding of this landing, fired on BOTH
    directions (the :func:`_pos_risk_flags` shape: one function, an ``op`` parameter) because
    ``on_submit`` and ``on_cancel`` call the EXACT SAME method
    (``set_latest_location_and_custodian_in_asset``, ``asset_movement.py:116-120``) — the FIRST
    doctype this campaign has found whose submit and cancel hooks are textually identical.
    Doctype-scoped at the call site; unconditional whenever the draft carries at least one row in
    its own ``assets`` child table (every real Asset Movement does — the field is ``reqd: 1``) —
    data-driven off the draft's own fields, no new bench read.

    THE WRITE MECHANISM, read line by line (``asset_movement.py:122-162``): for each row in
    ``self.assets``, ``get_latest_location_and_custodian(asset)`` re-queries ALL submitted Asset
    Movement Item rows for that asset (a live SQL join, ordered by ``transaction_date DESC``,
    ``LIMIT 1``) to recompute the CURRENT trail fresh from the bench — never read off this
    document's own fields, which is why submit and cancel can share one method: cancelling simply
    removes this document from ``docstatus = 1`` contention, so the same query naturally resolves
    to the prior movement (or to ``("", "")`` if none remains). ``update_asset_location_and_
    custodian`` then writes the result onto the Asset via raw ``frappe.db.set_value``
    (``asset_movement.py:159-162``) — confirmed from ``frappe.db.set_value``'s own docstring
    (``frappe/database/database.py:934-953``: *"do not call the ORM triggers... will not call
    Document events and should be avoided in normal cases"*) — the SAME "even more direct bypass
    than ``db_update()``: no ORM triggers, no Document events" grade Maintenance Visit's own
    ``update_status_and_actual_date`` already established for this campaign: no ``validate()``, no
    hooks, no version history on the Asset. A nuance this campaign has not yet seen: a document IS
    loaded first (``asset = frappe.get_doc("Asset", asset_id)``, line 157) — but ONLY to read the
    current ``custodian``/``location`` for the conditional comparison; the write itself never
    touches that loaded object, going straight to the module-level ``frappe.db.set_value``
    regardless.

    THE SHARPEST DISCLOSURE, cancel direction only (an unconditional PROSE addition — no sibling
    read this plan can perform would let it predict which case a real cancel resolves to, only
    that the asymmetry exists, the Maintenance Visit ``check_if_last_visit`` precedent): an
    ASYMMETRIC field-level guard the dossier's own §7 missed (``asset_movement.py:159-162``):
    ``custodian`` is written whenever the resolved employee differs from the Asset's current one
    (even an EMPTY resolved employee still overwrites a non-empty custodian with ``""``, a real
    clear), but ``location`` is written ONLY ``if location and location != asset.location`` — a
    FALSY (empty) resolved location is NEVER written. A cancel that rolls an asset back to "no
    prior movement" therefore clears ``custodian`` to empty but leaves ``location`` completely
    untouched — the dossier's own §7 claims both fields become empty strings; true for
    ``custodian`` alone."""
    assets = [r for r in (doc.get("assets") or []) if isinstance(r, dict) and r.get("asset")]
    if not assets:
        return []
    names = ", ".join(r["asset"] for r in assets)
    verb = "submitting" if op == "submit" else "cancelling"
    flags = [
        f"{verb} this Asset Movement recomputes and writes the location/custodian trail for "
        f"{len(assets)} referenced Asset(s) ({names}): ERPNext re-queries ALL submitted Asset "
        "Movements for each asset and writes the freshest result directly via "
        "frappe.db.set_value (asset_movement.py:156-162) — no validate(), no hooks, no version "
        "history on the Asset; a document IS loaded first (frappe.get_doc) but only to read the "
        "current values for comparison, never to perform the write itself"]
    if op == "cancel":
        flags.append(
            "if no earlier submitted movement remains for a referenced asset after this cancel, "
            "its custodian is cleared to an empty string, but its location is NOT cleared — "
            "update_asset_location_and_custodian only writes location when the recomputed value "
            "is truthy (asset_movement.py:161: 'if location and location != asset.location') — "
            "an asymmetry the dossier's own §7 missed, describing both fields as clearing to "
            "empty")
    return flags


def _delivery_trip_ledger_preview_unavailable_flag(doctype):
    """Delivery Trip breadth (2026-07-21) — the same "uncallable" shape, a bare ``Document`` MRO.
    Fires unconditionally for ``doctype == DELIVERY_TRIP``, submit-direction only.

    ``class DeliveryTrip(Document):`` (``delivery_trip.py:14``) — a DIRECT ``frappe.model.
    document.Document`` subclass — never ``AccountsController``, never ``StockController``. A
    full-file grep of ``delivery_trip.py`` finds no ``make_gl_entries``/``make_sl_entries``/
    ``GLEntry``/``StockLedgerEntry`` reference anywhere. ERPNext's own
    ``get_accounting_ledger_preview`` calls ``doc.make_gl_entries()`` bare and unguarded —
    ``AttributeError`` on a live bench for a real Delivery Trip, so ``_tool_plan_submit`` skips the
    network call entirely for this doctype too (joining the skip tuple, now its TWENTIETH member:
    ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
    SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT, MAINTENANCE_SCHEDULE,
    ASSET_MAINTENANCE_LOG, BANK_GUARANTEE, ASSET_MOVEMENT, DELIVERY_TRIP)``).

    Delivery Trip posts no GL/Stock Ledger row of any kind under its own name — it only mutates
    fields on OTHER, already-existing Delivery Notes (see
    :func:`_delivery_trip_cancel_risk_flags`); ``get_gl_entries`` honestly returns empty for it."""
    if doctype != DELIVERY_TRIP:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Delivery Trip has no make_gl_entries method anywhere in its class hierarchy "
        "(DeliveryTrip -> Document directly — never AccountsController or StockController) — "
        "ERPNext's own preview RPC would call doc.make_gl_entries() as a bare method call and "
        "raise AttributeError on a live bench, so this broker does not call it for a Delivery "
        "Trip at all. Delivery Trip posts no GL or Stock Ledger entry of any kind under its own "
        "name regardless — it only mutates fields on already-existing Delivery Notes, never an "
        "accounting document in its own right"]


def _delivery_trip_linked_delivery_notes(doc):
    """Shared helper (Delivery Trip breadth, 2026-07-21): the SAME distinct-name computation
    ``update_delivery_notes`` itself performs (``delivery_trip.py:122``:
    ``list(set(stop.delivery_note for stop in self.delivery_stops if stop.delivery_note))``),
    read off the draft's own ``delivery_stops`` child rows — no second-document read. Shared by
    both :func:`_delivery_trip_submit_risk_flags` and :func:`_delivery_trip_cancel_risk_flags` so
    the two directions never drift on which rows count."""
    stops = [r for r in (doc.get("delivery_stops") or []) if isinstance(r, dict)]
    seen = []
    for r in stops:
        note = r.get("delivery_note")
        if note and note not in seen:
            seen.append(note)
    return seen


def _delivery_trip_submit_risk_flags(doc):
    """Delivery Trip breadth (2026-07-21) — the submit-direction disclosure for ``validate()``'s
    own TWO throws (``delivery_trip.py:57-63``), both gated on ``self._action == "submit"``;
    doctype-scoped at the call site (fired only for ``doctype == DELIVERY_TRIP``).

    (1) ``if self._action == "submit" and not self.driver: frappe.throw(...)`` (lines 58-59) — a
    doomed-submit gate deterministic off the draft's OWN ``driver`` field, the Maintenance
    Schedule "submit will be REFUSED" shape: no live read needed, checked here directly.

    (2) Unconditionally when ``_action == "submit"``, ``self.validate_delivery_note_not_draft()``
    (lines 61-62, body 86-98) throws if ANY Delivery Note named by a ``delivery_stops`` row is
    still ``docstatus: 0`` — this needs a LIVE read of each named Delivery Note's own docstatus
    this plan cannot perform (the Asset Movement "monotonic validate needs other movements' state"
    shape). The linked Delivery Note names themselves ARE known from the draft's own
    ``delivery_stops`` rows (:func:`_delivery_trip_linked_delivery_notes`) and are surfaced as an
    unconditional, data-driven prose addition — the outcome (draft or not) stays prose."""
    if not doc.get("driver"):
        return [
            "submit will be REFUSED: no driver is set — validate() throws 'A driver must be set "
            "to submit.' (delivery_trip.py:58-59) before validate_delivery_note_not_draft() or "
            "validate_stop_addresses() ever run"]
    flags = []
    notes = _delivery_trip_linked_delivery_notes(doc)
    if notes:
        named = ", ".join(repr(n) for n in notes[:5])
        more = f" (+{len(notes) - 5} more)" if len(notes) > 5 else ""
        flags.append(
            f"submit will ALSO be refused if any of the following linked Delivery Note(s) is "
            f"still in draft state at execute time — validate_delivery_note_not_draft() "
            f"(delivery_trip.py:86-98) throws naming every draft one found: {named}{more}. This "
            "plan cannot verify each note's current docstatus without a live read; the names "
            "alone are known from this draft's own delivery_stops rows")
    return flags


def _delivery_trip_cancel_risk_flags(doc):
    """Delivery Trip breadth (2026-07-21) — THE central finding of this landing, cancel direction.
    Fully data-driven off the draft's own ``delivery_stops`` rows
    (:func:`_delivery_trip_linked_delivery_notes`) — no second-document read for the naming
    itself, though the STRUCTURAL claim below is proven from ``pacioli.cascade``'s own contract,
    not from a bench read. Doctype-scoped at the call site (fired only for
    ``doctype == DELIVERY_TRIP``).

    THE MECHANISM (``delivery_trip.py:112-150``): ``on_cancel`` calls ``update_delivery_notes(
    delete=True)``, which force-clears FIVE fields (``driver``/``driver_name``/``vehicle_no``/
    ``delivery_trip``/``lr_no``/``lr_date``) to ``None`` on every distinct Delivery Note named by
    a ``delivery_stops`` row, then — for each note where something actually changed — sets
    ``note_doc.flags.ignore_validate_update_after_submit = True`` and calls ``note_doc.save()``
    (line 146-147).

    **THE HONESTY GRADE, pinned from ``frappe/model/document.py``'s own ``_save``
    (552-611)/``check_docstatus_transition`` (1112-1150)/``validate_update_after_submit``
    (1164-1176) + ``base_document.py``'s ``_validate_update_after_submit`` (1270-1306): calling
    ``.save()`` on an ALREADY-SUBMITTED Delivery Note resolves ``_action = "update_after_submit"``
    and REQUIRES submit permission — the SANCTIONED, ERPNext-shipped mechanism for updating a
    submitted document's non-protected fields, not a bypass at all.** Permission checks,
    ``check_if_latest`` (freshness/timestamp-conflict protection), ``_validate_links``, the
    ``before_update_after_submit``/``on_update_after_submit``/``on_change`` hooks, and
    ``save_version()`` (real version history) ALL still run. The doctype's own custom
    ``validate()`` hook never fires for this action — a property of ``update_after_submit`` itself
    (``run_before_save_methods`` only calls it for ``_action in ("save", "submit")``), not a
    discretionary skip here. The ONE thing ``ignore_validate_update_after_submit`` itself removes
    is ``_validate_update_after_submit``'s own per-field "not allowed to change {field} after
    submission" check for any field lacking ``allow_on_submit`` — the flag's entire, narrow
    purpose. Placed ABOVE every bypass grade this campaign has named (Maintenance Visit's
    ``db_update``, Asset Movement's/Asset Maintenance Log's ``frappe.db.set_value``, Quality
    Inspection's raw SQL into another table).

    **THE SHARPEST FINDING, CORRECTED 2026-07-21 by supervisor verification against a real
    bench read of ``frappe/model/document.py`` + ``frappe/model/delete_doc.py``. The original
    landing's claim — that THIS Delivery Trip's own cancel step raises
    ``ValidationError("Cannot edit cancelled document")`` after the cascade cancels the Delivery
    Note first — is UNREACHABLE. Two facts, read line by line, invert it:**

    **(1) ``on_cancel`` runs BEFORE the back-link check.** ``run_post_save_methods``'s cancel
    branch (``document.py:1450-1452``) calls ``self.run_method("on_cancel")`` THEN
    ``self.check_no_back_links_exist()`` — not the other way round. Consequence: a LEAF cancel of
    THIS Delivery Trip NATIVELY SUCCEEDS even with submitted linked Delivery Notes —
    ``update_delivery_notes(delete=True)`` clears the notes' ``delivery_trip``/``driver``/
    ``vehicle_no``/``lr_no``/``lr_date`` fields via the sanctioned ``note_doc.save()`` (the
    honesty grade above) BEFORE ``check_no_back_links_exist`` -> ``check_if_doc_is_linked`` ever
    runs, so that check finds nothing pointing back at this Delivery Trip. The hook is a designed
    SELF-UNLINKING cancel, not a doomed one.

    **(2) The cascade's OWN note-first step fails at frappe's own gate, before this Delivery
    Trip is ever reached.** Cancelling a Delivery Note while its Delivery Trip is still submitted
    hits ``check_if_doc_is_linked(method="Cancel")`` (``frappe/model/delete_doc.py:301-376``,
    called from ``document.py:1572-1578``), which walks ALL static Link fields including CHILD
    TABLES — the ``meta.istable`` branch (``delete_doc.py:339-340``) adds ``parent``/
    ``parenttype`` and reports the child row's OWN ``docstatus``, which mirrors its submitted
    parent's. ``Delivery Stop.delivery_note`` (``delivery_stop.json``: ``fieldtype: "Link"``,
    ``options: "Delivery Note"``, ``istable: 1`` — confirmed via ``json.load``) is exactly such a
    field, so a submitted Delivery Trip's own ``delivery_stops`` rows keep linking it to the
    note. Delivery Note's own ``on_cancel`` sets ``ignore_linked_doctypes = ("GL Entry", "Stock
    Ledger Entry", "Repost Item Valuation", "Serial and Batch Bundle")``
    (``delivery_note.py:500-505``) — **Delivery Trip is NOT in that tuple** — so cancelling the
    note first, exactly what this broker's own dependents-first cascade order
    (:func:`pacioli.cascade.build_cascade`: "dependent before the document it depends on, target
    LAST") always does, raises ``frappe.LinkExistsError`` at that FIRST step, before this
    Delivery Trip's own cancel step ever runs.

    **Net, honest picture (at landing time, 2026-07-21, before the ruling below):** native leaf
    cancel of a submitted Delivery Trip = ALLOWED, self-unlinking, with a real cross-document
    side effect on submitted Delivery Notes. This broker's OWN leaf ``plan_cancel``, at landing,
    = REFUSED outright by the standing blast-radius check (a submitted dependent exists) —
    stricter than ERPNext in consent. This broker's OWN cascade cancel (the only remaining path
    once the leaf was refused) = FAILS AT EXECUTE at the Delivery Note step with frappe's own
    ``LinkExistsError``, before this Delivery Trip's own cancel step ever runs. **At landing this
    broker had NO WORKING CANCEL PATH for a submitted Delivery Trip with a submitted linked
    Delivery Note.** Not silently patched or reordered at landing (the companyless precedent's
    discipline: name the decision, don't invent the fix solo) — two structural options were
    named, neither chosen at landing: **(a)** a self-unlinking-doctype exception letting the leaf
    ``plan_cancel`` proceed despite the submitted dependent, disclosing the cross-document side
    effect instead of refusing outright; or **(b)** a target-first cascade order exception for
    this one mutual-edge doctype pair (Delivery Trip <-> Delivery Note), reversing
    ``build_cascade``'s own "dependent first" rule for this relationship only.

    **RULED 2026-07-21 — John's ruling 1** (``docs/plans/2026-07-21-cancel-truth-rulings.md``):
    option **(a)**, belted. Delivery Trip is now registered in
    :data:`pacioli.erpnext.SELF_UNLINKING_DOCTYPES`; the leaf ``plan_cancel`` blast-radius gate
    (:meth:`PacioliBroker._tool_plan_cancel`) treats this doctype's own submitted incoming links
    as non-blocking instead of refusing outright — every OTHER doctype's blast-radius refusal
    stays byte-identical (the added condition is scoped to ``SELF_UNLINKING_DOCTYPES``
    membership, never inferred). THE BELT stays: the mechanism/structural disclosures this
    function returns below are unconditionally carried into ``plan_cancel``'s own ``risk_flags``
    before any consent is minted, unchanged. THE SUSPENDERS is new: after a successful execute,
    :func:`_delivery_trip_self_unlink_readback` (wired into
    :meth:`PacioliBroker._governed_write`) re-reads every Delivery Note this draft's own
    ``delivery_stops`` named and ATTESTS the self-unlink actually happened — each note's own
    ``delivery_trip`` field should no longer name the cancelled trip. A note still linked is
    REPORTED in the outcome (``self_unlink_readback``), never silently passed — the cancel
    already happened by the time the readback runs, so this is truth-telling, never a
    retro-refusal. Option (b) was NOT taken: ``plan_cascade_cancel`` is UNCHANGED, still
    structurally dead at the Delivery Note step above (point (2) above), disclosed exactly as
    before — no cascade-order exception was built.

    **The originally-claimed mechanism survives as a SECOND, weaker lock**, unaffected by the
    ruling — pinned separately because it is real, just reachable only in the narrower case of a
    Delivery Note that is ALREADY cancelled independently of this flow (the blast-radius check
    only ever sees SUBMITTED links, so an already-cancelled note was never what the exception
    above needed to let through): ``note_doc.save()`` on that ALREADY-CANCELLED Delivery Note,
    and ``check_docstatus_transition`` (``document.py:1149-1150``) resolves
    ``previous.docstatus == 2`` with an UNCONDITIONAL ``raise
    frappe.ValidationError(_("Cannot edit cancelled document"))``, inside ``check_if_latest``,
    well BEFORE ``validate_update_after_submit`` even runs — so
    ``ignore_validate_update_after_submit`` never reaches a position to protect against it. A
    double lock, both directions receipted, disclosed together — never repaired or reordered;
    the cascade order itself stays correct (blast-radius-first); this is a genuine ERPNext-side
    collision, named plainly."""
    notes = _delivery_trip_linked_delivery_notes(doc)
    if not notes:
        return []
    named = ", ".join(repr(n) for n in notes[:5])
    more = f" (+{len(notes) - 5} more)" if len(notes) > 5 else ""
    return [
        f"cancelling will force-clear driver/driver_name/vehicle_no/delivery_trip/lr_no/lr_date "
        f"on the following linked Delivery Note(s) via note_doc.save() with "
        f"ignore_validate_update_after_submit=True (delivery_trip.py:112-150): {named}{more} — "
        "this is the SANCTIONED update_after_submit save path (permission checks, freshness, "
        "link validation, before/on_update_after_submit hooks, and version history all still "
        "run; only the custom validate() hook and the one allow_on_submit field-lock check are "
        "skipped), never a db_update/db_set/raw-SQL bypass",
        f"STRUCTURAL RISK — CASCADE FAILS AT THE NOTE STEP, NOT THIS DELIVERY TRIP'S OWN STEP: "
        f"this broker's own dependents-first cascade order (pacioli.cascade.build_cascade) "
        f"cancels {named}{more} BEFORE this Delivery Trip whenever this cancel is planned via "
        "plan_cascade_cancel. Cancelling a Delivery Note while its Delivery Trip is still "
        "submitted raises frappe.LinkExistsError at that NOTE step itself — "
        "check_if_doc_is_linked(method='Cancel') (frappe/model/delete_doc.py) walks child-table "
        "Link fields too, and Delivery Stop.delivery_note (istable=1) still points a submitted "
        "Delivery Trip at that Delivery Note; Delivery Note's own on_cancel "
        "ignore_linked_doctypes exemption (delivery_note.py:500-505) does not include Delivery "
        "Trip. The cascade fails at execute at that FIRST node, before this Delivery Trip's own "
        "cancel step ever runs",
        "SECOND LOCK (reachable only if the note step above were somehow bypassed): this "
        "Delivery Trip's own on_cancel would then attempt note_doc.save() on an "
        "ALREADY-CANCELLED Delivery Note, and check_docstatus_transition "
        "(frappe/model/document.py:1149-1150) unconditionally raises ValidationError('Cannot "
        "edit cancelled document') before ignore_validate_update_after_submit ever gets a "
        "chance to matter — a double lock, both directions receipted",
        "LEAF CANCEL ALLOWED under the self-unlinking exception (John's ruling 1, 2026-07-21, "
        "docs/plans/2026-07-21-cancel-truth-rulings.md): Delivery Trip is registered in "
        "SELF_UNLINKING_DOCTYPES, so this broker's leaf plan_cancel no longer refuses outright "
        "on a submitted linked Delivery Note the way an unregistered doctype's blast-radius "
        "check still would — native ERPNext's own on_cancel self-unlinks the note before "
        "frappe's own back-link check ever runs (on_cancel before check_no_back_links_exist, "
        "frappe/model/document.py:1450-1452). THE SUSPENDERS: after a successful execute, this "
        "broker re-reads each linked Delivery Note named above and ATTESTS the self-unlink "
        "actually happened (self_unlink_readback in the outcome payload) — a note still linked "
        "is REPORTED, never silently passed. plan_cascade_cancel remains the ONE structurally "
        "dead path for this pair (fails at the Delivery Note step above, frappe's own "
        "LinkExistsError) — no cascade-order exception was built; ruling 1 took the leaf "
        "exception, option (a), not a cascade reorder, option (b)"]


def _delivery_trip_self_unlink_readback(client, pre_cancel_doc, cancelled_name):
    """John's ruling 1 (2026-07-21, ``docs/plans/2026-07-21-cancel-truth-rulings.md``) — THE
    SUSPENDERS. Wired into :meth:`PacioliBroker._governed_write`, gated on ``op == "cancel"`` and
    ``doctype in SELF_UNLINKING_DOCTYPES`` — a no-op call site for every other doctype and for
    submit, and never reached at all for the SEPARATE ``plan_cascade_cancel``/``cascade_cancel``
    execute path (:meth:`PacioliBroker._tool_cascade_cancel`), which this ruling leaves untouched.

    Runs ONLY after a successful execute (the caller checks ``outcome.ok`` first) — the cancel has
    already landed by the time this function is called, so what follows is truth-telling about
    what happened, never a gate and never a retro-refusal. ``pre_cancel_doc`` is the SAME already-
    fetched document :meth:`PacioliBroker._governed_write` read before calling
    ``client.cancel_document`` (never a second, potentially-stale re-read) — its own
    ``delivery_stops`` rows are the checklist, run through the SAME shared helper
    (:func:`_delivery_trip_linked_delivery_notes`) the belt-side disclosure already uses, so the
    two directions can never drift on which notes count.

    For each named Delivery Note, re-reads it fresh (:meth:`ErpnextClient.get_document`) and
    ATTESTS the self-unlink actually happened: ``update_delivery_notes(delete=True)``
    (``delivery_trip.py:112-150``) sets the note's own ``delivery_trip`` field to ``None``, so a
    clean attestation is ``note.get("delivery_trip") != cancelled_name``. A note that STILL names
    the cancelled trip is collected into ``still_linked`` — REPORTED, never silently passed (the
    SECOND LOCK this doctype's own cancel docstring names is one way this could genuinely happen:
    ``ignore_validate_update_after_submit`` cleared a field this broker didn't ask it to skip, or
    the write raced with something else — the readback exists precisely to catch that rather than
    assume the mechanism always lands). A per-note bench read failure is caught here and NEVER
    allowed to crash past this function or flip the cancel's own already-recorded success — it is
    collected into ``readback_error`` instead, the same "a post-execute belt must never crash the
    flow it's checking" discipline :func:`_record_committed_or_confess`'s own docstring names for
    a sibling case.

    Returns ``{"checked": int, "clean": int, "still_linked": [str, ...]}`` — ``checked`` counts
    only the notes actually read (a failed read is not "checked"); ``readback_error`` is added
    (a list of ``{"delivery_note": name, "error": str}``) only when at least one read failed, so
    the common, clean case carries no extra key at all."""
    notes = _delivery_trip_linked_delivery_notes(pre_cancel_doc)
    checked = 0
    clean = 0
    still_linked = []
    errors = []
    for note_name in notes:
        try:
            note_doc = client.get_document(DELIVERY_NOTE, note_name)
        except Exception as exc:  # noqa: BLE001 — a readback-side failure must never crash the cancel
            errors.append({"delivery_note": note_name, "error": str(exc)})
            continue
        checked += 1
        if note_doc.get("delivery_trip") == cancelled_name:
            still_linked.append(note_name)
        else:
            clean += 1
    result = {"checked": checked, "clean": clean, "still_linked": still_linked}
    if errors:
        result["readback_error"] = errors
    return result


def _asset_value_adjustment_ledger_preview_unavailable_flag(doctype):
    """Asset Value Adjustment breadth (2026-07-21) — the same "uncallable" shape, a bare
    ``Document`` MRO. Fires unconditionally for ``doctype == ASSET_VALUE_ADJUSTMENT``,
    submit-direction only.

    ``class AssetValueAdjustment(Document):`` (``asset_value_adjustment.py:21``) — a DIRECT
    ``frappe.model.document.Document`` subclass, never ``AccountsController``/
    ``StockController``. A full-file grep finds no ``make_gl_entries``/``make_sl_entries``/
    ``GLEntry`` reference anywhere. ERPNext's own ``get_accounting_ledger_preview`` calls
    ``doc.make_gl_entries()`` bare and unguarded — ``AttributeError`` on a live bench, so
    ``_tool_plan_submit`` skips the network call entirely (joining the skip tuple, now its
    TWENTY-FIRST member).

    **The irony this doctype makes worth naming explicitly:** unlike every other UNCALLABLE row
    in this table (Dunning, LCV, …), an empty preview here does NOT mean "nothing will post to
    the ledger" — it means this DOCUMENT posts nothing under its OWN name, while submitting it
    DOES post real GL, synchronously, through the sibling Journal Entry
    ``make_asset_revaluation_entry`` builds and submits (see
    :func:`_asset_value_adjustment_submit_risk_flags`)."""
    if doctype != ASSET_VALUE_ADJUSTMENT:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Asset Value Adjustment has no make_gl_entries method anywhere in its class "
        "hierarchy (AssetValueAdjustment -> Document directly) — ERPNext's own preview RPC "
        "would call doc.make_gl_entries() as a bare method call and raise AttributeError on a "
        "live bench, so this broker does not call it for an Asset Value Adjustment at all. "
        "Unlike other UNCALLABLE rows, this document DOES cause real GL to post on submit — "
        "through a synchronously-created sibling Journal Entry (the revaluation entry), never "
        "under this document's own name; see the sibling-Journal-Entry disclosure for what "
        "that entry will contain"]


def _asset_value_adjustment_submit_risk_flags(doc):
    """Asset Value Adjustment breadth (2026-07-21) — the submit-direction disclosures for the
    campaign's FIRST sibling-document FACTORY row. Doctype-scoped at the call site (fired only
    for ``doctype == ASSET_VALUE_ADJUSTMENT``); data-driven where the draft's own fields decide
    it, unconditional prose where a live read of another document (the linked Asset) would be
    needed and this plan cannot perform one. Source pins live in ``erpnext.py``'s module
    docstring (``asset_value_adjustment.py``'s ``on_submit``/``make_asset_revaluation_entry``/
    ``update_asset``, ``asset_depreciation_schedule.py``'s ``reschedule_depreciation``,
    ``accounting_period.py``'s ``get_doctypes_for_closing``, and frappe's ``document.py`` for the
    ``ignore_permissions`` mechanics).

    Unlike every prior UNCALLABLE row, this doctype's own preview being empty does NOT mean
    nothing posts — see :func:`_asset_value_adjustment_ledger_preview_unavailable_flag`."""
    flags = []
    # (1) The synchronous sibling Journal Entry — unconditional, always fires on a real submit.
    # Contrast with Asset's own SCHEDULED depreciation JEs: THIS channel is fully inside the same
    # call the broker's own marker already governs (the JE's name is written back via db_set
    # before on_submit returns), a materially weaker disclosure need than a channel that fires
    # later outside any call this broker makes — still named for completeness.
    flags.append(
        "submitting this Asset Value Adjustment auto-creates AND submits a Journal Entry (the "
        "revaluation entry, make_asset_revaluation_entry) — SYNCHRONOUSLY, inside this same "
        "on_submit call, never a scheduler channel: the JE's own name is written back onto this "
        "document's journal_entry field (self.db_set) before on_submit returns, so this "
        "submit's own outcome already discloses the sibling's name — unlike Asset's own "
        "SCHEDULED depreciation Journal Entries, nothing here arrives later outside a call this "
        "broker makes. je.flags.ignore_permissions=True is set before je.submit() — the ACL "
        "permission gate is skipped for the JE's own submit (has_permission short-circuits for "
        "ANY permtype, document.py:407-408), while every hook/validation/version-history check "
        "on the JE's own submit runs normally")
    # (2) Doomed-submit: a blank company dooms the sibling JE's own submit (JE.company is reqd),
    # deterministic off THIS draft's own company field even though AVA's own schema never
    # requires it.
    if not doc.get("company"):
        flags.append(
            "submit will be REFUSED: no company is set on this Asset Value Adjustment — its own "
            "schema never requires company, but make_asset_revaluation_entry sets je.company = "
            "self.company unconditionally (asset_value_adjustment.py:102), and Journal Entry's "
            "own company field IS mandatory (reqd:1) — so the synchronously-built sibling "
            "Journal Entry's own submit will fail with frappe's MandatoryError, dooming this "
            "submit even though nothing on THIS document's own validation would catch it")
    # (3) The closed-books scope gap — unconditional prose: this doctype can never itself carry a
    # doctype-specific period lock, but the sibling JE genuinely can, on the identical date value.
    flags.append(
        "this broker's own closed-books check can never find a doctype-specific lock for Asset "
        "Value Adjustment (it is not a period_closing_doctypes member, and ERPNext's own "
        "Accounting Period UI restricts closeable doctypes to that list — get_doctypes_for_"
        "closing) — but the sibling Journal Entry this submit creates IS such a member, and its "
        "own native period-closing gate (validate_accounting_period_on_doc_save, fired by "
        "je.submit()) checks a real, independently-configurable lock against the IDENTICAL date "
        "value (je.posting_date = self.date, a direct copy). A period closed specifically for "
        "Journal Entry (closing one doctype for a period while others stay open is the "
        "feature's own purpose) can let this plan read 'ok' while the synchronously-created "
        "sibling Journal Entry is refused at execute by ERPNext's own native gate — this plan "
        "only ever queries locks for the doctype it was asked to plan, never for a sibling it "
        "does not yet know it will create")
    # (4) validate_date needs a live read of the linked Asset's own purchase_date — cross-
    # document state, prose per standing discipline, never a guessed flag.
    flags.append(
        "validate_date compares this document's own date against the linked Asset's own "
        "purchase_date (asset_value_adjustment.py:49-57) — a live read of another document this "
        "plan cannot perform; submit is refused if date falls before the asset's purchase date")
    # (5) The depreciation-reschedule sibling-document channel — needs the linked Asset's own
    # calculate_depreciation/finance_books state, prose per standing discipline (the Asset
    # Movement "monotonic validate needs other movements' state" shape).
    flags.append(
        "if the linked Asset has calculate_depreciation set, update_asset() ALSO reschedules "
        "that asset's depreciation via reschedule_depreciation (asset_depreciation_schedule.py:"
        "194-219), synchronously, inside this same submit: an existing submitted (Active) Asset "
        "Depreciation Schedule for the matching finance book is CANCELLED (with "
        "should_not_cancel_depreciation_entries=True — its own already-POSTED depreciation "
        "Journal Entries are explicitly preserved, never touched) and a brand-new one is "
        "CREATED AND SUBMITTED in its place — a THIRD sibling submittable document this submit "
        "arms, alongside the revaluation Journal Entry above. This plan cannot read the linked "
        "Asset's own calculate_depreciation flag to confirm whether this channel fires")
    # (6) The raw-write bypass on the linked Asset itself — unconditional, always fires (the
    # value/status writes happen regardless of calculate_depreciation).
    flags.append(
        "update_asset() also writes value_after_depreciation onto the linked Asset (and its "
        "finance_books child rows, when calculate_depreciation is set) via raw ORM db_update() "
        "calls — no validate(), no hooks, no version history (the Maintenance Visit bypass "
        "grade) — then calls Asset.set_status(), a raw db_set (the Asset Movement/Asset "
        "Maintenance Log bypass grade: before_change/on_change hooks fire, validate/permission/"
        "version-history do not)")
    return flags


def _asset_value_adjustment_cancel_risk_flags(doc):
    """Asset Value Adjustment breadth (2026-07-21) — the cancel-direction disclosures.
    Doctype-scoped at the call site; the sibling-JE bypass grade is data-driven off the draft's
    own ``journal_entry`` field (present on any genuinely-submitted AVA, since
    ``make_asset_revaluation_entry`` writes it back before ``on_submit`` returns); the
    depreciation-reschedule and raw-write disclosures are unconditional prose, the same
    mechanism ``update_asset()`` shares with the submit direction (sign-flipped internally by
    ``self.docstatus``, never by this disclosure layer)."""
    flags = []
    je_name = doc.get("journal_entry")
    if je_name:
        flags.append(
            f"cancelling this Asset Value Adjustment will cancel its own linked Journal Entry "
            f"{je_name!r} — via the JE's OWN full cancel() lifecycle (docstatus-transition "
            "validation, before_cancel/on_cancel hooks, check_no_back_links_exist, "
            "notifications, and version history ALL run normally) with ONLY the permission "
            "check skipped: revaluation_entry.flags.ignore_permissions = True "
            "(asset_value_adjustment.py:177) makes Document.has_permission return True "
            "unconditionally for ANY permission type (document.py:407-408), short-circuiting "
            "BOTH the generic write-permission check and the dedicated cancel-permission check "
            "— a PERMISSION-ONLY bypass, distinct in KIND (not merely degree) from every "
            "data-integrity bypass this campaign has named (db_update/db_set/raw SQL all skip "
            "hooks and version history too) and narrower than Delivery Trip's own sanctioned "
            "update_after_submit save (which still waives validate() and one field lock) — this "
            "waives ONLY authorization, nothing about the JE's own data-integrity checks")
    else:
        flags.append(
            "no linked Journal Entry is recorded on this draft (journal_entry is blank) — "
            "cancel_asset_revaluation_entry returns immediately; nothing to cancel on that side")
    flags.append(
        "if the linked Asset has calculate_depreciation set, update_asset() ALSO reschedules "
        "that asset's depreciation via reschedule_depreciation, synchronously, inside this same "
        "cancel: an existing submitted (Active) Asset Depreciation Schedule for the matching "
        "finance book is CANCELLED (already-posted depreciation Journal Entries preserved via "
        "should_not_cancel_depreciation_entries) and a new one CREATED AND SUBMITTED in its "
        "place, recalculated for the reversed value — this plan cannot read the linked Asset's "
        "own calculate_depreciation flag to confirm whether this channel fires")
    flags.append(
        "update_asset() also reverses value_after_depreciation on the linked Asset via raw ORM "
        "db_update() calls (no validate/hooks/version history — the Maintenance Visit bypass "
        "grade) and calls Asset.set_status(), a raw db_set (the Asset Movement/Asset "
        "Maintenance Log bypass grade)")
    return flags


def _payment_order_ledger_preview_unavailable_flag(doctype):
    """Payment Order breadth (2026-07-21) — the same "uncallable" shape, a bare ``Document`` MRO.
    Fires unconditionally for ``doctype == PAYMENT_ORDER``, submit-direction only.

    ``class PaymentOrder(Document):`` (``payment_order.py:13``) — a DIRECT
    ``frappe.model.document.Document`` subclass. A full-file grep of ``payment_order.py`` finds
    no ``make_gl_entries``/``make_sl_entries``/``GLEntry`` reference anywhere. ERPNext's own
    ``get_accounting_ledger_preview`` calls ``doc.make_gl_entries()`` bare and unguarded —
    ``AttributeError`` on a live bench for a real Payment Order, so ``_tool_plan_submit`` skips
    the network call entirely for this doctype too (joining the skip tuple, now its
    TWENTY-SECOND member).

    Payment Order posts no GL/Stock Ledger row of any kind under its own name — the ledger
    consequence of a payment lives entirely in the referenced Payment Entry/Payment Request/
    Journal Entry documents, never disclosed here (see
    :func:`_payment_order_write_risk_flags` for what it DOES do to those documents)."""
    if doctype != PAYMENT_ORDER:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Payment Order has no make_gl_entries method anywhere in its class hierarchy "
        "(PaymentOrder -> Document directly) — ERPNext's own preview RPC would call "
        "doc.make_gl_entries() as a bare method call and raise AttributeError on a live bench, "
        "so this broker does not call it for a Payment Order at all. Payment Order posts no GL "
        "or Stock Ledger entry of any kind under its own name — it only writes a status field "
        "onto referenced Payment Request/Payment Entry documents, never an accounting document "
        "in its own right"]


def _payment_order_write_risk_flags(doc, op):
    """Payment Order breadth (2026-07-21) — THE central finding, fired on BOTH directions (the
    Asset Movement ``op``-parameterized shape — though NOT textually identical calls the way that
    precedent is: ``on_submit`` calls ``self.update_payment_status()``, ``on_cancel`` calls
    ``self.update_payment_status(cancel=True)``, one shared implementation behind a boolean
    argument rather than two byte-identical hook bodies). Doctype-scoped at the call site; fully
    data-driven off the draft's own ``references``/``payment_order_type`` fields — no live read
    needed to name which documents get touched.

    THE MECHANISM, read line by line (``payment_order.py:44-57``): for EVERY row in
    ``self.references``, a raw ``frappe.db.set_value`` (line 57) writes a status value directly
    onto ANOTHER document — confirmed from ``frappe.db.set_value``'s own docstring
    (``frappe/database/database.py:934-953``: "do not call the ORM triggers... will not call
    Document events") — no ``validate()``, no hooks, no version history, no permission check on
    the target (the Asset Movement/Asset Maintenance Log bypass grade). The target
    doctype/field/child-column is chosen by ``self.payment_order_type``:
      * ``"Payment Request"``: writes ``status`` onto the Payment Request named by that row's own
        ``payment_request`` field (``ref_doc_field = frappe.scrub(self.payment_order_type)`` =
        ``"payment_request"``).
      * ``"Payment Entry"``: writes ``payment_order_status`` onto the Payment Entry named by that
        row's own ``reference_name`` field.
    On submit the value is ``"Payment Ordered"``; on cancel it is UNCONDITIONALLY ``"Initiated"``
    — confirmed no read of the target's current value anywhere in the method.

    THE SHARPEST DISCLOSURE, cancel direction + Payment-Request-typed rows only: Payment
    Request's own ``status`` field (``payment_request.json``) carries SIX further values beyond
    "Initiated" (``Requested``/``Partially Paid``/``Payment Ordered``/``Paid``/``Failed``/
    ``Cancelled``) — because the write never reads the target's current state first, a Payment
    Request that has since moved to "Paid" or "Partially Paid" (via an unrelated Payment Entry)
    is stomped back to "Initiated" by this cancel regardless of that later state. Payment Entry's
    own mirror field (``payment_order_status``) carries only the same two values
    (``Initiated``/``Payment Ordered``) this write ever produces — no equivalent loss on that
    branch, so the extra sentence is gated to the Payment Request case only."""
    refs = [r for r in (doc.get("references") or []) if isinstance(r, dict)]
    po_type = doc.get("payment_order_type")
    if po_type == "Payment Request":
        target_doctype, ref_field, key = "Payment Request", "status", "payment_request"
    elif po_type == "Payment Entry":
        target_doctype, ref_field, key = "Payment Entry", "payment_order_status", "reference_name"
    else:
        # payment_order_type is reqd on the schema (Select, reqd:1); an unset value on a real
        # draft is an authoring gap this plan discloses rather than guesses a target doctype for.
        return [
            "payment_order_type is not set on this draft — update_payment_status cannot resolve "
            "which doctype/field its raw frappe.db.set_value write targets; ERPNext's own schema "
            "requires this field (reqd:1), so a genuinely submittable Payment Order always "
            "carries one of 'Payment Request'/'Payment Entry'"]
    names = [r.get(key) for r in refs if isinstance(r, dict) and r.get(key)]
    if not names:
        return []
    value = "Payment Ordered" if op == "submit" else "Initiated"
    named = ", ".join(repr(n) for n in names[:5])
    more = f" (+{len(names) - 5} more)" if len(names) > 5 else ""
    verb = "submitting" if op == "submit" else "cancelling"
    flags = [
        f"{verb} this Payment Order writes {ref_field}={value!r} directly onto {len(names)} "
        f"referenced {target_doctype}(s) ({named}{more}) via raw frappe.db.set_value "
        "(payment_order.py:57) — no validate(), no hooks, no version history, no permission "
        "check on the target document"]
    if op == "cancel" and target_doctype == "Payment Request":
        flags.append(
            "this write is UNCONDITIONAL — update_payment_status never reads the Payment "
            "Request's current status first, so a request that has since moved to 'Paid' or "
            "'Partially Paid' (via an unrelated Payment Entry) is stomped back to 'Initiated' by "
            "this cancel regardless of that later state")
    return flags


def _share_transfer_ledger_preview_unavailable_flag(doctype):
    """Share Transfer breadth (2026-07-21) — the same "uncallable" shape, a bare ``Document`` MRO.
    Fires unconditionally for ``doctype == SHARE_TRANSFER``, submit-direction only.

    ``class ShareTransfer(Document):`` (``share_transfer.py:17``) — a DIRECT
    ``frappe.model.document.Document`` subclass. A full-file grep of ``share_transfer.py`` finds
    no ``make_gl_entries``/``make_sl_entries``/``GLEntry`` reference anywhere. ERPNext's own
    ``get_accounting_ledger_preview`` calls ``doc.make_gl_entries()`` bare and unguarded —
    ``AttributeError`` on a live bench for a real Share Transfer, so ``_tool_plan_submit`` skips
    the network call entirely for this doctype too (joining the skip tuple, now its
    TWENTY-THIRD member).

    Share Transfer posts no GL or Stock Ledger entry of any kind, sibling or otherwise — confirmed,
    no Journal Entry is ever ``.insert()``ed or ``.submit()``ed by ``on_submit``/``on_cancel``;
    ``make_jv_entry`` is a manual-UI-only draft-returning helper (module-level
    ``@frappe.whitelist()``, never called automatically). Its real side effects are share_balance
    child-table writes and ``folio_no`` writes on Shareholder documents, never disclosed here (see
    :func:`_share_transfer_submit_risk_flags`/:func:`_share_transfer_cancel_risk_flags`)."""
    if doctype != SHARE_TRANSFER:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Share Transfer has no make_gl_entries method anywhere in its class hierarchy "
        "(ShareTransfer -> Document directly) — ERPNext's own preview RPC would call "
        "doc.make_gl_entries() as a bare method call and raise AttributeError on a live bench, "
        "so this broker does not call it for a Share Transfer at all. Share Transfer posts no GL "
        "or Stock Ledger entry of any kind, sibling or otherwise — it only writes share_balance "
        "and folio_no fields onto Shareholder documents, never an accounting document"]


def _share_transfer_submit_risk_flags(doc):
    """Share Transfer breadth (2026-07-21) — the submit-direction disclosure, entirely data-driven
    off the draft's own ``transfer_type``/``from_folio_no``/``to_folio_no``/``from_shareholder``
    fields; doctype-scoped at the call site (fired only for ``doctype == SHARE_TRANSFER``); no
    live Shareholder read needed for the deterministic parts.

    THE SHARPEST FINDING — a doomed-submit gate the pre-verification addendum's own finding #3
    described too generically to catch: ``basic_validations()``'s PURCHASE branch
    (``share_transfer.py:170-177``) blanks ``self.to_shareholder`` to ``""`` at line 171, THEN —
    if ``self.from_folio_no`` is blank — calls ``self.autoname_folio(self.to_shareholder)`` at
    lines 174-175, passing the JUST-BLANKED field (never ``self.from_shareholder``, the real
    populated field for a Purchase). Traced the full chain, source-verified end to end:
      1. ``autoname_folio("")`` (share_transfer.py:242-249) calls ``self.get_shareholder_doc("")``.
      2. ``get_shareholder_doc("")`` (share_transfer.py:317-324) queries
         ``frappe.db.get_value("Shareholder", {"name": ""}, "name")`` — no Shareholder is ever
         named the empty string (``naming_series`` ``ACC-SH-.YYYY.-``), so this returns ``None`` —
         then calls ``frappe.get_doc("Shareholder", None)``.
      3. ``frappe.get_doc("Shareholder", None)`` dispatches to
         ``Document.__init__("Shareholder", None)`` (``frappe/model/document.py:140-146,
         206-221``) — ``self.name`` is set to the real value ``None`` (two positional args, not
         the Single-doctype special case), then ``load_from_db()`` runs.
      4. ``load_from_db()`` (``frappe/model/document.py:252-297``): ``self.name`` (``None``) fails
         ``isinstance(self.name, str | int)`` (line 271), so it falls to
         ``frappe.db.get_value(doctype="Shareholder", filters=None, fieldname="*", ...)``
         (document.py:286-291).
      5. ``get_values`` (``frappe/database/database.py:663-705``): ``filters is None`` fails the
         query branch's own condition outright (line 663 — NOT an arbitrary/most-recent row);
         Shareholder is not a Single doctype (line 691's ``elif`` fails too); the final ``else``
         (lines 704-705) returns ``None`` unconditionally.
      6. Back in ``load_from_db``, ``d`` is ``None`` -> ``frappe.throw(_("{0} {1} not found")
         .format(...), frappe.DoesNotExistError(doctype="Shareholder"))`` (document.py:294-297).
    CONCLUSION: any Purchase-type Share Transfer with a POPULATED ``from_shareholder`` (past the
    earlier blank-check, share_transfer.py:172-173) but a blank ``from_folio_no`` THROWS
    ``frappe.DoesNotExistError`` before any share_balance mutation runs, ever — genuinely
    reachable: ``from_folio_no``'s only auto-fill is a CLIENT-SIDE ``fetch_from:
    "from_shareholder.folio_no"`` (``share_transfer.json``), which stays blank whenever the
    seller's own Shareholder record has never had a folio_no assigned, or whenever the document is
    authored via the REST API bypassing the Desk form's fetch — exactly the channel this broker's
    own credential uses. Deterministic straight off this draft's own two fields.

    The Issue branch (lines 178-185) and the Transfer/else branch (186-190) carry NO equivalent
    bug — both correctly check ``to_folio_no`` and call ``autoname_folio(self.to_shareholder)`` on
    the REAL, non-blanked ``to_shareholder`` when it's blank, assigning a fresh folio number via a
    full ``.save()`` (autoname_folio, lines 242-249).

    ``folio_no_validation()`` (share_transfer.py:222-240), called immediately after
    ``basic_validations()`` inside ``validate()``, runs a SECOND independent check-or-write pass
    over whichever of ``from_shareholder``/``to_shareholder`` survives this transfer_type's own
    blanking — if that Shareholder's own ``folio_no`` is currently blank, it is set and
    ``.save()``d there too; this needs the target Shareholder's live state to know for certain, so
    it is disclosed as prose, not gated on a data-driven condition. Consequence, per the addendum:
    a Share Transfer that is only SAVED as a draft — never submitted — can already mutate a
    Shareholder document, since ``validate()`` runs on every ``Document.save()``, not just submit.

    The on_submit share_balance mutation itself (lines 45-95), branch by branch — every write a
    plain, hook-respecting ``.save()``/``.insert()`` on Shareholder (not submittable, per the
    addendum's correction #2 — no docstatus, no auto-submit):
      * Issue: ``get_company_shareholder()`` (auto-``.insert()``s a NEW company Shareholder if
        none exists for this company) appends a share_balance row and ``.save()``s; then
        ``to_shareholder``'s own Shareholder doc appends a row and ``.save()``s.
      * Purchase: ``remove_shares(from_shareholder)`` then ``remove_shares(company_shareholder)``
        — rewrites each target's share_balance child table in place and ``.save()``s, no new
        documents.
      * Transfer: ``remove_shares(from_shareholder)`` then ``to_shareholder``'s Shareholder doc
        appends a row and ``.save()``s."""
    flags = []
    transfer_type = doc.get("transfer_type")
    if (transfer_type == "Purchase" and doc.get("from_shareholder")
            and not doc.get("from_folio_no")):
        flags.append(
            "submit will be REFUSED: this is a Purchase-type transfer with a populated "
            "from_shareholder but a blank from_folio_no — basic_validations() checks "
            "from_folio_no but then calls autoname_folio(self.to_shareholder) using "
            "to_shareholder, which this SAME branch just blanked to '' one line earlier "
            "(share_transfer.py:171,174-175) — the resulting frappe.get_doc('Shareholder', "
            "None) throws frappe.DoesNotExistError('Shareholder None not found') "
            "(frappe/model/document.py:294-297) before any share_balance mutation runs")
    elif transfer_type in ("Issue", "Transfer") and not doc.get("to_folio_no"):
        flags.append(
            "submitting will assign a new auto-generated folio_no to the to_shareholder "
            "Shareholder via a full .save() (autoname_folio, share_transfer.py:242-249) — "
            "to_folio_no is blank on this draft, so basic_validations() fills it in before the "
            "share_balance mutation runs")
    flags.append(
        "validate() also runs folio_no_validation() (share_transfer.py:222-240) on every save, "
        "not only submit — for whichever of from_shareholder/to_shareholder survives this "
        "transfer_type's own blanking, if that Shareholder's own folio_no is currently blank it "
        "is set and .save()d there too; this needs the target Shareholder's live state, not "
        "readable from this draft alone")
    if transfer_type == "Issue":
        flags.append(
            "submitting will append a share_balance row to the company's own Shareholder "
            "(auto-.insert()ing a NEW one first if this company has none yet) and to the "
            "to_shareholder's own Shareholder, each a plain .save()/.insert() — Shareholder "
            "carries no is_submittable key at all, so neither write is itself a submit")
    elif transfer_type == "Purchase":
        flags.append(
            "submitting will rewrite the share_balance child table on both from_shareholder's "
            "and the company's own Shareholder via remove_shares() + .save() — no new documents "
            "created")
    elif transfer_type == "Transfer":
        flags.append(
            "submitting will rewrite from_shareholder's share_balance via remove_shares() + "
            ".save(), then append a share_balance row to to_shareholder's own Shareholder via a "
            "plain .save()")
    return flags


def _share_transfer_cancel_risk_flags(doc):
    """Share Transfer breadth (2026-07-21) — the cancel-direction disclosure. ``on_cancel``
    (share_transfer.py:97-149) carries ZERO ``frappe.throw`` calls and never calls ``validate()``
    — Frappe's own cancel lifecycle runs ``before_cancel``/``on_cancel``, not ``validate()``
    (confirmed: all 11 throws in this class live inside ``validate()``'s own call graph,
    ``basic_validations()``+``folio_no_validation()``, never ``on_cancel``) — so none of the
    submit-direction doomed-gate disclosures above apply here; structurally, ``on_cancel`` mirrors
    ``on_submit`` exactly, branch for branch, in reverse.

    THE UNGUARDED RISK (data-dependent, not deterministic from this draft — disclosed as prose per
    the template, not a gated flag, the addendum's own landing risk #6): every ``on_cancel``
    branch calls ``remove_shares()``/``get_shareholder_doc()`` (share_transfer.py:251-324) on the
    SAME ``from_shareholder``/``to_shareholder``/company-Shareholder names this document's own
    ``on_submit`` touched — each resolves via ``frappe.get_doc("Shareholder", shareholder)``,
    which throws ``frappe.DoesNotExistError`` if that Shareholder has since been deleted or
    renamed. Real, but data-dependent (needs live Shareholder existence, not readable from this
    draft alone)."""
    flags = [
        "on_cancel calls remove_shares()/get_shareholder_doc() on the same Shareholder names "
        "this document's own on_submit touched (frappe.get_doc('Shareholder', shareholder), "
        "share_transfer.py:251-324) — if any of those Shareholders has since been deleted or "
        "renamed, cancel throws frappe.DoesNotExistError; this is data-dependent, not readable "
        "from this draft alone"]
    transfer_type = doc.get("transfer_type")
    if transfer_type == "Issue":
        flags.append(
            "cancelling will rewrite the share_balance child table on both the company's own "
            "Shareholder and to_shareholder's own Shareholder via remove_shares() + .save() — "
            "reverses the on_submit append, no documents deleted")
    elif transfer_type == "Purchase":
        flags.append(
            "cancelling will append a share_balance row back onto from_shareholder's and the "
            "company's own Shareholder via a plain .save() each — reverses the on_submit "
            "remove_shares(), restoring the seller's and the company's holdings")
    elif transfer_type == "Transfer":
        flags.append(
            "cancelling will rewrite to_shareholder's share_balance via remove_shares() + "
            ".save(), then append a share_balance row back onto from_shareholder's own "
            "Shareholder via a plain .save()")
    return flags


def _bom_creator_ledger_preview_unavailable_flag(doctype):
    """BOM Creator breadth (2026-07-21) — the same "uncallable" shape, a bare ``Document`` MRO.
    Fires unconditionally for ``doctype == BOM_CREATOR``, submit-direction only.

    ``class BOMCreator(Document):`` (``bom_creator.py:37``) — a DIRECT
    ``frappe.model.document.Document`` subclass. A full-file grep of ``bom_creator.py`` finds no
    ``make_gl_entries``/``make_sl_entries``/``GLEntry`` reference anywhere. ERPNext's own
    ``get_accounting_ledger_preview`` calls ``doc.make_gl_entries()`` bare and unguarded —
    ``AttributeError`` on a live bench for a real BOM Creator, so ``_tool_plan_submit`` skips the
    network call entirely for this doctype too (joining the skip tuple, now its TWENTY-FOURTH
    member). BOM Creator posts no GL or Stock Ledger entry of any kind, ever — its only products
    are BOM documents (manufacturing, not accounting); see :func:`_bom_creator_submit_risk_flags`
    for what it actually does on submit."""
    if doctype != BOM_CREATOR:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: BOM Creator has no make_gl_entries method anywhere in its class hierarchy "
        "(BOMCreator -> Document directly) — ERPNext's own preview RPC would call "
        "doc.make_gl_entries() as a bare method call and raise AttributeError on a live bench, "
        "so this broker does not call it for a BOM Creator at all. BOM Creator posts no GL or "
        "Stock Ledger entry of any kind — its only products are BOM documents, built LATER by a "
        "background worker (see the queued_consequence disclosure on this same submit)"]


def _bom_creator_submit_risk_flags(doc):
    """BOM Creator breadth (2026-07-21) — the submit-direction disclosure for John's ruling 2, the
    two-phase PROVE ("truth always"; docs/plans/2026-07-21-cancel-truth-rulings.md), off the
    design study (docs/plans/dossiers/bom_creator.design.md, option (b)). Doctype-scoped at the
    call site (fired only for ``doctype == BOM_CREATOR``); both flags are deterministic off the
    draft's own fields, never a live read.

    THE CENTRAL FINDING (unconditional — fires on every BOM Creator submit): ``on_submit``
    (bom_creator.py:247-248) enqueues ``create_boms()`` via ``frappe.enqueue(..., queue="short",
    timeout=600, is_async=True)`` (bom_creator.py:255-261) — the docstatus transition this submit
    confirms IS real (before_submit's own set_status() already flipped ``status`` to "Submitted"
    inline, synchronously, bom_creator.py:133-142/163-165, BEFORE the enqueue even runs), but the
    actual BOM tree is built LATER, by a background worker, outside this consent marker's
    immediate response. This broker never claims a plain "committed" for that unseen work: the
    outcome this submit records is narrowed to "committed_pending_async" (see
    ``pacioli.tools._QueuedConsequenceEffects``), and a later reconciliation sweep
    (``prove_verify``) attests the real Completed/Failed result once the worker lands — call it
    again, or read this document's own status/error_log directly, to learn the outcome.

    THE AMEND-CYCLE FINDING (conditional — fires only when this draft is itself an amendment):
    ``create_bom()``'s own idempotency guard (bom_creator.py:328-337) is keyed to ``self.name`` —
    the CURRENT document's own name. An amended draft is a brand-new document under a new name
    (see ``pacioli.amend``), so the guard has never seen it and cannot recognize the original
    tree as already built: submitting an amended BOM Creator builds a FULL SECOND BOM tree,
    linked to the new creator's name, with nothing in ERPNext or this broker detecting or
    flagging that it duplicates the first tree's work."""
    flags = [
        "submitting this BOM Creator ENQUEUES create_boms() on frappe's 'short' queue (timeout "
        "600s, bom_creator.py:255-261) — the docstatus transition this submit confirms is real, "
        "but the BOM tree itself is built LATER by a background worker, outside this response; "
        "the recorded outcome is 'committed_pending_async', not 'committed', until a later "
        "prove_verify sweep resolves the real Completed (with the built BOM names) or Failed "
        "(with error_log) result — or still pending, if the worker hasn't landed it yet"]
    if doc.get("amended_from"):
        flags.append(
            f"this draft is an amendment of {doc.get('amended_from')!r}: create_bom()'s own "
            "idempotency guard is keyed to THIS document's own name (bom_creator.py:328-337), "
            "which the guard has never seen — submitting will build a FULL SECOND BOM tree, "
            "duplicating whatever the original creator already built, with nothing in ERPNext "
            "or this broker detecting or flagging the duplication")
    return flags


def _bom_creator_cancel_risk_flags(doc):
    """BOM Creator breadth (2026-07-21) — the cancel-direction disclosure, John's ruling 2.
    Doctype-scoped at the call site; data-driven off the draft's own ``status`` field.

    ``on_cancel`` (bom_creator.py:160-161) is synchronous — a single ``self.set_status(True)``
    db_set, nothing enqueued — but it does NOT cascade to any BOMs ``create_boms()`` already
    built: those BOM documents remain submitted, still pointing at the now-cancelled creator via
    their own ``bom_creator`` field (dossier §7/§8, confirmed). THE SHARPER CONSEQUENCE, verified
    against frappe's own ``check_no_back_links_exist``/``check_if_doc_is_linked``
    (document.py:1450-1452, delete_doc.py:301-320): a full two-checkout grep for ``'"options":
    "BOM Creator"'`` finds exactly one real incoming edge, BOM's own ``bom_creator`` Link
    (bom.json:600-608) — BOM Creator is NOT registered in :data:`pacioli.erpnext.
    SELF_UNLINKING_DOCTYPES` (its ``on_cancel`` never clears that field the way Delivery Trip's
    ``update_delivery_notes`` does), so once at least one submitted BOM exists under this
    creator's name, this broker's OWN blast-radius gate (the same one every other doctype's leaf
    ``plan_cancel`` already runs) refuses the leaf cancel outright — ``plan_cascade_cancel`` is
    the governed path (cancel the built BOM(s), themselves a governable row, before the creator).
    ``status`` is the best PLAN-TIME signal of whether that's live: any value past "Submitted"
    means ``create_boms()`` has started, and even a "Failed" status can carry a real PARTIAL
    tree (the design study's own partial-tree finding), so this flag fires generously rather than
    trying to predict the live blast-radius read exactly."""
    flags = [
        "cancelling this BOM Creator is synchronous (no enqueue) but does NOT cascade to any "
        "BOMs create_boms() already built — those BOM documents remain submitted, still "
        "pointing at this now-cancelled creator via their own bom_creator field; they are "
        "governable rows in their own right (BOM is a landed doctype)"]
    status = str(doc.get("status") or "")
    if status not in ("Draft", "Submitted"):
        flags.append(
            f"status is {status!r}: create_boms() has started (or finished) — if it built even "
            "one submitted BOM, this broker's own blast-radius gate will refuse a leaf cancel "
            "of this creator (BOM's own bom_creator Link is a real, submitted incoming link, "
            "and this doctype is NOT self-unlinking) — plan_cascade_cancel is the path if so")
    return flags


_BUDGET_AXES = (
    ("Material Request", "applicable_on_material_request",
     "action_if_annual_budget_exceeded_on_mr",
     "action_if_accumulated_monthly_budget_exceeded_on_mr"),
    ("Purchase Order", "applicable_on_purchase_order",
     "action_if_annual_budget_exceeded_on_po",
     "action_if_accumulated_monthly_budget_exceeded_on_po"),
    ("Booking Actual Expenses", "applicable_on_booking_actual_expenses",
     "action_if_annual_budget_exceeded",
     "action_if_accumulated_monthly_budget_exceeded"),
    ("Cumulative Expense", "applicable_on_cumulative_expense",
     "action_if_annual_exceeded_on_cumulative_expense",
     "action_if_accumulated_monthly_exceeded_on_cumulative_expense"),
)


def _budget_armed_axes(doc):
    """Budget breadth (2026-07-21) — the four ``applicable_on_*`` axes this doctype can arm,
    each read directly off the DRAFT's own Check field (never assumed from the schema's own
    ``default``), paired with its own two ``action_if_*`` Select fields (also read directly off
    the draft, never assumed — the ``action_if_*_on_cumulative_expense`` pair carries NO schema
    default at all, confirmed by a direct ``json.load`` of ``budget.json``, unlike the other three
    axes' own confirmed ``Stop``/``Warn`` defaults). Shared by both
    :func:`_budget_submit_risk_flags` and :func:`_budget_cancel_risk_flags` so the two directions
    describe the identical armed set, never independently re-derived. Returns a list of
    ``(label, annual_action, monthly_action)`` tuples for every axis whose own Check field is
    truthy on this draft."""
    return [(label, doc.get(annual_field) or "(blank)", doc.get(monthly_field) or "(blank)")
            for label, check_field, annual_field, monthly_field in _BUDGET_AXES
            if doc.get(check_field)]


def _budget_ledger_preview_unavailable_flag(doctype):
    """Budget breadth (2026-07-21) — the same "uncallable" shape, a bare ``Document`` MRO. Fires
    unconditionally for ``doctype == BUDGET``, submit-direction only.

    ``class Budget(Document):`` (``budget.py:28``) — a DIRECT ``frappe.model.document.Document``
    subclass. A full-file grep of ``budget.py`` finds no ``make_gl_entries``/``make_sl_entries``/
    ``GLEntry`` reference anywhere. ERPNext's own ``get_accounting_ledger_preview`` calls
    ``doc.make_gl_entries()`` bare and unguarded — ``AttributeError`` on a live bench for a real
    Budget, so ``_tool_plan_submit`` skips the network call entirely for this doctype too
    (joining the skip tuple, now its TWENTY-FIFTH member). Budget is a control/validation
    document — it never posts a GL or Stock Ledger entry of any kind, under its own name or a
    sibling's; its real consequence is the control-plane belt disclosed in
    :func:`_budget_submit_risk_flags`."""
    if doctype != BUDGET:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Budget has no make_gl_entries method anywhere in its class hierarchy "
        "(Budget -> Document directly) — ERPNext's own preview RPC would call "
        "doc.make_gl_entries() as a bare method call and raise AttributeError on a live bench, "
        "so this broker does not call it for a Budget at all. Budget posts no GL or Stock "
        "Ledger entry of any kind, ever — it is a control/validation document; its real "
        "consequence is arming future enforcement against OTHER documents, not a ledger post of "
        "its own (see the risk flags on this same submit)"]


def _budget_submit_risk_flags(doc):
    """Budget breadth (2026-07-21) — the submit-direction disclosure, off the pre-verification
    addendum's supervisor flag #3 (``docs/plans/dossiers/budget.verify.md``), re-verified from
    source. Doctype-scoped at the call site (fired only for ``doctype == BUDGET``); entirely
    data-driven off the draft's own ``applicable_on_*``/``action_if_*``/``account``/
    ``budget_against``/``cost_center``/``project``/``budget_start_date``/``budget_end_date``/
    ``budget_amount`` fields — no live read needed for the control-plane summary itself.

    THE CENTRAL FINDING: submitting a Budget does not merely create a control document — it ARMS
    A BELT governing the FUTURE submits of OTHER, unrelated documents (Purchase Order's/Material
    Request's own ``on_submit``, and every GL-posting doctype through ``general_ledger.py``'s
    ``make_gl_entries``), synchronously, until this Budget is cancelled or superseded, with NO
    consent step at the moment of refusal. THE DUAL-ENGINE FINDING (re-verified against source,
    see the module docstring for the full citation trail): ``general_ledger.py:229``/``:443`` call
    the LEGACY ``validate_expense_against_budget()`` unconditionally for every GL-posting
    doctype; Purchase Order's/Material Request's own ``on_submit`` instead route through
    ``buying_controller.py``'s ``validate_budget()`` (``:1024-1046``), which — on the schema
    DEFAULT (``Accounts Settings.use_legacy_budget_controller`` falsy) — builds
    ``budget_controller.BudgetValidation(doc=self).validate()`` instead, a separate
    ``frappe.qb``-based engine that still throws the same ``BudgetError`` and is still
    synchronous. Both engines are live simultaneously in this checkout; neither is async.

    THE DETERMINISTIC-FROM-DRAFT THROWS (gated below): ``validate_budget_amount``
    (``budget_amount <= 0``, budget.py:82-84) and ``validate_applicable_for``'s own combination
    logic (budget.py:173-189 — two throw branches plus a silent auto-mutation branch when no axis
    is enabled at all). ``validate_account`` (budget.py:148-165) needs a live read of the target
    Account's own ``is_group``/``company``/``report_type`` — genuinely external state this
    doc-only function cannot read, so it is disclosed as unconditional PROSE, never gated.
    ``validate_duplicate``/``validate_existing_expenses`` (budget.py:112-146, 191-230) query the
    LIVE state of other ``tabBudget``/``tabGL Entry`` rows — TOCTOU-shaped, per this campaign's
    "cross-document state stays prose" rule, disclosed as prose too."""
    flags = []
    axes = _budget_armed_axes(doc)
    budget_against = doc.get("budget_against")
    target_field = "cost_center" if budget_against == "Cost Center" else "project"
    target = doc.get(target_field)
    window = (doc.get("budget_start_date"), doc.get("budget_end_date"))
    if axes:
        armed_desc = "; ".join(
            f"{label} (annual={annual!r}, monthly-accumulated={monthly!r})"
            for label, annual, monthly in axes)
        flags.append(
            "submitting ARMS a belt governing FUTURE submits of OTHER documents: once "
            f"docstatus=1, every future submit on {', '.join(a[0] for a in axes)} whose "
            f"account={doc.get('account')!r}, {budget_against}={target!r} (including "
            "tree-descendants if that dimension is_tree), and posting date falls inside "
            f"[{window[0]!r}, {window[1]!r}] is evaluated against this Budget's own action_if_* "
            f"settings, with NO consent step at the moment of refusal: {armed_desc} — an axis "
            "reading 'Stop' is a hard future refusal (BudgetError aborts the OTHER document's "
            "own submit transaction); 'Warn' is non-blocking; 'Ignore' does nothing; a blank "
            "value means this draft has never set it")
    else:
        flags.append(
            "no applicable_on_* axis is enabled on this draft as authored — validate_applicable_"
            "for() will silently force applicable_on_booking_actual_expenses=1 before this "
            "Budget can be inserted (budget.py:173-189), arming the Booking Actual Expenses axis "
            "regardless of what was authored")
    budget_amount = doc.get("budget_amount")
    try:
        amount_is_non_positive = budget_amount is not None and float(budget_amount) <= 0
    except (TypeError, ValueError):
        amount_is_non_positive = False
    if amount_is_non_positive:
        flags.append(
            f"submit will be REFUSED: budget_amount ({budget_amount!r}) is <= 0 "
            "(validate_budget_amount, budget.py:82-84 — 'Budget Amount can not be {0}.')")
    mr = bool(doc.get("applicable_on_material_request"))
    po = bool(doc.get("applicable_on_purchase_order"))
    actual = bool(doc.get("applicable_on_booking_actual_expenses"))
    if mr and not (po and actual):
        flags.append(
            "submit will be REFUSED: applicable_on_material_request is set without BOTH "
            "applicable_on_purchase_order and applicable_on_booking_actual_expenses also set "
            "(validate_applicable_for, budget.py:173-178)")
    elif po and not actual:
        flags.append(
            "submit will be REFUSED: applicable_on_purchase_order is set without "
            "applicable_on_booking_actual_expenses also set (validate_applicable_for, "
            "budget.py:180-181)")
    flags.append(
        "validate_account() will refuse this submit if the account is a Group account, does "
        "not belong to this document's own company, or is not a Profit and Loss account "
        "(budget.py:148-165) — this needs a live read of the target Account's own "
        "is_group/company/report_type, which this draft-only disclosure cannot check")
    flags.append(
        "validate_duplicate() and validate_existing_expenses() (budget.py:112-146, 191-230) "
        "query the LIVE state of other tabBudget/tabGL Entry rows at the moment validate() runs "
        "— TOCTOU-shaped, can refuse even a brand-new Budget's first submit if another Budget or "
        "expense posts between plan and submit; not readable from this draft alone")
    return flags


def _budget_cancel_risk_flags(doc):
    """Budget breadth (2026-07-21) — the cancel-direction disclosure, the DISARM finding beyond
    the pre-verification addendum's own scope, confirmed by direct source read. Budget defines NO
    ``on_cancel`` hook at all (confirmed by the full method-list grep in the module docstring —
    the SIXTH member of the "no on_submit/on_cancel hook of any kind" family), so cancelling
    changes nothing beyond ``docstatus`` itself — but that flip is exactly what DISARMS this
    Budget's own belt: both enforcement engines filter on ``docstatus == 1``
    (``budget_controller.py:192``'s own ``bud.docstatus == 1``; the legacy
    ``validate_expense_against_budget`` query at ``budget.py:391``/``:475``'s own
    ``docstatus = 1``) — confirmed by direct source read, not assumed. Cancelling a submitted
    Budget means every future PO/MR/GL-posting submit that WOULD have been checked against it is
    no longer checked at all, effective immediately — the docstatus filter alone is the entire
    on/off switch, no separate disarm mechanism exists. Doctype-scoped at the call site, data-
    driven off the draft's own ``applicable_on_*``/``account``/``budget_against`` fields (shares
    :func:`_budget_armed_axes` with the submit direction, so both describe the identical armed
    set)."""
    axes = _budget_armed_axes(doc)
    if not axes:
        return [
            "this Budget has no applicable_on_* axis armed as authored, so cancelling it has no "
            "future enforcement to disarm"]
    budget_against = doc.get("budget_against")
    target_field = "cost_center" if budget_against == "Cost Center" else "project"
    target = doc.get(target_field)
    return [
        "cancelling DISARMS this Budget's own belt immediately and completely: both enforcement "
        "engines filter on docstatus == 1 (budget_controller.py:192, budget.py:391/475), so "
        f"future submits on {', '.join(a[0] for a in axes)} against "
        f"account={doc.get('account')!r}, {budget_against}={target!r} will no longer be "
        "checked against this Budget at all, effective the instant docstatus flips to 2"]


def _timesheet_ledger_preview_unavailable_flag(doctype):
    """Timesheet breadth (2026-07-21) — the same bare-Document "uncallable" shape every prior
    control/costing doctype in this skip tuple carries. Fires unconditionally for
    ``doctype == TIMESHEET``, submit-direction only.

    ``class Timesheet(Document):`` (``timesheet.py:25``) — a DIRECT ``frappe.model.document.
    Document`` subclass, confirmed by a full-file grep of BOTH ``timesheet.py`` AND
    ``timesheet_detail.py`` finding no ``make_gl_entries``/``make_sl_entries``/``GLEntry``
    reference anywhere in either. ERPNext's own ``get_accounting_ledger_preview`` calling
    ``doc.make_gl_entries()`` bare and unguarded would raise ``AttributeError`` on a live bench
    for a real Timesheet, so ``_tool_plan_submit`` skips the network call entirely for this
    doctype too — joining the skip tuple, its TWENTY-SIXTH member. Timesheet never posts a GL or
    Stock Ledger entry of any kind, under its own name or a sibling's — it is a costing/billing
    tracker; its own billing amounts feed a Sales Invoice's line items (via ``make_sales_invoice``)
    rather than posting anything directly, and a Sales Invoice can, in turn, rewrite THIS
    document's own billing fields after submit (see :func:`_timesheet_submit_risk_flags`)."""
    if doctype != TIMESHEET:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Timesheet has no make_gl_entries method anywhere in its class hierarchy "
        "(Timesheet -> Document directly, confirmed in timesheet.py AND timesheet_detail.py) — "
        "ERPNext's own preview RPC would call doc.make_gl_entries() as a bare method call and "
        "raise AttributeError on a live bench, so this broker does not call it for a Timesheet "
        "at all. Timesheet posts no GL or Stock Ledger entry of any kind, ever — its billing "
        "amounts feed a Sales Invoice's own line items instead (see the risk flags on this same "
        "submit for how a Sales Invoice can, in turn, rewrite THIS document's own status/billing "
        "fields after submit)"]


def _timesheet_second_writer_flag(direction):
    """Timesheet breadth (2026-07-21) — THE SECOND WRITER, the row's sharpest fact, shared
    verbatim by both :func:`_timesheet_submit_risk_flags` and
    :func:`_timesheet_cancel_risk_flags` so neither direction understates it. Genuinely
    cross-document, TOCTOU-shaped state (per this campaign's own "cross-document state stays
    prose" rule) — unconditional, never gated on this draft's own fields, because the fact is
    true regardless of what THIS draft currently holds.

    Sales Invoice's own ``on_submit`` (``sales_invoice.py:469``, calling ``update_time_sheet`` at
    line 524) and ``before_cancel`` (``:598``, calling it again at line 604) both mutate a
    referenced Timesheet's ``total_billable_amount``/``total_billed_amount``/``per_billed``/
    ``status`` via ``update_time_sheet()`` (``sales_invoice.py:828-837``), which ends with
    ``timesheet.flags.ignore_validate_update_after_submit = True; timesheet.db_update_all()`` — a
    RAW WHOLE-DOCUMENT write (the parent row AND every ``time_logs`` child row in one call,
    ``frappe/model/base_document.py:849-851``, self-documented "Raw update parent + children. DOES
    NOT VALIDATE AND CALL TRIGGERS"). This is the Maintenance Visit ``db_update`` bypass grade —
    hooks/version/permission all skipped on an already-loaded document — but WIDER (parent +
    every child row in one call, not one document's own targeted field update) and the FIRST case
    in this campaign where a document rewrites ITS OWN submitted state from outside, driven
    entirely by a DIFFERENT doctype's own submit/cancel lifecycle."""
    return (
        "status/billing fields on this Timesheet are NOT stable once submitted: Sales Invoice's "
        "own on_submit (sales_invoice.py:469->524) and before_cancel (:598->604) both call "
        "update_time_sheet() (sales_invoice.py:828-837), which recalculates "
        "total_billable_amount/total_billed_amount/per_billed, calls set_status(), then "
        "timesheet.db_update_all() with ignore_validate_update_after_submit=True — a RAW "
        "WHOLE-DOCUMENT write (parent row + every time_logs child row, frappe/model/"
        "base_document.py:849-851, self-documented 'DOES NOT VALIDATE AND CALL TRIGGERS'): no "
        "validate()/on_update_after_submit/permission re-check/version entry runs at all. This "
        f"can fire on EITHER a Sales Invoice submit OR a Sales Invoice cancel that names this "
        f"Timesheet — neither is this Timesheet's own hook, and neither is previewable from this "
        f"{direction} alone")


def _timesheet_status_precedence_flag():
    """Timesheet breadth (2026-07-21) — the status-precedence correction, shared verbatim by both
    directions. Unconditional prose: the derivation rule is always true regardless of this
    draft's own current field values."""
    return (
        "status is a THREE-SEQUENTIAL-IF derivation (set_status, timesheet.py:127-137), never "
        "if/elif: a truthy sales_invoice field ALWAYS wins last and forces status='Completed', "
        "overriding a per_billed>=100 'Billed' or 0<per_billed<100 'Partially Billed' result — "
        "per_billed alone never guarantees the 'Billed' label if sales_invoice is also set")


def _timesheet_submit_risk_flags(doc):
    """Timesheet breadth (2026-07-21) — the submit-direction disclosure, off the pre-verification
    addendum (``docs/plans/dossiers/timesheet.verify.md``), re-verified from source. Doctype-
    scoped at the call site (fired only for ``doctype == TIMESHEET``).

    THE DETERMINISTIC-FROM-DRAFT THROWS: ``validate_mandatory_fields()`` (``timesheet.py:158-
    167``), called from ``on_submit`` — three per-row throws, each readable straight off this
    draft's own ``time_logs`` child rows and this document's own header-level ``employee`` field:
    (1) a row where BOTH ``from_time`` AND ``to_time`` are absent (``timesheet.py:159-161`` — the
    addendum's own correction of the dossier's inverted English: ONE of the two set, one blank,
    PASSES this guard; only both-absent throws); (2) this document's own ``employee`` is set AND
    a row carries no ``activity_type`` (``:163-164`` — a HEADER field gating a ROW field, never
    the reverse); (3) a row's own ``hours`` reads ``0.0`` (``:166-167``).

    THE SECOND WRITER (:func:`_timesheet_second_writer_flag`) and the status-precedence
    correction (:func:`_timesheet_status_precedence_flag`) are both unconditional, cross-document
    prose — true regardless of this draft's own current values, per this campaign's own
    "cross-document state stays prose" rule."""
    flags = []
    rows = doc.get("time_logs") or []
    employee = doc.get("employee")
    blank_time_rows, missing_activity_rows, zero_hours_rows = [], [], []
    for row in rows:
        idx = row.get("idx")
        from_time = row.get("from_time")
        to_time = row.get("to_time")
        if not from_time and not to_time:
            blank_time_rows.append(idx)
        if employee and not row.get("activity_type"):
            missing_activity_rows.append(idx)
        hours = row.get("hours")
        try:
            hours_is_zero = hours is not None and float(hours) == 0.0
        except (TypeError, ValueError):
            hours_is_zero = False
        if hours_is_zero:
            zero_hours_rows.append(idx)
    if blank_time_rows:
        flags.append(
            f"submit will be REFUSED: row(s) {blank_time_rows} carry NEITHER from_time NOR "
            "to_time (validate_mandatory_fields, timesheet.py:159-161 — the guard is 'if not "
            "data.from_time and not data.to_time', BOTH absent, not either — a row with exactly "
            "one of the two set passes this guard)")
    if missing_activity_rows:
        flags.append(
            f"submit will be REFUSED: this Timesheet's own employee ({employee!r}) is set and "
            f"row(s) {missing_activity_rows} carry no activity_type (validate_mandatory_fields, "
            "timesheet.py:163-164)")
    if zero_hours_rows:
        flags.append(
            f"submit will be REFUSED: row(s) {zero_hours_rows} carry hours == 0 "
            "(validate_mandatory_fields, timesheet.py:166-167 — the guard is 'flt(data.hours) "
            "== 0.0', EXACTLY zero, despite its own 'greater than zero' message: a NEGATIVE "
            "hours value passes it, and the schema carries no non_negative constraint on the "
            "hours Float)")
    flags.append(_timesheet_second_writer_flag("draft"))
    flags.append(_timesheet_status_precedence_flag())
    flags.append(
        "on_submit also calls update_task_and_project() (timesheet.py:169-191): for every "
        "unique task/project named in time_logs, it saves the Task (status forced Completed/"
        "Working) and the Project via task.save(ignore_permissions=True)/project_doc.save("
        "ignore_permissions=True) — a PERMISSION-ONLY bypass (the Asset Value Adjustment grade: "
        "every validate()/hook/version-history write still runs, only the ACL/authorization "
        "gate is skipped), never a db_set/db_update/raw-SQL bypass")
    return flags


def _timesheet_cancel_risk_flags(doc):
    """Timesheet breadth (2026-07-21) — the cancel-direction disclosure. ``on_cancel``
    (``timesheet.py:151-152``) is unconditional — ``update_task_and_project()`` only, never a
    throw, never a docstatus guard of its own. ``before_cancel`` (``:148-149``) re-runs
    ``set_status()`` while ``docstatus`` is STILL 1 (not yet 2) — this recomputes the Billed/
    Partially Billed/Completed branch one more time, it does NOT stamp 'Cancelled'. Shares
    :func:`_timesheet_second_writer_flag`/:func:`_timesheet_status_precedence_flag` with the
    submit direction verbatim, so both directions describe the identical facts."""
    return [
        _timesheet_second_writer_flag("submitted document"),
        _timesheet_status_precedence_flag(),
        "before_cancel (timesheet.py:148-149) re-runs set_status() while docstatus is still 1 "
        "(not yet 2) — this recomputes the Billed/Partially Billed/Completed branch one more "
        "time, it does NOT stamp 'Cancelled'; the docstatus-driven 'Cancelled' baseline is only "
        "assigned the next time this document's own status is derived after cancel completes",
        "on_cancel (timesheet.py:151-152) is unconditional: update_task_and_project() touches "
        "every linked Task/Project via task.save(ignore_permissions=True)/project_doc.save("
        "ignore_permissions=True) — a PERMISSION-ONLY bypass, the same grade as the submit "
        "direction — never refuses, never a docstatus guard of its own"]


def _contract_ledger_preview_unavailable_flag(doctype):
    """Contract breadth (2026-07-21) — the same bare-Document "uncallable" shape every prior
    control/costing doctype in this skip tuple carries. Fires unconditionally for
    ``doctype == CONTRACT``, submit-direction only.

    ``class Contract(Document):`` (``contract.py:11``) — a DIRECT ``frappe.model.document.
    Document`` subclass, confirmed by a full-file grep finding no ``make_gl_entries`` reference
    anywhere in the 144-line file. ERPNext's own ``get_accounting_ledger_preview`` calling
    ``doc.make_gl_entries()`` bare and unguarded would raise ``AttributeError`` on a live bench
    for a real Contract, so ``_tool_plan_submit`` skips the network call entirely for this
    doctype too — joining the skip tuple, its TWENTY-SEVENTH member. Contract never posts a GL
    or Stock Ledger entry of any kind — it is a pure legal-document tracker."""
    if doctype != CONTRACT:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Contract has no make_gl_entries method anywhere in its class hierarchy "
        "(Contract -> Document directly, confirmed in contract.py) — ERPNext's own preview RPC "
        "would call doc.make_gl_entries() as a bare method call and raise AttributeError on a "
        "live bench, so this broker does not call it for a Contract at all. Contract posts no "
        "GL or Stock Ledger entry of any kind, ever"]


def _contract_writability_illusion_flag():
    """Contract breadth (2026-07-21) — THE WRITABILITY ILLUSION, shared verbatim by both
    :func:`_contract_submit_risk_flags` and :func:`_contract_cancel_risk_flags` so neither
    direction understates it. Unconditional prose — the mechanism is always true post-submit
    regardless of this draft's own current field values.

    ``status``/``fulfilment_status``/``is_signed`` all carry ``allow_on_submit: 1``
    (``contract.json``) and LOOK directly settable through a governed post-submit write — they
    are not, in practice. ``before_update_after_submit`` (``contract.py:67-69``) fires on EVERY
    save of an already-submitted Contract (``frappe/model/document.py:1135-1138``'s own
    ``check_docstatus_transition`` sets ``_action = "update_after_submit"`` whenever ``docstatus``
    stays SUBMITTED across a save; ``run_before_save_methods``, ``document.py:1410-1411``, then
    calls ``run_method("before_update_after_submit")`` unconditionally — not gated on any
    ``allow_on_submit`` field actually having changed) and unconditionally re-runs
    ``update_contract_status()`` + ``update_fulfilment_status()``, which RECOMPUTE both fields
    from ``start_date``/``end_date``/``is_signed``/fulfilment-term state — discarding whatever a
    caller just wrote into ``status``/``fulfilment_status`` directly on the very next save."""
    return (
        "status/fulfilment_status LOOK directly writable post-submit (both carry "
        "allow_on_submit=1) but are NOT, in practice: before_update_after_submit "
        "(contract.py:67-69) fires on every subsequent save of an already-submitted Contract "
        "(frappe/model/document.py:1135-1138/:1410-1411) and unconditionally recomputes both "
        "fields from start_date/end_date/is_signed/fulfilment-term state, discarding a direct "
        "write to either field on the very next save — is_signed (also allow_on_submit=1) is "
        "the real post-submit lever, since toggling it changes what update_contract_status() "
        "computes")


def _contract_scheduler_flag(doc):
    """Contract breadth (2026-07-21) — THE SCHEDULER, shared verbatim by both
    :func:`_contract_submit_risk_flags` and :func:`_contract_cancel_risk_flags`, but the arming
    clause is DATA-DRIVEN off this draft's own ``is_signed`` — the deterministic-from-draft
    standard the interrogation checklist sets, even though the scheduler's own trigger (today's
    date crossing ``start_date``/``end_date``) is inherently NOT deterministic from the draft
    alone (closer in kind to Asset Maintenance Log's own scheduler disclosure than to a doomed-
    submit boolean).

    ``update_status_for_contracts()`` (``contract.py:129-144``), registered in
    ``scheduler_events["daily_maintenance"]`` (``hooks.py:473``). Filters ``is_signed=True`` AND
    ``docstatus=1`` EXPLICITLY (``contract.py:137``) — the campaign's FIRST CONFIRMED SUBMITTED-
    STATE scheduler mutator (the mirror of Asset Maintenance Log's own draft-only-in-practice
    scheduler, ``erpnext.py:3576-3583``). Writes via ``frappe.db.set_value`` (``frappe/database/
    database.py:934-945``) once per matching contract — no ``validate()``/hooks/permission/
    version entry. Can only flip ``status`` between ``"Active"``/``"Inactive"`` — never
    ``"Unsigned"`` (filter requires ``is_signed=True``) and never ``"Cancelled"``."""
    is_signed = bool(doc.get("is_signed"))
    if is_signed:
        arming = ("this draft's own is_signed=True — ONCE SUBMITTED, this Contract's status is "
                  "armed for the daily scheduler")
    else:
        arming = ("this draft's own is_signed is falsy — the scheduler's docstatus=1 AND "
                  "is_signed=True filter will NOT match this Contract unless is_signed is set "
                  "before or after submit")
    return (
        f"a daily scheduler job (update_status_for_contracts, contract.py:129-144, registered "
        f"hooks.py:473) targets SUBMITTED contracts explicitly (filters is_signed=True AND "
        f"docstatus=1) and rewrites this document's own status field via a raw "
        f"frappe.db.set_value — no validate()/hooks/permission check/version entry runs; it can "
        f"only flip status between 'Active' and 'Inactive' based on today's date vs "
        f"start_date/end_date, and this fires OUTSIDE any save this broker's own tools perform: "
        f"{arming}")


def _contract_status_docstatus_disconnect_flag():
    """Contract breadth (2026-07-21) — THE STATUS/DOCSTATUS DISCONNECT, cancel-direction only
    (the finding is specifically about what a governed cancel does NOT do). Unconditional prose.

    The only code path that ever writes ``status="Cancelled"`` is ``on_discard()``
    (``contract.py:64-65``), and Frappe's own ``discard()`` (``frappe/model/document.py:1349-
    1362``) hard-guards ``if not self.docstatus.is_draft(): raise ...`` — so this path can NEVER
    touch a submitted (``docstatus=1``) Contract, and it is not reachable through this broker's
    own governed verb surface at all (no discard tool is exposed). A governed ``cancel()``
    (``docstatus`` 1 -> 2) has no ``on_cancel`` override — it never touches the ``status`` Select
    at all."""
    return (
        "cancelling this Contract will NOT change its own status field at all: no on_cancel "
        "override exists, and the only code path that ever writes status='Cancelled' "
        "(on_discard, contract.py:64-65) is hard-guarded by frappe itself to draft documents "
        "only (frappe/model/document.py:1349-1362) — a cancelled-docstatus Contract keeps "
        "DISPLAYING whatever Active/Inactive/Unsigned value it last computed, forever; "
        "'Cancelled' is a schema-declared status option that is structurally unreachable once "
        "this document has ever been submitted")


def _contract_submit_risk_flags(doc):
    """Contract breadth (2026-07-21) — the submit-direction disclosure, off the pre-verification
    addendum (``docs/plans/dossiers/contract.verify.md``), re-verified from source. Doctype-
    scoped at the call site (fired only for ``doctype == CONTRACT``).

    THE ONE DETERMINISTIC-FROM-DRAFT DOOMED-SUBMIT THROW: ``validate_dates()``
    (``contract.py:71-73``) — ``if self.end_date and self.end_date < self.start_date:
    frappe.throw(...)`` — readable straight off this draft's own ``start_date``/``end_date``
    fields (only checked when ``end_date`` is truthy). THE WRITABILITY ILLUSION
    (:func:`_contract_writability_illusion_flag`) and THE SCHEDULER
    (:func:`_contract_scheduler_flag`) are both named here too; ``before_submit`` also stamps
    ``signed_by_company = frappe.session.user`` (``contract.py:61-62``) — the submitting seat's
    own user lands on the document, overwriting whatever this field currently holds."""
    flags = []
    start_date = doc.get("start_date")
    end_date = doc.get("end_date")
    if start_date and end_date and str(end_date) < str(start_date):
        flags.append(
            f"submit will be REFUSED: end_date ({end_date!r}) is before start_date "
            f"({start_date!r}) (validate_dates, contract.py:71-73 — 'End Date cannot be before "
            "Start Date')")
    flags.append(_contract_writability_illusion_flag())
    flags.append(_contract_scheduler_flag(doc))
    flags.append(
        "before_submit (contract.py:61-62) unconditionally sets signed_by_company = the "
        "submitting session's own user, overwriting whatever this field currently holds — a "
        "decoy fieldname (it stores a User, never a registry Company)")
    return flags


def _contract_cancel_risk_flags(doc):
    """Contract breadth (2026-07-21) — the cancel-direction disclosure. No ``on_cancel``
    override exists in ``contract.py`` at all — a governed cancel is a PURE docstatus flip (1 ->
    2), Frappe's own generic back-link check only. Shares
    :func:`_contract_writability_illusion_flag`/:func:`_contract_scheduler_flag` with the submit
    direction (both facts remain true up to the instant of cancel) and adds THE STATUS/DOCSTATUS
    DISCONNECT (:func:`_contract_status_docstatus_disconnect_flag`), which is specifically about
    the cancel direction's own consequence."""
    return [
        _contract_status_docstatus_disconnect_flag(),
        _contract_writability_illusion_flag(),
        _contract_scheduler_flag(doc),
        "no on_cancel override exists in contract.py at all — cancelling is a pure docstatus "
        "flip (1 -> 2), frappe's own generic back-link check only; nothing here reverses the "
        "before_submit signed_by_company stamp or any prior scheduler-driven status flip"]


def _to_float(value):
    """Tolerant numeric read for Pick List's own draft fields (``picked_qty``/``stock_qty``/
    ``stock_reserved_qty``/``for_qty``) — a fixture or a live bench may hand back ``None``, an
    empty string, or a real number; this never raises, matching the campaign's own "readable
    straight off the draft" bar for a deterministic risk flag."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pick_list_ledger_preview_unavailable_flag(doctype):
    """Pick List breadth (2026-07-21) — the same bare-``TransactionBase`` "uncallable" shape every
    prior control/costing doctype in this skip tuple carries, this time the DEEPEST MRO of the
    category. Fires unconditionally for ``doctype == PICK_LIST``, submit-direction only.

    ``class PickList(TransactionBase):`` (``pick_list.py:46``), and ``class
    TransactionBase(StatusUpdater):`` (``erpnext/utilities/transaction_base.py:20``), and ``class
    StatusUpdater(Document):`` (``erpnext/controllers/status_updater.py:181``) — the real chain is
    ``PickList -> TransactionBase -> StatusUpdater -> Document``, 4-deep (the addendum's own MRO
    correction; the dossier's own "PickList -> TransactionBase -> Document" was one level short).
    The FOURTH doctype of this exact shape, after Installation Note/Maintenance Visit/Maintenance
    Schedule. A full-file grep of ``pick_list.py`` (1893 lines) finds no ``make_gl_entries``
    reference anywhere. ERPNext's own ``get_accounting_ledger_preview`` calling
    ``doc.make_gl_entries()`` bare and unguarded would raise ``AttributeError`` on a live bench for
    a real Pick List, so ``_tool_plan_submit`` skips the network call entirely for this doctype too
    — joining the skip tuple, its TWENTY-EIGHTH member. Pick List never posts a GL or Stock Ledger
    entry of any kind under its own name — it is an order/reference layer; the Stock Entries it
    references (created via the whitelisted ``create_stock_entry``) are what actually post."""
    if doctype != PICK_LIST:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Pick List has no make_gl_entries method anywhere in its class hierarchy (PickList "
        "-> TransactionBase -> StatusUpdater -> Document, never AccountsController or "
        "StockController) — ERPNext's own preview RPC would call doc.make_gl_entries() as a bare "
        "method call and raise AttributeError on a live bench, so this broker does not call it "
        "for a Pick List at all. Pick List posts no GL or Stock Ledger entry of any kind, ever — "
        "it references Stock Entries (which DO post) rather than posting itself"]


def _pick_list_cross_document_throws_flag():
    """Pick List breadth (2026-07-21) — the ~8 of this doctype's ~12 total submit-blocking throw
    sites that need LIVE cross-document/external state, unconditional prose per the standing
    "cross-document state stays prose" rule. Shared verbatim by
    :func:`_pick_list_submit_risk_flags` (this is submit-direction only — none of these run during
    a plain cancel). See that function's own docstring for the 4 DETERMINISTIC throws this
    disclosure deliberately excludes."""
    return (
        "the remaining ~8 of this doctype's ~12 total submit-blocking throw sites need LIVE "
        "cross-document/external state, not readable from this draft alone: validate() also runs "
        "validate_stock_qty (batch qty vs live get_batch_qty and bin qty vs live "
        "Bin.actual_qty, pick_list.py:128-169), check_serial_no_status (live Serial No records, "
        ":196-217), and validate_expired_batches (live Batch.expiry_date vs today, :288-313); "
        "before_save() also runs validate_warehouses' own company-mismatch branch (each row's "
        "live Warehouse.company, :180-193) and validate_sales_order_percentage (the referenced "
        "Sales Order's own live per_picked, :231-240); before_submit() also runs "
        "validate_sales_order (:246-263, gated on purpose=='Delivery' — throws if any referenced "
        "Sales Order Item carries live stock_reserved_qty > 0); on_submit() also runs, via "
        "update_reference_qty(), validate_picked_qty (:538-549 — needs Stock Settings' own live "
        "over_delivery_receipt_allowance PLUS the live cumulative picked/stock qty across every "
        "OTHER submitted Pick List referencing the same Sales Order Item/Packed Item)")


def _pick_list_reservation_two_jaw_flag(doc):
    """Pick List breadth (2026-07-21) — THE TWO-JAW TRAP, shared verbatim by both
    :func:`_pick_list_submit_risk_flags` and :func:`_pick_list_cancel_risk_flags` so neither
    direction understates it. The arming clause is DATA-DRIVEN off this draft's own ``locations``
    rows (the ``has_reserved_stock()`` condition, pick_list.py:915-925, is 100% deterministic from
    the draft), the same shape Contract's own scheduler flag used for its own data-driven arming
    clause.

    Jaw one: ``on_update_after_submit()`` (``:345-350``) throws if ``has_reserved_stock()`` is
    true — a clean, draft-readable "can this document still be edited" flag. Jaw two:
    ``on_cancel()`` (``:352-364``) carries ZERO ``frappe.throw`` calls (grep-clean) and its own
    ``ignore_linked_doctypes`` (``:353-357``) explicitly PRESERVES ``Serial and Batch Bundle``/
    ``Stock Reservation Entry``/``Delivery Note`` — cancel always succeeds and never auto-cancels
    any of the three. Together: once reserved, this Pick List CANNOT be edited further (jaw one)
    but CAN still be cancelled, orphaning any live Stock Reservation Entries against a
    now-cancelled parent (jaw two) — the dossier's own RED FLAG #3 names only jaw two."""
    armed = False
    if doc.get("purpose") == "Delivery":
        for row in (doc.get("locations") or []):
            if (row.get("sales_order") and row.get("sales_order_item")
                    and _to_float(row.get("stock_reserved_qty")) > 0):
                armed = True
                break
    if armed:
        arming = ("this draft ALREADY carries reserved stock on at least one location row "
                  "(purpose='Delivery', stock_reserved_qty > 0) — jaw one is armed the instant "
                  "this document is submitted")
    else:
        arming = ("this draft carries no reserved stock yet (purpose != 'Delivery', or every "
                  "location's stock_reserved_qty is 0) — jaw one only arms later, if "
                  "create_stock_reservation_entries is ever called against the submitted "
                  "document (a whitelisted side surface this broker does not expose)")
    return (
        "TWO-JAW TRAP: once any location row carries stock_reserved_qty > 0 (purpose='Delivery' "
        "only, has_reserved_stock(), pick_list.py:915-925), on_update_after_submit (:345-350) "
        "THROWS on any further save/edit of this Pick List — 'cannot be updated' — but on_cancel "
        "(:352-364) NEVER checks this and ALWAYS succeeds, explicitly preserving Serial and Batch "
        "Bundle/Stock Reservation Entry/Delivery Note via ignore_linked_doctypes (:353-357): "
        f"cancelling leaves any Stock Reservation Entries orphaned against a now-cancelled Pick "
        f"List. {arming}")


def _pick_list_reservation_delegation_flag():
    """Pick List breadth (2026-07-21) — THE RESERVATION MACHINERY, shared verbatim by both
    :func:`_pick_list_submit_risk_flags` and :func:`_pick_list_cancel_risk_flags`. Unconditional
    prose — the delegation shape is always true regardless of this draft's own current values.

    ``create_stock_reservation_entries`` (``pick_list.py:499-524``, whitelisted, callable on a
    SUBMITTED Pick List) is a 2-hop delegation: builds a per-Sales-Order ``items_details`` dict
    from this draft's own ``locations``, then calls ``frappe.get_doc("Sales Order",
    so).create_stock_reservation_entries(...)`` (``sales_order.py:834-851``), which itself
    re-delegates a THIRD hop to
    ``stock_reservation_entry.create_stock_reservation_entries_for_so_items``
    (``stock_reservation_entry.py:1575+``) — the function that actually mints the Stock
    Reservation Entry document(s), typed ``from_voucher_type: Literal["Pick List", "Purchase
    Receipt"]`` (confirmed) — Purchase Receipt shares this EXACT delegation path TODAY, not a
    future pairing. ``cancel_stock_reservation_entries`` (``:526-536``) is a single-hop delegation
    to the module-level function of the same name. Per the landing briefing's own honesty-grade
    vocabulary (clean ``.save()`` = Maintenance Schedule; ``db_update`` bypass = Maintenance
    Visit; raw SQL into another table = Quality Inspection): this is a FOURTH grade — multi-hop
    delegated creation through ANOTHER doctype's own method chain, never a direct insert in
    ``pick_list.py``'s own module. Neither whitelisted method is exposed by this broker (nothing
    granted toward either)."""
    return (
        "create_stock_reservation_entries/cancel_stock_reservation_entries (pick_list.py:499-536, "
        "both @frappe.whitelist(), callable on a submitted Pick List) are NOT exposed by this "
        "broker — nothing granted. Creation is a 2-hop delegation (PickList -> Sales Order's own "
        "create_stock_reservation_entries, sales_order.py:834-851 -> stock_reservation_entry."
        "create_stock_reservation_entries_for_so_items, stock_reservation_entry.py:1575+, typed "
        "Literal['Pick List', 'Purchase Receipt'] — Purchase Receipt shares this EXACT path "
        "today), never a direct insert in pick_list.py's own module; cancellation is a "
        "single-hop delegation to the module-level stock_reservation_entry.cancel_stock_"
        "reservation_entries")


def _pick_list_submit_risk_flags(doc):
    """Pick List breadth (2026-07-21) — the submit-direction disclosure, off the pre-verification
    addendum (``docs/plans/dossiers/pick_list.verify.md``) and the landing briefing's own
    re-derivation demand. Doctype-scoped at the call site (fired only for ``doctype ==
    PICK_LIST``).

    THE FOUR DETERMINISTIC-FROM-DRAFT THROWS, each readable straight off this draft's own
    ``locations`` child rows and header fields — the template's own bar for a gated risk flag:
    (1) ``validate_for_qty()`` (``pick_list.py:705-709``, ``validate()``) — ``purpose ==
    "Material Transfer for Manufacture"`` and ``for_qty`` falsy; (2) ``validate_warehouses()``'s
    own missing-warehouse branch (``:171-178``, ``before_save()``, gated on ``locations`` being
    non-empty) — any row with a blank ``warehouse``; (3) ``validate_picked_items()`` (``:265-
    273``, ``before_submit()``) — ``scan_mode`` set and a row's ``picked_qty < stock_qty``; (4)
    the ``on_submit``-called ``make_bundle_using_old_serial_batch_fields()``'s own throw
    (``:318-323``) — a row carries ``serial_no``/``batch_no`` but ``use_serial_batch_fields`` is
    falsy — the addendum's own sharpest find: the dossier's §6 quotes this exact call as a pure
    side effect with zero mention it can abort the submit. The remaining ~8 throw sites
    (:func:`_pick_list_cross_document_throws_flag`), THE TWO-JAW TRAP
    (:func:`_pick_list_reservation_two_jaw_flag`), and THE RESERVATION MACHINERY
    (:func:`_pick_list_reservation_delegation_flag`) are all named here too."""
    flags = []
    locations = doc.get("locations") or []
    purpose = doc.get("purpose")
    for_qty = doc.get("for_qty")
    if purpose == "Material Transfer for Manufacture" and not for_qty:
        flags.append(
            f"submit will be REFUSED: purpose is 'Material Transfer for Manufacture' and "
            f"for_qty is {for_qty!r} (validate_for_qty, pick_list.py:705-709 — 'Qty of Finished "
            "Goods Item should be greater than 0')")
    if locations:
        missing_wh_rows = [row.get("idx") for row in locations if not row.get("warehouse")]
        if missing_wh_rows:
            flags.append(
                f"submit will be REFUSED: row(s) {missing_wh_rows} carry no warehouse "
                "(validate_warehouses, pick_list.py:171-178 — 'Warehouse is required')")
    if doc.get("scan_mode"):
        short_rows = [row.get("idx") for row in locations
                      if _to_float(row.get("picked_qty")) < _to_float(row.get("stock_qty"))]
        if short_rows:
            flags.append(
                f"submit will be REFUSED: scan_mode is set and row(s) {short_rows} carry "
                "picked_qty < stock_qty (validate_picked_items, pick_list.py:265-273 — 'picked "
                "quantity is less than the required quantity')")
    legacy_rows = [row.get("idx") for row in locations
                   if (row.get("serial_no") or row.get("batch_no"))
                   and not row.get("use_serial_batch_fields")]
    if legacy_rows:
        flags.append(
            f"submit will be REFUSED: row(s) {legacy_rows} carry serial_no/batch_no but "
            "use_serial_batch_fields is falsy (make_bundle_using_old_serial_batch_fields, called "
            "from on_submit, pick_list.py:318-323 — 'Please enable Use Old Serial / Batch Fields "
            "to make_bundle')")
    flags.append(_pick_list_cross_document_throws_flag())
    flags.append(_pick_list_reservation_two_jaw_flag(doc))
    flags.append(_pick_list_reservation_delegation_flag())
    return flags


def _pick_list_cancel_risk_flags(doc):
    """Pick List breadth (2026-07-21) — the cancel-direction disclosure. ``on_cancel``
    (``pick_list.py:352-364``) carries ZERO ``frappe.throw`` calls (grep-clean) — cancel always
    succeeds. Shares THE TWO-JAW TRAP (:func:`_pick_list_reservation_two_jaw_flag`) and THE
    RESERVATION MACHINERY (:func:`_pick_list_reservation_delegation_flag`) with the submit
    direction verbatim (both facts remain true up to the instant of cancel), and adds the
    reversal-direction side effects named plainly."""
    return [
        _pick_list_reservation_two_jaw_flag(doc),
        _pick_list_reservation_delegation_flag(),
        "on_cancel (pick_list.py:352-364) carries ZERO frappe.throw calls — cancel always "
        "succeeds; ignore_linked_doctypes (:353-357) explicitly preserves Serial and Batch "
        "Bundle/Stock Reservation Entry/Delivery Note (never auto-cancelled), and "
        "update_status/update_bundle_picked_qty/update_reference_qty/"
        "update_sales_order_picking_status/update_prevdoc_status all reverse in the -1 "
        "direction (mirroring on_submit's own +1 side effects)"]


def _asset_repair_submit_risk_flags(doc):
    """Asset Repair breadth (2026-07-21) — the submit-direction disclosures. Doctype-scoped at the
    call site (fired only for ``doctype == ASSET_REPAIR``); every flag below is data-driven off
    the draft's own fields, never a blanket warning. Source pins live in ``erpnext.py``'s module
    docstring ("Breadth (Asset Repair)").

    Like Asset, Asset Repair's native ledger preview is CALLABLE (``make_gl_entries``,
    asset_repair.py:312-318 — its OWN method) — it does NOT join the skip tuple. ``projected_gl``
    is honest-empty for either of TWO independent causes, disclosed here per cause (the Asset
    precedent's own shape): (1) ``capitalize_repair_cost`` falsy (the FIRST gate, :205); (2)
    ``capitalize_repair_cost`` truthy but ``total_repair_cost <= 0`` (the SECOND, independent gate
    inside ``make_gl_entries`` itself, :316).

    No ``now_date`` parameter (unlike :func:`_asset_submit_risk_flags`) — ``completion_date``'s
    own future-date check is already covered by the generic ``posting_date is in the future``
    flag the call site appends from the same comparison every doctype shares."""
    flags = []
    capitalized = bool(doc.get("capitalize_repair_cost"))
    total = _coerce_float(doc.get("total_repair_cost")) or 0.0
    if not capitalized:
        flags.append(
            "capitalize_repair_cost is not set: submit posts NO GL entries at all (the first of "
            "two independent gates, asset_repair.py:205) — update_asset_value/set_increase_in_"
            "asset_life/reschedule_depreciation/add_asset_activity are all skipped too, the whole "
            "on_submit block being gated on this one flag")
    elif total <= 0:
        flags.append(
            "capitalize_repair_cost is set but total_repair_cost is 0 (or unset): make_gl_"
            "entries' own second, independent gate (asset_repair.py:316, flt(total_repair_cost) "
            "> 0) posts NO GL entries even though the asset-value/depreciation-life/activity-log "
            "side effects above DID run — a real split between 'capitalized' and 'posted'")
    # THE STOCK ENTRY AUTO-SUBMIT — unconditional, no capitalize_repair_cost gate, no try/except
    # (asset_repair.py:258-293, decrease_stock_quantity, called FIRST in on_submit).
    stock_items = doc.get("stock_items") or []
    if stock_items:
        flags.append(
            "stock_items is non-empty: submit auto-creates AND SUBMITS a Stock Entry "
            "(stock_entry_type=Material Issue, asset_repair.py:258-293) — a sibling submitted "
            "document riding this same consent, with NO try/except around its own .insert()/"
            ".submit() calls (:292-293); if either fails natively, the WHOLE Asset Repair submit "
            "fails with it. It also makes this Asset Repair a submitted-linked-document source "
            "from birth, blocking any later leaf-node plan_cancel and routing through "
            "plan_cascade_cancel instead")
    # THE BLANK-COMPANY CONSEQUENCE — genuinely doomed when either gate above is armed (unlike
    # Timesheet's own zero-consequence blank company; the AVA honesty grade, reachable on this
    # SAME doctype depending on the draft's own fields).
    if not doc.get("company"):
        if capitalized and total > 0:
            flags.append(
                "company is blank AND the GL gate above is armed: get_asset_account "
                "(asset.py:1217-1237, called from get_gl_entries) falls through to a blank/"
                "unresolvable Company lookup and THROWS — this submit will be REFUSED natively "
                "unless company is set before it runs")
        if stock_items:
            flags.append(
                "company is blank AND stock_items is non-empty: the auto-created Stock Entry's "
                "own company field is reqd=1 (stock_entry.json) — its .insert() will be REFUSED "
                "natively (MandatoryError) before it ever reaches .submit(), taking this whole "
                "Asset Repair submit down with it")
    # THE PENDING DOOMED SUBMIT — deterministic-from-draft (asset_repair.py:239-240).
    if doc.get("repair_status") == "Pending":
        flags.append(
            "repair_status is 'Pending': this submit will be REFUSED — check_repair_status "
            "(asset_repair.py:239-240) throws 'Please update Repair Status.' whenever docstatus "
            "reaches 1 with repair_status still Pending. update_status() (asset_repair.py:"
            "178-187) runs FIRST in the same validate() call, so the linked Asset's own status "
            "is raw-written toward 'Out of Order' via frappe.db.set_value before this throw "
            "aborts the request")
    else:
        # THE ASSET-STATUS SIDE-WRITE — real, reachable, ungated by capitalize_repair_cost, fires
        # on EVERY successful submit (repair_status != "Pending" is required to reach here) —
        # SUBMIT-ONLY, per the correction to the pre-verification addendum's own Correction #1/
        # Landing Risk #2 (see erpnext.py's module docstring "THE SHARPEST CORRECTION": frappe's
        # own run_before_save_methods() only calls run_method("validate") for _action in ("save",
        # "submit"), never for "cancel" — document.py:1402-1409).
        flags.append(
            "update_status() (asset_repair.py:178-187) unconditionally recomputes the linked "
            "Asset's own status via Asset.set_status() -> a raw db_set (asset.py:770-774) — a "
            "cross-document write outside the Asset's own hook pipeline, ungated by capitalize_"
            "repair_cost, riding this same submit consent with no second marker. This does NOT "
            "happen again on cancel (see _asset_repair_cancel_risk_flags)")
    return flags


def _asset_repair_cancel_risk_flags(doc):
    """Asset Repair breadth (2026-07-21) — the cancel-direction disclosures. Doctype-scoped at the
    call site; data-driven off the draft's own ``capitalize_repair_cost``/``stock_items`` fields.

    **Unlike submit, cancel does NOT re-trigger the Asset-status side-write.** Re-verified directly
    against ``frappe/model/document.py``: ``_save()``'s own ``run_before_save_methods()``
    (:1402-1409) calls ``self.run_method("validate")`` only for ``self._action in ("save",
    "submit")`` — for ``_action == "cancel"`` it calls ``self.run_method("before_cancel")``
    instead, never ``"validate"``. ``AssetRepair.validate()`` (and its own ``update_status()``
    call, asset_repair.py:66/178-187) is reachable ONLY through ``run_method("validate")``, so it
    never executes during a cancel — confirmed independently: ``AssetRepair.on_cancel``
    (:222-233) itself never calls ``self.validate()``/``self.update_status()`` either. This is a
    correction to the pre-verification addendum's own Correction #1/Landing Risk #2, which claimed
    the Asset-status flip fires "on a plain draft save, on the governed submit, and on cancel
    alike" — it fires on save/submit only."""
    flags = []
    capitalized = bool(doc.get("capitalize_repair_cost"))
    if capitalized:
        flags.append(
            "capitalize_repair_cost is set: cancel REVERSES update_asset_value (asset_repair.py:"
            "224-227, negated via the docstatus==1 check at :243), make_gl_entries(cancel=True) "
            "(ignore_linked_doctypes=('GL Entry', 'Stock Ledger Entry'), :314), and "
            "set_increase_in_asset_life — plus reschedule_depreciation and add_asset_activity — "
            "the full submit-side family, reversed")
    else:
        flags.append(
            "capitalize_repair_cost is not set: cancel reverses NOTHING on the linked Asset (the "
            "same gate as submit, asset_repair.py:224 — nothing was capitalized, so there is "
            "nothing to reverse)")
    flags.append(
        "cancel does NOT re-run update_status() — unlike submit, this cancel never touches the "
        "linked Asset's own status field (validate() does not run during a cancel action; see "
        "this function's own docstring, a correction to the pre-verification addendum)")
    if doc.get("stock_items"):
        flags.append(
            "stock_items is non-empty: the Stock Entry this Asset Repair's own submit created is "
            "NOT auto-cancelled here (on_cancel never references it) — ERPNext itself leaves it "
            "standing; if it is still submitted it will also appear as a submitted linked "
            "document, blocking a leaf plan_cancel and routing through plan_cascade_cancel "
            "instead (the SAME Stock Entry edge the blast-radius gate above already walks)")
    return flags


def _invoice_discounting_submit_risk_flags(doc):
    """Invoice Discounting breadth (2026-07-21) — the submit-direction disclosures. Doctype-scoped
    at the call site; every flag below is data-driven off the draft's own fields except THE
    INCOMING JOURNAL ENTRY REACH (the last one — an unconditional, structural disclosure). Source
    pins live in ``erpnext.py``'s module docstring ("Breadth (Invoice Discounting)").

    Like Asset/Asset Repair, this doctype's native ledger preview is CALLABLE (``make_gl_entries``,
    invoice_discounting.py:130-190 — its OWN method, the THIRD such row) — it does NOT join the
    skip tuple.

    THE DETERMINISTIC DOOMED SUBMIT (addendum Correction 2 — entirely missing from the dossier):
    ``validate_mandatory`` (:59-61) throws whenever ``self.docstatus == 1`` and NOT
    ``(loan_start_date and loan_period)`` — readable off the draft's own fields before a marker is
    ever minted, the same "submit will be REFUSED" shape Maintenance Schedule/Asset Repair's own
    landings established.

    THE TWO CROSS-DOCUMENT THROWS (addendum Correction 2, cont'd) — ``validate_invoices``
    (:63-87) fires on EVERY save, not merely submit: it reads live ``Discounted Invoice`` records
    (docstatus=1, :64-69) to refuse a Sales Invoice already discounted elsewhere (:73-77), and reads
    each named Sales Invoice's LIVE ``outstanding_amount`` (:79-81) to refuse a stale/inflated child
    row (:83-87) — both cross-document, unverifiable from this draft alone, staying prose per the
    checklist's own rule (never collapsed into one flag, never promoted to draft-only).

    THE HONEST-EMPTY GL ROWS — ``make_gl_entries`` (:139-188) appends a GL pair for an ``invoices``
    row only when that row's own ``outstanding_amount`` (:142) is truthy; a zero/blank row posts
    nothing for itself, silently, disclosed here per the draft's own child rows.

    THE is_discounted SIDE-WRITE — ``update_sales_invoice`` (:119-128, called unconditionally from
    on_submit :93) sets ``is_discounted=1`` on every named Sales Invoice via a raw
    ``frappe.db.set_value`` (:128) — a hookless, per-row write into ANOTHER doctype, riding this
    same submit consent with no second marker.

    THE INCOMING JOURNAL ENTRY REACH (the row's sharpest finding, addendum Correction 1 corrected —
    see ``erpnext.py``'s own module docstring "THE CASCADE QUESTION"/"THE INCOMING MUTATOR"): this
    document's own ``status`` is not stable even once submitted — a governed Journal Entry
    elsewhere, carrying an ``accounts`` row with ``reference_type == "Invoice Discounting"``/
    ``reference_name`` naming THIS document, walks it through ``update_invoice_discounting``
    (journal_entry.py:459-501) straight to ``InvoiceDiscounting.set_status``'s own ``if status:``
    branch (invoice_discounting.py:101-108) — a raw ``db_set`` this document's OWN ``on_submit``/
    ``on_cancel`` never reach (they only ever pass ``cancel=1``) — and from there into EVERY one of
    this document's own linked Sales Invoices' status. Disclosed unconditionally (this is a
    structural fact about the doctype, not conditional on this draft's own fields) — the reciprocal
    half of the SAME finding Journal Entry's own ``plan_submit``/``plan_cancel`` disclose
    (:func:`_journal_entry_invoice_discounting_reach_flags`, retrofitted 2026-07-21, commit
    3fa3303) — same citations, same mechanism, never parallel-invented."""
    flags = []
    # validate_mandatory's own guard is "self.docstatus == 1" — but that check runs INSIDE the
    # submit transition itself (frappe flips docstatus to 1 in memory before validate() fires), so
    # from THIS plan's perspective (always evaluating "what happens if this draft is submitted")
    # the condition is simply whether loan_start_date/loan_period are both set now — docstatus is
    # never read a second time here, the same "deterministic-from-draft, scoped by the call site"
    # shape every other doomed-submit flag in this module already uses.
    if not (doc.get("loan_start_date") and doc.get("loan_period")):
        flags.append(
            "loan_start_date and loan_period are both required once docstatus reaches 1: "
            "validate_mandatory (invoice_discounting.py:59-61) throws 'Loan Start Date and Loan "
            "Period are mandatory to save the Invoice Discounting' — this submit will be REFUSED "
            "unless both are set first")
    flags.append(
        "validate_invoices (invoice_discounting.py:63-87) runs on EVERY save, not merely submit: "
        "it reads live Discounted Invoice records (docstatus=1) to refuse a Sales Invoice already "
        "discounted elsewhere (:73-77), and reads each named Sales Invoice's LIVE "
        "outstanding_amount to refuse a stale/inflated child-row value (:83-87) — both "
        "cross-document, unverifiable from this draft alone, staying prose")
    rows = [r for r in (doc.get("invoices") or []) if isinstance(r, dict)]
    zero_rows = [r.get("sales_invoice") for r in rows
                if not _coerce_float(r.get("outstanding_amount"))]
    if rows and zero_rows:
        named = ", ".join(repr(n) for n in zero_rows if n) or f"{len(zero_rows)} unnamed row(s)"
        flags.append(
            f"{len(zero_rows)} of {len(rows)} invoice row(s) carry a zero/blank "
            f"outstanding_amount ({named}): make_gl_entries (invoice_discounting.py:142-188) "
            "appends NO GL pair for that row at all, silently — a real per-ROW honest-empty "
            "split, never a whole-document gate")
    if rows:
        flags.append(
            f"on_submit (invoice_discounting.py:93) unconditionally calls update_sales_invoice, "
            f"which sets is_discounted=1 on all {len(rows)} named Sales Invoice(s) via a raw "
            "frappe.db.set_value (:128) — a hookless write into another doctype, riding this "
            "same submit consent with no second marker")
    flags.append(
        "this Invoice Discounting's own status is not stable even once submitted: a governed "
        "Journal Entry elsewhere naming THIS document (reference_type='Invoice Discounting') can "
        "walk it Sanctioned->Disbursed or Disbursed->Settled via update_invoice_discounting "
        "(journal_entry.py:459-501) -> InvoiceDiscounting.set_status's own 'if status:' branch "
        "(invoice_discounting.py:101-108, a raw db_set this document's own on_submit/on_cancel "
        "never reach), cascading into every one of its own linked Sales Invoices' status in the "
        "same motion — entirely outside this document's own hooks. See the reciprocal disclosure "
        "on Journal Entry's own plan_submit/plan_cancel (retrofitted 2026-07-21)")
    return flags


def _invoice_discounting_cancel_risk_flags(doc):
    """Invoice Discounting breadth (2026-07-21) — the cancel-direction disclosures. Doctype-scoped
    at the call site; data-driven off the draft's own ``invoices`` rows, except THE INCOMING
    JOURNAL ENTRY REACH's own closing half (unconditional, structural — the cancel-direction voice
    of the submit-direction disclosure above).

    ``on_cancel`` (invoice_discounting.py:96-99): ``set_status(cancel=1)`` (own ``db_set``, :117,
    status -> 'Cancelled'); ``update_sales_invoice`` (:119-128, cancel direction — ``is_discounted``
    cleared to 0 ONLY when no OTHER submitted Discounted Invoice record still names the same Sales
    Invoice, :123-127, a conditional cross-document read); ``make_gl_entries`` reversal
    (``cancel=True``, :190).

    THIS DOCTYPE IS NOT A CASCADE LEAF (see ``erpnext.py``'s own module docstring "THE CASCADE
    QUESTION") — a submitted Journal Entry naming this document via the ``reference_type``
    Select/``reference_name`` Dynamic Link pair is what this broker's own blast-radius gate
    (``get_submitted_linked_docs``) already refuses a leaf cancel over, routing through
    ``plan_cascade_cancel`` instead; disclosed there, not repeated as a flag here."""
    flags = []
    flags.append(
        "on_cancel (invoice_discounting.py:96-99) sets status to 'Cancelled' via its own db_set "
        "(:117), then update_sales_invoice's cancel direction clears is_discounted to 0 on each "
        "named Sales Invoice ONLY when no OTHER submitted Discounted Invoice record still names "
        "it (:123-127) — a conditional cross-document read — and reverses the GL entries posted "
        "at submit (make_gl_entries(cancel=True), :190)")
    flags.append(
        "once this cancel is confirmed (status -> 'Cancelled' via this document's own db_set, "
        ":117), a further Journal Entry naming this same reference_name is itself refused by "
        "update_invoice_discounting's own cross-document stage check (journal_entry.py:460-467, "
        "_validate_invoice_discounting_status — the four expected-current-status checks across "
        "both JE directions are Sanctioned/Disbursed (JE submit) and Disbursed/Settled (JE "
        "cancel), NEVER Cancelled) — the "
        "Sanctioned->Disbursed/Disbursed->Settled walk this document's own submit-direction "
        "disclosure names cannot resume once this document is genuinely cancelled. Before that "
        "point, a submitted Journal Entry already naming this document is exactly what makes it "
        "NOT a cascade leaf (see the blast-radius gate above) — this cancel is refused outright "
        "while one exists")
    return flags


def _asset_capitalization_submit_risk_flags(doc):
    """Asset Capitalization breadth (2026-07-21) — the submit-direction disclosures. Doctype-scoped
    at the call site (fired only for ``doctype == ASSET_CAPITALIZATION``); flags are data-driven off
    the draft's own fields where the draft can decide it (the doomed-submit gate; whether the
    sibling-document factory is armed at all), prose where a live read of another document (the
    target/consumed Asset's own state) would be needed and this plan cannot perform one. Source pins
    live in ``erpnext.py``'s module docstring ("Breadth (Asset Capitalization)").

    Like Asset/Asset Repair/Invoice Discounting, this doctype's native ledger preview is CALLABLE
    (``make_gl_entries``, asset_capitalization.py:388-398 — its OWN method, dispatching to the
    framework's ``make_gl_entries``/``make_reverse_gl_entries`` by ``docstatus``) — it does NOT join
    the skip tuple.

    THE DETERMINISTIC DOOMED SUBMIT (entirely missing from the dossier) — ``before_submit``
    (:111-113) calls ``validate_source_mandatory`` (:286-292): throws whenever ALL THREE child
    tables (``stock_items``/``asset_items``/``service_items``) are empty, readable off the draft
    alone before a marker is ever minted.

    THE TARGET-ASSET COST WRITE — unconditional, fires on every successful submit.
    ``update_target_asset()`` (:554-573) ADDS this document's own ``total_value`` onto the target
    Asset's ``net_purchase_amount``/``purchase_amount``/``total_asset_cost`` via a single 3-field
    batched ``db_set`` (:567) — a raw ORM bypass (validate/hooks/version-history skipped) on ANOTHER
    document. **THE DOSSIER'S OWN DOUBLE-COUNT RED FLAG IS REFUTED** (see ``erpnext.py``'s own module
    docstring "THE HEADLINE RULING"): Frappe's own ``validate_amended_from`` blocks any amendment
    until the original's own cancel (and its matching subtract) has already landed, so add/subtract
    always alternate 1:1 by construction — no bespoke dedup guard exists here because none is needed.

    THE SIBLING-DOCUMENT FACTORY (submit direction) — armed off this draft's own ``asset_items``
    rows (presence = armed; the underlying ``calculate_depreciation`` gate lives on the CONSUMED
    Asset, a live cross-document read this plan cannot perform, disclosed as prose per the standing
    rule). The SECOND confirmed instance of Asset Value Adjustment's own ``reschedule_depreciation``
    factory (``a39adce``), reached via a different call path (consumed-asset depreciation, never
    AVA's own ``update_asset()``) — see ``erpnext.py``'s own module docstring for the full
    line-by-line trace.

    THE ASYNC CHANNEL — unconditional, this direction. ``repost_future_sle_and_gle`` (called from
    ``on_submit``, line 119, inherited from ``StockController``) can arm ERPNext's own scheduled
    "Repost Item Valuation" reposting job — an armed channel this broker does not govern (the Asset
    precedent)."""
    flags = []
    if not (doc.get("stock_items") or doc.get("asset_items") or doc.get("service_items")):
        flags.append(
            "stock_items, asset_items, and service_items are ALL empty: this submit will be "
            "REFUSED — validate_source_mandatory (asset_capitalization.py:286-292, called from "
            "before_submit at :111-113) throws 'Consumed Stock Items, Consumed Asset Items or "
            "Consumed Service Items is mandatory for Capitalization'")
    flags.append(
        "submitting this Asset Capitalization ADDS this document's own total_value onto the "
        "target Asset's net_purchase_amount/purchase_amount/total_asset_cost via a single "
        "3-field batched db_set (update_target_asset, asset_capitalization.py:554-573, add "
        "branch at 562-565, db_set at 567) — a raw ORM bypass (validate/hooks/version-history "
        "skipped) on ANOTHER document. This is symmetric with cancel's own subtract (see "
        "_asset_capitalization_cancel_risk_flags): Frappe's own validate_amended_from "
        "(frappe/model/document.py:613-618) blocks any amendment until the original's own cancel "
        "has already landed, so a re-submit can never double-count this add — the dossier's own "
        "RED FLAG here is refuted, not merely noted")
    if doc.get("asset_items"):
        flags.append(
            "asset_items is non-empty: submit calls depreciate_asset (depreciation.py:477-487) "
            "for each consumed asset with calculate_depreciation set (a live read of the CONSUMED "
            "Asset this plan cannot perform) — reschedule_depreciation cancels an existing "
            "submitted Asset Depreciation Schedule and CREATES+SUBMITS a replacement "
            "(asset_depreciation_schedule.py:194-219, the SAME function Asset Value Adjustment's "
            "own landing named), and make_depreciation_entry_on_disposal can, via "
            "_make_journal_entry_for_depreciation, CREATE+SUBMIT a catch-up Journal Entry for any "
            "unposted schedule row at/before the disposal date (depreciation.py:216-249, "
            "ignore_permissions=True) — the campaign's SECOND confirmed sibling-document-factory "
            "row, reached through a different call path than AVA's own. set_consumed_asset_status "
            "also flips each consumed asset's own status to 'Capitalized' via a raw db_set "
            "(asset_capitalization.py:596-604)")
    flags.append(
        "repost_future_sle_and_gle (asset_capitalization.py:119, inherited from StockController) "
        "can create a 'Repost Item Valuation' document processed by ERPNext's OWN scheduled "
        "stock-reposting job — an armed channel entirely outside this broker's consent, the same "
        "disclosure shape Asset's own scheduled depreciation/CWIP channels already carry")
    return flags


def _asset_capitalization_cancel_risk_flags(doc):
    """Asset Capitalization breadth (2026-07-21) — the cancel-direction disclosures. Doctype-scoped
    at the call site; the sibling-factory reversal is data-driven off the draft's own ``asset_items``
    rows (the same presence-armed shape the submit direction uses); THE TARGET-ASSET COST REVERSAL
    and THE ASYNC CHANNEL are unconditional prose, the same mechanisms the submit direction
    discloses, sign-flipped internally by ``self.docstatus`` (never by this disclosure layer).

    THIS DOCTYPE IS A GENUINE CASCADE LEAF (see ``erpnext.py``'s own module docstring) — an
    independent full two-checkout grep for ``'"options": "Asset Capitalization"'`` returns only this
    doctype's own self-referencing ``amended_from``; no submitted document anywhere can block this
    cancel via the blast-radius gate. ``on_cancel``'s own ``ignore_linked_doctypes`` tuple
    (asset_capitalization.py:123-130) also suppresses ERPNext's own generic linked-doc check for GL
    Entry/Stock Ledger Entry/Repost Item Valuation/Serial and Batch Bundle/Asset/Asset Movement —
    disclosed here as context, not repeated as a flag (nothing links in for the gate to catch)."""
    flags = []
    flags.append(
        "cancelling this Asset Capitalization SUBTRACTS the same total_value this document added "
        "at submit from the target Asset's net_purchase_amount/purchase_amount/total_asset_cost "
        "(update_target_asset, asset_capitalization.py:554-573, subtract branch at 558-561, the "
        "same 3-field batched db_set at 567) — the reversal half of the submit-direction "
        "disclosure, confirmed symmetric by construction (see erpnext.py's own module docstring "
        "'THE HEADLINE RULING'), never by a bespoke guard")
    if doc.get("asset_items"):
        flags.append(
            "asset_items is non-empty: cancel calls reverse_depreciation_entry_made_on_disposal "
            "(depreciation.py:504-516), which calls create_reverse_depreciation_entry "
            "(depreciation.py:544-563) to CREATE+SUBMIT a reversing Journal Entry when a matching "
            "schedule row's own journal_entry/disposal-timing conditions hold, and "
            "reset_depreciation_schedule (depreciation.py:498-501, which ALSO calls "
            "reschedule_depreciation — cancel+recreate+submit the schedule again) — the mirror of "
            "the submit-direction sibling-document factory. set_consumed_asset_status also "
            "restores each consumed asset's own status via Asset.set_status() called with NO "
            "argument (asset_capitalization.py:605-612, the call itself at line 606), recomputed "
            "fresh from the asset's own docstatus/depreciation state (asset.py:776-808) — never a "
            "guess, and never reachable while a second, independent Asset Capitalization still "
            "holds this same asset (validate_consumed_asset_item refuses any such document before "
            "it can ever be created — see erpnext.py's own module docstring)")
    flags.append(
        "repost_future_sle_and_gle (asset_capitalization.py:133, inherited from StockController) "
        "can ALSO arm a 'Repost Item Valuation' document on cancel — the same ERPNext-scheduled, "
        "broker-invisible channel the submit direction discloses")
    return flags


# The four founding stock rows' repost call sites — THE CONFIRMED RETROACTIVE GAP the Asset
# Capitalization landing reported (commit 5f74f17: "Stock Entry (:566/:602), Stock Reconciliation
# (:114/:126), Delivery Note (:479/:500), Purchase Receipt (:396/:467) all call the identical
# repost machinery and NONE has a risk-flag function disclosing it — banked as campaign debt").
# Every line re-verified against the v16.28.0 checkout (de59166) during the 2026-07-22 debt pass:
# per doctype, (submit-direction site, cancel-direction site), each inside its own
# on_submit/on_cancel path.
_STOCK_REPOST_SITES = {
    STOCK_ENTRY: ("stock_entry.py:566", "stock_entry.py:602"),
    STOCK_RECONCILIATION: ("stock_reconciliation.py:114", "stock_reconciliation.py:126"),
    DELIVERY_NOTE: ("delivery_note.py:479", "delivery_note.py:500"),
    PURCHASE_RECEIPT: ("purchase_receipt.py:396", "purchase_receipt.py:467"),
}


def _stock_repost_channel_flag(doctype, direction):
    """The repost-channel disclosure for the four founding stock rows (2026-07-22 debt pass) —
    the SAME async-channel shape Asset/Asset Capitalization/Subcontracting Receipt already
    carry, retrofitted to the rows that landed before the shape existed. Doctype-scoped off
    :data:`_STOCK_REPOST_SITES`; a no-op for every other doctype.

    THE MECHANISM (stock_controller.py:1830-1854, one shared inherited method, re-read at
    source during this pass): ``repost_future_sle_and_gle`` builds a Repost Item Valuation
    document (or item-wise repost entries, per the site's own Stock Reposting Settings)
    processed by ERPNext's OWN scheduled stock-reposting job — an armed channel entirely
    outside this broker's consent. THE DIRECTIONAL ASYMMETRY, the sharp part: on cancel the
    method's own ``if self.docstatus == 2: force = True`` (:1842-1843) makes the arming
    UNCONDITIONAL — every governed cancel of these doctypes arms the channel; on submit it is
    conditional (``force or future_sle_exists(args) or repost_required_for_queue(self)``,
    :1845) — armed only when this posting back-dates against existing future Stock Ledger
    Entries (or the queue already requires it), which is exactly the backdated-posting case a
    consenting human most needs named."""
    sites = _STOCK_REPOST_SITES.get(doctype)
    if not sites:
        return []
    if direction == "submit":
        return [
            f"repost_future_sle_and_gle ({sites[0]}, inherited from StockController "
            "stock_controller.py:1830-1854) can create a 'Repost Item Valuation' document "
            "processed by ERPNext's OWN scheduled stock-reposting job — an armed channel "
            "entirely outside this broker's consent (the Asset/Asset Capitalization disclosure "
            "shape). On submit this arms CONDITIONALLY (:1845): only when this posting is "
            "back-dated against existing future Stock Ledger Entries for its items (or the "
            "repost queue already requires it) — a live cross-document read this plan cannot "
            "perform, so whether it fires is not knowable from this draft alone"]
    return [
        f"repost_future_sle_and_gle ({sites[1]}, inherited from StockController "
        "stock_controller.py:1830-1854) arms a 'Repost Item Valuation' document "
        "UNCONDITIONALLY on this cancel — the method's own 'if self.docstatus == 2: force = "
        "True' (:1842-1843) short-circuits every condition, so ERPNext's OWN scheduled "
        "stock-reposting job re-derives downstream valuations after this cancel, hours or "
        "minutes later, entirely outside this broker's consent — the same channel the submit "
        "direction arms only for back-dated postings"]


def _work_order_submit_risk_flags(doc):
    """Work Order reserve_stock disclosure (2026-07-22 debt pass) — THE SECOND CONFIRMED
    RETROACTIVE GAP the Production Plan landing reported (commit ff941a5's forward check: WO's
    landed row had ZERO reserve_stock/StockReservation disclosure and no risk-flag functions at
    all, while PP's own landing had already refined the WO channel to a TRANSFER). Every cite
    re-verified against the v16.28.0 checkout (de59166) during this pass. Data-driven off the
    draft's own ``reserve_stock``/``production_plan``/``subcontracting_inward_order`` — a no-op
    for an unreserved draft.

    THE DISPATCH (work_order.py:944-945 ``on_submit`` -> ``update_stock_reservation`` :973-976
    -> the module-level ``make_stock_reservation_entries`` :2388-2419, branching on the draft's
    own shape at docstatus 1): a ``production_plan``-linked order TRANSFERS the plan's own
    reservation entries to new Work-Order-voucher ones
    (``transfer_reservation_entries_to``, stock_reservation_entry.py:1267+ — the PP landing's
    own refinement: new WO-voucher SREs plus a lifecycle-bypass update on the source entries,
    never a cancel); a ``subcontracting_inward_order``-linked one transfers from the SCIO with
    a ``qty_change`` adjustment; a standalone one CREATES AND SUBMITS a Stock Reservation Entry
    per required-items row (the wrapper's own :meth:`make_stock_reservation_entries`,
    stock_reservation_entry.py:1135-1211 — ``sre.save()`` then ``sre.submit()``).

    THE SILENT PARTIAL (:1175-1177): ``qty_to_be_reserved = qty if available >= qty else
    available`` — a short warehouse silently reserves LESS than the order's requirement, no
    throw, no message naming the shortfall. THE LIVE-STATE THROWS: zero available stock throws
    (``throw_stock_not_exists_error``, :1232) and a WO row without a source warehouse throws
    ("Source Warehouse is mandatory", :1164, unless skip_transfer/from_wip_warehouse) — live
    Bin/warehouse reads this plan cannot perform. THE STATUS WRITES: ``update_stock_reservation``
    ends in ``self.db_set("status", ...)`` (:976) and the module function repeats it (:2419) —
    raw single-field writes, no validate/hooks/versioning."""
    flags = []
    if not doc.get("reserve_stock"):
        return flags
    if doc.get("production_plan"):
        channel = (
            "this draft names a Production Plan, so the reservation arrives as a TRANSFER: the "
            "plan's own submitted Stock Reservation Entries are re-keyed into new "
            "Work-Order-voucher entries (transfer_reservation_entries_to, "
            "stock_reservation_entry.py:1267+) with a lifecycle-bypass update on the source "
            "entries — never a cancel — exactly the mechanism Production Plan's own landing "
            "refined")
    elif doc.get("subcontracting_inward_order"):
        channel = (
            "this draft names a Subcontracting Inward Order, so the reservation arrives as a "
            "TRANSFER from that order's own entries with a qty_change adjustment "
            "(work_order.py:2405-2411)")
    else:
        channel = (
            "a Stock Reservation Entry is CREATED AND SUBMITTED per required-items row "
            "(stock_reservation_entry.py:1135-1211, sre.save() then sre.submit()) — sibling "
            "submitted documents riding this same consent")
    flags.append(
        "reserve_stock is set: submitting this Work Order arms ERPNext's stock-reservation "
        "machinery (on_submit, work_order.py:944-945 -> update_stock_reservation :973-976 -> "
        f"make_stock_reservation_entries :2388-2419). {channel}. THE SILENT PARTIAL: a short "
        "warehouse reserves LESS than required with no throw and no shortfall named "
        "(stock_reservation_entry.py:1175-1177, qty_to_be_reserved = min(required, available)); "
        "zero available stock DOES throw (throw_stock_not_exists_error, :1232) and a row "
        "without a source warehouse throws ('Source Warehouse is mandatory', :1164, unless "
        "skip_transfer/from_wip_warehouse) — live Bin/warehouse reads this plan cannot "
        "perform. The order's own status is then rewritten via raw db_set "
        "(work_order.py:976 and again :2419) — no validate, no hooks, no version history")
    if doc.get("sales_order"):
        flags.append(
            "reserve_stock + sales_order: validate re-checks this order's fg_warehouse against "
            "the Sales Order's own item warehouses (validate_fg_warehouse_for_reservation, "
            "work_order.py:306-325) and THROWS on a mismatch ('Target Warehouse Reservation "
            "Error') — a live cross-document read this plan cannot perform")
    return flags


def _work_order_cancel_risk_flags(doc):
    """Work Order reserve_stock disclosure, cancel direction (2026-07-22 debt pass — see
    :func:`_work_order_submit_risk_flags` for the discovery story and re-verification). Armed
    off the draft's own ``reserve_stock``; a no-op otherwise."""
    flags = []
    if not doc.get("reserve_stock"):
        return flags
    flags.append(
        "reserve_stock is set: cancelling this Work Order auto-CANCELS every submitted Stock "
        "Reservation Entry held against it, under this same consent (on_cancel, "
        "work_order.py:949-951 -> on_close_or_cancel :968-969 -> update_stock_reservation "
        ":973-976 -> make_stock_reservation_entries :2388-2419, whose docstatus==2 branch "
        "routes to cancel_stock_reservation_entries, stock_reservation_entry.py:1100) — "
        "sibling cancels riding one marker. on_cancel also stamps status='Cancelled' via raw "
        "db_set (:951) and update_stock_reservation re-derives it (:976) — raw single-field "
        "writes, no validate/hooks/versioning")
    return flags


def _production_plan_ledger_preview_unavailable_flag(doctype):
    """Production Plan breadth (2026-07-21) — the same bare-``Document`` "uncallable" shape every
    prior control/planning doctype in this skip tuple carries. Fires unconditionally for
    ``doctype == PRODUCTION_PLAN``, submit-direction only.

    ``class ProductionPlan(Document):`` (``production_plan.py:41``) — a direct ``Document``
    subclass, never ``AccountsController``/``StockController``. A full-file grep of
    ``production_plan.py`` (2261 lines) finds no ``make_gl_entries`` reference anywhere. ERPNext's
    own ``get_accounting_ledger_preview`` calling ``doc.make_gl_entries()`` bare and unguarded
    would raise ``AttributeError`` on a live bench for a real Production Plan, so
    ``_tool_plan_submit`` skips the network call entirely for this doctype too — joining the skip
    tuple, its TWENTY-NINTH member. Production Plan never posts a GL or Stock Ledger entry of any
    kind under its own name — it is a planning/orchestration layer; the Work Orders/Material
    Requests/Stock Entries it spawns (via whitelisted button RPCs this broker does not expose) are
    what actually post."""
    if doctype != PRODUCTION_PLAN:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THE PREVIEW COULD NOT RUN, NOT BECAUSE NOTHING WOULD "
        "POST: Production Plan has no make_gl_entries method anywhere in its class hierarchy "
        "(ProductionPlan(Document) directly, never AccountsController or StockController) — "
        "ERPNext's own preview RPC would call doc.make_gl_entries() as a bare method call and "
        "raise AttributeError on a live bench, so this broker does not call it for a Production "
        "Plan at all. Production Plan posts no GL or Stock Ledger entry of any kind, ever — it "
        "is a manufacturing planning layer, not a posting document"]


def _production_plan_submit_risk_flags(doc):
    """Production Plan breadth (2026-07-21) — the submit-direction disclosure, off the
    pre-verification addendum (``docs/plans/dossiers/production_plan.verify.md``) and this
    landing's own re-derivation from source. Doctype-scoped at the call site (fired only for
    ``doctype == PRODUCTION_PLAN``).

    THE TWO AUTO-SUBMIT CHANNELS, the dossier's own headline finding OVERTURNED on the sharper of
    the two (see ``erpnext.py``'s own module docstring "TWO AUTO-SUBMIT CHANNELS" for the full
    source-cited trace): (1) Material Request submission is gated by a CALLER-supplied,
    non-persisted ``submit_material_request`` flag (unconditional prose — there is no draft field
    to read); (2) Stock Reservation Entry auto-submit is UNCONDITIONAL once the real
    ``reserve_stock`` Check field is set — data-driven off the draft alone, the dossier's own
    "auto-submitted: NO" claim overturned. THE FORWARD CHECK — ``reserve_stock`` propagates onto
    every Work Order this plan creates (``create_work_order``, ``production_plan.py:934``) — is
    disclosed here too, armed off the same field, naming the CONFIRMED RETROACTIVE GAP in Work
    Order's own landing (which never discloses this channel)."""
    flags = []
    reserve_stock = bool(doc.get("reserve_stock"))
    flags.append(
        "make_material_request (production_plan.py:961-1039, a whitelisted button RPC this "
        "broker does not expose) submits each Material Request it creates ONLY if the CALLER "
        "sets a transient, non-persisted submit_material_request key on the request "
        "(production_plan.js:330-345) — submit_material_request is confirmed NOT a doctype "
        "field (absent from the 58-field enumeration); this is caller-controlled, never "
        "readable off the saved draft, so it cannot be graded as a draft-deterministic flag")
    if reserve_stock:
        flags.append(
            "reserve_stock is SET on this draft: submitting will UNCONDITIONALLY create AND "
            "submit Stock Reservation Entries for every reservable sub_assembly_items/mr_items "
            "row (update_stock_reservation, production_plan.py:603-607 -> module "
            "make_stock_reservation_entries, :2216-2249 -> StockReservation."
            "make_stock_reservation_entries, stock_reservation_entry.py:1135-1211, sre.save() "
            "at stock_reservation_entry.py:1202 then sre.submit() at "
            "stock_reservation_entry.py:1208 with NO further gate) — the dossier's own "
            "'auto-submitted: NO' claim for this channel is overturned; this is the sharper of "
            "the two auto-submit channels because reserve_stock alone (a plain Check field) is "
            "the only condition, unlike Material Request's caller-flag gate")
        flags.append(
            "reserve_stock is ALSO set on every Work Order this plan creates "
            "(create_work_order, production_plan.py:934, wo.reserve_stock = self.reserve_stock) "
            "— a Work Order born from this plan will itself TRANSFER (not re-create) this "
            "plan's own Stock Reservation Entries onto itself when a user later submits it "
            "(StockReservation.transfer_reservation_entries_to, "
            "stock_reservation_entry.py:1267-1377, which creates+submits new Work-Order-voucher "
            "-typed SREs) — Work Order's OWN landing (the 21st supported doctype) does not "
            "disclose this channel anywhere (grepped: no reserve_stock/StockReservation mention "
            "in its own module-docstring section, no dedicated risk-flag function exists) — a "
            "CONFIRMED RETROACTIVE GAP, reported here, not silently fixed on Work Order's row")
    else:
        flags.append(
            "reserve_stock is NOT set on this draft — no Stock Reservation Entries will be "
            "created on submit, and any Work Order later created from this plan will not "
            "inherit a truthy reserve_stock either")
    sub_assembly_items = doc.get("sub_assembly_items") or []
    armed_rows = [
        row.get("idx") for row in (doc.get("mr_items") or [])
        if reserve_stock and row.get("main_item_code") and row.get("from_bom")
        and not any(sa.get("production_item") == row.get("main_item_code")
                   and sa.get("bom_no") == row.get("from_bom")
                   for sa in sub_assembly_items)
    ]
    if armed_rows:
        flags.append(
            f"mr_items row(s) {armed_rows} carry reserve_stock + main_item_code + from_bom "
            "with no draft-visible matching sub_assembly_item — add_reference_to_raw_materials "
            "(production_plan.py:609-630, called from on_submit) MAY throw at submit "
            "('Sub assembly item references are missing...', :626-630) depending on a LIVE "
            "frappe.get_cached_value('BOM', from_bom, 'item') comparison this plan cannot read "
            "— disclosed as an arming condition, not a hard REFUSED gate, since the final byte "
            "needs a live BOM read")
    flags.append(
        "on_submit also runs update_bin_qty (raw Bin.reserved_qty_for_production_plan writes, "
        "production_plan.py:669-680) and update_sales_order (hookless frappe.db.set_value "
        "point-writes of production_plan_qty onto EVERY referenced Sales Order Item, "
        ":632-647, into documents this broker does not separately govern)")
    flags.append(
        "set_status (whitelisted instance method, production_plan.py:688-711, callable with "
        "close=True/False and NO docstatus guard of its own) can force status to 'Closed' via "
        "a raw db_set at ANY docstatus, bypassing validate/hooks — nothing granted toward it "
        "by this landing")
    return flags


def _production_plan_cancel_risk_flags(doc):
    """Production Plan breadth (2026-07-21) — the cancel-direction disclosure. Shares THE
    CANCEL-ORDERING WRINKLE (see ``erpnext.py``'s own module docstring) — this doctype is
    genuinely NOT A LEAF (three real incoming Link edges: Work Order's own direct header Link,
    plus Purchase Order Item's/Material Request Item's own child-table Links resolving to their
    submittable parents), so a submitted Work Order/Purchase Order/Material Request still
    referencing this plan blocks the leaf ``plan_cancel`` outright (the standing blast-radius
    gate, exercised generically, zero ``cascade.py`` changes) — reached here only once no such
    submitted dependent remains (a genuine leaf cancel) or as the per-node disclosure inside a
    governed ``plan_cascade_cancel`` graph where this plan is cancelled LAST."""
    flags = []
    flags.append(
        "this Production Plan is NOT a cascade leaf: Work Order.production_plan (a direct "
        "header Link on submittable Work Order), Purchase Order Item.production_plan, and "
        "Material Request Item.production_plan (both child-table Links resolving to their "
        "submittable parents via frappe's own linked_with mechanism) are real incoming edges — "
        "a submitted dependent on any of the three blocks a leaf cancel outright; ERPNext's own "
        "on_cancel would otherwise run delete_draft_work_order/update_bin_qty/update_sales_"
        "order/update_stock_reservation BEFORE its own back-link check raises "
        "frappe.LinkExistsError (frappe/model/document.py:1450-1452), so this broker's own gate "
        "refusing FIRST is strictly safer than a raw native cancel")
    flags.append(
        "cancelling ALSO hard-deletes every DRAFT Work Order still naming this plan "
        "(delete_draft_work_order, production_plan.py:682-686, frappe.delete_doc — no trash, "
        "no undo) — submitted Work Orders are untouched by this method (they are what blocks "
        "the cancel above, not what this method reaches)")
    flags.append(
        "on_cancel also reverses update_bin_qty (Bin.reserved_qty_for_production_plan writes) "
        "and update_sales_order (the SAME hookless frappe.db.set_value point-writes the submit "
        "direction discloses, recomputed with this plan's own now-cancelled rows excluded)")
    if doc.get("reserve_stock"):
        flags.append(
            "reserve_stock is SET: cancelling calls the SAME update_stock_reservation "
            "(production_plan.py:603-607) -> module make_stock_reservation_entries "
            "(:2216-2249), which on docstatus==2 (:2246-2247) calls StockReservation."
            "cancel_stock_reservation_entries (stock_reservation_entry.py:1100-1133) — this "
            "queries ONLY docstatus==1 Stock Reservation Entries matching this document's own "
            "voucher_type/voucher_no and calls sre_doc.cancel() on each (:1127): a genuinely "
            "SYMMETRIC, correct reversal (unlike Pick List's own two-jaw trap, this cancel does "
            "not orphan any live reservation it created)")
    return flags


def _subcontracting_order_mutator_map_flag(direction):
    """Subcontracting Order breadth (2026-07-21) — THE SEVEN-PATH MUTATOR MAP, the row's center
    per the pre-verification addendum (``docs/plans/dossiers/subcontracting_order.verify.md``,
    "The submitted-state mutator map"), shared verbatim by both
    :func:`_subcontracting_order_submit_risk_flags` and
    :func:`_subcontracting_order_cancel_risk_flags` so neither direction understates it.
    Unconditional, cross-document prose (per the campaign's own "cross-document state stays
    prose" rule) — true regardless of what THIS draft currently holds.

    Six of seven paths that rewrite an already-submitted Subcontracting Order's own ``status``
    field and its ``Subcontracting Order Supplied Item`` child rows (``consumed_qty``/
    ``supplied_qty``/``returned_qty``/``total_supplied_qty``) carry NO write-permission check at
    all, firing as ordinary submit/cancel side effects of TWO OTHER submittable doctypes, never
    this order's own lifecycle: Subcontracting Receipt's ``on_submit``/``on_cancel``
    (``subcontracting_receipt.py:171``/``:195``) call ``set_subcontracting_order_status``
    (``subcontracting_controller.py:1272-1280``, ends in ``db_set``) AND
    ``set_consumed_qty_in_subcontract_order`` (``:1139-1160``, ending in a RAW module-level
    ``frappe.db.set_value`` straight into the child table, ``:1135-1137`` — no ``Document``
    instantiation on either side of the relationship, the sharpest honesty grade in this
    campaign's own taxonomy, worse than a ``.db_set()``/``.db_update()`` bypass because there is
    not even a loaded parent doc involved); Stock Entry's ``on_submit``/``on_cancel``
    (``stock_entry.py:546-563``/``:578-585``) run THREE separate reach-backs:
    ``reserve_stock_for_subcontracting`` (``:2420-2442``, calls the whitelisted
    ``reserve_raw_materials()`` DIRECTLY as an in-process Python call — the
    ``@frappe.whitelist()`` decorator is INERT here, it only gates HTTP),
    ``update_subcontract_order_supplied_items`` (``:3855-3888``, the SAME raw
    ``frappe.db.set_value`` pattern into the SAME child table, a THIRD independent writer of it),
    and ``update_subcontracting_order_status`` (``:4054-4062``) which routes through the
    UNWHITELISTED module function deliberately, per an upstream source comment quoted verbatim:
    'Trusted submit/cancel flow — a Stock operation must not require Subcontracting Order write
    permission, so use the no-check internal helper' (``stock_entry.py:4060-4061``). Only the 7th
    path — ``update_subcontracting_order_status``, the WHITELISTED module function
    (``subcontracting_order.py:493-500``), reached via ``check_permission("write")`` — carries any
    permission check at all."""
    return (
        f"this {direction}'s own status field and Subcontracting Order Supplied Item rows "
        "(consumed_qty/supplied_qty/returned_qty/total_supplied_qty) are NOT stable once "
        "submitted, independent of this document's OWN submit/cancel: Subcontracting Receipt's "
        "on_submit/on_cancel (subcontracting_receipt.py:171/:195) and Stock Entry's on_submit/"
        "on_cancel (stock_entry.py:546/:578, purpose Send to Subcontractor/Material Transfer) "
        "BOTH reach back into this order via internal, non-whitelisted helpers with ZERO "
        "write-permission check — one path is a RAW module-level frappe.db.set_value straight "
        "into the child table with no Document instantiation at all "
        "(subcontracting_controller.py:1135-1137, duplicated at stock_entry.py:3881), and the "
        "status path is a DELIBERATE bypass of the whitelisted boundary, per an upstream source "
        "comment quoted verbatim at stock_entry.py:4060-4061: 'a Stock operation must not "
        "require Subcontracting Order write permission, so use the no-check internal helper'. "
        "Of seven total mutator paths into this order's own status/supplied_items, only the "
        "whitelisted update_subcontracting_order_status (subcontracting_order.py:493-500) "
        "carries a write-permission check — the other six are routine consequences of "
        "submitting or cancelling a DIFFERENT document")


def _subcontracting_order_submit_risk_flags(doc):
    """Subcontracting Order breadth (2026-07-21) — the submit-direction disclosure, off the
    pre-verification addendum (``docs/plans/dossiers/subcontracting_order.verify.md``),
    re-verified from source. Doctype-scoped at the call site (fired only for
    ``doctype == SUBCONTRACTING_ORDER``).

    SIX ``validate()`` throws (``subcontracting_order.py:116-123`` chains
    ``validate_purchase_order_for_subcontracting``/``validate_service_items``/
    ``validate_supplied_items``) across seven line cites — the dossier never touched this surface
    at all. TWO are deterministic-from-draft (``:153`` no ``purchase_order`` set at all; ``:179``
    ``supplier_warehouse`` collides with a supplied item's own ``reserve_warehouse``) and become
    hard REFUSED flags. FOUR are cross-document (``:139``/``:142``/``:146``/``:150``, all need a
    LIVE read of the linked Purchase Order's own ``is_subcontracted``/
    ``is_old_subcontracting_flow``/``docstatus``/``per_received``) plus ``:165`` (a service
    item's ``Item.is_stock_item``, needs a live Item read) and stay prose, per the campaign's
    "cross-document state stays prose" rule.

    ``reserve_raw_materials`` (``subcontracting_order.py:347-412``) is an ``on_submit`` SIDE
    EFFECT (not merely a whitelisted callable a caller might separately invoke, the dossier's own
    RED FLAG 1 framing) — when ``reserve_stock`` is set, it runs unconditionally as PART of
    ``on_submit`` and creates Stock Reservation Entries via the SAME
    ``StockReservation.make_stock_reservation_entries()`` Production Plan's own landing already
    proved auto-submits with NO gate (``sre.save()`` then ``sre.submit()`` two lines later,
    ``stock_reservation_entry.py:1202``/``:1208`` — the identical shared wrapper class, so the
    identical unconditional auto-submit applies here too, confirmed directly rather than merely
    disclosed by analogy)."""
    flags = []
    if not doc.get("purchase_order"):
        flags.append(
            "submit will be REFUSED: no purchase_order is set on this draft "
            "(validate_purchase_order_for_subcontracting, subcontracting_order.py:153 — 'Please "
            "select a Subcontracting Purchase Order')")
    else:
        flags.append(
            "submit depends on a LIVE read of the linked Purchase Order "
            f"({doc.get('purchase_order')!r}): it will be REFUSED if that Purchase Order is not "
            "is_subcontracted (:139), is on the old subcontracting flow (:142), is not itself "
            "submitted (:146), or is already 100% subcontracted, per_received==100 (:150) — none "
            "of these four are readable off this draft alone")
    supplier_warehouse = doc.get("supplier_warehouse")
    colliding_rows = [
        row.get("idx") for row in (doc.get("supplied_items") or [])
        if supplier_warehouse and row.get("reserve_warehouse") == supplier_warehouse
    ]
    if colliding_rows:
        flags.append(
            f"submit will be REFUSED: supplied_items row(s) {colliding_rows} carry the SAME "
            f"reserve_warehouse as this order's own supplier_warehouse ({supplier_warehouse!r}) "
            "(validate_supplied_items, subcontracting_order.py:179 — 'Reserve Warehouse must be "
            "different from Supplier Warehouse')")
    flags.append(
        "validate_service_items (subcontracting_order.py:155-172) will ALSO REFUSE if any "
        "service_items row's own item is a stock item (:165, 'must be a non-stock item') — this "
        "needs a LIVE Item.is_stock_item read, never readable off this draft's own rows")
    if doc.get("reserve_stock"):
        flags.append(
            "reserve_stock is SET on this draft: on_submit's own reserve_raw_materials "
            "(subcontracting_order.py:125-128 -> :347-412) will UNCONDITIONALLY create AND "
            "submit Stock Reservation Entries for every supplied_items row (StockReservation."
            "make_stock_reservation_entries, stock_reservation_entry.py:1135-1211, sre.save() "
            "at :1202 then sre.submit() at :1208 with NO further gate — the SAME shared "
            "StockReservation class Production Plan's own landing already proved auto-submits "
            "unconditionally, confirmed here to apply identically) — if this order carries a "
            "production_plan back-reference and that plan's own SRE has unreserved qty "
            "matching, this takes a TRANSFER path instead (transfer_reservation_entries_to, "
            "stock_reservation_entry.py:1267-1377) rather than a fresh create")
    else:
        flags.append(
            "reserve_stock is NOT set on this draft — reserve_raw_materials will still run as "
            "an on_submit side effect but create no Stock Reservation Entries (its own body is "
            "gated on self.reserve_stock, subcontracting_order.py:349)")
    flags.append(_subcontracting_order_mutator_map_flag("submitted document"))
    flags.append(
        "on_submit also runs update_subcontracted_quantity_in_po (subcontracting_order.py:"
        "326-345), a raw frappe.db.set_value increment of the linked Purchase Order Item's own "
        "subcontracted_qty counter — a hookless point-write into a document this broker does "
        "not separately govern")
    return flags


def _subcontracting_order_cancel_risk_flags(doc):
    """Subcontracting Order breadth (2026-07-21) — the cancel-direction disclosure. THE CASCADE
    CORRECTION, this landing's own refinement beyond the dossier AND the addendum: both concluded
    (or repeated without correction) that Subcontracting Receipt Item/Subcontracting Receipt
    Supplied Item are 'non-submittable... never appear in submitted-links blast-radius reads' —
    conflating the CHILD doctype's own ``is_submittable: 0`` flag with whether the edge is walked
    at all. Both child doctypes are confirmed ``istable: 1`` under the submittable Subcontracting
    Receipt (``is_submittable: 1``, ``subcontracting_receipt.json``), and frappe's own
    ``get_submitted_linked_docs`` resolves a child-table Link hit back to its submittable PARENT
    via ``get_parent_if_child_table_doc=True`` (``frappe/desk/form/linked_with.py:121``,
    ``:328-363``) — the EXACT mechanism the Production Plan/Pick List/Timesheet landings already
    proved for Purchase Order Item/Material Request Item and Delivery Note Item/Sales Invoice
    Item. So this order is NOT a leaf on TWO independent submittable-referencer families, not the
    dossier's implied one: ``Stock Entry.subcontracting_order`` (a direct header Link,
    ``stock_entry.json:186``) AND ``Subcontracting Receipt Item``/``Supplied Item``'s own
    ``subcontracting_order`` Links (resolving to submittable Subcontracting Receipt) —
    reinforcing rather than contradicting THE SEVEN-PATH MUTATOR MAP above (Subcontracting
    Receipt is already proven to reach deep into this order's own status/child rows on its own
    submit/cancel; it would be an odd asymmetry for that same coupling to be invisible to the
    cascade walk)."""
    flags = []
    flags.append(
        "this Subcontracting Order is NOT a cascade leaf on TWO independent submittable-"
        "referencer families — a correction to the dossier's/addendum's own framing, which "
        "called Subcontracting Receipt Item/Supplied Item 'non-submittable, never appear in "
        "blast-radius reads': that conflates the CHILD doctype's own is_submittable=0 with "
        "whether the edge is walked at all. Both live under the submittable Subcontracting "
        "Receipt (is_submittable=1) and frappe's own get_submitted_linked_docs resolves a "
        "child-table Link hit back to its submittable PARENT (get_parent_if_child_table_doc="
        "True, frappe/desk/form/linked_with.py:121) — the same mechanism Production Plan/Pick "
        "List/Timesheet's own landings already proved. Real edges: Stock Entry."
        "subcontracting_order (a direct header Link) AND Subcontracting Receipt Item/Supplied "
        "Item.subcontracting_order (child-table Links resolving to submittable Subcontracting "
        "Receipt) — a submitted dependent on EITHER family blocks a leaf cancel outright")
    flags.append(_subcontracting_order_mutator_map_flag("submitted document"))
    flags.append(
        "on_cancel also reverses update_subcontracted_quantity_in_po (subcontracting_order.py:"
        "326-345, cancel=True) — the SAME raw frappe.db.set_value decrement of the linked "
        "Purchase Order Item's own subcontracted_qty counter the submit direction discloses")
    flags.append(
        "update_status() (subcontracting_order.py:292-324, called from on_cancel) carries its "
        "OWN cross-document gate the dossier's RED FLAGS never named: if this order's CURRENT "
        "status is already 'Closed' and the cancel is about to change it, check_on_hold_or_"
        "closed_status('Purchase Order', self.purchase_order) (subcontracting_order.py:293-294) "
        "throws if the linked Purchase Order's OWN status is On Hold or Closed — a live read "
        "this document's own draft cannot answer")
    if doc.get("reserve_stock"):
        flags.append(
            "reserve_stock is SET: unlike Production Plan's own symmetric cancel, this order's "
            "on_cancel (subcontracting_order.py:130-132) does NOT call "
            "cancel_stock_reservation_entries automatically — cancelling can leave live, "
            "submitted Stock Reservation Entries dangling against this now-cancelled order (no "
            "ignore_linked_doctypes set, confirmed absent from both the .py and .json) unless a "
            "caller separately invokes the whitelisted cancel_stock_reservation_entries() "
            "(subcontracting_order.py:421-429); Stock Reservation Entry's own voucher_no is a "
            "DynamicLink, not a plain Link, so it is invisible to this broker's own "
            "blast-radius gate too — a genuine orphan risk, the Pick List two-jaw-trap shape, "
            "for a different doctype family")
    flags.append(
        "downstream consequence, forward-looking: once this order's own status reads Closed or "
        "Cancelled, update_ordered_and_reserved_qty (subcontracting_controller.py:1162-1180, "
        "called from a Subcontracting Receipt's OWN on_submit -> update_stock_ledger) will "
        "REFUSE any future Subcontracting Receipt that tries to submit against it ('is "
        "cancelled or closed') — the DN/PR-style downstream refusal, disclosed here since "
        "cancelling is what puts this order into that terminal state")
    flags.append(
        "before_cancel (accounts_controller.py:395-396, validate_einvoice_fields) and on_trash "
        "(accounts_controller.py:486+, GL/PLE/repost cleanup) are both inherited from "
        "AccountsController — before_cancel is a base-app no-op stub (a literal 'pass', "
        "@erpnext.allow_regional), and on_trash's GL cleanup is moot given this order's own "
        "honest-empty ledger verdict; checked, confirmed inert, not a RED FLAG")
    return flags


def _subcontracting_inward_order_ledger_preview_deferral_flag(doctype):
    """Subcontracting Inward Order breadth (2026-07-22) — the POS Invoice false-positive shape,
    mirrored: POS's own preview is misleadingly NON-empty for a posting that never happens; this
    doctype's own preview is misleadingly EMPTY for a document whose real accounting consequence
    is genuinely substantial, just deferred onto sibling documents. Fires unconditionally for
    ``doctype == SUBCONTRACTING_INWARD_ORDER`` (a structural property of the voucher_type/
    voucher_no keying, true regardless of the document's own contents), submit-direction only.

    ``StockController.make_gl_entries`` (inherited, never overridden — confirmed by the full MRO
    trace in ``erpnext.py``'s own "Breadth (Subcontracting Inward Order)" module-docstring
    section: ``SubcontractingInwardOrder -> SubcontractingController -> StockController ->
    AccountsController -> TransactionBase -> StatusUpdater -> Document``, 7 nodes) genuinely
    EXISTS and IS callable — this doctype does NOT join the skip tuple, unlike Dunning/Production
    Plan/every bare-``Document`` doctype. But ``get_stock_ledger_details()``
    (``stock_controller.py:923-948``) queries ``Stock Ledger Entry`` filtered on
    ``voucher_type == self.doctype`` AND ``voucher_no == self.name`` — and this doctype's own
    ``on_submit``/``on_cancel`` (``subcontracting_inward_order.py:72-78``) call ONLY
    ``update_status()``/``update_subcontracted_quantity_in_so()``, neither of which ever writes
    an SLE under this voucher's own name. All real stock movement is deferred onto the FOUR
    spawned Stock Entry documents (Receive from Customer / Return Raw Material to Customer /
    Subcontracting Delivery / Subcontracting Return, each built by a whitelisted ``make_*``
    factory) whose OWN ``voucher_type`` is ``"Stock Entry"``, never this doctype's name. So
    ``sle_map`` is always ``{}``, ``gl_list`` is always ``[]`` — the emptiness is UNCONDITIONAL
    (a structural property of the keying, never case-by-case the way Asset's own conditional
    emptiness is), NOT the honest "a real submit posts nothing either" shape Sales/Purchase
    Order/Material Request carry.

    ``plan_cancel`` needs no equivalent flag: its ``projected_reversal`` comes from
    ``get_gl_entries(doctype, name)``, a REAL bench read of actually-posted rows, which correctly
    comes back empty (nothing was ever posted under this voucher to reverse) — the pre-existing
    generic "no live GL rows found for this voucher — nothing visible to unwind" flag already
    says so honestly, unchanged."""
    if doctype != SUBCONTRACTING_INWARD_ORDER:
        return []
    return [
        "PROJECTED GL ABOVE IS EMPTY, AND WILL ALWAYS BE EMPTY — BUT NOT BECAUSE THIS DOCUMENT "
        "HAS NO REAL ACCOUNTING CONSEQUENCE: this order's own on_submit/on_cancel never write a "
        "Stock Ledger Entry under its own voucher_type ('Subcontracting Inward Order') — "
        "make_gl_entries() runs its full machinery (it IS callable, unlike Dunning/Production "
        "Plan) and finds nothing, unconditionally, every time, because get_stock_ledger_details() "
        "keys strictly on this voucher's own voucher_type/voucher_no. Real stock movement from "
        "working this order happens on FOUR separate Stock Entry documents this order's own "
        "whitelisted make_* methods spawn (voucher_type='Stock Entry', not this doctype) — "
        "govern THOSE documents' own submit/cancel to see the real postings"]


def _subcontracting_inward_order_mutator_map_flag(direction):
    """Subcontracting Inward Order breadth (2026-07-22) — THE ELEVEN-ROW MUTATOR MAP plus THE NEW
    BYPASS CLASS, the row's center per the pre-verification addendum
    (``docs/plans/dossiers/subcontracting_inward_order.verify.md``, "The submitted-state mutator
    map"), shared verbatim by both :func:`_subcontracting_inward_order_submit_risk_flags` and
    :func:`_subcontracting_inward_order_cancel_risk_flags` so neither direction understates it.
    Unconditional, cross-document prose (per the campaign's own "cross-document state stays
    prose" rule) — true regardless of what THIS draft currently holds.

    FOUR other doctypes rewrite this order's own status/child rows through FIVE mechanisms, none
    of it reachable by reading ``subcontracting_inward_order.py`` alone: Stock Entry (via the
    ``SubcontractingInwardController`` mixin, mixed into ``StockEntry`` itself,
    ``stock_entry.py:92``, called from its own ``on_submit``/``on_cancel`` at :576/:618) across
    FIVE purposes — Receive from Customer and Manufacture consumption both recompute
    ``received_items`` quantities via ``frappe.db.bulk_update`` AND can insert+submit NEW rows
    AND can ``frappe.delete_doc`` a row that nets to zero
    (``subcontracting_inward_controller.py:700-786``/``:787-892``, deletes at :775/:852 — THE NEW
    BYPASS CLASS); Manufacture also writes ``items.produced_qty``/``.process_loss_qty`` via a
    cross-document ``db_set`` (``subcontracting_inward_order_item.py:39-52``); Delivery/Return
    write ``items.delivered_qty``/``.returned_qty`` via ``bulk_update`` (:652-671); Manufacture
    secondary items write ``secondary_items.produced_qty`` via ``bulk_update``, can insert+submit
    NEW rows, and can ``delete_doc`` one on cancel (:894-977, delete at :938 — THE NEW BYPASS
    CLASS again); EVERY purpose recomputes ``status`` via ``db_set`` (:1130-1136). Work Order
    writes ``received_items.work_order_qty`` via a RAW ``frappe.qb.update(table).set(case_expr)
    ...run()``, no ORM wrapper at all (``work_order.py:1005-1038``, called :947/:971). Sales
    Invoice writes ``received_items.billed_qty`` via the SAME raw querybuilder shape
    (``sales_invoice.py:839-858``, called :550/:684) — a SECOND independent raw-SQL writer.
    Sales Order force-closes this order's own ``status`` via its OWN status transition
    (``sales_order.py:602-626``: closing/reopening the parent Sales Order calls the BARE,
    unpermissioned module function ``set_subcontracting_inward_order_status`` directly) — a
    cross-document cascade with ZERO write-permission check on this order itself.

    Of all these paths, only the WHITELISTED module function ``update_subcontracting_inward_
    order_status`` (``subcontracting_inward_order.py:560-567``, gated on
    ``scio.check_permission("write")``) carries any write-permission check at all."""
    return (
        f"this {direction}'s own status field and received_items/secondary_items child rows "
        "(received_qty/consumed_qty/produced_qty/process_loss_qty/delivered_qty/returned_qty/"
        "work_order_qty/billed_qty) are NOT stable once submitted, independent of this "
        "document's OWN submit/cancel: Stock Entry's on_submit/on_cancel "
        "(stock_entry.py:576/:618, across five purposes), Work Order's on_submit/on_cancel "
        "(work_order.py:947/:971, a RAW frappe.qb UPDATE with no ORM wrapper), Sales Invoice's "
        "on_submit/on_cancel (sales_invoice.py:550/:684, the SAME raw querybuilder shape), and "
        "Sales Order's own status transition (sales_order.py:602-626, force-closes this order's "
        "status with ZERO write-permission check) ALL reach back into this order via internal, "
        "non-whitelisted paths. TWO of Stock Entry's own mechanisms go further than a bypass: "
        "they DELETE docstatus=1 child rows outright (frappe.delete_doc on received_items/"
        "secondary_items rows the SAME controller explicitly .submit()s elsewhere, "
        "subcontracting_inward_controller.py:775/:852/:938 vs. :734/:892/:977) — legal only "
        "because neither child doctype sets is_submittable, so frappe's own submitted-record "
        "delete guard (frappe/model/delete_doc.py:280-289) never fires for them. A docstatus=1 "
        "row of received_items/secondary_items on this order is NOT durable. Of every path into "
        "this order's own status/child rows, only the whitelisted update_subcontracting_"
        "inward_order_status (subcontracting_inward_order.py:560-567) carries a "
        "write-permission check — every other path is a routine consequence of submitting or "
        "cancelling a DIFFERENT document")


def _subcontracting_inward_order_submit_risk_flags(doc):
    """Subcontracting Inward Order breadth (2026-07-22) — the submit-direction disclosure, off
    the pre-verification addendum
    (``docs/plans/dossiers/subcontracting_inward_order.verify.md``), re-verified from source.
    Doctype-scoped at the call site (fired only for ``doctype == SUBCONTRACTING_INWARD_ORDER``).

    Carries THE ELEVEN-ROW MUTATOR MAP (shared with the cancel direction, see
    :func:`_subcontracting_inward_order_mutator_map_flag`), plus the STATUS three-way-writable
    disclosure (read_only:1 in the JSON does not mean the field is stable — this order's own
    ``update_status()``, the whitelisted API, and Sales Order's own force-close cascade all write
    it with no schema-level block), plus the validate()-chain-skip finding (this doctype never
    reaches ``StockController.validate()``/``AccountsController.validate()`` — a shared family
    trait ``SubcontractingController.validate()``'s own doctype-branch produces, undisclosed by
    the dossier)."""
    flags = []
    flags.append(_subcontracting_inward_order_mutator_map_flag("submitted document"))
    flags.append(
        "status is read_only:1 in the JSON but THREE-way backend-writable, none of the three "
        "blocked by that UI-only flag: this order's own update_status() (subcontracting_inward_"
        "order.py:80-131, a plain db_set, called from its own on_submit/on_cancel AND from "
        "every Stock Entry purpose touching it), the whitelisted update_subcontracting_inward_"
        "order_status API, and Sales Order's own force-close cascade "
        "(sales_order.py:602-626) when the linked sales_order's status closes")
    flags.append(
        "this doctype's own validate() (subcontracting_inward_order.py:64-70) chains "
        "super().validate() into SubcontractingController.validate() "
        "(subcontracting_controller.py:68-78), whose doctype-branch for Subcontracting Order/"
        "Receipt/Inward Order does NOT call super().validate() in turn — so "
        "StockController.validate()/AccountsController.validate() never run for this document's "
        "own submit (validate_duplicate_serial_and_batch_bundle/validate_inspection/"
        "validate_putaway_capacity/validate_inventory_dimension_mandatory and everything in "
        "AccountsController.validate() are skipped, a shared family trait, not disclosed by the "
        "dossier)")
    if doc.get("customer") and doc.get("customer_warehouse"):
        flags.append(
            "submit will be REFUSED if customer_warehouse does not belong to customer "
            "(validate_customer_warehouse, subcontracting_inward_order.py:143-149 — a LIVE read "
            "of Warehouse.customer, not readable off this draft alone)")
    flags.append(
        "on_submit also runs update_subcontracted_quantity_in_so (subcontracting_inward_"
        "order.py:133-141), incrementing each linked Sales Order Item's own subcontracted_qty "
        "via a clean, full .save() — the highest honesty grade in this campaign's own taxonomy, "
        "never a bypass, but still a write into a document this broker does not separately "
        "govern")
    return flags


def _subcontracting_inward_order_cancel_risk_flags(doc):
    """Subcontracting Inward Order breadth (2026-07-22) — the cancel-direction disclosure.
    Carries THE ELEVEN-ROW MUTATOR MAP (cancel direction), the cascade-not-a-leaf disclosure
    (two DIRECT header-level Links, no child-table resolution needed — simpler than
    Subcontracting Order's own landing), the hard cross-document gate that runs from the OTHER
    side (Stock Entry's own validate_closed_subcontracting_order), and the corrected on_cancel-
    without-super() finding."""
    flags = []
    flags.append(
        "this Subcontracting Inward Order is NOT a cascade leaf — two real, DIRECT header-level "
        "incoming Links (a full two-checkout grep for '\"options\": \"Subcontracting Inward "
        "Order\"' finds exactly 3 hits: Work Order.subcontracting_inward_order, "
        "work_order.json:660; Stock Entry.subcontracting_inward_order, stock_entry.json:734; "
        "and this order's own amended_from self-reference — no child-table resolution needed, "
        "unlike Subcontracting Order's own landing): a submitted Work Order or Stock Entry "
        "still referencing this order blocks a leaf cancel outright")
    flags.append(_subcontracting_inward_order_mutator_map_flag("submitted document"))
    flags.append(
        "on_cancel (subcontracting_inward_order.py:76-78) is an OVERRIDE that never calls "
        "super().on_cancel() — AccountsController.on_cancel is skipped entirely, not chained "
        "(mechanically a no-op for this doctype regardless, since it never posts a GL of its "
        "own — but the mechanism itself, not merely the dossier's old citation, is stated "
        "precisely here)")
    flags.append(
        "the HARD gate against cancelling a Stock Entry once THIS order is Closed runs from the "
        "OTHER side: StockEntry.validate_closed_subcontracting_order (stock_entry.py:1977-1983, "
        "called from Stock Entry's own validate() at :316 AND on_cancel() at :580) refuses "
        "save/submit/cancel of a linked Stock Entry via check_on_hold_or_closed_status("
        "'Subcontracting Inward Order', order) (erpnext/buying/utils.py:112-123) — that shared "
        "helper's 'On Hold' branch is DEAD CODE for this doctype specifically (status Select "
        "has no 'On Hold' option, 8 options confirmed); only the 'Closed' branch is ever "
        "reachable")
    flags.append(
        "ignore_linked_doctypes is NOT set anywhere in this doctype's class body — grepped the "
        "full 567-line file; the framework's default 'can't cancel while a submitted incoming "
        "link exists' check runs unmodified")
    return flags


def _subcontracting_receipt_ledger_preview_gap_flag(doctype):
    """Subcontracting Receipt breadth (2026-07-22) — THE ROOF ROW's own sixth ledger-preview
    shape, this landing's own finding, beyond both the dossier and the pre-verification addendum.
    Fires unconditionally for ``doctype == SUBCONTRACTING_RECEIPT`` (a structural property of the
    native preview's own SLE-seeding whitelist, true regardless of the draft's own contents),
    submit-direction only.

    ``StockController.make_gl_entries`` (``stock_controller.py:292``, called
    ``subcontracting_receipt.py:184``) genuinely EXISTS and calls SCR's own ``get_gl_entries``
    override (``:708-718``) — this doctype does NOT join the skip tuple, and a real submit posts
    BOTH a real Stock Ledger Entry (``update_stock_ledger``, an unconditional override,
    ``subcontracting_controller.py:1199-1239``) and conditional GL — the Delivery Note/Purchase
    Receipt/Stock Entry both-ledgers shape.

    But ERPNext's own ``get_accounting_ledger_preview`` (``stock_controller.py:2090-2119``) only
    pre-seeds an in-memory ``update_stock_ledger()`` call for doctypes named in its own literal
    whitelist tuple — ``("Purchase Receipt", "Delivery Note", "Stock Entry")``, line 2109 — or
    carrying a truthy ``update_stock`` field. Subcontracting Receipt is confirmed ABSENT from that
    tuple and carries no ``update_stock`` field (confirmed absent from the 80-field enumeration),
    so the preview calls ``doc.make_gl_entries()`` WITHOUT ever seeding a real Stock Ledger Entry
    row for this still-draft voucher first. Unlike Stock Reconciliation's own already-landed
    "quiet dishonest empty" shape (:func:`_stock_reconciliation_ledger_preview_incomplete_flag`),
    SCR's own ``make_item_gl_entries`` does not return quietly: ``stock_value_diff = frappe.db.
    get_value("Stock Ledger Entry", {...}, "stock_value_difference")``
    (``subcontracting_receipt.py:741-751``) returns ``None`` when no matching row exists (the
    draft's own case), and at ``:847``, ``if divisional_loss := flt(item.amount -
    stock_value_diff, item.precision("amount")):`` evaluates the bare Python subtraction BEFORE
    ``flt()`` ever gets a chance to coerce the ``None`` (unlike ``:778``'s ``flt(stock_value_diff)
    - service_cost``, which IS safe — ``flt(None)`` returns ``0.0`` by frappe's own documented
    contract). Subtracting ``None`` from a Python ``float`` raises ``TypeError`` — confirmed by
    direct interpreter reproduction of the exact expression shape.

    CONDITIONAL, not unconditional: this only fires when ``need_inventory_map`` is true (real
    stock items AND perpetual inventory enabled for the company, ``stock_controller.py:300-302``)
    AND a real inventory account resolves for at least one item's warehouse (``:740``) — the EXACT
    configuration under which a real submit of this SAME draft would post genuine GL rows. Under a
    company WITHOUT perpetual inventory, both the real submit and the preview correctly, honestly
    return empty (SCR's own ``get_gl_entries`` early-returns ``[]`` at line 712 for that case) —
    only the perpetual-inventory branch diverges from "empty" into "raises."

    THE SUPERVISOR'S RULING (2026-07-22, closing this landing's owed decision): the broker does
    NOT call a preview that upstream has structurally broken for this doctype. Subcontracting
    Receipt joins the ledger-preview SKIP tuple as its THIRTIETH member — for a cause distinct
    from every prior member (theirs: AttributeError, no ``make_gl_entries`` in the MRO; SCR's:
    genuinely CALLABLE machinery that ERPNext's own preview whitelist omission turns into a live
    TypeError on exactly the configurations where it matters). The treatment is identical (never
    call, disclose honestly); the CAUSE is this sixth shape's own. The projected GL in this plan
    is therefore empty BY THE BROKER'S OWN CHOICE, and this flag says so.

    ``plan_cancel`` needs no equivalent flag: its own ``projected_reversal`` comes from
    ``get_gl_entries(doctype, name)``, a REAL bench read of ACTUALLY-POSTED rows, correctly empty
    or correctly populated depending on whether this voucher ever really submitted — never routed
    through the crash-prone in-memory preview path at all."""
    if doctype != SUBCONTRACTING_RECEIPT:
        return []
    return [
        "PROJECTED GL IS EMPTY BECAUSE THIS BROKER DELIBERATELY DID NOT CALL THE NATIVE "
        "PREVIEW: Subcontracting Receipt is ABSENT from ERPNext's own native-preview "
        "SLE-seeding whitelist (unlike Delivery Note/Purchase Receipt/Stock Entry), which makes "
        "the native preview itself crash-prone for this doctype rather than merely empty — "
        "not because this submit has no real "
        "accounting consequence. A real submit of this document ALWAYS writes a Stock Ledger "
        "Entry (unconditional) and, whenever perpetual inventory is enabled for this company "
        "with a resolvable inventory account, ALSO writes real GL Entry rows — but the preview "
        "never seeds that SLE row first, and this doctype's own GL-building arithmetic "
        "(subcontracting_receipt.py:847) subtracts the resulting missing value BEFORE it can be "
        "safely coerced, a live TypeError risk under exactly that same perpetual-inventory "
        "configuration. Under perpetual inventory DISABLED, both a real submit and this preview "
        "honestly agree on empty — this caveat only bites under perpetual inventory enabled, "
        "the same configuration a meaningful preview most needs to be trustworthy"]


def _subcontracting_receipt_sco_writeback_flag(direction):
    """Subcontracting Receipt breadth (2026-07-22) — THE SCO WRITEBACK, mirroring Subcontracting
    Order's own landed SEVEN-PATH MUTATOR MAP paths 2-3 from THIS side, widened to four channels.
    Shared verbatim by both :func:`_subcontracting_receipt_submit_risk_flags` and
    :func:`_subcontracting_receipt_cancel_risk_flags` so neither direction understates it.
    Unconditional, cross-document prose (per the campaign's own "cross-document state stays
    prose" rule) — true regardless of what THIS draft currently holds.

    Path 2 (SCO's own numbering): ``self.set_subcontracting_order_status(update_bin=False)``
    (``subcontracting_receipt.py:176``/``206``) -> ``SubcontractingController.
    set_subcontracting_order_status`` (``subcontracting_controller.py:1272-1281``) ->
    ``sco_doc.update_status(...)`` -> ``self.db_set("status", status, ...)``
    (``subcontracting_order.py:292-321``, the db_set itself at ``:319``) — a ``db_set`` bypass.

    Path 3: ``self.set_consumed_qty_in_subcontract_order()`` (``:177``/``205``) ->
    ``subcontracting_controller.py:1139-1160`` -> ``__update_consumed_qty_in_subcontract_order``
    (``:1121-1137``) -> a RAW module-level ``frappe.db.set_value`` straight into ``Subcontracting
    Order Supplied Item`` rows at ``:1135-1137`` — no ``Document`` instantiation on either side,
    the campaign's rawest grade.

    TWO MORE channels Subcontracting Order's own landing (``8c0ba75``) did not enumerate: (a)
    ``update_prevdoc_status()`` (called ``:174``/``203``, a ``StatusUpdater`` method via SCR's own
    ``status_updater`` config set in ``__init__`` at ``:100-113``) -> ``update_qty()`` ->
    ``_update_children()`` writes ``Subcontracting Order Item.received_qty`` via a RAW
    ``frappe.db.sql`` UPDATE (``status_updater.py:597-602`` — the Quality-Inspection-shaped
    raw-SQL grade) -> ``_update_percent_field_in_targets()`` -> ``_update_percent_field()``
    writes ``Subcontracting Order.per_received`` via ``target.db_set(update_data, ...)`` on a
    freshly loaded lazy doc (``status_updater.py:676-682`` — a SECOND, independent ``db_set``
    bypass); (b) ``update_stock_ledger()`` (``:183``/``207``) opens with ``self.
    update_ordered_and_reserved_qty()`` (``subcontracting_controller.py:1200``) ->
    ``update_ordered_and_reserved_qty`` (``:1162-1180``) throws if the linked SCO's own status
    reads Closed/Cancelled (the DN/PR-style downstream refusal) and otherwise calls ``sco_doc.
    update_ordered_qty_for_subcontracting``/``update_reserved_qty_for_subcontracting`` — BIN-level
    writes into a THIRD structure this broker does not separately govern at all."""
    return (
        f"this {direction}'s own submit/cancel writes back into the linked Subcontracting "
        "Order's own status (TWICE, two independent call chains, both ending in a db_set — "
        "set_subcontracting_order_status->sco_doc.update_status, subcontracting_receipt.py:176/"
        "206 -> subcontracting_order.py:319, AND update_prevdoc_status's own StatusUpdater "
        "percent-field write, status_updater.py:676-682), Subcontracting Order Item's own "
        "received_qty (a RAW "
        "frappe.db.sql UPDATE, status_updater.py:597-602), Subcontracting Order Supplied Item's "
        "own consumed_qty (a RAW module-level frappe.db.set_value with no Document instantiation "
        "at all, subcontracting_controller.py:1135-1137), and the linked Subcontracting Order's "
        "own Bin-level ordered/reserved quantities (update_ordered_and_reserved_qty, "
        "subcontracting_controller.py:1162-1180, also the source of a downstream REFUSE if that "
        "order already reads Closed or Cancelled) — FOUR channels total, NONE permission-checked "
        "from this document's own side, two more than Subcontracting Order's own landing (8c0ba75) "
        "enumerated from this same direction")


def _subcontracting_receipt_submit_risk_flags(doc):
    """Subcontracting Receipt breadth (2026-07-22) — THE ROOF ROW's own submit-direction
    disclosure, off the pre-verification addendum
    (``docs/plans/dossiers/subcontracting_receipt.verify.md``), re-verified from source.
    Doctype-scoped at the call site (fired only for ``doctype == SUBCONTRACTING_RECEIPT``).

    Deterministic-from-draft throws (become hard REFUSED flags): ``validate_items_qty`` (any item
    row with both ``qty`` and ``rejected_qty`` falsy, ``:274-281``); ``validate_secondary_items``
    (a secondary/legacy-scrap item row with zero ``qty``, a set ``rejected_qty``, or a missing
    ``reference_name``, ``:569-589``, gated on ``_action == "submit"``, ``:153``);
    ``validate_accepted_warehouse`` (an item with qty but no resolvable warehouse, or accepted ==
    rejected warehouse, ``subcontracting_controller.py:591-606`` — read off ``self.items``/
    ``self.set_warehouse`` alone). Cross-document prose: ``validate_bom_required_qty``
    (``:608-646``, gated on live Buying Settings singles, compares against a LIVE BOM read via
    ``_get_materials_from_bom`` — never deterministic from this draft alone)."""
    flags = []
    if not any((row.get("qty") or row.get("rejected_qty")) for row in (doc.get("items") or [])):
        flags.append(
            "submit will be REFUSED: at least one items row carries both a zero qty and a zero "
            "rejected_qty (validate_items_qty, subcontracting_receipt.py:274-281 — 'Accepted Qty "
            "and Rejected Qty can't be zero at the same time')")
    flags.append(
        "validate_bom_required_qty (subcontracting_receipt.py:608-646, called from validate()) "
        "depends on a LIVE read of Buying Settings singles PLUS the linked BOM's own exploded "
        "materials (_get_materials_from_bom) — it can REFUSE this submit with a BOMQuantityError "
        "if consumed raw-material qty falls short of what the BOM requires; neither input is "
        "readable off this draft alone")
    flags.append(_subcontracting_receipt_sco_writeback_flag("submitted document"))
    flags.append(
        "update_status() (subcontracting_receipt.py:683-706, called from on_submit) writes THIS "
        "document's OWN status via a raw module-level frappe.db.set_value naming the doctype+"
        "name explicitly (:704-706) — rawer than a self.db_set() bypass, though a same-document "
        "write, not a cross-document one; status is read_only:1 AND reqd:1 in the schema but "
        "backend-writable regardless (the JSON flag only constrains the desk UI)")
    if not doc.get("is_return"):
        flags.append(
            "has_reserved_stock() (subcontracting_receipt.py:965-974) is a LIVE query, never "
            "readable off this draft (it walks supplied_items and asks get_sre_details_for_"
            "voucher against each linked Subcontracting Order) — IF it comes back true, "
            "update_stock_reservation_entries (inherited, stock_controller.py:1899-2019, called "
            "on_submit) loads each live submitted Stock Reservation Entry reserved against that "
            "order and mutates its consumed_qty (plus per-serial/batch delivered_qty) via a "
            "db_update bypass, then cascades into update_reserved_qty_in_voucher/update_status/"
            "update_reserved_stock_in_bin — further writes on a THIRD document this broker does "
            "not separately govern; a no-op for a Return receipt, deterministically excluded by "
            "is_return alone (:1956)")
    flags.append(
        "repost_future_sle_and_gle (subcontracting_receipt.py:185, inherited from "
        "StockController) can create a 'Repost Item Valuation' document processed by ERPNext's "
        "OWN scheduled reposting job (every 30 minutes) — an armed channel entirely outside this "
        "broker's consent, the Asset Capitalization precedent; joining this disclosure does NOT "
        "clear the still-open gap on Stock Entry/Stock Reconciliation/Delivery Note/Purchase "
        "Receipt, which carry the identical channel with no dedicated risk-flag of their own, "
        "banked as campaign debt")
    flags.append(
        "if Buying Settings.auto_create_purchase_receipt is enabled (a live bench setting, not "
        "readable off this draft), auto_create_purchase_receipt (subcontracting_receipt.py:"
        "961-963) creates a NEW Purchase Receipt sibling, save-only (submit is an unstated "
        "default-False, never an explicit kwarg) — a soft sibling this broker does not track, "
        "but see the cancel-direction disclosure for why it can later block THIS document's own "
        "cancel")
    for row in doc.get("items") or []:
        if row.get("job_card"):
            flags.append(
                "at least one items row names a job_card: update_job_card "
                "(subcontracting_receipt.py:224-228, called on_submit) reruns a live SUM over "
                "all SUBMITTED Subcontracting Receipt Items sharing that Job Card and writes the "
                "result via Job Card.set_manufactured_qty()'s own db_set "
                "(job_card.py:204-227) — a same-transaction write into a THIRD governed doctype")
            break
    flags.extend(_return_risk_flags(doc, SUBCONTRACTING_RECEIPT))
    return flags


def _subcontracting_receipt_cancel_risk_flags(doc):
    """Subcontracting Receipt breadth (2026-07-22) — THE ROOF ROW's own cancel-direction
    disclosure. THE CANCEL BACK-LINK GATE is this landing's own headline, confirmed exactly per
    the pre-verification addendum's own correction 2: ``frappe.model.document.Document.
    run_post_save_methods()`` runs ``self.run_method("on_cancel")`` THEN ``self.
    check_no_back_links_exist()`` (``frappe/model/document.py:1450-1452``) -> ``check_if_doc_is_
    linked(self, method="Cancel")`` (``:1572-1577``) -> raises ``frappe.LinkExistsError``
    (``delete_doc.py:474-487``) if ANY submitted document links to this SCR via a static Link
    field whose parent doctype is not in ``self.ignore_linked_doctypes``. SCR's own tuple
    (``subcontracting_receipt.py:196-201``) is ``("GL Entry", "Stock Ledger Entry", "Repost Item
    Valuation", "Serial and Batch Bundle")`` — Purchase Receipt is ABSENT, and ``Purchase Receipt.
    subcontracting_receipt`` IS a real Link (confirmed in ``purchase_receipt.json``). One
    exemption in the same code path: ``link_field == "amended_from" and method == "Cancel"`` is
    hardcoded-skipped (``delete_doc.py:319``), so ``amended_from`` never blocks cancel — but
    ``return_against`` gets NO such exemption.

    THE ORDERING WRINKLE: ``Purchase Receipt.subcontracting_receipt`` is one of this row's own 3
    cascade edges, discovered natively by ``get_submitted_linked_docs`` (the SAME generic walk
    this broker's own blast-radius disclosure already calls) — so in ORDINARY sequential use, THIS
    BROKER'S OWN GATE already refuses cleanly, BEFORE any cancel is attempted and before
    ``on_cancel``'s own side effects would run. ERPNext's framework-level check runs the OPPOSITE
    way — AFTER ``on_cancel`` — so it becomes the operative refusal only in a TOCTOU race (the
    Purchase Receipt submits in the gap between this plan and the actual cancel call); frappe's
    own transaction semantics keep that race safe (a post-``on_cancel`` exception rolls the whole
    request back), but the ordering divergence is real and worth stating precisely."""
    flags = []
    flags.append(
        "this Subcontracting Receipt is NOT a cascade leaf — a full two-checkout grep for "
        "'\"options\": \"Subcontracting Receipt\"' finds exactly 3 hits: Purchase Receipt."
        "subcontracting_receipt (a real, direct, external submittable-referencer Link), "
        "amended_from (self, hardcoded-exempt from the framework's own cancel back-link check), "
        "and return_against (self, NOT exempt) — a submitted Purchase Receipt still referencing "
        "this SCR blocks a leaf cancel outright via the standing blast-radius gate")
    flags.append(
        "cancel can be REFUSED by a framework-level back-link gate this document's own "
        "on_cancel() never names: this SCR's own ignore_linked_doctypes tuple "
        "(subcontracting_receipt.py:196-201, 'GL Entry'/'Stock Ledger Entry'/'Repost Item "
        "Valuation'/'Serial and Batch Bundle') does NOT include 'Purchase Receipt', and Purchase "
        "Receipt.subcontracting_receipt is a real Link field — so once a user has separately "
        "SUBMITTED the auto-created Purchase Receipt this SCR's own auto_create_purchase_receipt "
        "may have created (save-only, never auto-submitted), cancelling THIS document is refused "
        "by frappe.check_no_back_links_exist() (document.py:1450-1452 -> delete_doc.py:474-487) "
        "— independent of and in ADDITION to the SCO-closed throw below; this is a LIVE read of "
        "that Purchase Receipt's own docstatus, not knowable from this draft alone. IN ORDINARY "
        "SEQUENTIAL USE this broker's OWN blast-radius gate already catches this same Purchase "
        "Receipt edge and refuses BEFORE any cancel attempt at all — the framework-level check "
        "above becomes the operative refusal only in a TOCTOU race (the Purchase Receipt submits "
        "between this plan and the actual cancel call); frappe's own transaction rollback keeps "
        "that race safe")
    if doc.get("return_against"):
        flags.append(
            "this is a Return SCR (return_against set): return_against carries NO amended_from- "
            "style exemption from the SAME back-link gate above — if a Return SCR against this "
            "original is itself submitted, cancelling the ORIGINAL is refused too "
            "(delete_doc.py:319 hardcodes the exemption for amended_from only, never "
            "return_against)")
    flags.append(
        "validate_closed_subcontracting_order (subcontracting_receipt.py:221-222, called from "
        "on_cancel) throws if the linked Subcontracting Order's own status reads Closed or On "
        "Hold (check_for_on_hold_or_closed_status, stock_controller.py:2021-2056) — a LIVE read "
        "of that order's own status, not knowable from this draft alone")
    flags.append(_subcontracting_receipt_sco_writeback_flag("submitted document"))
    flags.append(
        "update_status() (subcontracting_receipt.py:683-706, called from on_cancel) writes THIS "
        "document's OWN status to Cancelled via the SAME raw module-level frappe.db.set_value "
        "the submit direction discloses (:704-706)")
    if not doc.get("is_return"):
        flags.append(
            "has_reserved_stock() is a LIVE query, never readable off this draft — IF this "
            "receipt drew against a reserve_stock=1 Subcontracting Order, "
            "update_stock_reservation_entries (inherited, stock_controller.py:1899-2019, called "
            "on_cancel too) reverses the SAME Stock Reservation Entry consumed_qty via the SAME "
            "db_update-bypass channel the submit direction discloses — a no-op for a Return "
            "receipt, deterministically excluded by is_return alone")
    flags.append(
        "delete_auto_created_batches (inherited, stock_controller.py:993+, called on_cancel line "
        "212) soft-cancels any Serial and Batch Bundle rows this receipt auto-created via a "
        "db_set — the identical mechanism Delivery Note's own landing already documented")
    for row in doc.get("items") or []:
        if row.get("job_card"):
            flags.append(
                "at least one items row names a job_card: update_job_card "
                "(subcontracting_receipt.py:224-228, called on_cancel too) reruns the SAME live "
                "SUM over SUBMITTED Subcontracting Receipt Items — cancel's own 'reversal' is "
                "never an explicit subtract, only the natural consequence of this row falling "
                "out of the live docstatus==1 sum on the next recompute")
            break
    flags.append(
        "repost_future_sle_and_gle (subcontracting_receipt.py:210, inherited from "
        "StockController) can ALSO arm a 'Repost Item Valuation' document on cancel — the same "
        "ERPNext-scheduled, broker-invisible channel the submit direction discloses; joining "
        "this disclosure does NOT clear the still-open SE/SR/DN/PR gap")
    flags.extend(_return_risk_flags(doc, SUBCONTRACTING_RECEIPT))
    return flags


class _QueuedConsequenceEffects:
    """Wraps a :class:`pacioli.store.SubmitEffects` for :data:`pacioli.erpnext.
    ENQUEUE_ON_SUBMIT_DOCTYPES`' submit path — John's ruling 2, the two-phase PROVE ("truth
    always", ``docs/plans/2026-07-21-cancel-truth-rulings.md``; design study ``docs/plans/
    dossiers/bom_creator.design.md``, option (b)).

    Delegates every ``effects`` method to the wrapped instance UNCHANGED except
    ``record_outcome``: when ``spine.governed_submit`` calls it with the confirmed-transition
    status literal ``"committed"`` (docstatus really did transition — this wrapper never touches
    that truth; ``spine.py``'s own gate already confirmed it, byte-identically to every other
    doctype), the LEDGER's own outcome status is rewritten to ``"committed_pending_async"`` and a
    ``queued_consequence`` marker is folded into ``result`` — IN PLACE, the same dict object
    ``spine.governed_submit`` still holds as its own local ``result`` and returns as
    ``SubmitResult.result`` — because the doctype's declared deliverable (BOM Creator's BOM tree)
    has NOT been built yet: ERPNext's own ``on_submit`` only ENQUEUES ``create_boms()`` on the
    channel/queue this class is constructed with; the response this broker just confirmed answers
    before any worker has dequeued it. The in-place mutation is deliberate, not an oversight: it
    is the ONLY way the caller-facing result (what ``_governed_write`` puts in ``out["result"]``)
    and the ledger's own recorded outcome ever see the SAME marker, with no second place for the
    two to drift apart. Every other status (``"unconfirmed"``/``"failed"``) passes through
    untouched — those already truthfully describe a transition that did NOT confirm; there is no
    "committed" language to narrow.

    ``"committed_pending_async"`` deliberately does NOT finalize the intent under
    ``prove.orphans()`` (only a literal ``"committed"`` status does — ``prove.py``'s own,
    unmodified, rule) — so the EXISTING orphan sweep already surfaces this submit as open until
    ``_sweep_queued_consequences`` (below, wired into ``prove_verify``) or a human resolves it
    with a SECOND, later outcome pointing at the same intent. No change to ``spine.py`` or
    ``prove.py`` was needed for this: doctype-specific truth-narrowing stays in the glue, exactly
    where every other doctype-specific gate in this campaign already lives."""

    def __init__(self, inner, *, channel, queue_name):
        self._inner = inner
        self._channel = channel
        self._queue = queue_name

    def claim_marker(self, reserved):
        return self._inner.claim_marker(reserved)

    def record_intent(self, body):
        return self._inner.record_intent(body)

    def execute(self):
        return self._inner.execute()

    def readback(self):
        return self._inner.readback()

    def record_outcome(self, intent, status, result, final_marker):
        if status == "committed":
            # Mutate the SAME dict object (never a copy) — see the docstring above for why: this
            # is `execute()`'s own return value, still held by spine.governed_submit's local
            # `result` and about to become `SubmitResult.result` verbatim.
            target = result if isinstance(result, dict) else {}
            target["queued_consequence"] = {
                "channel": self._channel,
                "queue": self._queue,
                "status_at_submit": target.get("status"),
            }
            result = target
            status = "committed_pending_async"
        return self._inner.record_outcome(intent, status, result, final_marker)


_QUEUED_CONSEQUENCE_RESOLVERS = {}  # doctype -> callable(client, docname) -> (status, attestation)
# populated just below _resolve_bom_creator_queued_consequence's own definition — kept as a
# module-level dict (not inlined into _sweep_queued_consequences) so a future ENQUEUE_ON_SUBMIT
# doctype adds one resolver function + one dict entry, never a change to the sweep itself.


def _resolve_bom_creator_queued_consequence(client, docname):
    """Phase 2 of John's ruling 2: read the BOM Creator's own live ``status`` field (the same
    cheap read ``get_bom_creator`` already does) and decide the honest terminal claim.

    ``"Completed"`` -> attest with the REAL built BOM names, cross-checked by an actual
    ``list_documents`` count (``filters={"bom_creator": docname, "docstatus": 1}``) — never the
    status string alone (the design study's own partial-tree lesson: a "Completed" status is
    exactly what ``create_boms()`` writes whether the tree is whole or not, so the honest
    attestation is the real BOM rows, not the flag). ``"Failed"`` -> attest the failure with
    ``error_log`` surfaced (``bom_creator.py:317-322``) — the tree may still be PARTIAL (every
    node before the failure already built a real, submitted BOM), so the same real count rides
    along here too, never just the string. Anything else (``"Draft"``/``"Submitted"``/``"In
    Progress"``/``"Cancelled"``/unrecognized) stays genuinely pending — said plainly, no outcome
    recorded, the sweep tries again next time it runs."""
    doc = client.get_document(BOM_CREATOR, docname)
    status = str(doc.get("status") or "")
    if status in ("Completed", "Failed"):
        rows = client.list_documents(
            BOM, filters={"bom_creator": docname, "docstatus": 1}, limit=200,
            party_field=SUPPORTED_DOCTYPES[BOM]["party_field"],
            date_field=_date_field_for(BOM))
        names = [r.get("name") for r in rows if isinstance(r, dict) and r.get("name")]
        attestation = {"queued_consequence_resolution": {
            "status": status, "built_boms": names, "count": len(names),
            "error_log": str(doc.get("error_log") or "") if status == "Failed" else None,
        }}
        return ("committed" if status == "Completed" else "failed"), attestation
    return "pending", {"status": status}


_QUEUED_CONSEQUENCE_RESOLVERS[BOM_CREATOR] = _resolve_bom_creator_queued_consequence


def _sweep_queued_consequences(client, store):
    """The reconciliation half of John's ruling 2 — walks the receipt chain for submits still
    marked ``"committed_pending_async"`` (see :class:`_QueuedConsequenceEffects`) and resolves
    each against the doctype's own resolver in :data:`_QUEUED_CONSEQUENCE_RESOLVERS`. Wired into
    ``prove_verify`` (never a new tool — see that tool's own docstring for why).

    A pending queued consequence is genuinely open only until it is either resolved here or by a
    human directly; this function is safe to call repeatedly (idempotent) — an intent already
    carrying a resolution outcome (its own body's ``result`` names
    ``"queued_consequence_resolution"``) is never re-resolved. A read or write failure for one
    intent is reported inline (never lets one bad row crash the whole sweep, or the prove_verify
    call it rides inside)."""
    receipts = store.receipts()
    outcomes_by_intent = {}
    for r in receipts:
        if r.kind == OUTCOME and isinstance(r.body, dict):
            outcomes_by_intent.setdefault(r.body.get("finalizes"), []).append(r)

    pending, resolved = [], []
    for intent in receipts:
        if intent.kind != INTENT or not isinstance(intent.body, dict):
            continue
        doctype = intent.body.get("doctype")
        resolver = _QUEUED_CONSEQUENCE_RESOLVERS.get(doctype)
        if resolver is None or intent.body.get("tool") != "submit":
            continue
        outs = outcomes_by_intent.get(intent.seq, [])
        already_resolved = any(
            isinstance(o.body, dict) and isinstance(o.body.get("result"), dict)
            and "queued_consequence_resolution" in o.body["result"]
            for o in outs)
        if already_resolved:
            continue
        armed = any(
            isinstance(o.body, dict) and o.body.get("status") == "committed_pending_async"
            for o in outs)
        if not armed:
            continue  # never armed (submit never confirmed) — not this sweep's business
        docname = intent.body.get("docname")
        try:
            status, attestation = resolver(client, docname)
        except Exception as exc:  # noqa: BLE001 — a bad read must never crash prove_verify
            pending.append({"seq": intent.seq, "docname": docname, "doctype": doctype,
                            "error": str(exc)})
            continue
        if status == "pending":
            pending.append({"seq": intent.seq, "docname": docname, "doctype": doctype,
                            **attestation})
            continue
        try:
            store.record_outcome(intent, status, attestation, final_marker=None)
        except Exception as exc:  # noqa: BLE001 — never crash the sweep; report and move on
            pending.append({"seq": intent.seq, "docname": docname, "doctype": doctype,
                            "error": f"resolved as {status!r} but could not record it ({exc})"})
            continue
        resolved.append({"seq": intent.seq, "docname": docname, "doctype": doctype,
                         "status": status, **attestation})
    return {"pending": pending, "resolved": resolved}


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


def _journal_entry_invoice_discounting_reach_flags(doc, op):
    """The Invoice Discounting reach — a RETROFIT to the founding Journal Entry row (2026-07-21,
    found by the pre-verification fan, ``docs/plans/dossiers/invoice_discounting.verify.md``):
    ``Journal Entry Account.reference_type`` is a **Select** whose options include
    ``"Invoice Discounting"`` (``journal_entry_account.json:184-189``) — a cross-document edge
    INVISIBLE to every Link-field scan this campaign has run, which is why the founding landing
    never carried it. Fired on BOTH directions (the ``op`` shape), doctype-scoped at the call
    sites, conditional on the draft's own ``accounts`` rows — data-driven, no bench read.

    THE MECHANISM, read line by line (``journal_entry.py:459-501`` ``update_invoice_discounting``,
    called from JE's own submit AND cancel paths): for every distinct ``reference_name`` whose
    row's ``reference_type == "Invoice Discounting"``, ERPNext loads the Invoice Discounting and,
    when a row matches its ``short_term_loan`` account, (1) **THROWS** unless the ID's current
    status matches the stage the direction expects (``"Row #N: Status must be {expected} for
    Invoice Discounting {name}"`` — a cross-document gate this plan cannot fully predict, the
    ID's live status being another document's state), then (2) walks the ID through its status
    machine — submit: credit row Sanctioned→Disbursed, debit row Disbursed→Settled; cancel: the
    same edges reversed (Disbursed→Sanctioned, Settled→Disbursed) — via
    ``InvoiceDiscounting.set_status`` (``invoice_discounting.py:101-108``): a raw ``db_set`` on
    the ID's own status (no validate, no hooks, no versioning on that write) that then loops
    EVERY child invoice calling ``SalesInvoice.set_status(update=True)`` — each linked Sales
    Invoice's status recomputed and ``db_set`` in the same motion. Supervisor re-verified all
    three reads (Select options / update_invoice_discounting / set_status cascade) by hand."""
    id_names = sorted({r["reference_name"] for r in (doc.get("accounts") or [])
                       if isinstance(r, dict) and r.get("reference_type") == "Invoice Discounting"
                       and r.get("reference_name")})
    if not id_names:
        return []
    named = ", ".join(id_names)
    if op == "submit":
        walk = ("credit rows walk each Invoice Discounting Sanctioned->Disbursed, debit rows "
                "Disbursed->Settled")
    else:
        walk = ("the status walk reverses: Disbursed->Sanctioned on credit rows, "
                "Settled->Disbursed on debit rows")
    return [
        f"this Journal Entry's accounts rows reach {len(id_names)} Invoice Discounting "
        f"document(s) ({named}) through the reference_type Select — a channel invisible to "
        f"Link-field scans: ERPNext's update_invoice_discounting (journal_entry.py:459-501) "
        f"THROWS unless each Invoice Discounting's live status matches the expected stage "
        f"(another document's state — this {op} can fail at execute on a clean-looking plan), "
        f"and when it proceeds, {walk} via a raw db_set (no validate/hooks/versioning), then "
        f"recomputes and db_sets EVERY linked Sales Invoice's status in the same motion "
        f"(invoice_discounting.py:101-108)"]


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
        cfg = SUPPORTED_DOCTYPES[doctype]
        rows = client.list_documents(doctype, filters=args.get("filters"),
                                     limit=int(args.get("limit", 20)),
                                     party_field=cfg["party_field"],
                                     date_field=_date_field_for(doctype))
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
        # Dunning breadth: the native preview RPC is UNCALLABLE for this doctype — Dunning has no
        # make_gl_entries method anywhere in its class hierarchy, so ERPNext's own preview would
        # call it as a bare method and raise AttributeError on a live bench (see
        # _dunning_ledger_preview_unavailable_flag's own docstring for the full finding). Skip the
        # network round-trip entirely rather than let every Dunning plan_submit refuse with an
        # opaque bench error; projected_gl is [] BY CONSTRUCTION for this doctype alone. Every
        # other doctype's preview call is unchanged. Landed Cost Voucher breadth: the SAME
        # AttributeError risk, for a different structural reason (LandedCostVoucher(Document) has
        # no make_gl_entries in its MRO either — see
        # _landed_cost_voucher_ledger_preview_unavailable_flag's own docstring) — joins the skip.
        # Blanket Order breadth: the SAME AttributeError risk (BlanketOrder(Document) also has no
        # make_gl_entries in its MRO — see
        # _blanket_order_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Job Card breadth: the SAME AttributeError risk (JobCard(Document) also has no
        # make_gl_entries in its MRO — see
        # _job_card_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # BOM breadth: the SAME AttributeError risk (BOM(WebsiteGenerator), and WebsiteGenerator
        # is a bare Document subclass — see _bom_ledger_preview_unavailable_flag's own docstring)
        # — joins the skip too.
        # Work Order breadth: the SAME AttributeError risk (WorkOrder(Document), no
        # make_gl_entries in 3114 lines — see _work_order_ledger_preview_unavailable_flag's own
        # docstring) — joins the skip too.
        # Packing Slip breadth: the SAME AttributeError risk (PackingSlip(StatusUpdater), and
        # StatusUpdater is a bare Document subclass — see
        # _packing_slip_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Cost Center Allocation breadth: the SAME AttributeError risk (a direct Document
        # subclass, no make_gl_entries anywhere — see
        # _cost_center_allocation_ledger_preview_unavailable_flag's own docstring) — joins the
        # skip too.
        # Supplier Scorecard Period breadth: the SAME AttributeError risk (a direct Document
        # subclass, no make_gl_entries anywhere — see
        # _supplier_scorecard_period_ledger_preview_unavailable_flag's own docstring) — joins the
        # skip too.
        # Quality Inspection breadth: the SAME AttributeError risk (a direct Document subclass, no
        # make_gl_entries anywhere in EITHER the erpnext-16 or frappe-16 checkouts — see
        # _quality_inspection_ledger_preview_unavailable_flag's own docstring) — joins the skip
        # too.
        # Installation Note breadth: the SAME AttributeError risk, reached through a DEEPER MRO
        # (InstallationNote -> TransactionBase -> StatusUpdater -> Document — no make_gl_entries
        # in either transaction_base.py or status_updater.py — see
        # _installation_note_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Shipment breadth: the SAME AttributeError risk, the SIMPLEST MRO in the category
        # (Shipment(Document) directly, no make_gl_entries anywhere — see
        # _shipment_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Sales Forecast breadth: the SAME AttributeError risk, the CLEANEST case yet
        # (SalesForecast(Document) directly, zero accounting/stock-controller-related imports of
        # any kind — see _sales_forecast_ledger_preview_unavailable_flag's own docstring) — joins
        # the skip too.
        # Project Update breadth: the SAME AttributeError risk, the SAME cleanest import shape as
        # Sales Forecast (ProjectUpdate(Document) directly, only frappe/Document imported — see
        # _project_update_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Maintenance Visit breadth: the SAME AttributeError risk, the SAME MRO depth Installation
        # Note established (MaintenanceVisit -> TransactionBase -> StatusUpdater -> Document —
        # see _maintenance_visit_ledger_preview_unavailable_flag's own docstring) — joins the
        # skip too.
        # Maintenance Schedule breadth: the SAME AttributeError risk, the SAME MRO depth
        # Installation Note/Maintenance Visit established (MaintenanceSchedule -> TransactionBase
        # -> StatusUpdater -> Document — see
        # _maintenance_schedule_ledger_preview_unavailable_flag's own docstring) — joins the skip
        # too.
        # Asset Maintenance Log breadth: the SAME AttributeError risk, the SIMPLEST bare-Document
        # MRO in the category — see
        # _asset_maintenance_log_ledger_preview_unavailable_flag's own docstring — joins the skip
        # too.
        # Bank Guarantee breadth: the SAME AttributeError risk, the SAME SIMPLEST bare-Document
        # MRO (BankGuarantee(Document) directly, no make_gl_entries anywhere — see
        # _bank_guarantee_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Asset Movement breadth: the SAME AttributeError risk, the SAME bare-Document MRO
        # (AssetMovement(Document) directly, no make_gl_entries anywhere — see
        # _asset_movement_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Delivery Trip breadth: the SAME AttributeError risk, the SAME bare-Document MRO
        # (DeliveryTrip(Document) directly, no make_gl_entries anywhere — see
        # _delivery_trip_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Asset Value Adjustment breadth: the SAME AttributeError risk, the SAME bare-Document MRO
        # (AssetValueAdjustment(Document) directly, no make_gl_entries anywhere — see
        # _asset_value_adjustment_ledger_preview_unavailable_flag's own docstring) — joins the
        # skip too. Unlike every prior member, THIS doctype's own empty preview does not mean no
        # GL posts at all: a sibling Journal Entry posts real GL synchronously (disclosed below).
        # Payment Order breadth: the SAME AttributeError risk, the SAME bare-Document MRO
        # (PaymentOrder(Document) directly, no make_gl_entries anywhere — see
        # _payment_order_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Share Transfer breadth: the SAME AttributeError risk, the SAME bare-Document MRO
        # (ShareTransfer(Document) directly, no make_gl_entries anywhere — see
        # _share_transfer_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # BOM Creator breadth: the SAME AttributeError risk, the SAME bare-Document MRO
        # (BOMCreator(Document) directly, no make_gl_entries anywhere — see
        # _bom_creator_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Budget breadth: the SAME AttributeError risk, the SAME bare-Document MRO
        # (Budget(Document) directly, no make_gl_entries anywhere — see
        # _budget_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Timesheet breadth: the SAME AttributeError risk, the SAME bare-Document MRO
        # (Timesheet(Document) directly, no make_gl_entries anywhere incl. timesheet_detail.py —
        # see _timesheet_ledger_preview_unavailable_flag's own docstring) — joins the skip too.
        # Contract breadth: the SAME AttributeError risk, the SAME bare-Document MRO
        # (Contract(Document) directly, no make_gl_entries anywhere — see
        # _contract_ledger_preview_unavailable_flag's own docstring) — joins the skip too, its
        # TWENTY-SEVENTH member.
        # Pick List breadth: the SAME AttributeError risk, the DEEPEST MRO of the category
        # (PickList -> TransactionBase -> StatusUpdater -> Document, 4-deep, the addendum's own
        # MRO correction — see _pick_list_ledger_preview_unavailable_flag's own docstring) — joins
        # the skip too, its TWENTY-EIGHTH member.
        # Production Plan breadth: the SAME AttributeError risk, the SAME bare-Document MRO
        # (ProductionPlan(Document) directly, no make_gl_entries anywhere in the 2261-line file —
        # see _production_plan_ledger_preview_unavailable_flag's own docstring) — joins the skip
        # too, its TWENTY-NINTH member.
        if doctype in (DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER,
                       PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
                       QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST,
                       PROJECT_UPDATE, MAINTENANCE_VISIT, MAINTENANCE_SCHEDULE,
                       ASSET_MAINTENANCE_LOG, BANK_GUARANTEE, ASSET_MOVEMENT, DELIVERY_TRIP,
                       ASSET_VALUE_ADJUSTMENT, PAYMENT_ORDER, SHARE_TRANSFER, BOM_CREATOR,
                       BUDGET, TIMESHEET, CONTRACT, PICK_LIST, PRODUCTION_PLAN,
                       # Subcontracting Receipt (THE ROOF ROW) is the tuple's THIRTIETH member
                       # for a DIFFERENT cause than every other member (all AttributeError-class:
                       # no make_gl_entries anywhere in the MRO): SCR's preview is genuinely
                       # CALLABLE but STRUCTURALLY BROKEN UPSTREAM — absent from ERPNext's own
                       # SLE-seeding whitelist (stock_controller.py:2109), its own :847 None-
                       # arithmetic raises a live TypeError on any draft preview under perpetual
                       # inventory (the exact config where a real submit posts real GL). The
                       # broker refuses to call a preview upstream broke — supervisor's ruling
                       # 2026-07-22, closing the landing's own owed decision; see
                       # _subcontracting_receipt_ledger_preview_gap_flag.
                       SUBCONTRACTING_RECEIPT):
            preview = {"gl_data": []}
        else:
            preview = client.ledger_preview(company=company, doctype=doctype, docname=name)
        risk_flags = []
        posting_date = _posting_date_of(doc, doctype)
        # The sentinel guard is load-bearing, not defensive: NO_DATE_FIELD compares
        # lexicographically GREATER than every ISO date (see its plan.py comment), so without the
        # branch a dateless doctype would falsely flag "in the future" on every plan.
        if posting_date != NO_DATE_FIELD and posting_date > self._now_date():
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
            # Invoice Discounting reach retrofit (pre-verification fan, 2026-07-21) — the
            # reference_type Select channel the founding landing couldn't see.
            risk_flags.extend(_journal_entry_invoice_discounting_reach_flags(doc, "submit"))
        # Envelope E2: physical-stock disclosure — fires only when the doc itself carries a
        # truthy update_stock (SI/PI); a no-op for every other doctype by construction. doctype
        # threaded through (POS Invoice breadth) so the wording can name POS Invoice's own
        # on_submit-bypass instead of falsely claiming a stock post that never happens.
        risk_flags.extend(_update_stock_risk_flags(doc, "submit", doctype))
        # Envelope E4: is_return / is_pos disclosures — doctype-agnostic (SI/PI/Delivery Note all
        # carry is_return; is_pos is free-standing), read from the doc's own fields, no-op unless
        # set. doctype threaded through (Delivery Note breadth) so the settlement wording can tell
        # a stock-only return apart from SI/PI's own receivable-bearing shape.
        risk_flags.extend(_return_risk_flags(doc, doctype))
        risk_flags.extend(_pos_risk_flags(doc, "submit"))
        # POS Invoice breadth: the preview's own make_gl_entries() bypasses on_submit entirely (see
        # _pos_invoice_ledger_deferral_flag's own docstring) — a no-op for every other doctype.
        risk_flags.extend(_pos_invoice_ledger_deferral_flag(doctype))
        # Dunning breadth: the preview call above was skipped entirely, not merely non-posting —
        # names why (see _dunning_ledger_preview_unavailable_flag's own docstring). A no-op for
        # every other doctype.
        risk_flags.extend(_dunning_ledger_preview_unavailable_flag(doctype))
        # Stock Reconciliation breadth: the preview call above WAS made (unlike Dunning — it's
        # callable and harmless here) but its empty result is a false negative, not an honest
        # no-op — names why (see _stock_reconciliation_ledger_preview_incomplete_flag's own
        # docstring). A no-op for every other doctype.
        risk_flags.extend(_stock_reconciliation_ledger_preview_incomplete_flag(doctype))
        # Landed Cost Voucher breadth: the preview call above was skipped entirely (the Dunning
        # shape), AND even a working preview would describe the wrong document — names why (see
        # _landed_cost_voucher_ledger_preview_unavailable_flag's own docstring). A no-op for every
        # other doctype.
        risk_flags.extend(_landed_cost_voucher_ledger_preview_unavailable_flag(doctype))
        # Blanket Order breadth: the preview call above was skipped entirely (the Dunning shape,
        # never LCV's dual-flag one — Blanket Order has no revaluation-elsewhere side effect) —
        # names why (see _blanket_order_ledger_preview_unavailable_flag's own docstring). A no-op
        # for every other doctype.
        risk_flags.extend(_blanket_order_ledger_preview_unavailable_flag(doctype))
        # Job Card breadth: the preview call above was skipped entirely (the Dunning/Blanket-Order
        # shape, never LCV's dual-flag one — Job Card has no revaluation-elsewhere side effect) —
        # names why (see _job_card_ledger_preview_unavailable_flag's own docstring). A no-op for
        # every other doctype.
        risk_flags.extend(_job_card_ledger_preview_unavailable_flag(doctype))
        # BOM breadth: same shape again (preview uncallable, skipped entirely; no
        # revaluation-elsewhere side effect on the submit/cancel lifecycle) — names why (see
        # _bom_ledger_preview_unavailable_flag's own docstring). A no-op for every other doctype.
        risk_flags.extend(_bom_ledger_preview_unavailable_flag(doctype))
        # Work Order breadth: same shape again — names why (see
        # _work_order_ledger_preview_unavailable_flag's own docstring). A no-op for every other
        # doctype.
        risk_flags.extend(_work_order_ledger_preview_unavailable_flag(doctype))
        # Asset breadth: the preview above was CALLABLE (Asset never joins the skip tuple) —
        # what these flags add is the two async GL channels submit arms (depreciation JEs +
        # the deferred CWIP transfer), the conditions under which an empty projected_gl is a
        # deferral not a no-op, and the auto-created sibling Asset Movement. Doctype-scoped,
        # data-driven — a no-op for every other doctype.
        if doctype == ASSET:
            risk_flags.extend(_asset_submit_risk_flags(doc, self._now_date()))
        # Packing Slip breadth: same shape again (preview uncallable, skipped entirely; no
        # revaluation-elsewhere side effect — on_submit/on_cancel rewrite a quantity counter on
        # the linked Delivery Note, never a ledger row) — names why (see
        # _packing_slip_ledger_preview_unavailable_flag's own docstring). A no-op for every other
        # doctype.
        risk_flags.extend(_packing_slip_ledger_preview_unavailable_flag(doctype))
        # Cost Center Allocation breadth: same shape again (preview uncallable, skipped entirely;
        # no on_submit/on_cancel hook of any kind — the simplest lifecycle this campaign has
        # found, never even a counter rewrite) — names why (see
        # _cost_center_allocation_ledger_preview_unavailable_flag's own docstring). A no-op for
        # every other doctype.
        risk_flags.extend(_cost_center_allocation_ledger_preview_unavailable_flag(doctype))
        # Supplier Scorecard Period breadth: same shape again (preview uncallable, skipped
        # entirely; no on_submit/on_cancel hook of any kind — the same simplest-lifecycle shape
        # Cost Center Allocation established) — names why (see
        # _supplier_scorecard_period_ledger_preview_unavailable_flag's own docstring). A no-op
        # for every other doctype.
        risk_flags.extend(_supplier_scorecard_period_ledger_preview_unavailable_flag(doctype))
        # Quality Inspection breadth: same shape again (preview uncallable, skipped entirely) —
        # names why (see _quality_inspection_ledger_preview_unavailable_flag's own docstring). A
        # no-op for every other doctype.
        risk_flags.extend(_quality_inspection_ledger_preview_unavailable_flag(doctype))
        # Quality Inspection breadth: before_submit's own readings-status gate, readable off the
        # draft (see _quality_inspection_submit_risk_flags's own docstring — a genuine dossier
        # omission). Doctype-scoped, data-driven — a no-op for every other doctype.
        if doctype == QUALITY_INSPECTION:
            risk_flags.extend(_quality_inspection_submit_risk_flags(doc))
        # Installation Note breadth: same shape again (preview uncallable, skipped entirely; a
        # DEEPER MRO than any prior member but the same conclusion) — names why (see
        # _installation_note_ledger_preview_unavailable_flag's own docstring). A no-op for every
        # other doctype. validate_serial_no()'s own doomed-submit gate needs no flag here (every
        # throw condition requires reading a DIFFERENT doctype — see the module docstring).
        risk_flags.extend(_installation_note_ledger_preview_unavailable_flag(doctype))
        # Shipment breadth: same shape again (preview uncallable, skipped entirely; the SIMPLEST
        # MRO in the category — bare Document, no make_gl_entries anywhere) — names why (see
        # _shipment_ledger_preview_unavailable_flag's own docstring). A no-op for every other
        # doctype. on_submit's own two throws need no flag here — both are doc-readable but
        # ordinary reqd/business-rule guards (see the module docstring).
        risk_flags.extend(_shipment_ledger_preview_unavailable_flag(doctype))
        # Sales Forecast breadth: same shape again (preview uncallable, skipped entirely; the
        # CLEANEST case yet — zero accounting/stock-controller imports of any kind) — names why
        # (see _sales_forecast_ledger_preview_unavailable_flag's own docstring). A no-op for every
        # other doctype. Neither on_submit nor on_cancel is even defined here, so there is no
        # other gate to disclose.
        risk_flags.extend(_sales_forecast_ledger_preview_unavailable_flag(doctype))
        # Project Update breadth: same shape again (preview uncallable, skipped entirely; the
        # SAME cleanest import shape as Sales Forecast) — names why (see
        # _project_update_ledger_preview_unavailable_flag's own docstring). A no-op for every
        # other doctype. Neither on_submit nor on_cancel is even defined here either (a bare
        # 'pass' class body), so there is no other gate to disclose. The genuinely new finding
        # this landing carries — date_field="date" is a REAL field that can still be blank on an
        # API-authored draft — needs no dedicated flag function: the standing
        # _plan_closed_books_risk call immediately below already discloses "no posting_date"
        # for ANY doctype whose posting_date reads back empty, Project Update included.
        risk_flags.extend(_project_update_ledger_preview_unavailable_flag(doctype))
        # Maintenance Visit breadth: same shape again (preview uncallable, skipped entirely; the
        # SAME MRO depth Installation Note established) — names why (see
        # _maintenance_visit_ledger_preview_unavailable_flag's own docstring). A no-op for every
        # other doctype. update_customer_issue(1)'s own Warranty Claim db_update bypass IS
        # data-driven off the draft's own maintenance_schedule/purposes fields — disclosed via its
        # own dedicated flag function, doctype-scoped, data-driven — a no-op for every other
        # doctype.
        risk_flags.extend(_maintenance_visit_ledger_preview_unavailable_flag(doctype))
        if doctype == MAINTENANCE_VISIT:
            risk_flags.extend(_maintenance_visit_submit_risk_flags(doc))
        # Maintenance Schedule breadth: same shape again (preview uncallable, skipped entirely;
        # the SAME MRO depth Installation Note/Maintenance Visit established) — names why (see
        # _maintenance_schedule_ledger_preview_unavailable_flag's own docstring). A no-op for
        # every other doctype. The Event auto-creation + Serial No .save() mutation are BOTH
        # data-driven off the draft's own schedules/items fields — disclosed via a dedicated
        # flag function, doctype-scoped — a no-op for every other doctype.
        risk_flags.extend(_maintenance_schedule_ledger_preview_unavailable_flag(doctype))
        if doctype == MAINTENANCE_SCHEDULE:
            risk_flags.extend(_maintenance_schedule_submit_risk_flags(doc))
        # Asset Maintenance Log breadth: same shape again (preview uncallable, skipped entirely;
        # the SIMPLEST bare-Document MRO in the category) — names why (see
        # _asset_maintenance_log_ledger_preview_unavailable_flag's own docstring). A no-op for
        # every other doctype. The doomed-submit determination + the Task/parent-Asset-Maintenance
        # .save() cascade are BOTH data-driven off the draft's own maintenance_status/
        # completion_date/task/asset_maintenance fields — disclosed via a dedicated flag function,
        # doctype-scoped — a no-op for every other doctype.
        risk_flags.extend(_asset_maintenance_log_ledger_preview_unavailable_flag(doctype))
        if doctype == ASSET_MAINTENANCE_LOG:
            risk_flags.extend(_asset_maintenance_log_submit_risk_flags(doc))
        # Bank Guarantee breadth: same shape again (preview uncallable, skipped entirely; the
        # SAME SIMPLEST bare-Document MRO in the category) — names why (see
        # _bank_guarantee_ledger_preview_unavailable_flag's own docstring). A no-op for every
        # other doctype. The doomed-submit determination is data-driven off the draft's own
        # bank_guarantee_number/name_of_beneficiary/bank fields — disclosed via a dedicated flag
        # function, doctype-scoped — a no-op for every other doctype. No cancel-direction flag
        # function: on_cancel is CONFIRMED ABSENT but with nothing for it to reverse either (this
        # doctype's own on_submit performs no mutation beyond its own three throws) — the
        # unremarkable case every simple doctype in this campaign already carries un-disclosed.
        risk_flags.extend(_bank_guarantee_ledger_preview_unavailable_flag(doctype))
        if doctype == BANK_GUARANTEE:
            risk_flags.extend(_bank_guarantee_submit_risk_flags(doc))
        # Asset Movement breadth: same shape again (preview uncallable, skipped entirely; a bare
        # Document MRO) — names why (see _asset_movement_ledger_preview_unavailable_flag's own
        # docstring). A no-op for every other doctype. THE central finding — on_submit and
        # on_cancel call the EXACT SAME method — is disclosed via ONE function shared by both
        # directions (_asset_movement_write_risk_flags, the _pos_risk_flags "op" shape),
        # data-driven off the draft's own assets child rows. Doctype-scoped — a no-op for every
        # other doctype.
        risk_flags.extend(_asset_movement_ledger_preview_unavailable_flag(doctype))
        if doctype == ASSET_MOVEMENT:
            risk_flags.extend(_asset_movement_write_risk_flags(doc, "submit"))
        # Delivery Trip breadth: same shape again (preview uncallable, skipped entirely; a bare
        # Document MRO) — names why (see _delivery_trip_ledger_preview_unavailable_flag's own
        # docstring). A no-op for every other doctype. The doomed-submit "driver must be set" gate
        # is deterministic off the draft; the linked-Delivery-Note-still-draft gate is prose,
        # naming only. Doctype-scoped — a no-op for every other doctype.
        risk_flags.extend(_delivery_trip_ledger_preview_unavailable_flag(doctype))
        if doctype == DELIVERY_TRIP:
            risk_flags.extend(_delivery_trip_submit_risk_flags(doc))
        # Asset Value Adjustment breadth: same shape again (preview uncallable, skipped entirely;
        # a bare Document MRO) — names why (see
        # _asset_value_adjustment_ledger_preview_unavailable_flag's own docstring). A no-op for
        # every other doctype. THE central finding — the synchronous sibling Journal Entry this
        # submit creates, the doomed-submit company-blank gate, the closed-books scope gap, the
        # cross-document validate_date/depreciation-reschedule prose, and the raw-write bypass on
        # the linked Asset — is disclosed via _asset_value_adjustment_submit_risk_flags.
        # Doctype-scoped — a no-op for every other doctype.
        risk_flags.extend(_asset_value_adjustment_ledger_preview_unavailable_flag(doctype))
        if doctype == ASSET_VALUE_ADJUSTMENT:
            risk_flags.extend(_asset_value_adjustment_submit_risk_flags(doc))
        # Payment Order breadth: same shape again (preview uncallable, skipped entirely; a bare
        # Document MRO) — names why (see _payment_order_ledger_preview_unavailable_flag's own
        # docstring). A no-op for every other doctype. THE central finding — update_payment_status
        # writes a status value directly onto every referenced Payment Request/Payment Entry via
        # raw frappe.db.set_value — is disclosed via _payment_order_write_risk_flags, data-driven
        # off the draft's own references/payment_order_type fields. Doctype-scoped — a no-op for
        # every other doctype.
        risk_flags.extend(_payment_order_ledger_preview_unavailable_flag(doctype))
        if doctype == PAYMENT_ORDER:
            risk_flags.extend(_payment_order_write_risk_flags(doc, "submit"))
        # Share Transfer breadth: same shape again (preview uncallable, skipped entirely; a bare
        # Document MRO) — names why (see _share_transfer_ledger_preview_unavailable_flag's own
        # docstring). THE sharpest finding — a doomed-submit gate in basic_validations()'s
        # Purchase branch (a from_folio_no/to_shareholder field mismatch in ERPNext's own source,
        # traced through frappe.get_doc("Shareholder", None)) — plus the folio_no_validation()
        # every-save write and the on_submit share_balance mutation are disclosed via
        # _share_transfer_submit_risk_flags, data-driven off the draft's own fields. Doctype-scoped
        # — a no-op for every other doctype.
        risk_flags.extend(_share_transfer_ledger_preview_unavailable_flag(doctype))
        if doctype == SHARE_TRANSFER:
            risk_flags.extend(_share_transfer_submit_risk_flags(doc))
        # BOM Creator breadth: same shape again (preview uncallable, skipped entirely; a bare
        # Document MRO) — names why (see _bom_creator_ledger_preview_unavailable_flag's own
        # docstring). THE central finding — John's ruling 2, the two-phase PROVE: on_submit
        # enqueues create_boms() on a background queue, so the outcome this submit will record is
        # narrowed to "committed_pending_async" rather than a plain "committed" — plus the
        # amend-cycle duplicate-tree finding when this draft's own amended_from is set — are
        # disclosed via _bom_creator_submit_risk_flags, data-driven off the draft's own fields.
        # Doctype-scoped — a no-op for every other doctype.
        risk_flags.extend(_bom_creator_ledger_preview_unavailable_flag(doctype))
        if doctype == BOM_CREATOR:
            risk_flags.extend(_bom_creator_submit_risk_flags(doc))
        # Budget breadth: same shape again (preview uncallable, skipped entirely; a bare Document
        # MRO) — names why (see _budget_ledger_preview_unavailable_flag's own docstring). THE
        # central finding — submitting a Budget ARMS a belt governing FUTURE submits of OTHER
        # documents (PO/MR through either engine, per Accounts Settings.
        # use_legacy_budget_controller), disclosed data-driven off the draft's own
        # applicable_on_*/action_if_*/account/budget_against/window fields — see
        # _budget_submit_risk_flags. Doctype-scoped — a no-op for every other doctype.
        risk_flags.extend(_budget_ledger_preview_unavailable_flag(doctype))
        if doctype == BUDGET:
            risk_flags.extend(_budget_submit_risk_flags(doc))
        # Timesheet breadth: same shape again (preview uncallable, skipped entirely; a bare
        # Document MRO) — names why (see _timesheet_ledger_preview_unavailable_flag's own
        # docstring). THE central finding — THE SECOND WRITER: a Sales Invoice's own submit OR
        # cancel can rewrite this Timesheet's own status/billing fields via a raw whole-document
        # db_update_all(), with zero validate()/hooks/permission re-check — plus the corrected
        # from_time/to_time boolean guard (BOTH absent throws, not either) and the status
        # precedence correction (sales_invoice truthy always wins "Completed" last) — see
        # _timesheet_submit_risk_flags. Doctype-scoped — a no-op for every other doctype.
        risk_flags.extend(_timesheet_ledger_preview_unavailable_flag(doctype))
        if doctype == TIMESHEET:
            risk_flags.extend(_timesheet_submit_risk_flags(doc))
        # Contract breadth: same shape again (preview uncallable, skipped entirely; a bare
        # Document MRO) — names why (see _contract_ledger_preview_unavailable_flag's own
        # docstring). THE central findings — THE WRITABILITY ILLUSION (status/fulfilment_status
        # look allow_on_submit-writable but before_update_after_submit stomps them back every
        # save), THE SCHEDULER (the campaign's first confirmed SUBMITTED-STATE scheduler
        # mutator), the end_date<start_date doomed-submit throw, and the before_submit
        # signed_by_company stamp — see _contract_submit_risk_flags. Doctype-scoped — a no-op
        # for every other doctype.
        risk_flags.extend(_contract_ledger_preview_unavailable_flag(doctype))
        if doctype == CONTRACT:
            risk_flags.extend(_contract_submit_risk_flags(doc))
        # Pick List breadth: same shape again (preview uncallable, skipped entirely; the deepest
        # bare MRO of the category) — names why (see
        # _pick_list_ledger_preview_unavailable_flag's own docstring). THE central findings — THE
        # CASCADE QUESTION (NOT a leaf on three independent counts, disclosed at the blast-radius
        # gate above, not here), the 4 deterministic doomed-submit throws (for_qty/missing-
        # warehouse/scan_mode-short/legacy-serial-batch), the ~8 cross-document throws staying
        # prose, THE TWO-JAW TRAP (can't edit once reserved, CAN cancel-and-orphan), and THE
        # RESERVATION MACHINERY (a multi-hop delegation shared with Purchase Receipt today) — see
        # _pick_list_submit_risk_flags. Doctype-scoped — a no-op for every other doctype.
        risk_flags.extend(_pick_list_ledger_preview_unavailable_flag(doctype))
        if doctype == PICK_LIST:
            risk_flags.extend(_pick_list_submit_risk_flags(doc))
        # Asset Repair breadth: the preview above was CALLABLE (Asset Repair never joins the skip
        # tuple — the Asset precedent, make_gl_entries is its own method) — what these flags add
        # is the two independent honest-empty causes, THE STOCK ENTRY AUTO-SUBMIT (unconditional,
        # no try/except), THE BLANK-COMPANY CONSEQUENCE (armed under two independent conditions,
        # unlike Timesheet's own zero-consequence blank company), the deterministic Pending
        # doomed-submit throw, and THE ASSET-STATUS SIDE-WRITE (submit-only, correcting the
        # pre-verification addendum's own "fires ... on cancel alike" claim) — see
        # _asset_repair_submit_risk_flags. Doctype-scoped, data-driven — a no-op for every other
        # doctype.
        if doctype == ASSET_REPAIR:
            risk_flags.extend(_asset_repair_submit_risk_flags(doc))
        # Invoice Discounting breadth: the preview above was CALLABLE (the THIRD such row, after
        # Asset/Asset Repair — make_gl_entries is its own method) — what these flags add is the
        # deterministic loan_start_date/loan_period doomed-submit throw, the two cross-document
        # validate_invoices throws (staying prose), the per-ROW honest-empty GL split, the
        # is_discounted side-write, and THE INCOMING JOURNAL ENTRY REACH — this document's own
        # status is not stable even once submitted, a governed Journal Entry elsewhere can flip it
        # (the reciprocal half of the retrofit already wired into Journal Entry's own disclosure,
        # commit 3fa3303) — see _invoice_discounting_submit_risk_flags. Doctype-scoped,
        # data-driven — a no-op for every other doctype.
        if doctype == INVOICE_DISCOUNTING:
            risk_flags.extend(_invoice_discounting_submit_risk_flags(doc))
        # Asset Capitalization breadth: the preview above was CALLABLE (the FOURTH such row, after
        # Asset/Asset Repair/Invoice Discounting — make_gl_entries is its own method) — what these
        # flags add is the deterministic empty-child-tables doomed-submit throw, the target-asset
        # cost write (the dossier's own double-count RED FLAG, refuted), the depreciation
        # sibling-document-factory (the SECOND confirmed instance, after Asset Value Adjustment),
        # and the repost_future_sle_and_gle async channel — see
        # _asset_capitalization_submit_risk_flags. Doctype-scoped, data-driven — a no-op for every
        # other doctype.
        if doctype == ASSET_CAPITALIZATION:
            risk_flags.extend(_asset_capitalization_submit_risk_flags(doc))
        # Production Plan breadth: the preview above was UNCALLABLE (its own skip-tuple flag,
        # above) — these flags add the TWO auto-submit channels (the caller-flag Material
        # Request prose, and the UNCONDITIONAL Stock Reservation Entry channel off the real
        # reserve_stock field — the dossier's own "auto-submitted: NO" claim overturned), THE
        # FORWARD CHECK naming the confirmed retroactive gap in Work Order's own landing, the
        # add_reference_to_raw_materials conditional-throw arming clause, and the hookless
        # Bin/Sales-Order-Item point-writes — see _production_plan_submit_risk_flags.
        # Doctype-scoped, data-driven off the draft's own reserve_stock/mr_items — a no-op for
        # every other doctype.
        risk_flags.extend(_production_plan_ledger_preview_unavailable_flag(doctype))
        if doctype == PRODUCTION_PLAN:
            risk_flags.extend(_production_plan_submit_risk_flags(doc))
        # The 2026-07-22 debt pass: Work Order's reserve_stock channel — the SECOND confirmed
        # retroactive gap (Production Plan's own forward check named it), closed with the
        # transfer-vs-create dispatch, the silent-partial, the live-state throws, and the raw
        # status db_sets — see _work_order_submit_risk_flags. Data-driven off the draft's own
        # reserve_stock/production_plan/subcontracting_inward_order — a no-op for every other
        # doctype and every unreserved draft.
        if doctype == WORK_ORDER:
            risk_flags.extend(_work_order_submit_risk_flags(doc))
        # Subcontracting Order breadth: the preview above was CALLABLE, honest-empty (the SO/PO
        # shape, NOT the skip tuple — make_gl_entries genuinely exists in the MRO but on_submit
        # never posts a Stock Ledger Entry under this voucher's own name) — these flags add THE
        # SEVEN-PATH MUTATOR MAP (six of seven paths into this order's own status/supplied_items
        # carry zero write-permission check, firing from Subcontracting Receipt's/Stock Entry's
        # OWN submit/cancel), the six validate() throws split deterministic-REFUSED vs
        # cross-document prose, and the unconditional Stock Reservation Entry auto-submit
        # channel off the draft's own reserve_stock (the SAME StockReservation class Production
        # Plan's own landing already proved auto-submits) — see
        # _subcontracting_order_submit_risk_flags. Doctype-scoped, data-driven off the draft's
        # own purchase_order/supplied_items/reserve_stock — a no-op for every other doctype.
        if doctype == SUBCONTRACTING_ORDER:
            risk_flags.extend(_subcontracting_order_submit_risk_flags(doc))
        # Subcontracting Inward Order breadth: the preview above WAS called (CALLABLE, unlike
        # Dunning/Production Plan's own skip tuple) but is unconditionally EMPTY — the POS
        # Invoice false-positive shape mirrored (see
        # _subcontracting_inward_order_ledger_preview_deferral_flag's own docstring) — plus THE
        # ELEVEN-ROW MUTATOR MAP + THE NEW BYPASS CLASS (destructive delete of docstatus=1 child
        # rows) and the status three-way-writable/validate-chain-skip findings — see
        # _subcontracting_inward_order_submit_risk_flags. Doctype-scoped — a no-op for every
        # other doctype.
        risk_flags.extend(_subcontracting_inward_order_ledger_preview_deferral_flag(doctype))
        if doctype == SUBCONTRACTING_INWARD_ORDER:
            risk_flags.extend(_subcontracting_inward_order_submit_risk_flags(doc))
        # Subcontracting Receipt breadth: THE ROOF ROW. The preview above was NOT called — SCR
        # is the skip tuple's 30th member by the supervisor's 2026-07-22 ruling, for its own
        # sixth-shape cause: genuinely CALLABLE both-ledgers machinery that ERPNext's own
        # preview-whitelist omission turns into a live TypeError on draft previews under
        # perpetual inventory (see _subcontracting_receipt_ledger_preview_gap_flag's docstring) —
        # this landing's own finding, beyond both dossier and addendum. Plus THE CANCEL BACK-LINK GATE
        # (submit-direction context), THE SCO WRITEBACK (four channels, two more than
        # Subcontracting Order's own landing enumerated), the deterministic validate() throws,
        # the repost channel, and the return/job-card disclosures — see
        # _subcontracting_receipt_submit_risk_flags. Doctype-scoped — a no-op for every other
        # doctype.
        risk_flags.extend(_subcontracting_receipt_ledger_preview_gap_flag(doctype))
        if doctype == SUBCONTRACTING_RECEIPT:
            risk_flags.extend(_subcontracting_receipt_submit_risk_flags(doc))
        # The 2026-07-22 debt pass: the repost async channel for the four founding stock rows
        # (Stock Entry/Stock Reconciliation/Delivery Note/Purchase Receipt) — the confirmed
        # retroactive gap the Asset Capitalization landing reported, closed with the SAME
        # disclosure shape those later rows already carry. Conditional arming on submit
        # (back-dated postings only) — see _stock_repost_channel_flag. A no-op for every other
        # doctype.
        risk_flags.extend(_stock_repost_channel_flag(doctype, "submit"))
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
        deny-on-unreadable at execute — it never loosens anything.

        **Declared-dateless doctype (BOM breadth, 2026-07-21):** the check is not applicable —
        disclosed PLAINLY here rather than silently skipped, then returns before any lock read
        (:func:`_locks_for` would return ``{}`` and ``check_red_line`` would pass the sentinel
        anyway; the early return just avoids two no-op calls while keeping the disclosure the
        one visible artifact). The execute-time gate stays symmetric: ``_governed_write`` reads
        locks through the same :func:`_locks_for` and runs the same ``check_red_line``, so plan
        and execute can never disagree about what "not applicable" means.

        **Companyless (or blank-company) doctype — live-prove fix, 2026-07-21, shape-driven off
        ``company`` presence, never a doctype list.** :meth:`pacioli.erpnext.ErpnextClient.
        get_period_locks` unconditionally opens with ``self._doc_path("Company", company)``, which
        raises ``ErpnextError("a document name is required")`` for anything that isn't a non-empty
        string — so calling it with ``company is None`` (Bank Guarantee/Project Update/Supplier
        Scorecard Period/Shipment/Asset Maintenance Log — confirmed absent from their own schemas)
        or ``company == ""`` (a schema-present but non-``reqd`` field left blank, e.g. Asset Value
        Adjustment) is a REAL crash on a live bench, not a defensive guess — the exact failure the
        2026-07-21 live-prove batch caught: ``_tool_plan_submit -> _plan_closed_books_risk ->
        get_period_locks -> _doc_path("Company", None) -> ErpnextError``. The fix routes THIS
        method's own lock read through :func:`_locks_for` (below, replacing the former direct
        ``client.get_period_locks`` call) — the SAME shared gateway ``_governed_write``'s
        execute-time gate already used, which now carries a symmetric guard: a falsy ``company``
        short-circuits to ``{}`` (no locks) before any network call, exactly the way the dateless
        sentinel already short-circuits above. There is no company to read Accounting
        Period/frozen-till/PCV locks against, and — matching the dateless case's own reasoning —
        none would apply even if there were: every companyless doctype in this broker's set is
        confirmed absent from ``period_closing_doctypes`` (the same 18-entry list) and posts no
        GL, so ERPNext itself never period-checks it either, company or no company.

        **Disclosure priority — the ``elif`` below, deliberately not a second early return.** With
        ``locks`` forced to ``{}`` by a missing company, ``check_red_line`` still runs its OWN
        blank/malformed-posting_date checks first (its first two branches need no ``locks`` at
        all) — a genuinely undated/malformed draft on a companyless doctype gets THAT more specific
        refusal reason, never silently swallowed behind "no company to check". Only when
        ``check_red_line`` says the date itself is fine (``ok`` with ``locks={}`` trivially passing
        for lack of any boundary) does the missing company become the thing worth disclosing.

        **Reachable only under REG_UNPINNED:** a company-PINNED target already refuses a
        companyless/blank-company document as "wrong books" at the standing ``target.company and
        company != target.company`` gate in :meth:`PacioliBroker._tool_plan_submit`/
        ``_tool_plan_cancel`` (and the identical TOCTOU belt in
        :meth:`PacioliBroker._governed_write`) BEFORE this method is ever called — so the ``elif``
        below only ever fires for the one registry posture that could otherwise reach
        ``get_period_locks`` with a company that isn't a real name."""
        if posting_date == NO_DATE_FIELD:
            risk_flags.append(
                "closed-books: not applicable — this doctype carries no date field at all "
                "(date_field=None, a source-verified pin), so there is no posting date for any "
                "period lock to bite on; ERPNext itself never period-checks this doctype either "
                "(not in period_closing_doctypes, posts no GL)")
            return
        # The companyless guard lives INSIDE _locks_for (shared with the execute-time gate), never
        # a separate early return here: a blank/malformed posting_date is a SHARPER, pre-existing
        # concern than a missing company (check_red_line's own first two branches already refuse
        # an undated/malformed posting regardless of company, the same E6 disclosure every other
        # dated doctype gets) — routing through _locks_for first, then letting check_red_line
        # judge, keeps that priority intact instead of a company check silently pre-empting it.
        # Only once the DATE itself is clean (check_red_line says ok on locks={}) does the missing
        # company become the thing worth naming — otherwise "no company" would silently swallow a
        # genuinely blank/malformed date behind a less specific message.
        locks = _locks_for(client, company, doctype, posting_date)
        ok, reason = check_red_line(posting_date, self._now_date(), locks)
        if not ok:
            risk_flags.append(f"closed-books: {reason} — this posting will be refused at execute "
                              "unless it changes before then")
        elif not company:
            risk_flags.append(
                "closed-books: not applicable — this document carries no company (company field "
                "absent from the doctype's own schema, or present but blank on this draft), so "
                "there is no company to read Accounting Period/frozen-till/PCV locks against; "
                "ERPNext itself never period-checks this doctype either (not in "
                "period_closing_doctypes, posts no GL) — reachable only under an unpinned target, "
                "since a company-pinned target already refuses this document as wrong-books "
                "before this check would ever run")

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
        # SELF_UNLINKING_DOCTYPES exception (John's ruling 1, 2026-07-21 —
        # docs/plans/2026-07-21-cancel-truth-rulings.md): the ONE added condition below. For a
        # registered doctype (DELIVERY_TRIP only, for now — pacioli.erpnext.
        # SELF_UNLINKING_DOCTYPES's own docstring carries the source receipt), ERPNext's own
        # on_cancel self-unlinks every submitted incoming link BEFORE frappe's own back-link check
        # runs (document.py:1450-1452), so refusing here would be stricter than ERPNext for no
        # protective reason — the belt (_delivery_trip_cancel_risk_flags's disclosure, below) and
        # the suspenders (the post-execute self_unlink_readback, wired into _governed_write) carry
        # the consent+truth burden instead of this gate. `doctype not in SELF_UNLINKING_DOCTYPES`
        # is True for every doctype but the registered one, so this is byte-identical for
        # everyone else — never a shape-inferred exception.
        linked = client.get_submitted_linked_docs(doctype, name)
        if linked and doctype not in SELF_UNLINKING_DOCTYPES:
            shown = ", ".join(
                f"{d.get('doctype')} {d.get('name')}" for d in linked[:5] if isinstance(d, dict))
            more = f" (+{len(linked) - 5} more)" if len(linked) > 5 else ""
            return _deny(f"{len(linked)} submitted document(s) link to {name}: {shown}{more} — "
                         "cancelling would break them; cancel those first, or use "
                         "plan_cascade_cancel to govern the whole graph in one consent",
                         stage="plan")
        reversal = client.get_gl_entries(doctype, name)
        risk_flags = []
        posting_date = _posting_date_of(doc, doctype)
        # Same load-bearing sentinel guard as _tool_plan_submit's: NO_DATE_FIELD sorts after
        # every ISO date, so the unbranched comparison would falsely flag every dateless doc.
        if posting_date != NO_DATE_FIELD and posting_date > self._now_date():
            risk_flags.append("posting_date is in the future")
        if not reversal:
            risk_flags.append("no live GL rows found for this voucher — nothing visible to unwind")
        # Landed Cost Voucher breadth: the "no live GL rows" flag just above is TRUE (this doctype
        # never posts under its own voucher_type) but MISLEADING (cancelling it re-triggers a real
        # reversal on OTHER documents' ledgers) — a deliberate divergence from Dunning's own
        # precedent (whose identical empty read needed no addition, because Dunning's cancel
        # genuinely touches nothing else). See
        # _landed_cost_voucher_cancel_revaluation_flag's own docstring. A no-op for every other
        # doctype.
        risk_flags.extend(_landed_cost_voucher_cancel_revaluation_flag(doctype))
        # Asset breadth: the status gate readable on the draft (validate_cancellation's own
        # conditions, pre-disclosed) + the multi-document unwind disclosure. Note the multi-doc
        # flag will rarely be REACHED through this tool: the blast-radius gate above refuses
        # first for any submitted Asset (its own auto-created receipt Asset Movement is a
        # submitted link from birth), routing cancels through plan_cascade_cancel — reaching
        # here means the movement graph was already cancelled out from under this asset.
        # Doctype-scoped, data-driven — a no-op for every other doctype.
        if doctype == ASSET:
            risk_flags.extend(_asset_cancel_risk_flags(doc))
        # Quality Inspection breadth: update_qc_reference's own raw-SQL write into the reference
        # document, branched on reference_type (see _quality_inspection_cancel_risk_flags's own
        # docstring — the central dossier correction). Doctype-scoped, data-driven off the
        # draft's own reference_type/reference_name — a no-op for every other doctype.
        if doctype == QUALITY_INSPECTION:
            risk_flags.extend(_quality_inspection_cancel_risk_flags(doc))
        # Maintenance Visit breadth: check_if_last_visit()'s own temporal-ordering peer
        # constraint (prose only — needs a sibling read this plan cannot perform, the
        # Installation Note validate_serial_no precedent) PLUS the conditional Warranty Claim
        # db_update touch if that gate does not refuse (see
        # _maintenance_visit_cancel_risk_flags's own docstring — the central dossier correction on
        # its own §7 framing). Doctype-scoped, data-driven off the draft's own
        # maintenance_schedule/purposes fields — a no-op for every other doctype.
        if doctype == MAINTENANCE_VISIT:
            risk_flags.extend(_maintenance_visit_cancel_risk_flags(doc))
        # Maintenance Schedule breadth: the Serial No field reset (data-driven off the draft's
        # own items rows) PLUS standing prose naming the permanent Event delete_events() call and
        # the plain fact that ERPNext's own on_cancel carries no gate against a linked Maintenance
        # Visit at all (see _maintenance_schedule_cancel_risk_flags's own docstring) — this
        # broker's OWN blast-radius check above is what actually protects that case. Doctype-
        # scoped — a no-op for every other doctype.
        if doctype == MAINTENANCE_SCHEDULE:
            risk_flags.extend(_maintenance_schedule_cancel_risk_flags(doc))
        # Asset Maintenance Log breadth: standing prose only — on_cancel is CONFIRMED ABSENT
        # ENTIRELY (see _asset_maintenance_log_cancel_risk_flags's own docstring), so cancelling
        # never reverses the Task/parent-Asset-Maintenance .save() cascade submit performed.
        # Doctype-scoped — a no-op for every other doctype.
        if doctype == ASSET_MAINTENANCE_LOG:
            risk_flags.extend(_asset_maintenance_log_cancel_risk_flags(doc))
        # Asset Movement breadth: THE central finding again, cancel direction — on_cancel calls
        # the EXACT SAME method on_submit does (see _asset_movement_write_risk_flags's own
        # docstring), plus the unconditional asymmetric-clear prose (custodian clears to empty on
        # a full rollback; location does not — a genuine dossier correction). Doctype-scoped,
        # data-driven off the draft's own assets child rows — a no-op for every other doctype.
        if doctype == ASSET_MOVEMENT:
            risk_flags.extend(_asset_movement_write_risk_flags(doc, "cancel"))
        # Delivery Trip breadth: THE central finding, cancel direction — the sanctioned
        # update_after_submit save mechanism naming the linked Delivery Notes it force-clears,
        # PLUS the corrected structural finding (see _delivery_trip_cancel_risk_flags's own
        # docstring — supervisor-verified 2026-07-21) and John's ruling 1 (2026-07-21,
        # docs/plans/2026-07-21-cancel-truth-rulings.md): the blast-radius gate above no longer
        # refuses this doctype's leaf cancel on a submitted linked Delivery Note (the
        # SELF_UNLINKING_DOCTYPES exception), so these flags now fire here on EVERY submitted
        # Delivery Trip whose delivery_stops names a note, not only the narrower pre-cancelled-note
        # case — the belt half of the ruling (full disclosure before consent); the readback (the
        # suspenders) runs post-execute in _governed_write. plan_cascade_cancel remains dead at the
        # DEPENDENT Delivery Note's own cascade step (frappe's LinkExistsError) — its own per-node
        # loop below carries the identical disclosure. Doctype-scoped, data-driven off the draft's
        # own delivery_stops rows — a no-op for every other doctype.
        if doctype == DELIVERY_TRIP:
            risk_flags.extend(_delivery_trip_cancel_risk_flags(doc))
        # Asset Value Adjustment breadth: THE PERMISSION-ONLY BYPASS on the sibling Journal
        # Entry's own cancel (data-driven off the draft's own journal_entry field), plus the
        # depreciation-reschedule sibling-document channel and the raw-write bypass on the
        # linked Asset (both unconditional prose — see
        # _asset_value_adjustment_cancel_risk_flags's own docstring). This is the LEAF cancel
        # path for a submitted Asset Value Adjustment (it is a leaf for its own target scans —
        # nothing links back to it); the per-node cascade loop below carries the same disclosure
        # for the DEPENDENT-node case (a submitted AVA appearing inside an Asset's own cascade
        # graph, via its own "asset" Link — one of Asset's own 18 edges). Doctype-scoped,
        # data-driven — a no-op for every other doctype.
        if doctype == ASSET_VALUE_ADJUSTMENT:
            risk_flags.extend(_asset_value_adjustment_cancel_risk_flags(doc))
        # Payment Order breadth: THE central finding again, cancel direction — the same
        # update_payment_status write mechanism, "Initiated" written unconditionally this
        # direction (see _payment_order_write_risk_flags's own docstring — the Payment Request
        # stomp disclosure). This is the LEAF cancel path, reached only when NO submitted
        # JE/PE/PR dependent exists (Payment Order is genuinely NOT a leaf — the standing
        # blast-radius gate above refuses first whenever one does, routing the cancel through
        # plan_cascade_cancel instead, where the per-node loop below carries the same
        # disclosure for the TARGET node — Payment Order is cancelled LAST in its own graph).
        # Doctype-scoped, data-driven off the draft's own references/payment_order_type fields —
        # a no-op for every other doctype.
        if doctype == PAYMENT_ORDER:
            risk_flags.extend(_payment_order_write_risk_flags(doc, "cancel"))
        # Share Transfer breadth: the cancel-direction mirror + the unguarded
        # remove_shares()/get_shareholder_doc() DoesNotExistError risk (data-dependent, prose
        # only — see _share_transfer_cancel_risk_flags's own docstring). Share Transfer is a
        # genuinely ISOLATED cascade leaf (no other doctype links to it, and its own outgoing
        # Links never point at a submittable doctype this broker cascades through) — this LEAF
        # cancel path is the ONLY cancel path this doctype will ever reach; no
        # plan_cascade_cancel wiring is needed. Doctype-scoped, data-driven off the draft's own
        # transfer_type field — a no-op for every other doctype.
        if doctype == SHARE_TRANSFER:
            risk_flags.extend(_share_transfer_cancel_risk_flags(doc))
        # BOM Creator breadth: THE central finding, cancel direction — this is the LEAF cancel
        # path, reached only when no submitted BOM (built by create_boms()) links back to this
        # creator yet; the blast-radius gate above refuses first once one exists (BOM Creator is
        # NOT self-unlinking — see _bom_creator_cancel_risk_flags's own docstring), routing the
        # cancel through plan_cascade_cancel instead. Doctype-scoped, data-driven off the draft's
        # own status field — a no-op for every other doctype.
        if doctype == BOM_CREATOR:
            risk_flags.extend(_bom_creator_cancel_risk_flags(doc))
        # Budget breadth: THE DISARM finding, cancel direction — Budget is a genuine, permanent
        # cascade LEAF (a full two-checkout grep finds only its own self-referencing
        # amended_from), so this leaf cancel path is the ONLY cancel path this doctype will ever
        # reach; no plan_cascade_cancel wiring is needed. Both enforcement engines filter on
        # docstatus == 1, so cancelling disarms this Budget's own belt immediately (see
        # _budget_cancel_risk_flags's own docstring). Doctype-scoped, data-driven off the draft's
        # own applicable_on_*/account/budget_against fields — a no-op for every other doctype.
        if doctype == BUDGET:
            risk_flags.extend(_budget_cancel_risk_flags(doc))
        # Timesheet breadth: THE SECOND WRITER + status-precedence disclosure, cancel direction —
        # this is the LEAF cancel path, reached only when no submitted Sales Invoice names this
        # Timesheet in its own timesheets child table yet; the blast-radius gate above refuses
        # first once one does (a real, confirmed child-table Link edge — see
        # _timesheet_cancel_risk_flags's own docstring), routing the cancel through
        # plan_cascade_cancel instead. Unconditional prose, not gated on this draft's own fields
        # — a no-op for every other doctype.
        if doctype == TIMESHEET:
            risk_flags.extend(_timesheet_cancel_risk_flags(doc))
        # Contract breadth: THE STATUS/DOCSTATUS DISCONNECT, cancel direction — Contract is a
        # genuine, permanent cascade LEAF (a full two-checkout grep finds only its own
        # self-referencing amended_from), so this leaf cancel path is the ONLY cancel path this
        # doctype will ever reach; no plan_cascade_cancel wiring is needed. Cancelling never
        # touches the status Select (no on_cancel override at all — see
        # _contract_cancel_risk_flags's own docstring), so a cancelled Contract keeps displaying
        # its last computed Active/Inactive/Unsigned status forever. Doctype-scoped, data-driven
        # off the draft's own is_signed field — a no-op for every other doctype.
        if doctype == CONTRACT:
            risk_flags.extend(_contract_cancel_risk_flags(doc))
        # Pick List breadth: THE TWO-JAW TRAP + THE RESERVATION MACHINERY, cancel direction — this
        # is the LEAF cancel path, reached only when no submitted Stock Entry/Delivery Note/Sales
        # Invoice names this Pick List yet (three real, confirmed incoming edges — see
        # _pick_list_cancel_risk_flags's own docstring), routing the cancel through
        # plan_cascade_cancel instead once one does. Doctype-scoped, data-driven off the draft's
        # own locations rows — a no-op for every other doctype.
        if doctype == PICK_LIST:
            risk_flags.extend(_pick_list_cancel_risk_flags(doc))
        # Asset Repair breadth: THE ASSET-STATUS SIDE-WRITE does NOT fire here (cancel skips
        # validate() entirely — see _asset_repair_cancel_risk_flags's own docstring, a correction
        # to the pre-verification addendum), plus the capitalize_repair_cost-gated reversal
        # family and the dangling-Stock-Entry disclosure (the SAME edge the blast-radius gate
        # above already walks — this is the LEAF cancel path, reached only when no submitted
        # Stock Entry names this Asset Repair yet). Doctype-scoped, data-driven off the draft's
        # own capitalize_repair_cost/stock_items fields — a no-op for every other doctype.
        if doctype == ASSET_REPAIR:
            risk_flags.extend(_asset_repair_cancel_risk_flags(doc))
        # Invoice Discounting breadth: the own-hooks reversal family (set_status(cancel=1),
        # update_sales_invoice's conditional is_discounted clear, the GL reversal) plus THE
        # INCOMING JOURNAL ENTRY REACH's own cancel-direction closing half — this document is NOT
        # a cascade leaf (a submitted Journal Entry naming it via the reference_type Select/
        # reference_name Dynamic Link pair is what the blast-radius gate above already refuses a
        # leaf cancel over) — see _invoice_discounting_cancel_risk_flags. Doctype-scoped,
        # data-driven off the draft's own invoices rows — a no-op for every other doctype.
        if doctype == INVOICE_DISCOUNTING:
            risk_flags.extend(_invoice_discounting_cancel_risk_flags(doc))
        # Asset Capitalization breadth: the target-asset cost reversal (symmetric with submit's
        # add — the double-count RED FLAG refutation applies both directions), the depreciation
        # sibling-document-factory reversal (armed off the draft's own asset_items rows), and the
        # repost_future_sle_and_gle async channel, cancel direction — this document is a GENUINE
        # CASCADE LEAF (a full two-checkout grep for '"options": "Asset Capitalization"' finds
        # only its own amended_from) so this is always the leaf cancel path, never routed through
        # plan_cascade_cancel for an incoming edge. See _asset_capitalization_cancel_risk_flags.
        # Doctype-scoped, data-driven off the draft's own asset_items rows — a no-op for every
        # other doctype.
        if doctype == ASSET_CAPITALIZATION:
            risk_flags.extend(_asset_capitalization_cancel_risk_flags(doc))
        # Production Plan breadth: THE CANCEL-ORDERING WRINKLE + the draft Work Order hard-delete
        # + the Bin/Sales-Order-Item reversal + the symmetric Stock Reservation Entry cancel
        # (armed off the draft's own reserve_stock) — this document is GENUINELY NOT A LEAF
        # (Work Order's own direct header Link, plus Purchase Order Item's/Material Request
        # Item's own child-table Links resolving to their submittable parents — see
        # _production_plan_cancel_risk_flags's own docstring), so this leaf path is reached only
        # once no submitted dependent remains; the blast-radius gate above already refuses when
        # one does. Doctype-scoped, data-driven off the draft's own reserve_stock — a no-op for
        # every other doctype.
        if doctype == PRODUCTION_PLAN:
            risk_flags.extend(_production_plan_cancel_risk_flags(doc))
        # The 2026-07-22 debt pass: Work Order's reserve_stock channel, cancel direction — every
        # submitted Stock Reservation Entry against this order auto-cancels under this consent,
        # plus the raw status db_sets — see _work_order_cancel_risk_flags. Data-driven off the
        # draft's own reserve_stock — a no-op otherwise.
        if doctype == WORK_ORDER:
            risk_flags.extend(_work_order_cancel_risk_flags(doc))
        # Subcontracting Order breadth: THE CASCADE CORRECTION (this landing's own refinement,
        # NOT the leaf the dossier/addendum implied) — this document is genuinely NOT A LEAF on
        # TWO independent submittable-referencer families (Stock Entry's own direct header Link,
        # AND Subcontracting Receipt Item's/Supplied Item's child-table Links resolving to
        # submittable Subcontracting Receipt via frappe's own linked_with mechanism — see
        # _subcontracting_order_cancel_risk_flags's own docstring), so this leaf path is reached
        # only once no submitted dependent remains; the blast-radius gate above already refuses
        # when one does. Also discloses THE SEVEN-PATH MUTATOR MAP (cancel direction), the
        # upstream/downstream cross-guards, and the orphan-reservation risk (cancel is NOT
        # symmetric with submit, unlike Production Plan). Doctype-scoped, data-driven off the
        # draft's own reserve_stock — a no-op for every other doctype.
        if doctype == SUBCONTRACTING_ORDER:
            risk_flags.extend(_subcontracting_order_cancel_risk_flags(doc))
        # Subcontracting Inward Order breadth: NOT a cascade leaf on two DIRECT header-level Link
        # families (Work Order, Stock Entry — no child-table resolution needed, simpler than
        # Subcontracting Order's own landing); also discloses THE ELEVEN-ROW MUTATOR MAP (cancel
        # direction), THE NEW BYPASS CLASS, the on_cancel-without-super() correction, and the
        # hard Closed-status gate that runs from Stock Entry's own validate() — see
        # _subcontracting_inward_order_cancel_risk_flags. Doctype-scoped — a no-op for every
        # other doctype.
        if doctype == SUBCONTRACTING_INWARD_ORDER:
            risk_flags.extend(_subcontracting_inward_order_cancel_risk_flags(doc))
        # Subcontracting Receipt breadth: THE ROOF ROW. NOT a cascade leaf — Purchase Receipt.
        # subcontracting_receipt is a real, direct header-level Link (one of this row's own 3
        # cascade edges), discovered by the standing blast-radius gate with zero new cascade.py
        # code; also discloses THE CANCEL BACK-LINK GATE (the ordering wrinkle: this broker's own
        # gate refuses first, in ordinary sequential use; ERPNext's own framework-level check is
        # the second-line, TOCTOU-only safety net), THE SCO WRITEBACK (cancel direction), the
        # SCO-closed upstream throw, and the repost/return/job-card/reservation findings — see
        # _subcontracting_receipt_cancel_risk_flags. Doctype-scoped — a no-op for every other
        # doctype.
        if doctype == SUBCONTRACTING_RECEIPT:
            risk_flags.extend(_subcontracting_receipt_cancel_risk_flags(doc))
        # The 2026-07-22 debt pass: the repost async channel, cancel direction, for the four
        # founding stock rows — UNCONDITIONAL here (docstatus==2 forces it,
        # stock_controller.py:1842-1843): every governed cancel of these doctypes arms ERPNext's
        # own scheduled reposting job. See _stock_repost_channel_flag. A no-op for every other
        # doctype.
        risk_flags.extend(_stock_repost_channel_flag(doctype, "cancel"))
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
            # Invoice Discounting reach retrofit (pre-verification fan, 2026-07-21) — cancel
            # direction of the reference_type Select channel; `doc` already fetched above.
            risk_flags.extend(_journal_entry_invoice_discounting_reach_flags(doc, "cancel"))
        settling_references = client.get_settling_references(doctype, name)
        risk_flags.extend(_settling_reference_risk_flags(settling_references, unlink_settings))
        # Envelope E2: cancelling an update_stock doc reverses its physical stock movement —
        # disclosed from the doc's own items rows, doctype-agnostic by construction. doctype
        # threaded through (POS Invoice breadth) — see _tool_plan_submit's identical comment.
        risk_flags.extend(_update_stock_risk_flags(doc, "cancel", doctype))
        # Envelope E4: is_return / is_pos disclosures, same fields, cancel direction. doctype
        # threaded through (Delivery Note breadth) — see _tool_plan_submit's identical comment.
        risk_flags.extend(_return_risk_flags(doc, doctype))
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
            # Sentinel guard (BOM breadth): a declared-dateless node carries NO_DATE_FIELD, which
            # sorts after every ISO date — without the branch every BOM node in a cascade graph
            # would falsely flag "in the future".
            if node["posting_date"] != NO_DATE_FIELD and node["posting_date"] > self._now_date():
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
            if node["doctype"] == JOURNAL_ENTRY:
                # Invoice Discounting reach retrofit (pre-verification fan, 2026-07-21) — the
                # per-node cascade voice of the same cancel-direction disclosure.
                risk_flags.extend(f"{node['docname']}: {flag}" for flag in
                                  _journal_entry_invoice_discounting_reach_flags(node_doc,
                                                                                 "cancel"))
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
                              for flag in _update_stock_risk_flags(node_doc, "cancel",
                                                                   node["doctype"]))
            # Envelope E4: same is_return / is_pos disclosures, per-node, docname-prefixed —
            # reusing node_doc already fetched above, no extra bench read. node["doctype"] threaded
            # through (Delivery Note breadth) — see _tool_plan_submit's identical comment.
            risk_flags.extend(f"{node['docname']}: {flag}"
                              for flag in _return_risk_flags(node_doc, node["doctype"]))
            risk_flags.extend(f"{node['docname']}: {flag}"
                              for flag in _pos_risk_flags(node_doc, "cancel"))
            # Asset breadth: THIS is the governed cancel path for an Asset (the leaf-node
            # plan_cancel refuses on its auto-created receipt movement from birth) — the same
            # status-gate + multi-document-unwind disclosures the single-op plan carries, made
            # per-node so a graph containing an asset names them against the right docname.
            if node["doctype"] == ASSET:
                risk_flags.extend(f"{node['docname']}: {flag}"
                                  for flag in _asset_cancel_risk_flags(node_doc))
            # Asset Movement breadth: Asset Movement Item's own "asset" field is one of Asset's
            # own 18 Link -> Asset edges, so a submitted Asset Movement DOES appear as a
            # dependent node inside an Asset's own cascade graph (the structural mechanism
            # Asset's own landing named) — the same write-mechanism disclosure the single-op
            # plan carries, made per-node so a graph containing one names it against the right
            # docname.
            if node["doctype"] == ASSET_MOVEMENT:
                risk_flags.extend(f"{node['docname']}: {flag}"
                                  for flag in _asset_movement_write_risk_flags(node_doc, "cancel"))
            # Delivery Trip breadth: THE COMMON case of the corrected structural finding — this
            # broker's own graph ordering ALWAYS cancels the dependent Delivery Note(s) before a
            # Delivery Trip target, which is exactly the condition that makes the cascade fail at
            # THAT NOTE STEP with frappe's own LinkExistsError (see
            # _delivery_trip_cancel_risk_flags's own docstring — supervisor-verified 2026-07-21).
            # UNCHANGED by John's ruling 1 (2026-07-21): the leaf plan_cancel exception does not
            # extend to plan_cascade_cancel (option (b), a cascade-order reorder, was not taken)
            # — this cascade path stays the ONE structurally dead path for this pair; use the leaf
            # plan_cancel instead, which now succeeds as a self-unlinking cancel with the belt and
            # suspenders disclosed. Made per-node so a graph containing one names it against the
            # right docname.
            if node["doctype"] == DELIVERY_TRIP:
                risk_flags.extend(f"{node['docname']}: {flag}"
                                  for flag in _delivery_trip_cancel_risk_flags(node_doc))
            # Asset Value Adjustment breadth: its own "asset" field is one of Asset's own 18
            # Link -> Asset edges (named explicitly in Asset's own cascade enumeration), so a
            # submitted Asset Value Adjustment DOES appear as a dependent node inside an Asset's
            # own cascade graph — the same permission-only-bypass/depreciation-reschedule/
            # raw-write disclosure the single-op leaf plan carries, made per-node so a graph
            # containing one names it against the right docname.
            if node["doctype"] == ASSET_VALUE_ADJUSTMENT:
                risk_flags.extend(f"{node['docname']}: {flag}"
                                  for flag in _asset_value_adjustment_cancel_risk_flags(node_doc))
            # Payment Order breadth: THE OTHER direction from Asset Movement/Asset Value
            # Adjustment above — Payment Order is genuinely NOT a leaf (three real incoming
            # submittable edges: Journal Entry/Payment Entry/Payment Request), so it is the
            # TARGET of its own cascade graph, cancelled LAST (graph[-1]) once its submitted
            # dependents are walked — this is how the write-mechanism disclosure reaches a
            # cascade-cancelled Payment Order at all, since the leaf plan_cancel path is refused
            # outright by the standing blast-radius gate whenever a real dependent exists. Made
            # per-node so a graph containing one names it against the right docname (the same
            # shape every other per-node entry in this loop already follows).
            if node["doctype"] == PAYMENT_ORDER:
                risk_flags.extend(f"{node['docname']}: {flag}"
                                  for flag in _payment_order_write_risk_flags(node_doc, "cancel"))
            # Timesheet breadth: THE SAME direction as Payment Order above — Timesheet is
            # genuinely NOT a leaf (a submitted Sales Invoice naming it in its own timesheets
            # child table is a real, confirmed incoming edge — the campaign's first confirmed
            # INCOMING child-table Link, see _timesheet_cancel_risk_flags's own docstring), so it
            # is the TARGET of its own cascade graph, cancelled LAST once the dependent Sales
            # Invoice is walked first. Made per-node so a graph containing one names it against
            # the right docname.
            if node["doctype"] == TIMESHEET:
                risk_flags.extend(f"{node['docname']}: {flag}"
                                  for flag in _timesheet_cancel_risk_flags(node_doc))
            # Pick List breadth: THE SAME direction as Payment Order/Timesheet above — Pick List
            # is genuinely NOT a leaf on three independent counts (Stock Entry.pick_list, a direct
            # header-level Link on submittable Stock Entry; Delivery Note Item.against_pick_list
            # and Sales Invoice Item.against_pick_list, both real Links on child tables under
            # submittable DN/SI — see _pick_list_cancel_risk_flags's own docstring), so it is the
            # TARGET of its own cascade graph, cancelled LAST once its submitted dependents are
            # walked. Made per-node so a graph containing one names it against the right docname.
            if node["doctype"] == PICK_LIST:
                risk_flags.extend(f"{node['docname']}: {flag}"
                                  for flag in _pick_list_cancel_risk_flags(node_doc))
            # Production Plan breadth: THE SAME direction as Pick List above — Production Plan is
            # genuinely NOT a leaf on three independent counts (Work Order.production_plan, a
            # direct header-level Link on submittable Work Order; Purchase Order Item.
            # production_plan and Material Request Item.production_plan, both real Links on
            # child tables under submittable PO/MR — see _production_plan_cancel_risk_flags's own
            # docstring), so it is the TARGET of its own cascade graph, cancelled LAST once its
            # submitted dependents are walked. Made per-node so a graph containing one names it
            # against the right docname.
            if node["doctype"] == PRODUCTION_PLAN:
                risk_flags.extend(f"{node['docname']}: {flag}"
                                  for flag in _production_plan_cancel_risk_flags(node_doc))
            # Subcontracting Order breadth: THE SAME direction as Production Plan above —
            # Subcontracting Order is genuinely NOT a leaf on TWO independent submittable-
            # referencer families (Stock Entry.subcontracting_order, a direct header-level Link;
            # Subcontracting Receipt Item/Supplied Item.subcontracting_order, child-table Links
            # resolving to submittable Subcontracting Receipt — see
            # _subcontracting_order_cancel_risk_flags's own docstring, THE CASCADE CORRECTION
            # beyond the dossier/addendum), so it is the TARGET of its own cascade graph,
            # cancelled LAST once its submitted dependents are walked. Made per-node so a graph
            # containing one names it against the right docname.
            if node["doctype"] == SUBCONTRACTING_ORDER:
                risk_flags.extend(f"{node['docname']}: {flag}"
                                  for flag in _subcontracting_order_cancel_risk_flags(node_doc))
            # Subcontracting Inward Order breadth: THE SAME direction — genuinely NOT a leaf on
            # two DIRECT header-level Link families (Work Order, Stock Entry — see
            # _subcontracting_inward_order_cancel_risk_flags's own docstring), so it is the
            # TARGET of its own cascade graph, cancelled LAST once its submitted dependents are
            # walked. Made per-node so a graph containing one names it against the right docname.
            if node["doctype"] == SUBCONTRACTING_INWARD_ORDER:
                risk_flags.extend(
                    f"{node['docname']}: {flag}"
                    for flag in _subcontracting_inward_order_cancel_risk_flags(node_doc))
            # Subcontracting Receipt breadth: THE ROOF ROW, THE SAME direction — a real,
            # submittable-referencer edge from Purchase Receipt.subcontracting_receipt (one of
            # this row's own 3 cascade edges), so a submitted Purchase Receipt still referencing
            # this SCR blocks a leaf cancel via the standing blast-radius gate; also discloses
            # THE CANCEL BACK-LINK GATE (+ its ordering wrinkle), THE SCO WRITEBACK, and the
            # repost/return/job-card findings — see
            # _subcontracting_receipt_cancel_risk_flags's own docstring. Made per-node so a graph
            # containing one names it against the right docname.
            if node["doctype"] == SUBCONTRACTING_RECEIPT:
                risk_flags.extend(
                    f"{node['docname']}: {flag}"
                    for flag in _subcontracting_receipt_cancel_risk_flags(node_doc))
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
        # BOM breadth: routed through _locks_for/_posting_date_of — a declared-dateless doctype
        # reads no locks ({}) and check_red_line (in governed_submit, against the plan's own
        # stored sentinel) passes by its explicit branch; an unreadable date on a DATED doctype
        # still denies exactly as before.
        locks = _locks_for(client, doc.get("company"), doctype,
                           _posting_date_of(doc, doctype))  # unreadable → deny
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
            out = {"name": str(result.get("name") or name),
                   "docstatus": int(result.get("docstatus", -1)),
                   "modified": str(result.get("modified") or ""),
                   "doctype": doctype}
            # BOM Creator breadth, John's ruling 2 (two-phase PROVE) — capture the doctype's own
            # `status` field straight off this SAME submit response, never a second network read:
            # before_submit's own set_status() (bom_creator.py:133-142/163-165) already flipped it
            # to "Submitted" inline, synchronously, before on_submit's enqueue_bom_creation() even
            # runs. _QueuedConsequenceEffects.record_outcome (below) folds this into the
            # queued_consequence marker it writes for a confirmed transition. A no-op dict key for
            # every doctype outside ENQUEUE_ON_SUBMIT_DOCTYPES.
            if op == "submit" and doctype in ENQUEUE_ON_SUBMIT_DOCTYPES:
                out["status"] = result.get("status") if isinstance(result, dict) else None
            return out

        def readback():
            # Transport taxonomy (docs/plans/2026-07-07-transport-taxonomy.md): only reached when
            # `execute()` raised something the transport layer could not classify as an answered
            # refusal — the mutating call may already be in motion server-side, so the spine needs
            # the document's REAL docstatus rather than assuming no progress. Same read path as
            # everywhere else in this module (`client.get_document`), never a new surface. May
            # raise (a transient failure reading it back) — `spine.governed_submit` owns degrading
            # that to `readback_error`, never letting it crash this flow.
            return client.get_document(doctype, name).get("docstatus")

        effects = SubmitEffects(store, execute, readback)
        # BOM Creator breadth, John's ruling 2 (two-phase PROVE) — wrap the effects ONLY for a
        # registered doctype's submit; every other call site is byte-identical (a plain
        # SubmitEffects). See _QueuedConsequenceEffects's own docstring for why this lives here,
        # in the doctype-aware glue, rather than as a change to spine.py's shared, doctype-blind
        # core.
        if op == "submit" and doctype in ENQUEUE_ON_SUBMIT_DOCTYPES:
            channel, queue_name = ENQUEUE_ON_SUBMIT_CHANNELS[doctype]
            effects = _QueuedConsequenceEffects(effects, channel=channel, queue_name=queue_name)

        outcome = governed_submit(
            plan=plan, marker=marker, token=token,
            current_doc_version=str(doc.get("modified") or ""),
            now_epoch=self._now_epoch(), now_date=self._now_date(), locks=locks,
            effects=effects, op=op, transition=transition,
        )
        out = {"ok": outcome.ok, "stage": outcome.stage, "reason": outcome.reason}
        if outcome.ok:
            out["result"] = outcome.result
            # Delivery Trip breadth, John's ruling 1 (2026-07-21,
            # docs/plans/2026-07-21-cancel-truth-rulings.md) — THE SUSPENDERS: a registered
            # self-unlinking doctype's leaf cancel just landed (the exception in
            # _tool_plan_cancel's own blast-radius gate let it proceed despite a submitted linked
            # Delivery Note); re-read every note the PRE-CANCEL `doc` fetched above named and
            # attest the self-unlink actually happened. `doc` is the same snapshot every gate
            # above already validated against — never a fresh re-read, so the checklist matches
            # what consent was actually given for. Doctype+op scoped (SELF_UNLINKING_DOCTYPES,
            # cancel only) — a no-op for every other doctype, for submit, and for the SEPARATE
            # plan_cascade_cancel/cascade_cancel execute path (_tool_cascade_cancel), which this
            # ruling leaves untouched. Runs only after outcome.ok — a cancel that did not land
            # gets no readback at all.
            if op == "cancel" and doctype in SELF_UNLINKING_DOCTYPES:
                out["result"]["self_unlink_readback"] = _delivery_trip_self_unlink_readback(
                    client, doc, name)
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
                    "posting_date": _posting_date_of(d, dt),
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
                # node's check_fresh/check_red_line call, never a fresh read. BOM breadth: routed
                # through _locks_for — a declared-dateless node's posting_date is the
                # NO_DATE_FIELD sentinel (set by _cascade_node_meta), which reads no locks;
                # feeding it to get_period_locks would refuse the whole cascade on a non-ISO
                # date for a doctype ERPNext itself never period-checks.
                return _locks_for(client, company, doctype, posting_date)
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
        _, client, store = self._route(args)
        ok, reason = store.verify(expected_head=args.get("expected_head"))
        out = {"ok": ok, "reason": reason, "head": store.head(),
               "count": len(store.receipts())}
        # John's ruling 2 (two-phase PROVE, 2026-07-21) — this sweep is ADDITIVE to the chain
        # verify above, never a reason the verify itself can fail: `ok`/`reason` above describe
        # the LEDGER's own integrity, unaffected by whether a queued consequence has landed yet.
        # Only runs when the chain verified clean — a corrupt chain has nothing trustworthy to
        # sweep. See _sweep_queued_consequences's own docstring.
        out["queued_consequences"] = (
            _sweep_queued_consequences(client, store) if ok
            else {"pending": [], "resolved": [], "skipped": "chain did not verify"})
        return out

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
