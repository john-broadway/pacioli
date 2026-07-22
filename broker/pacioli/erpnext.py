# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Pacioli Broker — ERPNEXT: the REST client (glue, stdlib-only).

Thin, shape-pinned calls to a bench's documented REST surface, over an **injected transport**
(``transport(method, url, headers, params, body) -> (status, json_or_None)``) so every request the
broker can send is unit-tested without a network; a urllib default transport is provided for real
use. The calls become *proven* only against a live bench (SPEC §7) — the tests pin what we send,
the live falsification pins what the bench accepts.

Deliberate shapes (each is a security decision, not a convenience):

* **Submit rides the v1 doc-method surface** — ``POST /api/resource/Sales Invoice/<name>`` with
  ``run_method=submit`` in the **query string**. That is the one submit shape ``guard`` can scope
  narrowly (it classifies as the method ``"Sales Invoice.submit"``); the generic
  ``frappe.client.submit`` RPC takes its target from the request body and cannot be scoped by
  doctype. The query string (not the form body) is used so the classifier's ``form_dict`` read
  always sees it, whatever the body encoding.
* **The broker never sends** ``adv_adj`` **or rewrites** ``posting_date`` — the two levers that
  slip past ERPNext's period locks (the closed-books check, SPEC §3).
* **Doc names are fully URL-quoted** (``safe=""``) — Frappe allows ``/`` in names (naming series),
  and an unquoted slash would silently address a different resource path.
* **Period locks are read, never guessed** (``get_period_locks``): the frozen-till-date boundary
  is read from **both** ``Company.accounts_frozen_till_date`` (the v16 source) and the legacy
  ``Accounts Settings.acc_frozen_upto`` (v15) — the LATER of the two if both carry a value — plus
  the latest submitted Period Closing Voucher. **The Accounting Period check is doctype- and
  date-range-aware (F-S1)** — ``get_period_locks`` takes ``doctype``/``posting_date`` as REQUIRED
  parameters (no default: a default would silently reintroduce doctype-blindness), LISTs the
  periods that CONTAIN ``posting_date`` for this company, then reads each hit's ``disabled`` flag
  and ``closed_documents`` child rows with a second, full-document GET (the list endpoint never
  expands child tables) — refusing only when a period is enabled AND closes *this* doctype,
  matching ERPNext's own enforcement (``accounting_period.py``) instead of the prior
  fail-safe-not-fail-correct shape that refused every doctype past the latest period's end date
  regardless of what that period actually closed. **The LIST itself no longer filters on
  ``disabled`` (F-C1, v15 compatibility)** — that column is v16-only (absent from a v15 bench's
  ``Accounting Period`` schema), and frappe's list-filter builder has no meta-validation or
  sanitizer (``frappe/model/db_query.py::build_filter_conditions``/``prepare_filter_condition``),
  so filtering on a column a v15 bench doesn't have turns every governed op into an "unknown
  column" 500/417 — a v15-wide outage, not a lock. ``disabled`` is read instead from the same
  full-document item GET that already fetches ``closed_documents``: absent (the v15 shape) is
  treated as enabled (v15 has no period-disable concept — that's correct, not a fallback), and a
  truthy ``disabled`` on the full doc (v16) skips the period before its ``closed_documents`` rows
  are even inspected — a disabled period locks nothing, the same F-S1/PHASE-T semantics as before,
  now read from the item GET instead of the LIST filter. See ``get_period_locks``'s own docstring
  for the deny-bias rules and the deliberately-not-modeled ``exempted_role``. An unreadable lock
  source, or a malformed period/child row (including an unparseable ``disabled`` value), **raises**
  — the closed-books check refuses on "can't verify", it never treats unreadable or unparseable as
  unlocked.
* **Amend is a resource CREATE** (``POST /api/resource/Sales Invoice``, the collection URL) —
  ERPNext has no native ``amend()`` server method, so the amended draft is inserted with the
  payload :func:`pacioli.amend.amend_payload` builds (never the raw source document). Honest
  guard implication: ``pacioli_guard``'s resource grants are **not verb-granular**, so the
  Sales Invoice resource grant that already admits reads admits this create too.

**Breadth (Purchase Invoice) — doctype-generic client methods.** The read/plan/execute/amend
calls were confirmed generic from ERPNext source (the doc-method submit/cancel surface, the
resource-CRUD amend shape, and the native ledger-preview RPC all take ``doctype`` as a plain
argument) and generalized accordingly: ``get_document``/``list_documents``/``submit_document``/
``cancel_document``/``get_doc_for_amend``/``find_amendments``/``create_amended_draft`` all take an
explicit ``doctype``; :data:`SUPPORTED_DOCTYPES` is the broker's own "I've been built and tested
for these" allowlist (Sales Invoice, Purchase Invoice), distinct from — and belt-and-suspenders
alongside — ``pacioli_guard``'s per-credential ``resource_doctypes`` grant (SPEC, guard README).
**Honest limit:** every Purchase Invoice request shape here is knowledge-pinned from ERPNext's
documented REST conventions (the same generic surface Sales Invoice already rides, source-read
2026-07-03) — it has **not** been live-verified against a bench; live falsification of the PI path
is a future bench gate, exactly like the rest of this module (SPEC §7).

**Spine fix — the v16 frozen-books gap.** ``get_period_locks`` previously read only ``Accounts
Settings.acc_frozen_upto`` for the frozen-till-date boundary. ERPNext v16 migrated that field onto
``Company.accounts_frozen_till_date`` (``erpnext/patches/v16_0/migrate_account_freezing_settings_
to_company.py`` moves the value; the field is absent from ``accounts_settings.json`` on a v16
bench) — the real enforcement (``general_ledger.check_freezing_date``) reads Company, not Accounts
Settings, so the broker's lock was silently always-absent against a v16 bench. This now reads
**both** sources (Company for v16, Accounts Settings for an unmigrated v15 bench) and honors the
LATER date if both carry a value. **Reading Company is a NEW scope requirement** — an existing
broker credential scoped before this fix needs a new grant (Company DocType read) or every
plan/submit/cancel on that target will raise (an unreadable Company doc is a deny, never a silent
"no lock" — see ``pacioli/doctor.py``'s Company-read probe).

**Breadth (Payment Entry) — a third doctype.** :data:`SUPPORTED_DOCTYPES` gains ``"Payment
Entry"`` (``party_field="party"`` — the header-level field Payment Entry carries for both
Customer and Supplier payments; an Internal Transfer payment carries no party at all, which the
list tier surfaces as an absent field like any other doctype's missing value, same as everywhere
else in this module). Payment Entry's own ``references`` child rows (one per invoice/order/JE it
settles) are read and disclosed at the tool layer (``tools.py``), not here — this client stays as
doctype-blind for Payment Entry as it already was for Sales/Purchase Invoice. ``get_gl_entries``'s
field list now also carries ``against_voucher_type``/``against_voucher`` — for a Payment Entry
cancel a single voucher can touch N invoices at once (unlike SI/PI's one-document cancel radius),
so the projected-reversal rows need to say which invoice each is against.

**Breadth (Journal Entry) — a fourth doctype, and the first with no header-level party.**
:data:`SUPPORTED_DOCTYPES` gains ``"Journal Entry"`` with ``party_field=None`` — confirmed from
``journal_entry.json`` (the ERPNext v16 source checkout): the parent doctype carries no
Customer/Supplier-shaped field at all, only a boolean ``party_not_required`` and per-line
``party``/``party_type`` inside the ``accounts`` child table. ``_list_fields`` (below) treats
``None`` as "omit the party column", the one genuine consumer-side branch this forces
(``list_documents``'s ``party_field`` parameter already accepted any string; passing ``None``
through was the only gap). Journal Entry ALSO carries neither ``status`` nor ``grand_total`` —
confirmed absent from the same JSON — so its list-tier field set is its own branch, not a
one-field patch: ``docstatus`` (frappe's universal field, always present) stands in for
``status``, and ``total_debit``/``total_credit`` (the parent's own balance-check fields, set by
ERPNext's ``set_total_debit_credit`` on every save/validate) stand in for ``grand_total``, plus
``voucher_type`` for context (Bank/Cash/Contra/etc. — the field the JE-specific plan risk flags in
``tools.py`` key off). The read/plan/execute/amend surface itself needs **no** further client
change — ``get_document``/``submit_document``/``cancel_document``/``get_doc_for_amend``/
``find_amendments``/``create_amended_draft``/``get_gl_entries``/``get_active_workflows``/
``apply_workflow`` were already fully doctype-generic (design confirmed from source: the doc-method
submit/cancel surface, the resource-CRUD amend shape, and the native ledger-preview RPC all take
``doctype`` as a plain argument, and ``show_accounting_ledger_preview`` dispatches to
``doc.make_gl_entries()`` polymorphically — ``JournalEntry.make_gl_entries`` matches the call
shape exactly, confirmed by reading ``journal_entry.py``).

:meth:`get_accounts_settings` is new here — a small, doctype-blind read of the site's single
``Accounts Settings`` doctype for whichever fields the caller names. It exists for the Journal
Entry-specific ``plan_cancel`` disclosure in ``tools.py`` (whether
``unlink_payment_on_cancellation_of_invoice`` is on — it changes a cancel's blast radius from "a
generic-link cancel refusal" to "a silent raw-SQL unlink of other submitted JEs/Payment Entries",
scout-je.md §2/§5), but the method itself names no doctype and is reusable by any future disclosure
that needs another Accounts Settings field — unlike ``get_period_locks``, an unreadable read here
is the CALLER's decision whether to refuse or treat as absent (this method itself just raises on
an unreadable bench response, the same as every other read in this client).

**F-R1 — the settling-PE disclosure on cancel, doctype-generic.**
:meth:`get_settling_references` reads **Payment Ledger Entry** (the settlement ledger since
ERPNext v14) filtered on ``against_voucher_type``/``against_voucher_no`` = the target document —
doctype-blind by construction, so it surfaces whatever settles the target (a Payment Entry, most
commonly, since it alone sits in stock ERPNext's ``auto_cancel_exempted_doctypes``, but the read
never assumes that union stays that short). This exists because a cancel of ANY supported doctype
can silently unlink a settling voucher's allocation with no doc event and no separate consent —
ERPNext's own cancel blast-radius read (``get_submitted_linked_docs``) structurally cannot surface
it, since the exempt list removes the settling voucher from that traversal's allowed-source set at
two points (frappe's ``linked_with.py``). GL-entries-shaped (explicit fields/filters,
``limit_page_length: "0"``, F-V1 law) with the same structured-deny-on-non-list-body house pattern
:meth:`get_period_locks` already applies to its Accounting Period LIST read. Raises on an
unreadable response, same as every read in this client — ``tools.py`` is where that becomes a
whole-plan refusal (deny-biased, pin sheet ``docs/plans/2026-07-07-fr1-settling-pe-disclosure.md``).

**Breadth (Sales Order) — a fifth doctype, and the first with no ``posting_date`` field at all.**
:data:`SUPPORTED_DOCTYPES` gains ``"Sales Order"`` (``party_field="customer"``, matching Sales
Invoice's shape; ``submit_via=SUBMIT_VIA_RUN_METHOD``, matching SI/PI/PE — confirmed by reading
``sales_order.py`` (version-16): it overrides neither ``submit()`` nor ``cancel()``, so the
run_method vector never 403s the way it does for Journal Entry). Sales Order carries ``status`` and
``grand_total`` just like Sales Invoice — no ``_list_fields`` branch needed for those — but it is
the FIRST supported doctype confirmed to carry no ``posting_date`` field at all
(``sales_order.json``, 170 fields enumerated, none named ``posting_date``); its own date field is
``transaction_date``. This forces a genuinely new per-doctype config, ``"date_field"``
(:data:`SUPPORTED_DOCTYPES`), read by :func:`_list_fields`/:meth:`list_documents` here and by
``tools.py``'s ``_date_field_for`` at every call site that used to hardcode ``doc.get(
"posting_date")`` for the broker's own closed-books check — never a change to the internal Plan
vocabulary itself (``plan.posting_date`` stays the response/storage key name for every doctype;
only WHICH raw field its value is read from varies). See the full breadth-increment finding, cited
against source, in :data:`SUPPORTED_DOCTYPES`'s own comment block below — including the confirmed-
from-source fact that Sales Order posts NO GL entries of its own on submit (no ``make_gl_entries``
override; inherits ``StockController``'s conditional no-op, and a bare order has no Stock Ledger
Entry rows for ``get_gl_entries`` to find), so ``ledger_preview``'s ``gl_data`` for a Sales Order
plan is empty in the overwhelming common case — the mechanical preview call still succeeds (it
degrades to an empty list rather than raising), it simply has nothing to disclose for a
pre-accounting document that debits and credits nothing itself.

**Breadth (Purchase Order) — a sixth doctype, mechanically identical to Sales Order.**
:data:`SUPPORTED_DOCTYPES` gains ``"Purchase Order"`` (``party_field="supplier"``, matching
Purchase Invoice's shape; ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading
``purchase_order.py`` (version-16): it overrides neither ``submit()`` nor ``cancel()`` either).
Purchase Order carries ``status`` and ``grand_total`` just like Purchase Invoice, and — like Sales
Order before it — carries no ``posting_date`` field at all (``purchase_order.json``, 157 fields
enumerated, none named ``posting_date``); its own date field is ``transaction_date``, so it reuses
the ``"date_field"`` config Sales Order's landing built, no new mechanism required. Confirmed
from-source: Purchase Order posts NO GL entries of its own on submit or cancel (no
``make_gl_entries`` override in ``purchase_order.py``), and its cancel side-effects are entirely
status-updater/bin-qty scatter (Material Request, warehouse bins, Blanket Order, drop-ship
``received_qty``) — never a GL or Stock Ledger reversal; the ``ignore_linked_doctypes`` on cancel
(GL Entry, Payment Ledger Entry, Advance Payment Ledger Entry, Unreconcile Payment, Unreconcile
Payment Entries) keeps settled payments permanently out of the blast radius, unlike Purchase
Invoice. The full cited finding, including the doctypes that carry a ``Link`` field back to
Purchase Order (Purchase Receipt, Purchase Invoice, Sales Order drop-ship, Stock Entry
subcontracting, Subcontracting Order) and why ``cascade.py`` needed no changes to cover them, is in
:data:`SUPPORTED_DOCTYPES`'s own comment block below.

**Breadth (Material Request) — a seventh doctype, and the first with a NEW list-tier field
combination (status present, grand_total absent — Journal Entry has neither, Sales/Purchase Order
have both).** :data:`SUPPORTED_DOCTYPES` gains ``"Material Request"`` (``party_field=None`` — the
header-level ``customer`` field is present but type-gated (``depends_on:
eval:doc.material_request_type=="Customer Provided"``) and forcibly cleared by the controller for
the other 5 of 6 ``material_request_type`` flavors, never a stable counterparty — a stronger case
than Payment Entry's Internal Transfer, which is blank by type choice alone, not additionally
self-clearing on save; ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading
``material_request.py`` (version-16): it overrides neither ``submit()`` nor ``cancel()`` either).
Material Request carries ``status`` but NO ``grand_total`` at all (cost-blind by design — no
accounts child table, no rate/amount fields at the header level) — the first doctype to combine
"has status" with "has no grand_total," so it forces its own ``_list_fields`` branch (neither the
Journal Entry branch, which also drops ``status``, nor the generic branch, which requires
``grand_total``, fits). Like Sales/Purchase Order it carries no ``posting_date`` field at all — its
own date field is ``transaction_date`` — riding the existing ``date_field`` mechanism unchanged,
no new plumbing. Confirmed from-source: Material Request posts NEITHER GL NOR Stock Ledger entries
of its own on submit or cancel (no ``make_gl_entries``/``make_sl_entries``/``GLEntry``/
``StockLedgerEntry`` reference anywhere in ``material_request.py``); its ``on_submit``/
``on_cancel`` are pure status/counter updates (Production Plan ``requested_qty``, warehouse
``Bin.indented_qty`` via ``update_requested_qty``, and — Purchase type only —
``update_prevdoc_status`` refreshing a linked PO/RFQ/SQ). The full cited finding, including the
six-way ``material_request_type`` fulfillment-mode fork (documented, not built into a plan-tier
disclosure this landing — the same treatment owed to Stock Entry's own ``purpose`` polymorphism,
kept mechanical here rather than invented ad hoc), the doctypes carrying a Link field back to
Material Request, and why ``cascade.py`` needed no changes to cover them, is in
:data:`SUPPORTED_DOCTYPES`'s own comment block below.

**Breadth (Delivery Note) — the eighth supported doctype, and the FIRST that is itself
STOCK-PRIMARY (it posts real ledger rows on its own submit, never a pre-accounting/pre-receipt
placeholder like Sales/Purchase Order or Material Request).** Confirmed from source
(``delivery_note.json`` + ``delivery_note.py``, version-16 checkout, both read 2026-07-20/21;
dossier at ``docs/plans/dossiers/delivery_note.md``, re-verified against source rather than trusted
as pinned):

  * ``customer`` (Link -> Customer, ``reqd: 1``, lines 189-201) IS the header-level party field —
    ``party_field="customer"``, the SI/SO shape.
  * ``status`` (Select, ``reqd: 1``, lines 1062-1078: Draft/To Bill/Partially Billed/Completed/
    Return/Return Issued/Cancelled/Closed) and ``grand_total`` (Currency, lines 810-821) ARE BOTH
    present — the SI/SO/PO generic branch, no ``_list_fields`` change needed.
  * ``is_submittable: 1`` (line 1462) — a real docstatus 0/1/2 lifecycle.
  * **``date_field="posting_date"`` — Delivery Note is the FIRST stock-primary doctype confirmed to
    carry a real ``posting_date`` field** (Date, ``reqd: 1``, ``default: "Today"``, lines 246-258),
    unlike Sales Order/Purchase Order/Material Request (all ``transaction_date`` only). This
    doctype therefore exercises the plain, pre-existing DEFAULT path of :func:`_date_field_for` /
    ``SUPPORTED_DOCTYPES.get(doctype, {}).get("date_field", "posting_date")`` — the same field SI/
    PI/PE/JE already use — never the ``transaction_date`` branch those three doctypes built. The
    entry below still names ``"date_field": "posting_date"`` explicitly (never omitted), matching
    every other row's shape, even though it equals the default.
  * ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading ``delivery_note.py`` (version-16):
    no ``def submit``/``def cancel`` override anywhere in the file, only ``on_submit``/``on_cancel``
    hooks called by the base ``Document.submit()``/``.cancel()`` — the run_method vector never 403s
    for Delivery Note, the same finding shape as SO/PO/MR.
  * **Delivery Note posts BOTH ledgers — Stock Ledger Entry ALWAYS, GL Entry conditionally.** This
    is the corrected Wave-1 finding (the plan's own header note): "SL not GL" was wrong at the
    source level. ``on_submit`` (delivery_note.py:447-479) calls, in order,
    ``self.update_stock_ledger()`` (line 477 — inherited from ``SellingController.
    update_stock_ledger``, selling_controller.py:661, an UNCONDITIONAL SLE write, no perpetual-
    inventory gate) THEN ``self.make_gl_entries()`` (line 478 — the base
    ``StockController.make_gl_entries``, stock_controller.py:292-319, never overridden by Delivery
    Note or SellingController). That base method's own gate,
    ``need_inventory_map = (self.get_stock_items() or self.get("packed_items")) and cint(erpnext.
    is_perpetual_inventory_enabled(self.company))`` (stock_controller.py:303-305), is the SAME
    conditional no-op Sales/Purchase Order's landing found — the DIFFERENCE is what feeds it:
    ``get_gl_entries`` sources its rows from the voucher's OWN Stock Ledger Entry rows
    (``get_stock_ledger_details``), which a bare Sales/Purchase Order never has, but Delivery Note
    ALWAYS does (``update_stock_ledger()`` just ran, unconditionally, one line above) — so for any
    Delivery Note carrying stock items, with perpetual inventory enabled, ``make_gl_entries()``
    posts REAL GL rows (Inventory account debit / COGS credit, per-line tax rows if configured).
    ``on_cancel`` (481-508) mirrors this exactly: ``update_stock_ledger()`` (494, reverse SLE) then
    ``make_gl_entries_on_cancel()`` (499 -> ``stock_controller.py:1429-1437``, which calls
    ``make_reverse_gl_entries`` when ``docstatus==2`` and only if live GL rows exist for this
    voucher) then ``repost_future_sle_and_gle()`` (500).
  * **``ledger_preview`` (the native dry-run this broker's ``plan_submit``/``plan_cancel`` already
    calls, unchanged) returns REAL, non-empty ``gl_data`` for Delivery Note — unlike Sales Order/
    Purchase Order/Material Request, whose ``projected_gl`` is near-always empty by the document's
    own pre-accounting nature.** Confirmed at the source of the preview RPC itself:
    ``get_accounting_ledger_preview`` (stock_controller.py:2090-2119) does, BEFORE calling
    ``doc.make_gl_entries()`` in-memory: ``if doc.get("update_stock") or doc.doctype in ("Purchase
    Receipt", "Delivery Note", "Stock Entry"): doc.update_stock_ledger()`` (lines 2109-2110) —
    Delivery Note is EXPLICITLY named in that whitelist, so the preview seeds real (savepoint-only,
    rolled back after) Stock Ledger Entry rows before asking for GL, closing exactly the gap that
    leaves Sales/Purchase Order's preview empty. **Governance note for the tool layer (tools.py):
    the FakeClient test double's ``ledger_preview`` is a fixed, doctype-blind stub that ALWAYS
    returns one non-empty row regardless of doctype — no existing test anywhere in the suite
    asserts an EMPTY ``projected_gl`` for Sales Order/Purchase Order/Material Request (verified by
    grepping every ``projected_gl``/``gl_data`` assertion in the test tree), so this landing breaks
    no prior assumption. A new, real test (``TestDeliveryNoteLedgerDisclosure``) instead PINS the
    positive claim — that Delivery Note's ``plan_submit`` surfaces non-empty ``projected_gl`` — so
    a future regression that silently special-cased DN into an empty preview would fail loudly.**
  * **Cancel-refusal semantics — a genuine, PARTIAL disclosure gap, sharper than any prior
    doctype's.** ``check_next_docstatus`` (delivery_note.py:602-619) refuses the cancel outright if
    EITHER a submitted Sales Invoice (via ``Sales Invoice Item.delivery_note``) OR a submitted
    Installation Note (via ``Installation Note Item.prevdoc_docname``) references this Delivery
    Note. This broker's own blast-radius disclosure (``get_submitted_linked_docs``, the refusal
    ``_tool_plan_cancel`` already makes on ANY non-empty result) walks ERPNext's live Link-field
    graph — and the SALES INVOICE half of this refusal IS covered by it: ``sales_invoice_item.json``
    confirms ``delivery_note`` is a real ``Link`` field (``"options": "Delivery Note"``), so a
    submitted Sales Invoice referencing this DN is discovered and surfaces in the disclosure before
    a cancel is even planned. **The INSTALLATION NOTE half is NOT covered — confirmed by reading
    ``installation_note_item.json``: ``prevdoc_docname`` (the field ``check_next_docstatus`` joins
    on) is a plain ``Data`` field, not a ``Link``** — ERPNext's own generic linked-docs walker has
    no schema edge to find at all for this back-reference, at ANY docstatus, so a submitted
    Installation Note referencing this Delivery Note is structurally invisible to
    ``plan_cancel``'s disclosure — not merely a draft-dependent blind spot (the shape every SO/PO/MR
    gap already has), but a genuine hole in the generic mechanism for this one edge. No new broker
    gate is built for this (the answered ERPNext refusal still surfaces safely at the actual cancel
    call, through the same generic exception handling every other doctype's cancel already relies
    on) — but the disclosure at PLAN time is honestly incomplete for this one link, and that is
    recorded here rather than silently claimed covered.
  * **A second, ORDINARY disclosure gap, the same shape SO/PO/MR already have:**
    ``check_sales_order_on_hold_or_close("against_sales_order")`` (line 484, delegating to
    ``SellingController.check_sales_order_on_hold_or_close``, selling_controller.py:472-474 ->
    ``StockController.check_for_on_hold_or_closed_status("Sales Order", ...)``,
    stock_controller.py:2021+) refuses the cancel if a referenced Sales Order's OWN ``status`` is
    On Hold/Closed — a status read on a DIFFERENT document, not the submitted-docstatus graph
    ``get_submitted_linked_docs`` walks, so ``plan_cancel``'s disclosure cannot preview it either.
    No new broker code needed — same treatment as Purchase Order's MR-status check and Material
    Request's own-status check.
  * **Auto-created documents — documentation suffices, no JE-style special-casing.**
    (a) ``make_return_invoice`` (delivery_note.py:656-671), called from ``on_submit`` only when
    ``is_return`` AND ``issue_credit_note`` are both truthy, creates AND SUBMITS a separate Sales
    Invoice (``is_return=True``) as the actual credit note. (b) ``delete_auto_created_batches``
    (inherited, ``stock_controller.py:993+``), called unconditionally from ``on_cancel`` (line
    508), clears each item row's ``batch_no``/``serial_and_batch_bundle`` reference and marks the
    referenced Serial and Batch Bundle doc + its Serial and Batch Entry rows ``is_cancelled=1`` (a
    soft-cancel/unlink, not a hard delete). Neither is special-cased with new broker machinery, and
    the reasoning differs from Journal Entry's exchange-gain case on purpose: JE's bypass is a
    violation of THIS PROJECT'S OWN founding law (a debit==credit check ERPNext itself skips for
    that voucher_type) and earned a real refusal + an independent balance check; Delivery Note's
    auto-created Sales Invoice is an ordinary, BALANCED, transparently-readable posting (it shows up
    on the next ``get_sales_invoice``/``list_sales_invoices`` call exactly like any other document,
    and it is itself submitted through ERPNext's own governed lifecycle), and the batch cleanup is
    housekeeping with no ledger effect at all — documentation here is the correct, honest level of
    disclosure, not an under-build.
  * **A real code fix, not just documentation: the shared ``is_return`` disclosure
    (:func:`pacioli.tools._return_risk_flags`) previously assumed every ``is_return`` doctype also
    carries ``update_outstanding_for_self`` (true for Sales Invoice and Purchase Invoice, the only
    two doctypes that had ``is_return`` before this landing) and would have emitted a FALSE claim —
    "the original's outstanding is reduced by this reversal" — for Delivery Note, which carries
    ``is_return``/``return_against`` (confirmed present) but has NO ``update_outstanding_for_self``
    field at all (confirmed absent from ``delivery_note.json``'s full field list) and NEVER posts
    to a receivable/payable account of its own (Inventory/COGS GL only, per the ledger finding
    above — Delivery Note has no "outstanding" concept to reduce). ``tools.py`` now threads
    ``doctype`` into that function and keys the branch on FIELD PRESENCE
    (``"update_outstanding_for_self" in doc``, not mere truthiness — a real bench document simply
    omits a field its doctype's schema doesn't carry) rather than assuming every ``is_return``
    doctype shares Sales/Purchase Invoice's settlement shape. See ``TestDeliveryNoteReturnDisclosure``.
  * **Cascade edges — ``cascade.py`` needed NO changes** (it carries zero doctype-specific string
    literals — confirmed by grep of the module — the same finding as every prior breadth increment).
    Doctypes carrying a real ``Link`` field to Delivery Note (confirmed by grepping ``'"options":
    "Delivery Note"'`` across the v16 checkout and reading each field's ``fieldtype``, discarding
    any non-``Link`` hit): Purchase Receipt (``inter_company_reference``), Stock Entry
    (``delivery_note_no``), Shipment Delivery Note (``delivery_note``), Packing Slip
    (``delivery_note``), Delivery Stop (``delivery_note``), Sales Invoice Item (``delivery_note``),
    POS Invoice Item (``delivery_note``) — plus Delivery Note's own self-links (``amended_from``,
    ``return_against``). ERPNext's own ``get_submitted_linked_docs`` walks this Link-field graph
    generically (child tables included), so a submitted dependent referencing this Delivery Note is
    discovered and ordered ahead of it in cancel order with zero doctype-specific cascade code —
    same as every other supported doctype. (Installation Note Item is deliberately NOT in this
    list — see the cancel-refusal finding above: its back-reference is a ``Data`` field, not a real
    ``Link``, so it was never part of this walk to begin with, confirmed by reading its JSON, not
    merely absent from the grep by coincidence.)

**Breadth (Purchase Receipt) — the ninth supported doctype, and the SECOND STOCK-PRIMARY row (with
Delivery Note; it posts real Stock Ledger + conditional GL rows on its own submit — the received-
goods mirror of Delivery Note's shipped-goods shape).** Confirmed from source
(``purchase_receipt.json`` + ``purchase_receipt.py``, version-16 checkout, both read 2026-07-21;
dossier at ``docs/plans/dossiers/purchase_receipt.md``, re-verified against source rather than
trusted as pinned):

  * ``supplier`` (Link -> Supplier, ``reqd: 1``, lines 186-200) IS the header-level party field —
    ``party_field="supplier"``, the PI/PO shape.
  * ``status`` (Select, ``reqd: 1``, lines 873-889: Draft/Partly Billed/To Bill/Completed/Return/
    Return Issued/Cancelled/Closed — 8 values, not 6, re-counted directly from the ``options``
    string rather than trusting the dossier's prose count) and ``grand_total`` (Currency, lines
    811-820) ARE BOTH present — the SI/SO/PO/DN generic branch, no ``_list_fields`` change needed.
  * ``is_submittable: 1`` (line 1302) — a real docstatus 0/1/2 lifecycle.
  * ``posting_date`` (Date, ``reqd: 1``, ``default: "Today"``, lines 223-236) IS present —
    ``date_field="posting_date"``, the same DEFAULT path Delivery Note already rides (never the
    ``transaction_date`` branch SO/PO/MR built). The entry below still names it explicitly, matching
    every other row's shape.
  * ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading ``purchase_receipt.py``
    (version-16): no ``def submit``/``def cancel`` override anywhere in the file, only
    ``on_submit``/``on_cancel``/``before_cancel`` HOOKS called by the base
    ``Document.submit()``/``.cancel()`` — the run_method vector never 403s for Purchase Receipt.
  * **Purchase Receipt posts BOTH ledgers — Stock Ledger Entry ALWAYS, GL Entry conditionally —
    the identical shape Delivery Note's landing found, mirrored for the receiving side.**
    ``on_submit`` (purchase_receipt.py:375-399) calls, in order,
    ``self.make_bundle_for_sales_purchase_return()`` (389),
    ``self.make_bundle_using_old_serial_batch_fields()`` (390), THEN
    ``self.update_stock_ledger()`` (394 — inherited, an UNCONDITIONAL SLE write, no
    perpetual-inventory gate — the comment directly above the call in source even says so:
    "Updating stock ledger should always be called after updating prevdoc status") THEN
    ``self.make_gl_entries()`` (395 — the base ``StockController.make_gl_entries``,
    stock_controller.py:292-319, never overridden by Purchase Receipt). Exactly like Delivery Note,
    ``get_gl_entries`` (purchase_receipt.py:486-495, an OVERRIDE here, unlike Delivery Note which
    uses the base — Purchase Receipt's own version adds ``make_item_gl_entries``/
    ``make_tax_gl_entries``/``set_gl_entry_for_purchase_expense``/regional entries, but the
    triggering GATE is the same inherited ``StockController.make_gl_entries`` conditional) sources
    its rows from the voucher's OWN Stock Ledger Entry rows, which a Purchase Receipt ALWAYS has
    (``update_stock_ledger()`` just ran, unconditionally, one line above) — so for any Purchase
    Receipt carrying stock items, with perpetual inventory enabled, ``make_gl_entries()`` posts REAL
    GL rows (Stock/Asset account debit / Stock Received But Not Billed credit, per dossier §6).
    ``on_cancel`` (446-476) mirrors this: ``update_stock_ledger()`` (465, reverse SLE) then
    ``make_gl_entries_on_cancel()`` (466) then ``repost_future_sle_and_gle()`` (467).
  * **``ledger_preview`` returns REAL, non-empty ``gl_data`` for Purchase Receipt — confirmed at
    the SAME source line Delivery Note's landing cited.** ``get_accounting_ledger_preview``
    (stock_controller.py:2109-2110) reads: ``if doc.get("update_stock") or doc.doctype in
    ("Purchase Receipt", "Delivery Note", "Stock Entry"): doc.update_stock_ledger()`` — Purchase
    Receipt is the FIRST-NAMED doctype in that literal whitelist (Delivery Note's own citation
    already quoted this same line; re-cited here because it is Purchase Receipt's own governing
    line, not borrowed). The same governance note applies: the FakeClient test double's
    ``ledger_preview`` stub is fixed and doctype-blind, so ``TestPurchaseReceiptLedgerDisclosure``
    pins the positive claim directly, the same discipline ``TestDeliveryNoteLedgerDisclosure`` set.
  * **Cancel-refusal semantics — ONE named doctype, and (unlike Delivery Note) FULLY covered by
    this broker's blast-radius disclosure — a genuine divergence from Delivery Note's partial
    gap.** ``check_next_docstatus``/``on_cancel`` (purchase_receipt.py:436-444, 451-458 — the
    identical query duplicated in both places, confirmed by reading both) refuses the cancel
    outright if a submitted Purchase Invoice references this Purchase Receipt (joined on
    ``Purchase Invoice Item.purchase_receipt = self.name AND docstatus = 1``). Confirmed as a real
    ``Link`` field by reading ``purchase_invoice_item.json`` line 703-709
    (``"fieldtype": "Link", "options": "Purchase Receipt"``) — so ``get_submitted_linked_docs``
    (this broker's own blast-radius walk, the refusal ``_tool_plan_cancel`` already makes on ANY
    non-empty result) discovers a submitted Purchase Invoice referencing this receipt BEFORE a
    cancel is even planned. Purchase Receipt names only ONE doctype in its own cancel-refusal
    query (unlike Delivery Note's two), and that one doctype IS the Link-covered kind — so, unlike
    Delivery Note, Purchase Receipt's ``plan_cancel`` disclosure has NO equivalent Installation-
    Note-shaped blind spot for this particular refusal.
  * **A genuinely NEW disclosure gap Delivery Note did not have: the on-hold/closed check fires on
    SUBMIT too, not only cancel.** ``check_for_on_hold_or_closed_status("Purchase Order",
    "purchase_order")`` is called from ``validate()`` (purchase_receipt.py:264 — validate runs on
    every save AND is invoked by ERPNext's own submit path before ``on_submit``, so this check can
    refuse a **submit**, not merely a cancel) AND separately from ``on_cancel`` (line 449, the same
    call, again). This is a STATUS read on a different document (the source Purchase Order), not
    the submitted-docstatus graph ``get_submitted_linked_docs`` walks, so it is invisible to
    ``plan_submit``'s AND ``plan_cancel``'s disclosure alike — a WIDER version of the ordinary
    SO/PO/MR/DN on-hold-check gap (those only fired on cancel; this one fires on both verbs). No
    new broker gate is built for this: the answered ERPNext refusal still surfaces safely at the
    real submit/cancel call either way, through the same generic exception handling every other
    doctype's tools already rely on — but the disclosure gap is WIDER here and is recorded honestly
    as such, not understated to match the narrower cancel-only shape of prior doctypes.
  * **Auto-created documents and an orphan hazard — documentation suffices, no JE-style special-
    casing, mirroring Delivery Note's treatment.** (a) ``make_bundle_for_sales_purchase_return``
    and ``make_bundle_using_old_serial_batch_fields`` (both inherited from ``StockController``,
    called from ``on_submit`` lines 389-390) create Serial and Batch Bundle rows; (b)
    ``delete_auto_created_batches`` (inherited, called unconditionally from ``on_cancel`` line 474)
    soft-cancels them, the identical mechanism Delivery Note's own landing already documented. (c)
    ``before_cancel``/``remove_amount_difference_with_purchase_invoice`` (478-484) clears each
    item's ``amount_difference_with_purchase_invoice`` field — housekeeping, no ledger effect. (d)
    **A genuine orphan hazard, confirmed by field-type inspection, that is DIFFERENT IN KIND from
    the cancel-refusal gap above:** Landed Cost Voucher distributes freight/customs costs onto a
    Purchase Receipt via its child table, ``Landed Cost Purchase Receipt.receipt_document``
    (``landed_cost_purchase_receipt.json`` lines 25-36) — a **Dynamic Link**
    (``"fieldtype": "Dynamic Link", "options": "receipt_document_type"``), not a plain ``Link``.
    **CORRECTION (Landed Cost Voucher's own landing, 2026-07-21):** this comment originally claimed
    the Dynamic Link has "no edge to find it at all" — that was WRONG. Source-tracing frappe's
    ``get_references_across_doctypes_by_dynamic_link_field`` (``linked_with.py``) proves it is a
    LIVE distinct-value query, not a static schema scan, so a child-table Dynamic Link IS
    discoverable by ``get_submitted_linked_docs`` (the same mechanism Dunning's Payment Entry
    Reference landing later confirmed). So cancelling a Purchase Receipt that carries a submitted
    LCV against it is ALREADY blocked by this broker's blast-radius refusal — MORE protective than
    raw ERPNext, whose own ``on_cancel`` never checks for a linked LCV at all. Not an orphan hazard
    after all; the edge is seen. (Kept as a documented correction, not silently rewritten, per the
    anti-drift rule: never let a disproven claim stand.)
  * **``update_outstanding_for_self`` is confirmed ABSENT** from ``purchase_receipt.json``'s full
    148-field list, the same finding shape Delivery Note's landing made — Purchase Receipt is a
    STOCK-ONLY return doctype (Inventory/Asset GL only, no receivable/payable balance of its own).
    :func:`pacioli.tools._return_risk_flags` already keys its settlement branch on FIELD PRESENCE
    (not truthiness), a fix that landed with Delivery Note and needs NO further code change here —
    Purchase Receipt exercises the identical ``"update_outstanding_for_self" not in doc`` branch
    that fix built, confirmed live by ``TestPurchaseReceiptReturnDisclosure`` rather than merely
    assumed to still work.
  * **Cascade edges — ``cascade.py`` needed NO changes** (it carries zero doctype-specific string
    literals — confirmed by re-reading the module — the same finding as every prior breadth
    increment). Doctypes carrying a real ``Link`` field to Purchase Receipt (confirmed by grepping
    ``'"options": "Purchase Receipt"'`` across the v16 checkout and reading each field's
    ``fieldtype``, discarding the one Dynamic Link hit above): Stock Entry
    (``purchase_receipt_no``), Stock Entry Detail (``reference_purchase_receipt`` — NOTE: the
    dossier's link table names this field ``reference_purchase_receipt`` for Stock Entry itself;
    that is actually Stock Entry **Detail**'s field name — Stock Entry's own parent-level field is
    ``purchase_receipt_no``, a divergence from the dossier corrected here after reading both JSONs
    directly), Delivery Note (``inter_company_reference``), Asset (``purchase_receipt``), Purchase
    Invoice Item (``purchase_receipt``) — plus Purchase Receipt's own self-links (``amended_from``,
    ``return_against``). ERPNext's own ``get_submitted_linked_docs`` walks this Link-field graph
    generically (child tables included), so a submitted dependent referencing this Purchase Receipt
    is discovered and ordered ahead of it in cancel order with zero doctype-specific cascade code —
    same as every other supported doctype. (Landed Cost Purchase Receipt is deliberately NOT in
    this list — see the orphan-hazard finding above: its back-reference is a Dynamic Link, not a
    real Link, so it was never part of this walk to begin with.)

**Breadth (Stock Entry) — the tenth supported doctype, the LAST of Wave 1, and the hardest: a
THIRD stock-primary row, the FIRST genuinely polymorphic one, and the FIRST whose party field is
client-JS-gated rather than server-enforced.** Confirmed from source (``stock_entry.json`` +
``stock_entry.py``, version-16 checkout, both read 2026-07-21; dossier at
``docs/plans/dossiers/stock_entry.md``, re-verified against a fresh checkout — the full source-cited
finding, including the purpose-to-cascade map and every divergence from the dossier's own prose, is
in :data:`SUPPORTED_DOCTYPES`'s own comment block below, which this section summarizes):

  * ``party_field=None`` — ``supplier`` is present in schema (Link -> Supplier) but carries no
    ``reqd`` and its ``depends_on`` is a CLIENT-SIDE JS eval only (``stock_entry.js:1657-1659``),
    never read or cleared anywhere in ``stock_entry.py`` (zero hits) — a WEAKER basis for
    ``None`` than Material Request's own server-enforced clearing, recorded honestly as a
    divergence rather than claimed to match MR's citation strength. No ``customer`` field exists.
  * ``status`` and ``grand_total`` are BOTH confirmed ABSENT (87 fields enumerated) — a FOURTH
    ``_list_fields`` branch, the first to combine status-absent + grand_total-absent +
    party_field=None all at once. ``purpose`` (13 options, counted by splitting the field's own
    ``options`` string in code and cross-checked against ``validate_purpose``'s own list, not
    trusted from the dossier's prose) rides as the context column (Material Request's own
    precedent for this shape); ``total_incoming_value``/``total_outgoing_value``/
    ``value_difference`` stand in for ``grand_total``.
  * ``date_field="posting_date"`` — a real field, confirmed present, the same default path
    Delivery Note/Purchase Receipt already ride. ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed
    by reading all 4875 lines of ``stock_entry.py``: no ``def submit``/``def cancel`` override.
  * **Posts BOTH ledgers.** Stock Entry overrides ``update_stock_ledger()`` itself as an
    UNCONDITIONAL SLE write (no perpetual-inventory gate); ``make_gl_entries`` is inherited
    unmodified from ``StockController`` (only ``get_gl_entries`` is overridden, the additional-
    costs distribution). ``ledger_preview`` returns REAL non-empty ``gl_data`` — Stock Entry is
    the THIRD-named doctype in the same ``stock_controller.py:2109-2110`` whitelist Delivery
    Note's and Purchase Receipt's own landings cited, closing the set all three stock-primary
    doctypes now occupy. New positive-pin test: ``TestStockEntryLedgerDisclosure``.
  * **``is_return`` is present but ``return_against`` is CONFIRMED ABSENT** — a THIRD is_return
    shape. Traced to its one real producer (``work_order.py``'s ``make_stock_return_entry``): a
    raw-material-direction flag on an ordinary Work Order transfer, not a credit-note concept.
    Because ``return_against`` can never be present, :func:`pacioli.tools._return_risk_flags`'s
    settlement branch never fires — no code change needed, no false claim is possible; only the
    top-line RETURN + FREE-STANDING flags apply. The top-line "credit note... sale/purchase"
    wording is imprecise for a pure inventory redirection — documented as a known imprecision,
    deliberately not reworded without a fourth real-world shape to design the fix against.
  * **Purpose-to-cascade map** (documented, deliberately NOT built into a plan-tier disclosure
    column this landing — the OWED treatment the dossier itself names, matching Material Request's
    own precedent): Manufacture/Material-Transfer-for-Manufacture/Material-Consumption-for-
    Manufacture/Disassemble touch Work Order (with a "Stopped"-status refusal firing on BOTH
    submit and cancel — a WIDER gap than the ordinary cancel-only shape, invisible to either
    disclosure); Send to Subcontractor/Material Transfer (with a Subcontracting Order set) touch
    Subcontracting Order (with an On-Hold/Closed refusal ALSO firing on both submit and cancel, via
    ``validate()`` itself, the widest gap of the ten doctypes landed so far); inspection-bearing
    rows touch Quality Inspection (housekeeping only); Manufacture/Repack with a cost-tracking Work
    Order touch Project (cost allocation only); Material Transfer with ``add_to_transit`` touches
    Material Request's own ``transfer_status`` — corrected from the dossier's mistaken citation of
    ``update_transferred_qty``, which is actually a SELF-referential Goods-In-Transit tracker
    between paired Stock Entries, not a Material Request touch at all. One genuine dead-code
    finding: ``delete_linked_stock_entry``'s hard-delete branch is gated on purpose values
    ("Send to Warehouse"/"Receive at Warehouse") absent from the live 13-value ``valid_purposes``
    list — unreachable under current validation, not a live orphan-delete hazard.
  * **No downstream-submitted-document REFUSAL exists on Stock Entry's own cancel at all** — a
    genuine divergence from Delivery Note's/Purchase Receipt's own ``check_next_docstatus``-shaped
    methods. Stock Entry's ``on_cancel`` PUSHES updates into other doctypes rather than PULLING a
    refusal from one; this broker's blast-radius disclosure still runs unconditionally, but has
    very little to find for this doctype, by ERPNext's own design, not by a disclosure gap.
  * **Cascade edges — ``cascade.py`` needed NO changes.** Only ONE external doctype carries a real
    Link to Stock Entry (Journal Entry's own ``stock_entry`` field, Credit/Debit Note vouchers
    only) — every other doctype named in the purpose-to-cascade map above is a target Stock Entry
    writes INTO, never a source carrying a Link back.

**Breadth (Supplier Quotation) — the eleventh supported doctype, the FIRST of Wave 2
(docs/plans/2026-07-20-breadth-campaign-past-120.md), and the FIRST whose SUPPORTED_DOCTYPES
entry is byte-for-byte IDENTICAL to an already-landed doctype's, not merely shape-matched.**
Confirmed from source (``supplier_quotation.json`` + ``supplier_quotation.py``, version-16
checkout, both read 2026-07-21; dossier at ``docs/plans/dossiers/supplier_quotation.md``):

  * ``party_field="supplier"`` (Link -> Supplier, ``reqd``, lines 150-162) — the same fieldname
    and the same required-Link shape Purchase Order already carries.
  * ``status`` (Select, 5 options: Draft/Submitted/Stopped/Cancelled/Expired, lines 763-774) and
    ``grand_total`` (Currency, lines 633-641) are BOTH present — the generic ``_list_fields``
    branch, no new branch needed (the same shape SI/PI/PE/SO/PO/DN/PR already ride).
  * ``date_field="transaction_date"`` (lines 180-189, ``default: "Today"``) — **Supplier
    Quotation carries NO ``posting_date`` field at all**, confirmed absent across every field
    enumerated in ``supplier_quotation.json`` — the fourth doctype on this pattern (with Sales
    Order/Purchase Order/Material Request).
  * ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 362 lines of
    ``supplier_quotation.py`` (version-16): no ``def submit``/``def cancel`` override anywhere,
    only ``on_submit``/``on_cancel`` hooks.
  * **Supplier Quotation posts NO GL entries and NO Stock Ledger entries, on submit or cancel** —
    confirmed by grep of ``supplier_quotation.py``: zero hits for ``make_gl_entries``,
    ``make_sl_entries``, ``GLEntry``, or ``StockLedgerEntry`` anywhere in the file. It is a
    pre-receipt quotation document, mechanically identical to Purchase Order on this axis.
  * **This SUPPORTED_DOCTYPES entry equals Purchase Order's, field for field** —
    ``{"party_field": "supplier", "submit_via": SUBMIT_VIA_RUN_METHOD, "date_field":
    "transaction_date"}`` on both rows, asserted directly by this landing's tests
    (``SUPPORTED_DOCTYPES[SUPPLIER_QUOTATION] == SUPPORTED_DOCTYPES[PURCHASE_ORDER]``). Every
    prior "mechanically identical" pairing (e.g. Purchase Order vs. Sales Order) still differed on
    at least one axis (there, ``party_field``); this is the first pairing with a genuinely empty
    diff.
  * ``on_submit`` (supplier_quotation.py:130-132): ``db_set("status", "Submitted")`` then
    ``update_rfq_supplier_status(1)``. ``on_cancel`` (134-136): ``db_set("status", "Cancelled")``
    then ``update_rfq_supplier_status(0)``. **The doctype's one real side-effect is a status write
    on a DIFFERENT document** — ``Request for Quotation Supplier.quote_status``, reached by
    walking each submitted item's ``request_for_quotation`` link (method body: lines 171-225) —
    never a docstatus change on the RFQ itself and never a cascade. The ``include_me`` flag
    (``1`` on submit, counting this SQ's own items toward "Received"; ``0`` on cancel, excluding
    them so the status can revert to "Pending" if this was the RFQ's only quote) is the exact
    reversal shape a governed submit/cancel pair should have. Mechanically the same CLASS of
    side-effect Purchase Order's own ``update_prevdoc_status()`` and Material Request's own
    status-updater calls already exercise (a write into a sibling document's status field, never a
    GL/SL post, never a cascade) — only the target doctype (Request for Quotation, not MR/PO) and
    the method name differ.
  * **Cascade edges — ``cascade.py`` needed NO changes**, same finding as every prior doctype.
    Doctypes carrying a real ``Link`` field to Supplier Quotation (confirmed by grepping
    ``'"options": "Supplier Quotation"'`` across the v16 checkout): Purchase Order's own
    ``ref_sq`` field (purchase_order.json line 914 — **NOT** named ``supplier_quotation``, a
    correction against the dossier's own prose, which cited the field by its label rather than
    its fieldname; ``read_only``, no ``reqd``) and Purchase Order Item's ``supplier_quotation``
    field (purchase_order_item.json line 524; also ``read_only``, no ``reqd``) — both OPTIONAL,
    informational back-references, never a cascade-ordering edge; ERPNext's own Purchase Order
    cancel never checks for a linked Supplier Quotation at all. Quotation (the SELLING-side
    doctype — entirely distinct from Supplier Quotation, never repointed for this landing; see
    ``tools.py``'s ``TestUnsupportedDoctypeDenied``) also carries an optional
    ``supplier_quotation`` Link (quotation.json line 887, not ``reqd``) — irrelevant to this
    doctype's own cascade, since Quotation is a different doctype and is not itself landed here.
    **Supplier Quotation is a LEAF in the cancel dependency graph** — zero dependents that must
    cascade ahead of its own cancel, the same "leaf" finding Sales/Purchase Order/Material
    Request's own landings already made, this time with BOTH referencing fields confirmed
    optional (no ``reqd``) by direct JSON read, not merely absent from a Link-graph walk.

**Breadth (Quotation) — the twelfth supported doctype, Wave 2's second row, and the FIRST
DYNAMIC-PARTY doctype — the judgment call of Wave 2.** Confirmed from source
(``quotation.json`` + ``quotation.py``, version-16 checkout, both read 2026-07-21; dossier at
``docs/plans/dossiers/quotation.md``):

  * **There is NO static header-level ``customer`` Link field.** Quotation carries
    ``quotation_to`` (Link -> DocType, ``default: "Customer"``, ``reqd: 1``, lines 162-172 of
    ``quotation.json``) paired with ``party_name`` (Dynamic Link, ``options: "quotation_to"``,
    lines 173-185) — confirmed by enumerating all 130 fields in ``quotation.json``: the only
    ``fieldtype: "Link"`` hits naming Customer-shaped fields are ``customer_address`` (an Address
    Link, unrelated) and a hidden ``customer_group``; there is no field named ``customer`` at all.
    Tellingly, ``party_name``'s own ``oldfieldname`` is literally ``"customer"`` — this field was
    a static Customer Link in an earlier schema generation and was migrated to a Dynamic Link,
    the single clearest piece of source evidence that the dynamic pairing is a deliberate,
    load-bearing design choice, not an oversight. ``set_customer_name()`` (quotation.py:232-243)
    confirms the counterparty can resolve to four different doctypes depending on
    ``quotation_to``: Customer, Lead, Prospect, or CRM Deal.
  * **THE KEY DECISION: ``party_field=None``, following Material Request's None-precedent
    (self-clearing/type-gated party is never a stable single column), NOT a forced single Dynamic
    Link.** The broker's ``SUPPORTED_DOCTYPES`` shape expects ``party_field`` to name ONE static
    fieldname whose value IS the party (``"customer"``, ``"supplier"``, ``"party"``) — a Dynamic
    Link does not fit that shape at all: splicing ``party_name`` in alone would disclose a bare
    record name with no doctype context ("is this party_name a Customer or a Lead?"), a materially
    worse disclosure than Journal Entry's or Material Request's own ``None``. Rather than force a
    dynamic field into a slot built for a static one, ``quotation_to``/``party_name`` are instead
    surfaced as a PAIR of list-tier CONTEXT columns — the same treatment Material Request's
    ``material_request_type`` and Stock Entry's ``purpose`` already receive (a doctype-specific
    disclosure column standing in for the missing/unusable party slot), never collapsed into a
    single ``party_field`` string.
  * **This forces a genuinely NEW ``_list_fields`` branch — the FIFTH — not a fit for any of the
    other four.** Quotation combines ``party_field=None`` (like Journal Entry/Material Request/
    Stock Entry) with ``status`` PRESENT (line 863-875: Select, 8 options — Draft/Open/Replied/
    Partially Ordered/Ordered/Lost/Cancelled/Expired, ``read_only: 1``, ``reqd: 1``) AND
    ``grand_total`` PRESENT (line 700-709: Currency, ``read_only: 1``) — a combination no prior
    branch covers: Journal Entry drops both status and grand_total; Material Request keeps status
    but drops grand_total; Stock Entry drops both. The generic branch (used by SI/PI/PE/SO/PO/DN/
    PR/SQ) would splice the LITERAL ``party_field`` value — here ``None`` — into the requested
    field list, the exact bug ``STOCK_ENTRY``'s/``MATERIAL_REQUEST``'s own tests already guard
    against (``assertNotIn(None, fields)``). See :func:`_list_fields` below for the new branch,
    which splices ``quotation_to``/``party_name`` in place of the single party slot.
  * ``date_field="transaction_date"`` (line 229-240, ``default: "Today"``, ``reqd: 1``) —
    **Quotation carries NO ``posting_date`` field at all**, confirmed absent across all 130 fields
    enumerated in ``quotation.json`` — the fifth doctype on this pattern (with Sales Order/
    Purchase Order/Material Request/Supplier Quotation).
  * ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all of ``quotation.py``
    (version-16, full-file grep for ``def submit``/``def cancel``): no override of either method
    anywhere in the file, only ``on_submit``/``on_cancel`` hooks called by the base
    ``Document.submit()``/``.cancel()``.
  * **Quotation posts NO GL entries and NO Stock Ledger entries, on submit or cancel** —
    confirmed by grep of ``quotation.py``: zero hits for ``make_gl_entries``, ``make_sl_entries``,
    ``GLEntry``, or ``StockLedgerEntry`` anywhere in the file. Its amounts are informational; only
    a Sales Order or Sales Invoice built FROM a Quotation (``make_sales_order()``/
    ``make_sales_invoice()``, quotation.py:357-554) ever actually posts a ledger.
  * ``on_submit`` (quotation.py:290-298): an Authorization Control spending-approval check
    (``validate_approving_authority``, unrelated to GL — a permission gate, not a ledger post),
    then ``update_opportunity("Quotation")`` (line 245-251: sets any linked Opportunity's status,
    walked both from ``items[].prevdoc_docname`` and the header ``opportunity`` field) and
    ``update_lead()`` (line 228-230: if ``quotation_to == "Lead"``, calls
    ``Lead.set_status(update=True)``). Both are STATUS writes on sibling CRM documents, never a
    GL/SL post, never a cascade.
  * ``on_cancel`` (quotation.py:300-308): clears the ``lost_reasons`` child table if populated,
    calls ``super().on_cancel()`` (base housekeeping), then ``set_status(update=True)`` (recomputes
    status from order history — "Open" if no Sales Order references this Quotation),
    ``update_opportunity("Open")`` (reverts the linked Opportunity), and ``update_lead()`` again
    (reverts the Lead's own status). **Documented here as the cancel side-effect**: an
    Opportunity/Lead status reset, never a ledger reversal — there is nothing to reverse (see
    above). Unlike Delivery Note/Purchase Receipt, Quotation's own cancel carries NO
    downstream-submitted-document refusal (no ``check_next_docstatus``-shaped call anywhere in
    ``quotation.py``) — a Quotation can be cancelled even with Sales Orders already built from it;
    the SO's own ``prevdoc_docname`` link simply becomes a stale reference, matching ERPNext's own
    design choice (``make_sales_order()`` has its own separate
    ``allow_sales_order_creation_for_expired_quotation`` setting gating FRESH SO creation, not
    cancel).
  * **``declare_enquiry_lost()`` (quotation.py:260-288, ``@frappe.whitelist()``)** is a SEPARATE,
    non-cancel, non-amend state transition this broker does not govern (outside the mechanical
    get/list/submit/cancel/amend surface, like Journal Entry's own reserved-voucher-type carve-out
    is a plan-time refusal rather than a new verb) — it sets ``status="Lost"`` via ``db_set``
    directly, refusing (``frappe.throw``) if a Sales Order has already been made
    (``is_fully_ordered()``/``is_partially_ordered()``). Documented here for completeness, not
    built into a tool: it is a distinct write path from this landing's governed submit/cancel
    pair, and its own refusal condition (a Sales Order already exists) is exactly the kind of
    downstream-dependent fact ``plan_cancel``'s own blast-radius disclosure already surfaces
    generically for THIS doctype's real cancel path.
  * **Cascade edges — ``cascade.py`` needed NO changes.** Grepping ``'"options": "Quotation"'``
    across the full v16 checkout returns exactly TWO hits: Quotation's own self-referencing
    ``amended_from`` (line 209, the standard amendment chain every supported doctype already
    rides) and Sales Order Item's ``prevdoc_docname`` field (``sales_order_item.json``, Link,
    ``label: "Quotation"``, ``read_only: 1``, no ``reqd``) — the field ``make_sales_order()``'s
    own ``field_map`` populates (``quotation.py:465``: ``{"parent": "prevdoc_docname", "name":
    "quotation_item"}``) when a Sales Order is mapped from a Quotation. This is the ONE real
    external Link edge pointing at Quotation in the whole checkout. ERPNext's own
    ``get_submitted_linked_docs`` walks this Link-field graph generically (including child-table
    fields like Sales Order Item's), so a submitted Sales Order built from a Quotation is
    discovered and ordered ahead of it in cancel order with zero doctype-specific cascade code —
    the same mechanism every other doctype's dependents already ride. **Quotation is a LEAF in the
    cancel dependency graph** in the sense that it carries no cancel-blocking refusal of its own
    (see above); it is NOT structurally isolated the way Supplier Quotation is — a submitted Sales
    Order dependent IS discoverable, it simply never REFUSES the Quotation's own cancel (ERPNext's
    design choice, not a broker gap).

**Breadth (POS Invoice) — the thirteenth supported doctype, Wave 2's third row, and the doctype
that FORCES A CORRECTION to the pinned dossier's central GL/SL claim, not just an extension of it.**
Confirmed from source (``pos_invoice.json`` + ``pos_invoice.py``, version-16 checkout, both read
2026-07-21; dossier at ``docs/plans/dossiers/pos_invoice.md``; ``POSInvoice(SalesInvoice)``,
``pos_invoice.py:30``):

  * ``customer`` (Link -> Customer, ``pos_invoice.json`` line 217) IS the header-level party field
    — ``party_field="customer"``, the Sales Invoice shape. **Correction to the dossier:** the
    dossier cites ``reqd: 1`` for this field; the real schema carries only ``bold: 1`` — ``reqd``
    is confirmed ABSENT from the field's own JSON dict (verified by dumping the raw field object,
    not just grepping for the fieldname). The requirement is real but lives in
    ``validate()`` (``pos_invoice.py:199-201``: ``if not self.customer: frappe.throw(_("Please
    select Customer first"))``) — an APPLICATION-level throw, not a schema-level ``reqd`` flag.
    Functionally equivalent (a blank customer is refused either way), but the CITATION the dossier
    gave was wrong; corrected here against the raw field dump.
  * ``status`` (Select, 13 values, ``read_only: 1``, default ``"Draft"``, line 1337) and
    ``grand_total`` (Currency, ``options: "currency"``, ``reqd: 1``, ``read_only: 1``, line 970)
    ARE BOTH present — the generic branch, no ``_list_fields`` change (matching Sales Invoice
    exactly).
  * ``posting_date`` (Date, ``reqd: 1``, default ``"Today"``, line 293) — ``date_field`` stays the
    default ``"posting_date"``, the same real field Sales Invoice/Purchase Invoice/Payment
    Entry/Journal Entry/Delivery Note/Purchase Receipt/Stock Entry already carry.
  * ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all of ``pos_invoice.py``
    (1119 lines, version-16): no ``def submit``/``def cancel`` override anywhere, only
    ``before_submit``/``on_submit``/``before_cancel``/``on_cancel`` hooks (lines 237, 240, 266,
    285) called by the base ``Document.submit()``/``.cancel()``.
  * **THE CENTRAL FINDING, CONTRADICTING THE PINNED DOSSIER: POS Invoice posts NEITHER a GL Entry
    NOR a Stock Ledger Entry of its own, on submit or cancel.** The dossier's own §4 claims GL/SL
    posting is "inherited from SalesInvoice.on_submit" and cites a code comment
    ("# GL/SL posting is inherited from SalesInvoice.on_submit (selling_controller.py)") that does
    not exist anywhere in the real source — verified false by a full-file grep of
    ``pos_invoice.py`` for ``make_gl_entries``/``update_stock_ledger``: **zero hits, either
    string, anywhere in the 1119-line file.** ``POSInvoice.on_submit()`` (lines 240-263) is a
    COMPLETE override of ``SalesInvoice.on_submit()`` (``sales_invoice.py:469-528``, which DOES
    call ``self.update_stock_ledger()`` conditionally and ``self.make_gl_entries()``
    unconditionally) that **never calls ``super().on_submit()``** — confirmed by reading the
    method's full body: it calls ``make_loyalty_point_entry``/``apply_loyalty_points``/
    ``check_phone_payments``/``set_status``/the serial-batch-bundle helpers/
    ``create_and_add_consolidated_sales_invoice``, and nothing else. Frappe's own submit dispatch
    (``Document.run_method("on_submit")`` -> ``getattr(self, "on_submit")()``,
    ``frappe/model/document.py:1238-1252``) is a single, ordinary Python attribute lookup — normal
    MRO, calls the most-derived override ONCE — so ``SalesInvoice.on_submit`` (and its
    ``make_gl_entries()``/``update_stock_ledger()`` calls) is simply never reached for a real POS
    Invoice submit. The SAME pattern repeats for ``validate()`` (``pos_invoice.py:202``:
    ``super(SalesInvoice, self).validate()`` — explicitly SKIPS ``SalesInvoice.validate()``,
    jumping straight to the class above it in the MRO) and ``on_cancel()`` (``pos_invoice.py:287``:
    ``super(SalesInvoice, self).on_cancel()`` — same skip). This is not an oversight: it is
    ERPNext's own documented design for POS Invoice (a fast, lightweight point-of-sale record whose
    real accounting truth is deliberately DEFERRED) — ``debit_to``/``update_stock``/the items rows
    are carried as TEMPLATE data, consumed only when a POS Closing Entry (via POS Invoice Merge
    Log's ``merge_pos_invoice_into()``, ``pos_invoice_merge_log.py:207-253``) builds and submits a
    genuine, separate ``Sales Invoice`` — THAT document's own ``on_submit`` is
    ``SalesInvoice.on_submit`` unmodified (``self.doctype == "Sales Invoice"``, not a POSInvoice
    instance), and it is the one that actually calls ``make_gl_entries()``/``update_stock_ledger()``
    and posts real rows. Confirmed corroborating: zero GL-Entry-shaped assertions anywhere in
    ``test_pos_invoice.py`` (grepped in full), and no ``"POS Invoice"`` entry anywhere in
    ``erpnext/hooks.py``'s ``doc_events`` table (which would be the only other path a GL/SL post
    could be wired in externally).
  * **This creates a genuine divergence between this broker's own ``plan_submit`` preview and what
    a real submit does — the "no debit without a credit" promise the preview exists to keep is, for
    this ONE doctype, broken unless disclosed.** ERPNext's ``get_accounting_ledger_preview``
    (``stock_controller.py:2090-2119``, the RPC this broker's ``ledger_preview`` calls) does NOT go
    through ``on_submit`` at all — it calls ``doc.make_gl_entries()`` as a BARE method call (line
    2112). Since ``POSInvoice`` never overrides ``make_gl_entries`` itself (only ``on_submit``),
    that bare call resolves via ordinary Python MRO to the INHERITED
    ``SalesInvoice.make_gl_entries`` (``sales_invoice.py:1585``) — which genuinely runs and returns
    real-shaped GL rows (``make_customer_gl_entry``, ``sales_invoice.py:1654``, unconditionally
    posts the receivable/income rows whenever ``get_gl_entries()`` returns anything). So this
    broker's own ``projected_gl`` for a POS Invoice ``plan_submit`` comes back NON-EMPTY — a real
    simulation of a posting that will NEVER happen for this specific voucher. ``tools.py`` carries
    the fix: a new, doctype-gated risk flag (:func:`pacioli.tools._pos_invoice_ledger_deferral_flag`)
    names this plainly at plan time, rather than letting the preview's non-empty ``projected_gl``
    silently imply a posting that is not coming. **``plan_cancel`` needed NO equivalent fix** — its
    ``projected_reversal`` comes from ``get_gl_entries(doctype, name)``, a REAL bench read of
    actually-posted rows (not a simulation), which correctly comes back empty for a POS Invoice
    (nothing was ever posted to reverse) and the existing generic flag ("no live GL rows found for
    this voucher — nothing visible to unwind") already says so honestly, unchanged.
  * **Is POS Invoice in the ``get_accounting_ledger_preview`` whitelist tuple
    (stock_controller.py:2109: ``doc.doctype in ("Purchase Receipt", "Delivery Note", "Stock
    Entry")``), the way Delivery Note/Purchase Receipt/Stock Entry are? NO — confirmed absent by
    direct string comparison against the tuple literal.** This is irrelevant to the finding above,
    though: that whitelist exists to pre-seed an in-memory Stock Ledger Entry for doctypes whose
    ``make_gl_entries`` (the ``StockController`` base version) SOURCES its GL rows FROM existing
    Stock Ledger Entry detail rows. POS Invoice's inherited ``make_gl_entries`` is
    ``SalesInvoice``'s own override, which does not have that dependency (it builds GL rows
    directly from the document's own fields via ``make_customer_gl_entry``/``make_item_gl_entries``
    etc.) — so POS Invoice's absence from the tuple is expected and does not by itself explain why
    the preview is misleading; the ``on_submit``-bypass finding above is the actual cause. The
    OTHER half of that same line's ``or`` condition (``doc.get("update_stock")``) DOES still apply
    to POS Invoice document-by-document when its own ``update_stock`` field is truthy — this is the
    same mechanism Sales Invoice's own ``update_stock`` shape already rides, unaffected by POS
    Invoice landing, and does not change the central finding: the preview simulates a posting the
    real ``on_submit`` will not make, regardless of ``update_stock``.
  * **GL side, restated for the SUPPORTED_DOCTYPES row itself:** ``debit_to`` (Link -> Account,
    ``reqd: 1``, line 1356) exists on the schema and IS the receivable account a genuine submit
    would eventually use — but only via the LATER consolidated Sales Invoice, never this document's
    own submit. **Conditional SL:** ``update_stock`` (Check, default 0, line 590) exists and is
    copied onto the consolidated invoice's own items the same way, but — per the finding above —
    does NOT gate a real ``update_stock_ledger()`` call on THIS document either.
  * **Consolidation / cancel-block — a genuine, VERIFIED CORRECTION to the dossier's own §5-§7,
    which is MORE PESSIMISTIC than the real mechanism.** ``before_cancel`` (``pos_invoice.py:266-
    283``) hard-refuses the cancel when ``self.consolidated_invoice`` is set AND that Sales
    Invoice's own ``docstatus == 1``. ``consolidated_invoice`` (Link -> Sales Invoice, ``read_only:
    1``, line 1515) is a REAL Link field, populated by TWO paths, both confirmed by source read:
    (a) a return POS Invoice's own ``on_submit``, when ``invoice_type_in_pos == "Sales Invoice"``
    (a POS Settings singleton value read fresh via ``frappe.db.get_single_value``, NOT a persisted
    field on this document — genuinely undisclosable from the draft alone, a real disclosure gap,
    recorded honestly rather than claimed covered); (b) POS Invoice Merge Log's OWN batch
    consolidation of ANY POS Invoice, return or not (``pos_invoice_merge_log.py:379``:
    ``doc.update({"consolidated_invoice": ...})`` for every merged invoice, not just returns).
    **The dossier claims the resulting submitted Sales Invoice, and any submitted POS Closing
    Entry / POS Invoice Merge Log, are invisible to this broker's ``get_submitted_linked_docs``
    blast-radius disclosure ("it is not walked by the generic link traversal"). Traced against
    frappe's own ``linked_with.py`` (version-16) directly, this is WRONG — both ARE discoverable:**
    (1) ``sales_invoice_item.json`` line 987 confirms ``pos_invoice`` (Link -> POS Invoice) is set
    on every merged Sales Invoice Item row (``merge_pos_invoice_into()``,
    ``pos_invoice_merge_log.py:238``: ``si_item.pos_invoice = doc.name`` — for BOTH the return-
    consolidation path and the ordinary batch-merge path, confirmed by reading the shared mapper),
    the same shape ``Sales Invoice Item.delivery_note`` already rides for Delivery Note. (2)
    ``pos_invoice_reference.json`` line 18 confirms ``pos_invoice`` (Link -> POS Invoice,
    ``reqd: 1``) on the ``POS Invoice Reference`` CHILD table, embedded via a Table field
    (``pos_invoices``) in BOTH ``POS Closing Entry`` (``is_submittable: 1``,
    ``pos_closing_entry.json`` line 257) and ``POS Invoice Merge Log`` (``is_submittable: 1``,
    ``pos_invoice_merge_log.json`` line 130) — neither exempted (``erpnext/hooks.py:428``'s
    ``auto_cancel_exempted_doctypes`` names only ``"Payment Entry"``). Frappe's own
    ``get_linked_fields``/``get_references_across_doctypes``
    (``frappe/desk/form/linked_with.py:610-659``, ``324-364``) explicitly resolves a CHILD-TABLE
    Link field up to its embedding TOP-LEVEL parent via the child row's own ``parenttype`` column
    (``get_referencing_documents``'s ``is_child`` branch, line 356-363: ``groupby(res,
    key=lambda row: row["parenttype"])``, then re-queries THAT parent doctype filtered
    ``docstatus=1``) — the documented mechanism ("Include child table, link and dynamic link
    references") is not incidental, it is the FEATURE. So a submitted ``POS Closing Entry`` or
    ``POS Invoice Merge Log`` referencing this POS Invoice via its ``pos_invoices`` child table
    genuinely surfaces in ``get_submitted_linked_docs``'s response, exactly like the submitted
    consolidated Sales Invoice does — this broker's PRE-EXISTING blast-radius refusal (any
    non-empty result denies the plan) already covers BOTH halves of the real ``before_cancel``
    gate's practical effect, with zero new code. What remains genuinely undisclosable at plan time
    is narrower than the dossier implied: only the ``invoice_type_in_pos`` POS-Settings-singleton
    gate on the RETURN auto-consolidation path (see (a) above) — not the blast-radius visibility
    itself.
  * **Consolidation boundary (TRIAGE.md, ``pos_invoice_merge_log`` REFUSE):** this landing governs
    individual POS Invoice submit/cancel exactly like Sales Invoice; it does NOT add tools for
    ``POS Invoice Merge Log`` or drive POS Closing Entry's own batch-consolidation machinery — that
    remains a REFUSE doctype (``docs/plans/dossiers/TRIAGE.md``), an ERPNext-side automation layer
    outside this broker's plan/consent/prove frame. A submitted linked ``POS Closing Entry``/``POS
    Invoice Merge Log`` is still DISCLOSED (see above) — disclosure is not the same as governance.
  * **Return flow — ``update_outstanding_for_self`` (the field :func:`pacioli.tools.
    _return_risk_flags` keys on since Delivery Note's landing): CONFIRMED ABSENT** from
    ``pos_invoice.json``'s full 185-field list (grepped directly, zero hits, matching Delivery
    Note's/Purchase Receipt's own absence) — but POS Invoice is a GENUINELY THIRD shape, not a
    repeat of DN/PR's "no receivable at all" case: it DOES carry ``debit_to`` (see above), yet
    (per the central finding) posts NO GL of its own regardless. ``tools.py``'s
    :func:`_return_risk_flags` gains a doctype-gated branch for this — see its own docstring.
  * **``is_pos`` — unlike Sales Invoice, ALWAYS truthy on a real POS Invoice.** ``validate()``
    (``pos_invoice.py:203-206``) throws ("POS Invoice should have the field Include Payment
    checked") if ``not cint(self.is_pos)`` — so :func:`pacioli.tools._pos_risk_flags` (already
    fully doctype-agnostic, keyed on ``is_pos`` alone, built for Sales Invoice where the field is
    optional) fires unconditionally for every real, governable POS Invoice, never a no-op. No code
    change needed — documented here since it is a genuinely different USAGE pattern of existing,
    unmodified machinery, not a new mechanism.
  * **Cascade edges — ``cascade.py`` needed NO changes**, the same finding as every prior breadth
    increment (it carries zero doctype-specific string literals). Real ``Link`` fields naming POS
    Invoice (grepped ``'"options": "POS Invoice"'`` across the full v16 checkout, each hit's
    ``fieldtype`` read individually): POS Invoice's own self-links (``amended_from`` line 329,
    ``return_against`` line 342), ``Sales Invoice Item.pos_invoice`` (line 987, discussed above),
    and ``POS Invoice Reference.pos_invoice``/``.return_against`` (lines 18/64, discussed above).
    ERPNext's own ``get_submitted_linked_docs`` walks all of these generically, including the
    child-table resolution — zero doctype-specific cascade code needed, the same mechanism every
    other supported doctype's dependents already ride.

**Breadth (Dunning) — the fourteenth supported doctype, Wave 2's fourth row, and the FIRST doctype
whose native ``ledger_preview`` RPC this broker must NOT call — the dossier's own hedge
("projected_gl will be empty, same as SO/PO/MR") turns out to describe the wrong mechanism
entirely.** Confirmed from source (``dunning.json`` + ``dunning.py``, version-16 checkout, both
read 2026-07-21; dossier at ``docs/plans/dossiers/dunning.md``; ``class Dunning(AccountsController)``,
``dunning.py:25``):

  * ``customer`` (Link -> Customer, ``dunning.json`` lines 219-225, ``reqd: 1``) IS the
    header-level party field — ``party_field="customer"``. **The dossier's ``reqd: 1`` citation is
    CORRECT here** (unlike POS Invoice's landing, where the same citation shape was wrong) —
    verified by dumping the raw field dict, not just grepping the fieldname.
  * ``status`` (Select, ``Draft``/``Resolved``/``Unresolved``/``Cancelled``, ``allow_on_submit: 1``,
    ``read_only: 1``, default ``"Unresolved"``, lines 235-244) and ``grand_total`` (Currency,
    ``read_only: 1``, default ``"0"``, lines 226-234) are BOTH present — the generic
    ``_list_fields`` branch, no new branch needed (``customer``/``status``/``grand_total`` spliced
    in exactly like Sales Invoice/Sales Order).
  * ``posting_date`` (Date, ``reqd: 1``, default ``"Today"``, lines 92-98) — ``date_field`` stays
    the default ``"posting_date"``, the same real field SI/PI/PE/JE/DN/PR/SE/POS Invoice already
    carry. Used as the snapshot date for ``validate_overdue_payments``'s per-row
    ``overdue_days``/``interest`` calculation (``dunning.py:99-104``), not a GL posting date (see
    below — there is no GL posting to date).
  * ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 276 lines of ``dunning.py``:
    no ``def submit``/``def cancel`` override, no ``def on_submit`` override either (only
    ``validate`` and its five sub-validators, plus ``on_cancel`` — see below). Base
    ``Document.submit()``/``.cancel()`` apply, the same run_method doc-method surface every
    non-Journal-Entry doctype already rides.
  * **THE CENTRAL FINDING, GENUINELY NEW: Dunning has NO ``make_gl_entries`` method ANYWHERE in
    its class hierarchy — not defined, not inherited — so this broker's own ``ledger_preview`` RPC
    is UNCALLABLE for this doctype, not merely a no-op.** Confirmed two ways: (1) ``dunning.py``
    defines no ``on_submit`` and no ``make_gl_entries`` (full-file read, 276 lines); Dunning's
    submit-time work is entirely inside ``validate()`` (lines 72-77:
    ``validate_same_currency``/``validate_overdue_payments``/``validate_totals``/
    ``set_party_details``/``set_dunning_level``), and ``validate_totals`` (lines 106-111) only
    COMPUTES ``self.grand_total = self.total_outstanding + self.dunning_amount`` in memory — it
    never calls ``erpnext.accounts.general_ledger.make_gl_entries`` or constructs a ``GL Entry``.
    (2) ``Dunning``'s MRO is ``Dunning -> AccountsController -> TransactionBase -> StatusUpdater ->
    Document`` (confirmed: ``class AccountsController(TransactionBase)``,
    ``accounts_controller.py:105``; ``class TransactionBase(StatusUpdater)``,
    ``transaction_base.py:20``) — a full-tree grep for ``def make_gl_entries`` across every
    ``.py`` file in ``erpnext/`` finds it defined ONLY on ``StockController``
    (``stock_controller.py:292``, NOT an ancestor of ``AccountsController`` — the inheritance runs
    the other way, ``StockController(AccountsController)``) and on a short, closed list of
    individual doctype controllers that each define their OWN copy (``PurchaseInvoice``,
    ``SalesInvoice``, ``PaymentEntry``, ``JournalEntry``, ``Asset``, ``AssetCapitalization``,
    ``AssetRepair``, ``PeriodClosingVoucher``, ``InvoiceDiscounting``) — ``Dunning`` is not among
    them and shares no ancestor with any of them. This is a DIFFERENT shape from every prior
    "posts no GL" doctype landed so far: Sales Order/Purchase Order/Material Request/Supplier
    Quotation/Quotation all descend from ``StockController`` (via ``SellingController``/
    ``BuyingController``), so they DO inherit a real, callable ``make_gl_entries`` that
    conditionally no-ops — the native preview RPC succeeds and correctly returns empty
    ``gl_data``. Dunning has no such inherited method to call at all.
  * **Why this matters mechanically:** ERPNext's own ``get_accounting_ledger_preview``
    (``stock_controller.py:2090-2119``, the function this broker's ``ledger_preview`` calls via
    ``show_accounting_ledger_preview``) calls ``doc.make_gl_entries()`` as a BARE, unguarded method
    call (line 2112 — no ``getattr``/``hasattr`` guard, unlike the ``run_method("before_gl_preview")``
    hook dispatch just above it, which DOES degrade gracefully via ``getattr(self, method, None)``
    + a callable check, ``frappe/model/document.py:1238-1258``). For a real Dunning document, this
    bare call raises ``AttributeError: 'Dunning' object has no attribute 'make_gl_entries'`` on the
    live bench — frappe wraps the exception into a non-2xx JSON error response (carrying its
    ``exc_type``/``_server_messages`` envelope), which this broker's own ``_answered`` transport
    taxonomy classifies as answered, so ``client.ledger_preview`` raises a real ``ErpnextError``
    that ``dispatch()`` converts into a structured deny — EVERY ``plan_submit`` for a Dunning would
    refuse with an opaque bench error, never actually planning, if the call were made. **This is
    NOT the dossier's hedge** ("projected_gl will be empty, same as SO/PO/MR" — that describes a
    callable no-op, which is what SO/PO/MR/SQ/Q genuinely have; Dunning has no callable to invoke
    at all). The fix: ``tools.py``'s ``_tool_plan_submit`` skips the ``client.ledger_preview()``
    network call entirely for ``doctype == DUNNING`` (never sends the request that would 500 on a
    live bench) and reports ``projected_gl=[]`` BY CONSTRUCTION, paired with a new doctype-gated
    risk flag (:func:`pacioli.tools._dunning_ledger_preview_unavailable_flag`) naming plainly WHY
    it is empty — the preview mechanism itself does not apply to this doctype, not "nothing to
    post". ``plan_cancel`` needs no equivalent change: its ``projected_reversal`` comes from
    ``get_gl_entries(doctype, name)``, a REAL bench read of the ``GL Entry`` table filtered by
    voucher type/name, which naturally, safely returns empty for Dunning (no rows were ever
    written under that voucher_type) — no AttributeError risk, the same "no live GL rows found"
    flag every other never-posts doctype already gets.
  * **``on_cancel`` (``dunning.py:150-164``)** calls ``super().on_cancel()`` then sets
    ``self.ignore_linked_doctypes`` to a fixed 11-entry tuple (``GL Entry``, ``Stock Ledger
    Entry``, the various ``Repost *``/``Unreconcile *`` maintenance doctypes, ``Payment Ledger
    Entry``, ``Serial and Batch Bundle``) — the same precautionary list ``AccountsController``
    subclasses that DO post GL carry, inherited here as dead weight: Dunning never creates a ``GL
    Entry`` row (see above), so there is nothing in that list for a Dunning cancel to actually
    ignore. **No refusal on cancel** — confirmed by the absence of any ``frappe.throw`` anywhere in
    ``dunning.py``; a Dunning cancels freely regardless of any linked Sales Invoice's payment
    state. Cancelling does NOT reverse or touch the linked Sales Invoice(s) in
    ``overdue_payments`` — ``update_linked_dunnings`` (module-level, ``dunning.py:167-226``) is
    wired only to Sales Invoice's own post-submit hook (updating a linked Dunning's ``status`` when
    the invoice's ``outstanding_amount`` changes), never to Dunning's own ``on_cancel``.
  * **Cascade — a genuine correction to the dossier's own §5 "disclosure gap" claim, traced
    directly against frappe's ``linked_with.py`` (version-16) rather than assumed.** ``cascade.py``
    needs NO changes (doctype-blind by construction, the same finding every prior breadth increment
    has made). The only real ``Link`` field naming Dunning (grepped ``'"options": "Dunning"'``
    across the full v16 checkout) is Dunning's own ``amended_from`` self-link
    (``dunning.json`` line 156). **The dossier claims Payment Entry's reference to a Dunning is
    genuinely invisible to this broker's ``get_submitted_linked_docs`` walk, because ``Payment
    Entry Reference.reference_name`` is "a Data/String field, not a Link field."** Dumping the raw
    field dict directly (``payment_entry_reference.json``) shows this is WRONG: ``reference_name``
    is fieldtype ``Dynamic Link`` (``options: "reference_doctype"``, ``reqd: 1``), not ``Data`` —
    and frappe's own generic blast-radius walker explicitly resolves Dynamic Link fields, not just
    static Link fields: ``get_references_across_doctypes`` (``linked_with.py:192-226``) calls BOTH
    ``get_references_across_doctypes_by_link_field`` AND
    ``get_references_across_doctypes_by_dynamic_link_field`` (``linked_with.py:269-321``, which
    queries ``DocField``/``Custom Field`` rows filtered ``fieldtype == "Dynamic Link"`` and then the
    actual live data to find doctypes genuinely in use for that field), and every hit — link or
    dynamic-link — gets the SAME child-table-to-parent ``is_child``/``parenttype`` promotion
    (``linked_with.py:225``, ``324-364``) already cited for POS Invoice's own child-table edges.
    ``payment_entry.py:732`` confirms ``"Dunning"`` is one of the five ``reference_doctype`` values
    Payment Entry's own ``references`` child table supports. So a submitted Payment Entry whose
    ``references`` rows include ``reference_doctype="Dunning"``/``reference_name=<this Dunning>``
    genuinely surfaces in ``get_submitted_linked_docs("Dunning", name)``'s response — this broker's
    PRE-EXISTING generic refusal (any non-empty result denies the cascade/cancel plan) already
    covers it, with zero new code. **There is no disclosure gap here; the dossier's claim does not
    survive a direct read of frappe's own dynamic-link resolution.**
  * Dunning is also named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list (line 332,
    alongside Sales Invoice/Purchase Invoice/Journal Entry/Payment Entry/Stock Entry) — this
    broker's existing closed-books check (``get_period_locks``, already doctype-generic, matching a
    Period Closing Voucher's ``closed_documents`` rows against the doctype name it is passed) needs
    no change to apply to a locked Dunning period; it is not exempted from ``auto_cancel_exempted_
    doctypes`` (``hooks.py:428``, which names only ``"Payment Entry"``) either, though that list
    governs ERPNext's own desk "cancel all" feature, not this broker's cascade mechanism.

**Breadth (Stock Reconciliation) — Wave 2, the fifteenth doctype, and TWO genuinely new findings at
once: the SECOND doctype ever to need ``SUBMIT_VIA_CLIENT_RPC`` (a pinned dossier silence, not a
dossier error — the dossier never discusses transport at all), and a FIFTH ``ledger_preview`` shape
distinct from all four already landed.** Confirmed from source (``stock_reconciliation.json`` +
``stock_reconciliation.py``, version-16 checkout, both read 2026-07-21; dossier at
``docs/plans/dossiers/stock_reconciliation.md``; ``class StockReconciliation(StockController)``,
``stock_reconciliation.py:31``):

  * **No header-level party field of any kind** — confirmed absent from the complete 17-field
    enumeration in ``stock_reconciliation.json`` (no ``customer``, no ``supplier``, no generalized
    ``party``): ``party_field=None``, the Journal Entry/Material Request/Stock Entry/Quotation
    shape, for the same reason Material Request's own landing gave — an internal correction, no
    counterparty. The dossier's claim here is CORRECT.
  * ``status`` and ``grand_total`` are BOTH confirmed ABSENT (the same 17-field enumeration) — the
    dossier's claim here is also CORRECT. In their place: ``purpose`` (Select, ``""``/``"Opening
    Stock"``/``"Stock Reconciliation"``, lines 55-60 — a two-way fork, not Stock Entry's 13-way
    one) and ``difference_amount`` (Currency, ``read_only: 1``, lines 125-131 — the single
    aggregate variance ``set_total_qty_and_amount()`` freezes at validate time, the doctype's own
    nearest thing to a summary metric).
  * ``posting_date`` (Date, ``reqd: 1``, default ``"Today"``, lines 66-73, ``oldfieldname:
    "reconciliation_date"``) IS present — ``date_field`` stays the default ``"posting_date"``,
    never a ``transaction_date`` branch. The dossier's claim here is CORRECT.
  * **``submit_via`` — the dossier is SILENT on transport entirely, and reading the doctype's own
    submit/cancel overrides (not merely its ``on_submit``/``on_cancel`` hooks, which every prior
    landing's own verification stopped at) surfaces the SECOND ``SUBMIT_VIA_CLIENT_RPC`` doctype
    this broker has ever needed.** ``StockReconciliation`` DOES override ``submit()`` AND
    ``cancel()`` (``stock_reconciliation.py:1107-1127``, not merely ``on_submit``/``on_cancel``,
    which it ALSO overrides separately — see below): both bodies queue a background job via
    ``self.queue_action(...)`` when ``len(self.items) > 100`` (a genuine, documented async-outcome
    caveat — see below), otherwise call ``self._submit()``/``self._cancel()`` (the base
    ``Document``'s own private implementation) directly. **Neither override carries an
    ``@frappe.whitelist()`` decorator** — confirmed by grepping the full file for every
    ``@frappe.whitelist()`` occurrence (three total, at lines 1130/1310/1406, all AFTER the
    ``cancel`` method ends and none immediately above ``submit``/``cancel`` themselves). This is
    EXACTLY Journal Entry's own mechanism (``journal_entry.py:186,195``, cited in this module's own
    ``submit_via`` comment block above ``SUPPORTED_DOCTYPES``): frappe's REST ``run_method``
    dispatch (``frappe/api/v1.py:113-121``, ``execute_doc_method``) calls ``doc.is_whitelisted(
    method)`` BEFORE ``doc.run_method(method, ...)`` — ``Document.is_whitelisted``
    (``frappe/model/document.py:1657-1662``) resolves the METHOD OBJECT via ``getattr(self,
    method_name)`` and checks it against frappe's global ``whitelisted`` set (populated only by the
    ``@frappe.whitelist()`` decorator, ``frappe/__init__.py:457-476``); a subclass override that
    shadows the base ``Document.submit``/``.cancel`` (both of which ARE decorated,
    ``document.py:1338-1348``) with an UNDECORATED function object is simply not IN that set, so
    ``is_whitelisted`` raises ``PermissionError`` ("Method Not Allowed") for ANY attempt to submit
    or cancel a Stock Reconciliation via the ``run_method=submit``/``run_method=cancel`` URL-path
    vector every other non-Journal-Entry doctype rides. **``submit_via=SUBMIT_VIA_CLIENT_RPC`` is
    therefore the correct, source-verified value** — Stock Reconciliation rides
    ``frappe.client.submit``/``.cancel`` exactly like Journal Entry, needing zero new transport code
    (:meth:`ErpnextClient.submit_document`/``.cancel_document`` already branch generically on
    ``SUPPORTED_DOCTYPES[doctype]["submit_via"]``, and ``tools.py``'s ``_governed_write`` already
    threads the same already-fetched ``doc`` into ``client.submit_document(doctype, name, doc=doc)``
    unconditionally for every doctype, not gated on Journal Entry — built generically the first
    time). ``pacioli_guard``'s ``body_scoped_target`` (guard CHANGELOG 0.5.0) is likewise already
    doctype-generic (parses ``doc["doctype"]``/``doctype``+``name`` from the body dynamically, never
    hardcoded to ``"Journal Entry"`` — confirmed by reading ``guard/pacioli_guard/scope.py``), so no
    guard change is needed either. **The async-queue caveat, documented not gated:** for a
    reconciliation with over 100 item rows, a real submit/cancel enqueues a background job rather
    than completing synchronously — an ERPNext-native behavior applying to ANY caller, not
    introduced by this broker; the existing ``governed_submit``/``readback`` degrade-to-uncertain
    path (``spine.py``) already covers an outcome that does not match the immediate response,
    exactly as it does for any other transient post-execute read failure, so no new gate is built
    for it.
  * **THE CENTRAL LEDGER-PREVIEW FINDING: Stock Reconciliation IS a ``StockController`` subclass
    (unlike Dunning), so ``make_gl_entries`` genuinely EXISTS in its MRO and the native preview RPC
    does not raise — but the resulting empty ``projected_gl`` is MISLEADING, not honest, a FIFTH
    shape distinct from all four already landed.** ``get_accounting_ledger_preview``
    (``stock_controller.py:2090-2119``) forces ``doc.docstatus = 1`` (line 2107) then checks ``if
    doc.get("update_stock") or doc.doctype in ("Purchase Receipt", "Delivery Note", "Stock
    Entry"): doc.update_stock_ledger()`` (lines 2109-2110) — Stock Reconciliation carries no
    ``update_stock`` field (confirmed absent from the 17-field list) and is confirmed ABSENT from
    that three-item whitelist tuple by direct string comparison, so ``update_stock_ledger()`` is
    NEVER called in the preview's savepoint — unlike Delivery Note/Purchase Receipt/Stock Entry,
    whose own landings this exact whitelist line made honest. ``doc.make_gl_entries()`` (line 2112)
    IS still called, unconditionally and unguarded — and IS callable here (``StockReconciliation``
    inherits it straight from ``StockController``, ``stock_controller.py:292-319``, never
    overriding it), so there is no Dunning-shaped ``AttributeError``. Tracing the call: ``need_
    inventory_map`` gates on ``self.get_stock_items()`` (truthy for virtually any real Stock
    Reconciliation — its item rows exist specifically to correct stock items) AND perpetual
    inventory being enabled; when both hold, ``self.get_gl_entries(inventory_account_map)``
    resolves to ``StockReconciliation``'s own override (``stock_reconciliation.py:961-965``, which
    checks ``self.cost_center`` — defaulted from ``Company.cost_center`` in ``validate()``, line
    70-71, so populated on any doc that has been through a normal save — then delegates to
    ``super().get_gl_entries(inventory_account_map, self.expense_account, self.cost_center)``).
    That base ``StockController.get_gl_entries`` (``stock_controller.py:726-863``) calls
    ``self.get_stock_ledger_details()`` (line 923+), a LIVE QUERY against the REAL ``Stock Ledger
    Entry`` table filtered by ``voucher_type``/``voucher_no`` for THIS document — and because
    ``update_stock_ledger()`` was never called in the preview's savepoint, **no SLE rows exist for
    this voucher at all**, so the query returns an empty map. ``get_voucher_details``
    (``stock_controller.py:871-887``) has a Stock-Reconciliation-SPECIFIC branch that builds its
    return value by iterating THAT empty SLE map, so ``get_gl_entries``'s own ``for item_row in
    voucher_details:`` loop never executes, ``gl_list`` stays empty, and ``process_gl_map([])``
    (``general_ledger.py:190-192``) returns ``[]`` immediately — no exception anywhere, a clean,
    quiet empty result.
  * **Why this empty result is MISLEADING rather than honest, unlike SO/PO/MR/SQ/Q's own empty
    preview:** for Sales/Purchase Order/Material Request/Supplier Quotation/Quotation, the
    emptiness accurately reflects reality — a REAL submit of those doctypes posts no GL either
    (they are pre-accounting placeholders that never call ``update_stock_ledger()`` at all, at any
    time). Stock Reconciliation is the OPPOSITE: a real submit's ``on_submit``
    (``stock_reconciliation.py:109-114``) calls ``self.update_stock_ledger()``
    UNCONDITIONALLY, and that method (lines 749-816) REFUSES the submit outright
    (``frappe.throw``, lines 811-816) if it would end up creating zero SLE rows — meaning any
    document that successfully plans a submit is, by ERPNext's own validation, GUARANTEED to write
    real Stock Ledger Entry rows on that real submit, and (whenever perpetual inventory is enabled
    for the company) real GL Entry rows sourced from those same rows immediately afterward
    (``on_submit`` line 113, ``self.make_gl_entries()``). The preview's empty ``projected_gl`` is
    therefore a FALSE NEGATIVE for this doctype specifically — it undersells exactly the postings a
    real submit will make, the mirror image of POS Invoice's false POSITIVE (a non-empty preview
    for a posting that will never happen). **Fix:** a new, doctype-gated risk flag
    (:func:`pacioli.tools._stock_reconciliation_ledger_preview_incomplete_flag`), fired
    unconditionally for ``doctype == STOCK_RECONCILIATION``, submit-direction only — the preview
    call itself is NOT skipped (unlike Dunning: it is callable and harmless, never raises), only
    annotated. ``plan_cancel`` needs no equivalent fix: its ``projected_reversal`` comes from
    ``get_gl_entries(doctype, name)``, a REAL bench read of ACTUALLY-POSTED ``GL Entry`` rows (never
    ``make_gl_entries``), accurate for Stock Reconciliation exactly as for every other doctype.
  * **The absolute-vs-delta correction shape (a governance-relevant risk shape, not a mechanical
    finding):** a Stock Reconciliation submit does not record a delta ("received 10 more units");
    it records an ABSOLUTE state ("this warehouse now holds exactly N units at rate R"), and the
    SLE ``actual_qty`` ERPNext computes and posts is whatever delta closes the gap between the
    prior ledger balance and the entered value (``get_sle_for_items``, ``stock_reconciliation.py:
    858-914`` — ``qty_after_transaction`` is set directly to the entered ``row.qty``). The
    resulting variance posts to ``expense_account`` (the "Difference Account", defaulted from
    ``Company.stock_adjustment_account`` at validate time, line 66-69) — an adjustment account is
    the ledger's own way of saying "I do not know where this came from; I am correcting a prior
    error," not a normal counterparty. This is documented plainly (not gated) in the plan-tier
    disclosure, the same treatment Journal Entry's own reserved-voucher-type risk earned, because it
    changes what CONSENT to a Stock Reconciliation submit actually means: consent to an assertion of
    truth, not consent to a movement.
  * **Cancel — reversal, and a genuinely invisible refusal, confirmed by field type.**
    ``on_cancel`` (``stock_reconciliation.py:116-127``) calls ``validate_reserved_stock()``
    UNCONDITIONALLY (also called from ``validate()`` when ``self._action == "submit"``, line 91-92
    — so the SAME refusal can fire on submit too, not cancel-only) — this refuses (``frappe.throw``)
    if any item row whose entered ``qty`` differs from its ``current_qty`` has reserved stock
    against it, per a LIVE QUERY (``get_sre_reserved_qty_for_items_and_warehouses``,
    ``stock_reservation_entry.py``) against ``Stock Reservation Entry`` — there is NO field on the
    Stock Reconciliation document itself carrying this information (confirmed: reserved quantity is
    computed by joining item_code/warehouse against a wholly separate doctype's live rows, never a
    Link field on this document), so it is structurally invisible to this broker's plan-tier
    disclosure at both submit and cancel — the dossier's claim here is CORRECT, and the same
    "answered ERPNext refusal surfaces safely at the real call" treatment every prior doctype's own
    unpreviewable gap already gets applies here too; no new broker gate is built. ``on_cancel`` then
    sets ``self.ignore_linked_doctypes = ("GL Entry", "Stock Ledger Entry", "Repost Item
    Valuation", "Serial and Batch Bundle")`` (lines 118-123) — exempting these from ERPNext's own
    generic linked-doc cancel check, meaning a future reposted SLE/GL row or Repost Item Valuation
    depending on this reconciliation is not part of ERPNext's own refusal surface either (documented,
    not gated — the same shape Dunning's own dead-weight exemption list earned, though here the
    doctype DOES post to these tables for real, unlike Dunning). ``make_sle_on_cancel()`` (916-931)
    reverses the SLE rows by posting the inverse; the inherited ``make_gl_entries_on_cancel``
    (``stock_controller.py:1429-1437``) reverses GL rows for any that exist; ``repost_future_sle_
    and_gle()`` recalculates downstream valuation; the inherited ``delete_auto_created_batches``
    (``stock_controller.py:993+``) soft-cancels any Serial and Batch Bundle rows this reconciliation
    created. All unconditional reversals, none gated on perpetual inventory (the SLE reversal always
    runs; the GL reversal is a no-op if no GL rows were ever posted, mirroring the submit-side gate).
  * **Cascade — zero impact, matching the dossier.** Grepping the full v16 checkout for
    ``'"options": "Stock Reconciliation"'`` and reading each hit's ``fieldtype`` individually
    returns exactly ONE result: Stock Reconciliation's own self-referencing ``amended_from``
    (``stock_reconciliation.json`` line 138). No external doctype carries a real Link field to
    Stock Reconciliation — ``cascade.py`` needs NO changes (doctype-blind by construction, the same
    finding every prior breadth increment has made), and Stock Reconciliation can never be a
    dependent in any cancel cascade because nothing submitted cites it.
  * Stock Reconciliation is also named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list
    (line 343, alongside Sales Invoice/Purchase Invoice/Journal Entry/Stock Entry/Dunning/Delivery
    Note/Purchase Receipt) — this broker's existing closed-books check (``get_period_locks``,
    already doctype-generic) needs no change to apply to a locked Stock Reconciliation period; it
    is not exempted from ``auto_cancel_exempted_doctypes`` (``hooks.py:428``, which names only
    ``"Payment Entry"``) either.

**Breadth (Landed Cost Voucher) — the sixteenth supported doctype, Wave 2's sixth row and LAST,
and the sharpest disclosure case in the campaign: a document whose own submit/cancel rewrites
OTHER documents' ledgers, and whose one real cascade edge is a Dynamic Link this landing proves IS
generically discoverable — contra both the pinned dossier AND Purchase Receipt's own prior landing
comment (b2d06a9).** Confirmed from source (``landed_cost_voucher.json`` +
``landed_cost_voucher.py`` + ``landed_cost_purchase_receipt.json`` + ``landed_cost_item.json`` +
``landed_cost_vendor_invoice.json``, version-16 checkout, all read 2026-07-21; dossier at
``docs/plans/dossiers/landed_cost_voucher.md`` — right about party_field/status/grand_total/
posting_date, but WRONG about the Dynamic Link's discoverability, the same class of correction
Dunning's own landing made to a structurally identical claim):

  * **No header-level party field at all** — confirmed by enumerating ``landed_cost_voucher.json``'s
    complete ``field_order`` (naming_series/company/posting_date/purchase_receipts/items/
    vendor_invoices/taxes/total_vendor_invoices_cost/total_taxes_and_charges/
    distribute_charges_based_on/amended_from/landed_cost_help — no Customer/Supplier/Party-shaped
    field anywhere). ``party_field=None`` — the JE/MR/SE/Q/SR shape: costs allocate ONTO
    already-submitted receipts; whatever supplier identity exists lives on those receipts, not
    here. (A denormalized, ``read_only`` ``supplier`` field DOES exist on the ``Landed Cost
    Purchase Receipt`` CHILD row — ``landed_cost_purchase_receipt.json`` line 33-39 — populated at
    "Get Items" time for display only; it is not this doctype's own party column and is never
    spliced into ``party_field``, the same non-authoritative-child-field treatment Purchase
    Receipt's own ``supplier`` column on ``Landed Cost Purchase Receipt`` already got when THAT
    doctype's landing named it.)
  * **``status`` and ``grand_total`` are BOTH confirmed ABSENT** (the same 15-field enumeration) —
    this doctype is submittable (``"is_submittable": 1``, line 164) with docstatus alone standing
    in for status, and carries no financial total of its own beyond the aggregate it is FOR
    (``total_taxes_and_charges``, Currency, read-only, lines 92-99: ``sum(flt(d.base_amount) for d
    in self.get("taxes"))``, set in ``set_total_taxes_and_charges()``, ``landed_cost_voucher.py:
    199-200``). This is a SEVENTH ``_list_fields`` branch, not a reuse of Stock Reconciliation's own
    (same absence shape — party/status/grand_total all missing — but different substitute fields):
    ``distribute_charges_based_on`` (Select, ``"Qty\nAmount\nDistribute Manually"``, lines 105-110 —
    the context column, Stock Reconciliation's own ``purpose``-precedent role) and
    ``total_taxes_and_charges`` (the single aggregate, Stock Reconciliation's own
    ``difference_amount``-precedent role). ``total_vendor_invoices_cost`` (a second, optional
    aggregate for the separate vendor-invoice-claim path below) is deliberately NOT spliced in,
    matching Stock Reconciliation's own one-aggregate-field discipline.
  * ``posting_date`` (Date, ``reqd: 1``, default ``"Today"``, lines 134-139) IS present —
    ``date_field="posting_date"``, the same real field SI/PI/PE/JE/DN/PR/SE/POS Invoice/Dunning/
    Stock Reconciliation already carry.
  * ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 522 lines of
    ``landed_cost_voucher.py``: the only methods defined are ``get_items_from_purchase_receipts``,
    ``validate`` and its sub-validators, ``on_submit``/``on_cancel`` (both plain HOOKS, never
    overrides of ``submit()``/``cancel()`` themselves), ``update_landed_cost``,
    ``update_claimed_landed_cost``, ``validate_asset_qty_and_status``,
    ``update_rate_in_serial_no_for_non_asset_items``, and ``get_vendor_invoice_amount`` — no
    ``def submit``/``def cancel`` anywhere. Unlike Journal Entry and Stock Reconciliation, the
    run_method vector never 403s for Landed Cost Voucher.
  * **This SUPPORTED_DOCTYPES entry equals Stock Entry's, field for field** — ``{"party_field":
    None, "submit_via": SUBMIT_VIA_RUN_METHOD, "date_field": "posting_date"}`` on both rows, the
    SECOND zero-diff config pairing this campaign has found (after Supplier Quotation/Purchase
    Order) — asserted directly by this landing's own tests. The list-tier SHAPE still differs (its
    own seventh branch, above): a config match is not a ``_list_fields`` match, the same
    Stock-Reconciliation-vs-Stock-Entry lesson repeated one doctype later.
  * **THE CENTRAL LEDGER-PREVIEW FINDING — Landed Cost Voucher has no ``make_gl_entries`` method
    anywhere in its class hierarchy, the Dunning shape, but for a DIFFERENT structural reason, and
    with a sharper consequence even if it WERE callable.** ``class LandedCostVoucher(Document)``
    (``landed_cost_voucher.py:22``) — it descends directly from frappe's base ``Document``, never
    ``AccountsController`` (Dunning's own ancestor) and never ``StockController`` (SO/PO/MR/SQ/Q/DN/
    PR/SE's ancestor). A full-file grep of ``landed_cost_voucher.py`` finds zero ``make_gl_entries``
    definition. ERPNext's own ``get_accounting_ledger_preview`` (``stock_controller.py:2090-2119``)
    calls ``doc.make_gl_entries()`` as a bare, unguarded method call (line 2112) — for a real Landed
    Cost Voucher this raises ``AttributeError`` on a live bench, exactly Dunning's own finding, so
    this broker skips the ``client.ledger_preview()`` network call entirely for this doctype too
    (the "skip" category, not "deferral-flag" or "incomplete-flag" — see
    :func:`pacioli.tools._landed_cost_voucher_ledger_preview_unavailable_flag`). **The sharper
    point: even a HYPOTHETICALLY working preview would describe the WRONG DOCUMENT.** A Landed Cost
    Voucher never posts a GL Entry or Stock Ledger Entry under its own ``voucher_type`` — full stop,
    regardless of whether ``make_gl_entries`` existed — because its real accounting effect is a
    REVALUATION of OTHER documents (below). This is a genuinely new shape beyond Dunning's own: for
    Dunning, an empty ``projected_gl`` (however it got there) matches the doctype's own real-world
    ledger truth (Dunning posts nothing, ever). For Landed Cost Voucher, an empty ``projected_gl``
    is accurate about THIS voucher and structurally SILENT about the real posting this submit is
    about to trigger elsewhere — the plan-tier disclosure must say so plainly, not merely explain
    the ``AttributeError``.
  * **``on_submit``/``on_cancel`` call the SAME ``update_landed_cost()`` — a real revaluation of
    OTHER submitted documents' ledgers, the FIRST Pacioli-supported doctype whose own submit/cancel
    modifies a different document's posted rows rather than only its own.**
    ``on_submit`` (289-292): ``validate_applicable_charges_for_item()``, then
    ``update_landed_cost()``, then ``update_claimed_landed_cost()``. ``on_cancel`` (294-296): the
    SAME ``update_landed_cost()`` and ``update_claimed_landed_cost()`` again (``self.docstatus``
    is 2 by then; ``update_landed_cost``'s own ``if self.docstatus != 2`` guard at line 311 only
    skips the asset-qty validation, not the reval itself). ``update_landed_cost()`` (307-350): for
    every row in ``purchase_receipts``, fetches the named receipt document
    (``Purchase Invoice``/``Purchase Receipt``/``Stock Entry``/``Subcontracting Receipt``),
    recalculates its item valuation via ``doc.update_valuation_rate(reset_outgoing_rate=False)``
    (or ``calculate_items_qty_and_amount()`` for a Subcontracting Receipt), writes the new rate back
    via a bare ``item.db_update()`` (no revalidation), THEN — the destructive-looking but
    documented-native part — temporarily flips the receipt's ``docstatus`` to 2, calls
    ``update_stock_ledger(..., via_landed_cost_voucher=True)`` + ``make_gl_entries_on_cancel()`` to
    REVERSE its existing Stock Ledger Entry/GL Entry rows, flips ``docstatus`` back to 1, and calls
    ``update_stock_ledger(...)`` + ``make_gl_entries(...)`` + ``repost_future_sle_and_gle(...)``
    again to REPOST them at the new valuation. Submitting an LCV raises the target receipts' item
    cost and their Stock/Asset GL balance; cancelling it reverses that raise — both directions run
    through the SAME method, on the SAME target documents, never on the LCV's own voucher.
    **Disclosure implication:** ``plan_submit``'s empty ``projected_gl`` and ``plan_cancel``'s empty
    ``projected_reversal`` (``get_gl_entries("Landed Cost Voucher", name)``, honestly empty because
    no GL row was EVER posted under this voucher_type) are BOTH accurate about this voucher and
    BOTH silent about the real, consequential repost happening to the named receipts — a NEW
    doctype-gated flag fires on both verbs (:func:`pacioli.tools.
    _landed_cost_voucher_ledger_preview_unavailable_flag` on submit,
    :func:`pacioli.tools._landed_cost_voucher_cancel_revaluation_flag` on cancel) naming this
    plainly, rather than letting either verb's honest local emptiness read as "nothing happens".
  * **THE DYNAMIC-LINK CORRECTION — the sharpest finding of this landing.** The dossier (§6) claims
    ``Landed Cost Purchase Receipt.receipt_document`` (Dynamic Link, ``options:
    "receipt_document_type"``, ``landed_cost_purchase_receipt.json`` lines 25-36) is
    "structurally invisible" to this broker's ``get_submitted_linked_docs`` blast-radius walk
    because "the schema scanner has no static edge to traverse" — and Purchase Receipt's own EARLIER
    landing (commit b2d06a9, before Dunning's own correction below existed) repeats the identical
    claim as a documented "orphan hazard". **Traced directly against frappe's ``linked_with.py``
    (version-16) rather than assumed, both are WRONG, in the exact shape Dunning's own landing
    already corrected for a different doctype's Dynamic Link:** ``get_references_across_doctypes_
    by_dynamic_link_field`` (``linked_with.py:269-321``) is NOT a static schema scan — for every
    DocField with ``fieldtype == "Dynamic Link"`` on a doctype reachable from the submittable-
    doctype universe (which includes every CHILD TABLE of a submittable doctype, via
    ``get_child_tables_of_doctypes``, ``linked_with.py:158-189`` — ``Landed Cost Purchase Receipt``
    qualifies because ``landed_cost_voucher.json``'s own ``purchase_receipts`` field is
    ``fieldtype: "Table", options: "Landed Cost Purchase Receipt"`` on the submittable ``Landed
    Cost Voucher``), it runs a LIVE query for the DISTINCT values actually present in the sibling
    type-selector column (``frappe.get_all("Landed Cost Purchase Receipt", pluck=
    "receipt_document_type", distinct=1)``, line 308-313) and registers each one as a real link
    target. So the moment any Landed Cost Voucher exists referencing a Purchase Receipt (the very
    row being asked about), that row's own ``receipt_document_type="Purchase Receipt"`` value makes
    the edge discoverable, and ``get_referencing_documents`` (324-364) applies the SAME
    child-table-to-parent ``parenttype``/``docstatus=1`` promotion a plain Link field gets —
    resolving ``Landed Cost Purchase Receipt`` rows up to their submitted ``Landed Cost Voucher``
    parent. ``Landed Cost Voucher`` is not in ``erpnext/hooks.py``'s ``auto_cancel_exempted_
    doctypes`` (only ``"Payment Entry"``, line 428-430), so it is a valid ``allowed_parent``.
    **Consequence: a submitted Landed Cost Voucher referencing a Purchase Receipt (or Purchase
    Invoice, Stock Entry, or Subcontracting Receipt) genuinely surfaces in
    ``get_submitted_linked_docs(<receipt doctype>, <receipt name>)``'s response — this broker's
    PRE-EXISTING generic refusal (any non-empty result denies a ``plan_cancel``) already REFUSES to
    cancel a receipt with a submitted LCV against it, with zero new code.** This makes Pacioli's own
    governance MORE protective than raw ERPNext here: ERPNext's native ``check_next_docstatus``
    (Purchase Receipt's own on-cancel refusal, per that doctype's landing) checks ONLY for a
    submitted Purchase Invoice, never an LCV — ERPNext itself would let the receipt cancel and
    leave the LCV's own totals silently stale, a real orphan hazard IN ERPNEXT'S OWN DOMAIN MODEL —
    but this broker's disclosure layer catches it first and refuses the plan outright. The "orphan
    hazard" framing survives as a true statement about ERPNext's native behavior; the "structurally
    invisible to this broker" framing does not, and is corrected here exactly as Dunning's own
    landing corrected an analogous claim about Payment Entry Reference's Dynamic Link.
    ``Landed Cost Item.receipt_document`` (``landed_cost_item.json``, the SAME Dynamic Link shape,
    a denormalized per-line copy) rides the identical mechanism as a second, redundant edge.
  * **A genuinely NEW edge the dossier missed entirely: ``Landed Cost Vendor Invoice.vendor_invoice``
    is a REAL, plain ``Link`` field to Purchase Invoice** (``landed_cost_vendor_invoice.json`` lines
    9-16: ``"fieldtype": "Link", "options": "Purchase Invoice"``, not ``reqd``) — the optional
    "claim this Purchase Invoice's cost as already landed" path (``get_vendor_invoice_amount``/
    ``update_claimed_landed_cost``), entirely separate from the ``purchase_receipts`` reval table
    above. This needs no Dynamic-Link reasoning at all — it is covered by the ordinary
    ``get_references_across_doctypes_by_link_field`` walk every prior doctype's plain Link edges
    already ride, and it means a submitted Purchase Invoice can be discovered as an LCV dependent
    through TWO independent paths (a receipt-typed reval reference AND/OR a vendor-invoice claim).
  * **The REVERSE direction — cancelling the LCV itself — finds NO inbound dependents, correctly,
    but "correctly empty" here does not mean "safe".** Grepping the full v16 checkout for
    ``'"options": "Landed Cost Voucher"'`` returns exactly one hit: its own self-referencing
    ``amended_from`` (line 116, skipped by the tree walk's own ``if field["fieldname"] ==
    "amended_from": continue``, ``linked_with.py:112-114``). Nothing else links TO a Landed Cost
    Voucher — ``get_submitted_linked_docs("Landed Cost Voucher", name)`` is genuinely, honestly
    empty, so ``plan_cancel``'s blast-radius check never refuses an LCV cancel on this doctype's own
    account. **This is the correct answer to the WRONG question for this doctype**: the blast-radius
    walk asks "what depends on this LCV existing" (nothing), not "what does this LCV's own on_cancel
    hook rewrite" (the named receipts' Stock Ledger Entry + GL Entry rows, unconditionally, per the
    ``update_landed_cost()`` finding above) — a structurally different kind of hazard the Link-graph
    mechanism was never built to see, in EITHER direction, for a document whose economic effect is
    outbound revaluation rather than inbound reference. :func:`pacioli.tools.
    _landed_cost_voucher_cancel_revaluation_flag` names this directly at plan_cancel time.
  * **Cascade — ``cascade.py`` needs NO changes** (doctype-blind by construction, the same finding
    every prior breadth increment has made) — the corrected Dynamic Link/Link discovery above is
    entirely frappe's own generic mechanism, already exercised through the existing "any non-empty
    ``get_submitted_linked_docs`` result denies the plan" refusal; no doctype-specific string
    literal is needed to reach it.
  * Landed Cost Voucher is also named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list
    (line 341, alongside Delivery Note/Purchase Receipt/Stock Reconciliation) — this broker's
    existing closed-books check needs no change to apply to a locked LCV posting period.
  * **The "Get Items" population workflow** (``get_items_from_purchase_receipts``,
    ``@frappe.whitelist()``, lines 55-77) and ``validate_line_items``/``validate_receipt_documents``
    (97-176) enforce that every ``items`` row roots to a real, submitted receipt item before submit
    — there is no "freestanding LCV line" pattern this broker needs to model; ``validate_receipt_
    documents`` also refuses submit outright if a named receipt is not itself submitted or belongs
    to a different company (an ERPNext-native refusal this broker's own company-pin check already
    parallels, never bypassed).

**Breadth (Request for Quotation) — Wave 3's first row, the seventeenth supported doctype.** Full
source-cited finding below (dossier at ``docs/plans/dossiers/request_for_quotation.md`` — correct
on party_field/status/grand_total/date_field/submit_via, but its own §4 hedge on the ledger_preview
category needed direct verification, not a repeat, since the last six landings each found a
source-level dossier error). Summary of what lands in this row:

  * ``party_field=None`` — RFQ dispatches to MULTIPLE suppliers via a required child table
    (``suppliers``, ``Table -> Request for Quotation Supplier``, ``reqd: 1``,
    ``request_for_quotation.json`` lines 114-121), never a single header party. The one
    supplier-shaped header field, ``vendor`` (``Link -> Supplier``, lines 79-89), is confirmed
    ``hidden: 1``/``read_only: 1``/NOT ``reqd`` and described "For individual supplier" — it is
    set programmatically only by ``before_print``/``update_supplier_part_no`` (request_for_
    quotation.py:168-175, 218-223) to pick one supplier for a single-supplier PDF download; on a
    fresh or multi-supplier RFQ it is blank. Splicing it into the party slot would be misleading
    (mostly null, and wrong the moment more than one supplier is on the RFQ) — the same
    "self-clearing, not a stable counterparty" judgment Material Request's own ``customer`` field
    already earned this campaign's ``party_field=None`` treatment.
  * ``status`` (Select, ``\nDraft\nSubmitted\nCancelled``, ``read_only: 1``, ``reqd: 1``, lines
    230-244) IS present, set via ``db_set`` in ``on_submit``/``on_cancel`` and ``validate`` (Draft
    while ``docstatus < 1``). ``grand_total`` is confirmed ABSENT across the full 384-line field
    enumeration — no field name containing "total" anywhere; RFQ carries ``items`` (qty/
    description for quotation) but no rate/amount column at the header level, cost-blind by
    design (a procurement REQUEST, not a priced document). **This is the identical
    party=None/status-present/grand_total-absent SHAPE Material Request's own branch was built
    for** — see :func:`_list_fields` below for why this does NOT mean a literal reuse of that
    branch.
  * ``date_field="transaction_date"`` (``default: "Today"``, ``reqd: 1``, lines 98-108) —
    confirmed NO ``posting_date`` field anywhere in the 384 fields. Rides the existing
    ``date_field`` mechanism unchanged (the sixth doctype on this pattern, joining Sales/Purchase
    Order/Material Request/Supplier Quotation/Quotation).
  * ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 675 lines of
    ``request_for_quotation.py``: ``on_submit`` (161-166) and ``on_cancel`` (177-178) are bare,
    undecorated hook overrides (no ``@frappe.whitelist()`` anywhere near them — the file's eight
    ``@frappe.whitelist()`` occurrences all sit on separate utility RPC endpoints:
    ``get_supplier_email_preview``, module-level ``send_supplier_emails``/
    ``make_supplier_quotation_from_rfq``/``create_supplier_quotation``/``get_pdf``/
    ``get_item_from_material_requests_based_on_supplier``/``get_supplier_tag``/
    ``get_rfq_containing_supplier``, none of them ``submit``/``cancel`` itself); no ``def submit``/
    ``def cancel`` override exists at all. The run_method vector never 403s for RFQ, the same
    finding shape as every non-Journal-Entry/non-Stock-Reconciliation doctype so far.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED DIRECTLY RATHER THAN ASSUMED: RFQ is the SO/PO/MR/SQ/Q
    "honest-empty" category, NOT the Dunning/LCV "uncallable" category — a genuinely different
    answer from what a surface reading of the dossier's own §4 might suggest.** The dossier's own
    text ("It inherits BuyingController, which does not override make_gl_entries") is TRUE but
    incomplete on the one axis that matters: ``class RequestforQuotation(BuyingController)``
    (request_for_quotation.py:26) is the SAME declared parent Purchase Order/Material Request/
    Supplier Quotation already carry (confirmed: ``class PurchaseOrder(BuyingController)``,
    ``class MaterialRequest(BuyingController)``, ``class SupplierQuotation(BuyingController)``),
    and reading the controller chain itself (not assuming it) shows ``BuyingController``
    extends ``SubcontractingController`` (``buying_controller.py:33``), which extends
    ``StockController`` (``subcontracting_controller.py:27``), which defines a REAL
    ``make_gl_entries`` (``stock_controller.py:292``) — the identical MRO every "honest-empty"
    doctype in this campaign already rides. Unlike Dunning (``AccountsController`` only, never
    ``StockController``) and Landed Cost Voucher (bare ``Document``, no controller ancestry at
    all), RFQ genuinely INHERITS a callable ``make_gl_entries`` — ``get_accounting_ledger_
    preview``'s bare ``doc.make_gl_entries()`` call (``stock_controller.py:2112``) does not
    AttributeError for this doctype, so this broker's ``ledger_preview`` RPC is safely callable and
    is NOT added to the ``(DUNNING, LANDED_COST_VOUCHER)`` skip tuple in ``tools.py``. The
    emptiness itself is honest, not merely non-crashing: ``make_gl_entries``'s own body
    (``stock_controller.py:292-319``) only builds ``gl_entries`` via ``self.get_gl_entries(...)``
    when ``need_inventory_map`` (perpetual inventory + stock items present) is true, and even then
    ``get_gl_entries`` (line 726) sources its rows from ``self.get_stock_ledger_details()`` — a
    query over REAL, already-written ``Stock Ledger Entry`` rows for this voucher. RFQ never calls
    ``update_stock_ledger()``/writes an SLE anywhere in its 675 lines (confirmed by full-file
    grep), so that query always returns empty and ``get_gl_entries`` returns ``[]`` unconditionally
    — the same honest-empty mechanism SO/PO/MR/SQ/Q already ride, now confirmed for a seventeenth
    doctype rather than assumed from the dossier's hedge alone.
  * **``on_submit`` sends real email to suppliers — an external-communication side effect, not a
    ledger one, documented here for plan-tier disclosure (the same discipline Material Request's
    own on-hold/closed cancel-refusal gap and Dunning's dead-weight ``ignore_linked_doctypes`` got:
    named in prose, no new machinery built for it).** ``on_submit`` (161-166) unconditionally calls
    ``self.send_to_supplier()`` (192-206) after the status write; for every row in ``suppliers``
    with ``send_email`` truthy and an ``email_id``, it creates/links a Contact and (if needed) a
    portal User, renders a subject/message via ``frappe.render_template()`` (from an ``Email
    Template`` or the row defaults), and dispatches through
    ``frappe.core.doctype.communication.email.make(..., send_email=True)`` — a real outbound email,
    not a queued draft. This is NOT reversed on cancel (``on_cancel`` is the bare ``db_set(
    "status", "Cancelled")`` shown above, nothing else). No broker gate is needed or appropriate:
    this is ERPNext's own documented design for the doctype (an RFQ's entire purpose is dispatching
    to suppliers), not a defect this broker's plan/consent machinery could safely preview (email
    delivery depends on live Contact/User/Email Account state this broker never reads) or should
    refuse (blocking it would break the doctype's own function). ``plan_submit``'s disclosure
    should simply say a submit dispatches supplier email — informational, not a risk flag, the same
    treatment Purchase Order's own on-hold check note and Material Request's own hold/closed gap
    got: named plainly in this comment, not built into new runtime code.
  * **Cascade — a minor dossier correction, harmless to the conclusion.** Grepping the full v16
    checkout for ``'"options": "Request for Quotation"'`` returns exactly two hits: RFQ's own
    self-referencing ``amended_from`` (line 250) and ``supplier_quotation_item.request_for_
    quotation`` (``supplier_quotation_item.json``, ``fieldtype: "Link"``, ``options: "Request for
    Quotation"``, ``read_only: 1``) — a real, plain Link, the ONE external dependent edge. **The
    dossier's own §7 additionally claims ``supplier_quotation_item.request_for_quotation_item`` is
    itself "Link -> Request for Quotation Item"** — dumping the raw field dict directly shows this
    is WRONG: that field's ``fieldtype`` is ``"Data"`` (a hidden, no-copy string, not a Link at
    all). This does not change the cascade conclusion — the ONE real Link
    (``request_for_quotation``) is already sufficient for ERPNext's own
    ``get_submitted_linked_docs`` to walk the child-table-to-parent promotion generically (the same
    mechanism every prior doctype's cascade edges ride) — a submitted Supplier Quotation built from
    this RFQ is discovered and ordered ahead of it on cancel with zero doctype-specific
    ``cascade.py`` code, the same finding every prior breadth increment has made. RFQ's own
    ``on_cancel`` carries no ``ignore_linked_doctypes`` list and no ``frappe.throw`` anywhere in the
    file — a bare status write, refusing on nothing of its own.
  * Request for Quotation is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list
    (confirmed: only Sales/Purchase Invoice, Journal Entry, Bank Clearance, Stock Entry, Dunning,
    Invoice Discounting, Payment Entry, Period Closing Voucher, Process Deferred Accounting, the
    Asset trio, Delivery Note, Landed Cost Voucher, Purchase Receipt, Stock Reconciliation,
    Subcontracting Receipt) — consistent with a doctype that never posts to a period at all; the
    existing closed-books check simply never matches this doctype, no change needed.

**Breadth (Blanket Order) — Wave 3's second row, the eighteenth supported doctype.** Full
source-cited finding below (dossier at ``docs/plans/dossiers/blanket_order.md`` — correct on
status/grand_total absence, date_field, submit_via, and the ledger_preview "skip" category, but
WRONG on one cascade edge — the ninth landing in a row to find at least one dossier error rather
than trust it blind). Confirmed from source (``blanket_order.json`` (178 lines) + ``blanket_order.py``
(201 lines), version-16 checkout, both read 2026-07-21):

  * **``party_field=None`` — Blanket Order carries BOTH ``customer`` and ``supplier`` as real header
    Link fields, gated client-side only, never server-enforced.** ``customer`` (Link -> Customer,
    ``depends_on: eval:doc.blanket_order_type == "Selling"``, lines 44-50) and ``supplier`` (Link ->
    Supplier, ``depends_on: eval:doc.blanket_order_type == "Purchasing"``, lines 59-65) — NEITHER
    carries ``reqd: 1`` at the schema level (confirmed by dumping both raw field dicts). The
    ``depends_on`` gate is a pure client-side JS eval (frappe never enforces ``depends_on`` as a
    server-side constraint); reading all 201 lines of ``blanket_order.py`` confirms
    ``set_party_item_code()`` (lines 53-64, called from ``validate()``) dispatches on
    ``self.blanket_order_type`` to route item-code lookups, but never validates that ``customer`` XOR
    ``supplier`` is actually populated — a blank Blanket Order with both fields empty passes
    ``validate()`` without error; the JS toggle (``set_tc_name_filter``, referenced by the dossier) is
    the ONLY thing standing between a user and a genuinely partyless document. This is a WEAKER
    server-side guarantee than Material Request's own explicit ``self.customer = None`` clearing —
    still ``party_field=None`` (no single static fieldname is ever unconditionally the party), but
    for a different reason than Quotation's Dynamic Link pair: here TWO real static Link fields exist
    and either (both, neither, or one) could in principle be populated. **List-tier disclosure
    recommendation: splice ``blanket_order_type`` alongside BOTH ``customer`` AND ``supplier`` as
    context columns** (see :func:`_list_fields` below) — closer to Quotation's own
    type-plus-resolved-party treatment than to Request for Quotation's own bare, partyless branch,
    because (unlike RFQ's one-to-many child-table supplier data) Blanket Order's party genuinely
    lives on two scalar header fields, just conditionally which-one-is-live.
  * **``status`` field: CONFIRMED ABSENT — zero hits across the full 18-field enumeration in
    ``blanket_order.json``.** Docstatus (Draft/Submitted/Cancelled) is the only lifecycle signal;
    unlike Material Request (has ``status``) and like Stock Entry/Stock Reconciliation/Landed Cost
    Voucher (all three also lack it).
  * **``grand_total`` field: CONFIRMED ABSENT — zero hits across the same enumeration.** Blanket
    Order is a cost-blind rate/qty template (``items[].qty``/``items[].rate`` live only on the child
    table, ``Blanket Order Item``, never aggregated to a parent-level total) — the same absence shape
    as Stock Entry/Stock Reconciliation/Landed Cost Voucher, but (per the party finding above) NOT
    the same absence shape on the party axis (those three splice no party-ish column at all; Blanket
    Order's own party genuinely exists as two gated scalars).
  * **This is the NINTH ``_list_fields`` branch — no reuse of Stock Entry/Stock
    Reconciliation/Landed Cost Voucher's own party=None+status-absent+grand_total-absent shape**, for
    the same "substitute fields differ" reason every one of those three was not a reuse of its
    same-shape predecessor, and ALSO because — uniquely among the four — Blanket Order's own
    substitute set includes real party context (``customer``/``supplier``), which none of the other
    three carry at all. Confirmed substitute fields: ``blanket_order_type`` (Select, ``"Selling"``/
    ``"Purchasing"``, ``in_list_view: 1``, ``reqd: 1``, lines 36-43 — the type-context column, the
    same role ``purpose``/``distribute_charges_based_on`` play for Stock Entry/Stock
    Reconciliation/Landed Cost Voucher) and ``to_date`` (Date, ``reqd: 1``, ``allow_on_submit: 1``,
    lines 84-90 — the second half of the validity window, paired with ``from_date`` which already
    rides as ``date_field``; the nearest thing this schema has to a summary/completion metric,
    standing in for the missing ``grand_total`` slot the way ``per_ordered``/``per_received`` do for
    Material Request).
  * **``date_field="from_date"`` (Date, ``reqd: 1``, no default, lines 78-82) — confirmed NO
    ``posting_date`` field anywhere in the 18 enumerated fields, and no ``transaction_date`` field
    either** (unlike Sales/Purchase Order/Material Request/Supplier Quotation/Quotation/RFQ, which
    all use ``transaction_date`` — Blanket Order is the FIRST doctype on neither of those two
    patterns). ``from_date`` is the validity window's start — required, no ``allow_on_submit``
    (frozen once submitted), the natural closed-books effective date. ``to_date`` (the window's end)
    DOES carry ``allow_on_submit: 1`` — the only other field with that flag is the optional
    ``order_date`` — but is never used as ``date_field``: a closed-books check keys off when the
    agreement STARTS, not when it ends.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 201 lines of
    ``blanket_order.py``: no ``def submit``/``def cancel`` override anywhere, only ``validate()``
    (lines 43-47, no submit-specific work) and the base ``Document.submit()``/``.cancel()`` apply.**
    No ``on_submit``/``on_cancel`` method is defined in the class AT ALL — the dossier's "no
    on_submit hook at all" claim is CORRECT (a genuine rarity across this campaign's landings; even
    Dunning/RFQ/Quotation define at least one of the two).
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO RATHER THAN ASSUMED: Blanket Order is the
    Dunning/Landed Cost Voucher "uncallable" category, NOT the SO/PO/MR/SQ/Q/RFQ "honest-empty"
    one.** ``class BlanketOrder(Document)`` (``blanket_order.py:15``) — a direct ``Document``
    subclass, never ``AccountsController``, never ``StockController`` (confirmed: no other class
    named in the file, no import of either controller). A full-tree grep for ``def make_gl_entries``
    across every ``.py`` file in ``erpnext/`` (the same search Dunning's and Landed Cost Voucher's
    own landings ran) finds it defined ONLY on ``StockController`` and on the short, closed list of
    individual doctype controllers that each define their own copy — ``BlanketOrder`` shares no
    ancestry with any of them, identical to Landed Cost Voucher's own finding (bare ``Document``, no
    controller ancestry at all) and structurally closer to LCV's shape than to Dunning's
    (``AccountsController`` at least). ERPNext's own ``get_accounting_ledger_preview``
    (``stock_controller.py:2090-2119``) calls ``doc.make_gl_entries()`` as a bare, unguarded method
    call — for a real Blanket Order this raises ``AttributeError`` on a live bench, refusing EVERY
    ``plan_submit`` if the call were made. **Fix: joins the ``(DUNNING, LANDED_COST_VOUCHER)`` skip
    tuple in ``tools.py``, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER)``**, with its own
    honest ``_blanket_order_ledger_preview_unavailable_flag``. **Unlike Landed Cost Voucher, Blanket
    Order's empty preview is NOT also silent about a real posting elsewhere** — Blanket Order has no
    ``update_landed_cost()``-shaped side effect that rewrites another submitted document's ledger; its
    own submit posts nothing, anywhere, ever (see the cascade finding below for the ONE thing its
    submit/cancel does touch, and it is a quantity counter, never a ledger row) — so this is the
    simpler Dunning shape (one flag, submit-direction only), not the dual-flag LCV shape.
    ``plan_cancel`` needs no equivalent new flag: its ``projected_reversal`` comes from
    ``get_gl_entries(doctype, name)``, a real, safe bench read that naturally, honestly returns empty
    for Blanket Order (no GL row was ever written under this voucher_type, in either direction) — the
    pre-existing generic "no live GL rows found for this voucher — nothing visible to unwind" flag
    already says so, unchanged, the same finding Dunning's own landing made.
  * **``on_cancel`` — no method defined at all (confirmed above); Blanket Order's own cancel has NO
    refusal gate and NO direct side-effect of its own.** The dossier's own §6 heading ("update_
    ordered_qty() call on CANCEL ONLY, not on submit") is imprecise and worth correcting here: the
    quantity-tracking side effect it describes is not triggered by Blanket Order's OWN cancel at
    all — it is triggered by a REFERENCING Sales Order/Purchase Order's own submit AND cancel (both
    directions, not cancel-only), via ``StockController.update_blanket_order()``
    (``controllers/stock_controller.py:1583-1586`` — confirmed inherited by both ``SalesOrder`` and
    ``PurchaseOrder``, called from each one's own ``on_submit``/``on_cancel``,
    e.g. ``purchase_order.py:454``/``501``), which calls THIS Blanket Order's own
    ``update_ordered_qty()`` (``blanket_order.py:97-119``) to recompute each item row's
    ``ordered_qty`` counter from a live query against the referencing SO/PO table. This is entirely
    an UPSTREAM effect (a referencing SO/PO's lifecycle change reaches into this Blanket Order, never
    the reverse) and never a ledger write — a Bin-style quantity counter, not GL/SL. Cancelling a
    Blanket Order itself never touches this counter, never touches the referencing SO/PO, and is
    never refused by ERPNext regardless of how many submitted SO/PO items still reference it.
  * **Cascade — a genuine dossier correction, not merely a repeat.** Grepping the full v16 checkout
    for ``'"options": "Blanket Order"'`` filtered to real ``fieldtype: "Link"`` hits (not merely a
    string match on the file) returns THREE external Link fields, not the dossier's claimed two:
    ``sales_order_item.json`` (``blanket_order``, ``depends_on: eval:doc.against_blanket_order``) and
    ``purchase_order_item.json`` (same shape) — both correctly named by the dossier — PLUS
    ``quotation_item.json``'s own ``blanket_order`` field, which the dossier's §7 dismisses as "NOT a
    Link reference — child-table membership only." Dumping the raw field dict directly
    (``quotation_item.json``) shows this is WRONG: ``{"depends_on": "eval:doc.against_blanket_order",
    "fieldname": "blanket_order", "fieldtype": "Link", "options": "Blanket Order", ...}`` — a real
    Link field, byte-for-byte the same shape as the SO/PO Item fields the dossier got right. This
    does not change the cascade CONCLUSION (``cascade.py`` needs no changes regardless — it is fully
    doctype-blind, and ERPNext's own ``get_submitted_linked_docs`` walks all three child-table Link
    fields generically via the same child-table-to-parent promotion every prior landing's cascade
    edges already ride), but it DOES widen the real disclosure surface: a submitted Quotation
    referencing this Blanket Order (via ``quotation_item.blanket_order``) is ALSO discoverable by the
    blast-radius check, not just a submitted Sales Order or Purchase Order — covered by the existing
    generic refusal with zero new code, the same "no disclosure gap, just a wider one than claimed"
    shape Dunning's own Payment Entry correction and RFQ's own supplier_quotation_item correction
    already established.
  * Blanket Order is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list (confirmed
    absent from the same 18-entry list RFQ's own landing enumerated) — consistent with a doctype that
    never posts to a period at all.

**Breadth (Job Card) — Wave 3's third row, the nineteenth supported doctype.** Full source-cited
finding below (dossier at ``docs/plans/dossiers/job_card.md`` — correct on party/status/grand_total
absence, date_field, submit_via, and the ledger_preview "uncallable" category, but its own §7
undersells one cascade edge — the tenth landing in a row to find at least one dossier imprecision
rather than trust it blind). Confirmed from source (``job_card.json``, 95 fields enumerated, +
``job_card.py``, 1875 lines, version-16 checkout, both read 2026-07-21):

  * **``party_field=None`` — Job Card is a shop-floor operation record and carries no
    customer/supplier/party field of any kind.** Confirmed by enumerating all 95 fields in
    ``job_card.json``: zero fields named ``customer``, ``supplier``, ``party``, or any other
    Customer/Supplier Link at the header level — unlike Blanket Order's own two gated party
    fields, Job Card has no party concept to gate at all, the Journal-Entry/Stock-Entry/Stock-
    Reconciliation/Landed-Cost-Voucher/RFQ shape, not the Quotation/Blanket-Order one.
  * **``status`` field: CONFIRMED PRESENT** (Select, ``job_card.json`` field dict, default
    ``"Open"``) **with exactly the dossier's claimed 8 options** — ``Open``, ``Work In Progress``,
    ``Partially Transferred``, ``Material Transferred``, ``On Hold``, ``Submitted``, ``Cancelled``,
    ``Completed`` — confirmed byte-for-byte from the raw field dict. (The dossier's own "in_list_view
    flags confirm these" gloss overstates one thing: ``status`` itself does NOT carry
    ``in_list_view: 1`` in the schema — only ``work_order``/``workstation``/``operation``/
    ``for_quantity`` do — but ``status`` still rides the list tier below by the same standing
    convention every other supported doctype's own ``status`` column already follows, in_list_view
    flag or not.)
  * **``grand_total`` field: CONFIRMED ABSENT** — no field name containing "total" carries a
    financial meaning anywhere in the 95-field enumeration (only ``total_completed_qty`` and
    ``total_time_in_mins``, both non-monetary Float counters); no ``accounts`` child table, no
    rate/amount field at the header level (``hour_rate`` is a per-hour Currency rate, not an
    aggregate). Cost-blind by design, the same shape Material Request/Stock Entry/Stock
    Reconciliation/Landed Cost Voucher/RFQ/Blanket Order all share on this one axis.
  * **This party=None + status-present + grand_total-absent combination is the SAME SHAPE as
    Material Request and Request for Quotation — and, per the same "substitutes differ" discipline
    every prior same-shape branch in this campaign has followed, still NOT a reuse of either.**
    Material Request's own substitutes (``material_request_type``, ``per_ordered``,
    ``per_received``) and RFQ's own bare, substitute-free branch are both confirmed absent/
    inapplicable here: Job Card's genuinely different context columns are ``work_order`` (Link,
    ``reqd: 1``, ``in_list_view: 1`` — the parent manufacturing document this Job Card operates
    against), ``operation`` (Link, ``reqd: 1``, ``in_list_view: 1`` — which BOM operation this card
    covers), and ``for_quantity`` (Float, ``in_list_view: 1`` — the target quantity), none of which
    exist on ``material_request.json`` or ``request_for_quotation.json`` at all — the ELEVENTH
    ``_list_fields`` branch (see below).
  * **``date_field="posting_date"`` (Date, default ``"Today"``, not ``reqd``) — a real field,
    confirmed present, KEEPS the default** — Job Card is the first Wave-3 doctype on the same
    literal fieldname SI/PI/PE/JE/Dunning/Stock Reconciliation/Landed Cost Voucher already carry
    (never ``transaction_date`` like SO/PO/MR/SQ/Q/RFQ, never ``from_date`` like Blanket Order).
    No new ``date_field`` plumbing needed at all — the plainest date-field landing since Dunning's.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 1875 lines of ``job_card.py``:
    ``class JobCard(Document):`` (line 60) overrides neither ``submit()`` nor ``cancel()``
    anywhere.** Only ``on_submit`` (line 776) and ``on_cancel`` (line 783) hooks are defined,
    called by the base ``Document.submit()``/``.cancel()`` — the same run_method doc-method surface
    every non-Journal-Entry/non-Stock-Reconciliation doctype already rides. Job Card ALSO defines 14
    separate ``@frappe.whitelist()``-decorated callables (6 instance methods, 8 module-level
    functions — see the side-surface note below) — none of these are ``submit``/``cancel``
    overrides; they are independent RPC surfaces this broker does not touch.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: Job Card is the Dunning/Landed Cost
    Voucher/Blanket Order "uncallable" category, NOT the SO/PO/MR/SQ/Q/RFQ "honest-empty" one.**
    ``class JobCard(Document)`` (``job_card.py:60``) — a direct ``Document`` subclass, never
    ``AccountsController``, never ``StockController``. Confirmed by the import block
    (``job_card.py:26-29``): ``from erpnext.controllers.stock_controller import
    (QualityInspectionNotSubmittedError, QualityInspectionRejectedError)`` — only two EXCEPTION
    classes are imported from ``stock_controller``, never the ``StockController`` class itself, and
    a full-file grep finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry``/
    ``StockLedgerEntry`` reference anywhere in ``job_card.py``. The same full-tree ``def
    make_gl_entries`` grep every prior "uncallable" landing (Dunning/LCV/Blanket Order) has run
    confirms the method is defined only on ``StockController`` and a short, closed list of
    individual doctype controllers (``PurchaseInvoice``, ``SalesInvoice``, ``PaymentEntry``,
    ``JournalEntry``, ``Asset``, ``AssetCapitalization``, ``AssetRepair``,
    ``PeriodClosingVoucher``, ``InvoiceDiscounting``) — ``JobCard`` shares no ancestry with any of
    them, byte-for-byte the same MRO shape ``BlanketOrder(Document)`` already established. ERPNext's
    own ``get_accounting_ledger_preview`` (``stock_controller.py:2090-2119``) calls
    ``doc.make_gl_entries()`` as a bare, unguarded method call — for a real Job Card this raises
    ``AttributeError`` on a live bench, refusing EVERY ``plan_submit`` outright if called. **Fix:
    joins the ledger_preview skip tuple in ``tools.py``, now ``(DUNNING, LANDED_COST_VOUCHER,
    BLANKET_ORDER, JOB_CARD)``**, with its own honest
    ``_job_card_ledger_preview_unavailable_flag``. Like Blanket Order (and unlike Landed Cost
    Voucher), Job Card has no ``update_landed_cost()``-shaped side effect that rewrites some OTHER
    submitted document's ledger — its own submit posts nothing, anywhere, ever (real GL/SL posting
    happens later, when a Stock Entry ``make_stock_entry``/``make_stock_entry_for_semi_fg_item``
    creates and someone submits is itself submitted) — so this is the simpler Dunning/Blanket-Order
    shape, one flag, submit-direction only; ``plan_cancel`` needs no equivalent new flag:
    ``get_gl_entries(doctype, name)`` is a real, safe bench read that naturally, honestly returns
    empty for Job Card (no GL row was ever written under this voucher_type).
  * **``on_cancel`` (``job_card.py:783-785``) calls ``update_work_order()`` (954-981) and
    ``set_transferred_qty()`` (1174-1203) — the SAME two methods ``on_submit`` calls (776-781) —
    both a Work Order operation/quantity RESET, never a ledger reversal.**
    ``update_work_order()`` recomputes the linked Work Order Operation's ``completed_qty``/
    ``process_loss_qty``/``pending_qty``/``actual_operation_time`` from a live query across all Job
    Cards for that operation, updates ``Work Order.produced_qty`` when a finished good is carried,
    and (for a corrective Job Card) sums the corrective cost back into the Work Order's
    ``corrective_operation_cost`` — with ``wo.flags.ignore_validate_update_after_submit = True`` set
    deliberately (an intentional operational bypass of the Work Order's own submitted-doc
    revalidation, not a defect). ``set_transferred_qty()`` recomputes ``transferred_qty`` from
    submitted Material-Transfer Stock Entries and may roll the Job Card's own ``status`` back from
    "Material Transferred" toward "Open" — never touches a GL Entry, a Payment Ledger row, or any
    settled-payment state. No ``frappe.throw`` appears anywhere in ``job_card.py``'s cancel path —
    confirmed no refusal gate of its own on cancel.
  * **Cascade — the dossier's own §7 undersells one edge rather than getting it wrong outright: a
    genuine clarification, the tenth landing in a row to find at least one dossier imprecision.**
    Grepping the full v16 checkout for ``'"options": "Job Card"'`` filtered to real ``fieldtype:
    "Link"`` hits returns exactly the dossier's claimed five external doctypes — ``Stock Entry``,
    ``Material Request``, ``Subcontracting Receipt Item``, ``Subcontracting Order Item``,
    ``Purchase Order Item`` — plus Job Card's own ``amended_from``/``for_job_card`` self-links.
    **But the dossier's own §7 entry for Material Request reads differently in KIND from the other
    four: it describes MR only as "(type-conditional): May be created from Job Card via
    ``make_material_request()`` whitelist" — a document-creation-lineage framing — while the other
    four are each stated plainly as "Carries `job_card` Link field."** Dumping
    ``material_request.json``'s raw field dict directly shows Material Request's own ``job_card``
    field is BYTE-FOR-BYTE the same shape as the other four (``fieldtype: "Link"``, ``options: "Job
    Card"``, ``read_only: 1``) — a real, standing header-level Link, not merely an artifact of the
    creation workflow the dossier's own wording implies. (``material_request.py:753-775`` confirms
    the mapper populates it automatically when ``make_material_request()`` builds the MR — the
    creation path is real too, just not the ONLY thing this field is.) This does not change the
    cascade CONCLUSION (``cascade.py`` needs no changes regardless — it is fully doctype-blind, and
    ERPNext's own ``get_submitted_linked_docs`` walks all five Link fields generically, the same
    child-table/header promotion every prior landing's cascade edges already ride) but corrects the
    framing: a submitted Material Request referencing this Job Card is discoverable by the
    blast-radius check on exactly the same footing as a submitted Stock Entry or Purchase Order
    Item, not a lesser "lineage only" relationship — the same "wider/plainer than claimed, never a
    gap" shape Dunning's own Payment Entry correction and Blanket Order's own Quotation correction
    already established.
  * **THE SIDE-SURFACE CAVEAT — genuinely new to this campaign, not a repeat of Work Order's own
    ``stop_unstop()`` single-method gap (docs/plans/dossiers/work_order.md §8), but the same SHAPE
    at a much larger scale.** Job Card exposes 14 separate ``@frappe.whitelist()`` callables outside
    the broker's own submit/cancel/amend 5-verb surface — 6 instance methods
    (``get_required_items``, ``pause_job``, ``resume_job``, ``start_timer``, ``complete_job_card``,
    ``make_stock_entry_for_semi_fg_item``) and 8 module-level functions (``make_time_log``,
    ``make_subcontracting_po``, ``make_material_request``, ``make_stock_entry``,
    ``make_corrective_job_card``, ``get_operation_details``, ``get_operations``,
    ``get_job_details``). **This broker's own scope — ``JOB_CARD: {"submit_via":
    SUBMIT_VIA_RUN_METHOD, ...}`` — governs ONLY ``submit``/``cancel`` (plus the generic
    ``amend``/``get``/``list`` wrapper verbs); it grants NOTHING toward any of these 14 methods.**
    Several mutate state or create new documents outside docstatus entirely —
    ``pause_job``/``resume_job``/``start_timer`` flip ``is_paused``/append time-log rows without
    touching ``docstatus`` at all (Work Order's own ``stop_unstop()`` shape, generalized to a whole
    family of state mutations rather than one); ``complete_job_card`` can auto-submit;
    ``make_stock_entry``/``make_stock_entry_for_semi_fg_item`` create (and can submit) a genuinely
    new Stock Entry; ``make_subcontracting_po``/``make_material_request``/
    ``make_corrective_job_card`` each create a new document of a DIFFERENT doctype. None of this is
    scoped by ``deploy/scope-doctypes.list`` (a resource-doctype allowlist for REST reads/writes,
    orthogonal to method-level RPC grants) or by this broker's own tool surface — a
    ``pacioli_guard``-scoped credential limited to ``Job Card.submit``/``Job Card.cancel`` does not
    thereby gain any of these 14 methods, and this landing deliberately builds NO tool for any of
    them (documented here plainly, the same discipline the dossier's own §8 "Broker Scope Note"
    already recommends, matching Work Order dossier's own §8 caveat and Pick List's own
    reservation-RPC caveat in TRIAGE.md).
  * Job Card is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list (confirmed
    absent from the same 18-entry list RFQ's/Blanket Order's own landings enumerated) — consistent
    with a doctype that never posts to a period at all.

**Breadth (BOM) — Wave 3's fourth row, the twentieth supported doctype, and THE FIRST DATELESS
ONE.** Full source-cited finding below (dossier at ``docs/plans/dossiers/bom.md`` — correct on the
critical date finding, party/status/grand_total absence, submit_via, and the on_submit/on_cancel
semantics, but carrying THREE imprecisions this landing corrects, the eleventh landing in a row to
find at least one rather than trust it blind: (1) its §3 cites a "157-field JSON scan" — the real
``bom.json`` carries 94 fields (157 is ``purchase_order.json``'s count, a copy-slip; the CONCLUSION
survives re-verification: zero date fields among the 94); (2) its §7 names ``Supplier Quotation``
as a BOM-referencing doctype — a full-tree scan of every DocType JSON for ``fieldtype: "Link"`` +
``options: "BOM"`` returns 29 fields across 25 doctypes and Supplier Quotation is NOT among them,
an outright wrong edge, not an undersell; (3) its §4 waves the non-``get_routing`` whitelist
methods through as "client RPC support (cost calculation, tree rendering, etc.)" — ``update_cost``
in fact MUTATES a SUBMITTED BOM, see the side-surface caveat below). Confirmed from source
(``bom.json``, 94 fields enumerated, + ``bom.py``, read 2026-07-21, version-16 checkout at
16.28.0):

  * **``party_field=None`` — BOM is a product recipe/structure, not a transaction.** Confirmed by
    enumerating all 94 fields: zero ``customer``/``supplier``/``party`` fields at any level. Its
    two required Links are ``item`` (the product this recipe manufactures) and ``company`` (the
    costing context) — the Journal-Entry/Stock-Entry/Stock-Reconciliation/LCV/RFQ/Job-Card shape.
  * **``status`` field: CONFIRMED ABSENT** — no Select status anywhere; ``docstatus`` alone tracks
    the lifecycle (the Journal-Entry/Stock-Entry/SR/LCV/Blanket-Order shape). The user-facing
    lifecycle signals are two ``allow_on_submit`` Check fields instead: ``is_active`` (offered as
    a manufacturing blueprint at all) and ``is_default`` (the Item's default recipe).
  * **``grand_total`` field: CONFIRMED ABSENT** — the aggregate that exists instead is
    ``total_cost`` (Currency, read-only): a VALUATION SNAPSHOT computed on validate
    (``calculate_cost()``), stored on the document, never posted anywhere.
  * **``date_field=None`` — THE CRITICAL FINDING, A GENUINELY NEW SHAPE: BOM carries NO date
    field AT ALL.** Not ``posting_date`` (SI/PI/PE/JE/DN/PR/SE/POS/Dunning/SR/LCV/Job Card), not
    ``transaction_date`` (SO/PO/MR/SQ/Q/RFQ), not ``from_date`` (Blanket Order) — a
    Date/Datetime-typed scan of all 94 fields returns EMPTY; the only timestamps are frappe's own
    ``creation``/``modified`` metadata, which no ERPNext date gate reads as a posting date. A
    submittable doctype with no date is a new edge for the closed-books machinery: the broker's
    entire period-lock chain (``_plan_closed_books_risk`` → ``get_period_locks`` →
    ``check_red_line``) is keyed on a posting date, and ``get_period_locks`` REFUSES a non-ISO
    date by design — riding any default here would hard-deny every BOM write forever.
    **``"date_field": None`` is therefore an explicit, source-verified PIN meaning "no date field
    exists", and the machinery branches on the DECLARED pin, never on an empty read:**
    ``tools.py``'s ``_posting_date_of`` stores :data:`pacioli.plan.NO_DATE_FIELD` in the Plan's
    ``posting_date`` channel for a declared-dateless doctype, ``_locks_for`` skips the
    period-lock read entirely (there is no date to build the Accounting-Period range query on),
    and ``check_red_line`` passes the sentinel by its own explicit branch. **This is EQUAL to
    ERPNext, not weaker, three ways from source:** BOM is NOT in ``hooks.py``'s 18-entry
    ``period_closing_doctypes`` list (ERPNext never runs the Accounting-Period validation on a
    BOM save); ``check_freezing_date`` fires only inside ``general_ledger.py``'s GL-posting paths
    (lines 416/727) and BOM posts no GL (below); and there is no date on the document for any
    range check to bite on. An empty date on a doctype that DOES declare a date field stays
    exactly as refused as before — datelessness is declared per doctype, never inferred from a
    missing value. (The dossier's own §3 recommendation contemplated a ``creation``-fallback as
    one option; REJECTED here: ``creation`` is bench-internal metadata, not a posting date —
    feeding it to the period-lock chain would invent a closed-books refusal ERPNext itself never
    makes, on a date the books never see.)
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading ``bom.py``: ``class
    BOM(WebsiteGenerator):`` (line 104) overrides neither ``submit()`` nor ``cancel()``
    anywhere.** Only ``on_submit`` (line 397) and ``on_cancel`` (line 401) hooks are defined,
    called by the base ``Document.submit()``/``.cancel()`` — the same run_method surface every
    non-Journal-Entry/non-Stock-Reconciliation doctype rides.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: BOM is the Dunning/LCV/Blanket
    Order/Job Card "uncallable" category, NOT the "honest-empty" one.** ``class
    BOM(WebsiteGenerator)`` — frappe's ``WebsiteGenerator`` (``website_generator.py:11``) is a
    direct ``Document`` subclass (a website-route mixin, nothing accounting-shaped), and no
    ``make_gl_entries`` exists anywhere in frappe itself (full-tree grep) or in ``bom.py`` —
    never ``AccountsController``, never ``StockController``. ERPNext's own
    ``get_accounting_ledger_preview`` (``stock_controller.py:2090-2119``) calls
    ``doc.make_gl_entries()`` as a bare, unguarded method call — ``AttributeError`` on a live
    bench for a BOM, refusing every ``plan_submit`` outright if called. **Fix: joins the
    ledger_preview skip tuple in ``tools.py``, now ``(DUNNING, LANDED_COST_VOUCHER,
    BLANKET_ORDER, JOB_CARD, BOM)``**, with its own honest
    ``_bom_ledger_preview_unavailable_flag``. Like Blanket Order/Job Card (and unlike LCV), BOM
    has no side effect that rewrites some OTHER document's ledger — its own submit posts nothing,
    anywhere, ever (``total_cost`` is a stored valuation snapshot, not a posting); ``plan_cancel``
    needs no equivalent new flag: ``get_gl_entries`` naturally, honestly returns empty.
  * **``on_submit`` (``bom.py:397-399``): ``manage_default_bom()`` + ``update_bom_creator_status()``
    — activation bookkeeping, never a posting.** ``manage_default_bom()`` maintains the
    ``is_default``/``is_active`` flags and writes the Item master's ``default_bom`` back-pointer;
    ``update_bom_creator_status()`` updates a BOM Creator workflow document's flags when this BOM
    came from one. No document of another doctype is created, no ledger row is written.
  * **``on_cancel`` (``bom.py:401-408``): deactivate (``is_active``/``is_default`` → 0), then ONE
    real bench-side refusal gate — ``validate_bom_links()`` (``bom.py:1200-1211``) throws "Cannot
    deactivate or cancel BOM as it is linked with other BOMs" when any OTHER submitted+active BOM
    still includes this BOM as a sub-assembly (a live SQL over ``BOM Item`` rows).** Then
    ``manage_default_bom()`` reassigns the Item's default and ``update_bom_creator_status()``
    syncs the creator doc. The throw is ERPNext's own cancel-block, honored through the standing
    generic exception handling (an answered ``ErpnextError``), never bypassed — and in the common
    case the broker's own blast-radius gate refuses FIRST: a parent BOM's ``BOM Item`` row is a
    submitted Link to this BOM, so ``get_submitted_linked_docs`` surfaces the parent and
    ``plan_cancel`` refuses on the non-empty graph before any bench cancel is attempted (the
    leaf-node law, equal-or-stricter than ERPNext's own narrower active-parents-only check).
  * **Cascade — the widest Link fan-in of any doctype this campaign has landed, and dossier
    correction (2) above.** The full-tree scan returns 29 ``Link → BOM`` fields across 25
    doctypes: the manufacturing spine (``Work Order.bom_no``, ``Work Order Operation.bom``/
    ``bom_no``, ``Job Card.bom_no`` AND ``Job Card.semi_fg_bom`` — two fields, ``BOM
    Operation.bom_no``, ``BOM Item.bom_no`` — the self-referencing sub-assembly edge, ``BOM
    Update Tool/Log`` current/new pairs), planning (``Production Plan Item``/``Production Plan
    Sub Assembly Item``/``Material Request Plan Item``/``Master Production Schedule Item``),
    transactional child tables (``Sales Order Item.bom_no``, ``Purchase Invoice/Order/Receipt
    Item.bom``, ``Stock Entry.bom_no`` + ``Stock Entry Detail.bom_no``, ``Material Request
    Item.bom_no``), subcontracting (``Subcontracting BOM``/``Order Item``/``Inward Order
    Item``/``Receipt Item``), ``Item.default_bom``, ``Quality Inspection.bom_no`` — and NOT
    Supplier Quotation, anywhere. ``cascade.py`` needs no changes (doctype-blind, the same
    finding every landing has made); ERPNext's own ``get_submitted_linked_docs`` walks all of
    these generically with child-table→parent promotion, so a submitted Work Order, Stock Entry,
    or Sales Order carrying this BOM in a child row IS the blast radius ``plan_cancel``
    discloses/refuses on. One honest scope note: ``Item.default_bom`` is a link FROM a
    non-submittable master (Item has no docstatus), so it never appears in a SUBMITTED-links
    read — the Item's back-pointer is maintained by BOM's own ``manage_default_bom()`` on
    submit/cancel instead, disclosed here rather than pretended into the graph.
  * **THE SIDE-SURFACE CAVEAT — smaller than Job Card's 14 but SHARPER: one of these methods
    mutates a SUBMITTED BOM, and dossier correction (3) above.** BOM exposes 10 separate
    ``@frappe.whitelist()`` callables outside the 5-verb surface — 5 instance methods
    (``get_routing``, ``get_bom_material_detail``, ``update_cost``, ``add_raw_materials``,
    ``add_materials_from_bom``) and 5 module-level functions (``get_bom_items``,
    ``get_children``, ``get_bom_diff``, ``item_query``, ``make_variant_bom``). The one that
    matters most: **``update_cost`` (``bom.py:617-655``) on a docstatus-1 BOM sets
    ``self.flags.ignore_validate_update_after_submit = True``, recalculates, ``db_update()``s
    the SUBMITTED document's stored cost fields in place, and then recursively ``update_cost``s
    every submitted PARENT BOM that includes this one** — a real, cascading mutation of
    submitted documents entirely outside ``docstatus``, the Work-Order-``stop_unstop()``/
    Job-Card-time-log family taken one step further (those flip status; this rewrites stored
    valuation aggregates across a tree). ``make_variant_bom`` creates a genuinely new BOM
    document; ``add_raw_materials``/``add_materials_from_bom`` append child rows to a draft.
    The ``BOM: {...}`` entry below governs ONLY submit/cancel (plus the generic
    amend/get/list wrapper verbs) — it grants NOTHING toward any of these 10 methods, no tool is
    built for any of them this landing, and a ``pacioli_guard``-scoped credential limited to
    ``BOM.submit``/``BOM.cancel`` does not thereby gain ``update_cost`` (documented plainly, the
    same discipline Job Card's own 14-method caveat set).
  * BOM is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list (confirmed
    against the same 18-entry list every recent landing has enumerated) — load-bearing for the
    dateless design above, not merely consistent with it: it is one of the three source proofs
    that skipping the closed-books gate for BOM equals ERPNext exactly.

**Breadth (Work Order) — Wave 3's fifth row, the twenty-first supported doctype, and THE FIRST
DATETIME-DATED ONE.** Full source-cited finding below (dossier at
``docs/plans/dossiers/work_order.md`` — right on party/status/grand_total/submit_via and the
``stop_unstop`` side channel it flagged as its own §8 centerpiece, but carrying FOUR corrections
this landing makes, the twelfth in a row to find at least one: (1) its §9 is an OUTRIGHT
INVERSION — "Work Order's cancel does NOT refuse based on linked submitted documents" is false:
``validate_cancel`` (``work_order.py:1234-1249``, called first thing by ``on_cancel``) THROWS
"Cannot cancel because submitted Stock Entry {0} exists" on a live SQL over submitted Stock
Entries, exactly the class of refusal §9 claims absent; (2) its §3 recommends riding the
``transaction_date`` pattern unchanged while missing that ``planned_start_date`` is a
**Datetime** field — a raw ``"YYYY-MM-DD HH:MM:SS"`` read fails ``_is_iso_date`` and would
hard-deny every Work Order write at both plan and execute, so the pattern does NOT ride
unchanged (see the date finding below); (3) its §7 names a ``parent_work_order`` NSM
self-reference "at line 747" — no such field exists anywhere in v16's ``work_order.json`` (86
fields enumerated), a phantom edge exactly like BOM's own phantom Supplier Quotation, while the
REAL ``Serial No.work_order`` Link went unmentioned; (4) its §2/§10 list proposal reads
``produced_qty`` as list-view-flagged — only ``production_item``/``bom_no``/``qty`` carry
``in_list_view: 1``). Confirmed from source (``work_order.json``, 86 fields enumerated, +
``work_order.py``, read 2026-07-21, version-16 checkout at 16.28.0):

  * **``party_field=None`` — an internal manufacturing order.** Zero ``customer``/``supplier``/
    ``party`` fields across all 86 (the JE/SE/SR/LCV/RFQ/Job-Card/BOM shape). ``production_item``
    (Link → Item, ``reqd``) and ``bom_no`` (Link → BOM, ``reqd``) are the two identity links.
  * **``status``: CONFIRMED PRESENT** (Select, ``read_only: 1``, ``no_copy: 1``, default
    ``"Draft"``) with 10 real options — Draft/Submitted/Not Started/In Process/Stock Reserved/
    Stock Partially Reserved/Completed/**Stopped**/**Closed**/Cancelled. Stopped and Closed are
    the two side-channel states (below): both are reached OUTSIDE docstatus, by whitelist RPCs.
  * **``grand_total``: CONFIRMED ABSENT** — the cost fields that exist
    (``planned_operating_cost``/``actual_operating_cost``/``total_operating_cost``/
    ``corrective_operation_cost``) are operation-cost tracking, no document aggregate.
  * **``date_field="planned_start_date"`` — THE FIRST DATETIME-TYPED DATE FIELD, a genuinely new
    axis.** Work Order carries NO ``posting_date`` and NO ``transaction_date`` (confirmed absent
    across all 86 fields); its required, defaulted date is ``planned_start_date`` (**Datetime**,
    ``reqd: 1``, ``default: "now"``, ``allow_on_submit: 1``). A frappe REST read returns it as
    ``"YYYY-MM-DD HH:MM:SS[.ffffff]"`` — which fails the strict ISO-date shape every consumer
    validates (``check_red_line``, ``get_period_locks``), so binding the fieldname alone (the
    dossier's own recommendation) would hard-deny every Work Order write. **The fix lives in ONE
    place: ``tools.py``'s ``_posting_date_of`` now truncates a datetime read to its date part —
    ONLY when the first 10 chars are a valid ISO date immediately followed by a ``" "``/``"T"``
    separator** (the same date-part semantics ERPNext's own ``getdate()`` applies to a datetime
    everywhere it needs a date). A malformed value keeps its raw shape and stays REFUSED
    downstream, exactly as before — truncation is never a repair, only the declared
    datetime→date projection. Every consumer downstream of ``_posting_date_of`` then sees a
    plain ISO date: the Plan's channel, the future-date flags, ``get_period_locks``' range
    query, ``check_red_line``. ``allow_on_submit: 1`` means the date can move after planning
    without a docstatus change — it still bumps ``modified``, so ``check_fresh`` catches any
    plan/execute drift, the standing TOCTOU answer, nothing new needed.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — ``class WorkOrder(Document)`` (``work_order.py:70``)
    overrides neither ``submit()`` nor ``cancel()``** — only ``on_submit`` (line 929) and
    ``on_cancel`` (line 949) hooks. The file ALSO defines 19 separate ``@frappe.whitelist()``
    callables (see the side-surface caveat below) — none are submit/cancel overrides.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: the Dunning/LCV/Blanket Order/Job
    Card/BOM "uncallable" category.** ``WorkOrder(Document)`` — a direct ``Document`` subclass,
    never ``AccountsController``/``StockController``; no ``make_gl_entries`` anywhere in the
    3114-line file (grep), the same closed defined-on list every prior "uncallable" landing
    enumerated. (The dossier's own §5 hedged "empty list OR AttributeError" — it is
    ``AttributeError``, the skip category, settled from the MRO not left as a coin flip.)
    **Fix: joins the ledger_preview skip tuple in ``tools.py``, now ``(DUNNING,
    LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER)``**, with its own honest
    ``_work_order_ledger_preview_unavailable_flag``. No revaluation-elsewhere side effect on the
    lifecycle — the one-flag Dunning shape; ``plan_cancel`` needs no equivalent
    (``get_gl_entries`` honestly returns empty — a Work Order posts nothing under its own name;
    the real GL/SL lives on the Stock Entries and Job Cards built FROM it).
  * **``on_cancel`` (``work_order.py:949-971``) opens with TWO real ERPNext refusal gates —
    dossier correction (1) above:** ``validate_cancel`` throws (a) "Stopped Work Order cannot be
    cancelled, Unstop it first" when ``status == "Stopped"``, and (b) "Cannot cancel because
    submitted Stock Entry {0} exists" on a live SQL over docstatus-1 Stock Entries referencing
    this Work Order. Gate (b) is the SAME condition the broker's own blast-radius check refuses
    on first (a submitted ``Stock Entry.work_order`` Link IS a submitted linked doc), so in the
    common case ``plan_cancel`` refuses before the bench gate is ever reached; gate (a) —
    Stopped — is a STATUS check the broker's disclosure cannot see at plan time (status is not
    docstatus), surfacing instead as an answered ``ErpnextError`` at execute through the
    standing generic path, honored never bypassed. Past the gates, cancel is the reversal the
    dossier describes: ``db_set status``, then ``on_close_or_cancel`` unwinds the submit-side
    counters (SO/MR fulfillment, bin qtys, Stock Reservation Entries, Subcontracting Inward
    state).
  * **Cascade — 6 ``Link → Work Order`` fields total (full-tree scan): Stock Entry, Job Card,
    Material Request, Pick List, Serial No, + ``amended_from``.** Four match the dossier;
    ``Serial No.work_order`` is the edge it missed (an honest scope note applies: Serial No is a
    non-submittable master with no docstatus, so it can never appear in a SUBMITTED-links read —
    like ``Item.default_bom`` on the BOM landing, disclosed rather than pretended into the
    graph); the dossier's ``parent_work_order`` NSM self-link is a PHANTOM — no such field in
    v16. ``cascade.py`` unchanged as always; ``get_submitted_linked_docs`` walks the real five
    generically.
  * **THE SIDE-SURFACE CAVEAT — the dossier's §8 centerpiece, CONFIRMED and then some: not one
    ungated status mutator but TWO, in a 19-callable whitelist surface.** ``stop_unstop``
    (``work_order.py:2759-2775``) flips a SUBMITTED Work Order between Stopped and its live
    status with nothing but base ``write`` permission — no docstatus change, no submit/cancel
    lifecycle, invisible to the 5-verb surface. ``close_work_order`` (same file) is the second:
    it drives ``status`` to the terminal ``"Closed"`` (the one state ``stop_unstop`` itself
    refuses to touch) — the dossier never mentioned it. The other 17 whitelist callables include
    document factories (``make_stock_entry``, ``make_job_card``, ``make_material_request``,
    ``create_pick_list``, ``make_stock_return_entry``, ``make_work_order``, ``make_bom``) and
    reads. **The ``WORK_ORDER: {...}`` entry below grants NOTHING toward any of the 19; no tool
    is built for any of them this landing** — a ``pacioli_guard``-scoped credential limited to
    ``Work Order.submit``/``Work Order.cancel`` does not thereby gain ``stop_unstop`` or
    ``close_work_order``, and the broker's plan disclosure cannot see a Stop/Close that happens
    outside its sight (documented plainly here, the Job-Card-caveat discipline, one size larger
    in consequence: these two mutate the STATUS the cancel gate itself keys on).
  * Work Order is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list
    (confirmed against the same 18-entry list) and posts no GL — the closed-books check against
    ``planned_start_date`` is therefore the broker being deliberately equal-or-STRICTER than
    ERPNext (which never period-checks a Work Order), the same accepted posture every dated
    non-GL doctype in this table already rides (SO/PO/MR/SQ/Q/RFQ/Blanket Order/Job Card).

**Breadth (Asset) — Wave 3's sixth and final row, the twenty-second supported doctype, and the
heaviest since Payment Entry: the first with SCHEDULED, ASYNCHRONOUS GL posting, and the first
whose leaf-node cancel is STRUCTURALLY unreachable.** Full source-cited finding below (dossier at
``docs/plans/dossiers/asset.md`` — right on the async-depreciation scope boundary, the ownership
party fields, the ``available_for_use_date`` date key, and the conditional-GL branching; its own
corrections this landing, the thirteenth in a row: (1) its §8 cascade table stops at 9 edges — the
full-tree scan returns **18 ``Link → Asset`` fields**, missing among others ``Sales Invoice
Item.asset``/``POS Invoice Item.asset`` (a SOLD asset is referenced by the selling invoice),
``Serial No.asset``, ``Asset Shift Allocation.asset``, the four ``wip_composite_asset`` purchase
edges, ``Asset Capitalization.target_asset``, and ``Asset.split_from`` (a second self-link
besides ``amended_from``); (2) it never surfaces the STRUCTURAL consequence of its own §4 finding
— because submit auto-creates and SUBMITS an Asset Movement that links back to this asset, the
broker's leaf-node ``plan_cancel`` will refuse for EVERY submitted Asset, making
``plan_cascade_cancel`` the only governed cancel path (see below); (3) it missed the SECOND async
GL channel: ``make_post_gl_entry`` (``asset.py:1074``, in ``hooks.py``'s daily scheduler list)
posts the deferred CWIP transfer for a submitted asset whose ``available_for_use_date`` arrives
later — depreciation JEs are not the only postings that happen outside the broker's consent; the
dossier file also carries a leaked agent-transcript fragment at its tail, trimmed this landing).
Confirmed from source (``asset.json``, 76 fields enumerated, + ``asset.py`` +
``depreciation.py`` + ``hooks.py`` + frappe's ``document.py``/``linked_with.py``, read
2026-07-21, version-16 checkout at 16.28.0):

  * **``party_field=None`` — the ownership trio is metadata, not a GL party.** ``asset_owner``
    (Select: Company/Supplier/Customer) toggles which of ``asset_owner_company``/``supplier``/
    ``customer`` is shown — informational ownership state; the GL on submit debits the fixed
    asset account and credits CWIP, never a receivable/payable party account. The dossier's own
    §1 honest assessment reaches the same conclusion.
  * **``status``: CONFIRMED PRESENT and the widest yet** (Select, ``read_only: 1``,
    ``in_list_view: 1``, 13 options: Draft/Submitted/Cancelled/Partially Depreciated/Fully
    Depreciated/Sold/Scrapped/In Maintenance/Out of Order/Issue/Receipt/Capitalized/Work In
    Progress) — lifecycle AND depreciation/disposal states in one field, driven by
    ``get_status()``, never user-writable. Load-bearing for cancel: ``validate_cancellation``
    (``asset.py:728-736``) refuses while status is In Maintenance/Out of Order, and refuses
    unless status is one of Submitted/Partially Depreciated/Fully Depreciated — so
    Sold/Scrapped/Capitalized assets can NEVER be cancelled. A STATUS gate (not docstatus), but
    unlike Work Order's Stopped (reachable only via an out-of-surface RPC mid-flight), an
    asset's status is readable on the draft at plan time — ``tools.py``'s
    ``_asset_cancel_risk_flags`` disclose a doomed cancel in advance from the doc's own field.
  * **``grand_total``: CONFIRMED ABSENT** — ``net_purchase_amount`` (Currency, the input cost)
    and ``total_asset_cost`` (Currency, ``read_only``, purchase + additional costs, populated
    post-submit) are the value fields; both ride the list tier below.
  * **``date_field="available_for_use_date"`` — the GL POSTING date, chosen over the also-real
    ``purchase_date``** (the dossier's own §3 correction-in-place reaches this too):
    ``make_gl_entries`` stamps ``"posting_date": self.available_for_use_date`` on both GL rows
    (``asset.py:942/959``), and the deferred-GL scheduler keys on the same field — so the
    closed-books check runs against the date the books actually receive. A Date field (no
    projection needed). Empty-date refusal aligns with the bench's own gate: the field's
    ``mandatory_depends_on`` makes it required at ``docstatus==1`` for every asset type, and
    ``validate_in_use_date`` (``asset.py:384-386``) throws on submit without it — an undated
    Asset cannot reach a successful ERPNext submit either. **Asset IS in ``hooks.py``'s
    ``period_closing_doctypes`` list** — the FIRST Wave-3 row where the broker's closed-books
    check is natively EQUAL (ERPNext really does period-check Asset saves), not
    equal-or-stricter.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — ``class Asset(AccountsController)``
    (``asset.py:41``) overrides neither ``submit()`` nor ``cancel()``** — ``before_submit``
    (Composite-Asset capitalization gate)/``on_submit``/``on_cancel`` hooks only, plus 15
    ``@frappe.whitelist()`` callables (side-surface caveat below).
  * **THE LEDGER-PREVIEW FINDING — the preview is CALLABLE: Asset does NOT join the skip
    tuple.** ``Asset`` defines a real ``make_gl_entries`` (``asset.py:924-970``) — it is one of
    the individual doctype controllers every prior "uncallable" landing's full-tree grep
    enumerated — so ``show_accounting_ledger_preview`` works natively, the first Wave-3 row on
    the ordinary preview path. The preview's projected GL is CONDITIONAL by the method's own
    body: rows are drafted only when (Composite Asset OR a purchase document + purchase_amount)
    AND ``available_for_use_date <= today``, so an empty ``projected_gl`` for an Asset can be
    honest-empty for THREE distinct reasons, each disclosed data-driven by
    ``_asset_submit_risk_flags`` (future date → the deferred-GL scheduler channel; no purchase
    document → ``validate_make_gl_entry`` returns False at submit; Composite Component → never
    posts directly). One mirrored edge, deliberately NOT special-cased: the preview calls
    ``make_gl_entries`` bare (no ``validate_make_gl_entry`` gate), so an asset category missing
    its Fixed Asset Account throws inside the preview (``get_fixed_asset_account``,
    ``asset.py:896-909``) and the plan refuses — ERPNext's own preview button fails identically
    on that config error, so the broker mirrors the native surface exactly (``get_cwip_account``
    is preview-safe: its default swallows the missing-account case, ``asset.py:911-922``).
  * **TWO ASYNC GL CHANNELS — the scope boundary this landing exists to disclose.** (1)
    Depreciation: submit activates draft Asset Depreciation Schedules
    (``convert_draft_asset_depr_schedules_into_active``); the depreciation Journal Entries are
    then created and auto-submitted by ``post_depreciation_entries`` — ``hooks.py``'s DAILY
    scheduler — hours or days later, outside any consent marker this broker ever minted. (2)
    Deferred CWIP transfer: when ``available_for_use_date`` is still in the future at submit,
    ``make_gl_entries`` posts NOTHING (the date condition above); ``make_post_gl_entry`` —
    ALSO in the daily scheduler list — posts the CWIP→fixed-asset transfer on the day the date
    arrives, for CWIP-enabled categories. The broker governs the submit that ARMS both
    channels; it cannot govern either posting. Disclosed loudly in ``_asset_submit_risk_flags``,
    fired data-driven off the draft's own ``calculate_depreciation``/date fields.
  * **THE STRUCTURAL CANCEL FINDING — ``plan_cancel`` refuses for EVERY submitted Asset, by
    construction; ``plan_cascade_cancel`` is the governed cancel path.** ``on_submit`` calls
    ``make_asset_movement()``, which creates AND SUBMITS an Asset Movement (``is_submittable:
    1``, confirmed) whose child row links back to this asset — so every submitted Asset has at
    least one submitted dependent from birth. Frappe's own cancel survives this because
    ``on_cancel`` runs BEFORE ``check_no_back_links_exist`` (``document.py:1450-1452``) and
    Asset's ``on_cancel`` cancels its own movements/schedules/depreciation-JEs first
    (``cancel_movement_entries``/``cancel_asset_depr_schedules``/``delete_depreciation_entries``)
    — one raw bench cancel silently unwinds N documents. The broker's leaf-node law reads the
    same graph FIRST and refuses, naming the links — which is the honest shape: the human
    consents to the WHOLE unwind graph through ``plan_cascade_cancel`` instead of an invisible
    mass-cancel riding a single-document consent. Equal in effect (everything cancels either
    way), stricter in consent. Depreciation JEs appear in that same graph (frappe's
    ``SubmittableDocumentTree`` walks dynamic links — ``linked_with.py`` — and a depreciation
    JE's accounts rows carry ``reference_type="Asset"``), so a depreciated asset's cascade
    graph shows every JE the cancel will unwind, by name, before anyone consents.
  * **Cascade — 18 ``Link → Asset`` fields across 16 doctypes (dossier correction (1) above),
    the second-widest fan-in landed:** the asset-family spine (Asset Movement Item, Asset
    Depreciation Schedule, Asset Repair, Asset Maintenance (via ``asset_name``), Asset Value
    Adjustment, Asset Capitalization + its Asset Item child, Asset Shift Allocation, Asset
    Activity), disposal/sale edges (``Sales Invoice Item.asset``, ``POS Invoice Item.asset``),
    ``Serial No.asset``, the four ``wip_composite_asset`` procurement edges (Material
    Request/Purchase Order/Purchase Invoice/Purchase Receipt Item), and TWO self-links
    (``amended_from``, ``split_from``). Three of the referencing doctypes are non-submittable
    (Serial No, Asset Maintenance, Asset Activity — ``is_submittable: 0`` confirmed on each) and
    so can never appear in a submitted-links read — the standing like-``Item.default_bom``
    disclosure; the submittable rest (Asset Repair, Asset Value Adjustment, Asset Shift
    Allocation, Asset Capitalization, Asset Depreciation Schedule — all ``is_submittable: 1``
    confirmed) DO surface when submitted, which is exactly how the structural cancel finding
    above manifests. ``cascade.py`` unchanged as always.
  * **THE SIDE-SURFACE CAVEAT — 15 whitelist callables, the document-factory family:**
    ``make_sales_invoice``, ``create_asset_maintenance``, ``create_asset_repair``,
    ``create_asset_capitalization``, ``create_asset_value_adjustment``, ``transfer_asset``,
    ``make_journal_entry``, ``make_asset_movement``, ``split_asset`` (which CREATES a new Asset
    and adjusts this one), plus reads. The ``ASSET: {...}`` entry below grants NOTHING toward
    any of them; no tool is built for any of them this landing.

**Breadth (Packing Slip) — Wave 4's first row, the twenty-third supported doctype, and THE
SECOND DATELESS ONE (reusing BOM's own ``NO_DATE_FIELD`` machinery exactly, not new machinery) —
plus a genuinely NEW absence this landing forces into the open: Packing Slip carries no
``company`` field either.** Full source-cited finding below (dossier at
``docs/plans/dossiers/packing_slip.md`` — correct on every axis it addresses; its one omission
was never checking for ``company``, see below). Confirmed from source (``packing_slip.json``, 22
fields enumerated (11 data fields, matching the dossier's own count), + ``packing_slip.py``, 220
lines, version-16 checkout, both read 2026-07-21):

  * **``party_field=None`` — Packing Slip is a shipment-packing record, not a transaction.**
    Confirmed by enumerating all 22 fields: the only Link fields are ``delivery_note`` (required,
    ``in_list_view: 1`` — the Draft Delivery Note this slip packs) and ``letter_head`` (cosmetic
    print setting) — zero ``customer``/``supplier``/``party`` fields at any level. The party this
    slip's shipment ultimately belongs to is reachable only by following ``delivery_note`` to
    another document, never read directly off this one.
  * **``status`` field: CONFIRMED ABSENT** — no Select status anywhere in the 22 fields;
    ``docstatus`` alone tracks Draft/Submitted/Cancelled (``is_submittable: 1``, confirmed).
  * **``grand_total`` field: CONFIRMED ABSENT** — the two aggregates that exist instead are
    ``net_weight_pkg`` (Float, ``read_only``, computed by ``calculate_net_total_pkg()`` at
    validate) and ``gross_weight_pkg`` (Float, manually editable, defaults to
    ``net_weight_pkg`` when unset) — WEIGHT totals, never a monetary one; Packing Slip has no
    ``items`` rate/amount at the header level for any total to aggregate.
  * **``in_list_view`` fields, confirmed byte-for-byte against the dossier: ``delivery_note``,
    ``from_case_no``, ``to_case_no``** (``packing_slip.json``'s own flags) — the FIFTEENTH
    ``_list_fields`` branch, alongside the Company finding below.
  * **``date_field=None`` — THE SECOND DATELESS DOCTYPE, confirmed independently from source (a
    Date/Datetime-typed scan of the full 22-field enumeration returns EMPTY — no
    ``posting_date``, no ``transaction_date``, no ``from_date``, no ``planned_start_date``/
    ``available_for_use_date``-shaped field of any kind).** This landing reuses BOM's own
    declared-dateless machinery exactly, as directed: ``"date_field": None`` in
    :data:`SUPPORTED_DOCTYPES` (the source-verified pin), ``_posting_date_of`` maps it to
    :data:`pacioli.plan.NO_DATE_FIELD`, ``_locks_for`` skips the period-lock read, and
    ``check_red_line`` passes the sentinel by its own existing branch — zero new code in
    ``plan.py`` or ``tools.py`` for this axis, the same three-way "equal to ERPNext" proof BOM's
    own landing already established (absent from ``period_closing_doctypes`` below; no GL posting
    for ``check_freezing_date`` to fire on; no date exists to range-check). **Because two
    doctypes now share this pin, ``test_erpnext.py``'s own exclusivity test widens from "BOM is
    the only dateless doctype" to a two-member set — each doctype's datelessness is verified
    independently from ITS OWN source, never inherited by the set growing.**
  * **``company`` FIELD: CONFIRMED ABSENT TOO — A GENUINELY NEW STRUCTURAL FINDING THE DOSSIER
    DID NOT FLAG.** The same 22-field enumeration that proves datelessness also proves Packing
    Slip carries no ``company`` Link at all — not even indirectly (the field simply does not
    exist in the schema). Every governed verb's own "wrong books" belt
    (``_tool_plan_submit``/``_tool_plan_cancel``/``_tool_plan_cascade_cancel``/
    ``_governed_write``/``_amend_document`` in ``tools.py``, nine call sites in total) reads
    ``doc.get("company")`` and compares it against the target's own optional company pin
    (``registry.py``'s ``Target.company``); a companyless document always reads back ``None``.
    **This is NOT a bug and NOT new machinery this landing invents:** the check
    (``if target.company and company != target.company``) already, correctly, refuses a
    companyless document under a company-PINNED target (``None`` can never match a real pinned
    company name — an honest "we cannot verify this belongs to your books" refusal, the same
    deny-on-unverifiable posture this whole codebase already carries for a locked period or an
    unreadable clock) and passes it cleanly under the documented UNPINNED posture
    (``registry.py``: ``company`` is optional; unset means "accept any company's document", not
    "accept none" — ``REG_UNPINNED`` in the test suite is the existing, previously-built idiom
    for exactly this scenario, used here for the first time by a doctype that can *never* pass
    the pinned check, not merely one whose fixture happened to mismatch). **No new
    ``company_field`` pin, no new sentinel, and no change to any of the nine call sites are built
    this landing** — the existing optional-pin design already covers this doctype correctly, by
    construction; a company-pinned deployment can still read/list Packing Slips freely (reads
    carry no company check) but can only govern (submit/cancel/amend) them through an unpinned
    target. Flagged here plainly for whoever reviews this landing: a future increment MAY want a
    dedicated ``company_field: None``-style bypass (paralleling ``date_field``) if a pinned-target
    deployment needs to govern Packing Slip directly — that is a real design decision (does the
    broker infer company from the linked Delivery Note? via a new bench read?) deliberately left
    to that decision-maker, not invented solo here.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 220 lines of
    ``packing_slip.py``: ``class PackingSlip(StatusUpdater):`` (line 13) overrides neither
    ``submit()`` nor ``cancel()`` anywhere.** Only ``on_submit`` (line 73) and ``on_cancel``
    (line 76) hooks are defined, each a one-line call to ``self.update_prevdoc_status()``
    (inherited from ``StatusUpdater``, ``status_updater.py:193-195``), called by the base
    ``Document.submit()``/``.cancel()``.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: Packing Slip is the Dunning/Landed Cost
    Voucher/Blanket Order/Job Card/BOM/Work Order "uncallable" category, NOT the "honest-empty"
    one.** ``class PackingSlip(StatusUpdater)`` — ``StatusUpdater`` (``status_updater.py:181``)
    is a direct ``frappe.model.document.Document`` subclass (confirmed from its own import line),
    never ``AccountsController``, never ``StockController``, and a full-file grep of
    ``status_updater.py`` finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry`` reference
    anywhere. The same full-tree ``def make_gl_entries`` grep every prior "uncallable" landing has
    run confirms the method is defined only on ``StockController`` and the same short, closed list
    of individual doctype controllers — ``PackingSlip`` shares no ancestry with any of them.
    ERPNext's own ``get_accounting_ledger_preview`` calls ``doc.make_gl_entries()`` bare and
    unguarded — ``AttributeError`` on a live bench for a real Packing Slip. **Fix: joins the
    ledger_preview skip tuple in ``tools.py``, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER,
    JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP)``**, with its own honest
    ``_packing_slip_ledger_preview_unavailable_flag``. Packing Slip has no side effect that
    rewrites some OTHER document's LEDGER either (it rewrites another document's ``packed_qty``
    COUNTER — a quantity bookkeeping field, never a GL/Stock Ledger row) — the simpler
    Dunning/Blanket-Order/Job-Card/BOM/Work-Order shape, one flag, submit-direction only;
    ``plan_cancel`` needs no equivalent new flag: ``get_gl_entries`` naturally, honestly returns
    empty (no GL row was ever written under a Packing Slip's own voucher_type, in either
    direction).
  * **``on_submit``/``on_cancel`` (``packing_slip.py:73-77``) both call the SAME single method,
    ``update_prevdoc_status()`` — a QUANTITY-COUNTER recompute, never a ledger post.**
    ``update_prevdoc_status`` (``status_updater.py:193-195``) calls ``update_qty()``
    (``:532-544``), which walks the ``status_updater`` config PackingSlip's own ``__init__``
    defines (two entries: Delivery Note Item via ``dn_detail``, Packed Item via ``pi_detail``) and
    runs ``_update_children`` (``:549-602``) — a RAW ``frappe.db.sql`` ``UPDATE`` statement that
    sets ``target_dt.packed_qty`` (Delivery Note Item / Packed Item) to the live SUM of
    ``Packing Slip Item.qty`` across submitted Packing Slips referencing that row (``cond``
    flips sign for submit vs. cancel — include this Packing Slip's own rows on submit, exclude
    them on cancel, ``:539-542``). A direct SQL column patch, not a Document-level save of the
    Delivery Note, and never a GL Entry or Stock Ledger Entry. ``validate_qty()`` (called right
    after, ``:265`` onward) is the bench's own over-allowance guard on the just-updated
    ``packed_qty``, not part of this landing's own governance surface.
  * **Cascade — PACKING SLIP IS A CASCADE LEAF: the ONLY ``Link → Packing Slip`` field in the
    entire v16 tree is Packing Slip's own self-referencing ``amended_from``.** A full-tree grep
    for ``"options": "Packing Slip"`` across every DocType JSON in the checkout returns exactly
    ONE hit — ``packing_slip.json`` line 170, its own ``amended_from`` field. Delivery Note Item's
    ``dn_detail``/Packed Item's ``pi_detail`` (the join keys ``update_qty`` reads) are plain
    ``Data`` fields storing a child-row name as a string, confirmed from
    ``delivery_note_item.json``'s own field dict — NOT ``fieldtype: "Link"``, so they never
    surface in ``get_submitted_linked_docs``'s Link-field walk at all. **No other doctype anywhere
    carries a header or child-table Link field to Packing Slip** — a cascade shape narrower than
    even Stock Entry's own "only ONE external doctype" finding. ``cascade.py`` needed no changes
    (as always), and this landing builds no fabricated "external blast radius" test, because
    there is no real external edge to fabricate one against — a submitted Packing Slip's own
    ``plan_cancel`` is refused only by its OWN blast-radius read finding something (which, absent a
    real edge, it structurally never will) or by ERPNext's own throws, of which ``on_cancel`` has
    none (confirmed: no ``frappe.throw`` anywhere in ``packing_slip.py``'s cancel path).
  * **THE SIDE-SURFACE CAVEAT — the smallest of this campaign: ONE read-only whitelist
    callable.** ``item_details`` (``packing_slip.py:207-208``, module-level,
    ``@frappe.whitelist()`` + ``@frappe.validate_and_sanitize_search_inputs``) is a search-filter
    RPC for the item-picker UI — a plain ``frappe.db.sql`` ``SELECT`` against ``tabItem`` scoped
    to the given ``delivery_note``'s own items, confirmed to mutate nothing. Unlike every prior
    landing's side-surface caveat, there is no mutation to caveat here at all — the
    ``PACKING_SLIP: {...}`` entry below grants nothing toward it (nor needs to withhold anything
    of consequence), documented for completeness of the house pattern, not because it is risky.
  * **A caveat load-bearing to the whole doctype, not this broker's own scope:**
    ``validate_delivery_note()`` (``packing_slip.py:79-85``) throws unless the linked Delivery
    Note's own ``docstatus`` is 0 — a Packing Slip can only ever be created against a DRAFT
    Delivery Note, confirmed from source, matching the dossier's own caveat exactly.
  * Packing Slip is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list
    (confirmed absent from the same 18-entry list every recent landing has enumerated) —
    load-bearing for the dateless design (the second of the three source proofs BOM's own
    landing established, reused here rather than re-derived: no GL path exists for
    ``check_freezing_date`` to fire on either).

**Breadth (Cost Center Allocation) — Wave 4's second row, the twenty-fourth supported doctype,
and A DOSSIER CORRECTION THIS LANDING SETTLES FROM SOURCE: the dossier claimed the doctype was
BOTH dated (a real, required ``valid_from`` Date field) AND "DATELESS in the broker sense" —
two claims that cannot both be true.** Full source-cited finding below (dossier at
``docs/plans/dossiers/cost_center_allocation.md``; correct on every OTHER axis it addresses).
Confirmed from source (``cost_center_allocation.json``, 7 fields enumerated, +
``cost_center_allocation.py``, 160 lines, version-16 checkout, both read 2026-07-21):

  * **``party_field=None`` — Cost Center Allocation is a recurring-allocation RULE, not a
    transaction with a counterparty.** The only Link fields across all 7 enumerated fields are
    ``main_cost_center`` (required, ``in_list_view: 1`` — the routing destination every
    percentage in the child table allocates FROM) and ``company`` (required, fetched — see
    below) — zero ``customer``/``supplier``/``party`` fields at any level. ``allocation_percentages``
    is a required ``Table`` (child doctype Cost Center Allocation Percentage), never a header
    party.
  * **``status`` field: CONFIRMED ABSENT** — no Select status anywhere in the 7 fields;
    ``docstatus`` alone tracks Draft/Submitted/Cancelled (``is_submittable: 1``, confirmed).
  * **``grand_total`` field: CONFIRMED ABSENT, and genuinely so — there is no substitute
    aggregate of any kind either.** ``allocation_percentages`` holds PERCENTAGES (which must sum
    to 100, enforced by ``validate_total_allocation_percentage``), never a Currency amount; this
    is the smallest schema this campaign has found (7 fields total, versus BOM's 94 or Packing
    Slip's 22) and it carries no value/progress/type-context field of any kind to stand in for
    the missing ``grand_total`` — the same "no natural analog" shape Request for Quotation's own
    branch established, this time even barer (RFQ still kept ``status``; this doctype has
    neither).
  * **``in_list_view`` fields, confirmed byte-for-byte against the dossier:
    ``main_cost_center``, ``valid_from``** (``cost_center_allocation.json``'s own flags) — the
    SIXTEENTH ``_list_fields`` branch.
  * **``date_field="valid_from"`` — THE DOSSIER CORRECTION.** The dossier's own §3 documents
    ``valid_from`` as ``Date, reqd=1, default="Today"`` (correct, confirmed at
    ``cost_center_allocation.json:27-34``) and its own §12 summary table simultaneously calls the
    doctype "DATELESS in the broker sense" — an internally contradictory pair of claims: a
    declared-dateless pin (:data:`pacioli.plan.NO_DATE_FIELD`) is reserved for a doctype with
    **ZERO** Date/Datetime fields (BOM's and Packing Slip's own proof, each a full-field scan
    returning empty); Cost Center Allocation is the opposite shape — **one real, required Date
    field and no others.** A required field with a default is not merely present, it is
    guaranteed non-empty on every valid document — the strongest case for the ORDINARY dated
    path this campaign has seen, not the weakest. The correct read: ``date_field="valid_from"``,
    the **SIXTH** distinct date-fieldname pattern (``posting_date`` default; ``transaction_date``;
    ``from_date``; ``planned_start_date`` [Datetime]; ``available_for_use_date``; and now
    ``valid_from``) — riding the exact same generic :func:`pacioli.tools._date_field_for` /
    :func:`pacioli.tools._posting_date_of` / :func:`pacioli.tools._locks_for` /
    :func:`pacioli.plan.check_red_line` machinery Blanket Order's ``from_date`` and Asset's
    ``available_for_use_date`` already proved generalizes to an arbitrary named Date field — ZERO
    new ``plan.py``/``tools.py`` code for this axis, only a new fieldname spliced through the
    existing parameter. The closed-books check therefore runs the **NORMAL, EQUAL-OR-STRICTER**
    path against a real read value — the same posture Sales Order/Purchase Order's own
    ``transaction_date`` rows already carry (genuinely dated doctypes that are ALSO absent from
    ``period_closing_doctypes``, because neither posts GL itself) — never the dateless sentinel
    branch. Absence from ``period_closing_doctypes`` (confirmed below) is therefore evidence of
    "this doctype doesn't post GL", not evidence of "this doctype has no date" — the dossier's
    own reasoning error, corrected here.
  * **``company`` FIELD: CONFIRMED PRESENT, unlike Packing Slip.** ``company`` (Link, ``reqd:
    1``, ``fetch_from: "main_cost_center.company"`` — ``cost_center_allocation.json:44-50``) is
    fetched automatically from the chosen ``main_cost_center`` and therefore always populated on
    a valid document. The standing "wrong books" belt (the nine ``doc.get("company")`` call
    sites in ``tools.py``) applies to this doctype in its ordinary form — a company-PINNED
    target governs it exactly as it governs any other company-bearing doctype; there is no
    companyless edge case here (contrast Packing Slip's own landing, immediately above).
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 160 lines of
    ``cost_center_allocation.py``: ``class CostCenterAllocation(Document):`` (line 30) overrides
    neither ``submit()`` nor ``cancel()``, and defines no ``on_submit``/``on_cancel`` hook of ANY
    kind** — only ``__init__``, ``validate`` (and its four private ``validate_*`` helpers), and
    ``clear_cache`` are defined in the whole file.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: Cost Center Allocation is the
    Dunning/LCV/Blanket Order/Job Card/BOM/Work Order/Packing Slip "uncallable" category.**
    ``class CostCenterAllocation(Document)`` — a **direct** ``frappe.model.document.Document``
    subclass (confirmed from its own import line and class statement), never
    ``AccountsController``, never ``StockController`` — and a full-file grep of
    ``cost_center_allocation.py`` finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry``
    reference anywhere. ERPNext's own ``get_accounting_ledger_preview`` calls
    ``doc.make_gl_entries()`` bare and unguarded — ``AttributeError`` on a live bench for a real
    Cost Center Allocation. **Fix: joins the ledger_preview skip tuple in ``tools.py``, now
    ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    COST_CENTER_ALLOCATION)``**, with its own honest
    ``_cost_center_allocation_ledger_preview_unavailable_flag``.
  * **``on_submit``/``on_cancel`` — BOTH CONFIRMED ABSENT, not merely non-posting: the
    simplest submit/cancel lifecycle this campaign has found.** Unlike every prior "uncallable"
    doctype (Dunning through Packing Slip each carry SOME side effect on submit/cancel, even if
    never a ledger row — a counter rewrite, a status recompute, a linked-document mutation),
    Cost Center Allocation defines no ``on_submit``/``on_cancel`` hook at all — confirmed by
    reading every one of the file's 160 lines. Submitting or cancelling this doctype changes
    nothing beyond its own ``docstatus`` flip; ``validate()`` (the ONLY lifecycle hook present)
    runs at every save, submitted or not, and is unaffected by this broker's own scope.
  * **Cascade — COST CENTER ALLOCATION IS A CASCADE LEAF: the ONLY ``Link → Cost Center
    Allocation`` field in the entire v16 tree is its own self-referencing ``amended_from``.** A
    full-tree grep for ``"options": "Cost Center Allocation"`` across every DocType JSON in the
    checkout returns exactly ONE hit — ``cost_center_allocation.json`` line 63, its own
    ``amended_from`` field. **No other doctype anywhere carries a header or child-table Link
    field to Cost Center Allocation** — the same narrow shape Packing Slip's own landing
    established. ``cascade.py`` needed no changes (as always), and this landing builds no
    fabricated "external blast radius" test, because there is no real external edge to fabricate
    one against.
  * **THE SIDE-SURFACE CAVEAT — ZERO whitelist callables, the smallest of this campaign.** A
    full grep of ``cost_center_allocation.py`` for ``@frappe.whitelist()`` finds NOTHING — not
    even Packing Slip's one read-only search filter. There is nothing to document beyond this
    absence: the ``COST_CENTER_ALLOCATION: {...}`` entry below grants nothing toward a side
    surface that does not exist.
  * **RED FLAGS: none found.** A full scan of ``cost_center_allocation.py`` for
    ``frappe.enqueue()``, scheduler registration, or any async channel returns nothing. The four
    private ``validate_*`` helpers run synchronous ``frappe.db.get_value``/``frappe.get_all``
    queries only (checking the last GL Entry date against the chosen cost center, warning on a
    backdated/overlapping allocation, and enforcing the main/child cost-center hierarchy rules)
    — none of them post, mutate another document, or enqueue anything.
  * Cost Center Allocation is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes``
    list (confirmed absent from the same 18-entry list every recent landing has enumerated) —
    consistent with the "genuinely dated but non-GL-posting" shape Sales Order/Purchase Order
    already carry, NOT evidence of datelessness (the dossier's own reasoning error, corrected
    above).

**Breadth (Supplier Scorecard Period) — Wave 4's third row, the twenty-fifth supported doctype,
and Wave 4's FIRST ROW WITH A REAL PARTY FIELD** (Packing Slip and Cost Center Allocation, the
first two Wave-4 rows, both carry ``party_field=None``). Full source-cited finding below (dossier
at ``docs/plans/dossiers/supplier_scorecard_period.md`` — correct on every fact it checked except
two: a sloppy "GL party" framing on its one real finding, and a genuine omission on ``company``
and on the doctype's own machine-generation lifecycle, both corrected below). Confirmed from
source (``supplier_scorecard_period.json``, 12 fields enumerated (9 regular + 3 structural,
matching the dossier's own count), + ``supplier_scorecard_period.py`` (161 lines) +
``supplier_scorecard.py`` (the PARENT doctype, 449 lines) + ``erpnext/hooks.py``, version-16
checkout, all read 2026-07-21):

  * **``party_field="supplier"`` — a real, required, header-level Link, the same fieldname/shape
    Purchase Order/Purchase Receipt/Supplier Quotation already established (not new machinery).**
    Confirmed by enumerating all 12 fields (``supplier_scorecard_period.json`` lines 23-30):
    ``supplier`` (Link -> Supplier, ``reqd: 1``, ``in_list_view: 1``) is the ONLY party-shaped
    field in the schema — no ``customer``, no Dynamic Link pair. **Dossier correction (framing,
    not fact):** the dossier's own §1 calls this a "GL party (supplier account resolution)" — a
    label this doctype does not earn: Supplier Scorecard Period posts no GL entry of any kind
    (confirmed below, the UNCALLABLE preview finding), so no "account resolution" happens
    anywhere in its lifecycle. The field is real and the pin is correct
    (``party_field="supplier"``, decided on the field's own reality — a required, static, header
    Link — never on the GL framing), but this is the FIRST Wave-4 row where that distinction
    actually matters, because it is also the first Wave-4 row where ``party_field`` is genuinely
    non-``None``.
  * **``status`` field: CONFIRMED ABSENT** — no Select status anywhere in the 12 fields;
    ``docstatus`` alone tracks Draft/Submitted/Cancelled (``is_submittable: 1``, confirmed).
  * **``grand_total`` field: CONFIRMED ABSENT — ``total_score`` (Percent, ``read_only: 1``,
    ``in_list_view: 1``) is the nearest analog, but it is a SCORE, never a monetary total.**
    Computed in ``validate()`` -> ``calculate_score()`` (``supplier_scorecard_period.py:87-91``):
    a weighted sum of each criterion's clamped ``[0, max_score]`` score, itself computed via
    ``frappe.safe_eval()`` against a formula + variable-interpolated values
    (``calculate_criteria``/``calculate_variables``/``get_eval_statement``,
    ``supplier_scorecard_period.py:57-119``). No Currency/Float aggregate of any kind exists on
    this schema.
  * **``in_list_view`` fields, confirmed byte-for-byte against the dossier: ``supplier``,
    ``total_score``, ``start_date``** (``supplier_scorecard_period.json``'s own flags —
    ``end_date`` is NOT list-view-flagged) — the SEVENTEENTH ``_list_fields`` branch.
  * **``company`` FIELD: CONFIRMED ABSENT — A DOSSIER OMISSION (its own summary table has no
    company row at all), THE SECOND COMPANYLESS DOCTYPE after Packing Slip's own landing.** The
    same 12-field enumeration that proves the party/status/grand_total findings also proves no
    ``company`` Link exists anywhere in the schema — not even indirectly (no ``fetch_from``, no
    conditional). Exactly as Packing Slip's own landing established: every governed verb's
    existing "wrong books" belt (nine ``doc.get("company")`` call sites in ``tools.py``) reads
    back ``None`` for this doctype and therefore refuses under a company-PINNED target (``None``
    never matches a real pin — an honest, deny-biased refusal, not a bug) while governing cleanly
    under the documented UNPINNED posture (``registry.py``/``REG_UNPINNED``, the same idiom
    Packing Slip's own landing used first). No new ``company_field`` pin, no new sentinel, and no
    change to any of the nine call sites are built this landing either — see Packing Slip's own
    module-docstring paragraph for the full argument, reused here rather than re-derived.
    **Genuinely new about this combination:** unlike Packing Slip (also dateless), Supplier
    Scorecard Period IS genuinely dated (``start_date``, below) — the first doctype to combine a
    real party field, a real date field, AND an absent company field all at once.
    **CORRECTION (2026-07-21 live-prove batch):** the closed-books disclosure did NOT, in fact,
    "DOES read a real period-lock call even under the unpinned target" as originally claimed here
    — it CRASHED. ``get_period_locks(None, ...)`` is not a valid companyless lock read; it is
    ``get_period_locks``'s own first line, ``self._doc_path("Company", company)``, raising
    ``ErpnextError("a document name is required")`` for ``company=None`` — a real failure on a
    live bench that the test suite's own double masked by tolerating ``company=None`` where the
    real client refuses (fixed alongside the production guard; see ``tools.py``'s
    ``_plan_closed_books_risk``/``_locks_for`` docstrings for the shape-driven fix, now honestly
    disclosed as "not applicable — no company to check" rather than read at all).
  * **``date_field="start_date"`` — a real, required Date field, no default; chosen over the
    doctype's OTHER real, required Date field (``end_date``) as the period's own ANCHOR, the same
    "use the window's own start, not its close" convention Blanket Order's own ``from_date``
    (never ``to_date``) already established.** Confirmed present at
    ``supplier_scorecard_period.json`` lines 49-55 (``start_date``) and 56-61 (``end_date``);
    ``start_date`` is also the ONE of the pair flagged ``in_list_view: 1`` (``end_date`` is not)
    — the doctype's own list-tier already prefers it. Neither ``supplier_scorecard_period.py``'s
    ``validate()`` nor any of its own six helpers reads either date at all (confirmed by reading
    all 161 lines) — the pick is a broker-side convention, not something the doctype's own code
    favors one way or the other. Rides the existing generic ``_date_field_for``/
    ``_posting_date_of``/``_locks_for``/``check_red_line`` machinery unchanged (the same
    fieldname-splice shape every dated branch since Blanket Order's ``from_date`` already proved
    generalizes) — zero new ``plan.py``/``tools.py`` code for this axis, the SEVENTH distinct
    date-fieldname pattern this campaign has found (``posting_date``; ``transaction_date``;
    ``from_date``; ``planned_start_date`` [Datetime]; ``available_for_use_date``; ``valid_from``;
    and now ``start_date``).
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 161 lines of
    ``supplier_scorecard_period.py``: ``class SupplierScorecardPeriod(Document):`` (line 16)
    overrides neither ``submit()`` nor ``cancel()``, and defines no ``on_submit``/``on_cancel``
    hook of ANY kind** — only ``validate`` and its own six helpers (``validate_criteria_weights``,
    ``calculate_variables``, ``calculate_criteria``, ``calculate_score``,
    ``calculate_weighted_score``, ``get_eval_statement``) are defined on the class; the two
    module-level functions (``import_string_path``, ``make_supplier_scorecard``) sit outside the
    class entirely. The same simplest-lifecycle shape Cost Center Allocation's own landing
    established — not new, a second doctype sharing it.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: Supplier Scorecard Period is the
    Dunning/LCV/Blanket Order/Job Card/BOM/Work Order/Packing Slip/Cost Center Allocation
    "uncallable" category.** ``class SupplierScorecardPeriod(Document)`` — a **direct**
    ``frappe.model.document.Document`` subclass (confirmed from its own import line and class
    statement), never ``AccountsController``, never ``StockController`` — and a full-file grep of
    ``supplier_scorecard_period.py`` finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry``
    reference anywhere. ERPNext's own ``get_accounting_ledger_preview`` calls
    ``doc.make_gl_entries()`` bare and unguarded — ``AttributeError`` on a live bench for a real
    Supplier Scorecard Period. **Fix: joins the ledger_preview skip tuple in ``tools.py``, now
    ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD)``**, with its own honest
    ``_supplier_scorecard_period_ledger_preview_unavailable_flag``.
  * **``on_submit``/``on_cancel`` — BOTH CONFIRMED ABSENT, the second doctype (after Cost Center
    Allocation) with no hook of any kind on either lifecycle event.** Submitting or cancelling
    this doctype changes nothing beyond its own ``docstatus`` flip.
  * **Cascade — SUPPLIER SCORECARD PERIOD IS A CASCADE LEAF: the ONLY ``Link -> Supplier
    Scorecard Period`` field in the entire v16 tree is its own self-referencing
    ``amended_from``.** A full-tree grep for ``"options": "Supplier Scorecard Period"`` across
    every DocType JSON in the checkout returns exactly ONE hit —
    ``supplier_scorecard_period.json`` line 99, its own ``amended_from`` field. The relationship
    with the parent ``Supplier Scorecard`` runs the OTHER way (this doctype's own ``scorecard``
    field, a required Link TO Supplier Scorecard — never the reverse), and
    ``supplier_scorecard.py``'s own SQL joins (``calculate_total_score``, ``get_timeline_data``)
    address ``tabSupplier Scorecard Period`` by raw string, never via a schema-declared Link
    field on the PARENT doctype. **No other doctype anywhere carries a header or child-table
    Link field to Supplier Scorecard Period** — ``cascade.py`` needed no changes (as always), and
    this landing builds no fabricated "external blast radius" test, because there is no real
    external edge to fabricate one against.
  * **THE SIDE-SURFACE CAVEAT — ZERO whitelist callables.** A full grep of
    ``supplier_scorecard_period.py`` for ``@frappe.whitelist()`` finds NOTHING; the one
    module-level function outside the class, ``make_supplier_scorecard`` (lines 130-160), is a
    plain (undecorated) mapper helper invoked by an action button on the PARENT ``Supplier
    Scorecard`` doctype's own client script — not browser-RPC-reachable on its own. Nothing to
    document beyond this absence: the ``SUPPLIER_SCORECARD_PERIOD: {...}`` entry below grants
    nothing toward a side surface that does not exist.
  * **A DOSSIER CORRECTION TO §11 ("RED FLAGS: NONE FOUND") — narrowly true, but scoped to the
    wrong file: Supplier Scorecard Period is MACHINE-GENERATED-AND-SUBMITTED in the ordinary
    case, by its own PARENT doctype, never by this broker.** The dossier's own enqueue/scheduler
    scan was (correctly) confined to ``supplier_scorecard_period.py`` itself, which indeed
    carries no such call — but ``Supplier Scorecard`` (the PARENT, a SEPARATE doctype) both (a)
    auto-creates AND auto-submits Supplier Scorecard Period documents on every one of its OWN
    saves (``supplier_scorecard.py:57-60``, ``on_update`` -> ``make_all_scorecards``), and (b) is
    itself swept once daily by a scheduled job (``erpnext/hooks.py:469``, ``daily_maintenance``
    -> ``refresh_scorecards``, ``supplier_scorecard.py:183-197``) that calls the identical
    ``make_all_scorecards`` for EVERY Supplier Scorecard on the bench. ``make_all_scorecards``
    (``supplier_scorecard.py:200-257``) walks each configured period window, and where no
    submitted period already covers it, builds one via ``make_supplier_scorecard`` (the mapper
    this landing's own side-surface caveat names above), sets ``start_date``/``end_date``
    directly, then calls ``period_card.insert(ignore_permissions=True)`` followed immediately by
    ``period_card.submit()`` — synchronously, in-process (confirmed: no ``frappe.enqueue`` in
    either function; this is not an async channel, just a same-request side effect on a
    different, related doctype). **This is not a red flag this broker must defend against** — the
    broker only ever governs an EXISTING Supplier Scorecard Period through its own
    plan/consent/execute path; it never creates one, and this scheduled/on-save auto-submission
    runs entirely outside this broker's own surface regardless of whether pacioli is installed at
    all. It IS, however, load-bearing operational context the dossier's file-scoped RED FLAGS
    section never surfaced: with ``in_create: 1`` also permitting direct manual creation, an
    independently-authored Supplier Scorecard Period (one a human builds and submits by hand, or
    one this broker's own ``submit_supplier_scorecard_period`` tool governs) behaves identically
    through this broker's generic path either way — but in the ORDINARY case, a document already
    found SUBMITTED was very likely produced by the scheduler or the parent's own save, never by
    a governed write this broker was ever asked to make.
  * Supplier Scorecard Period is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes``
    list (confirmed absent from the same 18-entry list every recent landing has enumerated) —
    consistent with a genuinely dated but non-GL-posting doctype (the same shape Cost Center
    Allocation's own landing established), NOT evidence of datelessness (this doctype is not
    dateless — see above).

**Breadth (Quality Inspection) — Wave 4's fourth row, the twenty-sixth supported doctype, and the
FIRST DOCTYPE ON A DYNAMIC LINK PAIR SINCE QUOTATION.** Full source-cited finding below (dossier
at ``docs/plans/dossiers/quality_inspection.md`` — correct on party/status/grand_total/date_field/
submit_via/ledger-preview, but two real corrections land below: a whitelist-count undercount and a
cascade §7 framing gap that undersells the Job Card branch specifically). Confirmed from source
(``quality_inspection.json``, 30 fields enumerated (23 real + 7 Column/Section Break layout) via
``json.load``, + ``quality_inspection.py`` (524 lines), version-16 checkout, both read 2026-07-21):

  * **``party_field=None`` — Quality Inspection carries NO static Customer/Supplier/party field;
    the reference is a Dynamic Link pair, ``reference_type`` (Select, ``reqd: 1``, 7 named options
    plus Job Card — ``quality_inspection.json`` lines 77-83) and ``reference_name`` (Dynamic Link,
    ``reqd: 1``, ``options: "reference_type"`` — lines 84-95), the SAME shape Quotation's own
    ``quotation_to``/``party_name`` pair already established.** ``reference_name`` alone carries
    the schema's own ``in_list_view: 1`` flag — ``reference_type`` does not — but (the Quotation
    precedent this campaign already settled: a Dynamic Link's type-half is meaningless without its
    name-half, so both context columns ride the list tier together, never one alone) both are
    spliced into the eighteenth ``_list_fields`` branch below regardless of the schema's own
    per-field flag. **``company`` (Link -> Company, lines 255-260) IS present on this schema —
    UNLIKE Packing Slip/Supplier Scorecard Period, this is not a companyless row** — but it is
    NOT ``reqd`` and carries no default; ``set_company()`` (91-95) sets it programmatically from
    whatever document ``reference_type``/``reference_name`` resolve to, on every ``validate()``.
    **Dossier framing correction:** its own §1 calls this "a GL party fixture" — misleading, in
    the same way Supplier Scorecard Period's own "GL party" label was: Quality Inspection posts NO
    GL entry of any kind (the UNCALLABLE preview finding below), so nothing about ``company`` here
    resolves a GL party; it is plain descriptive metadata mirroring the referenced transaction's
    own company, nothing more.
  * **``status`` field: CONFIRMED PRESENT** (Select, ``quality_inspection.json`` lines 232-239,
    ``reqd: 1``, options ``\nAccepted\nRejected\nCancelled``, default ``"Accepted"`` — byte-for-
    byte the dossier's own claim). **``grand_total`` field: CONFIRMED ABSENT** — no Currency/Float
    aggregate of any kind exists anywhere in the 30-field enumeration; Quality Inspection is a
    pure control/quality record, never a transaction with an amount.
  * **``in_list_view`` fields, confirmed byte-for-byte against the dossier via ``json.load``: four
    — ``report_date``, ``inspection_type``, ``reference_name``, ``item_code``** (lines 55, 69, 88,
    104) — the EIGHTEENTH ``_list_fields`` branch (see below).
  * **``date_field="report_date"`` — a real, required Date field (``reqd: 1``, default "Today",
    lines 51-61), KEEPS the default fieldname literal but still needs its own branch (the context
    columns force one) — the EIGHTH distinct date-fieldname pattern this campaign has found
    (``posting_date``; ``transaction_date``; ``from_date``; ``planned_start_date`` [Datetime];
    ``available_for_use_date``; ``valid_from``; ``start_date``; and now ``report_date``, a
    literal fieldname no prior branch has used even though it echoes "posting" semantics).**
    Quality Inspection is confirmed absent from ``erpnext/hooks.py``'s ``period_closing_doctypes``
    list (the same 18-entry list every recent landing has enumerated) and posts no GL of any kind
    (below) — ``report_date`` is metadata only, never a period-lock-relevant posting date in
    ERPNext's own accounting sense, though this broker's own closed-books disclosure still reads
    it the same equal-or-stricter way every dated doctype's own date_field is read.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 524 lines of
    ``quality_inspection.py``: ``class QualityInspection(Document):`` (line 20) overrides neither
    ``submit()`` nor ``cancel()`` anywhere** — only ``on_discard``/``on_update``/``on_submit``/
    ``on_cancel``/``on_trash``/``before_submit`` HOOKS are defined, called by the base
    ``Document.submit()``/``.cancel()``, the same run_method doc-method surface every non-Journal-
    Entry/non-Stock-Reconciliation doctype already rides.
  * **A GENUINE DOSSIER OMISSION: ``before_submit`` (line 152-153) calls
    ``validate_readings_status_mandatory()`` (210-213), which THROWS** (``"Row #{idx}: Status is
    mandatory"``) **the first time it finds a ``readings`` child row with no ``status`` value set
    — a real, doomed-submit refusal gate the dossier's own §4 ("Submit via") and §6 ("on_submit
    side effects") both never mention (§6 describes only the ``on_submit`` hook itself, never the
    EARLIER ``before_submit`` one).** Readable off the draft's own ``readings`` rows before a
    marker is ever minted — the same "status gate readable on the draft" shape Asset's own
    ``validate_cancellation`` disclosure established, here on the SUBMIT side. ``tools.py`` carries
    a new doctype-scoped, data-driven flag naming this (``_quality_inspection_submit_risk_flags``).
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE FULL MRO ACROSS BOTH CHECKOUTS: Quality
    Inspection is the Dunning/Blanket Order/Job Card/BOM/Work Order/Packing Slip/Cost Center
    Allocation/Supplier Scorecard Period "uncallable" category.** ``class QualityInspection
    (Document)`` (``quality_inspection.py:20``) — a **direct** ``frappe.model.document.Document``
    subclass, never ``AccountsController``, never ``StockController``. A full-tree grep for
    ``def make_gl_entries`` across BOTH the erpnext-16 AND frappe-16 checkouts (not merely
    erpnext's own tree — the widest sweep this campaign has run) finds it defined ONLY on
    ``StockController`` and nine individual controllers (``Asset``, ``AssetCapitalization``,
    ``AssetRepair``, ``SalesInvoice``, ``PurchaseInvoice``, ``PaymentEntry``, ``JournalEntry``,
    ``PeriodClosingVoucher``, ``InvoiceDiscounting``) — ``frappe.model.document.Document`` itself
    defines no such method, confirmed by reading ``frappe/model/document.py``'s own class body.
    ``QualityInspection`` shares no ancestry with any of the nine. ERPNext's own
    ``get_accounting_ledger_preview`` calls ``doc.make_gl_entries()`` as a bare, unguarded method
    call — ``AttributeError`` on a live bench for a real Quality Inspection. **Fix: joins the
    ledger_preview skip tuple in ``tools.py``, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER,
    JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
    QUALITY_INSPECTION)``**, with its own honest
    ``_quality_inspection_ledger_preview_unavailable_flag``.
  * **``on_submit`` (195-200) is CONDITIONAL — fires ``update_qc_reference()`` only when
    ``Stock Settings.action_if_quality_inspection_is_not_submitted == "Stop"`` (a site-wide
    setting, not a field on the draft this broker reads) — ``on_cancel`` (202-205) is
    UNCONDITIONAL: sets ``self.ignore_linked_doctypes = "Serial and Batch Bundle"`` then ALWAYS
    calls ``update_qc_reference()``. Neither hook contains a ``frappe.throw`` of its own (a
    full-file grep finds ``frappe.throw`` only in ``validate_inspection_required``/
    ``set_status_based_on_acceptance_formula``/``get_formula_evaluation_data`` — never in
    ``on_submit``/``on_cancel``/``update_qc_reference`` themselves) — confirmed no cancel-side
    refusal gate of its own.**
  * **THE CENTRAL DOSSIER CORRECTION — A SUBMITTED-STATE WRITE INTO ANOTHER DOCUMENT, read line by
    line: the dossier's own §7 ("removes the QI link from the reference document's child row") is
    right for six of the seven ``reference_type`` options but WRONG for the seventh (Job Card).**
    ``update_qc_reference()`` (215-262) branches on ``self.reference_type``: for every reference
    type EXCEPT Job Card, it runs a ``frappe.qb`` query-builder ``UPDATE`` against the reference
    document's own CHILD TABLE row (``Purchase Receipt Item``/``Delivery Note Item``/``Stock Entry
    Detail``/etc., filtered by ``parent``+``item_code``, further narrowed by ``batch_no``/
    ``child_row_reference`` when set) — the shape the dossier's own framing describes. **For
    ``reference_type == "Job Card"`` specifically (218-227), there IS no child row at all: the
    write is a raw ``frappe.db.sql`` statement — ``UPDATE tabJob Card SET quality_inspection = %s,
    modified = %s WHERE name = %s and production_item = %s`` — directly against the Job Card
    doctype's OWN TOP-LEVEL row**, bypassing ``Document.save()``, Job Card's own ``validate``/
    ``on_update`` hooks, and version history entirely, and checking neither this Quality
    Inspection's own docstatus meaning nor the target Job Card's. Every branch also finishes with
    ``frappe.db.set_value(self.reference_type, self.reference_name, "modified", self.modified)``
    (257-262) — a cache-busting write on the reference document's OWN top-level ``modified``
    stamp, again outside any save/hook cycle. **Practical exposure:** the standing blast-radius
    check (``get_submitted_linked_docs``) already refuses this Quality Inspection's own cancel
    first whenever the reference document is CURRENTLY submitted (Job Card's own
    ``quality_inspection`` Link field, or a child-table Link promoted to its submitted parent, for
    the other six reference types) — the same "rarely reached through this tool" shape Asset's
    own multi-document unwind carries — but an ALREADY-CANCELLED reference document (docstatus 2,
    invisible to that same blast-radius check) is NOT protected the same way: this cancel still
    silently rewrites its ``quality_inspection`` field via raw SQL, no hook, no version row.
    ``tools.py`` carries a new doctype-scoped, data-driven cancel flag naming this precisely per
    branch (``_quality_inspection_cancel_risk_flags``, gated on the draft's own ``reference_type``/
    ``reference_name``).
  * **Cascade edges — NINE hits from a full-tree scan for ``fieldtype: "Link"`` +
    ``options: "Quality Inspection"``, confirmed byte-for-byte against the dossier's own table:
    ``Delivery Note Item``, ``Job Card``, ``POS Invoice Item``, ``Purchase Invoice Item``,
    ``Purchase Receipt Item``, ``Quality Inspection`` (its own ``amended_from``), ``Sales Invoice
    Item``, ``Stock Entry Detail``, ``Subcontracting Receipt Item``.** Seven of the nine are
    fieldtype Link on NON-submittable child tables (the six ``*Item``/``*Detail`` rows, each
    ``is_submittable: 0`` on its own doctype, promoted to its parent's docstatus per the standing
    convention every prior landing's cascade edges already ride) plus the self-referencing
    ``amended_from``. **``Job Card`` is the genuine external submittable edge — ``is_submittable:
    1``, confirmed — the first real blast-radius partner this doctype carries**, unlike Packing
    Slip's/Cost Center Allocation's/Supplier Scorecard Period's own cascade-leaf shape: a submitted
    Job Card whose own ``quality_inspection`` field names this document is discoverable by
    ``get_submitted_linked_docs`` and refuses a leaf-node ``plan_cancel``, the SAME footing every
    other supported doctype's own real submittable link already stands on. ``cascade.py`` needed
    NO changes (doctype-blind, as always).
  * **THE SIDE-SURFACE CAVEAT — A SECOND DOSSIER CORRECTION: FIVE ``@frappe.whitelist()``
    callables, not four.** The dossier's own §9 header claims "4 total, 2 instance + 2 module-
    level" and then separately lists a fifth item (``make_quality_inspection``) as "not decorated
    @frappe.whitelist() — callable via frappe.call() by convention" — **verified WRONG**: line 487
    reads ``@frappe.whitelist()`` immediately above ``def make_quality_inspection(source_name,
    target_doc=None):`` (488), confirmed by both a direct read of the surrounding lines and a
    ``grep -n "@frappe.whitelist"`` sweep of the whole file, which returns five decorator hits
    (155, 175, 369, 469, 487) against five ``def`` lines (156, 176, 371, 471, 488) — a one-to-one
    match. **The real count is FIVE: two instance methods** (``get_item_specification_details``
    (156) appends template-driven rows to ``self.readings``; ``get_quality_inspection_template``
    (176) resolves a template from ``self.bom_no``/``self.item_code`` and calls the first) **and
    three module-level functions** (``item_query`` (371, an autocomplete search filter),
    ``quality_inspection_query`` (471, a second autocomplete search filter),
    ``make_quality_inspection`` (488, a ``get_mapped_doc`` BOM-to-QI mapper that creates a new,
    UNSUBMITTED draft). **This broker's own entry governs ONLY submit/cancel/amend (plus the
    generic get/list wrappers) — it grants NOTHING toward any of these five**, none of which is a
    submit/cancel override; nothing here mutates already-submitted state outside the
    ``update_qc_reference`` finding above.

**Breadth (Installation Note) — Wave 4's fifth row, the twenty-seventh supported doctype, and
Wave 4's SECOND ROW WITH A REAL PARTY FIELD (after Supplier Scorecard Period).** Full source-cited
finding below (dossier at ``docs/plans/dossiers/installation_note.md`` — correct on every axis it
checked; the only imprecision, corrected here, is an incomplete §7 that omits the shared
``validate_qty()`` over-allowance guard every ``StatusUpdater``-descended doctype's own
``update_prevdoc_status()`` call carries). Confirmed from source (``installation_note.json``, 23
fields enumerated via ``json.load``, + ``installation_note.py`` (133 lines), version-16 checkout,
both read 2026-07-21):

  * **``party_field="customer"`` — a real, required, header-level Link** (``reqd: 1``,
    ``installation_note.json`` lines 57-69), the same static-party shape Purchase Order/Purchase
    Receipt/Supplier Quotation/Supplier Scorecard Period already established (never a Dynamic Link
    pair like Quotation/Quality Inspection). Three more Customer-adjacent fields ride alongside as
    metadata, never as the ``party_field`` itself: ``customer_address`` (Link -> Address),
    ``contact_person`` (Link -> Contact), ``customer_name`` (Data, ``read_only: 1``, denormalized),
    and ``customer_group`` (Link -> Customer Group) — none of these is spliced by
    :func:`_list_fields`, the same "one real fieldname, not every party-adjacent column" discipline
    every prior real-party branch already follows.
  * **``status`` field: CONFIRMED PRESENT** (Select, ``installation_note.json`` lines 168-181,
    ``options: "Draft\\nSubmitted\\nCancelled"``, default ``"Draft"``, ``read_only: 1``) —
    stamped by the lifecycle itself (``on_update`` sets ``"Draft"``, ``on_submit`` sets
    ``"Submitted"``, ``on_cancel`` sets ``"Cancelled"``, each via ``self.db_set("status", ...)``),
    never user-writable. **``grand_total`` field: CONFIRMED ABSENT** — no Currency/Float aggregate
    of any kind exists anywhere in the 23-field enumeration; Installation Note is a fulfillment
    record (which items got installed against which Delivery Note), never a transaction with an
    amount.
  * **``in_list_view`` fields, confirmed byte-for-byte via ``json.load``: exactly ONE —
    ``remarks``** (Small Text, line 209) — the NINETEENTH ``_list_fields`` branch (see below), and
    the first branch to combine a REAL spliced ``party_field`` with a genuinely absent
    ``grand_total`` AND no aggregate/type-fork substitute of any kind (the same "no natural
    analog" shape Request for Quotation's/Cost Center Allocation's own branches established, here
    for the first time alongside a real party column rather than instead of one).
  * **``date_field="inst_date"`` — a real, required Date field** (``reqd: 1``,
    ``installation_note.json`` lines 152-160, no default) — the NINTH distinct date-fieldname
    pattern this campaign has found (``posting_date``; ``transaction_date``; ``from_date``;
    ``planned_start_date`` [Datetime]; ``available_for_use_date``; ``valid_from``; ``start_date``;
    ``report_date``; and now ``inst_date``). **Every Date/Datetime field on this doctype named, not
    merely the one chosen:** the schema carries exactly ONE other Date/Datetime-shaped field,
    ``inst_time`` (Time, lines 161-167, no ``reqd``, no default) — a clock-time-only field, never a
    calendar date, and confirmed NOT read anywhere in ``installation_note.py`` for any
    period-lock-relevant purpose. ``inst_date`` rides the existing generic
    :func:`_date_field_for`/:func:`_posting_date_of`/:func:`_locks_for`/
    :func:`pacioli.plan.check_red_line` machinery unchanged — zero new ``plan.py``/``tools.py``
    code for this axis. Installation Note is confirmed ABSENT from ``erpnext/hooks.py``'s
    ``period_closing_doctypes`` list (the same 18-entry list every recent landing has enumerated)
    and posts no GL of any kind (below) — ``inst_date`` is a fulfillment date only, never a
    period-lock-relevant posting date in ERPNext's own accounting sense, though this broker's own
    closed-books disclosure still reads it the same equal-or-stricter way every dated doctype's own
    ``date_field`` is read.
  * **``company`` FIELD: CONFIRMED PRESENT AND REQUIRED** (``reqd: 1``, lines 182-193) — UNLIKE
    Packing Slip/Supplier Scorecard Period, this is not a companyless row; the standing "wrong
    books" belt (the nine ``doc.get("company")`` call sites in ``tools.py``) applies in its
    ordinary form, no new machinery needed.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 133 lines of
    ``installation_note.py``: ``class InstallationNote(TransactionBase):`` (line 13) overrides
    neither ``submit()`` nor ``cancel()`` anywhere** — only ``validate``/``on_update``/
    ``on_submit``/``on_cancel`` and five private helpers are defined, all called by the base
    ``Document.submit()``/``.cancel()``/``.save()``.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE FULL MRO — Installation Note is the Dunning/
    Blanket Order/Job Card/BOM/Work Order/Packing Slip/Cost Center Allocation/Supplier Scorecard
    Period/Quality Inspection "uncallable" category, reached through a NEW, DEEPER MRO than any
    prior member.** ``class InstallationNote(TransactionBase)`` (``installation_note.py:13``),
    and ``class TransactionBase(StatusUpdater)`` (``transaction_base.py:20``) — a full-file grep
    of BOTH ``transaction_base.py`` and ``status_updater.py`` finds no ``make_gl_entries``/
    ``make_sl_entries``/``GLEntry`` reference anywhere in either file. Unlike Packing Slip
    (``PackingSlip(StatusUpdater)``, ONE level above ``Document``), Installation Note's own MRO is
    ``InstallationNote -> TransactionBase -> StatusUpdater -> Document`` — TWO levels — but the
    conclusion is identical: no class in the chain defines ``make_gl_entries``, so ERPNext's own
    ``get_accounting_ledger_preview`` calling ``doc.make_gl_entries()`` bare and unguarded raises
    ``AttributeError`` on a live bench for a real Installation Note. **Fix: joins the
    ledger_preview skip tuple in ``tools.py``, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER,
    JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
    QUALITY_INSPECTION, INSTALLATION_NOTE)``**, with its own honest
    ``_installation_note_ledger_preview_unavailable_flag``.
  * **``on_submit`` (125-128) calls ``validate_serial_no()`` THEN ``update_prevdoc_status()`` THEN
    ``db_set("status", "Submitted")``; ``on_cancel`` (130-132) calls ONLY
    ``update_prevdoc_status()`` then ``db_set("status", "Cancelled")`` — read line by line for
    throws, on both paths:**
    - ``validate_serial_no()`` (97-107) walks every item row and, for a serialized item, checks
      the entered serial numbers EXIST (``is_serial_no_exist``, a ``frappe.db.exists("Serial
      No", ...)`` read) and MATCH the linked Delivery Note Item's own serial numbers
      (``is_serial_no_match``, sourced via ``get_prevdoc_serial_no`` — a second document's own
      field); ``is_serial_no_added`` (74-79) throws if a serialized Item has no serial entered, or
      a non-serialized Item has one. **Every one of these throw conditions requires a READ OF A
      DIFFERENT DOCTYPE** (``Item.has_serial_no``, ``Serial No`` existence, the linked ``Delivery
      Note Item``'s own ``serial_no``) — none is derivable purely from this draft's own fields, the
      SAME reason every dedicated risk-flag function in ``tools.py`` reads only the draft's own
      fields and never fetches a second doctype. Consistent with that standing discipline, no new
      risk-flag function is built for this gate — it is disclosed here as a real, doomed-submit
      path this broker's own ``plan_submit`` structurally cannot preview, the same class of gap
      Sales Order's own ``check_nextdoc_docstatus`` cancel-refusal and Purchase Order's own
      ``check_for_on_hold_or_closed_status`` already carry: an ERPNext-native refusal that surfaces
      as an ordinary answered ``ErpnextError`` at execute time, never bypassed, never silently
      swallowed.
    - ``update_prevdoc_status()`` (inherited from ``StatusUpdater``, ``status_updater.py:193-195``)
      is called by BOTH ``on_submit`` and ``on_cancel`` and is itself ``update_qty()`` (:532-544)
      THEN ``validate_qty()`` (:265 onward) — the exact SAME two-call shape Packing Slip's own
      landing already established. ``update_qty()`` runs a raw ``frappe.db.sql`` ``UPDATE``
      against Delivery Note Item's own ``installed_qty`` column (the doctype's own
      ``status_updater`` config: ``source_dt="Installation Note Item"``,
      ``target_dt="Delivery Note Item"``, ``target_field="installed_qty"``,
      ``target_ref_field="qty"``, ``target_parent_dt="Delivery Note"``,
      ``target_parent_field="per_installed"``) — a quantity COUNTER + percent-complete rewrite on
      the linked Delivery Note, never a GL or Stock Ledger row. ``validate_qty()`` is ERPNext's own
      over-allowance guard (``OverAllowanceError`` via ``limits_crossed_error``, the SAME native
      mechanism this campaign has already named and left unmodeled on Packing Slip's own landing) —
      **it runs on cancel too**, since this doctype's ``status_updater`` entry carries a
      ``target_ref_field`` (unlike a config that opts out via ``validate_qty: False``); in
      practice cancelling only ever EXCLUDES this document's own qty from the target sum
      (``update_qty``'s own ``cond`` flips sign on ``self.docstatus != 1``), so an over-allowance
      throw on the cancel path specifically would require the target to already be over its
      allowance BEFORE this document's own contribution is removed — an edge condition, not the
      ordinary case, and (per the same precedent) not part of this landing's own governance
      surface: it is ERPNext's own native guard, unchanged and unmodeled, the same as every other
      ``StatusUpdater``-descended doctype's own shared mechanism.
    - No ``ignore_linked_doctypes`` is set anywhere in ``installation_note.py`` (confirmed absent
      by grep), and no auto-cancel of any sibling document occurs on either path.
  * **Cascade — INSTALLATION NOTE IS A CASCADE LEAF: the ONLY ``Link -> Installation Note`` field
    in the entire v16 tree is its own self-referencing ``amended_from``** (full-tree grep for
    ``"options": "Installation Note"`` across every DocType JSON in the checkout returns exactly
    ONE hit, ``installation_note.json`` line 202). The doctype's own three references TO a
    Delivery Note (``prevdoc_detail_docname``/``prevdoc_docname``/``prevdoc_doctype``, all on the
    child ``Installation Note Item``) are plain ``Data`` fields storing a name/doctype as a string
    (confirmed from ``installation_note_item.json``'s own 8-field enumeration) — NOT
    ``fieldtype: "Link"``, the same shape Packing Slip's own ``dn_detail``/``pi_detail`` fields
    already established, so they never surface in ``get_submitted_linked_docs``'s Link-field walk
    either direction. ``cascade.py`` needed no changes (doctype-blind, as always), and this landing
    builds no fabricated external blast-radius test, because there is no real external edge to
    fabricate one against.
  * **THE SIDE-SURFACE CAVEAT — ZERO whitelist callables, confirmed by a full grep of
    ``installation_note.py`` for ``@frappe.whitelist()``** — the same smallest-side-surface shape
    Cost Center Allocation's own landing established; nothing to withhold because nothing exists.

**Breadth (Shipment) — Wave 4's sixth row, the twenty-eighth supported doctype, and the FIRST
DOCTYPE WITH TWO INDEPENDENT DYNAMIC-SELECTOR PAIRS.** Full source-cited finding below (dossier at
``docs/plans/dossiers/shipment.md`` — correct on every axis it checked; no dossier error found,
though this landing surfaces one genuine omission the dossier never claimed either way, named
below). Confirmed from source (``shipment.json``, 56 fields enumerated via ``json.load``, +
``shipment.py``, 148 lines, version-16 checkout, both read 2026-07-21):

  * **``party_field=None`` — a THIRD distinct reason for the value, after Quotation's true Dynamic
    Link mechanism and Blanket Order's two-real-gated-Link-fields mechanism.** Shipment carries TWO
    SEPARATE Select-driven trios of mutually exclusive Links, never a single fieldname: pickup side
    — ``pickup_from_type`` (Select, ``Company``/``Customer``/``Supplier``, default ``"Company"``,
    ``shipment.json:72-78``) gates ``pickup_company``/``pickup_customer``/``pickup_supplier`` (each
    ``depends_on``); delivery side — ``delivery_to_type`` (Select, same three options, default
    ``"Customer"``, lines 150-156) gates ``delivery_company``/``delivery_customer``/
    ``delivery_supplier``. Confirmed by reading all 148 lines of ``shipment.py``: no code anywhere
    validates that exactly one of the three gated Links on either side is actually populated or that
    it matches its own type selector — the same client-side-only enforcement Blanket Order's own
    landing already found, doubled.
  * **Each side ALSO carries its own pre-built resolved-value mirror — ``pickup``/``delivery_to``
    (both Data, ``hidden: 1``, ``read_only: 1``, ``in_list_view: 1``, lines 100-107/178-185) — and
    THIS is the genuine omission the dossier never checked either way: these two columns are
    populated ONLY by ``shipment.js``'s client-side form events** (``frm.set_value("pickup",
    frm.doc[pickup_from])`` / ``frm.set_value("delivery_to", frm.doc[delivery_to])``, confirmed by
    grep) **— NEVER by ``shipment.py`` itself** (confirmed: a full grep of ``shipment.py`` for
    ``self.pickup`` / ``self.delivery_to`` finds nothing). A Shipment created via this broker's own
    API path — which never touches the desk form's JS — will show these two list-tier columns
    BLANK regardless of what the underlying gated Link fields actually hold. Disclosed here in
    full, not silently adopted.
  * **List-tier recommendation — the Quotation/Quality Inspection "type and resolved value ride
    together" precedent, DOUBLED for the first time.** Since the schema's own ``in_list_view``
    flags land on ``pickup``/``delivery_to``/``pickup_date`` (never on the raw type/Link fields —
    confirmed by enumerating all 56 fields), and a resolved value is meaningless without knowing
    which of the three doctypes it names, this branch splices BOTH pairs: ``pickup_from_type``
    alongside ``pickup``, and ``delivery_to_type`` alongside ``delivery_to`` — never one column of
    a pair without its other half, applied to TWO independent relationships in the same doctype for
    the first time this campaign has found.
  * **``status`` field: CONFIRMED PRESENT** (Select, ``read_only: 1``, ``Draft``/``Submitted``/
    ``Booked``/``Cancelled``/``Completed``, ``shipment.json:345-353``) — stamped exclusively via
    ``self.db_set("status", ...)``: ``"Draft"`` on ``validate()`` when ``docstatus == 0``
    (``shipment.py:82``), ``"Submitted"`` on ``on_submit`` (``:89``), ``"Cancelled"`` on
    ``on_cancel``/``on_discard`` (``:92``/``:74``). **A genuine verified finding: ``Booked`` and
    ``Completed`` are declared options NEVER set anywhere in this v16 OSS tree** — a full-tree grep
    for either string as a status assignment finds only the type declaration itself and one
    list-view color-coding reference in ``shipment_list.js``; both states are reachable only through
    paid carrier-integration apps outside this checkout. This broker's own tools grant nothing
    toward reaching either state regardless — the governed surface is submit/cancel/amend only.
  * **``grand_total`` field: CONFIRMED ABSENT** across the full 56-field enumeration.
    ``value_of_goods`` (Currency, ``reqd: 1``, ``shipment.json:259-265``) is the summary column —
    computed by ``set_value_of_goods()`` (``shipment.py:109-113``) as the sum of
    ``shipment_delivery_note[].grand_total``, falling back to whatever value was already present if
    that sum is zero. ``total_weight`` (Float, ``read_only: 1``, computed by
    ``get_total_weight()``/``set_total_weight()``, ``:99-103``) is a second candidate summary field
    but carries no ``in_list_view`` flag in the schema and is deliberately excluded — one real
    summary column rides, not every candidate. **``shipment_amount`` (Currency, ``no_copy: 1``,
    ``shipment.json:337-344``) is confirmed present but genuinely unused: a full grep of every
    ``.py`` file in the checkout for ``shipment_amount`` finds only its own auto-generated type
    declaration** — the dossier's own "no GL/SL, carrier-API-populated metadata only" verdict holds.
  * **``company`` field: CONFIRMED ABSENT** — a field-by-field scan of all 56 fields finds no
    fieldname literally ``company`` anywhere (only the gated ``pickup_company``/``delivery_company``
    halves of each dynamic pair) — the THIRD companyless doctype after Packing Slip/Supplier
    Scorecard Period, so ``REG_UNPINNED`` is the only registry shape this doctype can ever govern
    through (same posture, tests below).
  * **``date_field="pickup_date"``** (Date, ``reqd: 1``, ``allow_on_submit: 1``, no default,
    ``shipment.json:266-273``) — the TENTH distinct date-fieldname pattern. ``pickup_from``/
    ``pickup_to`` (both Time, ``allow_on_submit: 1``, ``reqd: 1``, defaults ``"09:00"``/``"17:00"``,
    lines 274-289) are named explicitly and EXCLUDED — clock-time-only fields, never calendar dates,
    never a ``date_field`` candidate. **The Work Order ``allow_on_submit`` note applies again:**
    ``pickup_date`` can move post-submit without a docstatus change, but it still bumps
    ``modified``, so ``check_fresh`` (``plan.py``) catches any plan/execute drift — the standing
    TOCTOU answer, nothing new needed. Confirmed absent from ``erpnext/hooks.py``'s
    ``period_closing_doctypes`` list (the same 18-entry list every recent landing has enumerated);
    Shipment posts no GL of any kind (below), so ``pickup_date`` is a fulfillment date only.
    **CORRECTION (2026-07-21 live-prove batch):** the claim that "this broker's own closed-books
    disclosure still reads it the same equal-or-stricter way every dated doctype's own
    ``date_field`` is read" was wrong for THIS doctype specifically — Shipment is companyless
    (above), so that disclosure actually called ``get_period_locks(None, "Shipment",
    pickup_date)``, which crashes inside ``get_period_locks``'s own ``_doc_path("Company", None)``
    on a live bench. Fixed by a shape-driven guard (falsy ``company`` short-circuits to a plain
    "not applicable" disclosure, never a lock read) in ``tools.py``'s own
    ``_plan_closed_books_risk``/``_locks_for`` — see their docstrings for the full finding.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 148 lines of ``shipment.py``:
    ``class Shipment(Document)`` overrides neither ``submit()`` nor ``cancel()`` anywhere** — only
    ``on_discard``/``validate``/``on_submit``/``on_cancel`` hooks and five private helpers
    (``validate_weight``/``set_total_weight``/``get_total_weight``/``validate_pickup_time``/
    ``set_value_of_goods``) are defined.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: ``class Shipment(Document)``
    (``shipment.py:14``) — a direct ``Document`` subclass, the SIMPLEST MRO in this "uncallable"
    category** (tied with Job Card's/Work Order's/Blanket Order's own bare-``Document`` shape). A
    full-file grep of ``shipment.py`` finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry``
    reference anywhere, and ``Document`` itself carries none either (``frappe/model/document.py``,
    confirmed). ERPNext's own ``get_accounting_ledger_preview`` calling ``doc.make_gl_entries()``
    bare and unguarded would raise ``AttributeError`` on a live bench for a real Shipment. **Fix:
    joins the skip tuple in ``tools.py``, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER,
    JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
    QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT)``**, with its own honest
    ``_shipment_ledger_preview_unavailable_flag``. ``plan_cancel`` needs no equivalent new flag —
    ``get_gl_entries`` honestly returns empty (no GL row was ever written under this voucher_type,
    in either direction).
  * **``on_submit`` (``shipment.py:84-89``) — two throws, both doc-readable, neither flagged as new
    machinery.** ``frappe.throw("Please enter Shipment Parcel information")`` if ``shipment_parcel``
    is empty, and ``frappe.throw("Value of goods cannot be 0")`` if ``value_of_goods == 0`` after
    ``set_value_of_goods()`` runs — both derivable purely from the draft's own fields (a child table
    + a header Currency field), unlike Installation Note's own serial-no gate which needed another
    doctype's state. These are ordinary reqd/business-rule guards, not a hidden or async mechanism —
    consistent with every other "simple row" landing in this campaign (Job Card/BOM/Work
    Order/Packing Slip/Cost Center Allocation/Supplier Scorecard Period), no new risk-flag function
    is built; they surface as an ordinary answered ``ErpnextError`` at execute time, never bypassed,
    never silently swallowed.
  * **``on_cancel`` (``shipment.py:91-92``) carries NO throw of its own** — just
    ``self.db_set("status", "Cancelled")``. (``on_discard``, ``:73-74``, is a SEPARATE hook for
    discarding an unsubmitted draft via the desk's own "discard" action, never reached by this
    broker's own cancel tool, which only ever applies to a submitted, ``docstatus == 1`` document.)
    No ``ignore_linked_doctypes`` is set anywhere (confirmed absent by grep), and no auto-cancel of
    any sibling document occurs on either path.
  * **Cascade — SHIPMENT IS A CASCADE LEAF: the ONLY ``Link -> Shipment`` field in the entire v16
    tree is its own self-referencing ``amended_from``** (full-tree grep for ``'"options":
    "Shipment"'`` across every DocType JSON in the checkout returns exactly ONE hit, ``shipment.json``
    itself). ``cascade.py`` needed no changes (doctype-blind, as always), and this landing builds no
    fabricated external blast-radius test, because there is no real external edge to fabricate one
    against.
  * **THE SIDE-SURFACE CAVEAT — THREE read-only whitelist callables, the WIDEST all-read-only
    surface this campaign has found** (Packing Slip's own precedent was ONE). ``get_address_name``
    (``shipment.py:116-119``, calls ``get_party_shipping_address`` — a read), ``get_contact_name``
    (``:122-125``, calls ``get_default_contact`` — a read), and ``get_company_contact``
    (``:128-147``, reads ``User`` fields behind an explicit ``frappe.has_permission("User", "read",
    throw=True)`` check) — confirmed, each one, to mutate nothing (no ``self.save()``, no
    ``db_set``, no ``frappe.db.set_value`` anywhere in any of the three). Still zero mutation to
    caveat; this entry grants NOTHING toward any of them regardless.

**Breadth (Sales Forecast) — Wave 4's seventh row, the twenty-ninth supported doctype.** Full
source-cited finding below (dossier at ``docs/plans/dossiers/sales_forecast.md`` — correct on
party/status/grand_total/submit_via/ledger_preview-category/cascade-count; one claim this landing
narrows rather than rejects, named below). Confirmed from source (``sales_forecast.json``, 20
fields enumerated via ``json.load``, + ``sales_forecast.py``, 92 lines, version-16 checkout, both
read 2026-07-21):

  * **``party_field=None`` — confirmed by enumerating all 20 fields: zero fields named
    ``customer``/``supplier``/``party``/any Dynamic Link anywhere.** ``company`` (Link, ``reqd:
    1``, ``in_list_view: 1``, ``sales_forecast.json:40-46``) is metadata (which books this demand
    plan belongs to) — never a GL counterparty, the same non-party reading Job Card's/BOM's/Work
    Order's own ``company`` fields already established.
  * **``status`` field: CONFIRMED PRESENT** (Select, ``read_only: 1``, ``in_list_view: 1``,
    options ``"Planned\nMPS Generated\nCancelled"``, ``sales_forecast.json:140-147``) — but **a
    genuine narrowing of the dossier's own §11 wording: the field carries NO schema-level
    ``default`` at all** (confirmed absent from its field dict — unlike ``posting_date``'s own
    ``"default": "Today"`` two fields above it) **and NO code path anywhere ever writes it to
    ``"Planned"``.** A full 92-line read of ``sales_forecast.py`` plus a ``hooks.py`` grep for
    ``"Sales Forecast"`` (zero ``doc_events`` entries of any kind) confirms the ONLY place
    ``self.db_set("status", ...)`` is ever called is ``on_discard`` (``:35-36``, see below), which
    writes ``"Cancelled"`` — never ``"Planned"``. The desk's own list-view color-coding
    (``sales_forecast_list.js``) reads ``doc.status === "Planned"`` as a real branch (not a bare
    ``else``), which is the likely source of the dossier's "defaults to" phrasing, but that is a
    CLIENT-side read of whatever value already got saved, never a value this doctype's own
    server-side code, schema default, or hook chain ever supplies. **Practical consequence for
    this broker: an API-created Sales Forecast (the only kind this broker ever touches) may carry
    a genuinely BLANK ``status`` unless the caller explicitly wrote one** — this landing's own
    fixtures model the conventional desk-entered value (``"Planned"``) precisely because it is
    conventional, never because any code guarantees it.
  * **``grand_total`` field: CONFIRMED ABSENT** across the full 20-field enumeration — no field
    name containing "total" or any monetary meaning exists anywhere on this schema at all (unlike
    Job Card's own non-monetary ``total_completed_qty``/``total_time_in_mins`` counters, this
    doctype carries no aggregate field of ANY kind, monetary or otherwise).
  * **This party=None + status-present + grand_total-absent combination is the SAME SHAPE as
    Material Request/Request for Quotation/Job Card — and, per the same "substitutes differ"
    discipline every prior same-shape branch in this campaign has followed, still NOT a reuse of
    any of them: Sales Forecast has no natural analog for the missing ``grand_total`` at all**
    (``frequency``, ``demand_number``, ``parent_warehouse``, and ``from_date`` are all real,
    non-``in_list_view`` fields — none rides the list tier) **— the TWENTY-FIRST ``_list_fields``
    branch, and the first time this campaign's own bare/no-substitute shape (Request for
    Quotation's own EIGHTH branch) is reached a SECOND time: the requested-column list this
    branch returns is byte-identical to RFQ's own** (``["name", "status", "docstatus", "company",
    date_field, "modified"]``) **— not a reuse (this doctype still forces its own explicit,
    independently source-verified conditional, the same one-branch-per-doctype discipline every
    landing in this campaign follows), but a genuine convergence: both doctypes independently carry
    ``party_field=None``, a real ``status``, an absent ``grand_total`` with no substitute, a real
    ``company``, and exactly one real date field with no other ``in_list_view``-flagged column.**
  * **``date_field`` — TWO real Date fields exist on this schema, the choice made explicit:
    ``posting_date`` (Date, default ``"Today"``, NOT ``reqd``, ``in_list_view: 1``,
    ``sales_forecast.json:51-57``) is chosen over ``from_date`` (Date, default ``"Today"``,
    ``reqd: 1``, NOT ``in_list_view``, ``:97-103``) — the literal, standard fieldname convention
    (SI/PI/PE/JE/Dunning/Stock Reconciliation/Landed Cost Voucher/Job Card's own pattern) wins over
    a field that is required but never surfaced in the doctype's own list view. ``from_date`` is
    real (it feeds ``generate_manual_demand()``'s own date-window math, ``:38-62``) but is a
    workflow-input parameter, not this document's own transactional date column — the same
    "required but not the chosen date_field" shape Blanket Order's own ``to_date`` played
    (context, never the primary), except here ``from_date`` is not even spliced as context, since
    it carries no ``in_list_view`` flag at all. Requires zero new date-fieldname plumbing —
    ``posting_date`` is already the function-level default.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 92 lines of
    sales_forecast.py: ``class SalesForecast(Document):`` (line 11) overrides neither ``submit()``
    nor ``cancel()`` anywhere.** Only ``on_discard`` (line 35) is a lifecycle-adjacent hook; two
    ``@frappe.whitelist()`` callables also exist (``generate_demand`` — an instance method, line
    64; ``create_mps`` — a module-level function, line 70) but neither is a submit/cancel
    override.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: Sales Forecast is the Dunning/Landed
    Cost Voucher/Blanket Order/Job Card/BOM/Work Order/Packing Slip/Cost Center Allocation/
    Supplier Scorecard Period/Quality Inspection/Installation Note/Shipment "uncallable" category
    — and the CLEANEST case in it yet.** ``class SalesForecast(Document):`` (``sales_forecast.py:
    11``) — a direct ``Document`` subclass. Unlike every prior "uncallable" member (even Job
    Card's/Shipment's own bare-``Document`` shape, which still import at least an exception class
    from ``stock_controller``), Sales Forecast's own import block (``sales_forecast.py:4-8``) pulls
    in ONLY ``frappe``, ``frappe._``, ``frappe.model.document.Document``,
    ``frappe.model.mapper.get_mapped_doc``, and ``frappe.utils.add_to_date`` — **zero
    accounting/stock-controller-related imports of any kind.** A full-file grep finds no
    ``make_gl_entries``/``make_sl_entries``/``GLEntry``/``StockLedgerEntry`` reference anywhere.
    ERPNext's own ``get_accounting_ledger_preview`` calling ``doc.make_gl_entries()`` bare and
    unguarded would raise ``AttributeError`` on a live bench for a real Sales Forecast. **Fix:
    joins the skip tuple in ``tools.py``, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER,
    JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
    QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST)``**, with its own honest
    ``_sales_forecast_ledger_preview_unavailable_flag``. ``plan_cancel`` needs no equivalent new
    flag — ``get_gl_entries`` honestly returns empty (no GL row was ever written under this
    voucher_type, in either direction — this doctype posts NOTHING, ever, full stop).
  * **``on_submit``/``on_cancel`` — BOTH CONFIRMED ABSENT ENTIRELY, not merely non-throwing.**
    Sales Forecast is the THIRD doctype in this campaign (after Cost Center Allocation/Supplier
    Scorecard Period) confirmed to define neither hook at all (grep: zero matches for ``def
    on_submit``/``def on_cancel`` in ``sales_forecast.py``). **``on_discard`` (``:35-36``,
    ``self.db_set("status", "Cancelled")``, no ``frappe.throw``) is a SEPARATE hook reachable ONLY
    from ``Document.discard()`` (``frappe/model/document.py:1348-1365``), which itself REFUSES
    unless ``self.docstatus.is_draft()`` — confirmed from frappe-16 source. This broker's own
    ``cancel_sales_forecast`` tool only ever targets a SUBMITTED (``docstatus == 1``) document,
    which calls ERPNext's ``cancel`` action (``Document.cancel()`` → ``_cancel()`` →
    ``run_method("on_cancel")``, ``document.py:1344-1346``/``:1451``) — a hook this doctype does
    not define.** Practical consequence: cancelling a submitted Sales Forecast through this broker
    flips ``docstatus`` 1→2 and changes NOTHING else — the doctype's own ``status`` field is left
    exactly as it was (whatever the caller last wrote, per the finding above), never becoming
    ``"Cancelled"`` in that column. Only a DRAFT discard (a desk-only action this broker's own
    tool surface never reaches) ever writes ``"Cancelled"`` into ``status``. No
    ``ignore_linked_doctypes`` is set anywhere (confirmed absent by grep), and no auto-cancel of
    any sibling document occurs on either path.
  * **Cascade — full-tree grep for ``'"options": "Sales Forecast"'`` across every DocType JSON in
    the checkout returns exactly TWO hits: ``sales_forecast.json`` itself (``amended_from``, the
    standard self-referencing amendment chain) and ``master_production_schedule.json``
    (``sales_forecast``, a real header-level Link, confirmed absent from any child table —
    ``master_production_schedule_item.json``'s own 9-field enumeration carries no such field).**
    This is neither a true cascade leaf (Shipment's/Installation Note's own zero-external-Link
    shape) nor a live external cascade edge (a real doctype that CAN reach ``docstatus == 1``):
    Master Production Schedule's own ``is_submittable`` is confirmed ``None`` (falsy) via
    ``json.load`` — it can never itself be submitted through any normal channel, so
    ``get_submitted_linked_docs``' own submitted-only walk can never return one, regardless of how
    many Sales Forecasts it references. ``cascade.py`` needed no changes (doctype-blind, as
    always); this landing's own blast-radius test pins the MECHANISM against a synthetic stub
    (the same FakeClient double every prior blast-radius test already uses) while naming plainly
    that the specific submitted-MPS scenario it exercises cannot occur on a real bench.
  * **THE SIDE-SURFACE CAVEAT — two whitelisted callables, both read/transform-only, matching the
    dossier's own count.** ``generate_demand()`` (``sales_forecast.py:64-67``, instance method) —
    clears and regenerates the ``items`` child table from ``selected_items``/``from_date``/
    ``frequency``, mutating only this document's own unsaved in-memory rows (no cross-doctype
    write, no auto-submit). ``create_mps(source_name, target_doc=None)`` (``:70-92``, module-level,
    invoked client-side via ``frappe.model.open_mapped_doc`` with the dotted path
    ``erpnext.manufacturing.doctype.sales_forecast.sales_forecast.create_mps`` —
    ``sales_forecast.js:33-38``) maps a submitted Sales Forecast to an UNSAVED Master Production
    Schedule draft (``get_mapped_doc``, ``validation: {"docstatus": ["=", 1]}``) — the caller must
    save/submit it separately; this function itself neither saves nor submits anything. The
    ``SUPPORTED_DOCTYPES`` entry below governs ONLY submit/cancel/amend (plus generic get/list) —
    it grants NOTHING toward either callable.
  * Sales Forecast is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list
    (confirmed absent from the same 18-entry list every recent landing has enumerated) — consistent
    with a doctype that posts no GL of any kind.

**Breadth (Project Update) — Wave 4's eighth row, the thirtieth supported doctype.** Full
source-cited finding below (dossier at ``docs/plans/dossiers/project_update.md`` — correct on
party/status/grand_total/submit_via/ledger_preview-category/cascade-count; two things the
dossier's own file-scoped read never surfaced, named below). Confirmed from source
(``project_update.json``, 9 fields enumerated via ``json.load`` (7 data + 2 layout, matching the
dossier's own count) + ``project_update.py``, 29 lines, + ``project.py`` (the PARENT doctype, a
SEPARATE file), + ``erpnext/hooks.py``, version-16 checkout, all read 2026-07-21):

  * **``party_field=None`` — confirmed by enumerating all 9 fields: the only two Link fields are
    ``project`` (required, ``in_list_view: 1`` — Link to Project, not a GL party entity) and
    ``amended_from`` (self-referential). Zero ``customer``/``supplier``/``party`` fields of any
    kind.**
  * **``status`` field: CONFIRMED ABSENT. ``grand_total`` field: CONFIRMED ABSENT.** No Select
    field exists anywhere in the 9-field enumeration; ``docstatus`` alone tracks Draft/Submitted/
    Cancelled. The ONLY field carrying ``in_list_view: 1`` is ``project`` itself
    (``project_update.json:30``) — confirmed byte-for-byte against the dossier's own claim, the
    narrowest ``in_list_view`` set this campaign has found (even Packing Slip's own bare branch
    keeps three). A real ``sent`` Check field exists (default ``"0"``, ``project_update.json:36-41``)
    but is NOT ``in_list_view``-flagged and, unlike BOM's ``is_active``/``is_default`` (genuine
    lifecycle state this broker's own governance model can read meaningfully) or Work Order's
    ``produced_qty`` (genuine progress state), ``sent`` tracks an unrelated reminder-email side
    channel (see the side-surface finding below) — deliberately NOT spliced in as a manufactured
    substitute for the missing ``status``/``grand_total`` columns.
  * **``company`` FIELD: CONFIRMED ABSENT — THE FOURTH COMPANYLESS DOCTYPE, after Packing
    Slip/Supplier Scorecard Period/Shipment.** The same 9-field enumeration that proves the
    party/status/grand_total findings also proves no ``company`` Link exists anywhere in this
    schema, not even indirectly. The existing "wrong books" belt (nine ``doc.get("company")`` call
    sites in ``tools.py``) already covers this correctly, unchanged: a company-PINNED target
    refuses this doctype (``None`` can never match a real pin), and the documented UNPINNED
    posture (``registry.py``/``REG_UNPINNED``, the same idiom Packing Slip's own landing used
    first) governs it cleanly. No new ``company_field`` pin, no new sentinel, no change to any of
    the nine call sites.
  * **``date_field="date"`` — A GENUINELY NEW SHAPE ON THE DATE AXIS: a REAL, declared date field
    (never the ``date_field=None`` dateless pin BOM/Packing Slip carry) that can still be
    genuinely BLANK on a real, API-authored draft — the first doctype in this campaign to combine
    both.** ``date`` (Date, ``project_update.json:47-52``) carries ``reqd: 0`` and NO ``default``
    key — unlike every prior dated doctype's own date field, which was either ``reqd: 1`` (SI/PI/
    PE/SO/PO/MR/SQ/Q/RFQ/Job Card/Work Order/CCA/SSP/Quality Inspection/Installation
    Note/Shipment) or carried a schema ``default`` (Dunning/Stock Reconciliation/LCV/Blanket
    Order/Asset/Sales Forecast all default to ``"Today"`` or similar). A full read of
    ``project_update.py``'s 29 lines finds no ``validate``/``before_save``/``on_submit`` of any
    kind that ever sets ``date`` server-side — the ONLY code that ever writes it is
    ``project_update.js``'s own ``validate`` client event (browser-only, unreachable via this
    broker's REST-only path) and ``project.py``'s own ``send_project_update_email_to_users``
    (:600-628, a DIFFERENT doctype's module, discussed below), which passes ``"date": today()``
    only when THAT scheduler machinery constructs the draft. **An independently-authored Project
    Update — inserted directly via the REST API, the only kind of draft this broker was ever asked
    to plan/submit from cold — can therefore carry a genuinely blank ``date``.** This is not a gap
    this landing needs to patch: the EXISTING generic machinery already governs it correctly, by
    construction. :func:`pacioli.tools._posting_date_of` reads a missing/``None`` ``date`` as
    ``""`` (the same "empty read on a doctype that DOES declare a date field is unverifiable,
    never dateless" rule its own docstring already states); :func:`pacioli.plan.check_red_line`'s
    existing "no posting_date: refusing to submit an undated posting" branch fires on that empty
    string, both as the ``plan_submit`` DISCLOSURE (Envelope E6, ``ok: True`` with the risk flag
    named) and — more load-bearing — as the real EXECUTE-time refusal
    (``pacioli.spine.governed_submit`` calls ``check_red_line`` against the PLAN's own stored
    ``posting_date``, captured at plan time, so a blank-dated Project Update is refused at
    ``submit_project_update`` too, not merely warned about). Deny-biased, never a crash, never a
    silent bypass: a blank date on this real (not dateless) field denies governance exactly as a
    locked period or an unreadable clock already does elsewhere in this codebase. Zero new
    ``plan.py``/``tools.py`` code for this axis — the finding is that the machinery BOM's own
    landing built already covers this genuinely different (data-nullable, not schema-absent) case
    correctly.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 29 lines of
    ``project_update.py``: ``class ProjectUpdate(Document):`` (line 9) carries a BARE ``pass``
    body (line 29) — no ``submit()``/``cancel()`` override, no ``validate()``, no hook of ANY kind
    beyond the auto-generated type-stub comment block.** The simplest class body this campaign has
    found — even the prior "hookless" trio (Cost Center Allocation/Supplier Scorecard Period/Sales
    Forecast) still defined ``validate()`` or ``on_discard``; Project Update defines nothing at
    all beyond the two module-level functions discussed below.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: Project Update is the Dunning/LCV/Blanket
    Order/Job Card/BOM/Work Order/Packing Slip/Cost Center Allocation/Supplier Scorecard
    Period/Quality Inspection/Installation Note/Shipment/Sales Forecast "uncallable" category, the
    SAME cleanest import shape as Sales Forecast.** ``class ProjectUpdate(Document)`` — a direct
    ``Document`` subclass; the file's own import block (``project_update.py:5-6``) pulls in ONLY
    ``frappe`` and ``frappe.model.document.Document`` — zero accounting/stock-controller-related
    names of any kind. A full-file grep finds no ``make_gl_entries``/``make_sl_entries``/
    ``GLEntry``/``StockLedgerEntry`` reference anywhere. ERPNext's own
    ``get_accounting_ledger_preview`` calling ``doc.make_gl_entries()`` bare and unguarded would
    raise ``AttributeError`` on a live bench for a real Project Update. **Fix: joins the skip
    tuple in ``tools.py``, now ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM,
    WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
    QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE)``**, with its
    own honest ``_project_update_ledger_preview_unavailable_flag``.
  * **``on_submit``/``on_cancel`` — BOTH CONFIRMED ABSENT ENTIRELY, the FOURTH doctype in this
    campaign (after Cost Center Allocation/Supplier Scorecard Period/Sales Forecast) confirmed to
    define neither hook at all** (grep: zero matches for ``def on_submit``/``def on_cancel`` in
    ``project_update.py``, and no ``ignore_linked_doctypes``/``frappe.throw`` of any kind either).
    A governed submit or cancel through this broker changes ``docstatus`` alone; nothing else
    about the document is ever touched by this broker's own write path.
  * **Cascade — PROJECT UPDATE IS A CASCADE LEAF: the ONLY ``Link -> Project Update`` field in
    the entire v16 tree is its own self-referencing ``amended_from``.** A full-tree grep for
    ``'"options": "Project Update"'`` across every DocType JSON in the checkout returns exactly
    ONE hit — ``project_update.json`` line 74, its own ``amended_from`` field. No other doctype
    anywhere carries a header or child-table Link field to Project Update. ``cascade.py`` needed
    no changes (as always), and this landing builds no fabricated "external blast radius" test,
    because there is no real external edge to fabricate one against.
  * **THE SIDE-SURFACE CAVEAT — a TWO-LAYERED finding, the second layer genuinely new and NOT
    surfaced by the dossier's own file-scoped §9/§11 (the same "scoped to the wrong file" shape
    Supplier Scorecard Period's own landing already established).**
    (1) ``project_update.py``'s own whitelist surface: ONE module-level
    ``@frappe.whitelist()`` callable, ``daily_reminder()`` (line 32-53) — calls the un-whitelisted
    private helper ``email_sending()`` (line 56-106), which dispatches ``frappe.sendmail()``
    SYNCHRONOUSLY (line 104, no ``frappe.enqueue``). Confirmed absent from
    ``erpnext/hooks.py``'s ``scheduler_events`` — this function is reachable only by a direct RPC
    call, matching the dossier's own "manually triggered, not a hook" framing exactly. This
    entry grants NOTHING toward it.
    (2) **A GENUINELY NEW FINDING the dossier's own file-scoped read never surfaced: Project's OWN
    module (``project.py``, a DIFFERENT doctype entirely) both auto-CREATES and later
    MUTATES Project Update documents from outside, on a schedule.** ``send_project_update_email_
    to_users()`` (``project.py:600-628``) inserts a brand-new, UNSUBMITTED Project Update
    (``"sent": 0``, ``"date": today()``) whenever a Project's own ``collect_progress`` setting
    fires — called by ``hourly_reminder``/``project_status_update_reminder``'s own
    ``daily_reminder``/``twice_daily_reminder``/``weekly_reminder`` helpers, registered under
    ``erpnext/hooks.py``'s ``hourly``/``hourly_maintenance`` scheduler events (:448, :454-455).
    ``collect_project_status()`` (``project.py:631-663``, ALSO ``hourly_maintenance``, :454)
    appends reply rows to the ``users`` child table and calls ``doc.save(ignore_permissions=True)``
    — a plain save, which Frappe's own non-``allow_on_submit`` field guard means this only ever
    succeeds against a DRAFT. ``send_project_status_email_to_users()`` (``project.py:666-685``,
    ``daily_maintenance``, :475) sends the daily digest email, THEN calls
    ``doc.db_set("sent", 1)`` — a raw DB write that bypasses docstatus/permission checks entirely
    and could, in principle, flip ``sent`` on an ALREADY-SUBMITTED Project Update regardless of
    what this broker's own tool surface exposes. **None of this is a gap this broker's own scope
    must grant or withhold** — it touches no ``docstatus``, no GL, nothing this broker's
    governance model tracks — **but it is load-bearing operational context the dossier's own
    file-scoped RED FLAGS section never surfaced: in the ORDINARY case, a Project Update this
    broker finds was very likely produced (and will later be silently mutated) by Project's own
    scheduler, never authored or touched by a governed write this broker was ever asked to make.**
  * Project Update is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list
    (confirmed absent from the same 18-entry list every recent landing has enumerated) — consistent
    with a doctype that posts no GL of any kind.

**Breadth (Maintenance Visit) — Wave 4's ninth row, the thirty-first supported doctype.** Full
source-cited finding below (dossier at ``docs/plans/dossiers/maintenance_visit.md`` — correct on
party/status/grand_total/date_field/submit_via/ledger_preview-category/cascade-count/whitelist-
count; one real imprecision in its own §7 framing, corrected below, plus a docstatus-guard
question the dossier posed that source settles outright). Confirmed from source
(``maintenance_visit.json``, 32 fields enumerated via ``json.load``, +
``maintenance_visit_purpose.json`` (the child table, 11 fields) + ``maintenance_visit.py`` (210
lines) + ``warranty_claim.json`` (the mutated sibling doctype) + ``erpnext/hooks.py``, version-16
checkout, all read 2026-07-21):

  * **``party_field="customer"`` — a real, required, header-level Link** (``reqd: 1``,
    ``maintenance_visit.json`` lines 65-74), the same static-party shape Installation
    Note/Supplier Scorecard Period already established (never a Dynamic Link pair). Four more
    Customer-adjacent fields ride alongside as metadata, never as the ``party_field`` itself:
    ``customer_address`` (Link -> Address), ``contact_person`` (Link -> Contact),
    ``customer_name`` (Data, denormalized), ``customer_group`` (Link -> Customer Group), and
    ``territory`` (Link -> Territory) — none of these is spliced by :func:`_list_fields`, the same
    "one real fieldname" discipline every prior real-party branch already follows.
  * **``status`` field: CONFIRMED PRESENT** (Select, ``options: "\\nDraft\\nCancelled\\nSubmitted"``,
    default ``"Draft"``, ``read_only: 1``, ``reqd: 1``, ``no_copy: 1``, NOT ``in_list_view``) —
    stamped by the lifecycle itself (``on_submit``/``on_cancel`` each call ``self.db_set("status",
    ...)``), never user-writable. **``grand_total`` field: CONFIRMED ABSENT** — a full enumeration
    of all 32 fields finds not one ``Currency``/``Float``/``Percent`` field anywhere; Maintenance
    Visit is a service-visit fulfillment record, never a transaction with an amount. **TWO
    separate ``in_list_view`` fields, confirmed byte-for-byte via ``json.load``:**
    ``completion_status`` (Select, ``options: "\\nPartially Completed\\nFully Completed"``,
    ``reqd: 1``, ``in_standard_filter: 1``) and ``maintenance_type`` (Select, ``options:
    "\\nScheduled\\nUnscheduled\\nBreakdown"``, default ``"Unscheduled"``, ``reqd: 1``,
    ``in_standard_filter: 1``) — the TWENTY-THIRD ``_list_fields`` branch (see below): the SAME
    categorical shape Installation Note established (real party + status + company present,
    ``grand_total`` absent), but NOT a reuse — Installation Note's own single substitute
    (``remarks``) doesn't exist on this schema, and this branch splices TWO named substitutes
    instead of one. **Neither ``completion_status`` nor ``maintenance_type`` is the submit-
    lifecycle ``status`` — both are orthogonal, user-set data-classification fields**, a genuine
    distinction from every prior doctype's single ``status`` column.
  * **``date_field="mntc_date"`` — a real, required Date field with a schema default**
    (``reqd: 1``, ``default: "Today"``, ``no_copy: 1``, ``maintenance_visit.json`` lines 121-130)
    — the TWELFTH distinct date-fieldname pattern this campaign has found (``posting_date``;
    ``transaction_date``; ``from_date``; ``planned_start_date`` [Datetime]; ``available_for_use_
    date``; ``valid_from``; ``start_date``; ``report_date``; ``inst_date``; ``pickup_date``;
    ``date``; and now ``mntc_date``). Unlike Installation Note's ``inst_date`` (``reqd``, NO
    default) and Project Update's ``date`` (NOT ``reqd``, no default), ``mntc_date`` carries BOTH
    ``reqd: 1`` AND a schema default together. **Every Date/Datetime-shaped field on this doctype
    named, not merely the one chosen:** the schema carries exactly ONE other such field,
    ``mntc_time`` (Time, lines 131-138, no ``reqd``, no default, ``no_copy: 1``) — a clock-time-
    only field, confirmed read in exactly one place in ``maintenance_visit.py``
    (``check_if_last_visit``'s own same-day tie-break comparison, line 184-186), never as a
    period-lock-relevant date and never as the chosen ``date_field``. Maintenance Visit is
    confirmed ABSENT from ``erpnext/hooks.py``'s ``period_closing_doctypes`` list (the same
    18-entry list every recent landing has enumerated) and posts no GL of any kind (below).
  * **``company`` FIELD: CONFIRMED PRESENT AND REQUIRED** (``reqd: 1``, lines 230-239) — the
    standing "wrong books" belt applies in its ordinary form, no new machinery.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 210 lines of
    ``maintenance_visit.py``: ``class MaintenanceVisit(TransactionBase):`` (line 12) overrides
    neither ``submit()`` nor ``cancel()`` anywhere** — only ``validate``/``on_submit``/
    ``on_cancel``/``on_update`` and four private helpers (``validate_serial_no``,
    ``validate_purpose_table``, ``validate_maintenance_date``, ``update_status_and_actual_date``,
    ``update_customer_issue``, ``check_if_last_visit``) are defined, all called by the base
    ``Document.submit()``/``.cancel()``/``.save()``.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE FULL MRO — Maintenance Visit is the
    Dunning/.../Project Update "uncallable" category, the SAME DEPTH OF MRO Installation Note
    established.** ``class MaintenanceVisit(TransactionBase)`` (``maintenance_visit.py:12``), and
    ``class TransactionBase(StatusUpdater)`` (``transaction_base.py:20``) — a full-file grep of
    BOTH ``transaction_base.py`` and ``status_updater.py`` (plus ``maintenance_visit.py`` itself)
    finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry``/``StockLedgerEntry`` reference
    anywhere in any of the three. ERPNext's own ``get_accounting_ledger_preview`` calling
    ``doc.make_gl_entries()`` bare and unguarded raises ``AttributeError`` on a live bench for a
    real Maintenance Visit. **Fix: joins the ledger_preview skip tuple in ``tools.py``, now
    ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
    SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT)``**, with its own honest
    ``_maintenance_visit_ledger_preview_unavailable_flag``.
  * **``on_submit`` (198-201) calls ``update_customer_issue(1)`` THEN ``db_set("status",
    "Submitted")`` THEN ``update_status_and_actual_date()``; ``on_cancel`` (203-206) calls
    ``check_if_last_visit()`` THEN ``db_set("status", "Cancelled")`` THEN
    ``update_status_and_actual_date(cancel=True)`` — read line by line, both directions:**
    - ``update_customer_issue(flag)`` (132-172) is gated on ``if not self.maintenance_schedule:``
      — it never runs at all for a schedule-driven visit. Inside that gate, for EVERY
      ``purposes`` child row where ``d.prevdoc_docname and d.prevdoc_doctype == "Warranty
      Claim"``: it computes ``resolution_date``/``resolved_by``/``resolution_details``/``status``
      (from this document's own ``mntc_date``/``service_person``/``work_done``/
      ``completion_status`` on submit, or from a query for another qualifying sibling — see below
      — on cancel), then ``wc_doc = frappe.get_doc("Warranty Claim", d.prevdoc_docname);
      wc_doc.update({...}); wc_doc.db_update()``. **This is ONE INDEPENDENT WRITE PER MATCHING
      PURPOSE ROW** — a single Maintenance Visit submit (or cancel) can therefore mutate MORE
      THAN ONE Warranty Claim if more than one purpose row names one. **The dossier's own §11 asks
      whether a docstatus guard exists on this write; source settles it outright: Warranty Claim
      carries no ``is_submittable`` key at all** (confirmed from ``warranty_claim.json`` — absent,
      not ``0``) — **it is not a submittable doctype, so there is no docstatus lifecycle for this
      write to bypass.** What ``db_update()`` DOES bypass, regardless, is Warranty Claim's own
      ``validate()``/``on_update`` hooks, permission checks, and version history — a raw column
      UPDATE against an already-loaded document, never a ``doc.save()``, the same
      bypass-the-normal-save-path shape Quality Inspection's own ``update_qc_reference()`` carries
      against a submittable reference document (see :func:`_quality_inspection_cancel_risk_flags`).
      Fully derivable from this draft's own fields (``maintenance_schedule`` + the ``purposes``
      child rows) at plan time, with no second-document read needed — disclosed as a genuine,
      data-driven risk flag (:func:`_maintenance_visit_submit_risk_flags`), the Asset-precedent
      shape, not blanket prose.
    - ``check_if_last_visit()`` (174-196), the on_cancel gate: walks ``purposes`` and keeps
      OVERWRITING ``check_for_docname`` on every row carrying a truthy ``prevdoc_docname`` — so
      only the LAST such row's value is ever checked, and **the dossier's own §7 framing ("the
      SAME Warranty Claim") is imprecise: the raw SQL match is on ``prevdoc_docname`` STRING
      equality alone, with NO filter on ``prevdoc_doctype`` at all** — the source even carries a
      commented-out ``# check_for_doctype = d.prevdoc_doctype`` (line 180), a filter ERPNext's own
      authors considered and left unimplemented. In principle this gate could fire for two
      Maintenance Visits both referencing a same-named Sales Order, not only a Warranty Claim
      (``prevdoc_docname`` is a ``Dynamic Link`` on the ``Maintenance Visit Purpose`` child table,
      confirmed from ``maintenance_visit_purpose.json``, gated by the sibling ``prevdoc_doctype``
      Link field — not restricted to Warranty Claim by schema). The query itself (line 183-186)
      finds every OTHER submitted (``docstatus = 1``) Maintenance Visit sharing that
      ``prevdoc_docname`` with a LATER ``mntc_date`` (or same date, later ``mntc_time``); if any
      exist, ERPNext throws the dossier's own correctly-quoted message —
      ``frappe.throw(_("Cancel Material Visits {0} before cancelling this Maintenance Visit")
      .format(check_lst))`` (line 191-192) — refusing the cancel outright (the following ``raise
      Exception`` on line 194 is unreachable dead code after the throw already raises). **This is
      a SAME-DOCTYPE PEER constraint, invisible to this broker's own blast-radius/cascade
      machinery**: ``prevdoc_docname`` is a Dynamic Link FROM this child table pointing OUT to an
      external document, never a Link TO Maintenance Visit, so ``cascade.py``'s generic Link walk
      cannot see it, and the constraint is checked against OTHER Maintenance Visit rows, not
      documents linking to this one. **Every throw condition requires a sibling read this
      broker's ``plan_cancel`` does not already perform** (a raw SQL join across ``tabMaintenance
      Visit``/``tabMaintenance Visit Purpose``) — the same class of gap Installation Note's own
      ``validate_serial_no`` and Sales/Purchase Order's own native refusals already carry:
      disclosed here as a real, doomed-cancel path this broker's ``plan_cancel`` structurally
      cannot preview without new sibling-query machinery, never invented, never bypassed. If the
      gate does NOT throw, ``self.update_customer_issue(0)`` runs — the SAME Warranty Claim
      write(s) described above, but with reset values computed from a second sibling query (the
      LATEST other ``Partially Completed`` submitted Maintenance Visit against the same
      ``prevdoc_docname``, or a plain reset to ``"Open"``/blank if none exists) — also
      undisclosable in exact value without the same sibling read, but the FACT of the touch is
      disclosed (:func:`_maintenance_visit_cancel_risk_flags`), naming which Warranty Claim(s) are
      candidates from the draft's own fields alone.
    - ``update_status_and_actual_date()`` (102-130, called by BOTH directions) writes
      ``completion_status``/``actual_date`` onto the linked Maintenance Schedule Detail row(s) —
      via ``frappe.db.set_value`` (an even more direct bypass than ``db_update()``: no document
      load at all) — a THIRD doctype touched, a quantity/metadata counter only, never a GL or
      Stock Ledger row.
  * **Cascade — MAINTENANCE VISIT IS A CASCADE LEAF: the ONLY ``Link -> Maintenance Visit`` field
    in the entire v16 tree is its own self-referencing ``amended_from``** (full-tree grep for
    ``'"options": "Maintenance Visit"'`` across every DocType JSON in the checkout returns exactly
    ONE hit, ``maintenance_visit.json``'s own ``amended_from`` field — confirmed matching the
    dossier's own §8 count). ``cascade.py`` needed no changes (as always), and this landing builds
    no fabricated external blast-radius test, because there is no real external edge to fabricate
    one against; the temporal-ordering peer constraint above is a SEPARATE, same-doctype mechanism,
    not a cascade edge.
  * **THE SIDE-SURFACE CAVEAT — ZERO whitelist callables, confirmed by a full grep of
    ``maintenance_visit.py`` for ``@frappe.whitelist()``** — matching the dossier's own §9 count;
    the same smallest-side-surface shape Installation Note's own landing established. Nothing to
    withhold because nothing exists.
  * Maintenance Visit is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list
    (confirmed absent from the same 18-entry list every recent landing has enumerated) —
    consistent with a doctype that posts no GL of any kind.

**Breadth (Maintenance Schedule) — Wave 4's tenth row, the thirty-second supported doctype.** Full
source-cited finding below (dossier at ``docs/plans/dossiers/maintenance_schedule.md`` — correct on
every axis it checks: party/status/grand_total/date_field/submit_via/ledger_preview-category/
cascade-count/whitelist-count; two genuine precision gaps closed below, neither a factual error).
Confirmed from source (``maintenance_schedule.json``, 24 fields enumerated via ``json.load``, +
``maintenance_schedule.py`` (495 lines) + ``maintenance_visit.json``/``maintenance_visit_purpose.
json`` (the cascade partner and its own Dynamic Link) + frappe's own ``event.json`` (the
auto-created sibling) + ``erpnext/hooks.py``, version-16 checkout, all read 2026-07-21):

  * **``party_field="customer"`` — a real, header-level Link, but the FIRST party_field row in
    this whole campaign where the field itself is NOT required.** ``customer`` (Link -> Customer,
    ``maintenance_schedule.json`` lines 51-61) carries no ``"reqd"`` key at all — confirmed absent,
    not ``0`` — unlike Installation Note's/Maintenance Visit's/Supplier Scorecard Period's own
    required ``customer``/``supplier``. The decision still lands on ``party_field="customer"``:
    the campaign's own test has always been "a real, static, singular header-level Link", never
    reqd-ness, and nothing in :func:`_list_fields`/``list_documents`` depends on the field being
    required — a blank ``customer`` on this list-tier column simply reads back empty for that row,
    the same tolerance every nullable Link column already has. Four more Customer-adjacent fields
    ride alongside as metadata only, never spliced: ``customer_address`` (Link -> Address),
    ``contact_person`` (Link -> Contact), ``customer_group`` (Link -> Customer Group), and
    ``territory`` (Link -> Territory).
  * **``status`` field: CONFIRMED PRESENT** (Select, ``maintenance_schedule.json`` lines 67-79,
    options ``"\\nDraft\\nSubmitted\\nCancelled"``, default ``"Draft"``, ``read_only: 1``,
    ``reqd: 1``, NOT ``in_list_view`` — stamped by the lifecycle itself via ``on_update``'s own
    ``self.db_set("status", "Draft")`` alongside ``on_submit``/``on_cancel``). **``grand_total``
    field: CONFIRMED ABSENT** — a full enumeration of all 24 fields finds not one ``Currency``/
    ``Float``/``Percent`` field anywhere; a maintenance contract schedule carries no amount of its
    own. **Exactly ONE ``in_list_view`` field, confirmed via ``json.load``: ``customer_name``**
    (Data, ``bold: 1``, ``in_global_search: 1``, lines 129-140, denormalized from ``customer``) —
    forces the TWENTY-FOURTH ``_list_fields`` branch (see below): the SAME categorical shape
    Installation Note established (real party + status + company present, ``grand_total`` absent),
    but NOT a reuse — Installation Note's own single substitute (``remarks``) doesn't exist on
    this schema; this branch splices its OWN single substitute (``customer_name``) instead.
  * **``date_field="transaction_date"`` — a real, required Date field with NO schema default**
    (``reqd: 1``, no ``"default"`` key, lines 80-87) — REJOINS the standing Sales Order/Purchase
    Order/Material Request/Supplier Quotation/Quotation/Request for Quotation ``transaction_date``
    set as its SEVENTH member, needing zero new date-fieldname plumbing (contrast the eleven
    doctype-specific patterns Wave 3/4 has otherwise been finding one after another). ``company``
    (Link -> Company, lines 211-220) **CONFIRMED PRESENT AND REQUIRED** — the standing "wrong
    books" belt applies in its ordinary form.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 495 lines of
    ``maintenance_schedule.py``: ``class MaintenanceSchedule(TransactionBase):`` (line 13)
    overrides neither ``submit()`` nor ``cancel()`` anywhere** — only ``validate``/``on_submit``/
    ``on_cancel``/``on_update``/``on_trash`` hooks and private helpers are defined, all called by
    the base ``Document.submit()``/``.cancel()``/``.save()``.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE FULL MRO — Maintenance Schedule is the
    Dunning/.../Maintenance Visit "uncallable" category, the SAME MRO DEPTH Installation
    Note/Maintenance Visit established.** ``class MaintenanceSchedule(TransactionBase)``
    (``maintenance_schedule.py:13``), and ``class TransactionBase(StatusUpdater)``
    (``transaction_base.py:20``) — a full-file grep of ``maintenance_schedule.py``,
    ``transaction_base.py``, AND ``status_updater.py`` finds no ``make_gl_entries``/
    ``make_sl_entries``/``GLEntry``/``StockLedgerEntry`` reference anywhere in any of the three.
    ERPNext's own ``get_accounting_ledger_preview`` calling ``doc.make_gl_entries()`` bare and
    unguarded raises ``AttributeError`` on a live bench for a real Maintenance Schedule. **Fix:
    joins the ledger_preview skip tuple in ``tools.py``, now ``(DUNNING, LANDED_COST_VOUCHER,
    BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION,
    SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST,
    PROJECT_UPDATE, MAINTENANCE_VISIT, MAINTENANCE_SCHEDULE)``**, with its own honest
    ``_maintenance_schedule_ledger_preview_unavailable_flag``.
  * **``on_submit`` (102-158), read line by line — the dossier's own sharpest ask, settled to the
    mechanism:**
    - Line 103-104: ``if not self.get("schedules"): throw(_("Please click on 'Generate Schedule'
      to get schedule"))`` — a doomed-submit gate readable off the draft's OWN ``schedules`` child
      table before a marker is ever minted (empty list == certain refusal), the same "status gate
      readable on the draft" shape Quality Inspection's own ``before_submit`` disclosure
      established. ``check_serial_no_added()``/``validate_schedule()`` (105-106) are ordinary
      cross-table consistency guards that only fire on a genuinely malformed schedule
      (items/schedules item_code mismatch) — the same "doc-readable but ordinary business-rule
      guard" class Shipment's/RFQ's own throws already earned no dedicated flag for.
    - **Per ``items`` row carrying a ``serial_and_batch_bundle`` (110-117): ``validate_serial_no()``
      can THROW** (under warranty, under an existing maintenance contract, or delivered after this
      row's own ``start_date``) **then ``update_amc_date(serial_nos, d.end_date)`` runs — settled
      precisely from ``update_amc_date``'s own body (301-305): ``serial_no_doc = frappe.get_doc(
      "Serial No", serial_no); serial_no_doc.amc_expiry_date = amc_expiry_date; serial_no_doc.
      save()`` — A FULL DOCUMENT ``.save()`` (``validate()``/hooks/versioning ALL run), never
      ``db_set()`` or raw SQL.** This is the FIRST cross-document mutation this entire campaign has
      found that runs through the NORMAL, hook-respecting save path rather than a bypass —
      contrast Maintenance Visit's own Warranty Claim ``db_update()``, Quality Inspection's raw-SQL
      Job Card ``UPDATE``, and Maintenance Visit's own ``update_status_and_actual_date`` via
      ``frappe.db.set_value`` (no document load at all). Fully derivable from the draft's own
      ``items`` rows (item_code + ``serial_and_batch_bundle`` id) at plan time; the ACTUAL resolved
      Serial Nos are not (that needs a live ``Serial and Batch Bundle`` read this disclosure cannot
      perform) — disclosed as a data-driven flag naming the item row + bundle, never the resolved
      serial numbers (:func:`_maintenance_schedule_submit_risk_flags`).
    - **THE DOSSIER'S OWN SHARPEST FINDING, NAMED PRECISELY: Event auto-creation (134-156).** For
      every ``items`` row, ``frappe.db.get_all("Maintenance Schedule Detail", {"parent": self.
      name, "item_code": d.item_code}, ["scheduled_date"])`` re-reads this row's own already-saved
      ``schedules`` entries, and for EACH matching row builds
      ``frappe.get_doc({"doctype": "Event", "owner": ..., "subject": ..., "starts_on":
      cstr(scheduled_date) + " 10:00:00", "event_type": "Private"})``, calls ``event.
      add_participant(self.doctype, self.name)``, then ``event.insert(ignore_permissions=1)`` —
      confirmed from frappe's own ``event.json``: ``is_submittable`` is unset entirely (not ``0``)
      — Event is not a submittable doctype at all, so these are ordinary Desk records inserted
      directly, NEVER themselves submitted. In the ordinary case (schedule rows built 1:1 by
      ``generate_schedule()``) this creates exactly ``len(schedules)`` Event documents — disclosed
      as a data-driven count read straight off the draft's own ``schedules`` rows, no second read
      needed.
    - Line 158: ``self.db_set("status", "Submitted")`` — the final stamp.
  * **``on_cancel`` (391-402), read line by line — VERIFIED PLAINLY (the Work Order §9 lesson):
    ZERO reference to Maintenance Visit anywhere in the method, or anywhere else in the file** (a
    full grep of ``maintenance_schedule.py`` for ``"Maintenance Visit"`` returns nothing outside
    the module-level ``make_maintenance_visit()`` mapper). No ``ignore_linked_doctypes``, no query
    against submitted Maintenance Visit rows, no throw of any kind (a full grep of ``on_cancel``'s
    own body finds no ``frappe.throw``/``throw`` call) — the dossier's own claim holds exactly as
    stated, confirmed rather than merely repeated. What ``on_cancel`` DOES do:
    - 393-399: per ``items`` row with a ``serial_and_batch_bundle``, ``update_amc_date(serial_nos)``
      with NO date argument — the SAME full ``.save()`` mechanism as submit, this time clearing
      the field back to ``None`` rather than setting it.
    - 401: ``self.db_set("status", "Cancelled")``.
    - 402: ``delete_events(self.doctype, self.name)`` (``transaction_base.py:582-599``) — queries
      ``tabEvent``/``tabEvent Participants`` for every Event whose participant row names this
      exact ``(doctype, name)``, then ``frappe.delete_doc("Event", events, for_reload=True)`` — a
      REAL, PERMANENT delete, not a soft-delete and not mere orphaning, of every Event this same
      document's own submit created.
    - **A THIRD lifecycle hook the dossier's own §7 never named: ``on_trash`` (404-405) ALSO calls
      ``delete_events(self.doctype, self.name)``** — the identical Event cleanup fires whether this
      document is merely cancelled or fully deleted from the desk, a genuinely new finding beyond
      the dossier's own on_cancel-scoped read.
  * **Cascade — GENUINELY NOT A LEAF, rare for a Wave 4 row: a full-tree grep for
    ``'"options": "Maintenance Schedule"'`` returns exactly TWO hits** (confirmed matching the
    dossier's own §8 count): Maintenance Schedule's own self-referencing ``amended_from``, and
    Maintenance Visit's own ``maintenance_schedule`` field (Link, ``read_only: 1``, NOT ``reqd``,
    ``maintenance_visit.json`` lines 279-284) on a doctype confirmed ``is_submittable: 1``. This is
    a REAL, STATIC Link — never a Dynamic Link — so it is fully visible to this broker's own
    ``get_submitted_linked_docs`` walk with zero ``cascade.py`` changes needed (the same generic
    mechanism every prior real edge already rides). **Practical consequence: a submitted
    Maintenance Visit naming this Maintenance Schedule REFUSES this broker's own leaf-node
    ``plan_cancel`` outright (the blast-radius gate fires first, naming the link) — even though
    ``on_cancel`` above enforces NOTHING of the kind. This broker's own governance is genuinely
    STRICTER than ERPNext here**, an equal-or-stricter finding pinned by a real blast-radius test
    (the Job Card/Quality Inspection precedent), never a fabricated one. Separately confirmed:
    Maintenance Visit Purpose's own Dynamic Link pair (``prevdoc_doctype``/``prevdoc_docname``,
    ``maintenance_visit_purpose.json`` lines 88-99) carries NO edge back to Maintenance Schedule in
    practice — ``make_maintenance_visit()``'s own ``get_mapped_doc`` field map (472-493) never sets
    either field on the purposes it creates, and the only other assignments to ``prevdoc_doctype``
    anywhere in the v16 tree set it to ``"Sales Order"`` or ``"Warranty Claim"`` (confirmed by
    grep) — correctly absent from the dossier's own cascade table.
  * **THE SIDE-SURFACE CAVEAT — FIVE ``@frappe.whitelist()`` callables, confirmed by a full grep of
    ``maintenance_schedule.py``, matching the dossier's own §9 count exactly:** three instance
    methods (``generate_schedule`` (48), guarded ``if self.docstatus != 0: return`` — draft-only,
    never mutates a submitted document; ``validate_end_date_visits`` (71), an in-memory calc-only
    helper; ``get_pending_data`` (407), read-only) and two module-level functions
    (``get_serial_nos_from_schedule`` (432), read-only; ``make_maintenance_visit`` (446), requires
    the source's ``docstatus == 1`` but only RETURNS an unsaved mapped Maintenance Visit doclist —
    the caller must still insert/submit it themselves, no direct mutation here). Nothing to
    withhold because nothing here mutates already-submitted state outside the findings above.
  * Maintenance Schedule is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list
    (confirmed absent from the same 18-entry list every recent landing has enumerated) —
    consistent with a doctype that posts no GL of any kind. The dossier's own §11 reports "None
    found" for RED FLAGS — accurate as far as it checked (no async, no background jobs, no status
    mutator outside submit/cancel) — but undersells the two structural findings this landing's own
    read surfaces: the real, stricter-than-ERPNext blast-radius edge above, and the
    create-then-permanently-delete Event side effect spanning submit, cancel, AND on_trash.

**Breadth (Asset Maintenance Log) — Wave 5's first row, the thirty-third supported doctype.** Full
source-cited finding below (dossier at ``docs/plans/dossiers/asset_maintenance_log.md`` — correct
on party/grand_total/submit_via/ledger_preview-category/cascade-count/whitelist-count; the
dossier's own summary table is SELF-CONTRADICTORY — it calls the doctype "DATELESS in GL terms"
in prose while its own §3 lists two real Date fields, and its own line citations for
``erpnext/hooks.py`` are off by roughly 1400 lines — both settled below from source, neither a
factual miss on the fields/hooks themselves). Confirmed from source (``asset_maintenance_log.json``,
23 fields enumerated via ``json.load``, + ``asset_maintenance_log.py`` (97 lines) +
``asset_maintenance.py`` (the parent doctype whose own ``on_update`` drives this one) +
``erpnext/hooks.py``, version-16 checkout, all read 2026-07-21):

  * **``party_field=None`` — the standing "no party concept at all" shape** (Stock Entry/Stock
    Reconciliation/LCV/Blanket Order/BOM/Job Card/Work Order/Cost Center Allocation/Sales
    Forecast/Project Update's own reason, not a new one). The only Link fields on this schema are
    ``asset_maintenance`` (Link -> Asset Maintenance, lines 36-40) and ``task`` (Link -> Asset
    Maintenance Task, lines 76-80) plus the self-referencing ``amended_from`` — confirmed by a
    full 23-field enumeration: no ``customer``/``supplier``/``party`` fieldname anywhere. These two
    are operational routing (which parent record and which task this log reports against), never
    a GL party.
  * **``maintenance_status`` field: CONFIRMED PRESENT, but this is the FIRST doctype in the whole
    campaign whose lifecycle-adjacent Select is NOT literally named ``"status"``.** (Select,
    ``asset_maintenance_log.json`` lines 118-125, options ``"Planned\\nCompleted\\nCancelled\\n
    Overdue"`` — matching the dossier exactly — ``reqd: 1``, ``in_standard_filter: 1``.)
    **A genuine dossier omission: NEITHER ``read_only`` NOR ``default`` is set at all** (both keys
    confirmed absent, not ``0``/``""``) — unlike every prior campaign doctype's own status column
    (always ``read_only: 1``, stamped only by ``db_set`` from a hook), this field is directly
    WRITABLE by whoever creates or edits the document, required, with no schema default. Because
    the real fieldname is ``maintenance_status`` and not ``status``, this broker's own
    :func:`_list_fields` cannot splice the literal string ``"status"`` here the way every prior
    branch has — that would ask the bench for a column this schema does not carry, the same
    unknown-column failure class every "not a reuse" branch in this campaign avoids; the literal
    ``"maintenance_status"`` fieldname rides instead (see below). **``grand_total`` field:
    CONFIRMED ABSENT** — the full 23-field enumeration finds no ``Currency``/``Float``/``Percent``
    field anywhere; a maintenance log carries no amount of its own.
  * **Every Date/Datetime field, enumerated — settling the dossier's own self-contradiction.** The
    dossier's summary table calls this doctype "DATELESS in GL terms" while its own §3 lists TWO
    real ``Date`` fields: ``due_date`` (``in_list_view: 1``, ``read_only: 1``, ``fetch_from:
    "task.next_due_date"``, lines 104-111 — a scheduling REFERENCE copied in from the parent task,
    never written by this document's own lifecycle) and ``completion_date`` (``in_list_view: 1``,
    lines 112-117 — writable, ``reqd`` absent, no schema default — the field recording when THIS
    log's own maintenance work actually happened). **Per this campaign's own declared-dateless
    rule (the BOM precedent — ``date_field=None`` is a pin for a doctype with ZERO Date/Datetime
    fields of any kind), a doctype carrying even ONE real Date field is a normal dated row, never
    dateless — "no ``posting_date``-shaped field" is not the same claim as "no date field," and
    the dossier's own prose conflated them.** ``date_field="completion_date"`` is the decision:
    the operational date (when the work was actually completed — the same "the real transactional
    date, not a scheduling target" test every prior date-field choice in this campaign has
    followed), never ``due_date`` (a read-only, inbound-only reference this document's own
    ``on_submit``/``validate`` never write). **Governability of a blank ``completion_date`` — the
    Project Update precedent, but STRONGER: here a blank value is not an accident, it is
    VALIDATE()-ENFORCED for one entire class of valid submitted documents.** ``validate()``
    (lines 54-55) throws if ``maintenance_status != "Completed"`` AND ``completion_date`` is set —
    meaning every submitted ``"Cancelled"`` Asset Maintenance Log MUST carry a blank
    ``completion_date`` by construction, not by omission. The EXISTING ``_posting_date_of``/
    ``check_red_line`` machinery already governs this correctly with zero new code (Project
    Update's own precedent): a blank read denies at both the plan-time disclosure (Envelope E6)
    and the real execute-time gate, deny-biased, never a crash, never a silent bypass — proven
    below in tests, not merely asserted. THIS is the thirteenth distinct date-fieldname pattern
    this campaign has found (rejoining no prior set — ``completion_date`` is a new fieldname).
  * **``company`` field: CONFIRMED ABSENT** — the full 23-field enumeration finds no fieldname
    literally ``company`` anywhere — **the FIFTH companyless doctype after Packing Slip/Supplier
    Scorecard Period/Shipment/Project Update**, so ``REG_UNPINNED`` is the only registry shape this
    doctype can ever govern through (same posture, tests below).
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 97 lines of
    ``asset_maintenance_log.py``: ``class AssetMaintenanceLog(Document)`` overrides neither
    ``submit()`` nor ``cancel()`` anywhere** — only ``validate``/``on_submit``/
    ``update_maintenance_task`` (a private helper) and one module-level whitelisted search helper
    are defined.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: ``class AssetMaintenanceLog(Document)``
    (``asset_maintenance_log.py:14``) — a DIRECT ``Document`` subclass, the SIMPLEST MRO in the
    "uncallable" category** (tied with Job Card's/Work Order's/Blanket Order's/Shipment's own
    bare-``Document`` shape). A full-file grep of ``asset_maintenance_log.py`` finds no
    ``make_gl_entries``/``make_sl_entries``/``GLEntry``/``StockLedgerEntry`` reference anywhere.
    ERPNext's own ``get_accounting_ledger_preview`` calling ``doc.make_gl_entries()`` bare and
    unguarded raises ``AttributeError`` on a live bench for a real Asset Maintenance Log. **Fix:
    joins the ledger_preview skip tuple in ``tools.py``, now ``(DUNNING, LANDED_COST_VOUCHER,
    BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION,
    SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST,
    PROJECT_UPDATE, MAINTENANCE_VISIT, MAINTENANCE_SCHEDULE, ASSET_MAINTENANCE_LOG)``**, with its
    own honest ``_asset_maintenance_log_ledger_preview_unavailable_flag``.
  * **``validate()``/``on_submit`` (lines 44-60), read line by line — a fully deterministic
    doomed-submit gate, readable off the draft's own ``maintenance_status``/``completion_date``
    fields before a marker is ever minted (the Quality Inspection ``before_submit`` precedent):**
    - Lines 45-49: ``if getdate(self.due_date) < getdate(nowdate()) and self.maintenance_status
      not in ["Completed", "Cancelled"]: self.maintenance_status = "Overdue"`` — runs on EVERY
      save INCLUDING the submit path (frappe's own ``run_before_save_methods()`` calls
      ``validate`` for both the ``"save"`` and ``"submit"`` actions, confirmed from
      ``frappe/model/document.py:1402-1407`` — ``validate()`` always fires before ``on_submit``
      within the same call). An in-flight auto-flip to ``"Overdue"``, never a help toward passing
      the gate below (Overdue is not in the allowed set either).
    - Line 51-52: throws ``"Please select Completion Date for Completed Asset Maintenance Log"``
      if ``maintenance_status == "Completed"`` and ``completion_date`` is blank.
    - Line 54-55: throws ``"Please select Maintenance Status as Completed or remove Completion
      Date"`` if ``maintenance_status != "Completed"`` and ``completion_date`` is set.
    - Line 58-59 (``on_submit``, only reached if ``validate()`` above did not already throw):
      throws ``"Maintenance Status has to be Cancelled or Completed to Submit"`` unless
      ``maintenance_status`` is exactly ``"Completed"`` or ``"Cancelled"``.
    **Net effect, fully derivable from the draft: submit succeeds ONLY for
    (``"Completed"`` + a set ``completion_date``) or (``"Cancelled"`` + a blank
    ``completion_date``) — every other combination is doomed**, disclosed precisely
    (:func:`_asset_maintenance_log_submit_risk_flags`).
    - Line 60, the real side effect: ``self.update_maintenance_task()`` (lines 62-77) — a
      genuine cross-document cascade beyond the dossier's own §6 framing. It loads the linked
      Asset Maintenance TASK (``frappe.get_doc("Asset Maintenance Task", self.task)``) and, only
      when ``maintenance_status == "Completed"`` AND the Task's own ``last_completion_date``
      differs from this log's ``completion_date``, sets ``last_completion_date``/a recalculated
      ``next_due_date``/``maintenance_status = "Planned"`` on the TASK and calls a FULL
      ``.save()`` (validate/hooks/versioning all run — the Maintenance Schedule Serial No
      precedent); when ``maintenance_status == "Cancelled"``, it instead sets the TASK's own
      ``maintenance_status = "Cancelled"`` and saves. **Then, UNCONDITIONALLY (outside either
      branch), it loads the PARENT Asset Maintenance record itself
      (``frappe.get_doc("Asset Maintenance", self.asset_maintenance)``) and calls a full
      ``.save()`` on it too** — which re-triggers that PARENT's own ``on_update`` hook
      (``asset_maintenance.py:43-46``: ``assign_tasks()`` for every task row, plus
      ``sync_maintenance_tasks()`` (53-68), which re-walks the parent's ENTIRE task table and can
      create or update OTHER SIBLING Asset Maintenance Log documents via
      ``update_maintenance_log()`` (``asset_maintenance.py:125-162``). A real, source-verified
      blast radius beyond this single document — named as a mechanism, never enumerated further
      (the actual sibling rows touched need a live read of the parent's own task table this
      static disclosure cannot perform, the same honesty limit the Maintenance Schedule Serial No
      disclosure already established).
  * **``on_cancel`` is CONFIRMED ABSENT ENTIRELY** — a full-file grep of ``asset_maintenance_log.py``
    finds no ``on_cancel`` method of any kind. Frappe's own cancel path
    (``frappe/model/document.py``'s ``_save()``) SKIPS the doctype's ``validate()`` hook for the
    ``"cancel"`` action entirely (``run_before_save_methods`` calls only ``before_cancel``, never
    defined here either) — cancelling this doctype is a PURE ``docstatus`` flip plus frappe's own
    generic ``check_no_back_links_exist()``. **Practical consequence: cancelling an Asset
    Maintenance Log NEVER reverses the Task/parent-Asset-Maintenance mutation ``on_submit``
    performed** — there is no code path that undoes it. No ``ignore_linked_doctypes`` (confirmed
    absent), no throw of any kind.
  * **A RED FLAG BEYOND THE DOSSIER'S OWN §11 SCOPE: TWO distinct mechanisms rewrite
    ``maintenance_status`` completely outside ``validate``/``on_submit``, neither gated by
    ``docstatus`` in its own filter — but they differ sharply in what each can actually reach, a
    precision this landing settles rather than assumes.**
    (1) The dossier's own scheduler finding, confirmed and precisely re-pinned: the
    ``"daily_maintenance"`` scheduler job ``update_asset_maintenance_log_status()``
    (``asset_maintenance_log.py:80-88``, registered ``erpnext/hooks.py:485`` — NOT "~1847" as the
    dossier states; ``hooks.py`` is 712 lines total, a dossier line-number correction, not a
    content error) runs a raw ``frappe.qb`` UPDATE — ``.set(AssetMaintenanceLog.maintenance_status,
    "Overdue").where((maintenance_status == "Planned") & (due_date < today()))`` — a bare SQL
    statement: no document load, no hooks, no permission check, no ``docstatus`` condition. Its own
    WHERE clause requires ``maintenance_status == "Planned"``, though, and ``on_submit`` above
    refuses submission unless ``maintenance_status`` is already ``"Completed"``/``"Cancelled"`` —
    so under normal operation a SUBMITTED document's status can never read back ``"Planned"``,
    meaning this scheduler's real blast radius is DRAFT documents left sitting past their
    ``due_date``, not submitted ones — a precision correction on the framing, not a factual miss
    on the mechanism itself.
    (2) **The sharper, genuinely dossier-missed finding: ``AssetMaintenance.sync_maintenance_tasks()``
    (``asset_maintenance.py:53-68``), called from that PARENT doctype's own ``on_update``
    (``asset_maintenance.py:43-46`` — fires on EVERY save of the parent record, not on a
    schedule), calls ``maintenance_log.db_set("maintenance_status", "Cancelled")`` for every Asset
    Maintenance Log whose ``task`` is no longer in the parent's own current task list — carrying
    NO ``maintenance_status`` or ``docstatus`` filter at all.** ``db_set()``'s own docstring
    (``frappe/model/document.py:1507-1511``) states plainly it "does not trigger controller
    validations" — so this mechanism genuinely CAN, and does, silently rewrite a SUBMITTED,
    ``"Completed"`` Asset Maintenance Log's own status to ``"Cancelled"``, with no ``validate()``,
    no ``on_update``, no version entry (``track_changes: 1`` on this schema notwithstanding —
    ``db_set`` bypasses the normal ``save()`` version path too), no permission re-check on the log
    itself — merely by editing an UNRELATED Asset Maintenance record's own child task table. The
    dossier's own §11 named only mechanism (1).
  * **Cascade — CASCADE LEAF: the ONLY ``Link -> Asset Maintenance Log`` field in the entire v16
    tree is its own self-referencing ``amended_from``** (full-tree grep for ``'"options": "Asset
    Maintenance Log"'`` across every DocType JSON in the checkout returns exactly ONE hit,
    ``asset_maintenance_log.json`` itself, matching the dossier's own §8). ``cascade.py`` needed no
    changes, and this landing builds no fabricated external blast-radius test.
  * **THE SIDE-SURFACE CAVEAT — ONE** ``@frappe.whitelist()`` **callable, confirmed by a full grep
    of ``asset_maintenance_log.py``, matching the dossier's own §9 count exactly:**
    ``get_maintenance_tasks`` (module-level, lines 91-97, also decorated
    ``@frappe.validate_and_sanitize_search_inputs``) — an autocomplete search helper returning
    ``frappe.db.get_values(...)``, confirmed read-only (no ``save``/``db_set``/``frappe.db.set_value``
    anywhere in its body). Nothing to withhold because nothing mutates.
  * Asset Maintenance Log is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list
    (18 entries, ``hooks.py:326-345`` — NOT "~1850" as the dossier states, the same line-number
    correction as the scheduler registration above) — consistent with a doctype that posts no GL
    of any kind; the wildcard ``doc_events["*"]["validate"]`` hooks (SLA apply, deletion-job
    guard) apply to every doctype equally and carry no accounting-period gate of their own.

**Breadth (Bank Guarantee) — Wave 5's second row, the thirty-fourth supported doctype, and the
SECOND doctype in this campaign whose own dossier gets ``submit_via`` itself wrong (not merely a
line-number or characterization gap).** Full source-cited finding below (dossier at
``docs/plans/dossiers/bank_guarantee.md`` — correct on party-shape/status/grand_total/date_field-
fieldname/cascade-count/whitelist-count; **WRONG on submit_via**, settled below from source).
Confirmed from source (``bank_guarantee.json`` — 24 real fields enumerated via ``json.load`` out
of 30 ``field_order`` entries, the other 6 being Column/Section Break layout fields — +
``bank_guarantee.py`` (74 lines) + ``bank_guarantee.js`` (74 lines) + ``erpnext/hooks.py``,
version-16 checkout, all read 2026-07-21):

  * **``party_field=None`` — but NOT for the usual "no party concept at all" reason: this is a
    genuine DUAL CONDITIONAL pair, the Blanket Order shape.** ``customer`` (Link -> Customer,
    ``depends_on: eval: doc.reference_doctype == "Sales Order"``, ``bank_guarantee.json`` lines
    63-68) and ``supplier`` (Link -> Supplier, ``depends_on: eval: doc.reference_doctype ==
    "Purchase Order"``, lines 69-75) are both real, static, scalar header Link fields — never a
    Dynamic Link pair (Quotation's/Quality Inspection's own shape) and never a child table
    (Request for Quotation's own shape). Which one is meaningful for a given row is driven by
    ``bg_type`` (Select, ``""``/``"Receiving"``/``"Providing"``, ``reqd: 1``) via a CLIENT-SIDE
    handler only (``bank_guarantee.js:36-42``: ``bg_type`` Receiving -> sets
    ``reference_doctype`` to "Sales Order"; Providing -> "Purchase Order") — ``reference_doctype``
    is itself the schema's own ``depends_on`` selector for ``customer``/``supplier``, never
    ``bg_type`` directly, though the two are always correlated by this JS. ``validate()``
    (``bank_guarantee.py:46-47``) throws ``"Select the customer or supplier."`` unless at least
    one of the two is populated — but carries no upper bound (both could in principle be set at
    once; ERPNext never clears the other). Splicing both onto the list tier as context (never one
    alone, which would silently misreport half of all real rows) is the exact Blanket Order
    precedent.
  * **``status`` field: CONFIRMED ABSENT** (the full 24-field enumeration finds no field literally
    named ``status`` anywhere — ``docstatus`` is the only lifecycle signal, the Journal
    Entry/Stock Entry/Stock Reconciliation/Landed Cost Voucher/Blanket Order/BOM/Cost Center
    Allocation shape). **``grand_total`` field: CONFIRMED ABSENT** too — the same enumeration finds
    no ``Currency``/``Float``/``Percent`` field named ``grand_total``. ``amount`` (Currency,
    ``reqd: 1``, ``in_list_view: 1``, lines 87-92) is the ONLY ``in_list_view``-flagged field on
    the entire schema — the single real aggregate, standing in for the missing ``grand_total``
    slot (the Stock Reconciliation ``difference_amount``/Landed Cost Voucher
    ``total_taxes_and_charges`` precedent, never a completion/type-fork substitute).
  * **``date_field="start_date"`` — REJOINS Supplier Scorecard Period's own SEVENTH date-fieldname
    pattern rather than forcing a new one; a genuine dossier gap on its sibling ``end_date``
    settled from source.** ``start_date`` (Date, ``reqd: 1``, no default, lines 94-98) is a
    literal, real, required field of the EXACT SAME fieldname Supplier Scorecard Period's own
    landing already established (``supplier_scorecard_period.json`` also carries a real, required
    ``start_date`` — confirmed by a direct re-read) — the SECOND doctype to reach this pattern, not
    the fourteenth. ``end_date`` (Date, ``read_only: 1``, no ``reqd``, NOT ``in_list_view``-
    flagged, lines 106-109) is the schema's only other Date field; the dossier describes it as
    "calculated from ``start_date`` + ``validity`` in days" without naming WHERE — a full read of
    ``bank_guarantee.py`` (74 lines) finds no reference to ``end_date`` anywhere at all: the
    calculation is CLIENT-SIDE ONLY (``bank_guarantee.js:65-72``, the ``start_date``/``validity``
    form handlers calling ``frappe.datetime.add_days``/``frm.set_value``), never server-enforced.
    A document created or edited through the REST API (this broker's own only path) can carry a
    blank or stale ``end_date`` that ERPNext itself never recomputes — a real, dossier-omitted
    precision, though not one this broker's own list/plan/submit surface ever reads or governs
    (``end_date`` is spliced nowhere, disclosed nowhere), so it changes no behavior, only the
    documentation record.
  * **``company`` field: CONFIRMED ABSENT** — the full 24-field enumeration finds no fieldname
    literally ``company`` anywhere — **the SIXTH companyless doctype after Packing Slip/Supplier
    Scorecard Period/Shipment/Project Update/Asset Maintenance Log**, so ``REG_UNPINNED`` is the
    only registry shape this doctype can ever govern through (same posture, tests below).
  * **``submit_via`` — THE DOSSIER'S OWN ERROR, SETTLED FROM SOURCE.** The dossier's own §4 claims
    ``client_rpc``, citing "on_submit OVERRIDE present." **``on_submit`` is a HOOK method
    (``bank_guarantee.py:49``), never a ``def submit(self)``/``def cancel(self)`` override on the
    class** — a full 74-line read of ``bank_guarantee.py`` confirms ``class
    BankGuarantee(Document):`` defines only ``validate`` and ``on_submit`` (both ordinary Frappe
    lifecycle hooks, dispatched by ``run_method`` the same as every other doctype's own
    ``on_submit``); neither ``submit`` nor ``cancel`` is defined anywhere in the file, so neither
    is shadowed, and ``Document.submit``/``.cancel`` (both decorated ``@frappe.whitelist()``,
    ``frappe/model/document.py:1338-1348``) remain reachable via the ordinary
    ``run_method=submit``/``run_method=cancel`` URL-path vector. This is exactly the distinction
    Stock Reconciliation's own landing drew out in full (this module's own comment block above
    :data:`SUPPORTED_DOCTYPES`): ``SUBMIT_VIA_CLIENT_RPC`` is reserved for a class that genuinely
    shadows ``Document.submit``/``.cancel`` with an UNDECORATED override — Journal Entry and Stock
    Reconciliation are the only two doctypes this broker has ever found to do that. Bank Guarantee
    does not; **``submit_via=SUBMIT_VIA_RUN_METHOD`` is the source-verified correct value**, never
    ``client_rpc``.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: ``class BankGuarantee(Document):``
    (``bank_guarantee.py:10``) — a DIRECT ``Document`` subclass, the SIMPLEST MRO in the
    "uncallable" category** (tied with Job Card's/Work Order's/Blanket Order's/Shipment's/BOM's/
    Asset Maintenance Log's own bare-``Document`` shape). A full-file grep of
    ``bank_guarantee.py`` finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry``/
    ``StockLedgerEntry`` reference anywhere. ERPNext's own ``get_accounting_ledger_preview``
    calling ``doc.make_gl_entries()`` bare and unguarded raises ``AttributeError`` on a live bench
    for a real Bank Guarantee. **Fix: joins the ledger_preview skip tuple in ``tools.py``, now
    ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
    SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT, MAINTENANCE_SCHEDULE,
    ASSET_MAINTENANCE_LOG, BANK_GUARANTEE)``**, with its own honest
    ``_bank_guarantee_ledger_preview_unavailable_flag``.
  * **``on_submit`` (lines 49-55), read line by line — a fully deterministic doomed-submit gate,
    readable off the draft's own fields before a marker is ever minted (the Quality Inspection
    ``before_submit``/Asset Maintenance Log ``on_submit`` precedent), and the PLAINEST such gate
    this campaign has found — nothing else happens either way.** Three throws in strict source
    order, each stopping the method at the first failure (``frappe.throw`` raises immediately):
    ``bank_guarantee_number`` blank (lines 50-51, ``"Enter the Bank Guarantee Number before
    submitting."``), then ``name_of_beneficiary`` blank (52-53, ``"Enter the name of the
    Beneficiary before submitting."``), then ``bank`` blank (54-55, ``"Enter the name of the bank
    or lending institution before submitting."``). NONE of the three carries ``reqd: 1`` at the
    schema level (confirmed absent from all three field definitions) — a draft can be saved and
    read back with any or all of them blank; only ``on_submit`` itself enforces them, so a
    document missing two or three still surfaces only the FIRST throw on a real submit. **When all
    three are present, submit succeeds with NO further side effect of any kind** — a full 74-line
    read finds no document creation, no other document's write, no enqueue, nothing beyond the
    three checks themselves — the plainest "doomed-or-nothing" submit story this campaign has
    found, disclosed precisely (:func:`_bank_guarantee_submit_risk_flags`). ``validate()``'s own
    separate customer-or-supplier gate (lines 46-47) is NOT re-disclosed there: it runs on every
    save (frappe's own ``run_before_save_methods`` calls ``validate`` for both the ``"save"`` and
    ``"submit"`` actions, the same Asset Maintenance Log/Quality Inspection precedent), so an
    EXISTING draft this broker can already read back has necessarily satisfied it at its own last
    save — it cannot newly doom a submit this flag needs to preview.
  * **``on_cancel`` is CONFIRMED ABSENT ENTIRELY** — a full-file grep of ``bank_guarantee.py`` finds
    no ``on_cancel`` method of any kind. Cancelling this doctype is therefore a PURE ``docstatus``
    flip plus frappe's own generic ``check_no_back_links_exist()`` — and unlike Asset Maintenance
    Log (which has a real cross-document ``.save()`` cascade its own cancel never undoes), there is
    genuinely NOTHING for a Bank Guarantee's own cancel to fail to reverse: ``on_submit`` here
    performs no mutation beyond its own three throws. No ``ignore_linked_doctypes`` (confirmed
    absent), no throw of any kind on cancel.
  * **Cascade — CASCADE LEAF: the ONLY ``Link -> Bank Guarantee`` field in the entire v16 tree
    (BOTH the erpnext-16 AND frappe-16 checkouts grepped) is its own self-referencing
    ``amended_from``** (matching the dossier's own §8 count exactly). ``cascade.py`` needed no
    changes, and this landing builds no fabricated external blast-radius test.
  * **THE SIDE-SURFACE CAVEAT — ONE** ``@frappe.whitelist()`` **callable, confirmed by a full grep
    of ``bank_guarantee.py``, matching the dossier's own §9 count exactly:**
    ``get_voucher_details`` (module-level, lines 58-73) — a read-only ``frappe.db.get_value`` fetch
    against the reference Sales/Purchase Order (confirmed: no ``save``/``db_set``/
    ``frappe.db.set_value`` anywhere in its body). Nothing to withhold because nothing mutates.
  * Bank Guarantee is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list (18
    entries, ``hooks.py:326-345``) — consistent with a doctype that posts no GL of any kind.

**Breadth (Asset Movement) — Wave 5's third row, the thirty-fifth supported doctype, and the
SECOND Datetime-dated doctype in this campaign (after Work Order), with a wrinkle Work Order did
not carry: its own ``date_field`` fieldname literally collides with the SEVEN-member Date-typed
``transaction_date`` set (Sales/Purchase Order, Material Request, Supplier Quotation, Quotation,
Request for Quotation, Maintenance Schedule).** Full source-cited finding below (dossier at
``docs/plans/dossiers/asset_movement.md`` — correct on party-shape/status/grand_total/cascade-
count/whitelist-count/closed-books; ONE genuine dossier error, settled below from source: its own
§7 claims a cancelled-to-empty movement clears BOTH ``location`` and ``custodian`` to empty
strings — true only for ``custodian``; ``location`` carries an asymmetric truthy guard the dossier
never traced into ``update_asset_location_and_custodian``'s own body). Confirmed from source
(``asset_movement.json`` — 7 real fields enumerated via ``json.load`` out of 11
``field_order`` entries, the other 4 being Column/Section Break layout fields
(``column_break_4``, ``reference``, ``section_break_10``, ``column_break_9``) — +
``asset_movement.py`` (182 lines) + ``asset_movement_item.json`` (the child table) +
``asset.py`` (the birth-movement/cancel-cascade partner) + ``erpnext/hooks.py``, version-16
checkout at 16.28.0, all read 2026-07-21):

  * **``party_field=None`` — a real Dynamic Link pair, but never a GL party.**
    ``reference_doctype``/``reference_name`` (Link -> DocType / Dynamic Link, lines 58-71) point
    at the Purchase Receipt or Purchase Invoice that seeded an auto-created movement — a
    transactional PROVENANCE pointer, never populated for most user-authored movements and never
    read as a party anywhere in ``asset_movement.py``. The child table's own
    ``from_employee``/``to_employee`` (Link -> Employee, ``asset_movement_item.json`` lines
    47-61) are custodian-trail fields on EACH row, not a singular header party either — the same
    "no header party concept" shape RFQ's own child-table party established, but Asset Movement's
    own party-shaped fields are OUTGOING references (this document points AT other things), never
    the INCOMING GL-party shape SI/PI/PE carry.
  * **``status``: CONFIRMED ABSENT** — the complete 7-real-field enumeration finds no field
    literally named ``status`` anywhere; ``docstatus`` is the only lifecycle signal (the Journal
    Entry/Stock
    Entry/Stock Reconciliation/Landed Cost Voucher/Blanket Order/BOM/Cost Center Allocation/Bank
    Guarantee shape). ``purpose`` (Select, ``reqd: 1``, ``in_list_view: 1``, lines 32-39, options
    ``"\nIssue\nReceipt\nTransfer\nTransfer and Issue"``) is the closest adjacent field, but it is
    a PEER-VALUE router (the Stock Entry precedent: purpose selects WHICH movement semantics
    apply, never a state transition — an amended Receipt movement stays "Receipt", it does not
    progress through the four values). **``grand_total``: CONFIRMED ABSENT** too — the complete
    enumeration finds no ``Currency``/``Float``/``Percent`` field anywhere; Asset Movement carries
    no aggregate of any kind, not even a stand-in substitute (unlike Stock Reconciliation's
    ``difference_amount`` or Bank Guarantee's ``amount``) — a pure state-capture trail, no value on
    the document at all. The THREE ``in_list_view``-flagged fields, confirmed via ``json.load``:
    ``company`` (line 25), ``purpose`` (line 35), ``transaction_date`` (line 44) — see the list-
    tier finding below.
  * **``date_field="transaction_date"`` (Datetime, ``reqd: 1``, ``default: "Now"``, lines 40-47) —
    REJOINS the transaction_date FIELDNAME (now used by eight doctypes total) but is only the
    SECOND DATETIME-TYPED ``date_field`` this entire campaign has found, after Work Order's
    ``planned_start_date``.** ``allow_on_submit`` is confirmed ABSENT (no such key on the field at
    all, unlike Work Order's own ``allow_on_submit: 1``) — the date cannot move post-submit without
    a fresh docstatus cycle. The fieldname collision is purely NOMINAL: seven other supported
    doctypes also use the literal string ``"transaction_date"`` as their own ``date_field``, but
    every one of those seven is a plain **Date** field (no time component, a 10-character ISO
    read); Asset Movement's own copy of the identically-named field is genuinely **Datetime** (a
    ``"YYYY-MM-DD HH:MM:SS[.ffffff]"`` REST read), so it needs the SAME datetime-to-date projection
    Work Order's own landing built into ``tools.py``'s ``_posting_date_of`` — reused unchanged, not
    reinvented (the projection keys on the VALUE's own shape at read time, never on which fieldname
    string produced it, so a literal fieldname collision between a Date-typed group and a
    Datetime-typed member is safe by construction; each doctype's OWN type must still be pinned
    from its own source, never inherited by name-match — the same discipline
    ``test_work_order_is_the_only_planned_start_date_user`` established, now doubled because this
    is the first fieldname the campaign has ever reused across a type boundary).
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 182 lines of
    ``asset_movement.py``: ``class AssetMovement(Document)`` (line 13) defines 13 methods total
    (``validate``, five ``validate_*`` helpers, ``on_submit``, ``on_cancel``,
    ``set_latest_location_and_custodian_in_asset``, ``get_latest_location_and_custodian``,
    ``update_asset_location_and_custodian``, ``log_asset_activity``) and overrides NEITHER
    ``submit()`` nor ``cancel()`` anywhere** — only the ``on_submit``/``on_cancel`` HOOK methods
    (lines 116/119) are dispatched by the base ``Document.submit()``/``.cancel()``, the same
    run_method transport every non-JE/SR doctype in this table rides.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: ``class AssetMovement(Document):``
    (line 13) — a DIRECT ``Document`` subclass, never ``AccountsController``, never
    ``StockController``.** A full-file grep of ``asset_movement.py`` for
    ``make_gl_entries``/``make_sl_entries``/``GLEntry``/``StockLedgerEntry`` returns nothing.
    ERPNext's own ``get_accounting_ledger_preview`` calling ``doc.make_gl_entries()`` bare and
    unguarded raises ``AttributeError`` on a live bench for a real Asset Movement. **Fix: joins the
    ledger_preview skip tuple in ``tools.py``, now its NINETEENTH member —
    ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
    SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT, MAINTENANCE_SCHEDULE,
    ASSET_MAINTENANCE_LOG, BANK_GUARANTEE, ASSET_MOVEMENT)``**, with its own honest
    ``_asset_movement_ledger_preview_unavailable_flag``. Consistent with the doctype's own summary
    (section 5 of the dossier): a pure state-capture document, no GL of any kind, ever.
  * **THE CENTRAL FINDING — ``on_submit`` AND ``on_cancel`` call the EXACT SAME METHOD** (lines
    116-120: both bodies are the single line ``self.set_latest_location_and_custodian_in_asset()``)
    **— the FIRST doctype this campaign has found whose submit and cancel hooks are textually
    IDENTICAL.** That method (lines 122-126) loops every row in ``self.assets`` and, per row,
    calls ``get_latest_location_and_custodian(asset)`` (lines 128-154) — a live SQL join over
    ``tabAsset Movement Item``/``tabAsset Movement`` for ALL **submitted** movements referencing
    that asset, ``ORDER BY transaction_date DESC LIMIT 1`` — recomputed FRESH from the bench every
    time, never read off this document's own fields (so submit and cancel differ only in WHICH row
    the freshest submitted trail resolves to, never in the write mechanism itself: cancelling
    removes this document from ``docstatus = 1`` contention, so the same query naturally resolves
    to the PRIOR movement, or to ``("", "")`` if none remains). The result is then written onto the
    Asset by ``update_asset_location_and_custodian`` (lines 156-162) via **raw
    ``frappe.db.set_value``** — confirmed from ``frappe.db.set_value``'s own docstring
    (``frappe/database/database.py:934-953``): *"do not call the ORM triggers... this function will
    not call Document events and should be avoided in normal cases"* — the SAME "even more direct
    bypass than ``db_update()``: no document load at all" grade Maintenance Visit's own
    ``update_status_and_actual_date`` already established for this campaign (no ``validate()``, no
    hooks, no version history on the Asset). **A nuance this campaign has not yet seen: a document
    IS loaded first** (``asset = frappe.get_doc("Asset", asset_id)``, line 157) **— but ONLY to
    read the current ``custodian``/``location`` for the conditional comparison; the write itself
    never touches that loaded object**, going straight to the module-level ``frappe.db.set_value``
    regardless. Disclosed as a genuine, unconditional, data-driven risk flag
    (:func:`_asset_movement_write_risk_flags` in ``tools.py``, fired on BOTH submit and cancel,
    naming every referenced Asset from the draft's own ``assets`` rows).
  * **THE SHARPEST DISCLOSURE — an ASYMMETRIC field-level guard the dossier's own §7 missed**
    (lines 159-162): ``custodian`` is written whenever ``cstr(employee) != asset.custodian`` — an
    EMPTY resolved employee still overwrites a non-empty custodian with ``""``, a real clear — but
    ``location`` is written ONLY ``if location and location != asset.location``: a FALSY (empty)
    resolved location is NEVER written. On a cancel that rolls an asset back to "no prior movement"
    (``get_latest_location_and_custodian`` returning ``("", "")``), the Asset's ``custodian``
    clears to ``""`` but its ``location`` is left completely untouched, silently retaining whatever
    the last movement set it to. The dossier's own §7 states both fields "become empty strings" —
    true for ``custodian`` alone; disclosed in prose on the cancel direction (no sibling read this
    plan can perform would let it predict which case applies to a REAL cancel, only that the
    asymmetry exists).
  * **Cascade — a LEAF for INCOMING links (the ONLY ``Link -> Asset Movement`` field in the entire
    v16 tree, BOTH the erpnext-16 AND frappe-16 checkouts grepped, is its own self-referencing
    ``amended_from``, matching the dossier's own §8 count exactly) — but NOT absent from every
    cascade graph.** Asset Movement Item's own ``asset`` field (Link -> Asset, ``reqd: 1``,
    ``asset_movement_item.json`` line 19-24) is one of Asset's own 18 ``Link -> Asset`` edges (this
    broker's own Asset landing already enumerated and named it — "Asset Movement Item" — among the
    asset-family spine); since Asset Movement is itself submittable, a submitted Asset Movement
    DOES appear as a dependent NODE inside an ASSET's own ``plan_cascade_cancel`` graph (the
    structural mechanism Asset's own landing named: every submitted Asset carries a submitted
    birth movement from ``make_asset_movement()``, which is why a leaf ``plan_cancel`` refuses for
    every submitted Asset). Two different directions of the same relationship, not a contradiction:
    nothing links INTO an Asset Movement (leaf, for its own cancel), while an Asset Movement itself
    links OUT to the Asset it references (a dependent, inside THAT asset's own cascade). The
    per-node cascade disclosure in ``tools.py``'s ``_tool_plan_cascade_cancel`` fires the same write
    -mechanism flag for any ``ASSET_MOVEMENT`` node in a graph, docname-qualified.
  * **The auto-created, auto-submitted "birth" movement (already disclosed from Asset's OWN side
    at its landing) — restated here from Asset Movement's own side, context only, no new runtime
    branch needed.** When an Asset is submitted, ``Asset.make_asset_movement()``
    (``asset.py:571-600``) builds a movement with ``purpose="Receipt"``, ``reference_doctype``/
    ``reference_name`` pointing at the seeding Purchase Receipt/Purchase Invoice (or blank if
    neither exists), inserts it, and immediately calls ``asset_movement.submit()`` — so this
    document ARRIVES at ``docstatus=1`` before this broker's own ``plan_submit`` could ever see it
    as a draft; the standing "not a draft" refusal already covers the case, no dedicated flag
    needed. Symmetrically, ``Asset.cancel_movement_entries()`` (``asset.py:738-749``) finds every
    SUBMITTED movement referencing an asset and calls the ordinary ``.cancel()`` lifecycle on each
    (never a bypass — a real submit/cancel round trip that runs this same document's own
    ``on_cancel`` hook), which is the OTHER direction the central finding above already covers.
    Independently-authored movements (created by a human, not by ``Asset.on_submit``) can carry any
    of the four ``purpose`` values and may have no ``reference_doctype``/``reference_name`` at all
    — ``purpose == "Receipt"`` alone is never sufficient to distinguish a birth movement from a
    genuine user-authored receipt, so no flag speculates about origin from that field alone.
  * **THE SIDE-SURFACE CAVEAT — ZERO** ``@frappe.whitelist()`` **callables, confirmed by a full grep
    of ``asset_movement.py``** — matching the dossier's own §9 count exactly. Nothing to withhold
    because nothing exists.
  * Asset Movement is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list (18
    entries, ``hooks.py:326-345`` — Asset itself IS on the list, Asset Movement is not) —
    consistent with a doctype that posts no GL of any kind; the broker's own closed-books check
    against ``transaction_date`` is therefore equal-or-STRICTER than ERPNext here, the same
    accepted posture every dated non-GL doctype in this table already rides.

**Breadth (Delivery Trip) — the thirty-sixth supported doctype, and the THIRD Datetime-dated
doctype in this campaign (after Work Order and Asset Movement) — with a shape neither predecessor
carries: it is genuinely NOT a cascade leaf (a real submittable Link points IN), and its own
``on_cancel`` mutates the EXACT SAME dependent documents this broker's own cascade order cancels
immediately before it.** Full source-cited finding below (dossier at
``docs/plans/dossiers/delivery_trip.md`` — correct on party/status-options/whitelist-count/
cascade-count/closed-books; genuine gaps closed below: the "23 fields" framing conflates the raw
``fields`` array length with the real, non-layout field count; §7 covers ``on_cancel`` only,
missing two sibling lifecycle hooks that fire the identical mutation; and the honesty-grade
placement of that mutation, plus the structural cascade-order collision it creates, are pinned
precisely from ``frappe/model/document.py`` below — neither is in the dossier at all). Confirmed
from source (``delivery_trip.json`` — 15 real fields enumerated via ``json.load`` out of 23
``fields``-array entries, the other 8 being 6 Column/Section Break layout fields plus 2 Button
fields — + ``delivery_trip.py`` (479 lines) + ``delivery_stop.json`` (the child table) +
``delivery_note.json``/``delivery_note.py`` (the cascade partner) + ``delivery_trip_list.js`` (the
list-view UI) + ``frappe/model/document.py``/``frappe/model/base_document.py`` (the ``.save()``
lifecycle a cross-document mutation rides) + ``erpnext/hooks.py``, version-16 checkout at 16.28.0,
all read 2026-07-21):

  * **``party_field=None`` — confirmed, but ``company`` (Link -> Company, ``reqd: 1``,
    ``delivery_trip.json`` line ~32) is a REAL, required field — Delivery Trip is NOT one of this
    campaign's companyless rows.** ``driver`` (Link -> Driver, optional) and ``employee`` (Link ->
    Employee, ``read_only``, ``fetch_from: "driver.employee"``) are metadata only, never read as a
    GL party anywhere in ``delivery_trip.py``; GL posting happens only through the linked Delivery
    Note's own party, never this document's.
  * **``status``: CONFIRMED REAL** — a genuine Select field (``read_only: 1``, ``in_standard_
    filter: 1``, ``no_copy: 1``, options ``"Draft\\nScheduled\\nIn Transit\\nCompleted\\n
    Cancelled"`` — 5 values, matching the dossier's own §2 exactly). Maintained ENTIRELY by this
    doctype's own ``update_status()`` (``delivery_trip.py:100-110``) via ``self.db_set("status",
    status)`` — an own-document ``db_set``, never validate/hooks-driven — called from
    ``on_submit``/``on_update_after_submit``/``on_cancel``/``on_discard`` (four call sites, lines
    72/75/78/54). This is a LIGHTER cousin of the cross-document bypass grades this campaign has
    already named (Maintenance Visit's ``db_update``, Asset Movement's ``frappe.db.set_value``):
    the write targets THIS document alone, is fully internal bookkeeping (no external field or
    document is touched), and is never disclosed as a runtime risk flag for that reason — the same
    "self-status maintenance is not a governance-relevant mutation" posture every other real-status
    doctype in this table already carries. **``grand_total``: CONFIRMED ABSENT**, with no aggregate
    substitute of any kind (``total_distance``, a Float, is a distance measurement, never a
    monetary or count aggregate) — the same "no value on the document at all" shape Asset Movement
    established. The two ``in_list_view``-flagged fields, confirmed via ``json.load``:
    ``driver_name`` and ``departure_time`` (matching the dossier's own §2 exactly) — but
    ``delivery_trip_list.js`` (ERPNext's own list-view controller, NOT read by the dossier at all)
    additionally declares ``add_fields: ["status"]`` and a full ``get_indicator`` color mapping
    keyed on this same ``status`` value — confirming ``status`` is a genuine, ERPNext-authored
    list-relevant column even though the schema itself never flags it ``in_list_view`` (see the
    list-tier finding below).
  * **``date_field="departure_time"`` (Datetime, ``reqd: 1``, ``in_list_view: 1``) — CONFIRMED,
    and a genuinely NEW fourteenth date-fieldname pattern (sole member), never colliding with any
    of the thirteen prior patterns' literal fieldnames.** Unlike Asset Movement's own
    ``transaction_date`` (which collided nominally with seven Date-typed doctypes already on that
    literal fieldname), ``"departure_time"`` is a fieldname this campaign has never seen before —
    no exclusivity-set widening needed, only a new single-member pin. ``allow_on_submit`` is
    CONFIRMED ABSENT (no such key on the field at all) and, unlike Asset Movement's own
    ``transaction_date``, this field carries **no schema ``default`` of any kind** — blank on a
    fresh draft until a human sets it, the same "reqd with no default" shape Installation Note's
    own ``inst_date`` established, now combined for the first time with a Datetime fieldtype. The
    projection machinery needs zero changes: :func:`pacioli.tools._posting_date_of` keys on the
    VALUE's own shape at read time (a 10-char ISO date followed by a ``" "``/``"T"`` separator),
    never on which fieldname produced it — proven behaviorally in ``test_tools.py``'s own
    ``TestDeliveryTripDatetimeDateProjection``, mirroring ``TestWorkOrderDatetimeDateProjection``/
    ``TestAssetMovementDatetimeDateProjection`` exactly.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 479 lines of
    ``delivery_trip.py``: ``class DeliveryTrip(Document)`` (line 14) defines 16 methods total
    (``__init__``, ``on_discard``, ``validate``, ``on_update``, ``on_trash``, ``on_submit``,
    ``on_update_after_submit``, ``on_cancel``, ``validate_stop_addresses``,
    ``validate_delivery_note_not_draft``, ``update_status``, ``update_delivery_notes``,
    ``process_route``, ``form_route_list``, ``rearrange_stops``, ``get_directions``) and overrides
    NEITHER ``submit()`` NOR ``cancel()`` anywhere** — only the ``on_submit``/``on_cancel`` HOOK
    methods (lines 71-72/77-79) are dispatched by the base ``Document.submit()``/``.cancel()``, the
    same run_method transport every non-JE/SR doctype in this table rides.
  * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: ``class DeliveryTrip(Document):`` (line
    14) — a DIRECT ``Document`` subclass, never ``AccountsController``, never
    ``StockController``.** A full-file grep of ``delivery_trip.py`` for ``make_gl_entries``/
    ``make_sl_entries``/``GLEntry``/``StockLedgerEntry`` returns nothing. ERPNext's own
    ``get_accounting_ledger_preview`` calling ``doc.make_gl_entries()`` bare and unguarded raises
    ``AttributeError`` on a live bench for a real Delivery Trip. **Fix: joins the ledger_preview
    skip tuple in ``tools.py``, now its TWENTIETH member —
    ``(DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION, INSTALLATION_NOTE,
    SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT, MAINTENANCE_SCHEDULE,
    ASSET_MAINTENANCE_LOG, BANK_GUARANTEE, ASSET_MOVEMENT, DELIVERY_TRIP)``**, with its own honest
    ``_delivery_trip_ledger_preview_unavailable_flag``. Delivery Trip posts no GL of any kind under
    its own name — it only mutates fields on OTHER, already-existing Delivery Notes.
  * **``validate()`` (lines 57-63) — TWO throws, BOTH gated on ``self._action == "submit"``, read
    line by line:** ``if self._action == "submit" and not self.driver: frappe.throw(_("A driver
    must be set to submit."))`` (lines 58-59) — deterministic off the draft's OWN ``driver`` field,
    a doomed-submit gate disclosed data-driven (:func:`_delivery_trip_submit_risk_flags`, the
    Maintenance Schedule "submit will be REFUSED" shape). Then, unconditionally when
    ``_action == "submit"``, ``self.validate_delivery_note_not_draft()`` (lines 61-62, body at
    86-98) throws if ANY Delivery Note named by a ``delivery_stops`` row is still ``docstatus: 0`` —
    this needs a LIVE read of each named Delivery Note's own docstatus this plan cannot perform
    (the Asset Movement "monotonic validate needs other movements' state" shape); the linked
    Delivery Note names ARE known from the draft's own ``delivery_stops`` rows and are surfaced as
    an unconditional data-driven prose addition, the outcome itself staying prose. Finally,
    ``self.validate_stop_addresses()`` (line 63, body 81-84) runs on EVERY save regardless of
    action — a non-throwing address-display FILL, not a gate, never disclosed as a risk.
  * **THE CENTRAL FINDING — the cross-document mutation, its exact honesty grade pinned from
    ``frappe/model/document.py`` line by line (a NEW grade this campaign has not yet needed).**
    ``on_cancel`` (lines 77-79) calls ``self.update_status()`` then
    ``self.update_delivery_notes(delete=True)`` (body 112-150); **the dossier's own §7 covers
    ``on_cancel`` only — TWO sibling lifecycle hooks fire the IDENTICAL call, both missed
    entirely: ``on_trash`` (lines 68-69) and ``on_discard`` (lines 53-55).** Neither is reachable
    through this broker's own governed verb surface (no delete/discard tool is exposed), so no
    runtime branch is needed for them — named here for completeness only, the same "no dedicated
    flag needed" posture Asset Movement's own birth-movement finding established. For every
    distinct Delivery Note named by a ``delivery_stops`` row (lines 122, ``list(set(stop.
    delivery_note for stop in self.delivery_stops if stop.delivery_note))``):
    ``note_doc = frappe.get_doc("Delivery Note", delivery_note)``; five fields
    (``driver``/``driver_name``/``vehicle_no``/``delivery_trip``/``lr_no``/``lr_date``) are set to
    ``None``; if any actually changed, ``note_doc.flags.ignore_validate_update_after_submit =
    True`` then ``note_doc.save()`` (line 146-147). **Read against ``frappe/model/document.py``'s
    own ``_save`` (552-611) and ``check_docstatus_transition`` (1112-1150): calling ``.save()`` on
    an ALREADY-SUBMITTED Delivery Note resolves ``self._action = "update_after_submit"``
    (``check_docstatus_transition``, line 1137) and REQUIRES submit permission (line 1138) — this
    is NOT a bypass grade at all, but the SANCTIONED, ERPNext-shipped mechanism for updating a
    submitted document's non-protected fields: permission checks, ``check_if_latest`` (freshness/
    timestamp-conflict protection), ``_validate_links``, the ``before_update_after_submit``/
    ``on_update_after_submit``/``on_change`` hooks, and ``save_version()`` (version history) ALL
    still run (``_save``, lines 574-605, 1445-1466). The doctype's own custom ``validate()`` hook
    is the ONE thing that never fires for this action — not because of anything Delivery Trip's own
    code does, but because ``run_before_save_methods`` (1383-1413) only calls
    ``self.run_method("validate")`` for ``_action in ("save", "submit")``, never
    ``"update_after_submit"`` — a property of EVERY submitted-document field update in frappe, not
    a discretionary skip here. The ONE thing the ``ignore_validate_update_after_submit`` flag
    itself removes is ``validate_update_after_submit``'s own per-field check (``document.py:1164-
    1176`` calling ``base_document.py``'s ``_validate_update_after_submit``, lines 1270-1306): any
    field lacking ``allow_on_submit`` that differs from the DB value normally throws "Not allowed
    to change {field} after submission" — this is the SPECIFIC, narrow protection the flag exists
    to waive, and the ONLY thing it waives. **Placement in this campaign's honesty-grade ladder:
    ABOVE every bypass grade found so far (Maintenance Visit's ``db_update`` — hooks/version/
    permission all skipped; Asset Movement's/Asset Maintenance Log's ``db_set``/``frappe.db.
    set_value`` — no ORM triggers, no Document events at all; Quality Inspection's raw SQL into
    another table) — this is the correct, sanctioned API for the job, distinct from every bypass
    family this table has named, disclosed here for honesty rather than as a warning.**
  * **THE SHARPEST FINDING, CORRECTED 2026-07-21 by supervisor verification — beyond the dossier
    entirely, and beyond this landing's own first pass: the collision is real but does NOT land
    where first claimed.** Two facts from ``frappe/model/document.py``/``frappe/model/
    delete_doc.py``, read line by line, invert the original claim:

    1. **``run_post_save_methods`` (``document.py:1437-1452``) runs ``on_cancel`` BEFORE
       ``check_no_back_links_exist`` for the cancel action** (lines 1450-1452:
       ``self.run_method("on_cancel")`` then ``self.check_no_back_links_exist()``).
       Consequence: a LEAF cancel of THIS Delivery Trip, even with submitted linked Delivery
       Notes, NATIVELY SUCCEEDS — ``update_delivery_notes(delete=True)`` clears the notes'
       ``delivery_trip``/``driver``/``vehicle_no``/``lr_no``/``lr_date`` fields via the
       sanctioned update-after-submit ``note_doc.save()`` (the honesty-grade bullet above)
       BEFORE ``check_no_back_links_exist`` -> ``check_if_doc_is_linked(self, method="Cancel")``
       ever runs, so by the time that link check executes it finds ZERO Delivery Notes still
       pointing at this Delivery Trip. The hook is a designed SELF-UNLINKING cancel, not a
       doomed one.
    2. **The cascade's own note-first step is what actually fails — at frappe's OWN gate, not
       this doctype's.** ``check_if_doc_is_linked`` (``delete_doc.py:376-388``, walking
       ``get_linked_docs`` at ``delete_doc.py:301-356``) enumerates EVERY static Link field
       across the whole schema pointing at the document being cancelled, INCLUDING child-table
       Link fields (the ``meta.istable`` branch, ``delete_doc.py:339-340``, adds ``parent``/
       ``parenttype`` and reports the CHILD ROW'S OWN ``docstatus`` — which mirrors its
       parent's). ``Delivery Stop.delivery_note`` (``delivery_stop.json``: ``fieldtype:
       "Link"``, ``options: "Delivery Note"``, table ``istable: 1`` — confirmed by
       ``json.load``) is exactly such a field: a submitted Delivery Trip's own child rows carry
       ``docstatus: 1``, so cancelling the Delivery Note THEY POINT AT while the Delivery Trip
       is still submitted is a hit. Delivery Note's own ``on_cancel`` sets
       ``ignore_linked_doctypes = ("GL Entry", "Stock Ledger Entry", "Repost Item Valuation",
       "Serial and Batch Bundle")`` (``delivery_note.py:500-505``) — **Delivery Trip is NOT in
       that tuple** — so cancelling the Delivery Note first (this broker's own dependents-first
       cascade order, ``pacioli.cascade.build_cascade``'s own module docstring: "dependent
       before the document it depends on, target last") raises ``frappe.LinkExistsError``
       immediately, at the NOTE step, before this Delivery Trip is ever reached.

    **Net, honest picture (at landing time, 2026-07-21, before the ruling below):** native leaf
    cancel of a submitted Delivery Trip = ALLOWED, self-unlinking, with a real cross-document
    side effect on submitted Delivery Notes (the honesty-grade bullet above). This broker's OWN
    leaf ``plan_cancel``, at landing, = REFUSED outright by the standing blast-radius check (a
    submitted dependent exists) — stricter than ERPNext in consent. This broker's OWN cascade
    cancel (the only path a refused leaf can take) = FAILS AT EXECUTE at the Delivery Note step
    with frappe's own ``LinkExistsError``, before this Delivery Trip's own cancel step ever runs.
    **At landing this broker had NO WORKING CANCEL PATH for a submitted Delivery Trip with a
    submitted linked Delivery Note** — not a bug silently patched or reordered at landing (per
    this campaign's own "no invented bypass" discipline, the companyless precedent above). Two
    structural options were named at landing, neither chosen: **(a)** a self-unlinking-doctype
    exception letting the leaf ``plan_cancel`` proceed despite the submitted dependent,
    disclosing the cross-document side effect instead of refusing outright; or **(b)** a
    target-first cascade order exception for this specific mutual-edge doctype pair (Delivery
    Trip <-> Delivery Note), reversing ``build_cascade``'s own "dependent first" rule for this
    one relationship.

    **RULED 2026-07-21 — John's ruling 1** (``docs/plans/2026-07-21-cancel-truth-rulings.md``):
    option **(a)**, belted. Delivery Trip is registered in :data:`SELF_UNLINKING_DOCTYPES`; the
    leaf ``plan_cancel`` blast-radius gate now treats a registered doctype's own submitted
    incoming links as non-blocking instead of refusing outright — the belt (full disclosure of
    the note-side writes, already built) stays, and a new suspenders readback re-reads every
    linked Delivery Note after a successful execute and attests the self-unlink actually
    happened, reporting any note still linked rather than silently passing
    (``self_unlink_readback`` in the outcome payload — see
    :func:`pacioli.tools._delivery_trip_self_unlink_readback`'s own docstring). Option (b) was
    NOT taken: ``plan_cascade_cancel`` is untouched, still structurally dead at the Delivery Note
    step below, disclosed exactly as before.

    **The originally-claimed mechanism survives as a SECOND, weaker lock, pinned separately: IF
    the note step were somehow already past** (hypothetically — it structurally cannot be, per
    point 2 above) **and this Delivery Trip's own ``on_cancel`` then ran
    ``update_delivery_notes(delete=True)`` against an ALREADY-CANCELLED Delivery Note,
    ``note_doc.save()`` would resolve ``previous.docstatus == 2`` in
    ``check_docstatus_transition`` (``document.py:1149-1150``): an unconditional ``raise
    frappe.ValidationError(_("Cannot edit cancelled document"))`` inside ``check_if_latest``,
    before ``validate_update_after_submit`` ever runs.** A double lock, both directions
    receipted, disclosed together. Disclosed in :func:`_delivery_trip_cancel_risk_flags`, wired
    into both the leaf ``plan_cancel`` path (reachable only in the narrower case of an
    independently pre-cancelled note) and every ``DELIVERY_TRIP`` node in a
    ``plan_cascade_cancel`` graph (the common case, where the note-step failure now applies) —
    never repaired, never reordered; the broker's own cascade order is correct
    (blast-radius-first) and this is a genuine ERPNext-side collision, named plainly.
  * **Cascade — GENUINELY NOT A LEAF, confirmed by a full-tree grep of ``"options": "Delivery
    Trip"`` over BOTH the erpnext-16 and frappe-16 checkouts: exactly two hits** — Delivery Trip's
    own self-referencing ``amended_from`` (``delivery_trip.json`` line ~162), and Delivery Note's
    own REAL, STATIC ``delivery_trip`` field (``delivery_note.json`` line 1366, ``Link ->
    "Delivery Trip"``, on a doctype confirmed ``is_submittable: 1``, ``delivery_note.json:1462``).
    ERPNext's own ``on_cancel`` (this doctype's own body, lines 77-79) enforces NO gate whatsoever
    against a linked Delivery Note's docstatus — **this broker's own blast-radius check
    (``get_submitted_linked_docs``) is genuinely STRICTER than ERPNext here, in POSTURE**,
    refusing a leaf cancel where native ERPNext would silently SUCCEED as a self-unlinking cancel
    (per the corrected finding above) — never a "collision" natively at all, since ERPNext's own
    order runs this doctype's ``on_cancel`` before its own back-link check. The Maintenance
    Schedule precedent, a real external-partner Link, zero ``cascade.py`` changes needed (the
    same generic walk every prior edge rides).
  * **THE SIDE-SURFACE CAVEAT — FIVE** ``@frappe.whitelist()`` **callables, confirmed by a full
    grep of ``delivery_trip.py`` — matching the dossier's own §9 count exactly:** one instance
    method, ``process_route`` (152-204, NO docstatus check, calls ``self.save()`` at line 204 —
    Google Maps-dependent, throws if ``Google Settings.api_key`` is unset, line 282-283) and four
    module-level functions — ``get_contact_and_address`` (307, read-only), ``get_contact_display``
    (369, read-only), ``notify_customers`` (407-452, mutates: ``stop.db_set("email_sent_to", ...)``
    line 445 and ``delivery_trip.db_set("email_notification_sent", 1)`` line 450 — sanctioned by
    ``email_notification_sent``'s own ``allow_on_submit: 1``, no docstatus check either), and
    ``get_driver_email`` (473, read-only). The broker grants NONE of them — but TWO carry a genuine
    submitted-state mutation surface, named in the docstring per standing discipline.
  * Delivery Trip is NOT named in ``erpnext/hooks.py``'s ``period_closing_doctypes`` list (18
    entries, ``hooks.py:326-345`` — Delivery Note IS on the list, Delivery Trip is not) —
    consistent with a doctype that posts no GL of any kind; the broker's own closed-books check
    against ``departure_time`` is therefore equal-or-STRICTER than ERPNext here, the same accepted
    posture every dated non-GL doctype in this table already rides.

**Breadth (Asset Value Adjustment) — Wave 5's fifth row, the campaign's FIRST SIBLING-DOCUMENT
FACTORY: on_submit synchronously builds and SUBMITS a peer Journal Entry (the revaluation entry),
on_cancel synchronously cancels it — a fundamentally different scope-boundary shape than Asset's
own SCHEDULED, asynchronous depreciation JEs.** Full source-cited finding below (dossier at
``docs/plans/dossiers/asset_value_adjustment.md`` — correct on party/status/grand_total absence,
submit_via, the UNCALLABLE preview verdict, the zero cascade edges, and the "no frappe.throw in
cancel" observation; genuine gaps closed below: ``company`` is NOT required — the first "company
present" doctype in this campaign where that's true; the dossier's own §10 code excerpt shows
``je.submit()`` but SILENTLY DROPS the ``je.flags.ignore_permissions = True`` line immediately
above it (line 126) — the dossier names the bypass only on the cancel side (§7/§10), never on the
submit side where it is equally present; and ``update_asset()`` — read line by line below — turns
out to arm a THIRD sibling-document channel the dossier's one-line "reschedules depreciation"
never unpacks: ``reschedule_depreciation`` can CANCEL an existing submitted Asset Depreciation
Schedule and CREATE+SUBMIT a new one, synchronously, in the same request). Confirmed from source
(``asset_value_adjustment.json`` — 12 real fields out of 17 raw ``fields``-array entries via
``json.load``, the other 5 being Column/Section Break layout — + ``asset_value_adjustment.py``
(232 lines) + ``asset.py``/``depreciation.py``/``asset_depreciation_schedule.py`` (the sibling
mechanisms) + ``erpnext/hooks.py`` + ``erpnext/accounts/doctype/accounting_period/
accounting_period.py`` (the closed-books scope question) + frappe's ``document.py`` (the
``ignore_permissions``/``.cancel()``/``.submit()`` lifecycle), version-16 checkout at 16.28.0, all
read 2026-07-21):

  * **``party_field=None`` — confirmed, and ``company`` (Link -> Company,
    ``asset_value_adjustment.json`` line 28) is present WITHOUT ``reqd``.** Re-checking EVERY
    prior "company present" doctype landed so far (a full pass, not the two spot-checks any
    earlier landing happened to need) finds this is the THIRD such row, not the first: Purchase
    Invoice (one of the four founding doctypes, pre-dating this campaign's own source-citation
    discipline) and Quality Inspection (26th, its own landing noted "company DOES exist … though
    not reqd" in passing, never called out as a shape) both already carry an optional ``company``
    — every OTHER "company present" doctype (Sales Order/Payment Entry/Journal Entry/Delivery
    Note/Material Request/Stock Entry/Quotation/Request for Quotation/Blanket Order/Job Card/BOM/
    Work Order/Asset/Asset Movement/Delivery Trip) carries ``reqd: 1``. **What IS genuinely new
    here is the CONSEQUENCE, not the shape: a blank ``company`` on this doctype is a DOOMED
    SUBMIT, deterministic off the draft** — ``make_asset_revaluation_entry`` sets ``je.company =
    self.company`` (line 102) unconditionally, and Journal Entry's own ``company`` field IS
    ``reqd: 1`` (``journal_entry.json``, confirmed) — so a blank-company AVA reaches
    ``je.submit()`` and fails there with frappe's own ``MandatoryError``, even though Asset Value
    Adjustment's own schema never required it. Disclosed data-driven
    (:func:`pacioli.tools._asset_value_adjustment_submit_risk_flags`) rather than silently
    inherited as "company is optional, nothing to say" — the field's REALITY decided this, not its
    ``reqd`` flag, the same discipline Maintenance Schedule's own optional ``customer`` splice
    already established for a party field.
  * **``status``/``grand_total``: CONFIRMED ABSENT, ``docstatus`` the only lifecycle signal** — no
    substitute total field either; the three real ``in_list_view`` columns
    (``finance_book``/``current_asset_value``/``new_asset_value``, all confirmed via
    ``json.load``) are the closest thing to a summary this schema carries, none of them a single
    aggregate. Notably, ``asset`` itself — the field naming WHICH asset this adjustment concerns —
    carries no ``in_list_view`` flag at all (confirmed absent), an honestly counter-intuitive
    omission this landing's own ``_list_fields`` branch (below) keeps rather than repairs, the
    same "splice only what the schema itself flags" discipline every prior branch in this table
    has followed.
  * **``date_field="date"`` — a plain, required Date field (``reqd: 1``, no default,
    ``allow_on_submit`` confirmed absent) — REJOINS Project Update's own ELEVENTH date pattern (a
    second member, never a new one)**, per the standing "a fieldname collision joins a proven
    exclusivity set" discipline: Project Update's own ``date`` is likewise ``reqd``-less with no
    default, while Asset Value Adjustment's copy IS ``reqd`` — the two doctypes share a literal
    fieldname with different ``reqd`` postures, the same kind of nominal-only collision Asset
    Movement's ``transaction_date`` rode against the Date-typed set (widened by string match, the
    type/behavior distinction pinned per-doctype, never inherited).
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — ``class AssetValueAdjustment(Document):``
    (``asset_value_adjustment.py:21``) defines no ``def submit``/``def cancel`` anywhere in its
    11-method body** — ``on_submit``/``on_cancel`` hooks only (lines 66/76).
  * **Ledger preview: UNCALLABLE, the same bare-``Document`` MRO this campaign already knows —
    joins the skip tuple, its TWENTY-FIRST member.** No ``make_gl_entries``/``make_sl_entries``/
    ``GLEntry`` reference anywhere in ``asset_value_adjustment.py``. **The irony worth naming
    plainly: this doctype's OWN preview is honestly empty, yet submitting it DOES create real GL —
    through the synchronously-created sibling Journal Entry, never through this document's own
    name.** The ledger-preview-unavailable disclosure says so explicitly, distinguishing this from
    every prior UNCALLABLE row (Dunning, LCV, …) whose emptiness really does mean "no GL posts."
  * **THE SIBLING JOURNAL ENTRY — SYNCHRONOUS, not scheduled; the mechanism differs from Asset's
    own async depreciation channel by KIND, not just timing.** ``make_asset_revaluation_entry``
    (lines 86-129) builds a Journal Entry in memory (``je.posting_date = self.date`` — line 101, a
    direct copy, never re-derived), appends two accounting rows keyed on the value increase/
    decrease, sets ``je.flags.ignore_permissions = True`` (line 126) THEN calls ``je.submit()``
    (line 127) — all inside this same ``on_submit`` call, inside the SAME request/transaction the
    broker's own marker already governs. The JE's own name is written back via
    ``self.db_set("journal_entry", je.name)`` (line 129) BEFORE ``on_submit`` returns, so the
    broker's own execute-time response for THIS submit already carries the sibling's name — no
    separate reconciliation sweep is needed to prove the sibling exists, unlike Asset's own
    depreciation JEs (created hours/days later by ``post_depreciation_entries``, outside this or
    any broker call, needing :func:`pacioli.tools._tool_prove_orphans`-style reconciliation if
    ever audited against the broker's own receipt ledger). Placed correctly against Asset's own
    disclosed async-GL scope boundary: Asset's finding is "this broker cannot see or govern a
    channel that fires later, outside any call it makes"; this doctype's finding is "this broker
    DOES see the channel — it fires inside the very call being governed, and the created
    document's name is legible in that same call's own outcome" — a materially different, WEAKER
    disclosure need (there is no time-boundary or later-arriving state to warn about), still
    disclosed here for completeness rather than silently dropped (:func:`pacioli.tools.
    _asset_value_adjustment_submit_risk_flags`).
  * **THE PERMISSION-ONLY BYPASS — a bypass grade this campaign has not yet named, on BOTH
    directions of the sibling Journal Entry's lifecycle (submit line 126, cancel line 177),
    pinned line-by-line from ``frappe/model/document.py``.** ``je.flags.ignore_permissions = True``
    (submit) and ``revaluation_entry.flags.ignore_permissions = True`` (cancel, line 177, alongside
    ``revaluation_entry.flags.via_asset_value_adjustment = True``) both feed the SAME single choke
    point: ``Document.has_permission`` (``document.py:400-412``) returns ``True`` UNCONDITIONALLY
    for ANY ``permtype`` the instant ``self.flags.ignore_permissions`` is truthy (line 407-408),
    before ``frappe.permissions.has_permission`` is ever consulted — so this skips BOTH the
    generic write-permission gate every ``_save()`` call makes (``check_permission("write",
    "save")``, line 577) AND the dedicated submit/cancel-permission gate
    ``check_docstatus_transition`` makes for a real docstatus move (``check_permission("submit")``
    line 1127/1138, ``check_permission("cancel")`` line 1141) — an ACL/authorization bypass, not a
    data-integrity one. **Distinct in KIND from every bypass this campaign has named, not merely
    in degree: Maintenance Visit's ``db_update``, Asset Movement's/Asset Maintenance Log's
    ``frappe.db.set_value``, and Quality Inspection's raw SQL all skip validate/hooks/version-
    history (data-integrity checks); Delivery Trip's sanctioned ``update_after_submit`` save skips
    only the doctype's own ``validate()`` and one field-lock check while running permission checks
    NORMALLY. This bypass is the mirror image: EVERY hook, EVERY validation, ``check_no_back_links_
    exist``, notifications, webhooks, and ``save_version()`` run on the sibling JE's cancel exactly
    as if a human with full JE-cancel rights had clicked Cancel themselves (the cancel-side
    ``_cancel()`` -> ``_save()`` path is the JE's OWN ordinary, full lifecycle) — the ONLY thing
    skipped is whether the ACTING CREDENTIAL is permitted to submit/cancel a Journal Entry at all.**
    ERPNext's own comment names the intent plainly (line 176: "Ignore permissions to match Journal
    Entry submission behavior") — a design choice that an AVA-submit/cancel credential should be
    able to create/retire its own revaluation JE even without independent JE permissions, never
    disclosed by the dossier on the submit side at all.
  * **``update_asset()`` — READ LINE BY LINE, three distinct write mechanisms, one of them a
    THIRD sibling-document channel the dossier's one line ("reschedules depreciation") never
    names.** Called identically from both ``on_submit`` and ``on_cancel`` (line 68/78), with the
    direction folded into a single sign flip (``update_asset_value_after_depreciation``, line 188:
    ``difference_amount = self.difference_amount if self.docstatus == 1 else -1 *
    self.difference_amount`` — confirmed the exact line and semantics the dossier names).
    (1) ``update_asset_value_after_depreciation`` (lines 187-203): when ``asset.calculate_
    depreciation`` is set, each matching ``finance_books`` child row gets
    ``row.value_after_depreciation``/``row.expected_value_after_useful_life`` adjusted then
    ``row.db_update()`` (line 199) — a RAW partial-row ORM write, no validate/hooks/version
    history; then ``asset.value_after_depreciation += flt(difference_amount); asset.db_update()``
    (lines 201-202) — the SAME grade on the parent Asset itself. This is the Maintenance Visit
    ``db_update`` bypass grade, applied here to the linked Asset rather than a sibling document.
    (2) ``reschedule_depreciation(asset, note)`` (``asset_depreciation_schedule.py:194-219``,
    called unconditionally at line 184 but a no-op when ``asset.finance_books`` is empty): for
    EACH finance-book row, reads the asset's current Asset Depreciation Schedule
    (``is_submittable: 1``, confirmed). **If a submitted (``docstatus==1``) schedule exists, it is
    CANCELLED** (line 216-217: ``current_schedule.flags.should_not_cancel_depreciation_entries =
    True; current_schedule.cancel()`` — the flag SUPPRESSES that schedule's own ``on_cancel``
    cascade-cancel of its posted depreciation Journal Entries, confirmed at
    ``asset_depreciation_schedule.py:107-109``: ``if not self.flags.should_not_cancel_
    depreciation_entries: self.cancel_depreciation_entries()`` — so already-POSTED depreciation
    JEs are explicitly preserved, never touched, by this specific call), and a brand-new Asset
    Depreciation Schedule is built and **SUBMITTED** (line 219: ``new_schedule.submit()``) —A
    THIRD sibling submittable document this landing's own submit/cancel arms, alongside the
    revaluation Journal Entry above, entirely unnamed by the dossier's single-line summary. This
    channel needs a LIVE read of the linked Asset's own ``calculate_depreciation``/
    ``finance_books`` state this plan cannot perform (the Asset Movement "monotonic validate needs
    other movements' state" shape) — disclosed as unconditional prose, never a data-driven flag
    invented on a field this draft does not carry.
    (3) ``asset.set_status()`` (line 185, body ``asset.py:770-774``): ``self.db_set("status",
    status)`` — the Asset Movement/Asset Maintenance Log ``db_set`` bypass grade (``before_change``/
    ``on_change`` hooks fire; validate/permission/version-history do not).
  * **THE CLOSED-BOOKS SCOPE GAP — traced precisely, not assumed equal.** Asset Value Adjustment
    is confirmed ABSENT from ``hooks.py``'s ``period_closing_doctypes`` (18 entries,
    ``hooks.py:326-345``), so this broker's own doctype-and-date-aware closed-books check
    (``get_period_locks``) can never find a matching ``closed_documents`` entry for "Asset Value
    Adjustment" for a structural reason beyond mere absence from the list: ERPNext's own
    ``Accounting Period.get_doctypes_for_closing`` (``accounting_period.py:86-95``) — the SAME
    server-side query the client form's own dropdown uses — restricts the offered choices to
    ``frappe.get_hooks("period_closing_doctypes")`` ONLY, so "Asset Value Adjustment" can never be
    selected as a closeable doctype through the sanctioned UI path at all. The doctype-specific
    branch of this broker's own closed-books belt is therefore a structural no-op for THIS
    doctype specifically (only the doctype-INDEPENDENT boundaries — ``accounts_frozen_till_date``/
    the latest Period Closing Voucher — can ever lock it), the same equal-or-stricter posture
    every other non-``period_closing_doctypes`` row already carries. **The sibling Journal Entry
    is different: Journal Entry genuinely IS a ``period_closing_doctypes`` member**, so its own
    native ``validate_accounting_period_on_doc_save`` hook (registered on `"validate"`, fired by
    ``je.submit()``'s own ``run_before_save_methods``) DOES check a real, normally-configurable
    ``closed_documents`` lock for "Journal Entry" against the identical date value
    (``je.posting_date = self.date``, a direct copy — the VALUES can never diverge). **This
    creates a genuine, disclosed scope gap: a period closed specifically for Journal Entry (a
    standard, independent per-doctype close — closing one doctype for a period while leaving
    others open is the FEATURE's own purpose) would let this broker's own ``plan_submit`` read
    "ok" for Asset Value Adjustment (no lock possible under that doctype name) while the
    synchronously-created sibling Journal Entry is refused at EXECUTE time by ERPNext's own native
    per-period gate — a real failure mode this plan cannot see, because it only ever queries locks
    for the doctype it was asked to plan, never for a sibling it does not yet know it will
    create.** Disclosed as unconditional prose (:func:`pacioli.tools.
    _asset_value_adjustment_submit_risk_flags`) rather than a second live network call this
    landing does not add.
  * **Doomed-submit / cross-document gates in ``validate()`` (lines 44-47), needing a live read
    this plan cannot perform:** ``validate_date`` (lines 49-57) throws if ``self.date`` falls
    before the linked Asset's own ``purchase_date`` — a live comparison against another document's
    field, disclosed as prose, never a data-driven flag guessing the Asset's own purchase date.
  * **Cascade — CONFIRMED ZERO incoming edges (dossier §8 correct), a full-tree grep for
    ``"options": "Asset Value Adjustment"`` over BOTH the erpnext-16 and frappe-16 checkouts
    returning only this doctype's own self-referencing ``amended_from``.** But this document is
    NOT purely a cascade leaf in the fuller sense Asset Movement's own landing already
    established: its own ``asset`` field (Link -> Asset, ``reqd: 1``) is one of the SAME 18
    ``Link -> Asset`` edges Asset's own landing enumerated — Asset's own cascade docstring names
    "Asset Value Adjustment" explicitly among that 18. **Two directions of one relationship, same
    as Asset Movement's own precedent: nothing links INTO Asset Value Adjustment (a leaf for its
    own target scans), while a submitted Asset Value Adjustment DOES appear as a dependent NODE
    inside an Asset's own ``plan_cascade_cancel`` graph** — the per-node cancel-risk disclosure is
    wired into that loop too (:meth:`PacioliBroker._tool_plan_cascade_cancel`), the same shape
    Asset Movement's own landing established.
  * **Side surface: ONE** ``@frappe.whitelist()`` **callable, confirmed by a full grep of
    ``asset_value_adjustment.py`` — matching the dossier's own §9 count exactly:**
    ``get_value_of_accounting_dimensions`` (lines 229-232) — read-only, returns a dict of Asset
    field values, no mutation. The broker grants nothing toward it.

Breadth (Payment Order) — Wave 5's sixth row, the thirty-eighth supported doctype (dossier at
``docs/plans/dossiers/payment_order.md`` — the central correction settles ``party_field`` from
source, below; a second correction on the doomed "Override present: YES" framing, a third on the
``period_closing_doctypes`` line citation, and a fourth closing a genuine gap: the dossier never
mentions ``company`` at all). Full source read of ``payment_order.py`` (121 lines) and
``payment_order.json``/``payment_order_reference.json`` (both ``json.load``-enumerated):

  * **``company`` — PRESENT AND REQUIRED, a gap the dossier's own summary table never mentions.**
    ``payment_order.json``: ``{"fieldname": "company", "fieldtype": "Link", "options": "Company",
    "reqd": 1}`` — the ordinary pinned-company path every non-companyless doctype in this campaign
    rides, spliced literally into the list tier below. Not the "present-but-not-reqd" 3-member set
    (PI/QI/AVA) — a plain, mandatory field, confirmed by full enumeration of all 10 real fields (12
    raw ``fields``-array entries less two layout breaks — ``column_break_2``/``section_break_5``).
  * **``party_field`` — THE CENTRAL CORRECTION. The dossier's §1 calls this ``party_field="party"``
    ("Single static party header: YES"); reading the schema AND the code says otherwise: ``party``
    (Link -> Supplier, ``payment_order.json``) carries ``"depends_on": "eval:
    doc.payment_order_type=='Payment Request';"`` — conditional, not unconditional. This campaign's
    own standing rule (stated verbatim in this table's Blanket Order entry: "no single static
    fieldname is ever unconditionally the party") has so far been applied to DUAL conditional pairs
    (Blanket Order's customer/supplier, Bank Guarantee's customer/supplier) and Dynamic Link pairs
    (Quotation, Quality Inspection) — Payment Order is the FIRST case of a SINGLE field that is
    still conditional, and the same rule applies for the same reason: ``party_field=None``, not
    ``"party"``. Stronger than any prior conditional-party doctype's own case: a full grep of
    ``payment_order.py`` finds ``self.party``/``doc.party`` referenced NOWHERE in the Python
    module — not in ``update_payment_status``, not in ``make_journal_entry`` (which takes its
    ``supplier`` from a whitelisted PARAMETER, never ``self.party``). Confirmed further from BOTH
    of ERPNext's own ``make_payment_order`` mapper functions that create a Payment Order in the
    first place (``payment_request.py:1068-1100`` and ``payment_entry.py:3559-3591``): neither
    ``set_missing_values`` ever sets ``target.party`` — only ``target.payment_order_type`` and a
    ``references`` child row carrying its OWN ``supplier`` field. The header ``party`` field is
    genuinely decorative UI-conditional metadata, never read or written by this doctype's own
    server-side code in any direction — the real per-line party lives on each ``references`` child
    row's own ``supplier`` field (``payment_order_reference.json``, ``in_standard_filter: 1``,
    always present regardless of ``payment_order_type``), never disclosed as a governed
    ``party_field`` for the same reason RFQ's own child-table supplier data never was.
  * **``status``/``grand_total`` — CONFIRMED ABSENT (dossier correct)**, no substitute of any
    kind — the Asset Movement "nothing left to splice" shape: the child ``references`` table
    carries each line's own ``amount``, but nothing rolls up onto the parent.
  * **in_list_view — CONFIRMED, three real fields (dossier correct):** ``party`` (conditional,
    still ``in_list_view: 1`` on the schema — spliced as CONTEXT below, the same "splice a real
    conditional field literally by name" treatment Blanket Order's/Bank Guarantee's own party
    pairs already established, just for a single field this time), ``posting_date`` (the
    ``date_field``), and ``company_bank`` (Link -> Bank, fetched from ``company_bank_account``).
    No ERPNext-authored ``payment_order_list.js`` exists (confirmed: no such file in the checkout)
    to add anything beyond the schema's own flags, unlike Delivery Trip's own ``status`` case.
  * **``date_field="posting_date"``** (Date, default ``"Today"``, ``reqd`` absent) — REJOINS the
    largest existing pattern in this table as its FOURTEENTH member (SI/PI/PE/JE/DN/PR/SE/POS/
    Dunning/SR/LCV/JC/Sales Forecast, now Payment Order) — the unremarkable, most common case,
    never a new pattern.
  * **Period-closing: Payment Order is NOT in ``hooks.py``'s ``period_closing_doctypes``** — a
    dossier LINE-NUMBER correction: the dossier cites "line 117-133"; the real tuple, confirmed by
    reading the checkout directly, is at ``hooks.py:326-345`` (an 18-entry list — Sales Invoice,
    Purchase Invoice, Journal Entry, Bank Clearance, Stock Entry, Dunning, Invoice Discounting,
    Payment Entry, Period Closing Voucher, Process Deferred Accounting, Asset, Asset
    Capitalization, Asset Repair, Delivery Note, Landed Cost Voucher, Purchase Receipt, Stock
    Reconciliation, Subcontracting Receipt) — Payment Order genuinely absent either way, the
    dossier's CONCLUSION survives, its citation does not.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD`` — a SECOND dossier-error class correction.** The
    dossier's §4 header literally reads "Override present: YES", the exact mislabeling this
    campaign has now caught twice before (Bank Guarantee, Wave 4): ``class
    PaymentOrder(Document):`` (``payment_order.py:13``) defines exactly TWO methods on the whole
    class — ``on_submit`` (lines 38-39: ``self.update_payment_status()``) and ``on_cancel`` (lines
    41-42: ``self.update_payment_status(cancel=True)``) — both lifecycle HOOKS called by the base
    ``Document.submit()``/``.cancel()``, never overrides of those methods themselves (no ``def
    submit``/``def cancel`` anywhere in the file). The dossier's own body text (§4's "Path:
    run_method (not client_rpc)") already reaches the right ANSWER; only its own header contradicts
    it — the same "conclusion right, label wrong" shape Bank Guarantee's landing named. **Payment
    Order also carries NO ``validate()`` method at all** — confirmed absent from the full 121-line
    file — the plainest hook-body shape this campaign has found: two hooks, zero throws, zero
    gates of any kind anywhere in the class.
  * **Ledger preview: UNCALLABLE, the same bare-``Document`` MRO.** No ``make_gl_entries``/
    ``make_sl_entries``/``GLEntry`` reference anywhere in ``payment_order.py`` — joins the skip
    tuple, its TWENTY-SECOND member. Payment Order posts no GL or Stock Ledger entry of any kind
    under its own name; the ledger consequence of a payment lives entirely in the referenced
    Payment Entry/Payment Request/Journal Entry documents, never here.
  * **THE CENTRAL FINDING — ``update_payment_status`` (lines 44-57), fired identically (modulo one
    boolean argument) from both ``on_submit`` and ``on_cancel``:** for every row in
    ``self.references``, a raw ``frappe.db.set_value`` (line 57) writes a status value directly
    onto ANOTHER document — no ``validate()``, no hooks, no version history, no permission check
    on the target (the Asset Movement/Asset Maintenance Log bypass grade) — into a doctype/field
    pair chosen by ``self.payment_order_type``: ``"Payment Request"`` writes ``status`` onto the
    Payment Request named by that row's own ``payment_request`` field
    (``ref_doc_field = frappe.scrub(self.payment_order_type)`` = ``"payment_request"``);
    ``"Payment Entry"`` writes ``payment_order_status`` onto the Payment Entry named by that row's
    own ``reference_name`` field. On submit the value is ``"Payment Ordered"``; on cancel it is
    UNCONDITIONALLY ``"Initiated"`` — confirmed no ``frappe.db.get_value``/``frappe.get_doc`` read
    of the target's current value anywhere in the method. **The sharpest disclosure: Payment
    Request's own ``status`` field (``payment_request.json``) carries SIX further values beyond
    "Initiated" (``Requested``/``Partially Paid``/``Payment Ordered``/``Paid``/``Failed``/
    ``Cancelled``) — cancelling a Payment Order unconditionally stomps every referenced Payment
    Request back to "Initiated" even if its own state has since moved past that (e.g. to "Paid" by
    an unrelated Payment Entry), a real regression this raw-write pattern has not paired with a
    richer downstream state machine before now. Payment Entry's own mirror field
    (``payment_order_status``) carries only the same two values (``Initiated``/``Payment
    Ordered``) — no equivalent loss on that branch.**
  * **Cascade — genuinely NOT A LEAF, THREE real incoming submittable edges, confirmed by a
    full-tree grep for ``"options": "Payment Order"`` over BOTH the erpnext-16 and frappe-16
    checkouts:** Journal Entry (``payment_order``, ``is_submittable: 1``), Payment Entry
    (``payment_order``, ``is_submittable: 1``), Payment Request (``payment_order``,
    ``is_submittable: 1``) — plus this doctype's own self-referencing ``amended_from``, four hits
    total (matching the dossier's own §8 count). ERPNext's own generic ``get_submitted_linked_docs``
    mechanism already discovers these live — zero ``cascade.py`` code needed, the standing
    mechanism every non-leaf doctype in this campaign rides. **The JE edge carries its own twist:**
    a draft JE created by THIS doctype's own whitelisted ``make_payment_records`` links back via
    ``je.payment_order = doc.name``, but that JE is saved (``je.save()``), never submitted
    (``je.flags.ignore_mandatory = True; je.save()`` — confirmed, no ``je.submit()`` call anywhere
    in ``make_journal_entry``) — a DRAFT dependent never gates a cancel (only ``docstatus: 1``
    dependents do); only a JE a human separately submits later becomes a real blocking edge, the
    same way every other doctype's draft-dependent gap already reads.
  * **Side surface: THREE** ``@frappe.whitelist()`` **callables, matching the dossier's own §9
    count:** ``get_mop_query``/``get_supplier_query`` (both also decorated
    ``@frappe.validate_and_sanitize_search_inputs``, read-only search queries for the client
    form's own dropdowns) and ``make_payment_records`` (name, supplier, mode_of_payment=None) —
    **MUTATES**: calls ``make_journal_entry`` to build and save (never submit) a draft Journal
    Entry linked back via ``payment_order``, with ``ignore_mandatory=True`` and no deduplication
    against a prior call with the same (supplier, mode_of_payment) — an orphan-draft risk the
    dossier's own §11 names correctly. Nothing granted toward any of the three.

Breadth (Share Transfer) — a full-attention landing off the pre-verification addendum
(``docs/plans/dossiers/share_transfer.verify.md``, 2026-07-21), the thirty-ninth supported doctype.
The addendum already caught the dossier's two central errors — corrected below, both re-verified
here from a fresh ``json.load``/line-by-line read of ``share_transfer.py`` (341 lines),
``share_transfer.json``, and ``shareholder.py``/``shareholder.json`` (41/15 lines) — plus ONE
finding beyond the addendum's own scope, traced through both the erpnext and frappe checkouts:

  * **``party_field`` — CONFIRMED ``None`` (addendum correction #1, re-verified).** ``from_
    shareholder``/``to_shareholder`` (both Link -> Shareholder) are a CONDITIONAL PAIR, but the
    addendum's own dossier-vs-source correction reverses the ORIGINAL dossier's claimed
    directions: ``from_shareholder`` carries ``"depends_on": "eval:doc.transfer_type != 'Issue'"``
    (``share_transfer.json`` line 60 — hidden ONLY on Issue) and ``to_shareholder`` carries
    ``"depends_on": "eval:doc.transfer_type != 'Purchase'"`` (line 93 — hidden ONLY on Purchase),
    confirmed again here by a fresh ``json.load`` and cross-checked against ``basic_validations()``
    itself (``share_transfer.py:171``: ``self.to_shareholder = ""`` under Purchase; line 179:
    ``self.from_shareholder = ""`` under Issue — the code blanks the OPPOSITE field from what the
    original dossier claimed was hidden). **This pair is a GENUINELY NEW sub-variant of the
    dual-conditional-pair shape this table already carries (Blanket Order's customer/supplier,
    Bank Guarantee's own identical pair)**: those two are governed by a two-state Select where
    EXACTLY ONE of the pair is ever visible (mutually exclusive, never both, never neither).
    Share Transfer's own three-state ``transfer_type`` (Issue/Purchase/Transfer) is NOT mutually
    exclusive in the same way — Issue shows only ``to_shareholder``, Purchase shows only
    ``from_shareholder``, but **Transfer shows BOTH simultaneously** (neither hidden). The
    mechanical treatment stays the same as the two-state precedent — splice both real fields
    literally by name alongside the router column — but the underlying visibility logic is a
    different shape, worth naming rather than silently folding into the existing pattern.
    ``party_field=None`` regardless, the same "no single static fieldname is ever unconditionally
    the party" rule this table already states.
  * **Shareholder is NOT submittable at all (addendum correction #2, re-verified).**
    ``json.load`` on ``shareholder.json`` confirms ``'is_submittable' not in shareholder_json`` —
    the key is genuinely absent (Frappe treats an absent key as falsy), the same absent-key shape
    this campaign already named for Event (Maintenance Schedule's own landing). Every Shareholder
    write this doctype makes — in ``on_submit``/``on_cancel`` AND, per the finding below, in
    ``validate()`` itself — is an ordinary ``.save()``/``.insert()`` on a plain Document: no
    ``docstatus`` state machine, no auto-submit of any kind. The Maintenance Schedule "clean
    ``.save()``" honesty grade applies throughout, never a bypass grade.
  * **``company`` — PRESENT AND REQUIRED** (addendum correction #4): ``share_transfer.json``'s
    ``company`` field is ``{"fieldtype": "Link", "options": "Company", "reqd": 1}`` — the ordinary
    pinned-company path, NOT companyless, confirmed by the complete 26-field/17-data-field
    enumeration (``json.load``, matching both the dossier's own title line and the addendum's
    count exactly: 9 layout breaks — 5 Section Break, 4 Column Break — plus 17 real fields).
  * **``in_list_view`` set is exactly ``{transfer_type}``** (confirmed via ``json.load`` —
    ``[f['fieldname'] for f in fields if f.get('in_list_view')] == ['transfer_type']``), matching
    the addendum precisely.
  * **``date_field="date"``** — Date (not Datetime), ``reqd: 1``, no ``default`` key (confirmed).
    REJOINS the standing ``date_field="date"`` pattern (Project Update, Asset Value Adjustment) as
    its THIRD member — Share Transfer's own copy is ``reqd`` like AVA's, unlike Project Update's
    ``reqd``-less copy — a purely nominal fieldname collision, never a new pattern.
  * **``submit_via=SUBMIT_VIA_RUN_METHOD``** — ``class ShareTransfer(Document):``
    (``share_transfer.py:17``) defines 11 methods (``on_submit``, ``on_cancel``, ``validate``,
    ``basic_validations``, ``share_exists``, ``folio_no_validation``, ``autoname_folio``,
    ``remove_shares``, ``return_share_balance_entry``, ``get_shareholder_doc``,
    ``get_company_shareholder``) plus the module-level ``make_jv_entry`` — none shadow
    ``Document.submit``/``.cancel``. Confirmed by a full grep for ``def submit``/``def cancel``:
    zero matches.
  * **Ledger preview: UNCALLABLE, the same bare-``Document`` MRO** (``ShareTransfer -> Document``
    directly, no intermediate base class) — no ``make_gl_entries``/``make_sl_entries``/``GLEntry``
    reference anywhere in ``share_transfer.py``. Joins the skip tuple, its TWENTY-THIRD member.
    Share Transfer posts no GL or Stock Ledger entry of any kind, sibling or otherwise — confirmed,
    no Journal Entry is ever ``.insert()``ed or ``.submit()``ed by ``on_submit``/``on_cancel``;
    ``make_jv_entry`` is a manual-UI-only draft-returning helper (module-level
    ``@frappe.whitelist()``, the sole whitelisted callable — matching both the dossier's and the
    addendum's count of 1), never called automatically.
  * **Cascade: a genuinely ISOLATED LEAF — confirmed the strictest way this campaign has found.**
    A full-tree grep of ``'"options": "Share Transfer"'`` over BOTH ``erpnext-16`` and
    ``frappe-16`` returns exactly ONE hit, ``share_transfer.json:180`` (the doctype's own
    self-referencing ``amended_from``) — no other doctype anywhere links to Share Transfer.
    UNLIKE Asset Movement/Asset Value Adjustment (leaves for their own incoming edges, yet each
    still a dependent NODE inside Asset's own 18-edge cascade graph via its own outgoing ``asset``
    Link), Share Transfer's own outgoing Link fields (``from_shareholder``/``to_shareholder`` ->
    Shareholder, ``share_type`` -> Share Type, ``asset_account``/``equity_or_liability_account``
    -> Account, ``company`` -> Company) point ONLY at non-submittable/non-transactional doctypes
    this broker's cascade mechanism never treats as a graph target — Share Transfer can never
    appear as a node in ANY OTHER doctype's ``plan_cascade_cancel`` graph either. A submitted
    Share Transfer's leaf ``plan_cancel`` is therefore NEVER refused by the standing blast-radius
    gate; ``plan_cascade_cancel``/``cascade_cancel`` need no doctype-specific wiring at all.
  * **THE FINDING BEYOND THE ADDENDUM'S OWN SCOPE — a doomed-submit gate hiding inside the
    Purchase branch of ``basic_validations()`` (``share_transfer.py:170-177``), traced end to end
    through BOTH checkouts.** The addendum's own finding #3 described ``autoname_folio`` firing
    "when ``self.from_folio_no``/``self.to_folio_no`` is blank" generically, without noticing a
    genuine field/branch mismatch in ERPNext's own source: line 171 blanks ``self.to_shareholder``
    to ``""`` for a Purchase-type transfer, THEN lines 174-175 check ``self.from_folio_no`` but —
    if it is blank — call ``self.autoname_folio(self.to_shareholder)``, passing the JUST-BLANKED
    field (never ``self.from_shareholder``, the real populated field for a Purchase). Traced the
    full chain:
      1. ``autoname_folio("")`` (lines 242-249) calls ``self.get_shareholder_doc("")``.
      2. ``get_shareholder_doc("")`` (lines 317-324): ``frappe.db.get_value("Shareholder",
         {"name": ""}, "name")`` — no Shareholder is ever named the empty string (naming_series
         ``ACC-SH-.YYYY.-``) — returns ``None`` — then calls ``frappe.get_doc("Shareholder",
         None)``.
      3. ``frappe.get_doc("Shareholder", None)`` dispatches to ``Document.__init__("Shareholder",
         None)`` (``frappe/model/document.py:140-146``, ``206-221``) — ``self.name`` is set to the
         real value ``None`` (two positional args, not the Single-doctype special case), then
         ``load_from_db()`` runs.
      4. ``load_from_db()`` (``frappe/model/document.py:252-297``): ``self.name`` (``None``) fails
         ``isinstance(self.name, str | int)`` (line 271), so it falls to
         ``frappe.db.get_value(doctype="Shareholder", filters=None, fieldname="*", ...)``
         (lines 286-291).
      5. ``get_values`` (``frappe/database/database.py:663-705``): ``filters is None`` fails the
         query-branch condition at line 663 outright (skipping any table read — NOT an arbitrary
         or most-recent row); Shareholder is not a Single doctype (line 691's ``elif`` fails too);
         the final ``else`` (lines 704-705) returns ``None`` unconditionally.
      6. Back in ``load_from_db``, ``d`` is ``None`` -> ``frappe.throw(_("{0} {1} not found")
         .format(...), frappe.DoesNotExistError(doctype="Shareholder"))``
         (``document.py:294-297``) — message ``"Shareholder None not found"``.
    **CONCLUSION: any Purchase-type Share Transfer whose ``from_shareholder`` is populated (past
    the earlier blank-check) but whose ``from_folio_no`` is blank at ``validate()`` time THROWS
    ``frappe.DoesNotExistError`` before any share_balance mutation ever runs.** This is genuinely
    reachable, not a theoretical edge: ``from_folio_no``'s only auto-fill is a CLIENT-SIDE
    ``fetch_from: "from_shareholder.folio_no"`` (``share_transfer.json``), which stays blank
    whenever the seller's own Shareholder has never had a folio_no assigned, or whenever the
    document is authored via the REST API bypassing the Desk form's fetch entirely — exactly the
    channel this broker's own credential uses. Deterministic straight off this draft's own two
    fields, disclosed as a doomed-submit flag (see ``_share_transfer_submit_risk_flags`` in
    ``tools.py``). The Issue branch's mirror check (lines 178-185: checks ``to_folio_no``, calls
    ``autoname_folio(self.to_shareholder)`` on the REAL non-blanked field) and the Transfer/else
    branch (186-190: same shape) carry NO equivalent bug — both correctly assign a fresh folio
    number to the real target Shareholder via a full ``.save()`` when blank.
  * **``folio_no_validation()`` (``share_transfer.py:222-240``), called immediately after
    ``basic_validations()`` inside ``validate()`` — the addendum's finding #3, re-verified:** for
    each of ``from_shareholder``/``to_shareholder`` still populated after ``basic_validations()``'s
    own blanking, if that Shareholder's own ``folio_no`` is currently blank, it is set (from this
    document's own ``from_folio_no``/``to_folio_no``) and ``.save()``d — a SECOND independent
    write pass, needing the target Shareholder's live state to know for certain, so it stays
    prose-disclosed rather than a gated flag. **Consequence, unchanged from the addendum: ``.save()``
    of a Share Transfer as a bare DRAFT already mutates a Shareholder** — ``validate()`` runs on
    every save, not only submit — a governance-relevant gap for any channel that treats
    insert/save of this doctype as out of scope while gating only ``submit``.
  * **``on_submit``/``on_cancel`` (lines 45-95/97-149) mirror each other exactly, branch for
    branch, reversed** — confirmed matching the addendum precisely: Issue appends a
    share_balance row to the auto-created-if-absent company Shareholder AND to
    ``to_shareholder``'s own Shareholder (two ``.save()``/``.insert()`` calls); Purchase rewrites
    ``from_shareholder``'s and the company's own share_balance via ``remove_shares()`` (in-place
    child-table rewrite + ``.save()``, no new documents); Transfer rewrites ``from_shareholder``
    via ``remove_shares()`` and appends to ``to_shareholder``. ``on_cancel`` carries ZERO
    ``frappe.throw`` calls and never calls ``validate()`` (Frappe's own cancel lifecycle runs
    ``before_cancel``/``on_cancel``, not ``validate()``) — confirmed by a full grep, matching the
    addendum's own line-by-line re-derivation of all 11 throws (173, 177, 181, 185, 188, 192, 194,
    196, 200, 230, 240 — every one inside ``validate()``'s own call graph, none in ``on_cancel``).
  * **The unguarded ``remove_shares()``/``get_shareholder_doc()`` risk on cancel (addendum landing
    risk #6) — data-dependent, disclosed as prose, not a gated flag:** every ``on_cancel`` branch
    calls ``frappe.get_doc("Shareholder", shareholder)`` on the same names ``on_submit`` touched;
    if any has since been deleted or renamed, cancel throws ``frappe.DoesNotExistError`` — real,
    but not readable from this draft alone.

Breadth (BOM Creator) — the fortieth supported doctype, John's ruling 2 (two-phase PROVE, "truth
always"; ``docs/plans/2026-07-21-cancel-truth-rulings.md``), built off the design study
(``docs/plans/dossiers/bom_creator.design.md``, option (b)). Verified directly against
``/root/.pacioli/refs/erpnext-16/erpnext/manufacturing/doctype/bom_creator/bom_creator.py`` (613
lines) and ``bom_creator.json`` (40 fields, ``json.load``-enumerated), plus a full two-checkout
grep (erpnext-16 AND frappe-16) for ``'"options": "BOM Creator"'``.

* **submit_via=run_method — confirmed:** no ``def submit``/``def cancel`` override anywhere in
  ``bom_creator.py`` — only ``on_submit``/``on_cancel``/``before_submit`` HOOKS, called by the base
  ``Document.submit()``/``.cancel()``, never overrides of those methods themselves.
* **party_field=None** — zero Customer/Supplier/Party-shaped field at the document header level
  across all 40 enumerated fields; BOM Creator operates purely on Item and manufacturing
  operations.
* **status CONFIRMED REAL**: ``Select``, ``default: "Draft"``, ``options: "Draft\\nSubmitted\\nIn
  Progress\\nCompleted\\nFailed\\nCancelled"``, ``read_only: 1``, ``no_copy: 1`` — alongside a
  separate ``error_log`` (``Text``, ``read_only: 1``) populated only on the Failed branch
  (``bom_creator.py:317-322``). No ``grand_total`` — ``raw_material_cost`` (Currency, in_list_view,
  read_only) is the correct stand-in, alongside ``item_code``/``currency`` (the fortieth doctype's
  own ``_list_fields`` branch — all three confirmed ``in_list_view: 1``, nothing else).
* **company IS present and reqd** — NOT companyless (``bom_creator.json``: ``{"fieldtype": "Link",
  "options": "Company", "reqd": 1}``).
* **date_field=None — THE THIRD DATELESS DOCTYPE** (after BOM/Packing Slip): a direct field-type
  enumeration of all 40 fields finds zero ``Date``/``Datetime`` fieldtypes (not eyeballed — see
  ``bom_creator.json``). BOM Creator is absent from ``hooks.py``'s ``period_closing_doctypes``
  list, so the closed-books chain's declared-dateless pass (``pacioli.plan.NO_DATE_FIELD``) is
  equal to ERPNext here too, never a weakening.
* **THE CENTRAL FINDING — the two-phase PROVE mechanism.** ``on_submit`` (``bom_creator.py:
  247-248``) is one line, ``self.enqueue_bom_creation()``, which (``bom_creator.py:255-261``) calls
  ``frappe.enqueue(self.create_boms, queue="short", timeout=600, is_async=True)``. This runs
  AFTER ``before_submit`` (``bom_creator.py:163-165``) has already called ``set_status()``
  (``bom_creator.py:133-142``), which sets ``status = "Submitted"`` from ``docstatus == 1`` —
  inline, synchronously, inside the same request this broker's submit call answers. So the
  docstatus transition this broker confirms IS real; only the doctype's OWN declared deliverable
  — the actual BOM tree, built by ``create_boms()`` on the "short" queue, up to a 600-second
  worker timeout — remains genuinely unknown at that moment. A doctype registered in
  :data:`ENQUEUE_ON_SUBMIT_DOCTYPES` (below) is never claimed a plain ``"committed"`` at submit:
  the ledger's own outcome status narrows to ``"committed_pending_async"``
  (``tools.py``'s ``_QueuedConsequenceEffects``), carrying a ``queued_consequence`` marker
  (channel/queue/status_at_submit); a later sweep (``prove_verify``'s own
  ``_sweep_queued_consequences``) attests the terminal ``Completed`` (with the built BOM names,
  cross-checked by a real ``BOM`` count, never the status string alone — the design study's own
  partial-tree lesson) or ``Failed`` (with ``error_log`` surfaced) result once the worker lands,
  or reports it still pending, said plainly, never silently. ``create_bom()``'s own idempotency
  guard (``bom_creator.py:328-337``) is keyed to ``self.name`` — the CURRENT document's own name
  — so an AMENDED draft (a fresh document under a new name, ``amended_from`` set) builds a full
  SECOND BOM tree with nothing in ERPNext or this broker detecting the duplication; disclosed at
  ``plan_submit`` for an amended draft specifically (``_bom_creator_submit_risk_flags``,
  data-driven off the draft's own ``amended_from`` field).
* **on_cancel (``bom_creator.py:160-161``) is synchronous** — a single ``self.set_status(True)``
  db_set, nothing enqueued — but it does NOT cascade to any BOMs the job already built: those BOM
  documents remain submitted, still pointing at the now-cancelled creator via their own
  ``bom_creator`` field (dossier §7/§8, confirmed).
* **CASCADE: NOT a leaf.** The full two-checkout grep for ``'"options": "BOM Creator"'`` returns
  exactly TWO hits: this doctype's own self-referencing ``amended_from`` (``bom_creator.json:
  228``, auto-exempted from frappe's own back-link check by field name — ``delete_doc.py:319``)
  and BOM's own real, submittable, non-required ``bom_creator`` Link (``bom.json:600-608``). So
  once ``create_boms()`` has built at least one submitted BOM, this broker's OWN blast-radius
  gate (mirroring frappe's ``check_no_back_links_exist``, ``document.py:1450-1452``) refuses a
  leaf cancel of the creator exactly as it would for any other doctype with a submitted incoming
  link. BOM Creator is deliberately NOT registered in :data:`SELF_UNLINKING_DOCTYPES` — its
  ``on_cancel`` never touches the BOM's own ``bom_creator`` field the way Delivery Trip's
  ``update_delivery_notes`` clears the Delivery Note's own linking fields — so
  ``plan_cascade_cancel`` (cancelling the built BOM(s) first, a governable row in their own
  right, then the creator) is the governed path once any BOM exists; disclosed at ``plan_cancel``
  (``_bom_creator_cancel_risk_flags``, data-driven off the draft's own ``status`` field).
* **Native ledger_preview UNCALLABLE** (``BOMCreator(Document)`` directly, no ``make_gl_entries``
  anywhere — joins the skip tuple, 24th member); BOM Creator posts no GL of any kind, ever — its
  only products are BOM documents (manufacturing, not accounting).
* **CAVEAT: 8 whitelisted callables** (confirmed by a decorator-by-decorator count, matching the
  dossier): ``add_boms`` forces a ``self.submit()`` directly; ``enqueue_create_boms``
  (``bom_creator.py:250-253``) is an UNGOVERNED re-trigger of ``create_boms()``, gated only by
  ``self.check_permission("submit")`` — no plan, no marker — that can re-fire after a
  Completed/Failed terminal status (the idempotency guard bounds but does not prevent this);
  ``edit_bom_creator``/``add_item``/``add_sub_assembly``/``delete_node`` all mutate a submitted
  document's rows via ``.save()`` with no docstatus check; ``get_default_bom``/``get_children``
  are read-only. This grant covers submit/cancel/reads only; it does NOT extend to those methods,
  and no tool is built for any of them.

Breadth (Budget) — the forty-first supported doctype, landed off a pre-verification addendum
(``docs/plans/dossiers/budget.verify.md``, 2026-07-21). Re-verified directly against
``/root/.pacioli/refs/erpnext-16/erpnext/accounts/doctype/budget/budget.py`` (864 lines) and
``budget.json`` (40 fields, complete ``json.load`` enumeration), plus
``erpnext/controllers/buying_controller.py``, ``erpnext/controllers/budget_controller.py``,
``erpnext/accounts/general_ledger.py``, ``erpnext/stock/doctype/material_request/
material_request.py``, ``erpnext/buying/doctype/purchase_order/purchase_order.py``, and
``erpnext/hooks.py`` — every load-bearing citation below independently re-verified by direct
read/grep, not inherited from the addendum. The addendum held on every load-bearing claim; one
gloss named: its "Stop for annual/actual, Warn for monthly-accumulated — confirmed from the JSON
``default`` keys" generalization is true for three of the four ``applicable_on_*`` axes, but the
Cumulative Expense axis's two ``action_if_*`` Selects carry NO ``default`` key at all
(``budget.json:213-226``) — which is why :func:`pacioli.tools._budget_armed_axes` reads the live
draft's own values and never assumes a schema default.

* **party_field=None — a dual CONDITIONAL pair, ``cost_center``/``project``, gated on
  ``budget_against`` (Select, ``"Cost Center"``/``"Project"``, ``reqd: 1``, ``in_list_view: 1``).**
  ``cost_center`` carries ``depends_on: "eval:doc.budget_against == 'Cost Center'"``, ``project``
  carries ``== 'Project'`` (confirmed via a fresh ``json.load``); ``set_null_value()``
  (``budget.py:167-171``) clears whichever is inactive server-side on every ``validate()``. These
  are GL DIMENSION selectors (Cost Center/Project), never a Customer/Supplier-shaped party — the
  addendum's own framing, confirmed.
* **status ABSENT, grand_total ABSENT** — zero fieldname hits across all 40 fields (confirmed by
  a direct field-by-field enumeration, not eyeballed). ``budget_amount`` (Currency, ``reqd: 1``)
  and ``budget_distribution_total`` (Currency, ``read_only: 1``, sum of the ``budget_distribution``
  child rows) are the only monetary fields — NEITHER carries ``in_list_view: 1`` on this schema
  (confirmed), so neither stands in as a spliced list-tier substitute (see the ``_list_fields``
  branch below — this is a genuinely bare branch, no aggregate rides the list tier at all).
  ``in_list_view`` is exactly ``["budget_against", "company", "account"]`` — confirmed field-by-
  field, no other field (including ``cost_center``/``project``) carries the flag.
* **company reqd=1** — confirmed directly from the field dump; this is NOT a companyless row.
* **date_field="budget_start_date" — a Date (not Datetime), and THE CAMPAIGN'S FIRST HIDDEN
  date_field pin.** Both ``budget_start_date``/``budget_end_date`` are ``hidden: 1``, schema-
  optional (no ``reqd`` key), no ``default`` — but ``validate()`` (``budget.py:70-80``)
  unconditionally calls ``set_fiscal_year_dates()`` (``:99-110``), which reads
  ``frappe.get_cached_value("Fiscal Year", self.from_fiscal_year, "year_start_date")`` /
  ``"year_end_date"`` off ``from_fiscal_year``/``to_fiscal_year`` — both schema ``reqd: 1`` Links,
  so no document can even be inserted without them. **Every persisted Budget, draft or submitted,
  has both hidden dates populated by construction, on every single save** — a genuinely new date-
  field shape: hidden + schema-optional but validate()-FORCED non-blank via a mandatory upstream
  Link, distinct from a merely-optional-but-sometimes-blank field (Project Update) and from a
  declared-dateless doctype (BOM/Packing Slip/BOM Creator, ``date_field=None``). Chosen over its
  own sibling ``budget_end_date`` as the anchor, the same "window start, not its close" convention
  Blanket Order's ``from_date``/Supplier Scorecard Period's ``start_date`` already established.
  Not a blocker for the generic ``_date_field_for``/``_posting_date_of`` machinery (it reads
  ``doc.get(fieldname)`` regardless of UI visibility) — but because BOTH ends of the window are
  hidden, this is the first validity-window doctype where NEITHER end is a list-tier splice
  candidate; the ``_list_fields`` branch below splices no date at all, a genuinely new list-tier
  shape (see below). ``"budget_start_date"`` does not collide with any existing ``date_field``
  string — the FIFTEENTH distinct date-fieldname pattern this campaign has found.
* **submit_via=SUBMIT_VIA_RUN_METHOD** — confirmed by a full method-list grep of ``budget.py``'s
  ``Budget`` class: only ``validate``/7 private ``validate_*`` helpers/``before_save``/
  ``on_update``/``allocate_budget`` + 6 private distribution helpers/``validate_distribution_
  totals`` are defined. **Zero** ``submit``/``cancel``/``on_submit``/``on_cancel``/
  ``before_submit``/``on_trash`` anywhere in the class body — Budget is the SIXTH member of the
  "no on_submit/on_cancel hook of any kind" family (Blanket Order/Cost Center Allocation/Supplier
  Scorecard Period/Sales Forecast/Project Update/**Budget**), but by far its richest: where CCA's/
  SSP's own ``validate()`` bodies are a handful of helpers and Project Update's is a bare ``pass``,
  Budget's ``validate()`` alone spans 8 sequential sub-validations (``:70-80``) plus a completely
  separate ``before_save()``/``on_update()`` pair (``:232-352``) driving monthly/quarterly/half-
  yearly/yearly distribution regeneration on EVERY save, not just submit. The absence of
  ``on_submit``/``on_cancel`` here says nothing about how much logic gates a Budget's save — only
  that none of it is submit/cancel-specific.
* **Ledger preview: UNCALLABLE, confirmed from source.** ``class Budget(Document)``
  (``budget.py:28``) — a direct subclass, never ``AccountsController``/``StockController``. A
  full-file grep of ``budget.py`` finds no ``make_gl_entries``/``make_sl_entries``/``GLEntry``
  reference anywhere. Joins the skip tuple, its TWENTY-FIFTH member.
* **Cascade: a genuine LEAF, confirmed the strict way.** A full-tree grep of
  ``'"options": "Budget"'`` across BOTH ``erpnext-16`` AND ``frappe-16`` returns exactly ONE hit —
  ``budget.json:101``, the doctype's own self-referencing ``amended_from`` (Link, ``is_submittable:
  1``). ``revision_of`` (``budget.json:257-263``) is ``fieldtype: "Data"``, NOT a Link — it holds
  a Budget name as plain text, never a schema-enforced back-reference, so it does not widen this
  scan. No ``cascade.py`` wiring needed.
* **Whitelist count: exactly 1** — ``grep "@frappe.whitelist" budget.py`` returns one hit
  (``:851``, module-level ``revise_budget(budget_name)``, never instance-level). Its mutation is
  CLEAN, not a bypass: ``frappe.get_doc`` → ``old_budget.cancel()`` (a full, standard
  ``Document.cancel()``, a no-op beyond the ``docstatus`` flip since Budget defines no
  ``on_cancel``) → ``frappe.copy_doc()`` → ``new_budget.insert()`` (standard, re-runs full
  ``validate()``) — no raw SQL, no ``db_set``/``db_update`` bypass anywhere. Nothing granted; no
  tool built toward it.
* **THE CONTROL-PLANE FINDING — Budget's submit arms a belt governing FUTURE submits of OTHER
  documents.** Confirmed by an independent re-read of both engines: ``general_ledger.py``'s two
  call sites (``:229``, inside ``make_gl_entries``'s own cost-center-allocation branch; ``:443``,
  its own post-submit branch) call ``validate_expense_against_budget()`` (the LEGACY function,
  imported ``from erpnext.accounts.doctype.budget.budget import validate_expense_against_budget``
  at ``general_ledger.py:23``) UNCONDITIONALLY — no settings branch — for every GL-posting
  doctype. But Purchase Order's own ``on_submit`` (``purchase_order.py:441-455``) and Material
  Request's own ``on_submit`` (``material_request.py:230-236``, gated behind a cheap
  ``frappe.db.exists("Budget", {"applicable_on_material_request": 1, "docstatus": 1})`` check)
  both call ``self.validate_budget()`` (``buying_controller.py:1024-1046``), which branches on
  ``Accounts Settings.use_legacy_budget_controller``: **FALSY (the schema default) builds
  ``budget_controller.BudgetValidation(doc=self).validate()`` instead** — a separate,
  ``frappe.qb``-based reimplementation (``erpnext/controllers/budget_controller.py``, its own
  ``stop()``/``warn()``/``execute_action()`` at lines 288/291/294) that imports the SAME
  ``BudgetError`` (``budget_controller.py:9``) and is STILL synchronous, in-process, never
  enqueued. **Both engines are live simultaneously in this checkout**, depending on which document
  type is submitting — a docstring naming only ``validate_expense_against_budget()`` as "the"
  mechanism describes a path PO/MR take only when a deprecated setting is enabled; on a default
  bench they go through ``BudgetValidation``. Neither is async; the dossier's "Async channel"
  label (its own RED FLAG #1) is corrected.
* **THE DISARM FINDING (cancel direction, beyond the addendum's own scope) — confirmed by direct
  source read.** Both enforcement engines filter on ``docstatus == 1``:
  ``budget_controller.py:192`` (``bud.docstatus == 1``), ``:216``/``:246``
  (``po.docstatus.eq(1)``/``mr.docstatus.eq(1)`` on the OTHER document, not Budget itself — the
  Budget-side filter is ``:192``), and the legacy query at ``budget.py:391``/``:475``
  (``docstatus = 1``, confirmed by direct read). Cancelling a submitted Budget therefore DISARMS
  its own belt immediately and completely: every future PO/MR/GL-posting submit that would have
  been checked against it is no longer checked at all, the instant docstatus flips to 2 — no
  separate mechanism cancels or "turns off" the enforcement; the docstatus filter alone is the
  entire on/off switch. Disclosed data-driven off the draft's own ``applicable_on_*``/``account``/
  ``budget_against`` fields (``_budget_cancel_risk_flags``, ``tools.py``).
* **Deterministic-from-draft throws vs TOCTOU-shaped ones — the split the addendum's own landing
  risk #1 names.** ``validate_budget_amount`` (``budget_amount <= 0``, ``:82-84``) and
  ``validate_applicable_for``'s own combination logic (``:173-189`` — two throw branches PLUS a
  silent auto-mutation branch: if none of the three ``applicable_on_*`` axes is enabled,
  ``applicable_on_booking_actual_expenses`` is force-set to 1, never a throw) are both readable
  purely from the draft's own fields — gated flags. ``validate_account`` (``:148-165`` — group
  account / wrong company / non-P&L root type) needs a live read of the target Account's own
  ``is_group``/``company``/``report_type`` — genuinely external state this doc-only risk-flag
  function cannot read, so it stays PROSE, never gated (unconditional, informational).
  ``validate_duplicate`` (``:112-146``) and ``validate_existing_expenses`` (``:191-230``) query
  the LIVE state of other ``tabBudget``/``tabGL Entry`` rows at the moment ``validate()`` runs —
  genuinely TOCTOU-shaped, per this campaign's own "cross-document state stays prose" rule,
  sharper here because it can fire on the FIRST submit of a brand-new Budget, not just a
  revision.
* **``period_closing_doctypes``: Budget confirmed ABSENT** (``erpnext/hooks.py:326-345``, the
  real tuple — list runs Sales Invoice through Subcontracting Receipt, no Budget entry). Budget
  DOES appear once elsewhere in ``hooks.py`` (``accounting_dimension_doctypes``, ~line 546) — an
  unrelated list, never a lifecycle hook, and Budget has zero entries in ``hooks.py``'s own
  ``doc_events`` dict.

Breadth (Timesheet) — the forty-second supported doctype, landed off a pre-verification addendum
(``docs/plans/dossiers/timesheet.verify.md``, 2026-07-21). Re-verified directly against
``/root/.pacioli/refs/erpnext-16/erpnext/projects/doctype/timesheet/timesheet.py`` (577 lines) and
``timesheet.json`` (37 fields, complete ``json.load`` enumeration), plus
``erpnext/projects/doctype/timesheet_detail/timesheet_detail.{json,py}``,
``erpnext/accounts/doctype/sales_invoice/sales_invoice.py``, ``erpnext/accounts/doctype/
sales_invoice_timesheet/sales_invoice_timesheet.json``, ``erpnext/hooks.py``, ``erpnext/patches/
v14_0/remove_hr_and_payroll_modules.py``, and ``frappe/model/base_document.py``/``document.py`` —
every load-bearing citation below independently re-verified by direct read/grep, not inherited
from the dossier or the addendum.

**THE TRANSPORT RULING (dossier error, corrected — the same class of error that has now happened
THREE times): the dossier's own §4 concludes "client_rpc category."** That is wrong per this
campaign's own standing law: ``SUBMIT_VIA_CLIENT_RPC`` is reserved for a class that genuinely
SHADOWS ``Document.submit``/``.cancel`` with an undecorated override — Journal Entry and Stock
Reconciliation, the only two ever found. A lifecycle HOOK (``on_submit``) that throws is NOT an
override. Both dossier and addendum confirm ``timesheet.py``'s ``class Timesheet(Document):``
(line 25) defines no ``submit``/``cancel`` override anywhere in its 25-def class body (enumerated
below) — **Timesheet ships as ``SUBMIT_VIA_RUN_METHOD``.**

* **party_field="customer" — a plain, singular, header-level Link (``timesheet.json`` line
  confirmed via ``json.load``: ``fieldtype: "Link"``, ``options: "Customer"``, no ``reqd`` key).**
  The simple shape, for once — no dual conditional pair, no Dynamic Link, no child table. No
  ``supplier``/``party`` field anywhere across all 37 fields.
* **status precedence is BACKWARDS from how the dossier presents it — the sharpest correction in
  this row, re-verified line by line against ``timesheet.py:127-137``:**
  ```python
  def set_status(self):
      self.status = {"0": "Draft", "1": "Submitted", "2": "Cancelled"}[str(self.docstatus or 0)]
      if flt(self.per_billed, self.precision("per_billed")) >= 100.0:
          self.status = "Billed"
      if 0.0 < flt(self.per_billed, self.precision("per_billed")) < 100.0:
          self.status = "Partially Billed"
      if self.sales_invoice:
          self.status = "Completed"
  ```
  Three SEQUENTIAL UNCONDITIONAL ``if`` statements, never ``elif`` — the LAST one to fire wins.
  Real precedence (highest wins): **Completed** (``sales_invoice`` truthy) overrides **Billed**/
  **Partially Billed** overrides the ``docstatus`` baseline. ``per_billed >= 100`` does NOT
  guarantee "Billed" — a fully billed Timesheet with ``sales_invoice`` also set reads "Completed."
  Status options (7, confirmed via ``json.load``): ``Draft``/``Submitted``/``Partially Billed``/
  ``Billed``/``Payslip``/``Completed``/``Cancelled`` — Select, read-only, ``in_standard_filter: 1``,
  default ``"Draft"``. ``grand_total`` is ABSENT; the two stand-ins (``total_billable_amount``/
  ``total_billed_amount``, both ``allow_on_submit: 1``) are never renamed into the slot — Sales
  Invoice's own ``update_time_sheet`` writes both directly (see THE SECOND WRITER below).
  ``in_list_view`` is exactly ``{start_date, per_billed}`` (confirmed via ``json.load`` filter —
  no other field, including ``status``/``total_billable_amount``/``total_billed_amount``, carries
  the flag) — a combination not seen in any of the 31 prior ``_list_fields`` branches, forcing its
  own new branch (see below).
* **``validate_mandatory_fields()``'s first throw is inverted in the dossier's English (Section
  6) — the second sharpest correction.** Dossier: *"Row must have ``from_time`` AND ``to_time``"*
  — read naturally, missing EITHER one throws. Actual code (``timesheet.py:159-161``):
  ``if not data.from_time and not data.to_time: frappe.throw(...)`` — throws only when **BOTH**
  are absent. A row with one set and one blank passes this guard. The other two throws in the
  same method are quoted correctly: ``activity_type`` required if the PARENT document's own
  ``employee`` field is set (``:163-164`` — a header-level field read per row, not a row-level
  one); ``hours == 0.0`` (``:166-167``). All three are readable straight off the draft's own
  ``time_logs`` child rows — gated, deterministic risk flags
  (:func:`pacioli.tools._timesheet_submit_risk_flags`), never the dossier's paraphrase.
* **date_field="start_date" — a real Date field (not Datetime), ``read_only: 1``, no ``reqd``, no
  schema default, JOINS the existing ``start_date`` exclusivity set as its THIRD member** (after
  Supplier Scorecard Period and Bank Guarantee — confirmed independently from this doctype's own
  schema, not inherited by copy-paste: ``start_date``/``end_date`` are BOTH ``Date``, BOTH
  ``read_only: 1``, and BOTH recomputed by ``set_dates()`` on every ``validate()`` while
  ``docstatus < 2`` — **the dossier's own §3 timing claim is corrected by the addendum: this is
  NOT "on submit" alone, it recomputes on EVERY draft save AND every submit** — the window-start
  convention every prior ``start_date``/``from_date`` row already established. ``end_date`` is the
  window's close, never the anchor, the same convention. No collision with any other
  ``date_field`` string.
* **Ledger preview: UNCALLABLE, confirmed from source.** ``class Timesheet(Document):``
  (``timesheet.py:25``) — a direct subclass, never ``AccountsController``/``StockController``. A
  full-file grep of BOTH ``timesheet.py`` AND ``timesheet_detail.py`` finds no
  ``make_gl_entries``/``make_sl_entries``/``GLEntry`` reference anywhere. Joins the skip tuple —
  its TWENTY-SIXTH member (the addendum's own "22nd member" tally was already known-stale, written
  before four more landings; the live tuple in ``tools.py`` is counted directly at landing time,
  not inherited).
* **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by a full method-list grep of
  ``timesheet.py``'s ``Timesheet`` class: 25 defs total** (``validate``, ``on_discard``,
  ``on_update_after_submit``, ``calculate_hours``, ``calculate_total_amounts``,
  ``calculate_percentage_billed``, ``update_billing_hours`` (deprecated), ``set_status``,
  ``set_dates``, ``before_cancel``, ``on_cancel``, ``on_submit``, ``validate_mandatory_fields``,
  ``update_task_and_project``, ``validate_dates``, ``validate_time_logs``, ``validate_overlap``,
  ``set_project`` (deprecated), ``validate_project`` (deprecated), ``validate_overlap_for``,
  ``get_overlap_for``, ``check_internal_overlap``, ``update_cost``, ``update_time_rates``,
  ``unlink_sales_invoice``) — zero ``submit``/``cancel`` overrides among them. See THE TRANSPORT
  RULING above.
* **THREE lifecycle hooks the dossier's own §6/§7 never named:**

  1. ``on_update_after_submit()`` (``timesheet.py:81-84``) re-runs ``validate_mandatory_fields()``
     + ``update_task_and_project()`` + ``validate_time_logs()`` on EVERY post-submit resave — a
     live path: Timesheet carries 8 ``allow_on_submit`` fields, confirmed via ``json.load``:
     ``title``, ``total_hours``, ``total_billable_hours``, ``total_billed_hours``,
     ``total_costing_amount``, ``total_billable_amount``, ``total_billed_amount``, ``per_billed``.
  2. ``on_discard()`` (``timesheet.py:78-79``) — ``self.db_set("status", "Cancelled")``, a direct
     ``db_set`` bypass fired when a DRAFT is discarded (not a submitted-document mechanism, but a
     real, undocumented hook).
  3. ``before_cancel()`` (``timesheet.py:148-149``) — ``self.set_status()``, re-derived while
     ``docstatus`` is STILL 1 (not yet 2). This recomputes the Billed/Partially Billed/Completed
     branch one more time — it does NOT stamp "Cancelled"; easy to misread as a cancel-time status
     stamp, worth naming precisely.

* **THE SECOND WRITER — the row's sharpest fact, entirely undocumented by the dossier's own
  Sections 6/7/11.** Timesheet's status/billing fields are written from OUTSIDE, by Sales
  Invoice, via a raw no-trigger whole-document write:

  - ``SalesInvoice.on_submit`` (``sales_invoice.py:469``) calls ``self.update_time_sheet(...)`` —
    the call site is at line **524** (the addendum's own citation of ":522" is a two-line miss,
    corrected here); ``SalesInvoice.before_cancel`` (``:598``) calls it again at line **604**
    (addendum correct on this one).
  - ``update_time_sheet`` (``sales_invoice.py:828-837``):
    ```python
    def update_time_sheet(self, sales_invoice):
        for d in self.timesheets:
            if d.time_sheet:
                timesheet = frappe.get_doc("Timesheet", d.time_sheet)
                self.update_time_sheet_detail(timesheet, d, sales_invoice)
                timesheet.calculate_total_amounts()
                timesheet.calculate_percentage_billed()
                timesheet.flags.ignore_validate_update_after_submit = True
                timesheet.set_status()
                timesheet.db_update_all()
    ```
  - ``SalesInvoice.on_cancel`` (``:606``) calls ``self.unlink_sales_invoice_from_timesheets()`` at
    ``:657`` (``sales_invoice.py:765-770``), which calls Timesheet's own ``unlink_sales_invoice()``
    then repeats the identical ``ignore_validate_update_after_submit`` + ``db_update_all()``
    pattern.
  - ``db_update_all()`` (``frappe/model/base_document.py:849-851``, overridden
    ``frappe/model/document.py:2138-2145``) is self-documented: *"Raw update parent + children.
    DOES NOT VALIDATE AND CALL TRIGGERS."* Confirmed by direct read: it calls ``self.db_update()``
    THEN loops every non-computed table fieldname (``time_logs`` included) calling
    ``doc.db_update()`` on each child row too.

  **Honesty-grade placement, precisely: this is the Maintenance Visit ``db_update`` bypass grade
  (hooks/version/permission all skipped on an already-loaded document) but WIDER — a WHOLE-
  DOCUMENT raw write (parent row + every child row in one call) rather than one already-loaded
  document's own targeted field update, and the FIRST case in this campaign where a document
  rewrites ITS OWN submitted state from outside, driven entirely by a DIFFERENT doctype's own
  submit/cancel lifecycle rather than a sibling document being touched.** It fires on EITHER a
  Sales Invoice submit OR a Sales Invoice cancel that names this Timesheet — disclosed in BOTH
  directions (:func:`pacioli.tools._timesheet_submit_risk_flags`/
  ``_timesheet_cancel_risk_flags``): a submitted Timesheet's own ``status``/``per_billed``/
  ``total_billed_amount`` are never stable, and neither this broker's own submit/cancel tools NOR
  ERPNext's own ``on_update_after_submit`` path ever sees it happen.
* **"Payslip" — a live status option with ZERO in-tree writer, an unflagged cross-app
  dependency.** ``set_status()`` is the ONLY place in ``timesheet.py`` that assigns ``status``,
  and it never produces "Payslip." A full-tree grep of BOTH checkouts for any other write to
  Timesheet's own ``status`` finds nothing. Cause, confirmed: ``erpnext/patches/v14_0/
  remove_hr_and_payroll_modules.py`` — HR/Payroll (Salary Slip included) moved to the separate
  ``hrms`` app as of v14, outside both ref checkouts. Core Sales Invoice logic still READS for it:
  ``validate_time_sheets_are_submitted()`` (``sales_invoice.py:923-943`` — the addendum's own
  ``:923-935`` citation stops short of the actual check at ``:938-943``, corrected here) accepts
  ``status in ["Submitted", "Payslip", "Partially Billed"]``. Disclosed here so a future reader
  doesn't hunt these trees for a setter that isn't there.
* **company IS PRESENT BUT OPTIONAL — NOT a new posture.** ``company`` (Link -> Company,
  confirmed via ``json.load``) carries no ``reqd`` key. **This is the FOURTH such row, not the
  first** — Purchase Invoice and Quality Inspection both carry the identical shape (confirmed by
  direct ``json.load`` of both), and Asset Value Adjustment's own landing (the THIRTY-SEVENTH
  doctype) already named itself explicitly "the THIRD such row after Purchase Invoice/Quality
  Inspection." **The governance call stands regardless of the ordinal:** the mechanics are already
  correct. The 0fdf91d company guard in ``_locks_for`` (``if not company: return {}``) covers
  present-but-blank exactly like schema-absent — ERPNext-EQUAL, since a blank-company document
  can't be in any company's closed period. Timesheet is NOT added to the companyless tally (still
  6) and does NOT need ``REG_UNPINNED`` gating for the whole doctype — an instance WITH company
  set participates in closed-books checks normally, exactly like Asset Value Adjustment's own
  precedent. **Unlike Asset Value Adjustment, a blank company on Timesheet does NOT doom its own
  submit** — ``company`` is referenced exactly ONCE in ``timesheet.py`` (``make_sales_invoice``,
  line 426, copying it onto a NEW unsaved Sales Invoice), never read by ``validate()``/
  ``on_submit``/``validate_mandatory_fields`` — there is no synchronously-created sibling document
  requiring it the way Asset Value Adjustment's own Journal Entry does, so this doctype's blank-
  company posture has NO submit-time consequence of its own.
* **Cascade — genuinely NOT a leaf, the FIRST confirmed INCOMING child-table Link edge this
  campaign has found (Delivery Trip's own child-table edge was OUTGOING, the mirror direction).**
  A full two-checkout grep for ``'"options": "Timesheet"'`` returns exactly 2 hits: Timesheet's
  own self-referencing ``amended_from``, and ``Sales Invoice Timesheet.time_sheet``
  (``sales_invoice_timesheet.json``: ``fieldtype: "Link"``, ``istable: 1`` on the CHILD doctype
  embedded in submittable Sales Invoice, confirmed via ``json.load`` — a REAL Link, never a
  Dynamic Link). This broker's own ``get_submitted_linked_docs`` calls frappe's native
  ``frappe.desk.form.linked_with.get_submitted_linked_docs`` endpoint directly (already the
  standing mechanism — Purchase Receipt's own landing already established "ERPNext's own
  ``get_submitted_linked_docs`` walks this Link-field graph generically, child tables included").
  Confirmed from frappe's own source (``linked_with.py``): ``get_references_across_doctypes``'s
  own docstring states "Include child table, link and dynamic link references," and
  ``SubmittableDocumentTree.get_next_level_children`` passes ``get_parent_if_child_table_doc=True``
  to ``get_referencing_documents``, resolving each child row back to its submittable PARENT under
  a ``docstatus == 1`` filter. **Practical consequence: a submitted Sales Invoice naming this
  Timesheet in its own ``timesheets`` child table BLOCKS this broker's own leaf ``plan_cancel``
  outright** (the standing blast-radius gate fires, naming the Sales Invoice) — ``plan_cascade_
  cancel`` (cancelling the Sales Invoice first, then this Timesheet) is the governed path,
  disclosed at plan time. ``cascade.py`` needed NO changes — the same generic mechanism every
  prior real edge already rides, zero doctype-specific code.
* **Whitelist count: exactly 7 module-level, confirmed by a full grep of ``timesheet.py`` for
  ``@frappe.whitelist()``, matching the dossier's own §9 count:** ``get_projectwise_timesheet_
  data``/``get_timesheet_detail_rate``/``get_timesheet`` (also
  ``@frappe.validate_and_sanitize_search_inputs``)/``get_timesheet_data``/``get_activity_cost``/
  ``get_events`` (six, all read-only, confirmed no ``save``/``db_set``/``frappe.db.set_value``
  anywhere in any body) + ``make_sales_invoice`` (line 411-460 — builds a NEW unsaved Sales
  Invoice via ``frappe.new_doc``, only ``.append()``s rows and calls ``run_method`` on the NEW
  doc, never touches the source Timesheet's own fields — confirmed by full-body read). Nothing
  granted; no tool built toward any of them. Two further module-level functions
  (``get_timesheets_list``/``get_list_context``, lines 516-576) are NOT ``@frappe.whitelist()``-
  decorated — plain web-portal helpers, confirmed by their own absent decorator, worth a line
  since the dossier's own enumeration stopped at the 7 whitelisted ones.
* **``period_closing_doctypes``: Timesheet confirmed ABSENT** (``erpnext/hooks.py:326-345``, the
  same 18-entry list — no Timesheet entry). No async/enqueue/scheduler channel anywhere in
  ``timesheet.py`` — confirmed by a full-file grep for ``frappe.enqueue``/scheduler hooks/
  ``sendmail``, re-confirming the addendum's own full read.

Breadth (Contract) — the forty-third supported doctype, landed off a pre-verification addendum
(``docs/plans/dossiers/contract.verify.md``, 2026-07-21). Re-verified directly against
``/root/.pacioli/refs/erpnext-16/erpnext/crm/doctype/contract/contract.{json,py,js}`` (144-line
``contract.py``, 32-field ``contract.json`` via complete ``json.load`` enumeration) and
``erpnext/hooks.py`` (712 lines), plus ``frappe/model/document.py``/``frappe/database/
database.py`` — every load-bearing citation below independently re-verified by direct read/grep,
not inherited from the dossier or the addendum.

**The addendum's own four corrections, adopted as-is (each independently re-verified here):**

1. ``company`` confirmed ABSENT from the 32-field enumeration — the SEVENTH companyless doctype
   after Packing Slip/Supplier Scorecard Period/Shipment/Project Update/Asset Maintenance Log/
   Bank Guarantee. Two decoy fieldnames contain the substring "company": ``signed_by_company``
   (Link -> User, contract.py:62, stores the SUBMITTING USER, not a registry Company) and
   ``signee_company`` (Signature fieldtype, a signature-pad image) — neither is ever spliced into
   the ``company`` slot; ``REG_UNPINNED`` is the only registry shape this doctype can ever govern
   through.
2. ``period_closing_doctypes`` citation corrected: the real list is ``hooks.py:326-345`` (18
   entries, Contract absent), not the dossier's own ":473-490" — line 473 is actually where
   ``update_status_for_contracts`` is registered in ``scheduler_events["daily_maintenance"]``
   (the scheduler finding below); the conclusion (not period-closing-gated) was always right.
3. ``before_update_after_submit`` (contract.py:67-69) is missing from the dossier's own §2/§4/
   §6/§7 entirely. The class body carries 9 methods, counted directly (not the ~6 the dossier's
   prose implies): ``validate``, ``set_missing_values``, ``before_submit``, ``on_discard``,
   ``before_update_after_submit``, ``validate_dates``, ``update_contract_status``,
   ``update_fulfilment_status``, ``get_fulfilment_progress`` (contract.py:49-104).
4. The dossier enumerates all four date-shaped fields accurately but never pins ONE as
   ``date_field`` — the interrogation checklist requires the pin be "named loudly." See below.

**Two further corrections this landing found beyond the addendum's own four (re-verified against
the LIVE ``SUPPORTED_DOCTYPES`` table and both checkouts directly, never assumed from either
document's own framing):**

5. **The addendum frames ``signed_on`` as "the campaign's FIRST allow_on_submit date pin" — this
   is WRONG as literally stated.** A direct scan of every already-landed ``SUPPORTED_DOCTYPES``
   entry's own ``date_field`` against its schema shows TWO prior ``allow_on_submit: 1`` date pins
   already exist: Work Order's ``planned_start_date`` (``work_order.json``: ``allow_on_submit:
   1, reqd: 1, default: "now"``) and Shipment's ``pickup_date`` (``shipment.json``:
   ``allow_on_submit: 1, reqd: 1``). **The narrower, true claim: ``signed_on`` is the first
   ``allow_on_submit`` date pin that is ALSO schema-optional** (``reqd`` absent, confirmed in the
   32-field dump) — Work Order's and Shipment's own pins are both ``reqd: 1``, so neither can
   ever actually be blank at submit despite being editable after it. ``signed_on`` is the first
   case where the SAME pinned field can be genuinely blank at submit AND is explicitly blessed to
   move after submit.
6. **The addendum also frames "the broker refuses a native-legal blank date at execute" dynamic
   as new for this campaign — it is not.** Project Update's own landing (the thirtieth doctype)
   already found this exact shape first: a real declared ``date`` field, ``reqd: 0``, no default,
   zero server-side code ever sets it — its own docstring calls it "the first doctype in this
   campaign to combine both" a real date field and a genuinely blank draft. Asset Maintenance
   Log's landing (the thirty-first) found a STRONGER form: ``completion_date`` is
   VALIDATE()-ENFORCED blank for every submitted "Cancelled" log (validate() itself throws if a
   non-"Completed" status carries a set ``completion_date``). Contract is at best the THIRD
   doctype on this dynamic, not the first. What IS genuinely new: Contract is the first case
   where the blank-capable pinned field is SIMULTANEOUSLY ``allow_on_submit`` (point 5) — neither
   Project Update's ``date`` nor Asset Maintenance Log's ``completion_date`` carries
   ``allow_on_submit`` at all, so neither can move again after a successful submission the way
   ``signed_on`` can.

**Summary of what lands in this row:**

* **party_field=None (Dynamic Link pair) — confirmed.** ``party_type`` (Select,
  ``"Customer\nSupplier\nEmployee"``, ``reqd: 1``, ``default: "Customer"``) + ``party_name``
  (Dynamic Link, ``options: "party_type"``, ``reqd: 1``) — the Quotation/Quality Inspection
  shape, both header-level and both required. ``party_full_name`` (Data, read_only) is computed
  in ``set_missing_values()`` (contract.py:55-59: ``field = self.party_type.lower() + "_name"``,
  then ``frappe.db.get_value(self.party_type, self.party_name, field)`` — a live read of the
  referenced Customer/Supplier/Employee's own name field). ``party_user`` (Link -> User) is
  metadata only, never a GL party.
* **``status`` PRESENT AND DOUBLED; ``grand_total`` ABSENT with no substitute.** ``status``
  (Select, ``"Unsigned\nActive\nInactive\nCancelled"``, ``allow_on_submit: 1``, ``in_list_view:
  1``) + ``fulfilment_status`` (Select, ``"N/A\nUnfulfilled\nPartially Fulfilled\nFulfilled\
  nLapsed"``, ``allow_on_submit: 1``, ``in_list_view: 1``) — both LOOK directly writable
  post-submit (see THE WRITABILITY ILLUSION below for why that reads wrong). No ``Currency``/
  ``Float``/``Percent`` field named or shaped like ``grand_total`` anywhere in the 32-field
  enumeration, and no substitute either (unlike Stock Reconciliation's ``difference_amount``/Bank
  Guarantee's ``amount``) — Contract carries no aggregate of any kind. ``in_list_view`` is
  exactly 5 fields, confirmed by direct enumeration in the SAME order the dossier states:
  ``status``, ``fulfilment_status``, ``signed_on``, ``contract_terms`` (Text Editor),
  ``document_name`` (Dynamic Link).
* **``date_field="signed_on"`` — THE PIN, made and documented here (the dossier left it open).**
  Four Date/Datetime-shaped fields exist: ``start_date``/``end_date`` (both Date, neither
  ``reqd``, drive ``update_contract_status()``'s own window check but are never
  ``allow_on_submit`` — frozen the instant this document submits), ``fulfilment_deadline`` (Date,
  feeds ``update_fulfilment_status()``'s own "Lapsed" branch only), and ``signed_on`` (Datetime,
  ``allow_on_submit: 1``, no ``reqd``, no schema default, and — confirmed by a full-file grep of
  BOTH ``contract.py`` AND ``contract.js`` — NEVER SET BY ANY CODE ANYWHERE, server or client; a
  pure, manually-entered field). ``signed_on`` is the pin: the one field representing WHEN the
  transaction (the actual signature event) happened — the same "real transactional date, not a
  scheduling target" test every prior date-field choice in this campaign has followed.
  ``start_date``/``end_date`` describe the AGREEMENT's own validity window instead, a different
  concept (a contract can be signed today and take effect next quarter). Consequences, stated
  loudly per the interrogation checklist:

  - The Datetime shape needs the existing ``_posting_date_of`` truncate-at-real-separator
    projection (Work Order/Asset Movement precedent) — reused unchanged, zero new plan.py/
    tools.py code.
  - Schema-optional and genuinely, verifiably blank-capable on a submitted Contract (point 6
    above) — NOT forced non-blank by any other code path, unlike Budget's own
    ``budget_start_date`` (validate()-forced via the reqd fiscal-year Links) or Timesheet's own
    ``start_date`` (recomputed by ``set_dates()`` every ``validate()``). A blank ``signed_on`` at
    plan time is DISCLOSED (Envelope E6, plan still returns ``ok: True``) and REFUSED at execute
    (``check_red_line``'s own "no posting_date" branch, the SAME machinery Project Update's and
    Asset Maintenance Log's own landings already proved correct, zero new code) — **this broker
    is STRICTER than ERPNext here**: a real, valid (per ERPNext) Contract submission with a blank
    ``signed_on`` will be refused by this broker's own governed submit, even though ERPNext
    itself has no code path that would ever refuse it.
  - Because ``signed_on`` is ``allow_on_submit: 1`` and no hook (``before_update_after_submit``
    included) ever recomputes it, a caller CAN change it on a later save of an already-submitted
    Contract — the pinned date can genuinely MOVE post-submit (point 5 above). Since Contract is
    companyless, this has ZERO practical closed-books consequence today (``_locks_for`` returns
    ``{}`` unconditionally for a falsy ``company``, before the date is even consulted for a lock)
    — the reasoning is stated here in full rather than assumed, per the interrogation checklist.
* **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed, dossier correct as far as it goes.** No
  ``def submit``/``def cancel`` override anywhere in the 144-line file (grep-clean); the 9-method
  class body (correction 3 above) shadows neither ``Document.submit`` nor ``.cancel``. The
  dossier's gap was the missed hook (correction 3), never the transport call itself.
* **THE WRITABILITY ILLUSION — a first for this campaign.** ``status``/``fulfilment_status``/
  ``is_signed`` all carry ``allow_on_submit: 1`` and LOOK directly settable through a governed
  post-submit write — they are not, in practice. ``before_update_after_submit``
  (contract.py:67-69) fires on every save of an already-submitted Contract (Frappe's own
  ``check_docstatus_transition``, ``frappe/model/document.py:1135-1138``: once ``docstatus`` is
  already SUBMITTED and stays SUBMITTED across a save, ``_action = "update_after_submit"``; then
  ``run_before_save_methods``, ``document.py:1410-1411``: ``elif self._action ==
  "update_after_submit": self.run_method("before_update_after_submit")`` — this fires on ANY save
  while ``docstatus`` stays 1, not gated on an ``allow_on_submit`` field actually changing) and
  unconditionally re-runs ``update_contract_status()`` + ``update_fulfilment_status()``, which
  RECOMPUTE both fields from ``start_date``/``end_date``/``is_signed``/fulfilment-term state —
  discarding whatever a caller just wrote into either field directly on the very next save.
  ``is_signed`` (Check, ``allow_on_submit: 1``) is the genuine live lever: toggling it is what
  actually changes the recomputed ``status`` (``update_contract_status``: ``"Unsigned"`` if
  falsy, else ``get_status(start_date, end_date)``) — a direct write to ``status``/
  ``fulfilment_status`` themselves never sticks past the next save.
* **THE SCHEDULER — the campaign's FIRST CONFIRMED SUBMITTED-STATE SCHEDULER MUTATOR.**
  ``update_status_for_contracts()`` (contract.py:129-144), registered in
  ``scheduler_events["daily_maintenance"]`` (``hooks.py:473`` — not the literal ``"daily"`` key,
  an empty list at ``hooks.py:460``; ``daily_maintenance`` still runs once a day, a naming
  precision, never a cadence error). Filters ``is_signed=True`` AND ``docstatus=1`` EXPLICITLY
  (contract.py:137) — the mirror image of Asset Maintenance Log's own scheduler
  (``asset_maintenance_log.py:80-88``, no ``docstatus`` filter at all in its ``frappe.qb`` WHERE,
  but its own ``on_submit`` refuses submission unless already Completed/Cancelled, so its real
  blast radius turned out DRAFT-ONLY in practice, ``erpnext.py:3576-3583``). Contract carries NO
  analogous gate anywhere in its 144 lines — nothing stops submitting a signed Contract — so this
  scheduler genuinely, deliberately, and exclusively targets SUBMITTED contracts and NEVER
  touches drafts (a draft with ``is_signed=True`` would need ``docstatus=1`` to match the filter,
  i.e. it would already be submitted). Writes via ``frappe.db.set_value("Contract", name,
  "status", status)`` (``frappe/database/database.py:934-945``, once per matching contract in a
  Python loop — N point-writes, never one bulk statement) — the method's own docstring: "do not
  call the ORM triggers... will not call Document events and should be avoided in normal cases."
  No document load, no ``validate()``/``before_update_after_submit``/permission check/version
  entry despite ``contract.json``'s own ``"track_changes": 1``. Only ever flips between
  ``"Active"``/``"Inactive"`` (``get_status``, contract.py:107-126: no ``end_date`` -> "Active";
  otherwise a straight date-range compare) — never writes ``"Unsigned"`` (filter requires
  ``is_signed=True``) or ``"Cancelled"``.
* **THE STATUS/DOCSTATUS DISCONNECT.** Tracing every code path that can ever write
  ``status="Cancelled"`` finds exactly ONE: ``on_discard()`` (contract.py:64-65,
  ``self.db_set("status", "Cancelled")``) — and Frappe's own ``discard()``
  (``frappe/model/document.py:1349-1362``) hard-guards ``if not self.docstatus.is_draft(): raise
  ...`` (line 1357-1358), so this path can NEVER touch a submitted (``docstatus=1``) Contract; it
  is also unreachable through this broker's own governed verb surface (no discard tool is
  exposed, the same posture every prior doctype's own ``on_discard`` finding has taken). **A
  submitted Contract's own ``status`` field can therefore only ever cycle Active <-> Inactive (or
  sit at "Unsigned" if never signed) for the rest of its life — "Cancelled" is a schema-declared
  4th option (``contract.json``'s own ``status`` Select) that is STRUCTURALLY UNREACHABLE
  post-submit.** A normal governed ``cancel()`` (``docstatus`` 1 -> 2) never touches the
  ``status`` Select at all (no ``on_cancel`` override defined) — a cancelled-``docstatus``
  Contract keeps DISPLAYING whatever Active/Inactive/Unsigned value it last computed, forever.
  ``docstatus`` cancellation and the ``status`` field's own "Cancelled" option are two entirely
  disconnected concepts, named here for the cancel-direction disclosure.
* **Ledger preview: UNCALLABLE, confirmed.** ``class Contract(Document):`` (contract.py:11) — a
  direct subclass, never ``AccountsController``/``StockController``. No ``make_gl_entries``
  anywhere in the file (grep-clean, all 144 lines). Joins the skip tuple as its TWENTY-SEVENTH
  member (the addendum's own "22nd member" tally was already stale, written before four more
  landings; counted live here: 26 members as of Timesheet's landing, Contract is the 27th).
* **Cascade: genuine LEAF, confirmed by a full-tree grep of BOTH checkouts.**
  ``grep -rn '"options": "Contract"' erpnext-16 frappe-16`` returns exactly ONE hit —
  ``contract.json:229``, the doctype's own self-referencing ``amended_from``. No other DocType
  anywhere in either tree links to Contract. ``cascade.py`` needs no changes.
* **Whitelist count: 0, confirmed — NOT a first.** No ``@frappe.whitelist()`` anywhere in
  ``contract.py`` (grep-clean, all 144 lines); ``contract.js``'s one ``frappe.call`` targets a
  DIFFERENT doctype's own module (``contract_template.get_contract_template``). A zero-whitelist
  count has already landed at least five times before this row (Cost Center Allocation, Supplier
  Scorecard Period, Installation Note, Maintenance Visit, Asset Movement, each confirmed by their
  own landing's own "ZERO whitelist callables" finding) — nothing to withhold, nothing novel
  about the count itself.
* **``period_closing_doctypes``: Contract confirmed ABSENT** (``hooks.py:326-345``, the correct
  citation — correction 2 above). No async/enqueue channel anywhere in ``contract.py`` beyond the
  daily scheduler already disclosed above.

Breadth (Pick List) — the forty-fourth supported doctype, landed off a pre-verification addendum
(``docs/plans/dossiers/pick_list.verify.md``, 2026-07-21). Re-verified directly against
``/root/.pacioli/refs/erpnext-16/erpnext/stock/doctype/pick_list/pick_list.{json,py}`` (35-field
``pick_list.json`` + 1893-line ``pick_list.py``, complete ``json.load``/``grep -n`` enumeration),
``erpnext/stock/doctype/pick_list_item/pick_list_item.json`` (37-field child table),
``erpnext/utilities/transaction_base.py``, ``erpnext/controllers/status_updater.py``,
``erpnext/hooks.py`` (712 lines), ``erpnext/stock/doctype/delivery_note_item/
delivery_note_item.json``, ``erpnext/accounts/doctype/sales_invoice_item/
sales_invoice_item.json``, ``erpnext/stock/doctype/stock_entry/stock_entry.json``, and frappe's own
``frappe/desk/form/linked_with.py`` — every load-bearing citation below independently re-verified
by direct read/grep, never inherited from the dossier, the addendum, or the landing briefing.

**THE CASCADE QUESTION (Supervisor Ruling 1) — the row's decisive fact, and both the dossier AND
the addendum got it WRONG.** Both concluded Pick List is a cascade LEAF, reasoning that Delivery
Note Item/Sales Invoice Item's own ``against_pick_list`` Link fields "never appear in a
submitted-links blast-radius read" because the CHILD doctype (Delivery Note Item / Sales Invoice
Item) is not itself submittable. This is the wrong test. Confirmed by direct ``json.load`` of both
PARENT doctypes: ``delivery_note.json``'s own ``is_submittable`` is ``1``; ``sales_invoice.json``'s
own ``is_submittable`` is ``1`` — both are genuinely submittable, and ``against_pick_list``
(``delivery_note_item.json``/``sales_invoice_item.json``, both ``fieldtype: "Link", "options":
"Pick List"``) lives on their own CHILD TABLES (``istable: 1``). Frappe's own
``frappe/desk/form/linked_with.py`` mechanism — re-read directly for this landing, not assumed —
resolves exactly this shape: ``get_references_across_doctypes`` marks each link
``is_child = link["doctype"] in all_child_tables`` (line 225), and
``SubmittableDocumentTree.get_next_level_children`` calls ``get_referencing_documents`` with
``get_parent_if_child_table_doc=True`` (``linked_with.py:121``), which — for a child-table hit —
groups the matching rows by ``parenttype`` and re-queries THAT parent doctype under
``parent_filters=[("docstatus", "=", 1)]`` (``linked_with.py:356-364``), restricted to
``allowed_parents`` (every submittable doctype). This is the EXACT mechanism this morning's
Timesheet landing already proved live (Sales Invoice Timesheet.time_sheet, also a child-table
Link under a submittable parent) — not a new question, a repeat of an already-settled one.
**Practical consequence: a submitted Delivery Note or Sales Invoice holding ``against_pick_list``
rows against this Pick List WILL surface in ``get_submitted_linked_docs`` and block a leaf
``plan_cancel``.** Beyond the DN/SI child-table edges, **Stock Entry.pick_list is a THIRD,
simpler, independent edge** — a direct HEADER-LEVEL ``Link`` field (confirmed,
``stock_entry.json``: ``{"fieldname": "pick_list", "fieldtype": "Link", "options": "Pick List"}``)
on Stock Entry, itself confirmed ``is_submittable: 1`` — the plainest possible non-leaf shape,
needing no child-table resolution step at all. The dossier's own cascade table (§8) even lists
this exact row (``Stock Entry | pick_list | 1 | ...``) but its own prose conclusion never draws
the conclusion its own table forces. **Pick List is NOT a cascade leaf, on three independent,
confirmed counts** (a full ``grep -rn '"options": "Pick List"'`` across BOTH checkouts returns
EXACTLY 4 hits total: Stock Entry.pick_list, Delivery Note Item.against_pick_list, Sales Invoice
Item.against_pick_list, and Pick List's own self-referencing ``amended_from`` — frappe-16 itself
contributes zero hits). ``cascade.py`` needs no changes — the same generic, doctype-blind
mechanism every prior real edge in this campaign already rides; ``plan_cascade_cancel`` (any of
the three dependents first, this Pick List last) is the governed path once a real dependent
exists, disclosed at plan time exactly like Timesheet's/Payment Order's own non-leaf rows.

**Corrections beyond the cascade question:**

1. **MRO — the addendum's own correction, re-verified.** The dossier's header/§5 states
   ``PickList -> TransactionBase -> Document`` (missing a level). Confirmed from source:
   ``class TransactionBase(StatusUpdater):`` (``erpnext/utilities/transaction_base.py:20``) and
   ``class StatusUpdater(Document):`` (``erpnext/controllers/status_updater.py:181``) — the real
   chain is ``PickList -> TransactionBase -> StatusUpdater -> Document``, 4-deep.
   ``update_prevdoc_status()`` (called at ``on_submit:286``/``on_cancel:364``) is defined in
   **StatusUpdater** (``status_updater.py:193``), not TransactionBase — matching the citation
   shape this campaign's own skip-tuple comments already use for Installation Note/Maintenance
   Visit/Maintenance Schedule. Pick List is the **FOURTH** doctype of this exact MRO shape, not a
   first.
2. **"submit throws (2)" — the dossier's own closing verification line — undercounts by roughly
   6x, exactly as the addendum found, independently re-derived here rather than inherited.**
   Frappe's ``Document._submit()`` sets ``docstatus = SUBMITTED`` THEN calls ``self.save()``
   (``frappe/model/document.py:1319-1322``) — the FULL ``validate()`` + ``before_save()`` gate set
   fires again on every real submit, not just ``before_submit()``. A full re-derivation across
   all four hook stages (``validate``, ``before_save``, ``before_submit``, ``on_submit``) finds
   **exactly 12** distinct ``frappe.throw`` call-sites reachable during a real submit (every
   ``frappe.throw`` in ``pick_list.py`` grepped and individually traced to its calling method):

   - **FOUR are genuinely deterministic-from-draft** (readable straight off this draft's own
     fields, the template's own bar for a gated risk flag):
     ``validate_for_qty()`` (``:705-709``, ``validate()`` — ``purpose == "Material Transfer for
     Manufacture"`` and ``for_qty`` falsy); ``validate_warehouses()``'s own missing-warehouse
     branch (``:171-178``, ``before_save()``, gated ``if self.get("locations")`` — any row with a
     blank ``warehouse``); ``validate_picked_items()`` (``:265-273``, ``before_submit()`` —
     ``scan_mode`` set and a row's ``picked_qty < stock_qty``); and the ``on_submit``-called
     ``make_bundle_using_old_serial_batch_fields()``'s own throw (``:318-323`` — a row carries
     ``serial_no``/``batch_no`` but ``use_serial_batch_fields`` is falsy) — this LAST one is the
     addendum's own sharpest find: the dossier's §6 quotes this exact call as a pure side effect
     with zero mention it can abort the submit.
   - **EIGHT need live cross-document/external state, stay prose per the standing rule**:
     ``validate_stock_qty()``'s own two throws (``:139-151`` batch qty vs live
     ``get_batch_qty()``; ``:164-169`` bin qty vs live ``Bin.actual_qty``, both ``validate()``);
     ``check_serial_no_status()`` (``:212-217``, ``validate()``, live Serial No records);
     ``validate_expired_batches()`` (``:310-313``, ``validate()``, live ``Batch.expiry_date`` vs
     today); ``validate_warehouses()``'s own company-mismatch branch (``:183-193``,
     ``before_save()``, each row's live ``Warehouse.company``); ``validate_sales_order_percentage()``
     (``:238-240``, ``before_save()``, the referenced Sales Order's own live ``per_picked``);
     ``validate_sales_order()`` (``:259-263``, ``before_submit()``, gated ``purpose == "Delivery"``
     — a referenced Sales Order Item's live ``stock_reserved_qty``); and ``validate_picked_qty()``
     (``:544-549``, reached from ``on_submit`` via ``update_reference_qty()`` ->
     ``update_packed_items_qty()``/``update_sales_order_item_qty()`` — needs Stock Settings' own
     live ``over_delivery_receipt_allowance`` PLUS the live cumulative picked/stock qty across
     every OTHER submitted Pick List referencing the same Sales Order Item/Packed Item).
3. **Dateless tally corrected — the addendum's own "third" is STALE, not this landing's error.**
   BOM Creator independently took THIRD ``NO_DATE_FIELD`` member status this same morning
   (``2d2a445``) — Pick List is the live **FOURTH** (BOM, Packing Slip, BOM Creator, Pick List),
   confirmed by a fresh count of :data:`SUPPORTED_DOCTYPES`'s own dateless entries plus this
   doctype's own complete field-type scan: zero ``Date``/``Datetime`` fields among the 35 parent
   (``pick_list.json``) AND zero among the 37 child (``pick_list_item.json``) fields — a
   parent-only scan would have been a real gap for a "full enumeration" charge; both tables are
   independently confirmed dateless here.
4. **Skip-tuple tally corrected — the addendum's own "22nd" is STALE**, written before four more
   landings (Budget/Timesheet/Contract) each joined the tuple. Live count: 27 members after
   Contract; Pick List is the **28th**.
5. **List-tier tally — the addendum left this genuinely open; resolved here by direct count.** A
   fresh count of ``if doctype ==`` code lines in ``_list_fields`` (excluding two backtick-quoted
   mentions inside this same function's own docstring prose, which are not code) finds **33**
   special branches after Contract, confirming Contract's own "33rd" framing — Pick List forces
   its own **34th**, detailed below.
6. **Minor (addendum's own footnote, re-confirmed):** ``material_request``'s own ``depends_on``
   (``eval:['Material Transfer', 'Material Issue'].includes(doc.purpose)``) references a
   ``"Material Issue"`` value that does not exist on the real schema — ``purpose``'s own Select
   options are confirmed exactly ``"Material Transfer for Manufacture\nMaterial Transfer\
   nDelivery"`` (3 values, ``json.load``-verified). A dead branch in ERPNext's own client-script
   logic, not a governance concern, noted so a future reader doesn't treat it as a real fourth
   ``purpose`` value.

**Summary of what lands in this row:**

* **party_field="customer" — a plain, real, always-queryable header Link, confirmed present on
  every draft regardless of form display.** ``customer`` (Link -> Customer, ``in_list_view: 1``,
  no ``reqd``, ``depends_on: eval:doc.purpose==='Delivery'``) — a genuinely DIFFERENT shape from
  every prior "conditional party" row this campaign has landed (Blanket Order/Bank
  Guarantee/Payment Order/Share Transfer), each of which conditions on WHICH field holds the real
  party (``party_field=None``, splicing the live field by literal name). Here ``depends_on`` is a
  pure Desk-form DISPLAY directive — it changes nothing about the schema, the database column, or
  this broker's own list/validation logic; ``customer`` is an ordinary, always-real
  ``party_field`` the same as Sales Order's/Delivery Note's own, merely form-hidden outside
  ``purpose == "Delivery"``.
* **``status`` PRESENT; ``grand_total`` ABSENT with no substitute.** ``status`` (Select,
  ``reqd: 1``, ``hidden: 1``, ``read_only: 1``, default ``"Draft"``, options ``"Draft\nOpen\
  nPartly Delivered\nPartially Transferred\nCompleted\nCancelled"``) is genuinely real and
  queryable despite being form-hidden — confirmed live in ``pick_list_list.js`` (ERPNext's own
  list-view controller): ``get_indicator`` reads ``doc.status`` directly for a color-coded
  indicator, the SAME mechanism Delivery Trip's own landing already established generalizes (a
  real field's list-tier inclusion is not gated on its ``in_list_view`` flag). Pick List's own
  ``status`` goes one step further than Delivery Trip's (which is un-flagged but NOT hidden):
  this is a real field that is BOTH ``hidden: 1`` AND un-flagged ``in_list_view`` — a distinct
  combination from Contract's own status (also ``hidden: 1``, but ALSO ``in_list_view: 1``).
  ``delivery_status``/``per_delivered`` are both real but neither carries ``in_list_view`` either
  — confirmed absent, never spliced. No ``Currency``/``Float``/``Percent`` field anywhere among
  the 35 fields is named or shaped like a total; ``in_list_view`` is confirmed EXACTLY ``{company,
  customer}`` — no substitute exists for the missing ``grand_total``.
* **DATELESS — the FOURTH ``NO_DATE_FIELD`` member (correction 3 above).** Zero Date/Datetime
  fields across all 35 parent + 37 child fields; absent from ``period_closing_doctypes``
  (``hooks.py:326-345``, the correct 18-entry list, confirmed).
* **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed.** No ``def submit``/``def cancel`` override
  anywhere in the 1893-line file (grep-clean); ``before_submit``/``on_submit``/``on_cancel``/
  ``on_update_after_submit`` are ordinary lifecycle hooks, never shadowed overrides.
* **Ledger preview: UNCALLABLE, confirmed.** ``class PickList(TransactionBase):`` — no
  ``make_gl_entries`` anywhere in ``pick_list.py`` (grep-clean, all 1893 lines). Joins the skip
  tuple as its **TWENTY-EIGHTH** member (correction 4 above), MRO cited 4-deep per correction 1.
  Pick List references Stock Entries (which DO post GL) but never posts one itself — order/
  reference layer only.
* **THE TWO-JAW TRAP (both jaws disclosed together, per the landing briefing).** Jaw one:
  ``on_update_after_submit()`` (``:345-350``) throws if ``has_reserved_stock()`` (``:915-925``) is
  true — 100% deterministic from this draft's own ``locations`` rows (``purpose == "Delivery"``
  and any row with ``stock_reserved_qty > 0``) — a clean, draft-readable "can this document still
  be edited" flag. Jaw two: ``on_cancel()`` (``:352-364``) carries ZERO ``frappe.throw`` calls
  (grep-clean) and its own ``ignore_linked_doctypes`` (``:353-357``) explicitly PRESERVES
  ``Serial and Batch Bundle``/``Stock Reservation Entry``/``Delivery Note`` — cancel always
  succeeds and never auto-cancels any of the three. Together: once a Pick List carries reserved
  stock, it CANNOT be edited further (jaw one) but CAN still be cancelled, orphaning any live
  Stock Reservation Entries against a now-cancelled parent (jaw two) — a materially different
  governance story than either jaw disclosed alone (the dossier's own RED FLAG #3 names only jaw
  two).
* **THE RESERVATION MACHINERY — a NEW cross-document mutation honesty grade, confirmed exactly
  as the addendum traced it.** ``create_stock_reservation_entries()`` (``:499-524``, whitelisted,
  callable on a submitted Pick List) is a 2-hop delegation: builds a per-Sales-Order
  ``items_details`` dict from this draft's own ``locations``, then calls
  ``frappe.get_doc("Sales Order", so).create_stock_reservation_entries(...)``
  (``sales_order.py:834-851``), which itself re-delegates a THIRD hop to
  ``stock_reservation_entry.create_stock_reservation_entries_for_so_items``
  (``stock_reservation_entry.py:1575+``) — the function that actually mints the Stock Reservation
  Entry document(s). That function is typed ``from_voucher_type: Literal["Pick List", "Purchase
  Receipt"]`` (confirmed) — **Purchase Receipt shares this EXACT delegation path TODAY**, not a
  future pairing; a future Purchase Receipt/Subcontracting Order landing must pin its own
  ``create_stock_reservation_entries``/``cancel_stock_reservation_entries`` surface under the SAME
  governance decision, not a fresh one. ``cancel_stock_reservation_entries()`` (``:526-536``) is a
  single-hop delegation to the module-level function of the same name. Per the landing briefing's
  own vocabulary (clean ``.save()`` = Maintenance Schedule; ``db_update`` bypass = Maintenance
  Visit; raw SQL into another table = Quality Inspection): Pick List's shape is a **FOURTH grade**
  — multi-hop delegated creation through ANOTHER doctype's own method chain, never a direct insert
  in ``pick_list.py``'s own module. Nothing is granted toward either whitelisted method (see
  whitelist count below).
* **Whitelist count: exactly 10, confirmed** (3 instance + 7 module, matching both the dossier's
  and the addendum's own count byte-for-byte) — ``create_stock_reservation_entries``/
  ``cancel_stock_reservation_entries``/``set_item_locations`` (instance, all callable on a
  submitted doc) + ``create_delivery_note``/``create_delivery``/``create_dn_for_pick_lists``/
  ``create_stock_entry``/``get_pending_work_orders``/``get_item_details``/``get_pick_list_query``
  (module). Nothing granted toward any of them.
* **Cascade: genuinely NOT a leaf, on three independent counts (see THE CASCADE QUESTION above).**
* **``period_closing_doctypes``: Pick List confirmed ABSENT** (``hooks.py:326-345``). No
  ``frappe.enqueue()``/scheduler reference anywhere in ``pick_list.py`` (full-file grep, zero
  hits) — confirmed complete, matching both dossier and addendum.

Breadth (Asset Repair) — the forty-fifth supported doctype, landed off a pre-verification
addendum (``docs/plans/dossiers/asset_repair.verify.md``, 2026-07-21). Re-verified directly
against ``/root/.pacioli/refs/erpnext-16/erpnext/assets/doctype/asset_repair/asset_repair.py``
(609 lines) and ``asset_repair.json`` (37 fields, complete ``json.load`` enumeration), plus
``erpnext/assets/doctype/asset/asset.py`` (``set_status``/``get_status``), ``erpnext/stock/
doctype/stock_entry/stock_entry.json``, ``erpnext/hooks.py`` (``period_closing_doctypes``),
``erpnext/controllers/accounts_controller.py``, ``erpnext/utilities/transaction_base.py``,
``erpnext/controllers/status_updater.py``, and ``frappe/model/document.py`` (``_save``/
``run_before_save_methods``/``run_post_save_methods`` — every load-bearing citation below
independently re-verified by direct read/grep, never inherited from the dossier, the addendum, or
the landing briefing.

**THE SHARPEST CORRECTION — the addendum's own Correction #1/Landing Risk #2 overclaims: the
Asset-status raw-write does NOT fire on cancel.** The addendum states the ``update_status()``
side-write "fires on a plain draft save, on the governed submit, and on cancel alike" because
``_submit()``/``_cancel()`` (``frappe/model/document.py:1319-1327``) both call ``self.save()``.
True as far as it goes, but ``save()`` -> ``_save()`` does NOT treat every action identically:
``_save()`` calls ``self.run_before_save_methods()`` (``document.py:587``), and THAT method's own
body (``document.py:1402-1409``) branches on ``self._action``: ``if self._action == "save":
self.run_method("validate")`` / ``elif self._action == "submit": self.run_method("validate")`` /
``elif self._action == "cancel": self.run_method("before_cancel")`` — **no ``"validate"`` call in
the cancel branch at all.** ``AssetRepair.validate()`` (asset_repair.py:61-70, which calls
``update_status()`` at line 66) is reachable ONLY through ``run_method("validate")`` — so it never
executes during a cancel. Confirmed independently for the record: ``AssetRepair.on_cancel``
(:222-233) itself never calls ``self.validate()`` or ``self.update_status()`` either. The
practical shape: this side-write DOES fire on this broker's own governed **submit** (every real
submit reaches ``_action == "submit"`` -> ``run_method("validate")``), and does **NOT** fire on
this broker's own governed **cancel** — disclosed asymmetrically, in each direction's own risk-flag
function, never the addendum's "alike" framing.

Confirmed from source (2026-07-21 checkout):

* **``party_field=None`` — confirmed by enumerating all 37 fields** (``json.load``-counted,
  matching both the dossier's and the addendum's own count). ``asset`` (Link -> Asset, ``reqd:
  1``, ``in_list_view: 1``) is a fixed-asset reference, never a GL party; ``company``/
  ``cost_center``/``project`` are operational/accounting-dimension fields, not parties. No Dynamic
  Link pair anywhere.
* **``status``="repair_status" PRESENT (Select, default "Pending", 3 options: Pending/Completed/
  Cancelled — confirmed via ``json.load``, exact string match); ``grand_total`` ABSENT, stand-in
  ``total_repair_cost``** (Currency, calculated at ``calculate_total_repair_cost``,
  asset_repair.py:199-200 — ``repair_cost + consumed_items_cost``, never renamed into the slot).
  ``in_list_view`` confirmed EXACTLY ``{asset, downtime}`` (``json.load``-filtered) — ``downtime``
  is a read-only ``Data`` field (hours, computed by the whitelisted ``get_downtime``), not a
  Date/Datetime type despite its name.
* **``date_field="completion_date"`` — the addendum's own pin, re-verified and LANDED.** GL
  entries stamp ``posting_date: self.completion_date`` at FOUR stamp sites across two methods
  (``get_gl_entries_for_repair_cost``: asset_repair.py:347/364, the credit/debit pair;
  ``get_gl_entries_for_consumed_items``: :403/:420, the per-consumed-item credit row and its
  fixed-asset debit counterpart — the addendum's own "three call sites" count missed :420,
  caught by the supervisor's grep) — the GL-posting-date law this campaign has followed since Asset's own
  ``available_for_use_date`` landing: the pin is the field GL actually posts under, never merely
  the ``reqd`` one. ``failure_date`` (Datetime, unconditionally ``reqd: 1``, no default) is real
  and required on every draft, but it is NOT what GL reads — picking it would misalign
  period-lock/freezing-date checks against a date the books never actually post under, a
  governance-correctness bug the addendum flagged correctly (its own Correction #2). Asset Repair
  can in fact NEVER be schema-dateless (the addendum's own correction to the dossier's wrong
  "DATELESS when neither filled" framing) — ``failure_date``'s unconditional ``reqd`` guarantees a
  value on every persisted document; ``date_field=None``/``NO_DATE_FIELD`` never applies here.
  ``completion_date`` itself: Datetime, no default, ``"mandatory_depends_on": "eval:doc.
  repair_status==\"Completed\""`` (asset_repair.json — confirmed) — genuinely schema-optional
  outside that one condition, needing the existing ``_posting_date_of`` Datetime->date projection
  (Work Order/Asset Movement/Contract precedent, reused unchanged). **"completion_date" REJOINS an
  existing fieldname as its SECOND member, by fieldname only — a nominal collision, not a shared
  pattern:** Asset Maintenance Log's own ``completion_date`` (its own THIRTEENTH date pattern) is a
  plain ``Date`` field with no ``mandatory_depends_on`` (``asset_maintenance_log.json``,
  re-confirmed: ``{"fieldname": "completion_date", "fieldtype": "Date", "in_list_view": 1}`` — no
  ``reqd`` key at all); Asset Repair's own copy is ``Datetime`` and conditionally mandatory — the
  SAME "fieldname collision across a type/shape difference" this campaign has already proven safe
  by construction (Asset Movement's own ``transaction_date`` rejoining a Date-typed set as a
  Datetime; the projection keys on the VALUE's own shape at read time, never on the fieldname
  string). **Landing risk #1 (the addendum's sharpest, LANDED as a named test):** ``completion_
  date``'s conditional mandate + ``check_repair_status`` (asset_repair.py:238-240) blocking submit
  ONLY for ``repair_status == "Pending"`` together allow a genuinely SUBMITTED Asset Repair with
  ``repair_status == "Cancelled"`` and ``completion_date`` truly empty — a state none of the prior
  three Datetime-dated doctypes (all unconditionally ``reqd``) could ever reach. The existing
  machinery already handles it safely and by construction, zero new code: ``_posting_date_of``
  reads the empty field as ``""``, ``check_red_line`` refuses it as "no posting_date" — the SAME
  "unverifiable, never dateless" rule Contract's own genuinely-blank ``signed_on`` already proved.
  **Period closing: Asset Repair IS in ``hooks.py``'s ``period_closing_doctypes`` list (line
  339, confirmed by counting the literal 18-entry list)** — natively EQUAL to the broker's own
  check, the Asset precedent, not stricter.
* **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by a full class-body grep of
  ``asset_repair.py``: 28 defs, zero named ``submit``/``cancel``** (the addendum's own count,
  re-verified: ``validate``, ``validate_asset``, ``validate_dates``, ``validate_purchase_
  invoices``, ``validate_duplicate_purchase_invoices``, ``validate_purchase_invoice_status``,
  ``validate_expense_account``, ``validate_purchase_invoice_repair_cost``, ``update_status``,
  ``calculate_consumed_items_cost``, ``calculate_repair_cost``, ``calculate_total_repair_cost``,
  ``on_submit``, ``cancel_sabb``, ``on_cancel``, ``after_delete``, ``check_repair_status``,
  ``update_asset_value``, ``get_total_value_of_stock_consumed``, ``decrease_stock_quantity``,
  ``validate_serial_no``, ``make_gl_entries``, ``get_gl_entries``, ``get_gl_entries_for_repair_
  cost``, ``get_gl_entries_for_consumed_items``, ``set_increase_in_asset_life``, ``get_
  depreciation_note``, ``add_asset_activity``); ``on_submit``/``on_cancel``/``validate`` are
  ordinary lifecycle hooks, never shadowed overrides.
* **THE MRO CORRECTION (the addendum's own, re-verified) — ``StockController``/
  ``DocumentController`` are FABRICATED, never in ``AssetRepair``'s ancestry.** Read directly:
  ``class AccountsController(TransactionBase):`` (accounts_controller.py:105); ``class
  TransactionBase(StatusUpdater):`` (transaction_base.py:20); ``class StatusUpdater(Document):``
  (status_updater.py:181). Real chain, 4-deep: ``AssetRepair -> AccountsController ->
  TransactionBase -> StatusUpdater -> frappe.model.document.Document`` — ``DocumentController`` is
  not a real class name anywhere in either checkout; ``StockController`` is a sibling controller
  (Stock Entry/Purchase Receipt/etc.), never an ancestor of ``AccountsController``.
* **THE LEDGER-PREVIEW FINDING — CALLABLE, the Asset precedent, NOT the skip tuple.**
  ``make_gl_entries`` (asset_repair.py:312-318) is ``AssetRepair``'s OWN method, unambiguous either
  way the MRO reads. ``projected_gl`` is honest-empty for either of TWO independent, data-driven
  causes, disclosed per cause by :func:`pacioli.tools._asset_repair_submit_risk_flags` (the Asset
  precedent's own "honest-empty, disclosed per cause" shape): (1) ``capitalize_repair_cost`` falsy
  — the FIRST gate (asset_repair.py:205) — no GL at all, and ``update_asset_value``/``set_
  increase_in_asset_life``/``reschedule_depreciation``/``add_asset_activity`` are skipped too
  (the whole ``on_submit`` block is gated on this one flag); (2) ``capitalize_repair_cost`` truthy
  but ``total_repair_cost <= 0`` — the SECOND, independent gate inside ``make_gl_entries`` itself
  (``if flt(self.total_repair_cost) > 0:``, asset_repair.py:316) — the asset-value/depreciation-
  life side effects above already ran, but GL still posts nothing; a real split between
  "capitalized" and "posted." Both line numbers confirmed exactly (the addendum's own citation,
  re-verified).
* **THE STOCK ENTRY AUTO-SUBMIT — unconditional, no ``capitalize_repair_cost`` gate at all, no
  try/except.** ``on_submit`` (asset_repair.py:202-213) calls ``decrease_stock_quantity()``
  (:258-293) FIRST, before the capitalized-only block: if ``stock_items`` is non-empty, a Stock
  Entry (``stock_entry_type="Material Issue"``, ``asset_repair=self.name``) is built, ``.insert()``-
  ed, then ``.submit()``-ed — bare, no exception handling of any kind (confirmed: :292-293 is two
  unguarded calls). A failed Stock Entry submit (e.g. its own mandatory ``company`` unmet) fails
  the WHOLE Asset Repair submit with it. This is also WHY Asset Repair is not a cascade leaf (see
  below) — a submitted Asset Repair with ``stock_items`` carries a submitted dependent from birth.
* **THE ASSET-STATUS SIDE-WRITE — the addendum's sharpest catch, the dossier missed it entirely;
  LANDED as SUBMIT-ONLY (see THE SHARPEST CORRECTION above).** ``validate()`` (:66) unconditionally
  calls ``update_status()`` (:178-187): ``if self.repair_status == "Pending" and self.asset_doc.
  status != "Out of Order": frappe.db.set_value("Asset", self.asset, "status", "Out of Order")``
  — else ``self.asset_doc.set_status()``, which itself does ``self.db_set("status", status)``
  (asset.py:770-774, re-verified: ``def set_status`` at line 770, the ``db_set`` call at line
  774) — both paths a raw, hookless write into the LINKED ASSET, entirely ungated by
  ``capitalize_repair_cost``. Because ``check_repair_status`` (:238-240, the LAST call in
  ``validate()``) refuses any submit while ``repair_status == "Pending"``, every SUCCESSFUL submit
  necessarily has ``repair_status`` in (Completed, Cancelled) — so the reachable branch on a real,
  governed submit is always the ``else``: ``Asset.set_status()`` recomputes the linked Asset's own
  status field from its own docstatus/depreciation state, a real cross-document mutation riding
  this broker's own submit consent with no second marker. The MV ``db_update``-bypass honesty
  grade, but into a DIFFERENT document (never itself).
* **Cascade — genuinely NOT a leaf; the SIMPLEST non-leaf shape this campaign has found (a direct
  header-level Link, no child-table resolution at all).** A full two-checkout grep for
  ``'"options": "Asset Repair"'`` returns exactly 2 hits: Asset Repair's own self-referencing
  ``amended_from`` (asset_repair.json:123) and Stock Entry's own ``asset_repair`` field
  (stock_entry.json:705, confirmed: ``{"depends_on": "eval:doc.asset_repair", "fieldname":
  "asset_repair", "fieldtype": "Link", "options": "Asset Repair", "read_only": 1}``) — a direct,
  header-level, ``read_only`` Link on submittable Stock Entry (``is_submittable: 1``). Zero hits in
  ``frappe-16``. This broker's own ``get_submitted_linked_docs`` (the standing, doctype-generic
  mechanism every prior non-leaf row rides — Pick List/Timesheet) walks this edge with ZERO
  ``cascade.py`` changes: a submitted Stock Entry naming this Asset Repair blocks a leaf
  ``plan_cancel``, and ``plan_cascade_cancel`` orders the Stock Entry first — the SAME parity
  answer Pick List's own landing gave for its own Stock Entry edge, and genuinely the answer to
  the dangling-Stock-Entry risk the addendum's own Landing Risk #3 raised: ERPNext itself never
  auto-reverses this Stock Entry on Asset Repair cancel, but Stock Entry is ALREADY a governed
  doctype in this broker's own set, so the cascade path provides the inventory-reversal parity
  ERPNext itself lacks.
* **``company`` PRESENT BUT NOT ``reqd`` (``fetch_from: "asset.company"``, no ``reqd`` key,
  confirmed — the addendum's own Correction #5) — the FIFTH such row** (after Purchase
  Invoice/Quality Inspection/Asset Value Adjustment/Timesheet), governed by the standing 0fdf91d
  ``_locks_for`` blank-company guard (ERPNext-equal, not added to the companyless tally). **Unlike
  Timesheet's own zero-consequence blank company, a blank company on Asset Repair IS genuinely
  consequential — the AVA honesty grade, not the Timesheet one — under TWO independent, data-
  driven conditions**, disclosed by :func:`pacioli.tools._asset_repair_submit_risk_flags`: (1)
  when the GL gate is armed (``capitalize_repair_cost`` truthy and ``total_repair_cost > 0``),
  ``get_asset_account`` (asset.py:1217-1237, called from ``get_gl_entries``) falls through to
  ``frappe.get_cached_value("Company", company, account_name)`` and throws ("Set Fixed Asset
  Account in company ...") when ``company`` is blank/unresolvable — a real, native submit refusal;
  (2) when ``stock_items`` is non-empty, the auto-created Stock Entry's own ``company`` field is
  ``reqd: 1`` (``stock_entry.json``, confirmed) — its ``.insert()`` throws ``MandatoryError``
  before ever reaching ``.submit()``. When NEITHER condition is armed (``capitalize_repair_cost``
  falsy AND ``stock_items`` empty), a blank company has no submit-time consequence at all — the
  Timesheet shape, genuinely reachable on this same doctype depending on the draft's own fields,
  never a fixed posture the way AVA's or Timesheet's own landings each described a single fate.
* **Whitelist: exactly 4, all read-only, confirmed** (``get_downtime`` :453-456, ``get_purchase_
  invoice`` :459-491, ``get_expense_accounts`` :494-507, ``get_unallocated_repair_cost``
  :551-566 — the addendum's own count, re-verified line-for-line) — nothing granted, no mutation in
  any body.
* **``in_list_view`` PLUS the ``total_repair_cost`` stand-in force the 35th special ``_list_fields``
  branch** (direct count of ``if doctype ==`` code lines after Pick List's own 34th, confirmed):
  ``["name", "repair_status", "docstatus", "asset", "downtime", "total_repair_cost", "company",
  date_field, "modified"]`` — the real ``in_list_view`` pair spliced alongside the status field and
  the grand_total stand-in, the Asset Movement/Delivery Trip convention (party_field=None means
  nothing to splice by name; the doctype's own real columns ride instead).
* **Skip tuple UNCHANGED at 28 members** (Asset Repair joins Asset as the SECOND CALLABLE-preview
  doctype this campaign has found, not a skip-tuple member at all — every other doctype since Asset
  has joined the skip tuple; Asset Repair is the first to rejoin Asset's own callable shape).
* **Minor (addendum's own Correction #4, confirmed):** the ``check_repair_status`` throw is a
  2-line statement — the condition at asset_repair.py:239, the ``frappe.throw(...)`` call at :240
  (the addendum's own "Line 240" single-line attribution collapsed the two).

Breadth (Invoice Discounting) — the forty-sixth supported doctype, landed off a pre-verification
addendum (``docs/plans/dossiers/invoice_discounting.verify.md``, 2026-07-21) whose own Correction 1
carried a wrong field-type claim, caught by the supervisor BEFORE dispatch and re-verified here.
Re-verified directly against ``/root/.pacioli/refs/erpnext-16/erpnext/accounts/doctype/
invoice_discounting/invoice_discounting.py`` (380 lines), ``invoice_discounting.json`` (complete
``json.load`` field enumeration), ``discounted_invoice.json`` (the child table), ``erpnext/
accounts/doctype/journal_entry_account/journal_entry_account.json``, ``erpnext/accounts/doctype/
journal_entry/journal_entry.py`` (``on_submit``/``on_cancel``/``update_invoice_discounting``),
``erpnext/hooks.py`` (``period_closing_doctypes``, ``auto_cancel_exempted_doctypes``),
``erpnext/controllers/accounts_controller.py``, ``erpnext/utilities/transaction_base.py``, and
directly against ``frappe/desk/form/linked_with.py`` + ``frappe/model/document.py`` (the cancel-time
back-link gate) — every load-bearing citation below independently re-verified, never inherited from
the dossier, the addendum, or the landing briefing.

**THE SUPERVISOR'S OWN CATCH, CONFIRMED — the addendum's Correction 1 calls ``reference_name`` "the
free Data field." FALSE.** ``json.load``-verified directly: ``journal_entry_account.json``'s
``reference_name`` field is ``{"fieldname": "reference_name", "fieldtype": "Dynamic Link", "label":
"Reference Name", "no_copy": 1, "options": "reference_type", "search_index": 1}`` — a real
**Dynamic Link**, paired with the SELECT ``reference_type`` field whose 16-entry hardcoded options
string includes ``"Invoice Discounting"`` (``journal_entry_account.json:184-198``, confirmed by
splitting the literal options string: Sales Invoice, Purchase Invoice, Journal Entry, Sales Order,
Purchase Order, Expense Claim, Asset, Loan, Payroll Entry, Employee Advance, Exchange Rate
Revaluation, Invoice Discounting, Fees, Full and Final Statement, Payment Entry, Bank Transaction —
**neither Asset Capitalization nor Subcontracting Receipt is a member of this specific list**, so
the briefing's speculation about those two rows sharing THIS channel is settled negative; the
remaining GL-heavy rows (Fees, Full and Final Statement, Loan, Payroll Entry, Employee Advance,
Exchange Rate Revaluation, Bank Transaction) are the ones actually worth checking at their own
future landings for this exact channel).

**THE CASCADE QUESTION — settled from source, not from the campaign's own grep method: Invoice
Discounting is NOT a cascade leaf.** The addendum's own Correction 1 already proved a submitted
Journal Entry can reach INTO an Invoice Discounting's status via this Dynamic Link pair (retrofitted
onto Journal Entry's own ``plan_submit``/``plan_cancel``/``plan_cascade_cancel`` disclosure,
commit ``3fa3303``, same-day). What that retrofit did NOT settle — and this landing does, from
source — is whether the broker's own standing blast-radius/cascade MACHINERY (not just its
disclosure text) actually SEES this edge. It does, on two independent counts:

1. **This broker's own ``get_submitted_linked_docs`` (the exact call ``_cascade_fetch_linked``
   wraps — ``erpnext.py``'s ``ErpnextClient.get_submitted_linked_docs``, POSTing to
   ``frappe.desk.form.linked_with.get_submitted_linked_docs``) walks Dynamic Link fields by the
   SAME live-distinct-value mechanism this campaign already proved twice (Landed Cost Voucher's own
   correction to Purchase Receipt's landing; Dunning's own Payment Entry Reference finding):
   ``SubmittableDocumentTree.get_doctype_references`` → ``get_references_across_doctypes`` calls
   BOTH ``get_references_across_doctypes_by_link_field`` AND
   ``get_references_across_doctypes_by_dynamic_link_field`` (``linked_with.py:214-217``) — the
   latter is a LIVE query (``frappe.get_all("Journal Entry Account", pluck="reference_type",
   distinct=1)``, ``linked_with.py:308-313``), not a static schema scan. ``Journal Entry Account``
   qualifies as a walked child table because Journal Entry itself (``is_submittable: 1``) carries
   ``{"fieldname": "accounts", "fieldtype": "Table", "options": "Journal Entry Account"}``
   (``journal_entry.json``) — ``get_child_tables_of_doctypes`` (``linked_with.py:158-189``) adds it
   to ``limit_link_doctypes`` automatically. The moment any Journal Entry Account row carries
   ``reference_type="Invoice Discounting"``, that value is discovered, and
   ``get_referencing_documents`` (``linked_with.py:324-364``) resolves the child-table hit up to its
   submitted ``Journal Entry`` parent (``parenttype``/``docstatus=1``/``allowed_parents`` — Journal
   Entry is NOT in ``hooks.py``'s ``auto_cancel_exempted_doctypes`` list, confirmed: that list holds
   only ``["Payment Entry"]``, ``hooks.py:428-430``). **Consequence: a submitted Journal Entry
   referencing an Invoice Discounting via this pair genuinely surfaces in
   ``get_submitted_linked_docs("Invoice Discounting", <name>)``'s response** — this broker's
   PRE-EXISTING generic refusal (any non-empty result denies a leaf ``plan_cancel``) already refuses
   to cancel an Invoice Discounting with a submitted Journal Entry against it, with ZERO new cascade
   code; ``plan_cascade_cancel`` orders that Journal Entry first (already a governed, "modeled"
   doctype) exactly like every other non-leaf row this campaign has landed.
2. **Independently, frappe's OWN native cancel-time gate agrees**, read directly:
   ``Document.check_no_back_links_exist`` (``document.py:1571-1578``) calls BOTH
   ``check_if_doc_is_linked(self, method="Cancel")`` AND
   ``check_if_doc_is_dynamically_linked(self, method="Cancel")`` — the second walks
   ``get_dynamic_link_map()`` (``frappe/model/dynamic_links.py``, a SEPARATE live-query mechanism
   from ``linked_with.py``, arriving at the same answer), which would ALSO refuse a raw ERPNext
   cancel of an Invoice Discounting referenced by a submitted Journal Entry Account row. Two
   independent code paths, same conclusion.

**So the addendum's own Landing Risk 1 framing — "a grep will never find it" — is true only of the
CAMPAIGN'S STATIC GREP METHOD, never of the runtime.** Both the broker's actual disclosure
machinery AND ERPNext's own native protection already see this edge; Invoice Discounting is
governed here as a genuine non-leaf, the THIRD non-leaf SHAPE this campaign has found (after a
direct header Link — Asset Repair — and a child-table PLAIN Link — Pick List/Timesheet): a
**child-table DYNAMIC Link**, tested both ways below (blocked leaf ``plan_cancel``,
``plan_cascade_cancel`` ordering the Journal Entry first).

**THE INCOMING MUTATOR, disclosed in BOTH directions, consistent with commit ``3fa3303``'s own
wording (never parallel-invented):** ``JournalEntry.on_submit``/``on_cancel``
(``journal_entry.py:212``/``:326``) call ``update_invoice_discounting`` (``:459-501``), which — for
every Journal Entry Account row naming this Invoice Discounting — THROWS unless this document's
CURRENT status matches the direction's expected stage, then calls
``inv_disc_doc.set_status(status=status)`` (``:501``): the ``if status:`` branch
(``invoice_discounting.py:101-108``) this document's OWN ``on_submit``/``on_cancel`` never reach
(they only ever pass ``cancel=1``, hitting the ``else:`` branch, ``:109-114``) — a raw ``db_set``
that ALSO loops every row in ``self.invoices`` calling ``SalesInvoice.set_status(update=True)``, a
third-level cascade. This document's own ``plan_submit``/``plan_cancel`` disclose the reciprocal
half of the SAME finding (:func:`pacioli.tools._invoice_discounting_submit_risk_flags`/
``_invoice_discounting_cancel_risk_flags``) — this document's own status is not stable even once
governed, submitted, or cancelled by this broker: a THIRD, independently-governed document (Journal
Entry) can still flip it, and cascade into every one of ITS OWN linked Sales Invoices, outside this
document's own hooks entirely.

**THE BYPASS-TAXONOMY CALL (addendum Landing Risk 2), made deliberately, not by default: this is
the SAME ``db_set``-via-the-target's-own-method family Asset/Asset Repair's own "Asset-status
raw side-write" already established — NOT Maintenance Visit's/Asset Maintenance Log's "parent-side
db_update bypass" family, and NOT Quality Inspection's raw-SQL family.** In the Asset Repair shape,
the CALLER (AssetRepair) invokes ``Asset.set_status()`` — a method THE TARGET ITSELF defines — and
that method's own body chooses a hookless ``db_set`` internally; the caller never reaches around the
target's public surface, it merely uses it, and the target's own author is the one who made that
method skip validate/hooks. Journal Entry's own call to ``inv_disc_doc.set_status(status=status)``
is structurally IDENTICAL: the caller uses the TARGET's (Invoice Discounting's) own public method,
and IT is the one whose ``if status:`` branch chooses ``db_set``. This is genuinely different from
Maintenance Visit's/Asset Maintenance Log's shape, where the caller fetches ANOTHER document and
calls the FRAMEWORK's generic, doctype-agnostic ``db_update()``/``frappe.db.set_value`` directly on
it — a much wider escape hatch the caller wields itself, not a narrow convenience method the target
authored for its own reasons. **The one genuinely NEW wrinkle, worth naming as its own qualifier
rather than a new family: this is a CHAINED, THIRD-PARTY-TRIGGERED self-bypass** — Asset Repair's
own chain is 2 doctypes deep (AssetRepair → Asset); this one is 3 deep and the trigger is a
completely independent, separately-governed document (Journal Entry) that is neither the source nor
the ultimate target (Sales Invoice) of the write it sets in motion. Call it **"delegated
self-bypass, chained"** — same primitive, new depth, a real but incremental variant.

Confirmed from source (2026-07-21 checkout):

* **``party_field=None`` — a CHILD-TABLE party shape, the RFQ/child-table-party family, not a
  Dynamic Link pair (Quotation/QI) and not a dual-conditional splice (Blanket Order/Bank
  Guarantee).** ``invoices`` (Table: Discounted Invoice, ``reqd: 1``) is the only party-bearing
  surface; each row carries its own ``customer`` (Link, ``read_only``, ``fetch_from:
  "sales_invoice.customer"``) — confirmed from ``discounted_invoice.json``'s complete 6-field
  enumeration. GL posts ``"party": d.customer`` per row (``invoice_discounting.py:154``/``175``) —
  no single header-level party column exists to name.
* **``status``="status" PRESENT (Select: Draft/Sanctioned/Disbursed/Settled/Cancelled,
  ``invoice_discounting.json:63-70``) — JSON-level ``read_only: 1`` (the addendum's own Correction
  4, confirmed: line 69), yet written by THREE separate runtime paths:** (a) this document's own
  ``set_status()`` ``else:`` branch's in-memory ``self.status = ...`` assignment (``:109-114``);
  (b) that SAME method's own ``db_set`` cancel tail (``:116-117``, ``if cancel:``); (c) the
  cross-document ``db_set`` via Journal Entry's ``update_invoice_discounting`` (above) — a real,
  named nuance — the checklist's own AML callout, here on a fieldname that IS literally "status".
  ``grand_total`` ABSENT; stand-in ``total_amount`` (Currency, ``read_only``, ``:94-100``, sum of
  child ``outstanding_amount``). ``in_list_view`` confirmed EXACTLY ``{posting_date, company}``
  (``json.load``-filtered) — forces its OWN, THIRTY-SIXTH special ``_list_fields`` branch (direct
  count after Asset Repair's own 35th): party_field=None means nothing to splice by name, so the
  doctype's own real ``status``/``total_amount`` columns ride instead, alongside the ``in_list_view``
  pair.
* **``date_field="posting_date"`` — REJOINS the large existing posting_date set, no new pattern, no
  exclusivity carve-out needed.** ``posting_date`` (Date, ``reqd: 1``, default "Today") is real and
  required on every draft; GL entries inherit it via ``get_gl_dict``'s own fallback
  (``accounts_controller.py:1329``: ``posting_date = args.get("posting_date") or
  self.get("posting_date")``) — neither of Invoice Discounting's two ``get_gl_dict(...)`` calls
  (``invoice_discounting.py:151-168``, ``:171-188``) passes a ``posting_date`` key, so the GL truly
  posts under this document's own field, never an override. ``loan_start_date``/``loan_end_date``
  (both plain Date, optional, ``loan_end_date`` computed by ``set_end_date()``) are NOT the pin —
  confirmed absent from GL posting entirely. All three Date fields are genuinely ``fieldtype:
  "Date"`` (``json.load``-confirmed) — no Datetime, no projection needed, the simplest date shape
  this campaign has landed in weeks.
* **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by a full read of all 380 lines of
  ``invoice_discounting.py``: no ``def submit(``/``def cancel(`` anywhere** — ``on_submit``/
  ``on_cancel`` are ordinary lifecycle hooks, never shadowed overrides.
* **THE MRO CORRECTION (addendum Correction 3, confirmed) — the dossier's own "StockController or
  controller chain" hedge does not rescue a class that isn't there.** Real chain, 4-deep:
  ``InvoiceDiscounting -> AccountsController -> TransactionBase -> StatusUpdater ->
  frappe.model.document.Document`` (``accounts_controller.py:105``, ``transaction_base.py:20`` —
  ``StockController`` is a sibling controller, never an ancestor of ``AccountsController``, the
  SAME correction Asset Repair's own landing already made for its own dossier).
* **THE LEDGER-PREVIEW FINDING — CALLABLE, the THIRD such row (after Asset, Asset Repair), NOT the
  skip tuple.** ``make_gl_entries`` (``invoice_discounting.py:130-190``) is this doctype's OWN
  method. ``projected_gl`` is honest-empty per invoice ROW (not per whole document) whenever a
  row's own ``outstanding_amount`` is zero/blank (``:142``, gating that row's GL pair alone) —
  disclosed data-driven off the draft's own ``invoices`` rows
  (:func:`pacioli.tools._invoice_discounting_submit_risk_flags`).
* **THE THREE ``validate()`` THROWS (addendum Correction 2, entirely missing from the dossier's own
  §11 "None found") — ``validate()`` (``:48-53``) calls ``validate_mandatory()`` then
  ``validate_invoices()``.** ``validate_mandatory`` (``:59-61``): ``if self.docstatus == 1 and not
  (self.loan_start_date and self.loan_period): frappe.throw(...)`` — DETERMINISTIC-from-draft (only
  ``docstatus``/``loan_start_date``/``loan_period``), firing exactly at submit-time save, the clean
  gated-flag shape. ``validate_invoices`` (``:63-87``) throws TWICE, both cross-document and BOTH
  staying prose per the checklist's own rule (never collapsed into one flag, never promoted to
  draft-only): a live ``Discounted Invoice`` (docstatus=1) peer read (``:64-77``, fires on every
  save, not merely submit) and a live Sales Invoice ``outstanding_amount`` read against the child
  row's possibly-stale cached value (``:79-87``).
* **on_submit** (``:92-94``): ``update_sales_invoice()`` (``:119-128`` — unconditionally sets
  ``is_discounted=1`` on every named Sales Invoice via a raw ``frappe.db.set_value``, ``:128``, a
  hookless per-row write into another doctype) then ``make_gl_entries()``.
* **on_cancel** (``:96-99``): ``set_status(cancel=1)`` (own ``db_set``, ``:117``) then
  ``update_sales_invoice()`` (cancel direction — ``is_discounted`` cleared to 0 ONLY when no OTHER
  submitted Discounted Invoice record still names the same Sales Invoice, ``:123-127``, a
  conditional cross-document read) then ``make_gl_entries()`` reversal (``cancel=(docstatus==2)``
  passed to the global ``make_gl_entries()``, ``:190``). No ``ignore_linked_doctypes`` set at all —
  the standing blast-radius gate is this document's ONLY protection against a submitted-Journal-
  Entry-shaped cancel hazard, and (per THE CASCADE QUESTION above) it genuinely provides one.
* **Whitelist: exactly 3, confirmed** (``create_disbursement_entry`` ``:192-255``, ``close_loan``
  ``:257-316`` — both instance methods returning a fresh, UNSAVED Journal Entry, never
  auto-submitted, never mutating this document; ``get_invoices`` ``:319-357``, a module-level pure
  read) — nothing granted. ``get_invoices`` builds its own WHERE-clause fragment via string
  concatenation of which filter keys are truthy (``:322-354``) — the query VALUES are parameterized
  (safe), only the clause SHAPE is data-driven off key presence, not user-controlled beyond that;
  worth a second look only if this read is ever exposed as a broker list-tool.
* **``company`` PRESENT AND ``reqd: 1``** (``invoice_discounting.json:71-78``, confirmed —
  UNLIKE Asset Repair's own present-but-optional company) — the standard wrong-books belt applies
  unconditionally, no companyless tally change.
* **``period_closing_doctypes``: Invoice Discounting IS the 7th entry** (``hooks.py:326-345``,
  confirmed by counting the literal 18-entry list — the addendum's own citation, re-verified) —
  natively EQUAL to this broker's own closed-books check, the Asset/Asset Repair precedent, the
  THIRD such row.
* **Tallies: the 46th supported doctype (235->240 tools); skip tuple UNCHANGED at 28 (CALLABLE
  joins nothing); THIRTY-SIXTH special ``_list_fields`` branch by direct count.**

Breadth (Asset Capitalization) — the forty-seventh supported doctype, landed off a pre-verification
addendum (``docs/plans/dossiers/asset_capitalization.verify.md``, 2026-07-21) whose own headline
RED FLAG (a target-asset cost double-count on re-submit) is ITSELF REFUTED here, with receipts
independently re-verified, not merely re-stated. Confirmed directly against
``/root/.pacioli/refs/erpnext-16/erpnext/assets/doctype/asset_capitalization/asset_capitalization.
{json,py}`` (39 fields, 882 lines), ``erpnext/assets/doctype/asset/depreciation.py``, ``erpnext/
assets/doctype/asset_depreciation_schedule/asset_depreciation_schedule.py``, ``erpnext/controllers/
stock_controller.py``, ``erpnext/hooks.py``, and ``frappe/model/document.py`` — every citation below
independently re-verified, never inherited from the dossier, the addendum, or the landing briefing.

**THE HEADLINE RULING — THE DOUBLE-COUNT RED FLAG IS REFUTED, RECEIPTS RE-WALKED FROM SOURCE.** The
dossier's own RED FLAG claimed: "If an AC is submitted, then amended, then re-submitted, the target
asset's cost is incremented TWICE (no deduplication logic)." ``update_target_asset()``
(``asset_capitalization.py:554-573``) reads a FRESH ``frappe.get_doc("Asset", self.target_asset)``
every call (line 556, no stale in-memory reference), then branches on ``self.docstatus``: ``== 2`` ->
subtract all three of ``net_purchase_amount``/``purchase_amount``/``total_asset_cost`` (lines
558-561); else -> add the same three (lines 562-565); a single 3-field ``db_set`` (lines 567-573,
one dict-form call, not three separate ones) commits the result. A submit always adds once (``self.
docstatus`` is already ``DocStatus.SUBMITTED`` by the time ``on_submit`` fires — ``Document._submit``,
``frappe/model/document.py:1319-1322``: ``self.docstatus = DocStatus.SUBMITTED; return self.save()``)
and a cancel always subtracts once (``Document._cancel``, ``document.py:1324-1327``, same shape,
``docstatus = DocStatus.CANCELLED`` before ``save()``, whose post-save hooks run ``on_cancel``) —
symmetric by construction, never by a bespoke guard. **Amendment is framework-BLOCKED without a
prior, already-committed cancel** — ``Document.insert()`` (``document.py:501-502``) unconditionally
calls ``validate_amended_from()`` (``document.py:613-618``) whenever ``amended_from`` is set — its
body is exactly: ``if frappe.db.get_value(self.doctype, self.get("amended_from"), "docstatus") != 2:
frappe.throw(...)`` — a DB read of the ORIGINAL's ``docstatus``, checked the moment the amended draft
is first inserted;
it can only pass once the original's own cancel (``docstatus=2``, and with it ``update_target_asset``'s
subtract branch) has already landed as part of that same prior ``cancel()``/``save()`` call. None of
``stock_items``/``asset_items``/``service_items``/``total_value`` carry ``allow_on_submit`` (confirmed
absent from every field's JSON entry, default false), so the amount subtracted at cancel is always the
IDENTICAL value that was added at the matching submit — no post-submit editing can create drift
between the two. The full chain is therefore: submit (+V) -> cancel (-V, net 0, and this is the ONLY
door an amendment can walk through) -> amend (a fresh draft, ``validate()`` reruns
``calculate_totals()``) -> submit (+V', a single fresh addition) — there is no window in which two
adds can land against the same lineage without an intervening subtract. **What IS true, narrowly: the
code carries no bespoke amendment-aware dedup guard of its own — but it needs none, because Frappe's
OWN submit/cancel/amend state machine already serves as the guard, and ``update_target_asset()``'s
docstatus-branch is correctly wired to it.** This is a DIFFERENT, MORE ROBUST pattern than a bespoke
check would be — not a defect, and not a published finding. This also closes a tracked campaign open
item (the fan's own AC double-count candidate) — say so plainly rather than re-flagging it.

**A SECOND OPEN QUESTION, CHASED TO GROUND AND ALSO CLOSED, DECISIVELY, FROM SOURCE.** The dossier
separately worried that multiple INDEPENDENT (non-amendment-chain) Asset Capitalizations consuming
the SAME asset could leave ``set_consumed_asset_status`` (``asset_capitalization.py:596-612``)
inconsistent on a partial cancel — a "single-asset-single-restorer" assumption in
``restore_consumed_asset_items``. **This cannot happen through governed operation, and the proof is
in ``validate_consumed_asset_item`` itself** (``asset_capitalization.py:234-251``): for every consumed
``asset_items`` row, a FRESH ``frappe.db.get_value`` read (``get_asset_for_validation``, lines
299-305) checks the asset's live ``status``, and ``frappe.throw``s ("Row #{0}: Consumed Asset {1}
cannot be {2}") whenever that status is ``Draft``/``Scrapped``/``Sold``/``Capitalized`` (line 246) —
and ``validate()`` runs this check on EVERY save, draft or submit alike. The moment AC#1 submits, its
own ``set_consumed_asset_status`` (line 597-604) sets the consumed asset's status to ``"Capitalized"``
via ``Asset.set_status("Capitalized")`` (a direct ``db_set``, ``asset.py:770-774``, status truthy so
``get_status()`` is never consulted). Any SECOND, independent Asset Capitalization naming that same
asset in ITS OWN ``asset_items`` — whether being drafted, saved, or submitted — is refused immediately
by this same throw, BEFORE it can ever be validly created, let alone submitted. Two independent
Asset Capitalizations can therefore never simultaneously hold the same consumed asset; the earlier
one must be cancelled first (which runs ``Asset.set_status()`` with no argument, line 606, recomputing
the status fresh from the asset's OWN docstatus/depreciation state via ``get_status()``,
``asset.py:776-808`` — which, notably, has NO branch that ever returns ``"Capitalized"`` at all,
confirming the restore is a genuine, correct reversal, never a guess) before any other Asset
Capitalization could validly consume it again. The "single restorer" assumption is therefore SAFE by
construction, not a latent defect — settled from source, not merely asserted.

**THE DEPRECIATION SIBLING-DOCUMENT FACTORY — AC IS THE SECOND CONFIRMED INSTANCE of the
``reschedule_depreciation`` factory AVA's landing (``a39adce``) named, reached via a DIFFERENT call
path (consumed-asset depreciation, not AVA's own ``update_asset()``).** The dossier's §10 ("NOT a new
document creation, but a side-effect GL posting via another doctype's routine") is WRONG — corrected
here, matching the addendum's own Correction 5, independently re-walked:

* **Submit direction, armed off the draft's own ``asset_items`` rows (presence = armed; the actual
  ``calculate_depreciation`` gate is a live read of the CONSUMED Asset, cross-document, prose per the
  standing rule).** ``get_gl_entries_for_consumed_asset_items`` (``asset_capitalization.py:472-503``),
  for each consumed asset where ``asset.asset_type != "Composite Component"`` (line 477) AND
  ``asset.calculate_depreciation`` (line 478), calls ``depreciate_asset(asset, self.posting_date,
  notes)`` (line 485). ``depreciate_asset`` (``depreciation.py:477-487``) calls:

  1. ``reschedule_depreciation(asset_doc, notes, disposal_date=date)`` (``depreciation.py:481``) — the
     IDENTICAL function AVA's own ``update_asset()`` uses (``asset_depreciation_schedule.py:194-219``,
     the same line range AVA's landing cited). It cancels an existing SUBMITTED (``docstatus==1``)
     Asset Depreciation Schedule with ``should_not_cancel_depreciation_entries = True`` (lines
     216-217 — already-POSTED depreciation Journal Entries are explicitly preserved, untouched) and
     CREATES + SUBMITS a replacement Asset Depreciation Schedule (line 219: ``new_schedule.submit()``).
  2. ``make_depreciation_entry_on_disposal(asset_doc, date)`` (``depreciation.py:482``) ->
     ``make_depreciation_entry`` (``depreciation.py:167-213``) -> ``_make_journal_entry_for_
     depreciation`` (``depreciation.py:216-249``), which, for any schedule row with no
     ``journal_entry`` yet and ``schedule_date <= disposal_date`` (the guard, lines 229-232), CREATES
     AND SUBMITS a brand-new Journal Entry (line 234 ``frappe.new_doc("Journal Entry")``, line 244
     ``je.flags.ignore_permissions = True``, line 245 ``je.save()``, line 248 ``je.submit()``) —
     catching up any unposted depreciation before disposal.
  3. ``asset_doc.reload()`` then ``cancel_depreciation_entries(asset_doc, date)``
     (``depreciation.py:486-487``, the call; the function at ``:491-495``) — in THIS core tree a
     literal ``pass`` STUB, self-documented "Overwritten via India Compliance app": on a vanilla
     bench it does nothing, but on a bench running India Compliance it becomes a live channel that
     CANCELS already-posted same-financial-year depreciation Journal Entries during this same
     submit — a cross-app extension point named here per the Timesheet "Payslip" precedent, so a
     future reader on such a bench isn't surprised by a mechanism this tree doesn't carry (caught
     by the supervisor's own read of the full ``depreciate_asset`` body; the addendum's two-call
     trace stopped at ``:482``).

* **Cancel direction, the mirror.** ``restore_consumed_asset_items()``
  (``asset_capitalization.py:581-594``) calls, for each consumed asset with ``calculate_depreciation``:

  1. ``reverse_depreciation_entry_made_on_disposal(asset)`` (line 587) -> ``depreciation.py:504-516``,
     which, when a matching schedule row carries a ``journal_entry`` and disposal-timing conditions
     hold, calls ``create_reverse_depreciation_entry`` (``depreciation.py:544-563``) — CREATES AND
     SUBMITS a reversing Journal Entry (line 545 ``make_reverse_journal_entry``, line 558
     ``reverse_journal_entry.submit()``).
  2. ``reset_depreciation_schedule(asset, notes)`` (line 593) -> ``depreciation.py:498-501``, which
     ALSO calls ``reschedule_depreciation`` — cancel + recreate + submit the schedule again,
     symmetric with the submit direction.

  Corroborated by AC's own code: ``on_cancel``'s ``ignore_linked_doctypes`` tuple
  (``asset_capitalization.py:123-130``) explicitly names ``"Asset"`` and ``"Repost Item Valuation"``
  (below) — the author anticipated cascade-delete friction from documents this lifecycle creates.

  Increment the sibling-document-factory tally: AVA was first, this is the SECOND confirmed instance,
  reached through a different call path (depreciation-of-a-consumed-asset vs. AVA's own direct
  ``update_asset()``).

**THE ASYNC CHANNEL — ``repost_future_sle_and_gle`` ARMS "Repost Item Valuation," an ERPNext-scheduled
channel this broker does not govern; the dossier's blanket "no async channels" claim does not ship.**
``repost_future_sle_and_gle()`` — called from BOTH ``on_submit`` (line 119) and ``on_cancel`` (line
133), inherited from ``StockController`` (``stock_controller.py:1830-1854``) — can create a "Repost
Item Valuation" document (via ``create_repost_item_valuation_entry``/``create_item_wise_repost_
entries``, ``stock_controller.py:1848-1854``) whenever ``future_sle_exists(args)`` or ``repost_
required_for_queue(self)``. Repost Item Valuation entries are processed by ERPNext's OWN scheduled
stock-reposting job, entirely outside any call this broker makes — the same "Asset precedent" shape
the interrogation checklist itself references (an armed-but-broker-invisible channel, disclosed data-
driven rather than hidden). AC's own ``on_cancel`` code is internally consistent with this: its
``ignore_linked_doctypes`` tuple (line 126) already names ``"Repost Item Valuation"``, which would be
pointless to suppress if this doctype could never produce one.

**RETROACTIVE CONSISTENCY CHECK ACROSS PRIOR StockController ROWS — A GENUINE GAP, FLAGGED, NOT
SILENTLY FIXED.** Stock Entry, Stock Reconciliation, Delivery Note, and Purchase Receipt are ALL
``StockController`` subclasses that call the SAME ``repost_future_sle_and_gle()`` from BOTH their own
``on_submit`` AND ``on_cancel`` (independently re-verified against each doctype's own ``.py`` source:
``stock_entry.py:566``/``602``; ``stock_reconciliation.py:114``/``126``; ``delivery_note.py:479``/
``500``; ``purchase_receipt.py:396``/``467``) — yet NONE of the four carries a dedicated risk-flag
function in ``tools.py`` (no ``_stock_entry_*``/``_stock_reconciliation_*``/``_delivery_note_*``/
``_purchase_receipt_*_risk_flags`` exists at all), so none of them surfaces "Repost Item Valuation" as
an actual ``risk_flags`` entry in a real ``plan_submit``/``plan_cancel`` response. In PROSE, the four
rows are inconsistent with each other too: Stock Entry's own module-docstring section never mentions
``repost_future_sle_and_gle``/"Repost Item Valuation" at all (total silence); Purchase Receipt's and
Delivery Note's own sections cite the bare function CALL as part of narrating the ``on_cancel``
sequence, with zero elaboration on what it can create; Stock Reconciliation's own section is the
fullest of the four, naming "Repost Item Valuation" specifically, but only in the context of its own
``ignore_linked_doctypes`` exemption list, never framed as an armed/ungoverned scheduler channel the
way the Asset precedent frames its own async gaps. **This is a real retroactive documentation-and-
disclosure gap across four already-landed, already-shipped rows — reported here prominently per
instruction, not silently patched**: fixing it (adding the flag/prose to those four rows) is the
supervisor's call, out of scope for this landing, which discloses ONLY Asset Capitalization's own
channel, correctly, going forward.

**THE MRO CORRECTION — 5-deep, one link deeper than every prior CALLABLE row (Asset Repair/Invoice
Discounting were each 4-deep), because StockController sits in the chain this time.** Real chain:
``AssetCapitalization -> StockController -> AccountsController -> TransactionBase -> StatusUpdater ->
Document`` (``asset_capitalization.py:48``; ``class StockController(AccountsController)``,
``stock_controller.py:86``; ``class AccountsController(TransactionBase)``,
``accounts_controller.py:105``; ``class TransactionBase(StatusUpdater)``, ``transaction_base.py:20``;
``class StatusUpdater(Document)``, ``status_updater.py:181`` — every link independently re-verified).
The addendum's own MRO correction (two extra links vs. the dossier's "StockController ->
AccountsController -> Document") is confirmed right. Doesn't change the CALLABLE verdict — Asset
Repair/Invoice Discounting's own chains skip ``StockController`` entirely (Asset Repair/Invoice
Discounting inherit ``AccountsController`` directly), so this is the CALLABLE category's deepest MRO
yet, though NOT the campaign's deepest overall (several UNCALLABLE rows, e.g. Installation Note/
Maintenance Visit/Maintenance Schedule, already run through comparably long bare-``Document`` chains
of their own kind).

**FIELD-COUNT ARITHMETIC CORRECTED — the addendum's own fix, independently re-verified by
``json.load``.** The dossier's "39 (16 data fields + 20 structural + 3 table fields)" breakdown is
wrong; a full enumeration of the ``.json``'s 39 ``fields[]`` entries gives **19 data fields + 17
structural (9 Column Break + 8 Section Break) + 3 table fields (``stock_items``/``asset_items``/
``service_items``) = 39** — the total was right, the 16/20 split was not.

**``make_gl_entries`` LINE RANGE CORRECTED — the addendum's own fix, independently re-verified.** The
real ``make_gl_entries(self, gl_entries=None, from_repost=False)`` is ``asset_capitalization.py:
388-398`` (dispatches to the framework's ``make_gl_entries``/``make_reverse_gl_entries`` by
``docstatus``, calling ``self.get_gl_entries()`` at line 393 when no rows are pre-supplied) — a
SEPARATE method, ``get_gl_entries(...)`` (``:400-426``), is the entry-list BUILDER
``make_gl_entries`` calls. The CALLABLE verdict (own method, reachable, unconditional on submit/
cancel) is unaffected; only the citation was wrong.

**``before_submit`` — MISSING FROM THE DOSSIER ENTIRELY, A CLEAN DETERMINISTIC-FROM-DRAFT DOOMED-
SUBMIT GATE.** ``before_submit()`` (``asset_capitalization.py:111-113``) calls
``validate_source_mandatory()`` (``:286-292``), which throws ("Consumed Stock Items, Consumed Asset
Items or Consumed Service Items is mandatory for Capitalization") whenever ALL THREE child tables
(``stock_items``/``asset_items``/``service_items``) are empty — readable off the draft alone, before a
marker is ever minted; disclosed data-driven (:func:`pacioli.tools.
_asset_capitalization_submit_risk_flags`).

**``update_target_asset()``'s ``db_set`` — a 3-FIELD BATCHED FORM, worth naming as its own call
shape.** ``asset_doc.db_set({...})`` (line 567) writes ``net_purchase_amount``/``purchase_amount``/
``total_asset_cost`` in ONE dict-form call — the same ``db_set``-bypass grade Asset Movement's own
precedent established (``before_change``/``on_change`` hooks fire; validate/permission/version-
history do not), here as a 3-field batched form rather than three separate single-field calls.

Confirmed from source (2026-07-21 checkout):

* **``party_field=None`` — confirmed, no Customer/Supplier/Party Link or Dynamic Link anywhere in the
  39-field enumeration.** ``company`` (Link, ``reqd: 1``, ``json:88-94``) is scope, not a GL party;
  ``target_asset``/``cost_center``/``project`` are likewise never parties.
* **``status``/``grand_total``: CONFIRMED ABSENT.** ``total_value`` (Currency, read-only,
  ``:366``: ``self.stock_items_total + self.asset_items_total + self.service_items_total``) is the
  stand-in aggregate. ``in_list_view``: confirmed EXACTLY ``{posting_date}`` (``json:99``,
  grep-verified — no other field carries ``in_list_view: 1``) — forces its OWN, THIRTY-SEVENTH
  special ``_list_fields`` branch (direct count after Invoice Discounting's own 36th): party_field=
  None means nothing to splice by name, ``total_value`` rides as the aggregate substitute (the
  Invoice Discounting judgment call, spliced regardless of its own missing ``in_list_view`` flag —
  the same tension Budget's own landing left unresolved between "splice only what's flagged" and "a
  genuine total substitute rides anyway," not resolved here either, simply followed consistently with
  the freshest, nearest-shaped precedent).
* **``date_field="posting_date"`` — a plain Date (``reqd: 1``, default "Today", ``json:96-105``),
  paired with a separate ``posting_time`` (Time, ``reqd: 1``, default "Now", ``json:106-113``) — the
  Stock Entry/Stock Reconciliation Date+Time PAIR precedent (both independently re-confirmed to carry
  this exact same two-field shape), NOT a new pattern. Neither field carries an explicit
  ``allow_on_submit`` key (absence = Frappe's implicit default of 0/false).
* **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 882 lines of
  ``asset_capitalization.py``: no ``def submit``/``def cancel`` anywhere** in the 31-method class
  body — ``before_submit``/``on_submit``/``on_cancel`` are ordinary lifecycle hooks, never shadowed
  overrides.
* **Ledger preview: CALLABLE — own ``make_gl_entries`` (line range corrected above), reachable and
  unconditional on submit/cancel** — does NOT join the skip tuple (unchanged at 28).
* **Cascade — a genuine LEAF for INCOMING links, confirmed by an independent full-tree grep of BOTH
  checkouts for ``'"options": "Asset Capitalization"'``: exactly ONE hit, this doctype's own
  self-referencing ``amended_from`` (``asset_capitalization.json:129-136``).** No external doctype
  carries a real field-level Link to Asset Capitalization. ``erpnext/assets/doctype/asset/asset.json``
  DOES carry a ``links`` (Connections-tab) entry naming Asset Capitalization
  (``link_doctype: "Asset Capitalization"``, ``link_fieldname: "target_asset"``,
  ``asset.json:624-628``, independently re-confirmed) — this is a UI-only reverse-connections
  convenience widget, not a field/cascade edge, and correctly does not show up in (or invalidate) the
  leaf grep above. **AC is, however, genuinely a DEPENDENT NODE inside an ASSET's own
  ``plan_cascade_cancel`` graph, on TWO edges, not one** — Asset's own landing already enumerated both:
  ``target_asset`` (a header Link, ``asset_capitalization.json``) and ``asset_capitalization_asset_
  item.asset`` (the consumed-asset child row, Link -> Asset, ``reqd: 1``, independently re-confirmed)
  are named among Asset's own "18 ``Link -> Asset`` fields across 16 doctypes" (see this module's own
  "Breadth (Asset)" section) — a submitted Asset Capitalization surfaces in an Asset's own cascade
  graph exactly like Asset Value Adjustment's single ``asset`` edge already does, but through TWO
  edges instead of one. Zero ``cascade.py`` changes either way (doctype-blind by construction, as
  always).
* **Whitelist: exactly 9, confirmed** (``@frappe.whitelist()`` at lines 307/315/615/641/668/716/732/
  778/801, matching the dossier and addendum exactly) — 7 read-only helpers plus 2 child-row
  in-place mutators (``set_warehouse_details``/``set_asset_values`` — draft-time ``validate()``
  helpers that update child-table rows in place, never an external document or a submitted state).
  Nothing granted.
* **``company`` PRESENT AND ``reqd: 1``** (``json:88-94``) — the standard wrong-books belt applies
  unconditionally; NOT one of the campaign's companyless rows.
* **``period_closing_doctypes``: a CITATION-ORDINAL CORRECTION to the addendum.** The addendum claims
  Asset Capitalization is "the 13th entry, at line 338" — independently re-counting the literal
  18-entry list (``hooks.py:326-345``) by hand gives Sales Invoice(1)/Purchase Invoice(2)/Journal
  Entry(3)/Bank Clearance(4)/Stock Entry(5)/Dunning(6)/Invoice Discounting(7)/Payment Entry(8)/Period
  Closing Voucher(9)/Process Deferred Accounting(10)/Asset(11)/**Asset Capitalization is the 12th
  entry**, at line 338 — Asset Repair is the 13th, at line 339 (matching Asset Repair's OWN landing
  citation, ``hooks.py:339``, which is correct). The addendum's ordinal was off by one; the LINE
  NUMBER (338) was already right. Natively EQUAL to this broker's own closed-books check — the
  FOURTH such row after Asset/Asset Repair/Invoice Discounting.
* **Tallies: the 47th supported doctype (240->245 tools); skip tuple UNCHANGED at 28 (CALLABLE joins
  nothing); THIRTY-SEVENTH special ``_list_fields`` branch by direct count; SECOND confirmed
  sibling-document-factory row (after Asset Value Adjustment); FOURTH period-closing native-equal
  row (after Asset/Asset Repair/Invoice Discounting).

Breadth (Production Plan) — the forty-eighth supported doctype, off a pre-verification addendum
(``docs/plans/dossiers/production_plan.verify.md``, 2026-07-21) that is UNUSUALLY STRONG — it
re-verified every dossier line citation against source before this landing ran — but whose own
list-tier tally ("26 branches") is stale by the time this landing ran (37 special branches stood
after Asset Capitalization; direct count of this function's own ``if doctype ==`` lines, confirmed
before this edit). Confirmed directly against ``/root/.pacioli/refs/erpnext-16/erpnext/
manufacturing/doctype/production_plan/production_plan.{json,py}`` (58 fields, 2261 lines),
``erpnext/manufacturing/doctype/work_order/work_order.{json,py}``, ``erpnext/buying/doctype/
purchase_order_item/purchase_order_item.json``, ``erpnext/stock/doctype/material_request_item/
material_request_item.json``, ``erpnext/stock/doctype/stock_reservation_entry/
stock_reservation_entry.py``, and ``frappe/model/document.py``/``frappe/model/delete_doc.py`` —
every citation below independently re-verified, never inherited from the dossier, the addendum, or
the landing briefing.

**THE CASCADE QUESTION — NOT A LEAF, on THREE independent counts, all missed by the dossier's own
full-tree scan (§8) despite the dossier's own text QUOTING one of the three fields in the same
sentence it called the scan empty.** An independent full two-checkout grep for ``'"options":
"Production Plan"'`` returns FOUR hits, not one: the doctype's own self-referencing
``amended_from`` (``production_plan.json:307-313``) plus three real external edges —
``Work Order.production_plan`` (``work_order.json:468-475``, a direct HEADER-level Link,
``read_only``/``no_copy``/``search_index: 1`` — the EXACT field ``delete_draft_work_order`` itself
already queries by, ``production_plan.py:683-684``), ``Purchase Order Item.production_plan``
(``purchase_order_item.json:835-841``, child-table Link, parent Purchase Order confirmed
``is_submittable: 1``), and ``Material Request Item.production_plan``
(``material_request_item.json:291-297``, child-table Link, parent Material Request confirmed
``is_submittable: 1``). The dossier's own §8 text NAMES "Material Request Item...
production_plan_qty, production_plan respectively" and then asserts, in the SAME sentence, that
neither "is Link fieldtype" — directly contradicted by the JSON it is citing (``production_plan``
on Material Request Item IS a real Link; only its SIBLING field, ``production_plan_qty``, is a
plain Float). The two child-table edges resolve to their submittable parents through the SAME
``get_parent_if_child_table_doc=True``/``parent_filters=[("docstatus", "=", 1)]`` mechanism
(``frappe/desk/form/linked_with.py:121``/``:356-364``) the Pick List/Timesheet landings already
proved — this broker's own ``get_submitted_linked_docs`` walks all three generically, ZERO
``cascade.py`` changes either way.

**THE CANCEL-ORDERING WRINKLE — the SAME shape Delivery Trip's landing named, this time WITHOUT a
structural dead end.** Traced directly against ``frappe/model/document.py``:
``run_post_save_methods`` (``:1450-1452``) runs ``on_cancel`` FIRST, then
``check_no_back_links_exist()`` (``:1572-1578``) AFTER, inside the SAME ``.save()`` call a raw
``cancel()`` makes. ``check_no_back_links_exist`` calls ``check_if_doc_is_linked``
(``frappe/model/delete_doc.py:376``), which calls ``get_linked_docs(doc, method="Cancel")``
(``:301``) — walking ``get_link_fields(doc.doctype)``, i.e. exactly the three real edges above
(``amended_from`` explicitly excluded, ``:319``) — and raises ``frappe.LinkExistsError``
(``raise_link_exists_exception``, ``:474``) the instant any linked row's ``docstatus==1`` is found
(``:356``, ``DocStatus.is_submitted()``). No ``ignore_linked_doctypes`` is set anywhere (confirmed
absent from both the ``.py`` class body and the ``.json`` schema — grepped, zero hits). **The
governance value this broker's own blast-radius gate provides, named plainly: a raw native
``cancel()`` on a Production Plan with a submitted Work Order/Purchase Order/Material Request still
attached would run ``delete_draft_work_order``/``update_bin_qty``/``update_sales_order``/
``update_stock_reservation`` FIRST and only THEN throw** — the whole request is one DB transaction
so the side effects unwind with it, but a raw caller would see real work happen before an opaque
failure. This broker's own leaf ``plan_cancel`` refuses BEFORE any of that runs, naming the
blocking document by name — never a silent leaf assumption — and (unlike Delivery Trip)
``plan_cascade_cancel`` here is a genuinely WORKING governed path, not a second dead end: the three
edges are ordinary submittable dependents, walked and cancelled first, the plan cancelled last,
exactly the Pick List/Purchase-Order-dependent shape.

**TWO AUTO-SUBMIT CHANNELS off this doctype's own submit — the dossier caught only the WEAKER one,
inverted in three separate places (§6, §11, §12).**

1. **Material Request — a CALLER-FLAG auto-submit, correctly flagged by the dossier but graded
   imprecisely.** ``make_material_request()`` (``production_plan.py:961-1039``,
   ``@frappe.whitelist()`` instance method) builds one Material Request per (sales_order,
   material_request_type) key, ``.save()``s it (``:1027``), then ``if self.get(
   "submit_material_request"): material_request.submit()`` (``:1028-1029``). ``submit_material_
   request`` is CONFIRMED NOT a doctype field (``json.load`` over all 58 fields finds no such
   fieldname) — it is a transient client-side key: ``production_plan.js:345`` sets
   ``frm.doc.submit_material_request = submit`` inside ``create_material_request(frm, submit)``,
   wired to the "Make Material Request" button's confirm dialog (``:330-339``), and the whole
   in-memory ``frm.doc`` (including this non-schema key) is serialized onto the RPC. **Grade:
   deterministic from a CALLER-supplied, non-persisted flag — a governed caller controls this
   directly by whether it sets that key on the request, never by anything readable off the saved
   draft.** Disclosed as prose, not a data-driven gate (there is no field to read).
2. **Stock Reservation Entry — UNCONDITIONAL once ``reserve_stock=1``, a REAL schema Check field
   (confirmed in the 58-field enumeration, draft-readable) — the dossier said "auto-submitted: NO"
   in three places (§6, §11, §12); OVERTURNED.** ``on_submit`` (``:590-594``) →
   ``update_stock_reservation()`` (``:603-607``, gated only on ``self.reserve_stock``) → module
   ``make_stock_reservation_entries()`` (``:2216-2249``) → when ``doc.docstatus == 1`` (``:2242``),
   calls ``StockReservation(doc, ...).make_stock_reservation_entries()`` — a DIFFERENT method, on
   the ``StockReservation`` class (``stock_reservation_entry.py:1135-1211``), which builds each
   Stock Reservation Entry, calls ``sre.save()`` (``:1202``), then **``sre.submit()``
   unconditionally two lines later (``:1208``) — no flag gates it, no further ``if`` guards it.**
   Corroborating receipt: the CANCEL direction is fully symmetric and genuinely correct (no orphan
   left behind, unlike Pick List's own two-jaw trap) — ``doc.docstatus == 2`` (``:2246-2247``)
   calls ``StockReservation(doc).cancel_stock_reservation_entries()`` (the CLASS method,
   ``stock_reservation_entry.py:1100-1133``), which queries SREs with ``docstatus==1 &
   voucher_type==self.doc.doctype & voucher_no==self.doc.name`` (``:1106-1122``) and calls
   ``sre_doc.cancel()`` on each (``:1127``) — this only ever finds anything if the create side
   truly auto-submitted, the same corroboration logic the addendum itself used. **This is sharper
   than the Material Request case: SRE auto-submit has NO gate at all beyond ``reserve_stock``
   itself** (a plain doctype Check field, not a runtime flag) — every submit with
   ``reserve_stock=1`` and available qty creates *and submits* SREs, deterministically, off the
   draft alone.

**THE FORWARD CHECK — ``reserve_stock`` propagates onto every Work Order this plan creates
(``create_work_order``, ``:923-949``, ``wo.reserve_stock = self.reserve_stock`` at ``:934``), and
Work Order's OWN landing (the 21st supported doctype) does NOT disclose the consequence — A
CONFIRMED RETROACTIVE GAP, reported here, NOT silently fixed on Work Order's own row.** Work
Order's own module-docstring section (this file, "Breadth (Work Order)") names "Stock Reservation
Entries" exactly ONCE, as a bare noun inside the ``on_close_or_cancel`` counter-unwind sentence —
no mention anywhere of ``reserve_stock``, ``StockReservation``, or ``make_stock_reservation_
entries``, and Work Order carries no dedicated ``_work_order_submit_risk_flags``/``_work_order_
cancel_risk_flags`` function in ``tools.py`` at all (confirmed: grepped, none exist — only the
ledger-preview-unavailable flag does). **The real mechanism, traced past what the addendum's
forward-note describes ("will itself auto-create+auto-submit its own SREs" undersells it): when a
Work Order born from a ``reserve_stock=1`` Production Plan (carrying ``wo.production_plan`` =
this plan's name, set via ``production_plan.py:742``) is later submitted with ``reserve_stock``
still truthy, its own ``on_submit`` (``work_order.py:929-946``) calls ``update_stock_reservation``
(``:973-976``) → module ``make_stock_reservation_entries`` (``work_order.py:2386-2416``) — and
because ``doc.production_plan`` is set and ``is_transfer`` defaults ``True``, it takes the
TRANSFER branch, ``sre.transfer_reservation_entries_to(doc.production_plan, from_doctype=
"Production Plan", to_doctype="Work Order")`` (``stock_reservation_entry.py:1267-1377``) — NOT a
fresh, independent ``make_stock_reservation_entries()`` call. This TRANSFERS: for each of the
Production Plan's own already-submitted SREs with available reserved qty, it CREATES AND SUBMITS
(``sre.save(); sre.submit()``, ``:1444-1445``) a NEW Work-Order-voucher-typed Stock Reservation
Entry (carrying ``from_voucher_type``/``from_voucher_no`` provenance back to the source SRE), while
marking the SOURCE (Production-Plan-voucher-typed) SRE's own ``delivered_qty``/``status`` via a raw
``frappe.qb.update`` SQL statement (``update_delivered_qty``, ``:1381-1394``) — a bypass of the
source SRE's own lifecycle entirely, never a ``.cancel()``/``.save()`` call on it.** So the forward
consequence IS real (new SRE documents ARE created and submitted off a Work Order this broker
already governs) but the mechanism is a TRANSFER/reassignment of the Production Plan's own
reservation, not a repeated independent create — worth naming precisely for whoever remediates the
Work Order gap. (A second, useful fact surfaced along the way: ``StockReservation`` — the wrapper
class Work Order/Production Plan/Subcontracting Order/Purchase Receipt ALL share directly — is a
DIFFERENT SRE-creation family from Sales Order's/Pick List's own ``create_stock_reservation_
entries_for_so_items`` delegation (the ``Literal["Pick List", "Purchase Receipt"]``-typed function)
— two distinct SRE-creation mechanisms coexist in this ERPNext tree, both already surfaced by this
campaign, now pinned as two separate families rather than one.)

**Confirmed from source (2026-07-21 checkout):**

* **``party_field=None`` — confirmed.** The only header field shaped like a party is ``customer``
  (Link, options ``Customer``, ``json:120-126``), gated ``depends_on: "eval: doc.get_items_from ==
  \"Sales Order\""`` — a pure UI filter/display condition, never a GL party line (no Supplier field
  anywhere in the 58-field enumeration). ``company`` (Link, ``reqd: 1``, ``json:76-82``) is scope,
  not a party.
* **``status``/``grand_total``.** ``status`` fieldname confirmed byte-for-byte: options
  ``"\nDraft\nSubmitted\nNot Started\nIn Process\nCompleted\nClosed\nCancelled\nMaterial
  Requested"`` (8 real options), ``read_only: 1``, ``no_copy: 1``, ``search_index: 1``, default
  ``"Draft"`` (``json:297-305``) — PRESENT but NOT ``in_list_view`` (confirmed: no
  ``in_list_view`` key on this field at all). ``grand_total`` CONFIRMED ABSENT, with NO
  substitute — ``total_planned_qty``/``total_produced_qty`` (``json``, both ``Float``, ``default
  "0"``, ``read_only: 1``) are plain quantity counters, correctly left as Floats, never renamed
  into the grand_total slot (the dossier's own restraint, confirmed right).
* **A WRITABLE-STATUS SURFACE, the AML shape, worth naming plainly.** ``set_status`` (whitelisted
  instance method, ``:688-711``, signature ``(self, close=None, update_bin=False)``) is directly
  client-callable and carries NO ``docstatus`` guard of its own anywhere in its body: when ``close``
  is truthy it runs a raw ``self.db_set("status", "Closed")`` (``:693``, bypassing
  validate/hooks/version-history) and calls ``update_bin_qty()`` (a second document's own Bin
  writes) — callable at ANY docstatus, not just a submitted one. Nothing granted toward it by this
  landing (the standard "nothing granted" policy); a ``pacioli_guard``-scoped credential limited to
  ``Production Plan.submit``/``.cancel`` does not thereby gain ``set_status``.
* **``date_field="posting_date"`` — a plain ``Date`` (``reqd: 1``, default ``"Today"``,
  ``json:95-101``), REJOINING the large existing posting_date set, no new pattern.** Four more
  ``Date`` fields exist (``from_date``/``to_date``/``from_delivery_date``/``to_delivery_date``, all
  optional filters) — confirmed NONE are ``Datetime`` (every Date/Datetime-typed field in the
  58-field enumeration checked by hand). No ``allow_on_submit`` on any of the five (absence =
  Frappe's implicit ``0``).
* **``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading all 2261 lines of
  ``production_plan.py``: zero ``def submit``/``def cancel`` overrides** in the class body — only
  lifecycle hooks (``on_submit``/``on_cancel``/``on_discard``) plus 31 ordinary methods.
* **Ledger preview: UNCALLABLE, confirmed from the MRO.** ``class ProductionPlan(Document):``
  (``:41``) — a direct ``Document`` subclass, never ``AccountsController``/``StockController``; no
  ``make_gl_entries`` anywhere in the 2261-line file (grepped). ERPNext's own
  ``get_accounting_ledger_preview`` calling ``doc.make_gl_entries()`` bare would raise
  ``AttributeError`` on a live bench, so ``_tool_plan_submit`` skips the network call entirely —
  **joins the ledger_preview skip tuple in ``tools.py``, its TWENTY-NINTH member** (unchanged shape
  from every prior bare-``Document`` row).
* **NO deterministic-from-draft doomed-submit gate exists.** ``validate()`` (``:126-134``) calls
  ``validate_sales_orders()`` (``:145-172``, whitelisted, also called with no args from
  ``validate()``) — but its only throw sites fire off a LIVE query
  (``sales_order_query``/``linked_with``-style aggregation against Sales Order Item's own live
  rows) and are silently skipped whenever ``self.sales_orders`` is empty — cross-document, never
  draft-deterministic. ``add_reference_to_raw_materials()`` (``on_submit`` only, ``:609-630``) can
  throw at ``:626-630``, but the throw's own gating condition needs a LIVE
  ``frappe.get_cached_value("BOM", item.from_bom, "item")`` read (``:624``) — the "no matching
  sub_assembly_item found" half is draft-readable, but the final disqualifying comparison is not,
  so this stays a data-driven ARMING disclosure (readable off ``reserve_stock``/
  ``main_item_code``/``from_bom``), never a hard "will be REFUSED" gate.
* **Whitelist: 17 real callables — 9 instance + 8 module, independently re-derived by decorator
  indentation, matching the addendum's corrected roster exactly** (the dossier's own 10/7 split was
  wrong on BOTH tiers, not just the count — it double-listed ``download_raw_materials``, a
  module-level function with no ``self``, as its own 10th instance method, and its own module list
  was missing ``get_items_for_material_requests`` entirely). Instance (9, tab-indented
  ``@frappe.whitelist()``): ``validate_sales_orders`` (145), ``get_open_sales_orders`` (210),
  ``get_pending_material_requests`` (235), ``combine_so_items`` (291), ``get_items`` (316),
  ``set_status`` (688), ``make_work_order`` (775), ``make_material_request`` (961),
  ``get_sub_assembly_items`` (1041). Module (8, column-0 ``@frappe.whitelist()``):
  ``download_raw_materials`` (1206), ``get_bin_details`` (1588), ``get_so_details`` (1623),
  ``get_items_for_material_requests`` (1646, ~200-line, read-only — grepped its body for
  ``.save()``/``.submit()``/``db_set``/``frappe.db.set_value``/``insert()``/``new_doc``/
  ``delete_doc``, none found), ``get_item_data`` (1901), ``sales_order_query`` (2144),
  ``make_stock_reservation_entries`` (2215), ``cancel_stock_reservation_entries`` (2252). Nothing
  granted toward any of the 17.
* **``company`` PRESENT AND ``reqd: 1``** (``json:76-82``) — the standard wrong-books belt applies
  unconditionally; NOT one of the campaign's companyless rows.
* **``period_closing_doctypes``: confirmed ABSENT** — Production Plan is not among the 18-entry
  list (``hooks.py:326-345``). This broker's own closed-books check is therefore deliberately
  EQUAL-or-STRICTER than ERPNext (which never period-checks a Production Plan), the same accepted
  posture every dated non-GL doctype in this table already rides (Work Order/SO/PO/MR).
* **No async/enqueue anywhere** — confirmed by grepping ``enqueue`` in both ``production_plan.py``
  and ``production_plan.js``: zero hits. No scheduler entry in ``erpnext/hooks.py`` names
  Production Plan. Both heavy computations (``get_sub_assembly_items``, ``get_items_for_
  material_requests``) run synchronously in the request. This doctype needs no BOM-Creator-shaped
  PROVE redesign — a plain synchronous submit/cancel row, with two real auto-submit side channels
  instead of the dossier's one.
* **``in_list_view`` exact order, ``json.load``-derived, a dossier correction (the dossier's own
  order was wrong).** Real order: ``company``, ``get_items_from``, ``posting_date``, ``item_code``,
  ``customer`` — ``item_code`` (``json:111-116``) precedes ``customer`` (``json:120-126``), not the
  reverse the dossier's §2 table claimed. Forces the THIRTY-EIGHTH special ``_list_fields`` branch:
  5 real list-view columns (party_field=None yet a real, if conditional, ``customer`` Link rides
  literally; ``get_items_from`` a Select-typed filter member; no grand_total substitute; ``status``
  spliced per the Pick List precedent — PRESENT even though not itself ``in_list_view``-flagged).
* **Field-count arithmetic, a genuinely new breakdown the dossier never attempted:** 58 total =
  28 data fields (9 Check + 8 Link + 5 Date + 4 Select + 2 Float) + 23 structural (11 Section
  Break + 5 Column Break + 7 Button) + 7 table-shaped (6 Table + 1 Table MultiSelect).
* **Tallies: the 48th supported doctype (245->250 tools); skip tuple grows to its TWENTY-NINTH
  member (UNCALLABLE joins, the same bare-``Document`` shape); THIRTY-EIGHTH special
  ``_list_fields`` branch by direct count; genuinely NOT A LEAF (three real incoming edges, the
  cascade mechanism unchanged); a CONFIRMED RETROACTIVE GAP reported against Work Order's own
  landing (the ``reserve_stock``/Stock Reservation Entry forward-channel), banked as campaign debt
  like Asset Capitalization's own repost-machinery gap, not silently fixed here.

Breadth (Subcontracting Order) — the forty-ninth supported doctype, off a pre-verification
addendum (``docs/plans/dossiers/subcontracting_order.verify.md``, 2026-07-21) whose own
line-citation checks were unusually thorough (every ``on_submit``/``on_cancel``/whitelist range
inside ``subcontracting_order.py`` verified byte-for-byte) but whose central finding — THE
SEVEN-PATH MUTATOR MAP below — was still under-scoped in one place, and whose own cascade section
repeats a dossier framing this landing overturns. Confirmed directly against
``/root/.pacioli/refs/erpnext-16/erpnext/subcontracting/doctype/subcontracting_order/
subcontracting_order.{json,py}`` (57 fields, is_submittable:1), ``erpnext/controllers/
subcontracting_controller.py``, ``erpnext/subcontracting/doctype/subcontracting_receipt/
subcontracting_receipt.py``, ``erpnext/stock/doctype/stock_entry/stock_entry.py``,
``erpnext/stock/doctype/stock_reservation_entry/stock_reservation_entry.{json,py}``, and
``frappe/desk/form/linked_with.py`` — every citation below independently re-verified, never
inherited from the dossier, the addendum, or the landing briefing.

**THE SEVEN-PATH MUTATOR MAP — the row's center, all seven line cites re-verified byte-for-byte.**
A submitted Subcontracting Order's own ``status`` field and its ``Subcontracting Order Supplied
Item`` child rows (``consumed_qty``/``supplied_qty``/``returned_qty``/``total_supplied_qty``) are
rewritten by SIX permission-free paths and only ONE permission-checked path: (1) the order's own
``on_submit``/``on_cancel`` (``subcontracting_order.py:125-132``) calling ``update_status()``
(``:292-324``, ends ``self.db_set("status", status, ...)`` at ``:319``); (2) Subcontracting
Receipt's ``on_submit``/``on_cancel`` (``subcontracting_receipt.py:171``/``:195``) calling
``self.set_subcontracting_order_status(update_bin=False)`` (``:176``/``:206`` — defined
``subcontracting_controller.py:1272-1280``, loads EACH linked, already-submitted SCO and calls
``sco_doc.update_status(...)`` from a completely separate document's own transaction, ending in
the SAME ``db_set``); (3) the SAME SCR ``on_submit``/``on_cancel`` calling
``self.set_consumed_qty_in_subcontract_order()`` (``:177``/``:205`` — defined
``subcontracting_controller.py:1139-1160``, calling ``__update_consumed_qty_in_subcontract_order``
at ``:1121-1136``, which ends in a RAW MODULE-LEVEL ``frappe.db.set_value`` straight into the
child table at ``:1135-1137`` — no ``Document`` instantiation on either side of the relationship,
the sharpest honesty grade the campaign's own taxonomy has produced, worse than a
``.db_set()``/``.db_update()`` bypass since there is not even a loaded parent doc involved); (4)
Stock Entry's ``on_submit`` (``stock_entry.py:558``) calling ``reserve_stock_for_subcontracting``
(``:2420-2442``), which for ``purpose == "Send to Subcontractor"`` calls the WHITELISTED
``reserve_raw_materials()`` DIRECTLY as an in-process Python method call
(``stock_entry.py:2440-2442``) — the ``@frappe.whitelist()`` decorator is INERT here, it only
gates an HTTP hit, never an in-process call; (5) Stock Entry's ``on_submit``/``on_cancel``
(``:560``/``:581``) calling ``update_subcontract_order_supplied_items`` (``:3855-3888``), the SAME
raw ``frappe.db.set_value`` pattern into the SAME child table (``:3881``) — a THIRD independent
writer of it; (6) Stock Entry's ``on_submit``/``on_cancel`` (``:561``/``:582``) calling
``update_subcontracting_order_status`` (``:4054-4062``), which routes through the UNWHITELISTED
module function ``set_subcontracting_order_status`` (``subcontracting_order.py:486-490``)
DELIBERATELY, per an upstream source comment quoted verbatim: *"Trusted submit/cancel flow — a
Stock operation must not require Subcontracting Order write permission, so use the no-check
internal helper"* (``stock_entry.py:4060-4061`` — the sharpest single claim in this landing, and
the addendum's own headline citation). Only path (7) — the WHITELISTED module function
``update_subcontracting_order_status`` (``subcontracting_order.py:493-500``), reached via
``sco.check_permission("write")`` — carries any write-permission check at all. **Governance read:**
a governed Subcontracting Order's ``status`` and ``supplied_items`` quantities can move from
Subcontracting Receipt or Stock Entry submit/cancel activity with ZERO SCO-scoped audit event —
the Timesheet second-writer disclosure vocabulary, but WIDER (two trigger doctypes, three
mechanisms, not one doctype and one raw-write grade).

**Ledger preview: CALLABLE, honest-empty — the SALES/PURCHASE ORDER shape, NOT the skip tuple.**
``SubcontractingOrder -> SubcontractingController -> StockController -> AccountsController ->
TransactionBase -> StatusUpdater -> Document`` (``subcontracting_controller.py:27``,
``stock_controller.py:86``, ``accounts_controller.py:105``, ``transaction_base.py:20``,
``status_updater.py:181`` — 7 classes, 6 edges, every link independently re-verified) means
``StockController.make_gl_entries`` (``stock_controller.py:292-319``) genuinely EXISTS in the MRO
and is never overridden — calling it raises no ``AttributeError``, so this doctype does NOT join
the skip tuple. But ``SubcontractingOrder.on_submit()`` (``:125-128``) never calls
``update_stock_ledger()`` — no Stock Ledger Entry row is ever posted under this voucher's own
name — so ``get_gl_entries`` (the base ``StockController`` method, never overridden here) sources
its rows from ``get_stock_ledger_details()``, finds nothing keyed to this voucher, and returns an
empty list regardless of the ``need_inventory_map`` gate. The native preview RPC still succeeds
(the mechanical call is made, unlike Dunning/Production Plan's own ``AttributeError`` risk); it
simply has nothing to disclose — the identical shape Sales Order's/Purchase Order's own landings
already established, one MRO layer deeper (``SubcontractingController`` sits between
``StockController`` and the leaf class, a layer neither SO nor PO carries) and now the CALLABLE
category's deepest MRO in the campaign, surpassing Asset Capitalization's own 5-deep
(``AssetCapitalization -> StockController -> AccountsController -> TransactionBase ->
StatusUpdater -> Document``, 6 classes) chain by one link.

**THE CASCADE CORRECTION — this landing's own refinement, not inherited from either the dossier or
the addendum.** Both the dossier (§8) and the addendum's own "Confirmed load-bearing claims"
section repeat, without correcting, the framing that ``Subcontracting Receipt Item``/
``Subcontracting Receipt Supplied Item`` are "non-submittable... never appear in submitted-links
blast-radius reads" — conflating those CHILD doctypes' own ``is_submittable: 0`` flag with whether
the edge is walked by the gate at all, the EXACT mistake Production Plan's dossier made about
``Purchase Order Item``/``Material Request Item`` one row earlier in this campaign. Both child
doctypes are confirmed ``istable: 1`` under the SUBMITTABLE Subcontracting Receipt
(``is_submittable: 1``, ``subcontracting_receipt.json``), and frappe's own
``get_submitted_linked_docs`` resolves a child-table Link hit back to its submittable PARENT via
``get_parent_if_child_table_doc=True`` (``frappe/desk/form/linked_with.py:121``, ``:328-363``) —
the identical mechanism the Production Plan/Pick List/Timesheet landings already proved for other
doctype pairs. **Subcontracting Order is NOT a leaf on TWO independent submittable-referencer
families, not the dossier's implied one:** ``Stock Entry.subcontracting_order`` (a direct
header-level Link, ``stock_entry.json:186``) AND ``Subcontracting Receipt Item``/``Subcontracting
Receipt Supplied Item.subcontracting_order`` (child-table Links resolving to submittable
Subcontracting Receipt). This reinforces rather than contradicts THE SEVEN-PATH MUTATOR MAP above
— Subcontracting Receipt is already proven to reach deep into this order's own status/child rows
on its own submit/cancel; it would be an odd asymmetry for that same coupling to be invisible to
the cascade walk. (A full two-checkout grep for ``'"options": "Subcontracting Order"'`` finds
exactly the dossier's/addendum's own 4 hits — Stock Entry, the order's own ``amended_from``, and
the two Subcontracting Receipt child items; the frappe tree contributes zero — no missed
referencer, only a missed CONCLUSION from the same grep results.)

**A genuine orphan-reservation risk, this landing's own finding: cancel is NOT symmetric with
submit for a ``reserve_stock=1`` order, unlike Production Plan's own clean reversal.**
``on_cancel`` (``subcontracting_order.py:130-132``) calls only ``update_status()`` and
``update_subcontracted_quantity_in_po(cancel=True)`` — it does NOT call
``cancel_stock_reservation_entries()`` the way Production Plan's own ``on_cancel`` calls
``update_stock_reservation`` symmetrically. No ``ignore_linked_doctypes`` is set anywhere
(confirmed absent from both the ``.py`` class body and the ``.json`` schema). A live, submitted
Stock Reservation Entry this order created on submit (see below) is therefore NOT automatically
reversed on cancel — and it is invisible to this broker's own blast-radius gate too, since
``Stock Reservation Entry.voucher_no`` is a ``DynamicLink``, not a plain ``Link`` (confirmed by
its auto-generated type annotation, ``stock_reservation_entry.py``), so it never appears in a
``'"options": "Subcontracting Order"'`` grep and is structurally outside ``get_submitted_linked_
docs``' own graph. The Pick List two-jaw-trap shape, reproduced for a different doctype family:
cancelling can leave a live reservation dangling against a now-cancelled order unless a caller
separately invokes the whitelisted ``cancel_stock_reservation_entries()``
(``subcontracting_order.py:421-429``).

**``reserve_raw_materials`` is an ``on_submit`` SIDE EFFECT, not merely a whitelisted callable a
caller might separately invoke — the dossier's own RED FLAG 1 framing.** When ``reserve_stock`` is
set, ``on_submit`` (``:125-128``) itself calls ``reserve_raw_materials()`` (``:347-412``), which —
via the SAME shared ``StockReservation`` wrapper class Production Plan's own landing already
proved auto-submits with no gate — calls ``sre.save()`` then ``sre.submit()`` two lines later
(``stock_reservation_entry.py:1202``/``:1208``, doctype-agnostic, no ``if self.doc.doctype``
guard around either call) UNCONDITIONALLY for every reservable ``supplied_items`` row. This is not
disclosed by analogy — the shared class makes the finding directly applicable, confirmed by
reading ``make_stock_reservation_entries``'s own body. If this order carries a ``production_plan``
back-reference (a plain ``Data`` field, not a Link) and that plan's own SRE has unreserved qty
matching, this takes a TRANSFER path (``transfer_reservation_entries_to``,
``stock_reservation_entry.py:1267-1377``) instead of a fresh create — the PP -> SCO direction of
the SAME transfer mechanism Production Plan's own landing found running PP -> Work Order.

**SIX ``validate()`` throws across seven line cites, the dossier's own untouched surface.**
``validate()`` (``subcontracting_order.py:116-123``) chains ``validate_purchase_order_for_
subcontracting``/``validate_service_items``/``validate_supplied_items`` — TWO throws are
deterministic-from-draft (``:153`` no ``purchase_order`` set at all; ``:179`` a supplied item's
own ``reserve_warehouse`` collides with this order's own ``supplier_warehouse``) and become hard
REFUSED flags; FOUR need a LIVE read of the linked Purchase Order (``:139`` not
``is_subcontracted``; ``:142`` ``is_old_subcontracting_flow``; ``:146`` not submitted; ``:150``
``per_received == 100``) plus ONE needs a live ``Item.is_stock_item`` read (``:165``) and stay
cross-document prose, per the campaign's standing "cross-document state stays prose" rule.

**Cross-guards, both directions (addendum's own framing, confirmed).** Downstream:
``update_ordered_and_reserved_qty`` (``subcontracting_controller.py:1162-1180``, called from a
Subcontracting Receipt's own ``on_submit -> update_stock_ledger``) throws if the linked SCO's
``status`` reads Closed or Cancelled — the DN/PR-style downstream refusal. Upstream:
``SubcontractingOrder.update_status()`` itself (``:293-294``) throws via ``check_on_hold_or_
closed_status("Purchase Order", self.purchase_order)`` if this order's OWN status is already
Closed and something tries to change it while its linked Purchase Order is On Hold/Closed — a
cross-document guard in the other direction. Neither is in the dossier's RED FLAGS.

**``before_cancel``/``on_trash``, inherited from ``AccountsController``, checked and inert.**
``before_cancel`` (``accounts_controller.py:395-396``) calls ``validate_einvoice_fields``, a
base-app no-op (``:4361-4362``, literally ``pass``, an ``@erpnext.allow_regional`` stub);
``on_trash`` (``:486+``) does GL/PLE/repost cleanup, moot given this order's own honest-empty
ledger verdict.

**The reservation-family voucher_type peers — a correction the addendum flagged, confirmed and
carried forward precisely.** ``StockReservationEntry.voucher_type`` is a closed ``Select``/
``Literal`` of exactly ``{"", "Sales Order", "Work Order", "Subcontracting Inward Order",
"Production Plan", "Subcontracting Order"}`` (``stock_reservation_entry.json`` field
``voucher_type``, mirrored at ``stock_reservation_entry.py:66-72``) — Subcontracting Order is a
genuine ``voucher_type`` PEER of Production Plan/Work Order/Sales Order/Subcontracting Inward
Order, sharing the SAME ``StockReservation`` wrapper class. Pick List and Purchase Receipt are
``from_voucher_type`` TRANSFER SOURCES ONLY (``Literal["Pick List", "Purchase Receipt"]``,
``stock_reservation_entry.py:1578``) — never co-equal voucher types; stated here precisely so it
is never flattened into a false parallel.

**Field-count arithmetic, ``json.load``-derived:** 57 total = 18 structural (7 Column Break + 7
Section Break + 4 Tab Break) + 35 data fields (1 Check + 2 Currency + 3 Data + 2 Date + 1 Float +
16 Link + 1 Percent + 3 Select + 3 Small Text + 3 Text Editor) + 4 Table fields
(``additional_costs``/``items``/``service_items``/``supplied_items``).

**Corrections found (dossier + addendum):**

* The dossier's §3/§12 ``period_closing_doctypes`` line citation (``"line 57–74"``) is fabricated
  or stale — the real list lives at ``erpnext/hooks.py:326-345`` (Subcontracting Order confirmed
  absent, only Subcontracting Receipt is present, at line 344) — the addendum's own correction 1,
  re-verified.
* ``company`` (Link, ``reqd: 1``, ``subcontracting_order.json:132-141``) is never enumerated
  anywhere in the dossier's §1 or §12 summary table — a reportable silent gap per the campaign's
  own convention (the addendum's correction 2, re-verified) — Subcontracting Order is NOT
  companyless; the standard wrong-books belt applies unconditionally; the companyless tally stays
  at 6.
* THE CASCADE CORRECTION above (this landing's own finding, beyond the addendum) — Subcontracting
  Receipt Item/Supplied Item DO count as real submittable-referencer edges, via their submittable
  parent, not merely non-submittable dead ends.

**Tallies: the 49th supported doctype (250->255 tools); ledger preview stays CALLABLE (joins
Sales Order/Purchase Order's own honest-empty category, NOT the skip tuple, which stays at 29
members); THIRTY-NINTH special ``_list_fields`` branch by direct count (``total``, not
``grand_total`` — a genuine stand-in fieldname, not a missing aggregate); genuinely NOT A LEAF on
TWO independent submittable-referencer families (a correction to the dossier's/addendum's own
undercount); companyless tally unchanged at 6; date-pattern set unchanged (``transaction_date``
rejoins the existing set, no new pattern); deepest MRO in the CALLABLE ledger-preview category (6
edges, surpassing Asset Capitalization's 5).

Breadth (Subcontracting Inward Order) — the fiftieth supported doctype, off a pre-verification
addendum (``docs/plans/dossiers/subcontracting_inward_order.verify.md``, 2026-07-21) whose own
central finding — THE ELEVEN-ROW MUTATOR MAP below, plus a genuinely NEW bypass class this
campaign has not named before — the dossier never touched at all (this doctype is new in v16,
``"creation": "2025-03-24"`` per its own JSON, and the dossier is thin exactly where flagged: it
never opens ``erpnext/controllers/subcontracting_inward_controller.py``, a 1150-line dedicated
mixin, mixed into ``StockEntry`` itself — ``class StockEntry(StockController,
SubcontractingInwardController)``, ``stock_entry.py:92``). Confirmed directly against
``/root/.pacioli/refs/erpnext-16/erpnext/subcontracting/doctype/subcontracting_inward_order/
subcontracting_inward_order.{json,py}`` (31 fields, is_submittable:1),
``erpnext/controllers/subcontracting_inward_controller.py``, ``erpnext/controllers/
subcontracting_controller.py``, ``erpnext/stock/doctype/stock_entry/stock_entry.py``,
``erpnext/manufacturing/doctype/work_order/work_order.py``, ``erpnext/accounts/doctype/
sales_invoice/sales_invoice.py``, ``erpnext/selling/doctype/sales_order/sales_order.py``, and
``frappe/model/delete_doc.py`` — every citation below independently re-verified, never inherited
from the dossier, the addendum, or the landing briefing.

  * ``customer`` (Link -> Customer, ``reqd:1``, ``bold:1``) IS the header-level party field —
    ``party_field="customer"``, a plain GL party, no Dynamic Link, no dual-conditional pair
    (``customer_warehouse``/``customer_name`` are metadata, confirmed by the full 31-field
    census). ``status`` (Select, 8 options, ``read_only:1`` AND ``reqd:1``) is present; no
    ``grand_total``/``total`` stand-in exists anywhere — this doctype tracks OPERATIONAL PROGRESS
    ONLY, via six ``in_list_view:1`` Percent columns (``per_raw_material_received``/
    ``per_produced``/``per_process_loss``/``per_delivered``/``per_raw_material_returned``/
    ``per_returned``), the full 7-field ``in_list_view`` set alongside ``transaction_date``
    (``json.load``-confirmed). ``date_field="transaction_date"`` (Date, ``reqd:1``,
    default ``"Today"``, ``fetch_from="sales_order.transaction_date"``, ``fetch_if_empty:1``) —
    rejoins the existing transaction_date set as its TENTH member, no new pattern.
    ``submit_via=SUBMIT_VIA_RUN_METHOD`` — confirmed by reading the full 567-line ``.py``: no
    ``def submit``/``def cancel`` override anywhere. ``company`` (Link, ``reqd:1``) is present and
    load-bearing — never gets its own line in the dossier (no dedicated section, absent from its
    own summary table) — a silent gap corrected here; NOT one of the six companyless doctypes,
    the standard wrong-books belt applies unconditionally.

  * **THE NEW BYPASS CLASS — a genuinely new taxonomy slot beyond db_set/bulk_update/raw-SQL/
    clean-save/delegated-self-bypass: destructive ``frappe.delete_doc`` of DOCSTATUS=1 CHILD
    ROWS.** Three call sites in ``subcontracting_inward_controller.py`` —
    ``update_inward_order_received_items_for_raw_materials_receipt`` (:775, deletes a
    ``Subcontracting Inward Order Received Item`` row when its ``required_qty``/``received_qty``
    both net to zero), ``update_inward_order_received_items_for_manufacture`` (:852, deletes
    another Received Item row on the same zero-net condition for consumed additional items), and
    ``update_inward_order_secondary_items`` (:938, deletes a ``Subcontracting Inward Order
    Secondary Item`` row on cancel when produced qty nets to zero) — call ``frappe.delete_doc``
    on rows the SAME controller explicitly ``.submit()``s elsewhere in the identical file (:734,
    :892, :977), giving them ``docstatus=1``. This is legal, mechanically, only because NEITHER
    child doctype sets ``is_submittable`` in its own JSON (``istable:1``, ``is_submittable``
    ABSENT on both ``subcontracting_inward_order_received_item.json`` and
    ``subcontracting_inward_order_secondary_item.json`` — ``json.load``-confirmed on both), so
    frappe's own ``check_permission_and_not_submitted`` guard (``frappe/model/delete_doc.py:
    280-289``, gated on ``doc.meta.is_submittable`` — the exact source read, not an inference)
    never fires for them. **A docstatus=1 row of ``received_items``/``secondary_items`` on a
    submitted Subcontracting Inward Order is NOT durable** — any tool surface that reads these
    child tables as a snapshot must not treat their presence as permanent; named deliberately
    below wherever this broker's own risk-flag disclosures touch them.

  * **THE ELEVEN-ROW MUTATOR MAP — the row's center, all citations re-verified byte-for-byte.**
    This doctype creates no downstream financial documents of its own, but its OWN submitted
    child tables are written by FOUR OTHER doctypes through FIVE mechanisms, none of it visible
    from reading ``subcontracting_inward_order.py`` alone:

    (1) **Stock Entry**, via the ``SubcontractingInwardController`` mixin's ``on_submit_
    subcontracting_inward``/``on_cancel_subcontracting_inward`` (called from ``StockEntry.
    on_submit``/``.on_cancel`` at ``stock_entry.py:576``/``:618``), across FIVE purposes: (a)
    Receive from Customer recomputes ``received_items.received_qty``/``.rate`` (a weighted-
    average, ``subcontracting_inward_controller.py:700-786``) via ``frappe.db.bulk_update``, can
    insert+submit NEW Received Item rows for additional items (:733-734), and can ``delete_doc``
    a row that nets to zero (:775 — THE NEW BYPASS CLASS); (b) Manufacture consumption
    recomputes ``received_items.consumed_qty`` the same way (:787-852, bulk_update + insert/
    submit + delete_doc at :852); (c) Manufacture ALSO writes ``items.produced_qty``/
    ``.process_loss_qty`` via a cross-document ``db_set`` on the SCIO Item row
    (``update_manufacturing_qty_fields``, ``subcontracting_inward_order_item.py:39-52``, called
    via ``update_inward_order_item()`` at :642-651 — the MV-shaped bypass grade); (d)
    Subcontracting Delivery/Return write ``items.delivered_qty``/``.returned_qty`` via
    ``frappe.db.bulk_update`` (``update_inward_order_item()``, :652-671); (e) Manufacture
    secondary items write ``secondary_items.produced_qty`` via ``bulk_update``, can insert+submit
    NEW rows, and can ``delete_doc`` a row on cancel (:894-977, delete at :938 — THE NEW BYPASS
    CLASS again); (f) Return Raw Material writes ``received_items.returned_qty`` via
    ``bulk_update`` (:680-698); (g) EVERY purpose linked to a SCIO recomputes ``status`` in full
    via ``update_inward_order_status()`` -> ``set_subcontracting_inward_order_status()`` ->
    ``scio.update_status()`` -> ``db_set`` (:1130-1136) — a ``db_set`` bypass routed through the
    doctype's OWN method, but from a DIFFERENT document's transaction.

    (2) **Work Order**, via ``update_subcontracting_inward_order_received_items`` — a RAW
    ``frappe.qb.update(table).set(case_expr)...run()``, no ORM/``bulk_update`` wrapper at all
    (``work_order.py:1005-1038``), writing ``received_items.work_order_qty``, called at
    ``work_order.py:947`` (``on_submit``) and ``:971`` (``on_close_or_cancel``, reached from
    ``on_cancel``) — the QI-shaped raw-SQL bypass grade.

    (3) **Sales Invoice**, via ``update_billed_qty_in_scio`` — the SAME raw querybuilder ``UPDATE``
    shape, writing ``received_items.billed_qty`` (``sales_invoice.py:839-858``), called at
    ``:550`` (``on_submit``) and ``:684`` (``on_cancel``) — a SECOND independent raw-SQL writer,
    a different doctype from Work Order, the identical honesty grade.

    (4) **Sales Order**, via its OWN status transition: ``SalesOrder.update_status()``
    (``sales_order.py:602-612``) calls ``update_subcontracting_order_status()``
    (``:615-626``), which does ``update_scio_status(scio, "Closed" if self.status == "Closed"
    else None)`` — closing (or reopening) the PARENT Sales Order force-writes this SCIO's own
    ``status``, through the BARE module function ``set_subcontracting_inward_order_status``
    (no ``check_permission`` call at all, unlike the whitelisted variant) — a cross-document
    status cascade with ZERO permission check on the SCIO itself.

    Plus one ADJACENT writer, not on SCIO's own child rows but worth carrying alongside: Return
    Raw Material Stock Entries adjust ``Stock Reservation Entry``/``Serial and Batch Entry``
    ``delivered_qty`` via ``adjust_stock_reservation_entries_for_return``
    (``subcontracting_inward_controller.py:1039-1128``, called ``stock_entry.py:551``/``:606``) —
    a ``db_set`` bypass on a THIRD document type entirely.

    **Governance read:** of the four writer doctypes and five mechanisms above, only the
    WHITELISTED module function ``update_subcontracting_inward_order_status``
    (``subcontracting_inward_order.py:560-567``, gated on ``scio.check_permission("write")``)
    carries any write-permission check at all — every other path is a routine consequence of
    submitting/cancelling a DIFFERENT document. Disclosed both directions in the SCO/Timesheet
    multi-writer vocabulary, WIDER still (four trigger doctypes, five mechanisms, plus the new
    delete class — the widest mutator map this campaign has built).

    **Cancel-side gates, in the same mixin (also missing from the dossier's RED FLAGS):**
    ``validate_manufacture_entry_cancel`` (:582-640, three separate ``frappe.throw`` paths —
    ``produced_qty < delivered_qty`` on the linked SCIO Item; a secondary item's delivered
    exceeding produced-minus-reversal; an additional RM's billed exceeding consumed-minus-
    reversal — all reading LIVE state off this SCIO's own submitted child rows from inside Stock
    Entry's own cancel flow); ``validate_delivery`` (:469-486, refuses a Subcontracting Delivery
    cancel if ``returned_qty > delivered_qty``); ``validate_receive_from_customer_cancel``
    (:564-580, refuses a Receive-from-Customer cancel if a Work Order already exists against the
    quantity being reversed).

  * **Ledger preview: CALLABLE-but-ALWAYS-EMPTY — the POS Invoice false-positive shape, NOT flat
    CALLABLE (Sales/Purchase Order) and NOT Asset's per-cause conditional emptiness.**
    ``SubcontractingInwardOrder -> SubcontractingController -> StockController ->
    AccountsController -> TransactionBase -> StatusUpdater -> Document`` (7 nodes, every edge
    independently re-verified: ``subcontracting_controller.py:27``, ``stock_controller.py:86``,
    ``accounts_controller.py:105``, ``erpnext/utilities/transaction_base.py:20``,
    ``status_updater.py:181`` — ties Subcontracting Order for the deepest MRO in the CALLABLE
    category) means ``StockController.make_gl_entries`` genuinely EXISTS and is never overridden
    — calling it raises no ``AttributeError``, so this doctype does NOT join the skip tuple
    (stays at 29). But ``get_stock_ledger_details()`` (``stock_controller.py:923-948``) queries
    ``Stock Ledger Entry`` filtered on ``voucher_type == self.doctype`` AND ``voucher_no ==
    self.name`` — and this doctype's own ``on_submit``/``on_cancel``
    (``subcontracting_inward_order.py:72-78``) call ONLY ``update_status()``/``update_
    subcontracted_quantity_in_so()``, neither of which ever writes an SLE under this voucher's
    own name. All real stock movement is deferred onto the FOUR spawned Stock Entry documents
    (Receive from Customer / Return Raw Material to Customer / Subcontracting Delivery /
    Subcontracting Return, each built by a whitelisted ``make_*`` factory) whose OWN
    ``voucher_type`` is ``"Stock Entry"``, never this doctype's name. So ``sle_map`` is always
    ``{}``, ``gl_list`` is always ``[]`` — ``make_gl_entries()`` runs its full machinery and posts
    nothing, EVERY TIME, UNCONDITIONALLY (a structural property of the voucher_type/voucher_no
    keying, never case-by-case the way Asset's conditional emptiness is). The mirror image of POS
    Invoice's own finding: POS's preview is misleadingly NON-empty for a posting that never
    happens; this doctype's preview is misleadingly EMPTY for a document whose real accounting
    consequence (four spawned Stock Entries' worth of stock movement) is genuinely substantial —
    just deferred onto sibling documents this broker separately governs. ``plan_cancel`` needs no
    equivalent flag (its ``projected_reversal`` is a REAL bench read of the ``GL Entry`` table,
    correctly empty because nothing was ever posted under this voucher to reverse).

  * **``on_cancel`` is an override WITHOUT ``super()`` — one precise correction to the dossier's
    §7, which claimed "parent class ``document.cancel()`` will invoke parent's standard cancel
    sequence."** ``SubcontractingInwardOrder.on_cancel`` (``subcontracting_inward_order.py:76-
    78``) never calls ``super().on_cancel()`` — ``AccountsController.on_cancel`` is skipped
    entirely, not chained (mechanically a no-op for this doctype regardless, and per the ledger
    finding above there is no GL to reverse anyway — but the dossier's STATED MECHANISM was
    wrong, the same override-without-``super()`` skip SCO's own landing named for a different
    doctype).

  * **The HARD gate runs from the OTHER side — ``StockEntry.validate_closed_subcontracting_
    order``.** ``StockEntry.validate_closed_subcontracting_order`` (``stock_entry.py:1977-
    1983``), called from Stock Entry's own ``validate()`` (:316, every save/submit) AND its own
    ``on_cancel`` (:580), refuses to save/submit/cancel a Stock Entry once the linked SCIO's
    ``status`` reads ``"Closed"``, via ``check_on_hold_or_closed_status("Subcontracting Inward
    Order", order)`` (``erpnext/buying/utils.py:112-123``). That shared helper checks for
    ``"Closed"`` OR ``"On Hold"`` — but this doctype's own ``status`` Select never offers
    ``"On Hold"`` as a value (8 options, confirmed by ``json.load``), so the ``"On Hold"`` half
    of the shared helper is DEAD CODE specific to this doctype; only the ``"Closed"`` branch is
    ever reachable. This is a hard cross-document gate, corrected from the dossier's RED FLAG 2
    (which described only the soft, non-refusing side of ``update_status()``'s own
    ``check_on_hold_or_closed_status("Sales Order", ...)`` call).

  * **``ignore_linked_doctypes`` mis-citation, corrected.** The dossier's §7 cites
    ``subcontracting_inward_order.json:308`` for "``ignore_linked_doctypes``: empty list" — line
    308 is ``"links": []``, the Connections-tab UI metadata, an unrelated JSON key.
    ``ignore_linked_doctypes`` is a Python class attribute; grepped the full 567-line ``.py`` and
    it is not set anywhere — the doctype runs frappe's framework default "can't cancel while
    linked" check unmodified. A concept conflation, not a wrong conclusion (the practical
    behavior — framework default applies — happens to still hold).

  * **The ``validate()`` chain never reaches ``StockController.validate()``.**
    ``SubcontractingInwardOrder.validate()`` calls ``super().validate()``
    (:65) -> ``SubcontractingController.validate()`` (``subcontracting_controller.py:68-78``),
    whose ``if self.doctype in [...]`` branch (covering Subcontracting Order/Receipt/Inward
    Order) runs ``validate_items()``/``create_raw_materials_supplied_or_received()``/
    ``set_valuation_rate_for_rm()`` and does NOT call ``super().validate()`` in that branch —
    only the ``else`` branch (other doctypes reusing the mixin) chains up. So
    ``validate_duplicate_serial_and_batch_bundle``/``validate_inspection``/``validate_
    customer_provided_item``/``validate_putaway_capacity``/``validate_inventory_dimension_
    mandatory`` and everything in ``AccountsController.validate()`` never run for this doctype's
    own document — a gap the dossier never traced at all (shared family behavior, not
    doctype-specific, but undisclosed either way).

  * **``status`` — read_only:1 in the JSON but THREE-way backend-writable.** The Contract/ID
    read-only-status nuance, stated plainly: (1) this doctype's own ``update_status()``
    (:80-131, called from its own ``on_submit``/``on_cancel`` AND from every Stock Entry purpose
    touching it) ends in a plain ``db_set``; (2) the whitelisted module function ``update_
    subcontracting_inward_order_status`` (the only permission-checked path); (3) Sales Order's
    own force-close cascade (THE ELEVEN-ROW MUTATOR MAP, writer 4 above) — none of these three
    are blocked by the field's own ``read_only:1``, which only constrains the desk UI.

  * **Cascade — genuinely NOT A LEAF, two DIRECT header-level Links, no child-table resolution
    needed (simpler than Subcontracting Order's own landing).** A full two-checkout grep for
    ``'"options": "Subcontracting Inward Order"'`` finds exactly 3 hits: ``Work Order.
    subcontracting_inward_order`` (``work_order.json:660``, direct header Link, submittable
    parent), ``Stock Entry.subcontracting_inward_order`` (``stock_entry.json:734``, direct
    header Link, submittable parent), and this doctype's own ``amended_from`` self-reference
    (``subcontracting_inward_order.json:127``) — the frappe-16 tree contributes zero. Unlike
    Subcontracting Order's own landing (which needed frappe's ``get_parent_if_child_table_doc``
    resolution for a child-table edge), both real referencer Links here are plain, top-level,
    header fields — no conflation risk, no correction needed, a submitted dependent on either
    family blocks a leaf cancel outright via the standing blast-radius gate.

  * **The reservation family — a voucher_type PEER, per the closed Literal SCO's own landing
    pinned.** ``StockReservationEntry.voucher_type`` is exactly ``{"", "Sales Order", "Work
    Order", "Subcontracting Inward Order", "Production Plan", "Subcontracting Order"}`` — this
    doctype is a genuine PEER, not a transfer source like Pick List/Purchase Receipt. Its own
    role in the family: ``create_stock_reservation_entries_for_inward``
    (``subcontracting_inward_controller.py:1008-1037``) creates AND submits a Stock Reservation
    Entry per received item, UNCONDITIONALLY, for every "Receive from Customer" Stock Entry
    (no ``reserve_stock``-style gate on this doctype at all — a structural difference from SCO's
    own conditional channel); ``cancel_stock_reservation_entries_for_inward`` reverses them
    symmetrically from Stock Entry's own ``on_cancel`` (:591) — a CLEAN reversal, unlike SCO's
    own orphan-reservation risk.

  * **Whitelist: exactly 6** (:237/:325/:388/:429/:507 five instance ``make_*`` Stock-Entry/Work-
    Order factories — none of the five ever calls ``.submit()`` on what they build, the caller
    bears that responsibility — plus :560/:561 the one module-level whitelisted status API, the
    ONLY permission-checked mutator in the entire map above). Nothing granted toward any of them.

  * **NOT in ``period_closing_doctypes``** (``erpnext/hooks.py:326-345``, 18 entries,
    ``"Subcontracting Receipt"`` present at line 344, this doctype absent) — broker
    equal-or-stricter, the standing posture.

  * ``update_subcontracted_quantity_in_so()`` (:133-141) — the ONE mutator this doctype's own
    ``on_submit``/``on_cancel`` runs directly against another document (each linked Sales Order
    Item's ``subcontracted_qty``) — is a clean, full ``.save()`` (:141), the highest honesty
    grade in this campaign's own taxonomy (the MS precedent), never a bypass.

**Corrections found (dossier + addendum, all re-verified from source):**

* §5's flat "CALLABLE" ledger-preview verdict, corrected to CALLABLE-but-unconditionally-empty,
  the POS false-positive shape — the addendum's own correction 1, re-verified mechanically via
  ``get_stock_ledger_details()``'s ``voucher_type``/``voucher_no`` keying.
* §7's "parent class ``document.cancel()`` will invoke parent's standard cancel sequence" is
  wrong — ``on_cancel`` never calls ``super()`` — the addendum's own correction 2, re-verified.
* RED FLAG 2's soft-guard framing is incomplete — the real hard gate runs from ``StockEntry.
  validate_closed_subcontracting_order``, not from this doctype's own ``on_cancel`` — the
  addendum's own correction 3, re-verified, including the confirmed-dead "On Hold" branch.
* ``ignore_linked_doctypes`` cite (json:308) is a mis-cite of the unrelated ``"links": []`` key —
  the addendum's own correction 4, re-verified; the underlying framework-default conclusion still
  holds.
* §4's "line 1-568" is off by one (567 lines) — the addendum's own correction 5, minor, confirmed
  by direct ``wc -l``.
* ``company`` never gets its own line in the dossier's §1/§12 — the addendum's own gap-fill,
  re-verified: present, ``reqd:1``, not companyless.
* RED FLAG 1 named only the whitelisted API as the path to ``"Closed"`` — the addendum's own
  gap-fill names the SECOND channel (Sales Order's own force-close cascade), re-verified.

**Tallies: the 50th supported doctype (255->260 tools); ledger preview stays CALLABLE (joins the
POS Invoice false-positive category, NOT the skip tuple, which stays at 29 members); FORTIETH
special ``_list_fields`` branch by direct count (six ``per_*`` Percent columns, no ``grand_total``
substitute at all — the widest operational-tracking branch this campaign has built); genuinely
NOT A LEAF on two direct header-level Link families (no child-table resolution needed);
companyless tally unchanged at 6; ``transaction_date`` rejoins its exclusivity set as its TENTH
member; whitelist 6, nothing granted; THE NEW BYPASS CLASS (destructive delete of docstatus=1
child rows) is a genuinely new taxonomy slot, the widest mutator map this campaign has built (four
writer doctypes, five mechanisms, plus the delete class); MRO ties Subcontracting Order for the
deepest in the CALLABLE ledger-preview category (7 nodes).

Breadth (Subcontracting Receipt) — THE ROOF ROW, the fifty-first and final GOVERN landing of the
breadth campaign, off a pre-verification addendum
(``docs/plans/dossiers/subcontracting_receipt.verify.md``, 2026-07-21) carrying 2 corrections and 5
landing risks — the sharpest of which (the cancel back-link gate) is confirmed here, and a SIXTH,
genuinely new finding beyond both dossier and addendum is added: the native ledger-preview RPC
itself is source-traced to a live crash risk for this doctype specifically. Confirmed directly
against ``/root/.pacioli/refs/erpnext-16/erpnext/subcontracting/doctype/subcontracting_receipt/
subcontracting_receipt.{json,py}`` (80 fields, is_submittable:1, 1129 lines),
``erpnext/controllers/subcontracting_controller.py``, ``erpnext/controllers/stock_controller.py``,
``erpnext/subcontracting/doctype/subcontracting_order/subcontracting_order.py``,
``erpnext/manufacturing/doctype/job_card/job_card.py``, ``erpnext/hooks.py``, and
``frappe/model/document.py``/``frappe/model/delete_doc.py`` — every citation below independently
re-verified, never inherited from the dossier, the addendum, or the landing briefing.

**THE SIXTH LEDGER-PREVIEW SHAPE — a live crash risk, this landing's own finding, sharper than the
addendum's own headline.** ``SubcontractingReceipt -> SubcontractingController -> StockController
-> AccountsController -> TransactionBase -> StatusUpdater -> Document`` (7 nodes, every edge
independently re-verified: ``subcontracting_controller.py:27``, ``stock_controller.py:86``,
``accounts_controller.py:105``, ``erpnext/utilities/transaction_base.py:20``,
``status_updater.py:181``) means ``StockController.make_gl_entries`` (``stock_controller.py:292``,
called ``subcontracting_receipt.py:184``) genuinely EXISTS and calls SCR's own ``get_gl_entries``
override (``:708-718``, via ``make_item_gl_entries`` at ``:720-909``) — this doctype does NOT join
the skip tuple, and posts BOTH real Stock Ledger Entry (``update_stock_ledger``, an UNCONDITIONAL
override, ``subcontracting_controller.py:1199-1239``) AND conditional GL on a genuine submit — a
both-ledgers row like Delivery Note/Purchase Receipt/Stock Entry. But ERPNext's own native preview
RPC, ``get_accounting_ledger_preview`` (``stock_controller.py:2090-2119``, reached via the
whitelisted ``show_accounting_ledger_preview``, ``:2059-2070`` — this broker's own
``PREVIEW_METHOD``), only pre-seeds an in-memory ``update_stock_ledger()`` call for the THREE
doctypes named in its own literal whitelist tuple — ``("Purchase Receipt", "Delivery Note", "Stock
Entry")``, line 2109 — or for any doctype carrying a truthy ``update_stock`` field (Sales/Purchase
Invoice's own toggle). **Subcontracting Receipt is confirmed ABSENT from that tuple** (direct string
comparison) **and carries no ``update_stock`` field either** (confirmed absent from the 80-field
enumeration) — so neither half of that line's ``or`` fires for it, and the preview calls
``doc.make_gl_entries()`` (line 2112) WITHOUT ever seeding a real Stock Ledger Entry row for this
still-draft voucher first. This alone would only reproduce Stock Reconciliation's own already-landed
"quiet dishonest empty" shape (its own ``get_gl_entries`` finds no SLE rows and returns ``[]``
cleanly, ``tools.py``'s own ``_stock_reconciliation_ledger_preview_incomplete_flag``) — but SCR's
OWN ``make_item_gl_entries`` goes one step further and CRASHES instead of returning quietly.
``stock_value_diff = frappe.db.get_value("Stock Ledger Entry", {...}, "stock_value_difference")``
(``subcontracting_receipt.py:741-751``) returns Python ``None`` when no matching row exists (the
draft's own case, since only a real submit's ``update_stock_ledger()`` ever writes that row) — and
at ``:847``, ``if divisional_loss := flt(item.amount - stock_value_diff, item.precision("amount")):``
evaluates the bare Python subtraction ``item.amount - stock_value_diff`` BEFORE ``flt()`` ever gets
a chance to coerce the ``None`` (unlike ``:778``'s ``flt(stock_value_diff) - service_cost``, which
IS safe — ``flt(None)`` returns ``0.0`` by frappe's own documented contract, confirmed against
``frappe/utils/data.py``'s own ``@typing.overload def flt(s: None) -> Literal[0.0]``). Subtracting
``None`` from a Python ``float`` raises ``TypeError: unsupported operand type(s) for -: 'float' and
'NoneType'`` — confirmed by direct interpreter reproduction of the exact expression shape, not
merely inferred from reading. **This is CONDITIONAL, not unconditional** — it only fires when
``need_inventory_map`` is true (``stock_controller.py:300-302``: real stock items AND perpetual
inventory enabled for the company) AND ``_inv_dict.get("account")`` resolves a real inventory
account for at least one item's warehouse (``:740``) — but that is the EXACT SAME configuration
under which a real submit of this SAME draft would post genuine, meaningful GL rows, i.e. the
precise case this broker's own preview exists to show correctly. Under a company WITHOUT perpetual
inventory, both the real submit AND the preview correctly, honestly return empty (SCR's own
``get_gl_entries`` early-returns ``[]`` at line 712 for that same case) — no dishonesty there, only
under the perpetual-inventory branch does the preview diverge from "empty" into "raises." Disclosed
data-driven, unconditionally scoped to this doctype (:func:`pacioli.tools.
_subcontracting_receipt_ledger_preview_gap_flag`, submit-direction), matching Stock Reconciliation's
own disclosure shape as closely as the underlying mechanism allows — but because the failure mode is
a live exception raised INSIDE the (uncaught) ``client.ledger_preview()`` call rather than a returned
value, the disclosure fires reliably in this broker's own FakeClient-backed test suite (which never
executes real ERPNext Python) and serves as the mechanism's permanent documentation, but cannot by
construction catch or soften a live crash on a real bench — that remains OWED, flagged here for the
supervisor's own decision (wrap ``client.ledger_preview()`` defensively, or accept as a known,
source-traced landing risk pending live-prove), never silently patched by this landing. Knowledge-
pinned from source, not live-verified, per the campaign's own standing discipline for claims that
require a live bench to fully settle.

**THE CANCEL BACK-LINK GATE — the addendum's own sharpest correction, confirmed exactly.** The
dossier's §7 enumerates only the explicit calls inside SCR's own custom ``on_cancel()``; it misses
that ``frappe.model.document.Document.run_post_save_methods()`` runs ``self.run_method("on_cancel")``
THEN ``self.check_no_back_links_exist()`` for the cancel action (``frappe/model/document.py:
1450-1452``, confirmed verbatim), which calls ``check_if_doc_is_linked(self, method="Cancel")``
(``document.py:1572-1577``) — raising ``frappe.LinkExistsError`` (``delete_doc.py:474-487``) if ANY
submitted document links to this SCR via a static Link field whose parent doctype is not in
``self.ignore_linked_doctypes`` (``get_linked_docs``, ``delete_doc.py:301-373``). SCR's own
``ignore_linked_doctypes`` tuple (``subcontracting_receipt.py:196-201``) is exactly ``("GL Entry",
"Stock Ledger Entry", "Repost Item Valuation", "Serial and Batch Bundle")`` — **"Purchase Receipt" is
absent**, and ``Purchase Receipt.subcontracting_receipt`` IS a real ``Link`` field (``options:
"Subcontracting Receipt"``, confirmed in ``purchase_receipt.json``). SCR's own
``auto_create_purchase_receipt()`` (``:961-963``, gated on the ``Buying Settings`` single
``auto_create_purchase_receipt``, ``make_purchase_receipt(self, save=True, notify=True)`` — no
``submit`` kwarg passed at all, an UNSTATED default-False, not an explicit kwarg, the addendum's own
minor correction 5) creates exactly that sibling, save-only — but once a USER separately submits it,
cancelling the original SCR is refused at the framework layer, independent of and in ADDITION to the
SCO-closed throw the dossier does document. One exemption in the same code path:
``link_field == "amended_from" and method == "Cancel"`` is hardcoded-skipped
(``delete_doc.py:319`` — the addendum's own citation says ``:318``, off by one from a direct
``grep -n``, a minor presentation slip, not a wrong conclusion), so the self-referencing
``amended_from`` edge never blocks cancel — but ``return_against`` gets NO such exemption, so a
submitted Return SCR blocks cancelling its original too. Disclosed as cross-document prose (never
deterministic-from-draft — the referenced Purchase Receipt's own docstatus isn't knowable from the
SCR draft alone), the same grading the DN/PR precedent already established for this class of gate.

**THE ORDERING WRINKLE — the broker's OWN gate is the cleaner, FIRST-fired refusal; ERPNext's
framework-level check is the second-line safety net, per PP/DT's own standing value.**
``Purchase Receipt.subcontracting_receipt`` is one of the row's own 3 cascade edges (above) — a
real, plain, header-level Link on a submittable doctype, discovered natively by ERPNext's own
``get_submitted_linked_docs`` (the SAME generic, doctype-blind walk ``plan_cancel``'s own
blast-radius disclosure already calls, zero new ``cascade.py`` code needed). So in ORDINARY
sequential use, this broker's OWN gate already refuses ``plan_cancel``/``cancel_subcontracting_
receipt`` cleanly the moment a submitted Purchase Receipt exists — BEFORE any cancel is even
attempted, hence before ``on_cancel``'s own side effects (GL reversal, the SCO writeback, status
writes) would ever run. ERPNext's own framework-level ``check_no_back_links_exist()`` runs the
OPPOSITE way — AFTER ``on_cancel`` (``document.py:1450-1452``'s own literal ordering) — so it only
becomes the OPERATIVE refusal in a TOCTOU race: the Purchase Receipt gets submitted in the narrow
window AFTER ``plan_cancel`` already returned clean but BEFORE the actual cancel call lands. Frappe's
own request-transaction semantics still make this SAFE (an exception raised after ``on_cancel``
rolls the whole request's transaction back, so no half-cancelled state persists) — but it is a real
ordering divergence between "our gate never even attempts the doomed operation" and "their gate
catches it after the attempt, saved only by transaction rollback," the value this broker's own
PLAN-before-CONSENT discipline exists to provide, stated precisely rather than left implicit.

**Whitelist: exactly 6, the addendum's own correction 1, confirmed by ``grep -c
"@frappe.whitelist()"``.** ``reset_raw_materials`` (:215/216), ``get_secondary_items`` (:379/380),
``set_missing_values`` (:457/458), ``make_subcontract_return_against_rejected_warehouse``
(:977/978), ``make_subcontract_return`` (:984/985), ``make_purchase_receipt`` (:991/992, module-
level, creates a NEW Purchase Receipt sibling — see THE CANCEL BACK-LINK GATE above; never mutates
self). The dossier's §9 lists 5 and discusses ``make_purchase_receipt`` in a separate paragraph
without folding it into its own count — the named recurring failure mode
(LANDING-TEMPLATE.md: "the decorator is per-def, look at each"). **SUPERVISOR RULING, applied as
written:** the standing own-module count convention STANDS (6, not counting inherited surface) —
BUT the inherited whitelisted instance method ``SubcontractingController.get_current_stock``
(``subcontracting_controller.py:1313-1314``, internally guarded ``if self.doctype in ["Purchase
Receipt", "Subcontracting Receipt"]`` — explicitly written to be callable on SCR instances too, via
MRO) gets this explicit note as a deliberate scoping statement: the count is per-module by
convention, the inherited surface is named so the choice stays visible rather than silent.
**Purchase Receipt's own landed row did NOT note ``get_current_stock``** — confirmed by a grep of
both ``erpnext.py`` and ``tools.py`` for the string, zero hits anywhere in either file, and zero
hits in Purchase Receipt's own dossier/addendum either — a gap in that prior landing, reported here
as a gap to note, not fixed retroactively by this row.

**THE SCO WRITEBACK, FROM THIS SIDE — mirroring Subcontracting Order's own landed SEVEN-PATH MUTATOR
MAP paths 2-3, grades confirmed identical, PLUS two channels that landing did not enumerate.**
Subcontracting Order's own landing (``8c0ba75``) named exactly two reach-backs from SCR into SCO:
path 2, ``self.set_subcontracting_order_status(update_bin=False)`` (``subcontracting_receipt.py:
176``/``206``) -> ``SubcontractingController.set_subcontracting_order_status``
(``subcontracting_controller.py:1272-1281``) -> ``sco_doc.update_status(...)`` -> ``self.db_set(
"status", status, update_modified=update_modified)`` (``subcontracting_order.py:292-321``, the
``db_set`` itself at ``:319`` — confirmed verbatim, matching SCO's own citation exactly); path 3,
``self.set_consumed_qty_in_subcontract_order()`` (``:177``/``205``) -> ``subcontracting_controller.py
:1139-1160`` -> ``__update_consumed_qty_in_subcontract_order`` (``:1121-1137``) -> a RAW module-level
``frappe.db.set_value`` straight into ``Subcontracting Order Supplied Item`` rows at ``:1135-1137`` —
no ``Document`` instantiation on either side, the campaign's rawest grade, confirmed identical to
SCO's own citation. **TWO MORE genuine channels SCO's own landing never enumerated, found here:**
(a) ``update_prevdoc_status()`` (called ``:174``/``203``, a ``StatusUpdater`` method via SCR's own
``status_updater`` config set in ``__init__`` at ``:100-113``, targeting ``Subcontracting Order
Item.received_qty``/``Subcontracting Order.per_received``) -> ``update_qty()`` ->
``_update_children()`` writes ``Subcontracting Order Item.received_qty`` via a RAW
``frappe.db.sql`` UPDATE (``status_updater.py:597-602`` — the Quality-Inspection-shaped raw-SQL
grade, not a ``db_set``) -> ``_update_percent_field_in_targets()`` -> ``_update_percent_field()``
writes ``Subcontracting Order.per_received`` via ``target.db_set(update_data, ...)`` on a freshly
loaded lazy doc (``status_updater.py:676-682`` — a ``db_set`` bypass, the SAME grade as path 2 but a
DIFFERENT call site); (b) ``update_stock_ledger()`` (``:183``/``207``) itself opens with
``self.update_ordered_and_reserved_qty()`` (``subcontracting_controller.py:1200``) ->
``update_ordered_and_reserved_qty`` (``:1162-1180``) throws if the linked SCO's own status reads
Closed/Cancelled (the DN/PR-style downstream refusal, confirmed exactly matching SCO's own landed
citation of this same method) and otherwise calls ``sco_doc.update_ordered_qty_for_subcontracting``/
``update_reserved_qty_for_subcontracting`` — BIN-level writes into a THIRD structure (Bin rows) this
broker does not separately govern at all. **Governance read, widened from SCO's own two-path
citation to FOUR:** a submitted SCR's own submit/cancel writes SCO's own ``status`` (twice, two
different call chains, both ending in ``db_set``), SCO Item's ``received_qty`` (raw SQL), SCO's own
``per_received`` (a second, independent ``db_set``), SCO Supplied Item's ``consumed_qty`` (raw
``frappe.db.set_value``), and SCO's own Bin-level ordered/reserved quantities — none of it
permission-checked from SCR's own side, the SAME zero-audit-event shape SCO's own landing already
named, now proven WIDER from the writer's own side of the relationship.

**``update_status()`` OWN status write — a RAW ``frappe.db.set_value``, rawer than a ``self.db_set``
bypass.** ``update_status(self, status=None, update_modified=False)`` (``:683-706``, called from
``on_submit``/``on_cancel`` at ``:186``/``:211``) ends: ``if status: frappe.db.set_value(
"Subcontracting Receipt", self.name, "status", status, update_modified=update_modified)``
(``:704-706``) — a MODULE-LEVEL ``frappe.db.set_value`` call naming the doctype+name explicitly,
never ``self.db_set(...)``. ``status`` is ``read_only:1`` AND ``reqd:1`` in the JSON
(``json:378-390``), but the JSON flag only constrains the desk UI — this write bypasses it
regardless, on the document's OWN name (not a cross-document write, but the rawest-grade mechanism
for a doctype's own status field this campaign has found for a same-document write).

**Repost channel — SCR joins the disclosure family, per AC's own landed shape; joining does NOT
clear the banked 4-row debt.** ``repost_future_sle_and_gle()`` — called from BOTH ``on_submit``
(``:185``) and ``on_cancel`` (``:210``), inherited from ``StockController``
(``stock_controller.py:1830-1854``) — can create a "Repost Item Valuation" document whenever
``future_sle_exists(args)`` or ``repost_required_for_queue(self)``, processed by ERPNext's OWN
scheduled reposting job (``repost_item_valuation.run_parallel_reposting``, every 30 minutes,
``hooks.py:437-438``, and ``repost_entries`` in ``hourly_maintenance``, ``hooks.py:452`` — the
former DOES call ``frappe.enqueue``, ``repost_item_valuation.py:644``), entirely outside any call
this broker makes — the Asset Capitalization precedent, disclosed data-driven here
(:func:`pacioli.tools._subcontracting_receipt_submit_risk_flags` /
``_subcontracting_receipt_cancel_risk_flags``). Asset Capitalization's own landing found the SAME
mechanism live, unflagged, on FOUR already-shipped rows — Stock Entry, Stock Reconciliation,
Delivery Note, Purchase Receipt — and named it explicitly as banked campaign debt, the supervisor's
call, not silently fixed by that landing. **This row joining the disclosure family does NOT clear
that debt** — SE/SR/DN/PR still carry zero dedicated risk-flag function for this channel; the RED
FLAGS section's blanket "no async channels" framing from the dossier does not ship here either, the
same correction Asset Capitalization's own landing already established as the standing rule.

**Returns dimension — ZERO new code needed, the existing DN/PR-shaped branch already applies
correctly by field presence.** ``is_return``/``return_against`` are both present
(``json:63``/``:78``); ``update_outstanding_for_self`` is confirmed ABSENT from the full 80-field
enumeration — the SAME stock-only shape Delivery Note's and Purchase Receipt's own landings already
built :func:`pacioli.tools._return_risk_flags`'s field-presence branch for, called unconditionally
at both ``plan_submit``/``plan_cancel`` call sites already (``tools.py:7708``/``:8458``) — SCR
exercises that identical branch the moment it reaches the generic dispatch, no doctype-specific
change required. ``status`` carries ``Return``/``Return Issued`` as two of its six real options
(``update_status``, ``:690-693``: ``Return`` when ``is_return``, ``Return Issued`` when
``per_returned == 100`` on the ORIGINAL); ``make_subcontract_return``/
``make_subcontract_return_against_rejected_warehouse`` (both whitelisted, counted above) are the two
factory entry points, both delegating to ``sales_and_purchase_return.make_return_doc`` — the same
machinery DN/PR's own return factories use.

**``update_job_card`` — both directions, a ``db_set`` bypass on a THIRD governed doctype, recomputed
fresh each time rather than incrementally reversed.** ``update_job_card()`` (``:224-228``, called
``on_submit``/``on_cancel`` at ``:188``/``:213``) calls ``Job Card.set_manufactured_qty()``
(``job_card.py:204-227``) for every item row carrying a ``job_card`` — that method reruns a LIVE SUM
query over all SUBMITTED Subcontracting Receipt Items sharing that Job Card (``docstatus == 1``,
``job_card.py:204-218``) and writes the result via ``self.db_set("manufactured_qty", ...)``
(``:221``) then ``self.set_status(update_status=True)`` (a further ``StatusUpdater.db_set`` if the
recomputed status differs, ``status_updater.py:214-216``) — cancel's own "reversal" is NEVER an
explicit subtract, only the natural consequence of the cancelled SCR's own rows falling out of the
live ``docstatus == 1`` sum on the NEXT recompute.

**Other on_submit machinery, graded.** ``validate_bom_required_qty()`` (``:608-646``, called
``on_submit`` line 173) is gated on live ``Buying Settings`` singles (skips only when backflush mode
is "Material Transferred for Subcontract" AND ``validate_consumed_qty`` is falsy) and, when it runs,
compares ``self.supplied_items``' own consumed qty against a LIVE read of the linked BOM's exploded
materials (``_get_materials_from_bom``) — CROSS-DOCUMENT PROSE, not deterministic-from-draft (needs
both a live Buying Settings read and a live BOM read). ``update_stock_reservation_entries()``
(inherited, ``stock_controller.py:1899-2019``) fires for SCR when ``self.has_reserved_stock() and
not self.is_return`` (``:1956``) — SCR is a genuine member of the reservation family's OWN data map
(``:1942-1946``: ``table_name="supplied_items"``, ``voucher_type="Subcontracting Order"``,
``field="consumed_qty"``) — it loads each live, submitted Stock Reservation Entry reserved against
the linked SCO and mutates ``consumed_qty`` (plus per-serial/batch ``delivered_qty`` on SRE child
rows) via ``entry.db_update()``/``sre_doc.db_update()`` (``:1982``/``:2014`` — a ``db_update`` bypass
on a THIRD document), THEN cascades into ``sre_doc.update_reserved_qty_in_voucher()``/
``update_status()``/``update_reserved_stock_in_bin()`` (``:2015-2017``) — a chain of further writes
this broker does not separately govern, the SAME "SE adjusts SRE via the mixin" shape SCIO's own
landing already found for a different doctype pair, here on SCR's own forward-consumption path
rather than a return. ``make_bundle_using_old_serial_batch_fields()`` (inherited,
``stock_controller.py:371+``, called for both ``items``/``supplied_items`` at ``:179-180``) is the
SAME legacy serial/batch-field migration shim Purchase Receipt's/Delivery Note's own landings already
document — housekeeping (Serial and Batch Bundle creation, ``do_not_submit`` by default), not a new
gate. ``delete_auto_created_batches()`` (inherited, ``stock_controller.py:993+``, called from
``on_cancel`` line 212) soft-cancels those same bundles on cancel — the identical mechanism Delivery
Note's own landing already documented, unchanged here.

Confirmed from source (2026-07-21 checkout):

* **``party_field="supplier"``** (Link -> Supplier, ``reqd:1``, label "Job Worker",
  ``json:114-124``/``:117``) — the sole party-shaped field across all 80 fields; ``supplier_name``
  (``:125-134``) is a fetched, read-only Data field, never a Link.
* **``status``/``total``/``in_list_view``:** Select, ``reqd:1``, ``read_only:1``
  (``json:378-390``), options ``\nDraft\nCompleted\nReturn\nReturn Issued\nCancelled\nClosed`` — a
  LEADING BLANK option (the ``\n``-first string, matched verbatim). No ``grand_total`` field exists
  anywhere in the 80; ``total`` (Currency, read_only, ``json:337-342``) is the stand-in aggregate,
  correctly not renamed into the slot. ``in_list_view: 1`` is EXACTLY ``{posting_date, per_returned}``
  — no third field, confirmed by full-field grep, not eyeballing.
* **``date_field="posting_date"``** (Date, ``reqd:1``, default "Today", ``json:143-152``) — the PRIMARY
  GL posting date (``get_gl_entries``/``update_stock_ledger`` both source rows keyed to it via the
  base SLE/GL mechanics), paired with ``posting_time`` (Time, not Datetime, ``:154-165``). **Zero
  Datetime-typed fields exist on this doctype** — confirmed by scanning all 80 fields for
  ``fieldtype in (Date, Datetime)``: only ``posting_date``, ``bill_date`` (Date, hidden, ``:363-367``,
  metadata), and ``lr_date`` (Date, "Vehicle Date", ``:516-521``, metadata) — ``bill_date``/``lr_date``
  are non-pins, never GL-relevant. The same DEFAULT path Delivery Note/Purchase Receipt/Stock
  Entry/Asset Capitalization already ride.
* **``company``** (Link, ``reqd:1``) — present and required; a standard companied row, not one of the
  six companyless doctypes; the standard wrong-books belt applies unconditionally.
* **``submit_via=SUBMIT_VIA_RUN_METHOD``** — confirmed by reading the full MRO (leaf class + 3
  ancestors incl. ``controllers/accounts_controller.py``): a grep for ``def submit`` / ``def
  cancel`` returns zero hits across all four files; only ``on_submit``/``on_cancel`` hooks exist.
* **Cascade — 3 edges, both-tree scan clean, matching the dossier/addendum exactly.** A full grep
  for ``'"options": "Subcontracting Receipt"'`` over BOTH erpnext-16 AND frappe-16 returns exactly
  the two files already found (``purchase_receipt.json``, ``subcontracting_receipt.json``) and
  exactly three edges: ``Purchase Receipt.subcontracting_receipt`` (a real, external, submittable-
  parent Link — see THE CANCEL BACK-LINK GATE above), ``Subcontracting Receipt.amended_from``
  (self, hardcoded cancel-exempt), ``Subcontracting Receipt.return_against`` (self, NOT exempt). The
  frappe-16 tree contributes zero additional hits — a clean scan, the second in a row after SCIO's
  own (the running dossier-error streak stays broken at two, not extended).
* **``period_closing_doctypes``: Subcontracting Receipt is the list's LAST entry, ``hooks.py:344``,
  the EIGHTEENTH and final member** (``hooks.py:326-345``, direct count) — natively EQUAL to this
  broker's own closed-books check (ERPNext really does period-check SCR saves), the Asset/Asset
  Repair/Invoice Discounting/Asset Capitalization precedent, **the FIFTH such row** — live-counted
  against the current source rather than assumed: Production Plan/Subcontracting Order/
  Subcontracting Inward Order (the three most recently landed rows) are all confirmed ABSENT from
  this same 18-entry list, so the tally held at 4 until this row.
* **MRO — 7 nodes, confirmed, ties Subcontracting Order/Subcontracting Inward Order for the deepest
  in the CALLABLE category.** ``SubcontractingReceipt -> SubcontractingController ->
  StockController -> AccountsController -> TransactionBase -> StatusUpdater -> Document`` — every
  edge independently re-verified by reading each class definition line.

**Corrections found (dossier + addendum, all re-verified from source; one beyond both):**

* Whitelist count corrected 5 -> 6 (the addendum's own correction 1, re-verified by
  ``grep -c``) — ``make_purchase_receipt`` folded into the count.
* THE CANCEL BACK-LINK GATE (the addendum's own correction 2) — a real framework-level REFUSE gate
  the dossier's §7 never named, confirmed exactly as the addendum states (one line-citation
  presentation slip corrected: ``delete_doc.py:319``, not ``:318``).
* THE SIXTH LEDGER-PREVIEW SHAPE — this landing's own finding, beyond both dossier and addendum:
  the native preview RPC is source-traced to a conditional live crash (not merely an incomplete or
  misleading result) for this doctype specifically, because SCR is absent from
  ``get_accounting_ledger_preview``'s own SLE-seeding whitelist AND its own GL-building arithmetic
  subtracts the resulting ``None`` before any ``flt()`` coercion can occur.
* THE SCO WRITEBACK MAP — widened from Subcontracting Order's own landed two-path citation (from
  the SCR side) to four, adding ``update_prevdoc_status``'s own raw-SQL-plus-db_set pair and
  ``update_ordered_and_reserved_qty``'s own downstream-refusal-plus-Bin-write pair, neither
  enumerated by SCO's own landing.

**Tallies: the 51st and FINAL supported doctype (260->265 tools) — THE ROOF ROW; ledger preview
stays CALLABLE (joins Delivery Note/Purchase Receipt/Stock Entry's own both-ledgers category, NOT
the skip tuple, which stays at 29 members, but with a live-crash caveat none of those three carry);
FORTY-FIRST special ``_list_fields`` branch by direct count (``total``, not ``grand_total`` — the
SAME stand-in-fieldname shape Subcontracting Order's own branch already established); MRO ties
Subcontracting Order/Subcontracting Inward Order for the deepest CALLABLE chain (7 nodes);
companyless tally unchanged at 6; whitelist exactly 6, nothing granted, plus the inherited
``get_current_stock`` scoping note; period-closing native-equal FIFTH row (after Asset/Asset
Repair/Invoice Discounting/Asset Capitalization); cascade 3 edges, clean both-tree scan, second
consecutive clean scan after Subcontracting Inward Order's own."""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request

from pacioli.amend import amend_payload
# Package-private, same discipline as doctor.py importing registry._resolve_ref /
# runtime._SEAL_KEY_BYTES: glue reusing a pure-core helper rather than re-deriving its own copy of
# the ISO-date shape (and risking the two silently drifting apart). check_red_line (plan.py) is
# the pure core that ultimately enforces posting_date's shape; this client validates it BEFORE
# spending a network round-trip building a query on an unverifiable date (F-S1).
from pacioli.plan import _is_iso_date

PREVIEW_METHOD = "erpnext.controllers.stock_controller.show_accounting_ledger_preview"

SALES_INVOICE = "Sales Invoice"
PURCHASE_INVOICE = "Purchase Invoice"
PAYMENT_ENTRY = "Payment Entry"
JOURNAL_ENTRY = "Journal Entry"
SALES_ORDER = "Sales Order"
PURCHASE_ORDER = "Purchase Order"
MATERIAL_REQUEST = "Material Request"
DELIVERY_NOTE = "Delivery Note"
PURCHASE_RECEIPT = "Purchase Receipt"
STOCK_ENTRY = "Stock Entry"
SUPPLIER_QUOTATION = "Supplier Quotation"
QUOTATION = "Quotation"
POS_INVOICE = "POS Invoice"
DUNNING = "Dunning"
STOCK_RECONCILIATION = "Stock Reconciliation"
LANDED_COST_VOUCHER = "Landed Cost Voucher"
REQUEST_FOR_QUOTATION = "Request for Quotation"
BLANKET_ORDER = "Blanket Order"
JOB_CARD = "Job Card"
BOM = "BOM"
WORK_ORDER = "Work Order"
ASSET = "Asset"
PACKING_SLIP = "Packing Slip"
COST_CENTER_ALLOCATION = "Cost Center Allocation"
SUPPLIER_SCORECARD_PERIOD = "Supplier Scorecard Period"
QUALITY_INSPECTION = "Quality Inspection"
INSTALLATION_NOTE = "Installation Note"
SHIPMENT = "Shipment"
SALES_FORECAST = "Sales Forecast"
PROJECT_UPDATE = "Project Update"
MAINTENANCE_VISIT = "Maintenance Visit"
MAINTENANCE_SCHEDULE = "Maintenance Schedule"
ASSET_MAINTENANCE_LOG = "Asset Maintenance Log"
BANK_GUARANTEE = "Bank Guarantee"
ASSET_MOVEMENT = "Asset Movement"
DELIVERY_TRIP = "Delivery Trip"
ASSET_VALUE_ADJUSTMENT = "Asset Value Adjustment"
PAYMENT_ORDER = "Payment Order"
SHARE_TRANSFER = "Share Transfer"
BOM_CREATOR = "BOM Creator"
BUDGET = "Budget"
TIMESHEET = "Timesheet"
CONTRACT = "Contract"
PICK_LIST = "Pick List"
ASSET_REPAIR = "Asset Repair"
INVOICE_DISCOUNTING = "Invoice Discounting"
ASSET_CAPITALIZATION = "Asset Capitalization"
PRODUCTION_PLAN = "Production Plan"
SUBCONTRACTING_ORDER = "Subcontracting Order"
SUBCONTRACTING_INWARD_ORDER = "Subcontracting Inward Order"
SUBCONTRACTING_RECEIPT = "Subcontracting Receipt"

# The two submit/cancel TRANSPORTS a SUPPORTED_DOCTYPES entry can name (see "submit_via" below).
SUBMIT_VIA_RUN_METHOD = "run_method"
SUBMIT_VIA_CLIENT_RPC = "client_rpc"

# Doctypes whose own on_cancel hook SELF-UNLINKS every submitted document that links back to it
# BEFORE frappe's own back-link check ever runs, because run_post_save_methods's cancel branch
# calls on_cancel THEN check_no_back_links_exist, never the other order (frappe/model/
# document.py:1450-1452 — Delivery Trip's own receipt, pinned in
# pacioli.tools._delivery_trip_cancel_risk_flags's own docstring). John's ruling 1 (2026-07-21,
# docs/plans/2026-07-21-cancel-truth-rulings.md): a doctype registered here gets its leaf
# plan_cancel ALLOWED where this broker's standing blast-radius gate would otherwise refuse a
# submitted incoming link — belted by the full pre-consent disclosure of the note-side writes
# (already built) and a post-execute readback attesting the self-unlink actually happened (the
# suspenders; a still-linked note is reported, never silently passed). Membership is a
# SOURCE-RECEIPTED claim, never inferred from doctype shape: adding a doctype here means someone
# has read its on_cancel body line by line AND confirmed (a full schema grep for
# '"options": "<Doctype>"', not a guess) that on_cancel clears every field that makes it a
# submitted incoming Link, for every doctype that can point at it. Contains exactly DELIVERY_TRIP
# for now — Delivery Note is the one doctype that can link to it (the two-hit full-tree grep in
# the "Breadth (Delivery Trip)" docstring section above), and its own delivery_trip/driver/
# driver_name/vehicle_no/lr_no/lr_date fields are exactly what update_delivery_notes(delete=True)
# clears. No cascade-order exception rides with this: plan_cascade_cancel stays untouched,
# structurally dead at the Delivery Note step (ruling 1 took the leaf exception, option (a), not
# a cascade reorder, option (b)).
SELF_UNLINKING_DOCTYPES = (DELIVERY_TRIP,)

# BOM Creator breadth (2026-07-21) — John's ruling 2 (two-phase PROVE, "truth always";
# docs/plans/2026-07-21-cancel-truth-rulings.md), off the design study
# (docs/plans/dossiers/bom_creator.design.md, option (b)). ``on_submit`` (bom_creator.py:247-248)
# is a single line, ``self.enqueue_bom_creation()``, which (bom_creator.py:255-261) calls
# ``frappe.enqueue(self.create_boms, queue="short", timeout=600, is_async=True)`` — the docstatus
# transition this broker confirms IS real (before_submit's own set_status() flips ``status`` to
# "Submitted" inline, synchronously, bom_creator.py:133-142/163-165, BEFORE the enqueue even
# happens), but the doctype's OWN declared deliverable — the actual BOM tree — is built LATER, by
# a background worker, outside the response this broker's submit call already answered. A doctype
# registered here is NOT claimed a plain "committed" at submit: the ledger's own outcome status is
# narrowed to "committed_pending_async" (tools.py's ``_QueuedConsequenceEffects``) and a later
# sweep (``prove_verify``'s own ``_sweep_queued_consequences``) attests the terminal Completed/
# Failed result once the worker lands, or reports it still pending. Membership is a
# SOURCE-RECEIPTED claim like SELF_UNLINKING_DOCTYPES above: a doctype belongs here only after
# someone has read its on_submit body and confirmed the enqueue call, never inferred from a
# Select field named "status" alone. Contains exactly BOM_CREATOR for now.
ENQUEUE_ON_SUBMIT_DOCTYPES = (BOM_CREATOR,)

# The channel/queue pair the two-phase PROVE disclosure and the queued_consequence marker name,
# keyed by doctype — kept separate from the membership tuple above the same way
# SELF_UNLINKING_DOCTYPES' own per-doctype mechanism detail lives in tools.py's risk-flag
# functions, not in this tuple itself. BOM Creator's is "bom_creator.create_boms" on frappe's
# "short" queue (bom_creator.py:256-260, verified above).
ENQUEUE_ON_SUBMIT_CHANNELS = {
    BOM_CREATOR: ("bom_creator.create_boms", "short"),
}

# The body-doctype override-submit RPC names (client_rpc transport). Knowledge-pinned from frappe
# source (frappe/client.py): `submit(doc)` does `frappe.get_doc(frappe.parse_json(doc)); doc.submit()`
# — the FULL doc travels in the body, reconstructed server-side, never re-fetched from the DB.
# `cancel(doctype, name)` does `frappe.get_doc(doctype, name); wrapper.cancel()` — doctype/name are
# plain sibling params; the doc is loaded fresh from the DB, no body payload needed. Both are
# ordinary /api/method RPCs, so their return value rides frappe's `response["message"]` envelope
# exactly like `ledger_preview`/`apply_workflow` — never the /api/resource `"data"` envelope.
_CLIENT_SUBMIT_METHOD = "frappe.client.submit"
_CLIENT_CANCEL_METHOD = "frappe.client.cancel"

# The broker's own per-doctype config (design §B) — "I've been built and tested for these", not
# the guard's per-credential resource-doctype allowlist. A pacioli_doctype outside this set is a
# structured deny at the tool layer (tools.py), never reaches this client. Knowledge-pinned, not
# live-verified (see module docstring). Journal Entry's party_field is None — it carries no
# header-level party at all (see module docstring); every consumer of this dict must treat None
# as "there is no party column to splice in", never as a missing/blank string.
#
# **submit_via** (override-doctype submit path, PHASE L / SCOPED-TOKEN-PROOF.md): SI/PI/PE/SO/PO/
# MR/DN/PR/SE/SQ/Q stay on the proven `SUBMIT_VIA_RUN_METHOD` (the URL-path `run_method=submit`/
# `cancel` doc-method surface — the only shape `pacioli_guard` could ORIGINALLY scope per-doctype,
# since `doctype` travels in the URL). Journal Entry alone is `SUBMIT_VIA_CLIENT_RPC`: `JournalEntry`
# overrides `submit()`/`cancel()` (>100-row background queuing,
# `erpnext/accounts/doctype/journal_entry/journal_entry.py:186,195`) WITHOUT `@frappe.whitelist()`,
# so frappe's REST handler 403s the run_method vector for JE specifically — every other supported
# doctype overrides neither and is unaffected (confirmed for Sales Order by reading
# `erpnext/selling/doctype/sales_order/sales_order.py`, for Purchase Order by reading
# `erpnext/buying/doctype/purchase_order/purchase_order.py`, for Material Request by reading
# `erpnext/stock/doctype/material_request/material_request.py`, for Supplier Quotation by
# reading `erpnext/buying/doctype/supplier_quotation/supplier_quotation.py`, and for Quotation by
# reading `erpnext/selling/doctype/quotation/quotation.py`, all version-16: no `def submit`/
# `def cancel` override anywhere in any of these files — only `on_submit`/`on_cancel`/
# `before_submit`/`before_cancel` HOOKS, called by the base `Document.submit()`/`.cancel()`, never
# overrides of those methods themselves).
# `frappe.client.submit`/`.cancel` is body-doctype (the doctype
# travels in the request body, not the URL), which is why this is only safe to enable now that
# `pacioli_guard.scope.body_scoped_target` parses that body and enforces the credential's
# per-doctype grant on it exactly as strictly as the URL-path shape — see guard CHANGELOG 0.5.0.
# KNOWLEDGE-PINNED, NOT LIVE-VERIFIED (Gate 10, next armed window).
#
# **date_field** (Sales Order breadth increment, 2026-07-20, joined by Purchase Order and Material
# Request) — the doctype's own transaction-date fieldname, which the broker's closed-books check
# (`get_period_locks`'s `posting_date` parameter) reads off the RAW document before storing it
# under the Plan's own `posting_date` attribute (an internal, doctype-agnostic vocabulary name
# that is NEVER renamed — see `pacioli/plan.py`'s `check_red_line`, which treats it as an opaque
# ISO-date value regardless of which ERPNext field it came from). SI/PI/PE/JE all carry a literal
# `posting_date` field — confirmed present on each doctype's JSON — so this was never surfaced as
# its own config before Sales Order. **Sales Order, Purchase Order, Material Request, Supplier
# Quotation, and Quotation all carry NO `posting_date` field at all** — confirmed absent from
# `sales_order.json` (170 fields enumerated), `purchase_order.json` (157 fields enumerated),
# `material_request.json` (40 fields enumerated), `supplier_quotation.json`, and `quotation.json`
# (130 fields enumerated), version-16 source checkout — only `transaction_date` (a plain Date
# field, not in the child tables, on all five doctypes). Every call site that used to hardcode
# `doc.get("posting_date")` now resolves the fieldname through
# this table first (`tools.py`'s `_date_field_for`); the DEFAULT for any doctype not listed here
# (including a 'generic' cascade node this broker has no descriptor for) stays `"posting_date"` —
# ERPNext's overwhelmingly common convention for GL-posting doctypes, a best-effort default for
# the unmodeled case, not a verified pin like the explicit rows below.
#
# **`date_field: None` (BOM breadth, 2026-07-21) is a THIRD state, distinct from both a named
# field and an absent key: an explicit, source-verified pin that the doctype carries NO date
# field at all** (BOM — zero Date/Datetime fields across all 94 of `bom.json`'s fields; frappe's
# `creation` metadata is deliberately NOT a stand-in, see the module docstring's BOM section).
# Every consumer branches on the DECLARED None, never on an empty read: `_posting_date_of`
# (tools.py) stores `pacioli.plan.NO_DATE_FIELD` in the Plan's posting_date channel, `_locks_for`
# skips the period-lock read entirely, and `check_red_line` passes the sentinel by its own
# explicit branch — equal to ERPNext (which never period-checks BOM: not in hooks.py's
# `period_closing_doctypes`, posts no GL for `check_freezing_date` to fire on), never weaker. A
# doctype whose declared date field merely READS empty stays refused exactly as before.
#
# **A Datetime-typed date_field (Work Order breadth, 2026-07-21) is a FOURTH wrinkle, on the
# VALUE axis rather than the key axis:** `planned_start_date` is the first declared date_field
# whose underlying fieldtype is Datetime, so a raw read carries a time part that fails the
# strict ISO-date shape every consumer validates. `tools.py`'s `_posting_date_of` projects it to
# its date part (truncation ONLY when a valid ISO date is immediately followed by a " "/"T"
# separator — malformed values keep their raw shape and stay refused). The fieldname declared
# here is still just the fieldname; the projection is the reader's job, in one place.
#
# **A hidden-but-forced date_field (Budget breadth, 2026-07-21) is a FIFTH wrinkle, on neither the
# key axis (BOM's `None`) nor the value axis (Work Order's Datetime) but the UI-VISIBILITY axis:**
# `budget_start_date` is `hidden: 1` and schema-optional (no `reqd` key) — yet `validate()`
# unconditionally derives it from `from_fiscal_year` (itself `reqd: 1`), so every persisted Budget
# carries a real, non-blank value on every save, draft or submitted. The generic
# `_date_field_for`/`_posting_date_of`/`_locks_for` machinery needs no change (it already reads
# `doc.get(fieldname)` regardless of the schema's own `hidden` flag) — this wrinkle is scoped
# entirely to `_list_fields` below, the first branch where a real, present `date_field` is
# deliberately NOT spliced into the requested column list, because a hidden field was never meant
# for list-view display and both ends of Budget's own validity window share this hidden posture
# (unlike Blanket Order's/Supplier Scorecard Period's own pairs, where the un-pinned end is
# visible and rides as list-tier context).
SUPPORTED_DOCTYPES = {
    SALES_INVOICE: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD,
                    "date_field": "posting_date"},
    PURCHASE_INVOICE: {"party_field": "supplier", "submit_via": SUBMIT_VIA_RUN_METHOD,
                       "date_field": "posting_date"},
    PAYMENT_ENTRY: {"party_field": "party", "submit_via": SUBMIT_VIA_RUN_METHOD,
                    "date_field": "posting_date"},
    JOURNAL_ENTRY: {"party_field": None, "submit_via": SUBMIT_VIA_CLIENT_RPC,
                    "date_field": "posting_date"},
    # Breadth (Sales Order) — the fifth supported doctype, and the FIRST that is not itself a
    # GL-posting document. Confirmed from source (sales_order.json + sales_order.py, version-16,
    # both fetched 2026-07-20):
    #   * `customer` (Link -> Customer) IS the header-level party field — party_field="customer",
    #     the same shape as Sales Invoice.
    #   * `status` (Select: Draft/On Hold/To Pay/To Deliver and Bill/To Bill/To Deliver/Completed/
    #     Cancelled/Closed) and `grand_total` (Currency) ARE both present — unlike Journal Entry,
    #     Sales Order needs NO `_list_fields` branch for those two; it is mechanically identical to
    #     Sales Invoice on every axis except the date field below.
    #   * `is_submittable: 1` — a real docstatus 0/1/2 lifecycle, submit/cancel/amend all apply.
    #   * `date_field="transaction_date"` — see the table-level comment above; the one genuinely
    #     new branch this doctype forces.
    #   * **Sales Order posts NO GL entries of its own on submit.** It has no `make_gl_entries`
    #     override (`grep` of sales_order.py); it inherits `StockController.make_gl_entries`
    #     (`erpnext/controllers/stock_controller.py:292-319`), whose body is a conditional no-op
    #     unless perpetual-inventory non-stock provisioning or fixed-asset Purchase-Receipt items
    #     are in play — and even when `need_inventory_map` is true, `get_gl_entries` sources rows
    #     from the voucher's OWN Stock Ledger Entry rows (`get_stock_ledger_details`), which a bare
    #     Sales Order never has (only Delivery Note / Sales Invoice-with-update_stock / Stock Entry
    #     / Purchase Receipt write Stock Ledger Entry rows) — so `ledger_preview`'s `gl_data` for a
    #     Sales Order is `[]` in the overwhelming common case. The generic ledger-preview RPC
    #     (`show_accounting_ledger_preview` -> `doc.make_gl_entries()` polymorphically) still WORKS
    #     for Sales Order (confirmed by reading the call chain — it degrades to an empty list
    #     rather than raising), so `plan_submit`'s mechanical shape is unchanged; the DISCLOSURE
    #     value of `projected_gl` for a Sales Order plan is simply near-always empty by the
    #     document's own nature (a pre-accounting order, not a posting) — this is not a defect to
    #     fix, it is what "no debit without a credit" looks like for a doctype that debits/credits
    #     nothing.
    #   * `on_submit` (sales_order.py:497-518): checks the customer credit limit, updates reserved
    #     qty, and — ONLY when the draft's own `reserve_stock` flag is truthy and it is not
    #     subcontracted — creates Stock Reservation Entries (a non-ledger reservation table, not
    #     Stock Ledger Entry). No auto-created sibling document (no Exchange-Gain-Or-Loss-style
    #     side document, unlike Journal Entry) — nothing here needed a broker-side gate or flag.
    #   * `on_cancel` (sales_order.py:527-557) has ONE real ERPNext-native refusal this broker does
    #     NOT separately model: `check_nextdoc_docstatus` throws ("Sales Invoice {0} must be
    #     deleted before cancelling this Sales Order") when a DRAFT (docstatus 0) Sales Invoice
    #     still references this order — a case the broker's own blast-radius disclosure
    #     (`get_submitted_linked_docs`, which surfaces only SUBMITTED docstatus-1 links) cannot see
    #     at `plan_cancel` time, since a draft dependent is invisible to that call by construction.
    #     No new gate is built for this: the actual `cancel_sales_order` call still refuses safely
    #     — ERPNext's own bench-side throw surfaces as an ordinary answered `ErpnextError` through
    #     the existing generic exception handling ("ERPNext's own cancel-blocks are honored, never
    #     bypassed" — the same standing posture every other doctype's cancel tool already
    #     documents) — the finding here is only that `plan_cancel`'s disclosure won't PREVIEW this
    #     particular refusal in advance for a draft-SI dependent, the same structural gap every
    #     other doctype already has for its own docstatus-0 dependents.
    #   * `ignore_linked_doctypes` on cancel names GL Entry/Stock Ledger Entry/Payment Ledger
    #     Entry/Advance Payment Ledger Entry/Unreconcile Payment/Unreconcile Payment Entries —
    #     ERPNext's OWN generic-link cancel check skips those tables for Sales Order, consistent
    #     with "Sales Order posts no GL of its own" above; nothing for the broker to model, since
    #     the broker's blast-radius check (`get_submitted_linked_docs`) already calls the SAME
    #     ERPNext endpoint that honors this exemption list.
    SALES_ORDER: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD,
                 "date_field": "transaction_date"},
    # Breadth (Purchase Order) — the sixth supported doctype, and (with Sales Order) the second
    # confirmed to carry no `posting_date` field. Confirmed from source (purchase_order.json +
    # purchase_order.py, version-16 checkout, both read 2026-07-20; pinned first in the dossier at
    # docs/plans/dossiers/purchase_order.md):
    #   * `supplier` (Link -> Supplier) IS the header-level party field — party_field="supplier"
    #     (purchase_order.json lines 187-198), the same shape as Purchase Invoice.
    #   * `status` (Select: Draft/On Hold/To Receive and Bill/To Bill/To Receive/Completed/
    #     Cancelled/Closed/Delivered — lines 899-912) and `grand_total` (Currency, lines 804-812)
    #     ARE both present — no `_list_fields` branch needed for those two.
    #   * `is_submittable: 1` (line 1324) — a real docstatus 0/1/2 lifecycle, submit/cancel/amend
    #     all apply.
    #   * `date_field="transaction_date"` (line 229, `"default": "Today"`) — **Purchase Order
    #     carries NO `posting_date` field at all**, confirmed absent across all 157 fields
    #     enumerated in `purchase_order.json` (same finding shape as Sales Order, the fifth
    #     doctype — this is now a two-doctype pattern, not a one-off).
    #   * `submit_via=SUBMIT_VIA_RUN_METHOD` — confirmed by reading `purchase_order.py`
    #     (version-16): no `def submit`/`def cancel` override anywhere in the file (only
    #     `on_submit`/`on_cancel` HOOKS, called by the base `Document.submit()`/`.cancel()`, not
    #     overrides of those methods themselves) — same reasoning as Sales Order, so the
    #     run_method vector never 403s for Purchase Order either.
    #   * **Purchase Order posts NO GL entries of its own on submit or cancel.** No
    #     `make_gl_entries` override anywhere in `purchase_order.py` (grep of the file); it is a
    #     pre-receipt commitment doctype, mechanically identical to Sales Order on this axis — GL
    #     posting is Purchase Receipt's/Purchase Invoice's job, not Purchase Order's.
    #   * `on_submit` (purchase_order.py:441-466): `update_status_updater()` if `is_against_so()`,
    #     `update_status_updater_if_from_pp()` if `is_against_pp()`, `update_prevdoc_status()`
    #     (Material Request/Supplier Quotation), `update_requested_qty()` unless subcontracted,
    #     `update_ordered_qty()` (warehouse bin `ordered_qty` counters via `update_bin_qty`/
    #     `get_ordered_qty` — bin-qty recalculation, never a Stock Ledger Entry write),
    #     `validate_budget()`, `update_reserved_qty_for_subcontract()`, `update_blanket_order()`,
    #     `auto_create_subcontracting_order()` if subcontracted and not the old flow. No GL/SL
    #     posting anywhere in this chain.
    #   * `on_cancel` (purchase_order.py:468-505) sets `ignore_linked_doctypes = ("GL Entry",
    #     "Payment Ledger Entry", "Advance Payment Ledger Entry", "Unreconcile Payment",
    #     "Unreconcile Payment Entries")` (lines 469-475) BEFORE calling `super().on_cancel()` —
    #     ERPNext's OWN generic-link cancel check skips those tables for Purchase Order, so
    #     settled payments are never part of the blast radius (unlike Purchase Invoice's cancel,
    #     which CAN unlink a Payment Entry) — consistent with "no GL of its own" above. Then:
    #     status_updater reversal (if against SO/PP), drop-ship qty zeroing
    #     (`set_received_qty_to_zero_for_drop_ship_items` + `update_receiving_percentage` if
    #     `has_drop_ship_item()`), `update_reserved_qty_for_subcontract()`,
    #     `check_for_on_hold_or_closed_status("Material Request", "material_request")` (refuses
    #     cancel if the source MR is On Hold/Closed — a STATUS check, not a docstatus check),
    #     `db_set("status", "Cancelled")`, `update_prevdoc_status()`, `update_requested_qty()`
    #     unless subcontracted, `update_ordered_qty()`, `update_blanket_order()`,
    #     `unlink_inter_company_doc()`. **Unlike Sales Order, Purchase Order's `on_cancel` has NO
    #     `check_nextdoc_docstatus`-style refusal** — confirmed absent by grep of
    #     `purchase_order.py` (no such call anywhere in the file) — so there is no draft-dependent
    #     blast-radius gap analogous to Sales Order's "Sales Invoice must be deleted before
    #     cancelling" case. The `check_for_on_hold_or_closed_status` MR check above IS a similar
    #     SHAPE of gap (an ERPNext-native refusal `plan_cancel`'s disclosure cannot preview, since
    #     it reads a status field, not the submitted-docstatus graph `get_submitted_linked_docs`
    #     walks) — no new broker gate needed: the answered refusal surfaces through the same
    #     generic exception handling every other doctype's cancel tool already relies on.
    #   * **Cascade edges — `cascade.py` needed NO changes**, same finding as Sales Order.
    #     `build_cascade`/`run_cascade` are fully doctype-blind; they consume whatever
    #     `node_meta(doctype, docname)` and `fetch_linked(doctype, docname)` (this module's
    #     `get_submitted_linked_docs` wrapper, via `tools.py`'s `_cascade_node_meta`/
    #     `_cascade_fetch_linked`) hand back, keyed on the internal `"posting_date"` name only
    #     (never the raw ERPNext field name — `_date_field_for` already resolves that inside the
    #     shared closure, so this breadth increment needed zero new cascade wiring). Doctypes
    #     carrying a `Link` field to Purchase Order (confirmed by grepping
    #     `'"options": "Purchase Order"'` across the v16 checkout): Purchase Receipt Item,
    #     Purchase Invoice Item, Sales Order Item/Sales Order itself (drop-ship), Stock Entry,
    #     Subcontracting Order, Subcontracting Receipt Item, Production Plan Sub Assembly Item —
    #     ERPNext's own `get_submitted_linked_docs` walks this Link-field graph generically, so a
    #     submitted Purchase Receipt/Purchase Invoice/Sales Order/Subcontracting Order referencing
    #     this Purchase Order is discovered and ordered ahead of it in cancel order with zero
    #     doctype-specific cascade code — same as every other supported doctype. Per the dossier:
    #     PO's OWN cancel is a status-updater-style scatter (qty/percentage re-normalization up the
    #     MR/PR/PI/Blanket-Order chain), never an explicit cascade_cancel of its own — the broker's
    #     generic cascade machinery models the inverse direction (dependents that must go BEFORE
    #     this PO cancels), which is the correct and only cascade concern for a governed cancel.
    PURCHASE_ORDER: {"party_field": "supplier", "submit_via": SUBMIT_VIA_RUN_METHOD,
                     "date_field": "transaction_date"},
    # Breadth (Material Request) — the seventh supported doctype, and the FIRST to combine
    # "status present" with "grand_total absent" — a new list-tier shape neither Journal Entry
    # (drops both) nor Sales/Purchase Order (carries both) covers. Confirmed from source
    # (material_request.json + material_request.py, version-16 checkout, both read 2026-07-20;
    # dossier at docs/plans/dossiers/material_request.md):
    #   * `customer` (Link -> Customer, lines 88-95) is NOT the header-level party field —
    #     `reqd` is absent and it carries `depends_on: eval:doc.material_request_type==
    #     "Customer Provided"` (line 89), so it is present in schema but conditional/type-locked.
    #     The controller FORCIBLY CLEARS it for the other 5 of 6 types
    #     (`validate_material_request_type`, material_request.py:218-222: `if
    #     self.material_request_type != "Customer Provided": self.customer = None`) — a stronger
    #     "never a stable counterparty" case than Payment Entry's Internal Transfer (blank by type
    #     choice alone, never additionally self-cleared on save). No supplier field is modeled at
    #     all — a Purchase-type MR names no party; the PURCHASE ORDER it is fulfilled by names the
    #     supplier, not the requisition. **party_field=None** — Journal Entry's shape, not Payment
    #     Entry's: there is no header-level party column to splice into the list tier at all.
    #   * `status` (Select, lines 179-193, `read_only: 1`, options include Draft/Submitted/
    #     Pending/Partially Ordered/Partially Received/Ordered/Issued/Transferred/Received/
    #     Stopped/Cancelled) IS present. `grand_total` is confirmed ABSENT across all 40 fields
    #     enumerated in `material_request.json` — no field name containing "total" anywhere; the
    #     doctype is cost-blind by design (no `accounts` child table, no rate/amount field at the
    #     header level; the two Percent fields `per_ordered`/`per_received`, lines 194-213, are
    #     completion trackers, never a financial total). This status-yes/grand_total-no
    #     combination is genuinely new — see `_list_fields` below for the branch it forces.
    #   * `is_submittable: 1` (line 378) — a real docstatus 0/1/2 lifecycle, submit/cancel/amend
    #     all apply. `amended_from` (Link -> Material Request, lines 122-130) confirms amend rides
    #     the same generic resource-CRUD shape every other supported doctype already uses.
    #   * `date_field="transaction_date"` (line 160, `reqd: 1`) — **Material Request carries NO
    #     `posting_date` field at all**, confirmed absent across all 40 fields (same finding shape
    #     as Sales/Purchase Order — now a three-doctype pattern). Rides the existing `date_field`
    #     mechanism unchanged; no new plumbing.
    #   * **Material Request posts NEITHER GL NOR Stock Ledger entries of its own, on submit or
    #     cancel.** Confirmed by grep of `material_request.py`: no `make_gl_entries`, no
    #     `make_sl_entries`, no `GLEntry`/`StockLedgerEntry` reference anywhere in the file. It
    #     inherits `BuyingController` -> `SubcontractingController` -> `StockController` (the same
    #     chain Purchase Order rides — confirmed by reading the class hierarchy in
    #     `controllers/buying_controller.py`/`controllers/subcontracting_controller.py`), so
    #     `StockController.make_gl_entries`'s conditional no-op is still reachable and
    #     `ledger_preview`'s RPC call succeeds (never AttributeErrors on a missing method) — it
    #     simply has nothing to disclose, exactly like Sales/Purchase Order.
    #   * `material_request_type` (Select, reqd, lines 79-87, six options: Purchase/Material
    #     Transfer/Material Issue/Manufacture/Subcontracting/Customer Provided) is a **stock-side
    #     fulfillment-mode fork**, not an accounting fork — Purchase feeds Purchase Orders/RFQs/
    #     Supplier Quotations; Material Transfer/Issue/Customer Provided feed Stock Entries;
    #     Manufacture feeds Work Orders; Subcontracting feeds `is_subcontracted` Purchase Orders
    #     (`get_mr_items_ordered_qty`, material_request.py:294-309, dispatches its own qty read by
    #     this same type). Documented here for the record — the same plan-tier disclosure
    #     treatment the Stock Entry dossier proposes for its own 13-way `purpose` field is a
    #     candidate here too, but is deliberately OWED, not built, for this landing (keeping it
    #     mechanical per the campaign DoD, rather than inventing bespoke machinery for one doctype
    #     at a time).
    #   * `on_submit` (material_request.py:230-236) and `on_cancel` (288-292) are STATUS/COUNTER
    #     updates only: `update_requested_qty_in_production_plan` (decrements a linked Production
    #     Plan's `requested_qty`, reversed with `cancel=True` on the cancel path),
    #     `update_requested_qty` (recomputes `Bin.indented_qty` via `update_bin_qty`), and —
    #     Purchase type only — `update_prevdoc_status` (refreshes linked PO/RFQ/SQ status). No GL,
    #     no Stock Ledger Entry write, on either verb — confirmed by the same grep above.
    #   * **Cascade edges — `cascade.py` needed NO changes**, same finding as Sales/Purchase
    #     Order. Doctypes carrying a `Link` field to Material Request (confirmed by grepping
    #     `'"options": "Material Request"'` across the v16 checkout, 16 files total including
    #     Material Request's own self-referencing `amended_from`): Purchase Invoice Item, Purchase
    #     Order Item, Request for Quotation Item, Supplier Quotation Item, Production Plan Item,
    #     Production Plan Material Request, Work Order, Sales Order Item, Delivery Note Item, Pick
    #     List, Pick List Item, Purchase Receipt Item, Stock Entry Detail, Subcontracting Order
    #     Item, Subcontracting Order Service Item — 14 external doctypes across buying,
    #     manufacturing, selling, stock, and subcontracting modules. ERPNext's own
    #     `get_submitted_linked_docs` walks this Link-field graph generically, so a submitted
    #     dependent referencing this Material Request is discovered and ordered ahead of it in
    #     cancel order with zero doctype-specific cascade code — same as every other supported
    #     doctype.
    #   * **Disclosure gap, the same shape as Sales/Purchase Order's, seen from the other side:**
    #     `before_cancel` (material_request.py:244-248) calls
    #     `check_on_hold_or_closed_status(self.doctype, self.name)` (`erpnext/buying/utils.py:
    #     112-123`) — refuses the cancel outright if the Material Request's OWN `status` is
    #     "Closed" or "On Hold". This is a STATUS check on the target document ITSELF, not the
    #     submitted-docstatus graph `get_submitted_linked_docs` walks, so `plan_cancel`'s
    #     disclosure cannot preview it in advance — the same structural gap Purchase Order's own
    #     MR-status check already documents (`check_for_on_hold_or_closed_status("Material
    #     Request", ...)`, purchase_order.py), now seen from the cancelled document's own side
    #     rather than a referencing PO's. No new broker gate needed: the answered refusal surfaces
    #     through the same generic exception handling every other doctype's cancel tool already
    #     relies on.
    MATERIAL_REQUEST: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                       "date_field": "transaction_date"},
    # Breadth (Delivery Note) — the eighth supported doctype, and the FIRST that is itself
    # STOCK-PRIMARY (real ledger rows on its own submit — see the module docstring's own
    # "Breadth (Delivery Note)" section for the full source-cited finding, dossier at
    # docs/plans/dossiers/delivery_note.md). Summary of what lands in this row:
    #   * party_field="customer" (delivery_note.json lines 189-201, reqd Link -> Customer).
    #   * status (lines 1062-1078) and grand_total (lines 810-821) both present — the generic
    #     SI/SO/PO branch, no `_list_fields` change.
    #   * date_field="posting_date" — Delivery Note KEEPS the default (a real `posting_date`
    #     field, lines 246-258), unlike Sales Order/Purchase Order/Material Request's
    #     `transaction_date` branch — the first stock-primary doctype to do so.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading delivery_note.py (version-16):
    #     no `def submit`/`def cancel` override, only on_submit/on_cancel hooks.
    #   * Posts BOTH Stock Ledger Entry (always, `update_stock_ledger()`, unconditional) and GL
    #     Entry (conditionally, `make_gl_entries()`, gated on perpetual inventory AND its own
    #     Stock Ledger Entry rows existing — which, unlike Sales/Purchase Order, they always do)
    #     — see the module docstring for the full stock_controller.py citation chain.
    #   * `ledger_preview` returns REAL non-empty `gl_data` for Delivery Note (confirmed:
    #     `get_accounting_ledger_preview` explicitly names Delivery Note in the doctypes that get
    #     an in-memory `update_stock_ledger()` seed before `make_gl_entries()` runs,
    #     stock_controller.py:2109-2110) — unlike SO/PO/MR. No existing test assumes an empty
    #     `projected_gl` for a non-invoice doctype (verified); a new real test pins the positive
    #     claim for Delivery Note instead (`TestDeliveryNoteLedgerDisclosure`, test_tools.py).
    #   * Cancel-refusal (`check_next_docstatus`) blocks on a submitted Sales Invoice OR
    #     Installation Note. The Sales Invoice half is already covered by this broker's generic
    #     `get_submitted_linked_docs` blast-radius disclosure (a real Link field,
    #     `Sales Invoice Item.delivery_note`); the Installation Note half is NOT — its
    #     back-reference (`Installation Note Item.prevdoc_docname`) is a plain Data field, not a
    #     Link, so ERPNext's own generic walker has no edge to find it at ANY docstatus. A
    #     genuinely PARTIAL disclosure, not merely the usual draft-dependent blind spot — recorded
    #     here rather than claimed covered. No new broker gate: the answered ERPNext refusal still
    #     surfaces safely at the real cancel call either way.
    #   * A second, ordinary disclosure gap (the SO/PO/MR shape): cancel also refuses if a
    #     referenced Sales Order is On Hold/Closed — a status read on a DIFFERENT document, not
    #     previewable by `plan_cancel`'s disclosure. No new code needed.
    #   * Auto-created documents (a return DN's `make_return_invoice` on submit; the inherited
    #     `delete_auto_created_batches` on cancel) get documentation, not JE-style special-casing
    #     — neither is a founding-law (debit==credit) bypass, both are transparent, readable,
    #     governed ERPNext machinery.
    #   * A REAL code fix (not just documentation): `tools._return_risk_flags` previously assumed
    #     every `is_return` doctype also carries `update_outstanding_for_self` (true for SI/PI,
    #     the only two that had `is_return` before this landing) — false for Delivery Note (the
    #     field is confirmed absent from its schema, and Delivery Note never posts to a
    #     receivable/payable account at all). Fixed by keying on field PRESENCE
    #     (`"update_outstanding_for_self" in doc`), not truthiness, with a third branch for a
    #     stock-only return. See TestDeliveryNoteReturnDisclosure.
    #   * Cascade edges — `cascade.py` needed NO changes (doctype-blind by construction, zero
    #     literals). Real Link fields to Delivery Note: Purchase Receipt.inter_company_reference,
    #     Stock Entry.delivery_note_no, Shipment Delivery Note.delivery_note,
    #     Packing Slip.delivery_note, Delivery Stop.delivery_note, Sales Invoice Item.delivery_note,
    #     POS Invoice Item.delivery_note, plus Delivery Note's own amended_from/return_against.
    DELIVERY_NOTE: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD,
                    "date_field": "posting_date"},
    # Breadth (Purchase Receipt) — the ninth supported doctype, and the SECOND STOCK-PRIMARY row
    # (with Delivery Note — see the module docstring's own "Breadth (Purchase Receipt)" section for
    # the full source-cited finding, dossier at docs/plans/dossiers/purchase_receipt.md). Summary:
    #   * party_field="supplier" (purchase_receipt.json lines 186-200, reqd Link -> Supplier).
    #   * status (lines 873-889, 8 values) and grand_total (lines 811-820) both present — the
    #     generic SI/SO/PO/DN branch, no `_list_fields` change.
    #   * date_field="posting_date" — Purchase Receipt KEEPS the default (a real `posting_date`
    #     field, lines 223-236), the same shape Delivery Note already rides.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading purchase_receipt.py (version-16):
    #     no `def submit`/`def cancel` override, only on_submit/on_cancel/before_cancel hooks.
    #   * Posts BOTH Stock Ledger Entry (always, `update_stock_ledger()`, unconditional) and GL
    #     Entry (conditionally, `make_gl_entries()`, same StockController gate Delivery Note's
    #     landing found) — see the module docstring for the full purchase_receipt.py citation.
    #   * `ledger_preview` returns REAL non-empty `gl_data` for Purchase Receipt — confirmed at the
    #     SAME line Delivery Note's landing cited (stock_controller.py:2109-2110, Purchase Receipt
    #     is the first-named doctype in that literal whitelist). New positive-pin test:
    #     TestPurchaseReceiptLedgerDisclosure (test_tools.py).
    #   * Cancel-refusal names only ONE doctype (Purchase Invoice, via a real
    #     Purchase Invoice Item.purchase_receipt Link field) — FULLY covered by this broker's
    #     generic blast-radius disclosure, a genuine divergence from Delivery Note's own PARTIAL
    #     (Sales-Invoice-covered / Installation-Note-uncovered) gap.
    #   * A WIDER disclosure gap than any prior doctype: `check_for_on_hold_or_closed_status(
    #     "Purchase Order", ...)` fires from BOTH validate() (i.e. on submit) AND on_cancel — not
    #     cancel-only like the SO/PO/MR/DN shape. A status read on a different document, invisible
    #     to plan_submit's AND plan_cancel's disclosure alike. No new broker gate: the answered
    #     refusal surfaces safely at the real submit/cancel call either way.
    #   * Auto-created bundles (make_bundle_for_sales_purchase_return /
    #     make_bundle_using_old_serial_batch_fields on submit; delete_auto_created_batches on
    #     cancel) get documentation, not JE-style special-casing — same treatment as Delivery Note.
    #     A genuine ORPHAN hazard (different in kind from a cancel-refusal gap): Landed Cost
    #     Voucher's receipt_document field is a Dynamic Link, not a real Link — invisible to
    #     get_submitted_linked_docs at any docstatus, and ERPNext's own on_cancel never checks for
    #     a linked LCV at all, so cancelling a Purchase Receipt can silently stale an LCV's
    #     distributed amounts (no refusal exists to preview — documented as a disclosure note only).
    #   * `update_outstanding_for_self` confirmed ABSENT (148-field list) — the same stock-only-
    #     return shape Delivery Note's landing fixed `_return_risk_flags` for; no further code
    #     change needed, exercised live by TestPurchaseReceiptReturnDisclosure.
    #   * Cascade edges — `cascade.py` needed NO changes (doctype-blind by construction, zero
    #     literals). Real Link fields to Purchase Receipt: Stock Entry.purchase_receipt_no, Stock
    #     Entry Detail.reference_purchase_receipt, Delivery Note.inter_company_reference,
    #     Asset.purchase_receipt, Purchase Invoice Item.purchase_receipt, plus Purchase Receipt's
    #     own amended_from/return_against. (Landed Cost Purchase Receipt.receipt_document is a
    #     Dynamic Link, deliberately NOT in this list — see the orphan-hazard finding above.)
    PURCHASE_RECEIPT: {"party_field": "supplier", "submit_via": SUBMIT_VIA_RUN_METHOD,
                       "date_field": "posting_date"},
    # Breadth (Stock Entry) — the tenth supported doctype, the LAST of Wave 1, and the hardest: a
    # THIRD stock-primary row (with Delivery Note/Purchase Receipt), the FIRST genuinely polymorphic
    # one (13-way `purpose`), and the FIRST whose party field is gated by CLIENT-SIDE JS alone, not
    # by server Python (see the module docstring's own "Breadth (Stock Entry)" section for the full
    # source-cited finding, dossier at docs/plans/dossiers/stock_entry.md, re-verified against a
    # fresh version-16 checkout rather than trusted as pinned — one dossier correction landed:
    # `update_transferred_qty` updates the OUTGOING Stock Entry's OWN `per_transferred` field, a
    # self-referential Goods-In-Transit tracker, NOT Material Request's transfer percentage; the
    # real Material Request touch is a DIFFERENT method, `set_material_request_transfer_status`).
    # Summary:
    #   * party_field=None. `supplier` (stock_entry.json line 460-468) is present in schema but
    #     carries NO `reqd` at all and its `depends_on` (`eval: erpnext.stock.
    #     is_subcontracting_or_return_transfer(doc)`) is a CLIENT-SIDE JS eval only
    #     (`stock_entry.js:1657-1659`: `doc.purpose == "Send to Subcontractor" || (doc.purpose ==
    #     "Material Transfer" && doc.is_return)`) — confirmed by grepping `stock_entry.py` for
    #     `.supplier`: ZERO hits, no server-side read or clear at all. This is a WEAKER basis for
    #     party than Material Request's own `party_field=None` decision (MR's `customer` is
    #     forcibly cleared server-side by `validate_material_request_type` for 5 of 6 types) —
    #     Stock Entry's `supplier` is closer to Payment Entry's Internal Transfer shape (blank by
    #     UI convention, never asserted or cleared by the controller), except even a subcontracting
    #     Stock Entry's `supplier` is optional (no `reqd`), so there is no purpose for which it is a
    #     stable, guaranteed counterparty either. No `customer` field exists on this doctype at
    #     all. **party_field=None** — Journal Entry's shape (no header-level party column to splice
    #     into the list tier), for a reason weaker than either JE's or MR's own citation.
    #   * `status` and `grand_total` are BOTH confirmed ABSENT (87 fields enumerated in
    #     `stock_entry.json`, neither fieldname present — `docstatus` is the only state signal, the
    #     JE shape). In their place: `total_incoming_value`/`total_outgoing_value`/
    #     `value_difference` (all three Currency, confirmed present) and `purpose` (Select,
    #     `read_only`, `fetch_from: stock_entry_type.purpose`, confirmed 13 options by splitting
    #     the field's own `options` string in code, not by trusting the dossier's prose count —
    #     Material Issue/Material Receipt/Material Transfer/Material Transfer for Manufacture/
    #     Material Consumption for Manufacture/Manufacture/Repack/Send to Subcontractor/
    #     Disassemble/Receive from Customer/Return Raw Material to Customer/Subcontracting
    #     Delivery/Subcontracting Return — matches `validate_purpose`'s own `valid_purposes` list,
    #     stock_entry.py lines 671-691, byte for byte). This is a FOURTH `_list_fields` branch —
    #     status-absent (unlike MR) AND grand_total-absent (unlike SI/SO/PO/DN/PR) AND
    #     party_field=None (like JE/MR) all at once, a combination no prior doctype exercised.
    #     `purpose` rides as the context column, the same role `voucher_type` plays for JE and
    #     `material_request_type` plays for MR (MR's own precedent for this shape).
    #   * `is_submittable: 1` (line 769) — a real docstatus 0/1/2 lifecycle; `amended_from` (Link
    #     -> Stock Entry) confirms amend rides the same generic resource-CRUD shape.
    #   * `date_field="posting_date"` — Stock Entry carries a real `posting_date` field (Date,
    #     `default: "Today"`, confirmed present), the same default path Delivery Note/Purchase
    #     Receipt already ride, never the `transaction_date` branch.
    #   * `submit_via=SUBMIT_VIA_RUN_METHOD` — confirmed by reading `stock_entry.py` (version-16):
    #     no `def submit`/`def cancel` override anywhere in the file (4875 lines), only
    #     `on_submit`/`on_cancel`/`validate` HOOKS called by the base `Document.submit()`/
    #     `.cancel()`.
    #   * **Posts BOTH ledgers.** Stock Entry OVERRIDES `update_stock_ledger()` itself
    #     (stock_entry.py:2073-2091) as an UNCONDITIONAL SLE write (no perpetual-inventory gate,
    #     reversed via `sl_entries.reverse()` when `docstatus==2`), called from `on_submit` (line
    #     556) and `on_cancel` (line 592). It does NOT override `make_gl_entries` itself (only
    #     `get_gl_entries`, line 2218, the additional-costs distribution logic) — the triggering
    #     GATE is the same inherited `StockController.make_gl_entries` (stock_controller.py:
    #     292-319) conditional Delivery Note's and Purchase Receipt's own landings already found,
    #     called from `on_submit` (line 564) / `make_gl_entries_on_cancel()` from `on_cancel` (line
    #     601). `ledger_preview` returns REAL non-empty `gl_data` for Stock Entry — confirmed at
    #     the SAME `stock_controller.py:2109-2110` whitelist line DN's and PR's own landings cited
    #     (`if doc.get("update_stock") or doc.doctype in ("Purchase Receipt", "Delivery Note",
    #     "Stock Entry"): doc.update_stock_ledger()` — Stock Entry is the THIRD-named doctype in
    #     that literal tuple, closing the set all three stock-primary doctypes now occupy). New
    #     positive-pin test: TestStockEntryLedgerDisclosure (test_tools.py). Stock Entry has NO
    #     `update_stock` field of its own (confirmed absent, the same finding DN/PR's own schemas
    #     share) — the SI/PI-shaped E2 disclosure (`_update_stock_risk_flags`) is a silent no-op
    #     for all three stock-primary doctypes; their OWN dedicated ledger-disclosure tests are the
    #     real coverage, not that flag.
    #   * **`is_return` is present (Check, hidden, read_only, default 0) but `return_against` is
    #     CONFIRMED ABSENT** — a THIRD is_return shape, different in kind from DN's/PR's own
    #     stock-only-but-return_against-bearing shape. Traced to its one real producer:
    #     `work_order.py:3114-3133`'s `make_stock_return_entry` sets `is_return=1` with
    #     `purpose="Material Transfer for Manufacture"` when returning excess, non-consumed raw
    #     material from a Work Order back to store — a raw-material-direction flag on an ordinary
    #     WO transfer, not a credit-note concept at all. Because `return_against` can never be
    #     present on this doctype, `_return_risk_flags`'s settlement branch (gated on
    #     `doc.get("return_against")`) NEVER fires for Stock Entry — no code change needed, no
    #     false settlement claim is possible — only the top-line RETURN flag and the FREE-STANDING
    #     flag apply (see `TestStockEntryReturnDisclosure`). The top-line wording itself
    #     ("credit note... money moves... sale/purchase") is imprecise for a pure inventory
    #     redirection with no AR/AP concept at all — a genuine, documented imprecision, deliberately
    #     NOT reworded tonight (no false CLAIM results, only an SI/PI-flavored turn of phrase;
    #     fixing the WORDING without a fourth real-world shape to design against would be inventing
    #     ahead of evidence, the same discipline that keeps the purpose-disclosure column OWED
    #     below rather than built ad hoc).
    #   * **Purpose-to-cascade map (documented, not built into a plan-tier disclosure column this
    #     landing — the OWED treatment the dossier itself names, matching MR's own precedent for
    #     its six-way `material_request_type` fork):** confirmed by reading `on_submit`
    #     (stock_entry.py:546-576) and `on_cancel` (578-618) in full —
    #     - Manufacture / Material Transfer for Manufacture / Material Consumption for Manufacture
    #       / Disassemble: touch **Work Order** — `update_work_order()` (549, 589) refuses outright
    #       if the linked WO's `status == "Stopped"` (`_validate_work_order`, line 2360-2369, fires
    #       on BOTH submit and cancel — a status read on a DIFFERENT document, the same
    #       PO/PR-shaped WIDER gap, invisible to `plan_submit`'s AND `plan_cancel`'s disclosure
    #       alike); `update_disassembled_order()` (Disassemble only); `make_stock_reserve_for_wip_
    #       and_fg()`/`cancel_stock_reserve_for_wip_and_fg()` reserve/release WIP+FG qty when the
    #       WO itself has `reserve_stock` set; Material Consumption for Manufacture additionally
    #       runs `validate_work_order_status()` on cancel (587), refusing if the WO is
    #       "Completed" — a SECOND, purpose-scoped status read on the same different document.
    #     - Send to Subcontractor / Material Transfer (when `subcontracting_order` is set): touch
    #       **Subcontracting Order** — `update_subcontracting_order_status()` (561, 582,
    #       housekeeping recompute); `reserve_stock_for_subcontracting()` (558, Send to
    #       Subcontractor only, when the SCO has `reserve_stock` set); `validate_closed_
    #       subcontracting_order()` (stock_entry.py:1977-1983, called from `validate()` line 316
    #       — so on EVERY save/submit — AND from `on_cancel` line 580) refuses outright if the
    #       linked Subcontracting Order (or Subcontracting Inward Order) is On Hold/Closed — the
    #       WIDEST disclosure gap of the three (fires on submit AND cancel, like PR's own PO
    #       check, via the same `erpnext.buying.utils.check_on_hold_or_closed_status` helper MR's
    #       own landing already cited).
    #     - Any purpose whose items carry `quality_inspection` (gated on `self.inspection_required`,
    #       not on `purpose`): touches **Quality Inspection** — `update_quality_inspection()` (569,
    #       605) re-points each QI row's `reference_type`/`reference_name`, pure housekeeping, no
    #       refusal.
    #     - Manufacture/Repack with `fg_completed_qty` and a `work_order` whose OWN
    #       `update_consumed_material_cost_in_project` flag is set: touches **Project** —
    #       `update_cost_in_project()` (567, 603), a cost allocation, no refusal.
    #     - Material Transfer with `add_to_transit` set: touches **Material Request** —
    #       `set_material_request_transfer_status()` (stock_entry.py:3982-4002), writing
    #       `Material Request.transfer_status` — the DOSSIER's own citation named this
    #       `update_transferred_qty` in error; that method (3895-3965) instead updates the
    #       OUTGOING Stock Entry's OWN `per_transferred` field via its child table's
    #       `against_stock_entry`/`ste_detail` pairing — a SELF-referential Goods-In-Transit
    #       completion tracker, corrected here after reading both methods directly.
    #     - Any purpose with `asset_repair` set: `delink_asset_repair_sabb()` (579) re-points a
    #       Serial and Batch Bundle's voucher reference to the Asset Repair — housekeeping, no
    #       refusal, and the ONLY method here that touches a doctype (Asset Repair) with no other
    #       cascade role.
    #     - **`delete_linked_stock_entry()` (693-703) — a genuine HARD `frappe.delete_doc` of a
    #       counterpart draft — is UNREACHABLE under the live 13-purpose set.** It is gated on
    #       `self.purpose == "Send to Warehouse"`, deleting DRAFT (`docstatus: 0`) Stock Entries
    #       with `purpose == "Receive at Warehouse"`. Neither "Send to Warehouse" nor "Receive at
    #       Warehouse" appears in `validate_purpose`'s own 13-value `valid_purposes` list (lines
    #       671-691) or the `purpose` field's own `options` string — a document carrying either
    #       value would already have been refused at `validate()`, before ever reaching
    #       `on_cancel`. Confirmed dead/legacy code (a pre-"Add to Transit" era Send/Receive at
    #       Warehouse feature), not a live delete hazard — recorded here rather than silently
    #       assumed safe.
    #   * **No downstream-submitted-document REFUSAL exists on Stock Entry's own cancel at
    #     all** — a genuine divergence from DN's/PR's own `check_next_docstatus`-shaped methods.
    #     Stock Entry's `on_cancel` PUSHES updates into other doctypes (the cascade map above)
    #     rather than PULLING a refusal from one; there is no equivalent query anywhere in
    #     `stock_entry.py`. This broker's blast-radius disclosure (`get_submitted_linked_docs`)
    #     still runs unconditionally at `plan_cancel` regardless, but for Stock Entry it has very
    #     little to find (see the cascade-edges note below) — not because the disclosure is
    #     incomplete, but because ERPNext itself never gates Stock Entry's own cancel on a
    #     downstream submitted document's existence in the first place.
    #   * **Cascade edges — `cascade.py` needed NO changes** (doctype-blind by construction, zero
    #     literals — confirmed by re-reading the module). Real `Link` fields carrying `"options":
    #     "Stock Entry"` (confirmed by grepping the full v16 checkout, 5 raw hits, every one
    #     inspected for `fieldtype`): Journal Entry's own `stock_entry` field (`depends_on:
    #     eval:in_list(["Credit Note", "Debit Note"], doc.voucher_type)` — the only EXTERNAL real
    #     Link to Stock Entry in the whole checkout) and Stock Entry Detail's own
    #     `against_stock_entry` (Stock Entry's OWN child table, self-referencing the GIT
    #     counterpart) — plus Stock Entry's own self-links (`outgoing_stock_entry`,
    #     `source_stock_entry`, `amended_from`). No OTHER doctype named in the purpose-to-cascade
    #     map above (Work Order, Subcontracting Order, Quality Inspection, Project, Material
    #     Request, Asset Repair) carries a real Link BACK to Stock Entry — consistent with the
    #     PUSH-not-PULL shape just above: those doctypes are targets Stock Entry writes into, never
    #     sources a submitted-Link-graph walk needs to discover.
    STOCK_ENTRY: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                 "date_field": "posting_date"},
    # Breadth (Supplier Quotation) — the eleventh supported doctype, the FIRST of Wave 2, and the
    # FIRST whose entry is byte-for-byte IDENTICAL to an already-landed doctype's (Purchase
    # Order's). Confirmed from source (supplier_quotation.json + supplier_quotation.py, version-16
    # checkout, both read 2026-07-21; dossier at docs/plans/dossiers/supplier_quotation.md):
    #   * `supplier` (Link -> Supplier, reqd, lines 150-162) IS the header-level party field —
    #     party_field="supplier", the same fieldname and shape as Purchase Order.
    #   * `status` (Select, 5 options: Draft/Submitted/Stopped/Cancelled/Expired, lines 763-774)
    #     and `grand_total` (Currency, lines 633-641) ARE both present — the generic branch, no
    #     `_list_fields` change needed.
    #   * `is_submittable: 1` (line 948) — a real docstatus 0/1/2 lifecycle, submit/cancel/amend
    #     all apply. `amended_from` (Link -> Supplier Quotation, lines 191-199) confirms amend
    #     rides the same generic resource-CRUD shape every other supported doctype already uses.
    #   * `date_field="transaction_date"` (line 181, `"default": "Today"`) — **Supplier Quotation
    #     carries NO `posting_date` field at all**, confirmed absent across every field enumerated
    #     in `supplier_quotation.json` (the fourth doctype on this pattern, with Sales Order/
    #     Purchase Order/Material Request).
    #   * `submit_via=SUBMIT_VIA_RUN_METHOD` — confirmed by reading all 362 lines of
    #     `supplier_quotation.py` (version-16): no `def submit`/`def cancel` override anywhere,
    #     only `on_submit`/`on_cancel` hooks.
    #   * **Supplier Quotation posts NO GL entries and NO Stock Ledger entries, on submit or
    #     cancel** — confirmed by grep of `supplier_quotation.py`: zero hits for
    #     `make_gl_entries`, `make_sl_entries`, `GLEntry`, or `StockLedgerEntry` anywhere in the
    #     file. A pre-receipt quotation document, mechanically identical to Purchase Order here.
    #   * **This entry equals Purchase Order's, field for field** — the same party_field,
    #     submit_via, and date_field values, asserted directly by this landing's own test
    #     (`SUPPORTED_DOCTYPES[SUPPLIER_QUOTATION] == SUPPORTED_DOCTYPES[PURCHASE_ORDER]`) — the
    #     first genuinely EMPTY diff between two landed doctypes' config (every prior
    #     "mechanically identical" pairing still differed on at least one field, usually
    #     party_field).
    #   * `on_submit` (supplier_quotation.py:130-132): `db_set("status", "Submitted")` then
    #     `update_rfq_supplier_status(1)`. `on_cancel` (134-136): `db_set("status", "Cancelled")`
    #     then `update_rfq_supplier_status(0)`. The ONE real side-effect: a status write on a
    #     DIFFERENT document (`Request for Quotation Supplier.quote_status`, method body lines
    #     171-225, walked via each submitted item's `request_for_quotation` link) — never a
    #     docstatus change on the RFQ itself, never a cascade. `include_me` (1 on submit, counting
    #     this SQ's own items toward "Received"; 0 on cancel, excluding them so the status can
    #     revert to "Pending" if this was the RFQ's only quote) is the reversal shape a governed
    #     submit/cancel pair should have. Mechanically the same CLASS of side-effect Purchase
    #     Order's own `update_prevdoc_status()` and Material Request's own status-updater calls
    #     already exercise (a write into a sibling document's status field, never GL/SL, never a
    #     cascade) — only the target doctype (Request for Quotation) and method name differ.
    #   * **Cascade edges — `cascade.py` needed NO changes**, same finding as every prior doctype.
    #     Doctypes carrying a real `Link` field to Supplier Quotation (confirmed by grepping
    #     `'"options": "Supplier Quotation"'` across the v16 checkout): Purchase Order's own
    #     `ref_sq` field (purchase_order.json line 914 — NOT named `supplier_quotation`, a
    #     correction against the dossier's own prose, which cited the field by its label rather
    #     than its fieldname; read_only, no reqd) and Purchase Order Item's `supplier_quotation`
    #     field (purchase_order_item.json line 524; also read_only, no reqd) — both OPTIONAL,
    #     informational back-references, never a cascade-ordering edge; ERPNext's own Purchase
    #     Order cancel never checks for a linked Supplier Quotation at all. Quotation (the
    #     SELLING-side doctype, entirely distinct from Supplier Quotation, never repointed for
    #     this landing) also carries an optional `supplier_quotation` Link (quotation.json line
    #     887, not reqd) — irrelevant to this doctype's own cascade, since Quotation is a
    #     different doctype and is not itself landed here. **Supplier Quotation is a LEAF in the
    #     cancel dependency graph** — zero dependents that must cascade ahead of its own cancel,
    #     the same "leaf" finding Sales/Purchase Order/Material Request's own landings already
    #     made, this time with BOTH referencing fields confirmed optional (no reqd) by direct JSON
    #     read, not merely absent from a Link-graph walk.
    SUPPLIER_QUOTATION: {"party_field": "supplier", "submit_via": SUBMIT_VIA_RUN_METHOD,
                         "date_field": "transaction_date"},
    # Breadth (Quotation) — the twelfth supported doctype, Wave 2's second row, and the FIRST
    # DYNAMIC-PARTY doctype landed — the judgment call of Wave 2. Confirmed from source
    # (quotation.json + quotation.py, version-16 checkout, both read 2026-07-21; dossier at
    # docs/plans/dossiers/quotation.md):
    #   * NO static `customer` Link field exists at the header level — confirmed by enumerating
    #     all 130 fields in quotation.json (the only Link fields naming Customer-shaped things are
    #     `customer_address`, an Address Link, and a hidden `customer_group`). Instead:
    #     `quotation_to` (Link -> DocType, default "Customer", reqd, lines 162-172) paired with
    #     `party_name` (Dynamic Link, options="quotation_to", lines 173-185) — the counterparty can
    #     resolve to Customer, Lead, Prospect, or CRM Deal (set_customer_name(), quotation.py:
    #     232-243). `party_name`'s own `oldfieldname` is literally "customer" — this WAS a static
    #     Link in an earlier schema generation, migrated to Dynamic Link; the clearest evidence the
    #     dynamic pairing is deliberate, not an oversight.
    #   * **THE KEY DECISION: party_field=None — following Material Request's None-precedent, NOT
    #     a forced single Dynamic Link.** SUPPORTED_DOCTYPES' party_field shape expects ONE static
    #     fieldname whose value IS the party; a Dynamic Link does not fit — splicing party_name
    #     alone would disclose a bare record name with no doctype context (a WORSE disclosure than
    #     Journal Entry's/Material Request's own None). quotation_to/party_name are instead
    #     surfaced as a PAIR of list-tier CONTEXT columns (see _list_fields below) — the same
    #     treatment Material Request's material_request_type and Stock Entry's purpose already
    #     receive, never collapsed into a single party_field string.
    #   * `status` (Select, 8 options: Draft/Open/Replied/Partially Ordered/Ordered/Lost/Cancelled/
    #     Expired, read_only, reqd, lines 863-875) and `grand_total` (Currency, read_only, lines
    #     700-709) ARE BOTH present. Combined with party_field=None this is a combination NO prior
    #     branch covers (JE: neither; MR: status only; SE: neither) — forces a genuinely NEW,
    #     FIFTH _list_fields branch (below), splicing quotation_to/party_name in place of the
    #     single party slot rather than the generic branch's literal party_field (which would
    #     splice None itself — the exact bug MR's/SE's own tests already guard against).
    #   * `is_submittable: 1` (line 1137) — a real docstatus 0/1/2 lifecycle. `amended_from`
    #     (Link -> Quotation, self-referencing, line 202-213) confirms amend rides the same
    #     generic resource-CRUD shape every other supported doctype already uses.
    #   * `date_field="transaction_date"` (line 229-240, default "Today", reqd) — Quotation
    #     carries NO `posting_date` field at all, confirmed absent across all 130 fields
    #     enumerated in quotation.json — the fifth doctype on this pattern (with Sales Order/
    #     Purchase Order/Material Request/Supplier Quotation).
    #   * `submit_via=SUBMIT_VIA_RUN_METHOD` — confirmed by reading all of quotation.py
    #     (version-16): no `def submit`/`def cancel` override anywhere, only `on_submit`/
    #     `on_cancel` hooks.
    #   * **Quotation posts NO GL entries and NO Stock Ledger entries, on submit or cancel** —
    #     confirmed by grep of quotation.py: zero hits for `make_gl_entries`, `make_sl_entries`,
    #     `GLEntry`, or `StockLedgerEntry` anywhere in the file. Its amounts are informational;
    #     only a Sales Order/Sales Invoice built FROM a Quotation (make_sales_order()/
    #     make_sales_invoice(), quotation.py:357-554) ever actually posts a ledger.
    #   * `on_submit` (quotation.py:290-298): an Authorization Control spending-approval check
    #     (validate_approving_authority — a permission gate, not a ledger post), then
    #     update_opportunity("Quotation") (245-251: sets any linked Opportunity's status) and
    #     update_lead() (228-230: if quotation_to=="Lead", calls Lead.set_status(update=True)).
    #     Both are STATUS writes on sibling CRM documents, never GL/SL, never a cascade.
    #   * `on_cancel` (quotation.py:300-308) — **documented here as the cancel side-effect**:
    #     clears `lost_reasons` if populated, calls super().on_cancel(), then
    #     set_status(update=True) (recomputes status from order history), update_opportunity(
    #     "Open") (reverts the linked Opportunity), and update_lead() again (reverts the Lead's own
    #     status) — an Opportunity/Lead status reset, never a ledger reversal (nothing to reverse).
    #     Unlike Delivery Note/Purchase Receipt, Quotation's own cancel carries NO
    #     downstream-submitted-document refusal (no check_next_docstatus-shaped call anywhere in
    #     quotation.py) — a Quotation can be cancelled even with Sales Orders already built from
    #     it; ERPNext's own design choice (make_sales_order() gates FRESH SO creation via
    #     `allow_sales_order_creation_for_expired_quotation`, not cancel).
    #   * `declare_enquiry_lost()` (quotation.py:260-288, @frappe.whitelist()) is a SEPARATE,
    #     non-cancel, non-amend state transition this broker does not govern — sets status="Lost"
    #     via db_set directly, refusing (frappe.throw) if a Sales Order already exists
    #     (is_fully_ordered()/is_partially_ordered()). Documented for completeness, not built into
    #     a tool: a distinct write path from this landing's governed submit/cancel pair.
    #   * **Cascade edges — cascade.py needed NO changes.** Grepping '"options": "Quotation"'
    #     across the full v16 checkout returns exactly TWO hits: Quotation's own self-referencing
    #     `amended_from` (line 209) and Sales Order Item's `prevdoc_docname` field
    #     (sales_order_item.json, Link, label "Quotation", read_only, no reqd — the field
    #     make_sales_order()'s own field_map populates, quotation.py:465). ERPNext's own
    #     get_submitted_linked_docs walks this Link-field graph generically (including
    #     child-table fields), so a submitted Sales Order built from a Quotation is discovered and
    #     ordered ahead of it in cancel order with zero doctype-specific cascade code. Quotation's
    #     cancel carries no cancel-blocking refusal of its own (see above) — it is NOT
    #     structurally isolated the way Supplier Quotation is; a submitted Sales Order dependent
    #     IS discoverable, it simply never refuses the Quotation's own cancel.
    QUOTATION: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
               "date_field": "transaction_date"},
    # Breadth (POS Invoice) — the thirteenth supported doctype, Wave 2's third row. Full
    # source-cited finding in the module docstring's own "Breadth (POS Invoice)" section above
    # (dossier at docs/plans/dossiers/pos_invoice.md). Summary of what lands in this row:
    #   * party_field="customer" (pos_invoice.json line 217) — the Sales Invoice shape. The
    #     dossier's "reqd: 1" citation is WRONG (corrected above): the schema carries only
    #     `bold: 1`; the real requirement is an application-level throw in validate()
    #     (pos_invoice.py:200-201), not a schema `reqd` flag.
    #   * status (line 1337) and grand_total (line 970) both present — the generic branch, byte-
    #     identical config to Sales Invoice.
    #   * date_field="posting_date" — KEEPS the default (a real field, line 293, reqd, default
    #     "Today") — never `transaction_date`.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading all 1119 lines of
    #     pos_invoice.py: no `def submit`/`def cancel` override.
    #   * **THE CENTRAL FINDING, CORRECTING THE PINNED DOSSIER: a real POS Invoice submit posts
    #     NEITHER a GL Entry NOR a Stock Ledger Entry of its own.** `POSInvoice.on_submit()`
    #     (pos_invoice.py:240-263) fully overrides `SalesInvoice.on_submit()` WITHOUT calling
    #     `super()` — confirmed by a full-file grep: zero hits for `make_gl_entries` or
    #     `update_stock_ledger` anywhere in pos_invoice.py. The same skip-the-direct-parent
    #     pattern repeats in validate() and on_cancel() (`super(SalesInvoice, self).<method>()`).
    #     Real GL/SL posting happens only later, on the SEPARATE, genuinely-submitted Sales
    #     Invoice a POS Closing Entry (via POS Invoice Merge Log) builds at consolidation.
    #   * This broker's OWN `ledger_preview` (`get_accounting_ledger_preview`,
    #     stock_controller.py:2090-2119) does NOT go through on_submit — it calls
    #     `doc.make_gl_entries()` directly, which resolves via ordinary Python MRO to the
    #     INHERITED `SalesInvoice.make_gl_entries` (POSInvoice never overrides it) — so
    #     `plan_submit`'s `projected_gl` for a POS Invoice comes back NON-EMPTY, a real
    #     simulation of a posting that will NEVER happen for this voucher. `tools.py` carries a
    #     new doctype-gated risk flag (`_pos_invoice_ledger_deferral_flag`) naming this plainly.
    #     `plan_cancel` needed no equivalent fix: its `projected_reversal` is a REAL bench read
    #     (`get_gl_entries`) that correctly comes back empty, already covered by the existing
    #     generic "no live GL rows found" flag.
    #   * POS Invoice is confirmed ABSENT from the `get_accounting_ledger_preview` whitelist
    #     tuple `("Purchase Receipt", "Delivery Note", "Stock Entry")` (stock_controller.py:2109)
    #     — unlike DN/PR/SE — but this is NOT the cause of the finding above (that whitelist
    #     exists for a different reason: pre-seeding SLE detail rows StockController's own
    #     make_gl_entries depends on; POS Invoice inherits SalesInvoice's independent
    #     make_gl_entries instead).
    #   * Cancel-block/consolidation — a VERIFIED CORRECTION to the dossier, which is MORE
    #     PESSIMISTIC than the real mechanism. `before_cancel` (pos_invoice.py:266-283) refuses
    #     when `consolidated_invoice` is set and that Sales Invoice's docstatus==1. Both the
    #     resulting Sales Invoice (via `Sales Invoice Item.pos_invoice`, a real Link, set by
    #     `merge_pos_invoice_into()` for BOTH the return-consolidation and ordinary batch-merge
    #     paths) AND a submitted POS Closing Entry/POS Invoice Merge Log (via the child-table
    #     Link `POS Invoice Reference.pos_invoice`, reqd, resolved up to its embedding
    #     submittable parent by frappe's own `get_referencing_documents`
    #     `parenttype`-groupby logic, frappe/desk/form/linked_with.py:356-363) ARE discoverable
    #     by this broker's existing `get_submitted_linked_docs` blast-radius refusal — traced
    #     directly against frappe's linked_with.py source, not the dossier's claim that neither
    #     is walked. Zero new cascade code needed. Consolidation boundary unchanged: this landing
    #     does not add tools for POS Invoice Merge Log (TRIAGE.md REFUSE) or drive POS Closing
    #     Entry's batch machinery — disclosure via the blast-radius walk is not governance.
    #   * update_outstanding_for_self CONFIRMED ABSENT (pos_invoice.json's full 185-field list)
    #     — but POS Invoice is a THIRD is_return shape, not a repeat of DN/PR's "no receivable at
    #     all": it DOES carry debit_to, yet posts no GL of its own regardless (see above).
    #     `_return_risk_flags` gains a doctype-gated branch for this.
    #   * is_pos ALWAYS truthy on a real POS Invoice (validate() throws otherwise,
    #     pos_invoice.py:203-206) — `_pos_risk_flags` (already doctype-agnostic) fires
    #     unconditionally; no code change, just a different usage pattern of existing machinery.
    #   * Cascade edges — cascade.py needed NO changes (doctype-blind by construction). Real Link
    #     fields to POS Invoice: its own amended_from/return_against, Sales Invoice
    #     Item.pos_invoice, POS Invoice Reference.pos_invoice/.return_against — all walked
    #     generically by get_submitted_linked_docs.
    POS_INVOICE: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD,
                 "date_field": "posting_date"},
    # Breadth (Dunning) — the fourteenth supported doctype, Wave 2's fourth row. Full source-cited
    # finding in the module docstring's own "Breadth (Dunning)" section above (dossier at
    # docs/plans/dossiers/dunning.md). Summary of what lands in this row:
    #   * party_field="customer" (dunning.json lines 219-225, reqd: 1 — the dossier's citation is
    #     CORRECT here) — the generic branch, byte-identical config shape to Sales Invoice/Sales
    #     Order (status + grand_total both present, no _list_fields change needed).
    #   * date_field="posting_date" — KEEPS the default (a real field, lines 92-98, reqd, default
    #     "Today") — the snapshot date validate_overdue_payments uses for its interest calc, not a
    #     GL posting date (there is no GL posting — see below).
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading all 276 lines of dunning.py: no
    #     `def submit`/`def cancel`/`def on_submit` override at all.
    #   * **THE CENTRAL FINDING, GENUINELY NEW AND DIFFERENT FROM EVERY PRIOR "posts no GL"
    #     DOCTYPE: Dunning has NO make_gl_entries method anywhere in its MRO** (Dunning ->
    #     AccountsController -> TransactionBase -> StatusUpdater -> Document — none of these
    #     define it; it exists only on StockController, not an ancestor of AccountsController, and
    #     on a short closed list of individual doctype controllers Dunning shares no ancestry
    #     with). Unlike Sales Order/Purchase Order/Material Request/Supplier Quotation/Quotation
    #     (all StockController descendants with a real, callable, conditionally-no-op
    #     make_gl_entries), this broker's own `ledger_preview` RPC would call `doc.make_gl_entries()`
    #     as a bare, unguarded method call (stock_controller.py:2112) and raise AttributeError on a
    #     live bench — every plan_submit for a Dunning would refuse with an opaque bench error if
    #     this call were made. The dossier's own hedge ("projected_gl will be empty, same as
    #     SO/PO/MR") describes the WRONG mechanism — those three have a callable no-op, Dunning has
    #     no callable at all. FIX: `tools.py`'s `_tool_plan_submit` skips the
    #     `client.ledger_preview()` network call entirely for `doctype == DUNNING` and reports
    #     `projected_gl=[]` by construction, paired with a new doctype-gated risk flag
    #     (`_dunning_ledger_preview_unavailable_flag`) naming plainly why. `plan_cancel` needed no
    #     equivalent fix: `get_gl_entries` is a real, safe bench read that naturally returns empty.
    #   * on_cancel (dunning.py:150-164) sets a stock ignore_linked_doctypes list (dead weight —
    #     Dunning never creates a GL Entry row, so nothing on that list applies) and calls no
    #     frappe.throw anywhere — no cancel refusal of its own. Cancelling a Dunning never touches
    #     the linked Sales Invoice(s); update_linked_dunnings is wired to Sales Invoice's own
    #     post-submit hook only, never to Dunning's on_cancel.
    #   * Cascade edges — cascade.py needed NO changes (doctype-blind by construction). Only real
    #     Link naming Dunning: its own amended_from self-link. A CORRECTION to the dossier's claimed
    #     "disclosure gap": Payment Entry Reference.reference_name is fieldtype Dynamic Link (not
    #     Data, as the dossier claims — verified by dumping the raw field dict), and frappe's own
    #     get_references_across_doctypes_by_dynamic_link_field (linked_with.py:269-321) resolves
    #     Dynamic Link fields with the SAME child-table-to-parent promotion static Links get — so a
    #     submitted Payment Entry referencing this Dunning via reference_doctype="Dunning" already
    #     surfaces in get_submitted_linked_docs, covered by the existing generic blast-radius
    #     refusal with zero new code. No disclosure gap.
    DUNNING: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD,
             "date_field": "posting_date"},
    # Breadth (Stock Reconciliation) — the fifteenth supported doctype, Wave 2's fifth row. Full
    # source-cited finding in the module docstring's own "Breadth (Stock Reconciliation)" section
    # above (dossier at docs/plans/dossiers/stock_reconciliation.md — the dossier is SILENT on
    # transport, not wrong; everything else it claims is confirmed correct). Summary of what lands
    # in this row:
    #   * party_field=None (stock_reconciliation.json's full 17-field list: no customer/supplier/
    #     party field at all) — the JE/MR/SE/Q shape.
    #   * status AND grand_total both confirmed ABSENT (same 17-field list) — purpose (a two-way
    #     fork, not Stock Entry's 13-way one) + difference_amount (the single aggregate variance)
    #     stand in — a SIXTH `_list_fields` branch, not a reuse of Stock Entry's own (different
    #     substitute fields).
    #   * date_field="posting_date" — KEEPS the default (a real field, lines 66-73, reqd, default
    #     "Today") — never transaction_date.
    #   * **submit_via=SUBMIT_VIA_CLIENT_RPC — NOT the run_method surface every non-JE doctype so
    #     far has used.** StockReconciliation overrides `submit()`/`cancel()` themselves (not just
    #     on_submit/on_cancel), neither decorated `@frappe.whitelist()` (confirmed: the file's only
    #     three `@frappe.whitelist()` occurrences all sit after `cancel` ends) — the exact JE
    #     mechanism (journal_entry.py:186,195): frappe's run_method REST dispatch calls
    #     doc.is_whitelisted(method) first, which 403s an undecorated override. Zero new transport
    #     code needed — submit_document/cancel_document and _governed_write's doc-passthrough were
    #     already built doctype-generic for JE, not JE-specific; pacioli_guard's body_scoped_target
    #     is likewise already doctype-generic. Async-queue caveat (>100 item rows enqueues a
    #     background job) documented, not gated — governed_submit's own readback/degrade path
    #     already covers an outcome that doesn't match the immediate response.
    #   * **THE CENTRAL LEDGER-PREVIEW FINDING — a FIFTH shape, distinct from Dunning (uncallable),
    #     POS Invoice (callable, misleading NON-empty), and SO/PO/MR/SQ/Q (callable, HONESTLY
    #     empty): Stock Reconciliation's preview is callable, raises nothing, and returns an EMPTY
    #     projected_gl too — but the emptiness is DISHONEST.** StockReconciliation IS a
    #     StockController subclass (make_gl_entries is inherited, callable, no AttributeError), but
    #     is confirmed ABSENT from get_accounting_ledger_preview's own SLE-seeding whitelist tuple
    #     (stock_controller.py:2109-2110, names only Purchase Receipt/Delivery Note/Stock Entry) and
    #     carries no update_stock field either — so update_stock_ledger() is never called in the
    #     preview's savepoint, no SLE rows exist for the voucher, get_gl_entries's own
    #     get_stock_ledger_details() query (and Stock-Reconciliation's own get_voucher_details
    #     branch, which iterates that same empty SLE map) finds nothing, and process_gl_map([])
    #     returns [] cleanly — no exception. UNLIKE SO/PO/MR/SQ/Q, whose own real submit ALSO posts
    #     no GL (the emptiness is honest), a REAL Stock Reconciliation submit ALWAYS writes Stock
    #     Ledger Entry rows (update_stock_ledger() is unconditional in on_submit, and REFUSES the
    #     submit outright if it would write zero SLE rows) and, whenever perpetual inventory is
    #     enabled, ALSO writes real GL Entry rows from those same rows — none of which the preview
    #     can show. FIX: a new doctype-gated risk flag
    #     (`_stock_reconciliation_ledger_preview_incomplete_flag`) fires for plan_submit only,
    #     naming the false negative plainly; the preview CALL itself is not skipped (unlike Dunning
    #     — it's callable and harmless here, never raises). plan_cancel needs no fix: its own
    #     projected_reversal already reads real posted GL Entry rows.
    #   * Absolute-vs-delta correction shape (governance-relevant, documented not gated): submit
    #     posts an ABSOLUTE state (qty_after_transaction set directly to the entered value), not a
    #     delta — the variance posts to the Difference Account (an adjustment account), the ledger's
    #     own way of saying "correcting a prior error," not a normal counterparty.
    #   * Cancel refusal (validate_reserved_stock, also fires on submit) is a LIVE QUERY against
    #     Stock Reservation Entry — no field on this document carries reserved-qty information, so
    #     it is structurally invisible to plan-tier disclosure at both submit and cancel — matches
    #     the dossier exactly, no new gate (the answered ERPNext refusal surfaces safely at the real
    #     call either way).
    #   * Cascade edges — cascade.py needed NO changes (doctype-blind by construction). Only real
    #     Link naming Stock Reconciliation across the full v16 checkout: its own amended_from
    #     self-link. Zero external dependents.
    STOCK_RECONCILIATION: {"party_field": None, "submit_via": SUBMIT_VIA_CLIENT_RPC,
                          "date_field": "posting_date"},
    # Breadth (Landed Cost Voucher) — the sixteenth supported doctype, Wave 2's sixth row and
    # LAST. Full source-cited finding in the module docstring's own "Breadth (Landed Cost
    # Voucher)" section above (dossier at docs/plans/dossiers/landed_cost_voucher.md — correct on
    # party_field/status/grand_total/posting_date, WRONG on the Dynamic Link's discoverability, a
    # correction this landing makes against frappe's own linked_with.py, the same class of fix
    # Dunning's landing made for Payment Entry Reference). Summary of what lands in this row:
    #   * party_field=None (no customer/supplier/party field anywhere in the 15-field schema —
    #     costs allocate onto already-submitted receipts; supplier identity lives on those
    #     receipts, not here) — the JE/MR/SE/Q/SR shape.
    #   * status AND grand_total both confirmed ABSENT — distribute_charges_based_on (context) +
    #     total_taxes_and_charges (the one aggregate) stand in — a SEVENTH _list_fields branch
    #     (same absence shape as Stock Reconciliation, different substitute fields, so its own
    #     branch, not a reuse — the same lesson Stock-Reconciliation-vs-Stock-Entry already taught).
    #   * date_field="posting_date" — a real field (reqd, default "Today"), never
    #     transaction_date.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading all 522 lines of
    #     landed_cost_voucher.py: no def submit/def cancel override, only on_submit/on_cancel
    #     hooks. This config dict is byte-for-byte IDENTICAL to Stock Entry's own entry — the
    #     SECOND zero-diff pairing this campaign has found (after Supplier Quotation/Purchase
    #     Order).
    #   * THE CENTRAL LEDGER-PREVIEW FINDING: LandedCostVoucher(Document) — never
    #     AccountsController, never StockController — has no make_gl_entries anywhere in its MRO,
    #     the Dunning shape (preview UNCALLABLE, skipped entirely rather than sent), but sharper:
    #     even a working preview would describe the WRONG document, since this doctype's real
    #     ledger effect never posts under its own voucher_type at all.
    #   * on_submit/on_cancel both call the SAME update_landed_cost() — a real revaluation of the
    #     Purchase Receipt/Purchase Invoice/Stock Entry/Subcontracting Receipt documents named in
    #     purchase_receipts: their item valuation is recalculated and their existing Stock Ledger
    #     Entry + GL Entry rows are reversed and reposted at the new rate, on BOTH submit (raises
    #     the cost) and cancel (reverses it) — the FIRST Pacioli doctype whose own submit/cancel
    #     rewrites a DIFFERENT document's posted ledger rows.
    #   * THE DYNAMIC-LINK CORRECTION (the sharpest finding): contra the dossier AND Purchase
    #     Receipt's own earlier landing (b2d06a9), Landed Cost Purchase Receipt.receipt_document
    #     (a Dynamic Link) IS genuinely discoverable by get_submitted_linked_docs — frappe's own
    #     dynamic-link resolution is a LIVE distinct-value query, not a static schema scan (the
    #     exact mechanism Dunning's own landing already proved for Payment Entry Reference). A
    #     submitted LCV referencing a receipt is found when planning to cancel that receipt, and
    #     this broker's pre-existing generic refusal already blocks it — MORE protective than raw
    #     ERPNext's own check_next_docstatus (which never checks for a linked LCV at all).
    #     Landed Cost Vendor Invoice.vendor_invoice is a SECOND, independent edge into Purchase
    #     Invoice — a real plain Link, missed by the dossier entirely.
    #   * The REVERSE direction (cancelling the LCV itself) finds no inbound dependents at all
    #     (nothing links to Landed Cost Voucher except its own amended_from) — correctly empty,
    #     but that does not mean safe: the real hazard (the receipts' SLE/GLE repost) is entirely
    #     outside what the Link-graph blast-radius mechanism was built to see.
    #   * Cascade — cascade.py needs NO changes, same finding as every prior breadth increment.
    LANDED_COST_VOUCHER: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                         "date_field": "posting_date"},
    # Breadth (Request for Quotation) — the seventeenth supported doctype, Wave 3's first row. Full
    # source-cited finding in the module docstring's own "Breadth (Request for Quotation)" section
    # above (dossier at docs/plans/dossiers/request_for_quotation.md). Summary of what lands in
    # this row:
    #   * party_field=None — RFQ dispatches to MULTIPLE suppliers via a required child table
    #     (suppliers, Table -> Request for Quotation Supplier); the one supplier-shaped header
    #     field (vendor) is hidden/read-only/optional, set only for a single-supplier PDF print,
    #     mostly blank — the same self-clearing judgment that earned Material Request's own
    #     party_field=None.
    #   * status present (Draft/Submitted/Cancelled), grand_total confirmed ABSENT — the IDENTICAL
    #     shape Material Request's branch was built for, but NOT a reuse of that branch (see
    #     _list_fields below) because Material Request's own substitute fields
    #     (material_request_type/per_ordered/per_received) do not exist on this doctype's schema.
    #   * date_field="transaction_date" — confirmed no posting_date field anywhere in the 384
    #     enumerated fields; the sixth doctype on this pattern.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading all 675 lines of
    #     request_for_quotation.py: on_submit/on_cancel are bare, undecorated hooks; no
    #     def submit/def cancel override anywhere; the run_method vector never 403s.
    #   * **THE LEDGER-PREVIEW FINDING: RFQ is the SO/PO/MR/SQ/Q "honest-empty" category, NOT the
    #     Dunning/LCV "uncallable" category — verified directly, not assumed from the dossier's own
    #     hedge.** class RequestforQuotation(BuyingController) is the SAME declared parent
    #     Purchase Order/Material Request/Supplier Quotation already carry, and BuyingController ->
    #     SubcontractingController -> StockController (confirmed by reading each class's own
    #     declaration) means RFQ genuinely INHERITS a real, callable make_gl_entries
    #     (stock_controller.py:292) — unlike Dunning (AccountsController only) and Landed Cost
    #     Voucher (bare Document), neither of which has StockController in its MRO at all. The
    #     native ledger_preview RPC's bare doc.make_gl_entries() call does NOT AttributeError for
    #     RFQ, so it is NOT added to tools.py's (DUNNING, LANDED_COST_VOUCHER) skip tuple. The
    #     emptiness is honest, not merely non-crashing: RFQ never calls update_stock_ledger()
    #     anywhere in its source, so make_gl_entries's own get_gl_entries()/
    #     get_stock_ledger_details() query always finds zero real Stock Ledger Entry rows for this
    #     voucher and returns [] unconditionally — the same honest-empty mechanism SO/PO/MR/SQ/Q
    #     already ride.
    #   * on_submit sends real email to suppliers (send_to_supplier(), external-communication side
    #     effect, never reversed on cancel) — documented in the module docstring's own section for
    #     plan-tier disclosure; no broker gate, matching Material Request's own hold/closed
    #     disclosure-gap precedent (named in prose, no new machinery).
    #   * Cascade edges — cascade.py needs NO changes (doctype-blind by construction). Only real
    #     Link naming RFQ: supplier_quotation_item.request_for_quotation (confirmed fieldtype
    #     Link) plus RFQ's own amended_from self-link. A CORRECTION to the dossier's own §7: it
    #     additionally claims supplier_quotation_item.request_for_quotation_item is itself a Link
    #     — dumping the raw field dict shows this is WRONG (fieldtype is Data, not Link); harmless
    #     to the conclusion since the one real Link already covers the cascade with zero new code.
    REQUEST_FOR_QUOTATION: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                           "date_field": "transaction_date"},
    # Breadth (Blanket Order) — the eighteenth supported doctype, Wave 3's second row. Full
    # source-cited finding in the module docstring's own "Breadth (Blanket Order)" section above
    # (dossier at docs/plans/dossiers/blanket_order.md — the ninth landing in a row to find at
    # least one dossier error). Summary of what lands in this row:
    #   * party_field=None — BlanketOrder carries BOTH customer and supplier as real header Link
    #     fields (client-side depends_on gated on blanket_order_type, NEITHER reqd at schema level,
    #     NEITHER server-enforced — set_party_item_code() dispatches on type but never validates
    #     either is populated). A weaker server guarantee than Material Request's own explicit
    #     clearing, but still no single static fieldname is ever unconditionally the party.
    #   * status confirmed ABSENT, grand_total confirmed ABSENT (zero hits, 18-field enumeration) —
    #     the Stock Entry/Stock Reconciliation/Landed Cost Voucher absence shape, but NOT a reuse of
    #     any of those three branches: substitutes differ (blanket_order_type + to_date, not
    #     purpose/difference_amount/distribute_charges_based_on), and uniquely this branch ALSO
    #     splices real party context (customer/supplier) none of those three carry at all.
    #   * date_field="from_date" — confirmed no posting_date AND no transaction_date field anywhere
    #     in the 18 enumerated fields; the FIRST doctype on neither established date-field pattern.
    #     to_date carries allow_on_submit=1 but is never date_field (a closed-books check keys off
    #     when the agreement STARTS).
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading all 201 lines of blanket_order.py:
    #     no def submit/def cancel override, and — a genuine rarity this campaign — no on_submit/
    #     on_cancel method defined AT ALL (the dossier's "no on_submit hook at all" claim is
    #     correct).
    #   * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: Blanket Order is the Dunning/Landed
    #     Cost Voucher "uncallable" category, NOT the SO/PO/MR/SQ/Q/RFQ "honest-empty" one.**
    #     class BlanketOrder(Document) — never AccountsController, never StockController, no
    #     make_gl_entries anywhere in its MRO (the same full-tree grep Dunning/LCV's own landings
    #     ran). ERPNext's own preview would AttributeError on a live bench if called. Joins the skip
    #     tuple in tools.py: (DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER). Unlike LCV, Blanket
    #     Order has no revaluation-elsewhere side effect (see on_cancel below) — the simpler Dunning
    #     shape, one flag, submit-direction only; plan_cancel needs no new flag (get_gl_entries is
    #     already honest).
    #   * on_cancel — no method defined at all; NO refusal gate, NO direct side-effect of its own. A
    #     dossier correction: the update_ordered_qty() quantity-counter effect it describes as
    #     "cancel only" is actually triggered by a REFERENCING Sales/Purchase Order's own on_submit
    #     AND on_cancel (both directions, via inherited StockController.update_blanket_order()),
    #     never by this Blanket Order's own lifecycle — an upstream effect, and a Bin-style quantity
    #     counter, never a ledger write.
    #   * Cascade — a genuine dossier correction: THREE real Link fields name Blanket Order, not the
    #     dossier's claimed two — sales_order_item.blanket_order and purchase_order_item.blanket_order
    #     (dossier got these right) PLUS quotation_item.blanket_order (dossier wrongly called this
    #     "not a Link reference"; dumping the raw field dict shows fieldtype: "Link", identical shape
    #     to the SO/PO Item fields). cascade.py needs no changes regardless (doctype-blind,
    #     get_submitted_linked_docs walks all three generically) — the correction widens the real
    #     disclosure surface (a submitted Quotation is also discoverable), not a gap.
    BLANKET_ORDER: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                    "date_field": "from_date"},
    # Breadth (Job Card) — the nineteenth supported doctype, Wave 3's third row. Full source-cited
    # finding in the module docstring's own "Breadth (Job Card)" section above (dossier at
    # docs/plans/dossiers/job_card.md — the tenth landing in a row to find at least one dossier
    # imprecision). Summary of what lands in this row:
    #   * party_field=None — a shop-floor operation record; zero customer/supplier/party fields
    #     anywhere across all 95 enumerated fields (job_card.json) — no party concept to gate at
    #     all, unlike Blanket Order's own two gated fields.
    #   * status confirmed PRESENT (8 options: Open/Work In Progress/Partially
    #     Transferred/Material Transferred/On Hold/Submitted/Cancelled/Completed), grand_total
    #     confirmed ABSENT (only total_completed_qty/total_time_in_mins, both non-monetary Float
    #     counters; no accounts child table). The Material Request/RFQ shape (party=None +
    #     status-present + grand_total-absent) — but NOT a reuse of either: substitutes differ
    #     (work_order/operation/for_quantity, all confirmed real Link/Float fields with
    #     in_list_view=1, don't exist on material_request.json or request_for_quotation.json) — the
    #     eleventh _list_fields branch.
    #   * date_field="posting_date" — confirmed present (Date, default "Today"), KEEPS the default;
    #     Job Card carries neither transaction_date nor from_date. No new date-field plumbing.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading all 1875 lines of job_card.py:
    #     class JobCard(Document) overrides neither submit() nor cancel(); only on_submit
    #     (line 776)/on_cancel (line 783) hooks. 14 separate @frappe.whitelist() callables also
    #     exist (see the side-surface caveat below) but none of them is a submit/cancel override.
    #   * **THE LEDGER-PREVIEW FINDING, VERIFIED FROM THE MRO: Job Card is the Dunning/Landed Cost
    #     Voucher/Blanket Order "uncallable" category, NOT the SO/PO/MR/SQ/Q/RFQ "honest-empty"
    #     one.** class JobCard(Document) — never AccountsController, never StockController (the
    #     import block pulls only two EXCEPTION classes from stock_controller, never the class
    #     itself); no make_gl_entries anywhere in its MRO (the same full-tree grep Dunning/LCV/
    #     Blanket Order's own landings ran). ERPNext's own preview would AttributeError on a live
    #     bench if called. Joins the skip tuple in tools.py: (DUNNING, LANDED_COST_VOUCHER,
    #     BLANKET_ORDER, JOB_CARD). Like Blanket Order, Job Card has no revaluation-elsewhere side
    #     effect — the simpler Dunning/Blanket-Order shape, one flag, submit-direction only;
    #     plan_cancel needs no new flag (get_gl_entries is already honest).
    #   * on_cancel (783-785) calls update_work_order() (954-981) and set_transferred_qty()
    #     (1174-1203) — the SAME two methods on_submit calls — a Work Order operation/quantity
    #     RESET only (completed_qty/process_loss_qty/pending_qty/produced_qty recomputed from a
    #     live query, wo.flags.ignore_validate_update_after_submit set deliberately), never a GL
    #     Entry, Payment Ledger row, or settled-payment state. No frappe.throw anywhere in the file
    #     — no refusal gate of its own on cancel.
    #   * Cascade — a genuine dossier clarification, not an outright error: the dossier's own §7
    #     names the same five external Link-carrying doctypes this landing confirms (Stock Entry,
    #     Material Request, Subcontracting Receipt Item, Subcontracting Order Item, Purchase Order
    #     Item), but frames Material Request's own edge only as "may be created from Job Card via
    #     make_material_request() whitelist" rather than "carries a job_card Link field" like the
    #     other four. Dumping material_request.json's raw field dict shows Material Request's
    #     job_card field is byte-for-byte the same shape as the other four (fieldtype: "Link",
    #     options: "Job Card", read_only: 1) — a real, standing header-level Link, not merely a
    #     creation-lineage artifact. cascade.py needs no changes regardless (doctype-blind,
    #     get_submitted_linked_docs walks all five generically) — the correction plainly states
    #     Material Request stands on equal footing with the other four, not a lesser relationship.
    #   * THE SIDE-SURFACE CAVEAT: Job Card exposes 14 separate @frappe.whitelist() callables (6
    #     instance methods incl. pause_job/resume_job/start_timer/complete_job_card/
    #     make_stock_entry_for_semi_fg_item, 8 module-level functions incl. make_stock_entry/
    #     make_subcontracting_po/make_material_request/make_corrective_job_card) outside this
    #     broker's submit/cancel/amend 5-verb surface. This entry governs ONLY submit/cancel (plus
    #     the generic amend/get/list wrappers) — it grants NOTHING toward any of these 14 methods,
    #     several of which mutate state outside docstatus entirely (pause_job/resume_job/
    #     start_timer flip is_paused/append time logs without touching docstatus — Work Order's own
    #     stop_unstop() shape, generalized) or create new documents (Stock Entry, Purchase Order,
    #     Material Request, a corrective Job Card). No tool is built for any of them this landing.
    JOB_CARD: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
              "date_field": "posting_date"},
    # Breadth (BOM) — Wave 3's fourth row, the twentieth supported doctype, THE FIRST DATELESS ONE.
    # Full source-cited finding in the module docstring above (dossier at
    # docs/plans/dossiers/bom.md — three corrections this landing: the real field count is 94 not
    # "157"; Supplier Quotation carries NO BOM link (§7 error); update_cost mutates a SUBMITTED
    # BOM, undersold as "client RPC support" in §4). The load-bearing pins:
    #   * party_field=None — a recipe, not a transaction (94-field enumeration: no party anywhere).
    #   * date_field=None — NOT a fieldname and NOT the absent-key default: the explicit
    #     source-verified pin that BOM has no date field at all (zero Date/Datetime fields).
    #     The whole closed-books chain branches on this DECLARED pin (never an empty read):
    #     _posting_date_of → plan.NO_DATE_FIELD, _locks_for → no period-lock read,
    #     check_red_line → explicit sentinel pass. Equal to ERPNext three ways from source (not
    #     in period_closing_doctypes; check_freezing_date is GL-path-only and BOM posts no GL;
    #     no date exists for any range check to bite on) — see the table comment above.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — BOM(WebsiteGenerator) overrides neither submit()
    #     nor cancel(); on_submit/on_cancel hooks only (bom.py:397/401).
    #   * Ledger preview: UNCALLABLE category — no make_gl_entries anywhere in the
    #     WebsiteGenerator→Document MRO; joins the skip tuple in tools.py, now
    #     (DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM), with
    #     _bom_ledger_preview_unavailable_flag naming it honestly.
    #   * on_cancel carries ERPNext's own refusal gate (validate_bom_links: refuses while another
    #     submitted+active BOM uses this one as a sub-assembly) — honored via the standing
    #     answered-ErpnextError path; the broker's own blast-radius gate usually refuses first
    #     (a parent BOM's BOM Item row IS a submitted link). 29 Link→BOM fields across 25
    #     doctypes make this the widest cancel fan-in landed so far.
    #   * Side surface NOT granted: 10 whitelist callables, most notably update_cost (rewrites a
    #     SUBMITTED BOM's stored costs + recursively its parents', outside docstatus entirely)
    #     and make_variant_bom (creates a new BOM). No tool built, nothing granted toward them.
    BOM: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
          "date_field": None},
    # Breadth (Work Order) — Wave 3's fifth row, the twenty-first supported doctype, THE FIRST
    # DATETIME-DATED ONE. Full source-cited finding in the module docstring above (dossier at
    # docs/plans/dossiers/work_order.md — four corrections this landing, sharpest: its §9
    # "cancel does NOT refuse on linked submitted documents" is an outright inversion —
    # validate_cancel THROWS on any submitted Stock Entry; and its §3 missed that
    # planned_start_date is a DATETIME, so the plain fieldname swap it recommends would
    # hard-deny every Work Order write). The load-bearing pins:
    #   * party_field=None — internal manufacturing order (86-field enumeration: no party).
    #   * date_field="planned_start_date" — Datetime (reqd, default "now", allow_on_submit).
    #     The VALUE needs the date-part projection (_posting_date_of truncates a well-formed
    #     datetime to its ISO date; malformed stays raw and refused) — see the table comment.
    #     allow_on_submit means the date can move post-plan without a docstatus change; it
    #     still bumps `modified`, so check_fresh catches the drift (the standing TOCTOU belt).
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — WorkOrder(Document) overrides neither submit()
    #     nor cancel() (work_order.py:70; hooks only at 929/949).
    #   * Ledger preview: UNCALLABLE category — no make_gl_entries in the MRO; joins the skip
    #     tuple, now (DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER),
    #     with _work_order_ledger_preview_unavailable_flag.
    #   * on_cancel opens with TWO real bench gates (validate_cancel): refuses while status is
    #     "Stopped", and refuses while ANY submitted Stock Entry references this Work Order —
    #     the second is the broker's own blast-radius refusal made bench-side too; the first is
    #     a STATUS (not docstatus) condition plan-time disclosure cannot see, honored at execute
    #     via the standing answered-ErpnextError path.
    #   * Side surface NOT granted: 19 whitelist callables including TWO submitted-state status
    #     mutators — stop_unstop (Stopped <-> live) and close_work_order (terminal Closed, which
    #     the dossier never mentioned) — plus seven document factories. Nothing granted, no tool
    #     built; both mutators move the very status the cancel gate keys on, outside this
    #     broker's sight.
    WORK_ORDER: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                 "date_field": "planned_start_date"},
    # Breadth (Asset) — Wave 3's sixth and final row, the twenty-second supported doctype. Full
    # source-cited finding in the module docstring above (dossier at
    # docs/plans/dossiers/asset.md — right on the async scope boundary; corrections: 18 not 9
    # cascade edges, the structural cancel consequence unstated, the SECOND async GL channel
    # (make_post_gl_entry, daily scheduler) missed entirely). The load-bearing pins:
    #   * party_field=None — the asset_owner/supplier/customer trio is ownership metadata, not
    #     a GL party (the GL debits fixed-asset, credits CWIP — no party account anywhere).
    #   * date_field="available_for_use_date" — THE GL POSTING DATE (make_gl_entries stamps it
    #     on both rows; the deferred-GL scheduler keys on it), chosen over the also-real
    #     purchase_date. Asset IS in period_closing_doctypes — the first Wave-3 row whose
    #     closed-books check is natively EQUAL to ERPNext, not equal-or-stricter. Empty-date
    #     refusal matches the bench's own mandatory-at-docstatus-1 + validate_in_use_date gates.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — Asset(AccountsController) overrides neither
    #     submit() nor cancel() (asset.py:41; hooks only).
    #   * Ledger preview: CALLABLE — Asset has a real make_gl_entries (asset.py:924), does NOT
    #     join the skip tuple; projected GL is conditional by the method's own body, each empty
    #     case disclosed data-driven by _asset_submit_risk_flags (tools.py).
    #   * TWO async GL channels armed by submit, neither governable: depreciation JEs
    #     (post_depreciation_entries, daily) and the deferred CWIP transfer for a future
    #     available_for_use_date (make_post_gl_entry, daily). Disclosed at plan time.
    #   * STRUCTURAL: submit auto-creates AND submits an Asset Movement linking back here, so
    #     leaf-node plan_cancel refuses for EVERY submitted Asset — plan_cascade_cancel is the
    #     governed cancel path (equal in effect to ERPNext's own silent multi-doc unwind,
    #     stricter in consent: the human sees the whole graph first).
    #   * validate_cancellation refuses on status (In Maintenance/Out of Order, or anything
    #     outside Submitted/Partially Depreciated/Fully Depreciated) — readable on the draft, so
    #     _asset_cancel_risk_flags pre-disclose a doomed cancel at plan time.
    #   * Side surface NOT granted: 15 whitelist callables (document factories incl.
    #     make_sales_invoice/split_asset/transfer_asset/make_journal_entry). No tool built.
    ASSET: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
            "date_field": "available_for_use_date"},
    # Breadth (Packing Slip) — Wave 4's first row, the twenty-third supported doctype, THE SECOND
    # DATELESS ONE (reuses BOM's own NO_DATE_FIELD machinery exactly — no new plan.py/tools.py
    # code for this axis). Full source-cited finding in the module docstring above (dossier at
    # docs/plans/dossiers/packing_slip.md — correct on every axis it checked; its one omission,
    # never flagged this landing's own new finding below, was never asked to check for). The
    # load-bearing pins:
    #   * party_field=None — a shipment-packing record; only Link fields are delivery_note
    #     (required, the Draft DN this slip packs) and letter_head (cosmetic) — no party anywhere
    #     across all 22 fields.
    #   * date_field=None — THE SECOND declared-dateless pin (a Date/Datetime-typed scan of all
    #     22 fields returns empty). Reuses plan.NO_DATE_FIELD/_posting_date_of/_locks_for/
    #     check_red_line's existing branch as-is — this landing adds zero new dateless machinery,
    #     only a second doctype proving its OWN datelessness independently from source.
    #   * A GENUINELY NEW FINDING, not shared with any prior doctype: Packing Slip ALSO carries
    #     no "company" field. The nine existing "wrong books" call sites in tools.py already
    #     handle this correctly and unchanged: a company-PINNED target refuses every governed
    #     write (None never matches a pinned company — an honest, deny-biased refusal, not a
    #     bug), while the documented UNPINNED posture (registry.py's optional company pin;
    #     REG_UNPINNED in the test suite, previously built for exactly this scenario) governs it
    #     cleanly. No new company_field pin or bypass is built this landing — see the module
    #     docstring's own paragraph for why that is a deliberate, left-open design decision.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — PackingSlip(StatusUpdater) overrides neither
    #     submit() nor cancel() (packing_slip.py:13; on_submit/on_cancel hooks only at 73/76,
    #     both one-line calls to update_prevdoc_status()).
    #   * Ledger preview: UNCALLABLE category — StatusUpdater(Document), no make_gl_entries
    #     anywhere in the MRO; joins the skip tuple in tools.py, now (DUNNING,
    #     LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP), with
    #     _packing_slip_ledger_preview_unavailable_flag naming it honestly. on_submit/on_cancel
    #     both recompute a QUANTITY COUNTER (packed_qty on Delivery Note Item/Packed Item, via a
    #     raw SQL UPDATE in StatusUpdater.update_qty) — never a ledger row either direction.
    #   * Cascade: a CASCADE LEAF — the ONLY Link -> Packing Slip field anywhere in the v16 tree
    #     is its own self-referencing amended_from (full-tree scan). No external doctype can ever
    #     block this Packing Slip's own cancel via the blast-radius gate; on_cancel itself throws
    #     nothing.
    #   * Side surface: ONE read-only whitelist callable (item_details, a search filter) — no
    #     mutation exists to withhold, the smallest side-surface caveat this campaign has found.
    PACKING_SLIP: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                   "date_field": None},
    # Breadth (Cost Center Allocation) — Wave 4's second row, the twenty-fourth supported
    # doctype. Full source-cited finding in the module docstring above (dossier at
    # docs/plans/dossiers/cost_center_allocation.md — a DOSSIER CORRECTION this landing settles
    # from source: its own §3 and §12 simultaneously claimed a real, required valid_from Date
    # field AND "DATELESS in the broker sense" — contradictory claims; verified that valid_from
    # IS the real date_field, a NORMAL dated doctype, never the NO_DATE_FIELD sentinel). The
    # load-bearing pins:
    #   * party_field=None — a recurring-allocation rule, not a transaction with a counterparty;
    #     main_cost_center (the routing destination) and company (fetched) are the only Link
    #     fields across all 7 enumerated fields — no party concept anywhere.
    #   * date_field="valid_from" — a real, required Date field (default "Today"), the SIXTH
    #     distinct date-fieldname pattern this campaign has found. Rides the existing generic
    #     _date_field_for/_posting_date_of/_locks_for/check_red_line machinery unchanged (the
    #     same fieldname-splice shape Blanket Order's from_date and Asset's
    #     available_for_use_date already proved generalizes) — zero new plan.py/tools.py code.
    #     The closed-books check runs the NORMAL, equal-or-stricter path — never the dateless
    #     sentinel.
    #   * company IS present (Link, reqd, fetch_from main_cost_center.company) — UNLIKE Packing
    #     Slip, the standing "wrong books" belt applies in its ordinary form; no companyless edge
    #     case here.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — CostCenterAllocation(Document) overrides neither
    #     submit() nor cancel(), and defines NO on_submit/on_cancel hook of any kind at all
    #     (cost_center_allocation.py:30; only __init__/validate/clear_cache exist) — the simplest
    #     submit/cancel lifecycle this campaign has found.
    #   * Ledger preview: UNCALLABLE category — a direct Document subclass, no make_gl_entries
    #     anywhere; joins the skip tuple in tools.py, now (DUNNING, LANDED_COST_VOUCHER,
    #     BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION), with
    #     _cost_center_allocation_ledger_preview_unavailable_flag naming it honestly.
    #   * Cascade: a CASCADE LEAF — the ONLY Link -> Cost Center Allocation field anywhere in the
    #     v16 tree is its own self-referencing amended_from (full-tree scan). No other doctype
    #     can ever block this doctype's own cancel via the blast-radius gate.
    #   * Side surface: ZERO @frappe.whitelist() callables — the smallest side-surface this
    #     campaign has found; nothing to withhold because nothing exists.
    COST_CENTER_ALLOCATION: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                             "date_field": "valid_from"},
    # Breadth (Supplier Scorecard Period) — Wave 4's third row, the twenty-fifth supported
    # doctype, and Wave 4's FIRST ROW WITH A REAL PARTY FIELD (Packing Slip/Cost Center
    # Allocation both carry party_field=None). Full source-cited finding in the module docstring
    # above (dossier at docs/plans/dossiers/supplier_scorecard_period.md — correct on every fact
    # it checked; one framing correction and one omission corrected there: the dossier's own "GL
    # party" label is misleading since this doctype posts no GL at all, and its RED FLAGS section
    # never checked the PARENT Supplier Scorecard doctype's own scheduler-driven auto-submission).
    # The load-bearing pins:
    #   * party_field="supplier" — a real, required, header-level Link (the same shape Purchase
    #     Order/Purchase Receipt/Supplier Quotation already established), decided on the field's
    #     own reality, never on the dossier's "GL party" framing (this doctype posts no GL at
    #     all — see the ledger-preview finding below).
    #   * status and grand_total both CONFIRMED ABSENT — total_score (Percent, read_only,
    #     computed) is a SCORE, never a monetary total; no substitute Currency/Float field of any
    #     kind exists.
    #   * company CONFIRMED ABSENT TOO — a dossier omission (its own summary table never checks
    #     for it), THE SECOND companyless doctype after Packing Slip. The standing "wrong books"
    #     belt (unchanged, nine call sites) refuses under a company-PINNED target and governs
    #     cleanly only under the documented UNPINNED posture — no new machinery. Genuinely new:
    #     this doctype is ALSO dated (unlike Packing Slip) — CORRECTION (2026-07-21 live-prove
    #     batch): the closed-books disclosure did NOT "still read a real (companyless) period-lock
    #     call" as originally claimed here; it crashed (get_period_locks(None, ...) raises inside
    #     get_period_locks's own _doc_path("Company", None) call). Fixed by a shape-driven guard
    #     in tools.py's _plan_closed_books_risk/_locks_for — see their docstrings.
    #   * date_field="start_date" — a real, required Date field (no default), chosen over the
    #     doctype's other real Date field (end_date) as the period's own anchor — the same
    #     "window start, not its close" convention Blanket Order's from_date established. Rides
    #     the existing generic date machinery unchanged — the SEVENTH distinct date-fieldname
    #     pattern this campaign has found.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — SupplierScorecardPeriod(Document) overrides neither
    #     submit() nor cancel(), and defines NO on_submit/on_cancel hook of any kind (only
    #     validate + its own six helpers exist) — the same simplest submit/cancel lifecycle
    #     Cost Center Allocation's own landing established; a second doctype sharing it.
    #   * Ledger preview: UNCALLABLE category — a direct Document subclass, no make_gl_entries
    #     anywhere; joins the skip tuple in tools.py, now (DUNNING, LANDED_COST_VOUCHER,
    #     BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION,
    #     SUPPLIER_SCORECARD_PERIOD), with
    #     _supplier_scorecard_period_ledger_preview_unavailable_flag naming it honestly.
    #   * Cascade: a CASCADE LEAF — the ONLY Link -> Supplier Scorecard Period field anywhere in
    #     the v16 tree is its own self-referencing amended_from (full-tree scan). The relationship
    #     with its own parent Supplier Scorecard runs the OTHER way (this doctype's own
    #     `scorecard` Link points AT Supplier Scorecard, never the reverse).
    #   * Side surface: ZERO @frappe.whitelist() callables — nothing to withhold because nothing
    #     exists; the one module-level mapper helper (make_supplier_scorecard) is undecorated.
    #   * A caveat load-bearing to the whole doctype, not this broker's own scope: Supplier
    #     Scorecard Period is ordinarily MACHINE-GENERATED AND MACHINE-SUBMITTED by its own parent
    #     Supplier Scorecard doctype (on every one of the parent's own saves, and once daily via
    #     the `refresh_scorecards` scheduled job, erpnext/hooks.py:469) — never by this broker.
    #     See the module docstring's own paragraph for the full argument (a dossier §11
    #     correction: its RED FLAGS scan was correctly scoped to this file alone, but the parent
    #     doctype's own scheduler-driven behavior is real operational context worth disclosing).
    SUPPLIER_SCORECARD_PERIOD: {"party_field": "supplier", "submit_via": SUBMIT_VIA_RUN_METHOD,
                                "date_field": "start_date"},
    # Breadth (Quality Inspection) — Wave 4's fourth row, the twenty-sixth supported doctype, and
    # the FIRST DOCTYPE ON A DYNAMIC LINK PAIR SINCE QUOTATION. Full source-cited finding in the
    # module docstring above (dossier at docs/plans/dossiers/quality_inspection.md — correct on
    # party/status/grand_total/date_field/submit_via/ledger-preview; two real corrections landed
    # above: a whitelist-count undercount [4 claimed, 5 real] and a cascade §7 framing gap that
    # undersells the Job Card branch specifically — read line by line, its own raw-SQL write hits
    # a submittable TOP-LEVEL document, never a child row, for that one reference_type). The
    # load-bearing pins:
    #   * party_field=None — a Dynamic Link pair (reference_type/reference_name), the SAME shape
    #     Quotation's own quotation_to/party_name pair already established. company IS present on
    #     this schema (unlike Packing Slip/Supplier Scorecard Period) but not reqd, no default —
    #     set programmatically from the referenced document (set_company()); the dossier's own "GL
    #     party fixture" framing is corrected (this doctype posts no GL at all — see below).
    #   * status CONFIRMED PRESENT (Accepted/Rejected/Cancelled, default "Accepted");
    #     grand_total CONFIRMED ABSENT — a pure control/quality record, never a transaction.
    #   * date_field="report_date" — a real, required Date field (default "Today") — the EIGHTH
    #     distinct date-fieldname pattern this campaign has found. NOT in period_closing_doctypes;
    #     metadata only (no GL posting logic reads it — this doctype posts no GL at all).
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — QualityInspection(Document) overrides neither
    #     submit() nor cancel(); only on_discard/on_update/on_submit/on_cancel/on_trash/
    #     before_submit hooks are defined. A GENUINE DOSSIER OMISSION: before_submit calls
    #     validate_readings_status_mandatory(), which THROWS on any readings row missing its own
    #     status value — a doomed-submit gate readable off the draft, never mentioned in the
    #     dossier's own §4/§6. tools.py carries a new data-driven submit flag naming this.
    #   * Ledger preview: UNCALLABLE category — a direct Document subclass, no make_gl_entries
    #     anywhere in either the erpnext-16 OR frappe-16 checkouts (the widest MRO sweep this
    #     campaign has run); joins the skip tuple in tools.py, now (DUNNING, LANDED_COST_VOUCHER,
    #     BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION,
    #     SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION), with
    #     _quality_inspection_ledger_preview_unavailable_flag naming it honestly.
    #   * on_submit is CONDITIONAL (Stock Settings.action_if_quality_inspection_is_not_submitted
    #     == "Stop"); on_cancel is UNCONDITIONAL. Both call update_qc_reference(), which writes
    #     THIS document's own name into the reference document via raw SQL/query-builder,
    #     bypassing the reference document's own validate/on_update/version history — for
    #     reference_type == "Job Card" specifically, directly against Job Card's own TOP-LEVEL
    #     row (no child table exists for that case), never merely a child row as the dossier's
    #     own §7 framing implies for every reference_type. Neither on_submit/on_cancel/
    #     update_qc_reference carries a frappe.throw of its own. tools.py carries a new
    #     data-driven cancel flag naming this precisely per branch
    #     (_quality_inspection_cancel_risk_flags, gated on reference_type/reference_name).
    #   * Cascade: NINE Link->Quality Inspection hits (full-tree scan) — seven non-submittable
    #     child-table rows + the self-referencing amended_from + ONE genuine external submittable
    #     edge, Job Card (is_submittable: 1) — the first real blast-radius partner this doctype
    #     carries, unlike Packing Slip/Cost Center Allocation/Supplier Scorecard Period's own
    #     cascade-leaf shape. cascade.py needed no changes (doctype-blind, as always).
    #   * Side surface: FIVE @frappe.whitelist() callables (2 instance + 3 module-level,
    #     including make_quality_inspection — a SECOND dossier correction: the dossier claimed
    #     this one was undecorated; verified @frappe.whitelist() at line 487). This entry grants
    #     NOTHING toward any of the five.
    QUALITY_INSPECTION: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                         "date_field": "report_date"},
    # Breadth (Installation Note) — Wave 4's fifth row, the twenty-seventh supported doctype, and
    # Wave 4's SECOND ROW WITH A REAL PARTY FIELD (after Supplier Scorecard Period). Full
    # source-cited finding in the module docstring above (dossier at
    # docs/plans/dossiers/installation_note.md — correct on every axis it checked; the one
    # imprecision, corrected there, is an incomplete §7 that omits the shared validate_qty()
    # over-allowance guard every StatusUpdater-descended doctype's own update_prevdoc_status()
    # carries). The load-bearing pins:
    #   * party_field="customer" — a real, required, header-level Link (installation_note.json
    #     lines 57-69), the same static-party shape Purchase Order/Supplier Scorecard Period
    #     already established. Three more Customer-adjacent fields (customer_address/
    #     contact_person/customer_name/customer_group) ride as metadata only, never spliced.
    #   * status CONFIRMED PRESENT (Draft/Submitted/Cancelled, default "Draft", read_only: 1 —
    #     stamped by the lifecycle itself, never user-writable); grand_total CONFIRMED ABSENT —
    #     no aggregate/type-fork substitute of any kind exists (the "no natural analog" shape
    #     RFQ/Cost Center Allocation established), the NINETEENTH _list_fields branch.
    #   * date_field="inst_date" — a real, required Date field (reqd, no default) — the NINTH
    #     distinct date-fieldname pattern. The schema's only OTHER Date/Datetime-shaped field,
    #     inst_time (Time, no reqd/default), is confirmed read nowhere for any period-lock
    #     purpose — named explicitly so no future landing mistakes it for a second date_field
    #     candidate. Confirmed absent from period_closing_doctypes; posts no GL of any kind.
    #   * company CONFIRMED PRESENT AND REQUIRED — unlike Packing Slip/Supplier Scorecard Period,
    #     the standing "wrong books" belt applies in its ordinary form, no new machinery.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — InstallationNote(TransactionBase) overrides neither
    #     submit() nor cancel() anywhere (installation_note.py:13; only validate/on_update/
    #     on_submit/on_cancel hooks + five private helpers are defined).
    #   * Ledger preview: UNCALLABLE category, reached through a DEEPER MRO than any prior member —
    #     InstallationNote -> TransactionBase -> StatusUpdater -> Document (TWO levels above
    #     Document, vs. Packing Slip's ONE) — no make_gl_entries anywhere in transaction_base.py or
    #     status_updater.py either. Joins the skip tuple in tools.py, now (DUNNING,
    #     LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    #     COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    #     INSTALLATION_NOTE), with _installation_note_ledger_preview_unavailable_flag naming it
    #     honestly.
    #   * on_submit calls validate_serial_no() (every throw condition requires reading a DIFFERENT
    #     doctype — Item.has_serial_no, Serial No existence, the linked Delivery Note Item's own
    #     serial_no — none derivable from this draft's own fields alone, so per the standing
    #     doc-only risk-flag discipline, no new flag function is built; disclosed in the module
    #     docstring as an ERPNext-native doomed-submit path this broker's own plan_submit
    #     structurally cannot preview, the same class of gap Sales Order's/Purchase Order's own
    #     unmodeled native refusals already carry) THEN update_prevdoc_status(); on_cancel calls
    #     ONLY update_prevdoc_status(). Both share update_qty() (a raw SQL installed_qty/
    #     per_installed counter rewrite on the linked Delivery Note, never a GL row) +
    #     validate_qty() (ERPNext's own over-allowance guard, the SAME shared StatusUpdater
    #     mechanism Packing Slip's own landing already named and left unmodeled — runs on cancel
    #     too here, though cancelling only ever EXCLUDES this document's own qty from the target
    #     sum, so a throw on that path specifically would require a pre-existing over-allowance).
    #     No ignore_linked_doctypes, no auto-cancel of any sibling document.
    #   * Cascade: a CASCADE LEAF — the ONLY Link -> Installation Note field anywhere in the v16
    #     tree is its own self-referencing amended_from (full-tree scan). The child table's own
    #     prevdoc_detail_docname/prevdoc_docname/prevdoc_doctype fields are plain Data (never
    #     Link), the same shape Packing Slip's own dn_detail/pi_detail fields established.
    #   * Side surface: ZERO @frappe.whitelist() callables — nothing to withhold, the same smallest
    #     side-surface shape Cost Center Allocation's own landing established.
    INSTALLATION_NOTE: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD,
                       "date_field": "inst_date"},
    # Breadth (Shipment) — Wave 4's sixth row, the twenty-eighth supported doctype, and the FIRST
    # doctype with TWO independent dynamic-selector pairs. Full source-cited finding in the module
    # docstring above (dossier at docs/plans/dossiers/shipment.md — correct on every axis it
    # checked; one genuine omission surfaced, named below). The load-bearing pins:
    #   * party_field=None — a THIRD distinct reason for the value: TWO separate Select-driven
    #     trios of mutually exclusive Links (pickup_from_type gates pickup_company/_customer/
    #     _supplier; delivery_to_type gates delivery_company/_customer/_supplier), never
    #     server-enforced (confirmed by reading all 148 lines of shipment.py). Each side also
    #     carries a pre-built resolved-value mirror (pickup/delivery_to, both in_list_view: 1) —
    #     populated ONLY by shipment.js client-side events, NEVER by shipment.py itself (confirmed
    #     by grep) — so an API-created Shipment shows these two columns blank regardless of the
    #     gated Link fields' real values. The TWENTIETH _list_fields branch splices BOTH type+
    #     resolved-value pairs (the Quotation/Quality Inspection precedent, doubled).
    #   * status CONFIRMED PRESENT (Draft/Submitted/Booked/Cancelled/Completed, read_only: 1,
    #     stamped only via db_set); Booked/Completed are declared but NEVER set anywhere in this
    #     v16 OSS tree (a verified finding, not a dossier error — reachable only via paid
    #     carrier-integration apps outside this checkout). grand_total CONFIRMED ABSENT —
    #     value_of_goods (Currency, reqd) is the summary column; total_weight is a real field but
    #     not in_list_view-flagged and is deliberately excluded; shipment_amount is confirmed
    #     present but genuinely unused anywhere in the checkout's own code.
    #   * company CONFIRMED ABSENT — the THIRD companyless doctype after Packing Slip/Supplier
    #     Scorecard Period; REG_UNPINNED is the only registry shape this doctype can ever govern
    #     through.
    #   * date_field="pickup_date" — a real, required Date field, allow_on_submit: 1 — the TENTH
    #     date-fieldname pattern. pickup_from/pickup_to are Time fields (clock-time-only), named
    #     and excluded. The Work Order allow_on_submit note applies: check_fresh (plan.py) covers
    #     any post-submit drift via the modified bump, nothing new needed. Confirmed absent from
    #     period_closing_doctypes; posts no GL of any kind.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — Shipment(Document) overrides neither submit() nor
    #     cancel() anywhere (shipment.py:14; only on_discard/validate/on_submit/on_cancel hooks +
    #     five private helpers are defined).
    #   * Ledger preview: UNCALLABLE category, the SIMPLEST MRO in the category — Shipment(Document)
    #     directly, no make_gl_entries anywhere in shipment.py or Document itself. Joins the skip
    #     tuple in tools.py, now (DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM,
    #     WORK_ORDER, PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD,
    #     QUALITY_INSPECTION, INSTALLATION_NOTE, SHIPMENT), with
    #     _shipment_ledger_preview_unavailable_flag naming it honestly.
    #   * on_submit's two throws (empty shipment_parcel; value_of_goods == 0) are both doc-readable
    #     but ordinary reqd/business-rule guards, not a hidden or async mechanism — no new
    #     risk-flag function, per the same discipline every other "simple row" landing follows.
    #     on_cancel carries no throw of its own at all (just a db_set). No ignore_linked_doctypes,
    #     no auto-cancel of any sibling document.
    #   * Cascade: a CASCADE LEAF — the ONLY Link -> Shipment field anywhere in the v16 tree is its
    #     own self-referencing amended_from (full-tree scan).
    #   * Side surface: THREE read-only @frappe.whitelist() callables (get_address_name/
    #     get_contact_name/get_company_contact) — the WIDEST all-read-only surface this campaign
    #     has found (Packing Slip's own precedent was ONE), still zero mutation to caveat; nothing
    #     granted toward any of them.
    SHIPMENT: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
              "date_field": "pickup_date"},
    # Breadth (Sales Forecast) — the twenty-ninth supported doctype, Wave 4's seventh row. Full
    # source-cited finding in the module docstring's own "Breadth (Sales Forecast)" section above
    # (dossier at docs/plans/dossiers/sales_forecast.md — correct on every axis, one claim
    # narrowed: "status defaults to Planned" has no schema-level or code-level backing). Summary:
    #   * party_field=None — zero customer/supplier/party fields across all 20 enumerated fields;
    #     company is metadata (which books this demand plan belongs to), never a GL party.
    #   * status confirmed PRESENT (Planned/MPS Generated/Cancelled, read_only, in_list_view) but
    #     carries NO schema default and NO code path ever writes "Planned" (only on_discard writes
    #     "Cancelled") — an API-created Sales Forecast may show a genuinely blank status.
    #     grand_total confirmed ABSENT with no substitute of any kind (frequency/demand_number/
    #     parent_warehouse/from_date are real but none is in_list_view). The Material
    #     Request/RFQ/Job Card absence shape, but not a reuse — forces its own TWENTY-FIRST
    #     _list_fields branch, which happens to converge byte-for-byte with RFQ's own bare EIGHTH
    #     branch output (a genuine convergence, never a copy).
    #   * date_field="posting_date" — chosen over the ALSO-real from_date (reqd but not
    #     in_list_view; a workflow-window input, not this doc's own transactional column). Zero
    #     new date-fieldname plumbing.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading all 92 lines of
    #     sales_forecast.py: no def submit/def cancel override, only on_discard (line 35) plus two
    #     whitelisted callables that are NOT submit/cancel overrides.
    #   * Ledger preview: UNCALLABLE, the CLEANEST case yet — SalesForecast(Document) imports
    #     ZERO accounting/stock-controller-related names at all (not even an exception class, unlike
    #     Job Card's/Shipment's own bare-Document shape). Joins the skip tuple in tools.py, now
    #     (DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    #     COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    #     INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST).
    #   * on_submit AND on_cancel are BOTH confirmed absent entirely (the THIRD doctype after Cost
    #     Center Allocation/Supplier Scorecard Period). Cancelling a submitted Sales Forecast via
    #     this broker flips docstatus 1->2 and touches nothing else — on_discard (draft-only,
    #     refused by frappe's own Document.discard() unless docstatus is a draft) is never reached
    #     by this broker's own cancel path.
    #   * Cascade: full-tree grep finds exactly 2 hits — Sales Forecast's own amended_from
    #     self-link, and Master Production Schedule.sales_forecast (a real external Link, but MPS's
    #     own is_submittable is None/falsy — it can never itself reach docstatus=1, so this edge
    #     can never actually block a cancel on a real bench). cascade.py needed no changes.
    #   * Side surface: 2 whitelisted callables (generate_demand — in-memory items-table rewrite
    #     only; create_mps — returns an UNSAVED Master Production Schedule draft, caller must
    #     save/submit separately). This entry grants NOTHING toward either.
    SALES_FORECAST: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                     "date_field": "posting_date"},
    # Breadth (Project Update) — the thirtieth supported doctype, Wave 4's eighth row. Full
    # source-cited finding in the module docstring's own "Breadth (Project Update)" section above
    # (dossier at docs/plans/dossiers/project_update.md — correct on party/status/grand_total/
    # submit_via/ledger_preview-category/cascade-count; two things its own file-scoped read never
    # surfaced, named below). Summary:
    #   * party_field=None — only two Link fields exist (project, amended_from), neither a party.
    #   * status confirmed ABSENT, grand_total confirmed ABSENT — the ONLY in_list_view field is
    #     "project" itself, the narrowest set this campaign has found. A real "sent" Check field
    #     exists but is NOT in_list_view and tracks an unrelated reminder-email side channel, not
    #     governance-relevant state — deliberately not spliced in as a manufactured substitute.
    #   * company confirmed ABSENT — the FOURTH companyless doctype after Packing Slip/Supplier
    #     Scorecard Period/Shipment. REG_UNPINNED is the only registry shape this doctype can ever
    #     govern through; the existing nine "wrong books" call sites need no change.
    #   * date_field="date" — a GENUINELY NEW combination: a REAL declared date field (never the
    #     date_field=None dateless pin) that carries reqd=0 AND no schema default — the first
    #     dated doctype in this campaign where BOTH are absent. Nothing in project_update.py ever
    #     sets it server-side (only the client-side JS validate() event and a DIFFERENT doctype's
    #     scheduler, see below); an API-authored draft can carry a genuinely blank date. The
    #     EXISTING _posting_date_of/check_red_line machinery already governs this correctly with
    #     zero new code: a blank date reads as "", which denies at both the plan-time disclosure
    #     (Envelope E6) and the real execute-time gate (governed_submit's own check_red_line
    #     against the plan's own stored posting_date) — deny-biased, never a crash, never a silent
    #     bypass.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading all 29 lines of
    #     project_update.py: class ProjectUpdate(Document) carries a BARE "pass" body — no
    #     submit/cancel override, no validate(), no hook of any kind. The simplest class body this
    #     campaign has found (simpler even than the prior hookless trio, each of which still had
    #     validate() or on_discard).
    #   * Ledger preview: UNCALLABLE — ProjectUpdate(Document) directly, import block pulls in
    #     ONLY frappe + Document, zero accounting/stock-controller names. Joins the skip tuple in
    #     tools.py, now (DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER,
    #     PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    #     INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE).
    #   * on_submit AND on_cancel are BOTH confirmed absent entirely — the FOURTH doctype after
    #     Cost Center Allocation/Supplier Scorecard Period/Sales Forecast. A governed write through
    #     this broker changes docstatus alone.
    #   * Cascade: a CASCADE LEAF — full-tree grep finds exactly ONE hit, Project Update's own
    #     amended_from self-link. cascade.py needed no changes.
    #   * Side surface, two layers: (1) project_update.py's own daily_reminder() whitelist ->
    #     un-whitelisted email_sending() -> synchronous frappe.sendmail(), confirmed absent from
    #     hooks.py's scheduler_events (manually triggered only, matching the dossier). (2) A
    #     GENUINELY NEW finding: Project's OWN module (project.py, a different doctype) both
    #     auto-creates Project Update drafts (send_project_update_email_to_users, called via the
    #     hourly/hourly_maintenance scheduler events) and later mutates them from outside
    #     (collect_project_status appends users rows via a plain save, draft-only in practice;
    #     send_project_status_email_to_users flips "sent" via a raw db_set that bypasses docstatus
    #     entirely) — load-bearing context, not a gap this entry grants or withholds.
    PROJECT_UPDATE: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                     "date_field": "date"},
    # Breadth (Maintenance Visit) — the thirty-first supported doctype, Wave 4's ninth row. Full
    # source-cited finding in the module docstring's own "Breadth (Maintenance Visit)" section
    # above (dossier at docs/plans/dossiers/maintenance_visit.md — correct on party/status/
    # grand_total/date_field/submit_via/ledger_preview-category/cascade-count/whitelist-count; one
    # real §7 imprecision corrected, plus a docstatus-guard question source settles outright).
    # Summary:
    #   * party_field="customer" — a real, required, header-level Link (maintenance_visit.json
    #     lines 65-74), the same static-party shape Installation Note/Supplier Scorecard Period
    #     already established. Four more Customer-adjacent fields (customer_address/
    #     contact_person/customer_name/customer_group/territory) ride as metadata only, never
    #     spliced.
    #   * status CONFIRMED PRESENT (Draft/Cancelled/Submitted, default "Draft", read_only: 1,
    #     NOT in_list_view — stamped by the lifecycle itself); grand_total CONFIRMED ABSENT (no
    #     Currency/Float/Percent field anywhere in 32 enumerated fields). TWO separate
    #     in_list_view fields — completion_status and maintenance_type, both orthogonal to the
    #     submit-lifecycle status column — force the TWENTY-THIRD _list_fields branch: the SAME
    #     categorical shape Installation Note established (real party + status + company,
    #     grand_total absent), but NOT a reuse — two named substitutes here, not Installation
    #     Note's one (remarks).
    #   * date_field="mntc_date" — Date, reqd=1, default="Today" — the TWELFTH distinct
    #     date-fieldname pattern (posting_date; transaction_date; from_date; planned_start_date;
    #     available_for_use_date; valid_from; start_date; report_date; inst_date; pickup_date;
    #     date; and now mntc_date), and the first to carry BOTH reqd AND a schema default
    #     together (Installation Note's inst_date is reqd with no default; Project Update's date
    #     is neither). The schema's only other Date/Datetime-shaped field, mntc_time (Time, no
    #     reqd/default), is read in exactly one place (check_if_last_visit's own same-day
    #     tie-break) — never a period-lock date, never the chosen date_field.
    #   * company CONFIRMED PRESENT AND REQUIRED — the standing "wrong books" belt applies in its
    #     ordinary form, no new machinery.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — MaintenanceVisit(TransactionBase) overrides neither
    #     submit() nor cancel() anywhere (maintenance_visit.py:12; only validate/on_submit/
    #     on_cancel/on_update hooks + private helpers are defined).
    #   * Ledger preview: UNCALLABLE — the SAME MRO depth Installation Note established
    #     (MaintenanceVisit -> TransactionBase -> StatusUpdater -> Document), no make_gl_entries
    #     anywhere in any of the three files. Joins the skip tuple in tools.py, now (DUNNING,
    #     LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    #     COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    #     INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT).
    #   * on_submit calls update_customer_issue(1) (gated on "not maintenance_schedule"; for every
    #     purposes row naming a Warranty Claim, directly rewrites that Warranty Claim's
    #     resolution_date/resolved_by/resolution_details/status via db_update() — ONE independent
    #     write per matching row, so a single submit can touch more than one Warranty Claim).
    #     Warranty Claim carries no is_submittable key at all (confirmed absent, not 0) — NOT a
    #     submittable doctype, so there is no docstatus lifecycle to bypass; db_update() still
    #     skips validate()/on_update hooks, permissions, and version history — the same
    #     bypass-the-normal-save-path shape Quality Inspection's own update_qc_reference() carries.
    #     Fully derivable from the draft's own fields — a genuine data-driven flag
    #     (_maintenance_visit_submit_risk_flags), not blanket prose.
    #   * on_cancel calls check_if_last_visit() first: a raw SQL peer query for OTHER SUBMITTED
    #     Maintenance Visit rows sharing the SAME prevdoc_docname with a LATER mntc_date (or same
    #     date, later mntc_time) — throws "Cancel Material Visits {0} before cancelling this
    #     Maintenance Visit" if any exist, refusing the cancel outright. A DOSSIER CORRECTION: its
    #     own §7 calls this "the SAME Warranty Claim", but the SQL match is on prevdoc_docname
    #     STRING equality alone with NO prevdoc_doctype filter (source even carries a
    #     commented-out "# check_for_doctype = d.prevdoc_doctype") — the gate is not restricted to
    #     Warranty Claim by schema. A SAME-DOCTYPE PEER constraint, invisible to this broker's own
    #     blast-radius/cascade machinery (prevdoc_docname is a Dynamic Link FROM this child table
    #     pointing OUT, never a Link TO Maintenance Visit) and requiring a sibling read plan_cancel
    #     does not perform — disclosed in prose only (module docstring + _maintenance_visit_
    #     cancel_risk_flags), the same undisclosable-without-a-new-read shape Installation Note's
    #     own validate_serial_no already established; no new machinery invented. If the gate
    #     passes, update_customer_issue(0) runs the SAME Warranty Claim write(s) with reset values
    #     from a second sibling query — the fact of the touch is disclosed, the exact values are
    #     not (same reason).
    #   * Cascade: a CASCADE LEAF — full-tree grep finds exactly ONE hit, Maintenance Visit's own
    #     amended_from self-link (matching the dossier's own §8 count). cascade.py needed no
    #     changes. The temporal-ordering peer constraint above is a separate mechanism, not a
    #     cascade edge.
    #   * Side surface: ZERO @frappe.whitelist() callables (confirmed by full grep, matching the
    #     dossier's own §9 count) — nothing to withhold.
    MAINTENANCE_VISIT: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD,
                       "date_field": "mntc_date"},
    # Breadth (Maintenance Schedule) — the thirty-second supported doctype, Wave 4's tenth and
    # last row. Full source-cited finding in the module docstring's own "Breadth (Maintenance
    # Schedule)" section above (dossier at docs/plans/dossiers/maintenance_schedule.md — correct
    # on every axis it checks; two precision gaps closed, neither a factual error). Summary:
    #   * party_field="customer" — a real header-level Link (maintenance_schedule.json lines
    #     51-61), but the FIRST party_field row this campaign has found where the field itself
    #     carries no "reqd" key at all (confirmed absent, not 0) — unlike Installation Note's/
    #     Maintenance Visit's own required customer. Still spliced by name: the test has always
    #     been "real, static, singular header Link", never reqd-ness.
    #   * status CONFIRMED PRESENT (Draft/Submitted/Cancelled, default "Draft", read_only,
    #     NOT in_list_view); grand_total CONFIRMED ABSENT (24 fields enumerated, no Currency/
    #     Float/Percent anywhere). Exactly ONE in_list_view field — customer_name (Data, bold,
    #     denormalized) — forces the TWENTY-FOURTH _list_fields branch: the SAME categorical
    #     shape Installation Note established, but NOT a reuse (its own substitute, remarks,
    #     doesn't exist here).
    #   * date_field="transaction_date" (reqd, no default) — REJOINS the standing SO/PO/MR/SQ/Q/
    #     RFQ set as its SEVENTH member, zero new date-fieldname plumbing needed.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — MaintenanceSchedule(TransactionBase) overrides
    #     neither submit() nor cancel() anywhere (maintenance_schedule.py:13).
    #   * Ledger preview: UNCALLABLE — the SAME MRO depth Installation Note/Maintenance Visit
    #     established (MaintenanceSchedule -> TransactionBase -> StatusUpdater -> Document), no
    #     make_gl_entries anywhere in any of the three files. Joins the skip tuple in tools.py,
    #     now (DUNNING, LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER,
    #     PACKING_SLIP, COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    #     INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT,
    #     MAINTENANCE_SCHEDULE).
    #   * on_submit auto-creates one Event per schedules row (frappe.get_doc({"doctype": "Event",
    #     ...}).insert(ignore_permissions=1); Event carries no is_submittable key at all — never
    #     itself submitted) AND, per items row carrying a serial_and_batch_bundle, calls
    #     update_amc_date() which runs serial_no_doc.save() — a FULL document save (validate/
    #     hooks/versioning all run), the FIRST cross-document mutation this campaign has found
    #     that is NOT a bypass (contrast Maintenance Visit's own db_update/raw-SQL precedents).
    #     Both fully derivable from the draft's own items/schedules rows — genuine data-driven
    #     flags (_maintenance_schedule_submit_risk_flags), not blanket prose.
    #   * on_cancel carries ZERO reference to Maintenance Visit anywhere (verified plainly, the
    #     Work Order §9 lesson) — no ignore_linked_doctypes, no throw of any kind. It reverses the
    #     same Serial No field via the same full .save() mechanism, then permanently DELETES
    #     (frappe.delete_doc, not orphaned) every Event this document's own submit created
    #     (delete_events, also called a THIRD time from on_trash — a finding beyond the dossier's
    #     own §7 scope).
    #   * Cascade: GENUINELY NOT A LEAF — a full-tree grep for '"options": "Maintenance
    #     Schedule"' finds TWO hits: its own amended_from, and Maintenance Visit's own real,
    #     static maintenance_schedule Link (is_submittable=1) — fully visible to
    #     get_submitted_linked_docs. A submitted Maintenance Visit therefore refuses this
    #     broker's own leaf plan_cancel even though ERPNext's native on_cancel enforces nothing
    #     of the kind — this broker is STRICTER than ERPNext here, pinned by a real blast-radius
    #     test (the Job Card/Quality Inspection precedent).
    #   * Side surface: FIVE @frappe.whitelist() callables (matching the dossier's own §9 count) —
    #     draft-only or read-only throughout; nothing to withhold.
    MAINTENANCE_SCHEDULE: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD,
                          "date_field": "transaction_date"},
    # Breadth (Asset Maintenance Log) — Wave 5's first row, the thirty-third supported doctype.
    # Full source-cited finding in the module docstring's own "Breadth (Asset Maintenance Log)"
    # section above (dossier at docs/plans/dossiers/asset_maintenance_log.md — correct on party/
    # grand_total/submit_via/ledger_preview-category/cascade-count/whitelist-count; the dossier's
    # own summary called it "DATELESS" while its own §3 lists two real Date fields, and its own
    # hooks.py line numbers were off by ~1400 — both settled from source, neither a factual miss
    # on the fields/hooks themselves). Summary:
    #   * party_field=None — the standing "no party concept at all" shape (asset_maintenance/task
    #     are operational routing Links, never a GL party; confirmed absent across all 23 fields).
    #   * maintenance_status CONFIRMED PRESENT (Planned/Completed/Cancelled/Overdue, reqd, NO
    #     read_only key and NO default at all — the FIRST campaign doctype whose lifecycle Select
    #     is writable rather than hook-stamped) — but this is the FIRST doctype whose status-shaped
    #     field is NOT literally named "status", so _list_fields cannot splice the literal string
    #     "status" here (see below). grand_total CONFIRMED ABSENT (23 fields enumerated, no
    #     Currency/Float/Percent anywhere).
    #   * date_field="completion_date" — TWO real Date fields exist (due_date: read-only,
    #     fetch_from task.next_due_date, a scheduling REFERENCE; completion_date: writable, the
    #     operational date) — a doctype with even one real Date field is dated, never the BOM
    #     dateless pin, settling the dossier's own self-contradiction. completion_date is REQUIRED
    #     to be blank (validate() throws otherwise) for every submitted "Cancelled" log — the
    #     Project Update blank-date precedent, but by construction rather than by accident; the
    #     existing _posting_date_of/check_red_line machinery already governs this correctly, zero
    #     new code. The THIRTEENTH distinct date-fieldname pattern this campaign has found.
    #   * company CONFIRMED ABSENT — the FIFTH companyless doctype after Packing Slip/Supplier
    #     Scorecard Period/Shipment/Project Update; REG_UNPINNED is the only registry shape this
    #     doctype can ever govern through.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — AssetMaintenanceLog(Document) overrides neither
    #     submit() nor cancel() anywhere (asset_maintenance_log.py:14).
    #   * Ledger preview: UNCALLABLE category, the SIMPLEST MRO (bare Document) — no
    #     make_gl_entries anywhere. Joins the skip tuple in tools.py, now (DUNNING,
    #     LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    #     COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    #     INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT,
    #     MAINTENANCE_SCHEDULE, ASSET_MAINTENANCE_LOG).
    #   * submit is doomed for every maintenance_status/completion_date combination EXCEPT
    #     (Completed + a set completion_date) or (Cancelled + a blank completion_date) — fully
    #     deterministic from the draft's own fields (validate()'s two throws + on_submit's own
    #     gate). When submit succeeds, on_submit's update_maintenance_task() saves the linked
    #     Asset Maintenance Task in full AND unconditionally saves the PARENT Asset Maintenance
    #     record too — re-triggering that parent's own on_update (task reassignment + a re-sync
    #     capable of creating/updating OTHER sibling Asset Maintenance Log documents). on_cancel is
    #     CONFIRMED ABSENT ENTIRELY — cancel never reverses either mutation.
    #   * RED FLAG beyond the dossier's own §11 scope: TWO status-rewrite mechanisms outside
    #     validate/on_submit. The scheduler (update_asset_maintenance_log_status, raw frappe.qb
    #     UPDATE, erpnext/hooks.py:485) only ever reaches "Planned" rows — draft-only in practice,
    #     since on_submit's own gate means a submitted doc can never read back "Planned". The
    #     sharper, dossier-missed one: AssetMaintenance.sync_maintenance_tasks() (the PARENT's own
    #     on_update) calls maintenance_log.db_set("maintenance_status", "Cancelled") with NO
    #     status/docstatus filter at all — db_set() skips validate/on_update/versioning entirely,
    #     so a SUBMITTED, Completed log CAN be silently flipped to Cancelled this way.
    #   * Cascade: a CASCADE LEAF — the ONLY Link -> Asset Maintenance Log field anywhere in the
    #     v16 tree is its own self-referencing amended_from (full-tree scan).
    #   * Side surface: ONE @frappe.whitelist() callable (get_maintenance_tasks, read-only search
    #     helper, matching the dossier's own §9 count) — nothing to withhold.
    ASSET_MAINTENANCE_LOG: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                           "date_field": "completion_date"},
    # Breadth (Bank Guarantee) — Wave 5's second row, the thirty-fourth supported doctype. Full
    # source-cited finding in the module docstring's own "Breadth (Bank Guarantee)" section above
    # (dossier at docs/plans/dossiers/bank_guarantee.md — correct on party-shape/status/
    # grand_total/date_field-fieldname/cascade-count/whitelist-count; WRONG on submit_via: the
    # dossier calls it client_rpc citing "an on_submit override", but on_submit is a HOOK method,
    # never a def submit(self)/def cancel(self) override — confirmed absent by a full 74-line read
    # of bank_guarantee.py; SUBMIT_VIA_CLIENT_RPC stays pinned to exactly Journal Entry/Stock
    # Reconciliation, the only two doctypes whose class genuinely shadows Document.submit/cancel
    # itself). Summary:
    #   * party_field=None — a genuine DUAL CONDITIONAL pair, not a single static fieldname:
    #     customer (depends_on doc.reference_doctype=="Sales Order") and supplier (depends_on
    #     doc.reference_doctype=="Purchase Order") are both real header Link fields, populated
    #     per bg_type (Receiving -> Sales Order -> customer; Providing -> Purchase Order ->
    #     supplier, bank_guarantee.js:36-42, client-side only). validate() requires exactly one of
    #     the two (bank_guarantee.py:46-47) — the Blanket Order precedent (both fields splice on
    #     the list tier as context, never one alone).
    #   * status CONFIRMED ABSENT (docstatus only, no explicit Select) — grand_total also
    #     CONFIRMED ABSENT; amount (Currency, reqd, in_list_view=1) is the sole aggregate, standing
    #     in for the missing grand_total slot (the Stock Reconciliation difference_amount/LCV
    #     total_taxes_and_charges precedent).
    #   * date_field="start_date" (Date, reqd, no default) — REJOINS Supplier Scorecard Period's
    #     own SEVENTH date-fieldname pattern (both schemas carry a literal, real, required
    #     start_date), the SECOND doctype on it, not a new one. A genuine dossier gap: end_date
    #     (Date, read_only, NOT in_list_view) is described as "calculated from start_date +
    #     validity" but the calculation is CLIENT-SIDE ONLY (bank_guarantee.js:65-72) — confirmed
    #     absent from bank_guarantee.py entirely, so an API-created/edited document never has
    #     end_date recomputed server-side; display convenience only, never governed here.
    #   * company CONFIRMED ABSENT — the SIXTH companyless doctype after Packing Slip/Supplier
    #     Scorecard Period/Shipment/Project Update/Asset Maintenance Log; REG_UNPINNED is the only
    #     registry shape this doctype can ever govern through.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD (dossier correction, above) — BankGuarantee(Document)
    #     overrides neither submit() nor cancel() anywhere (bank_guarantee.py:10); only validate()
    #     and on_submit() (a hook, not an override) are defined.
    #   * Ledger preview: UNCALLABLE category, the SIMPLEST MRO (bare Document) — no
    #     make_gl_entries anywhere. Joins the skip tuple in tools.py, now (DUNNING,
    #     LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    #     COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    #     INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT,
    #     MAINTENANCE_SCHEDULE, ASSET_MAINTENANCE_LOG, BANK_GUARANTEE).
    #   * on_submit (lines 49-55) is a fully deterministic doomed-submit gate, readable off the
    #     draft: three throws in strict order (bank_guarantee_number, then name_of_beneficiary,
    #     then bank — none reqd at the schema level, so a draft can be saved/read with any blank),
    #     each stopping the check at the first failure. When all three are present, submit
    #     succeeds with NO further side effect of any kind — on_submit performs validation only
    #     (confirmed by a full 74-line read), the plainest submit story this campaign has found.
    #     validate()'s own separate customer-or-supplier gate (lines 46-47) runs on every save, so
    #     an EXISTING draft has already satisfied it by construction — never re-disclosed as a
    #     submit risk.
    #   * on_cancel CONFIRMED ABSENT ENTIRELY — cancelling is a pure docstatus flip (frappe's own
    #     generic check_no_back_links_exist() only); nothing to reverse either, since submit itself
    #     performed no mutation beyond its own three throws.
    #   * Cascade: a CASCADE LEAF — the ONLY Link -> Bank Guarantee field anywhere in the v16 tree
    #     (erpnext AND frappe checkouts both grepped) is its own self-referencing amended_from.
    #   * Side surface: ONE @frappe.whitelist() callable (get_voucher_details, a read-only
    #     frappe.db.get_value fetch, matching the dossier's own count) — nothing to withhold.
    BANK_GUARANTEE: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                     "date_field": "start_date"},
    # Breadth (Asset Movement) — Wave 5's third row, the thirty-fifth supported doctype, the
    # SECOND Datetime-dated doctype (after Work Order). Full source-cited finding in the module
    # docstring's own "Breadth (Asset Movement)" section above (dossier at
    # docs/plans/dossiers/asset_movement.md — ONE genuine dossier error, settled from source: its
    # own §7 claims a cancelled-to-empty movement clears BOTH location and custodian to empty
    # strings — true only for custodian; location carries an asymmetric truthy guard the dossier
    # never traced into update_asset_location_and_custodian's own body). Summary:
    #   * party_field=None — reference_doctype/reference_name (Dynamic Link pair, provenance only,
    #     pointing at the seeding Purchase Receipt/Purchase Invoice) and the child table's own
    #     from_employee/to_employee are never a GL party.
    #   * status CONFIRMED ABSENT (docstatus only) — grand_total also CONFIRMED ABSENT, with no
    #     aggregate substitute of any kind (unlike SR's difference_amount or BG's amount) — a pure
    #     state-capture trail, no value on the document at all. purpose (Select, reqd,
    #     in_list_view, 4 peer values Issue/Receipt/Transfer/Transfer and Issue) is the closest
    #     adjacent field, the Stock Entry precedent (a router, never a state transition).
    #   * date_field="transaction_date" (Datetime, reqd, default "Now", allow_on_submit CONFIRMED
    #     ABSENT) — REJOINS the transaction_date FIELDNAME (now eight doctypes) but is only the
    #     SECOND Datetime-typed date_field this campaign has found (after Work Order's
    #     planned_start_date); the fieldname collision with the seven Date-typed members is purely
    #     nominal — _posting_date_of's datetime->date projection (Work Order's own machinery,
    #     reused unchanged) keys on the VALUE's own shape, never on which fieldname produced it.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — AssetMovement(Document) overrides neither submit()
    #     nor cancel() anywhere (asset_movement.py:13; 13 methods total, only on_submit/on_cancel
    #     hooks at lines 116/119).
    #   * Ledger preview: UNCALLABLE category, bare Document MRO — no make_gl_entries anywhere.
    #     Joins the skip tuple in tools.py, now its NINETEENTH member: (DUNNING,
    #     LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    #     COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    #     INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT,
    #     MAINTENANCE_SCHEDULE, ASSET_MAINTENANCE_LOG, BANK_GUARANTEE, ASSET_MOVEMENT).
    #   * THE CENTRAL FINDING: on_submit and on_cancel call the EXACT SAME METHOD
    #     (set_latest_location_and_custodian_in_asset, lines 116-120) — a live SQL re-query of the
    #     freshest submitted movement trail per referenced asset, written onto the Asset via raw
    #     frappe.db.set_value (lines 156-162, "even more direct bypass than db_update(): no ORM
    #     triggers, no Document events" — the Maintenance Visit update_status_and_actual_date
    #     grade), even though a document IS loaded first (frappe.get_doc, line 157) purely to
    #     READ current values for comparison, never to perform the write. Disclosed unconditionally
    #     on both directions (_asset_movement_write_risk_flags, tools.py), naming every referenced
    #     Asset from the draft's own assets rows.
    #   * THE SHARPEST DISCLOSURE: custodian clears to "" whenever the resolved employee differs
    #     (even to empty); location is written ONLY when the resolved value is truthy (line 161:
    #     "if location and location != asset.location") — a cancel-to-empty rollback clears
    #     custodian but leaves location untouched, the dossier's own §7 miss.
    #   * Cascade: a LEAF for INCOMING links (only its own self-referencing amended_from, both
    #     erpnext and frappe checkouts) — but Asset Movement Item's own asset field is one of
    #     Asset's own 18 Link -> Asset edges, so a submitted Asset Movement DOES appear as a
    #     dependent node inside an Asset's own plan_cascade_cancel graph. Two directions of the
    #     same relationship, not a contradiction.
    #   * Side surface: ZERO @frappe.whitelist() callables (matching the dossier's own count) —
    #     nothing to withhold because nothing exists.
    ASSET_MOVEMENT: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                     "date_field": "transaction_date"},
    # Breadth (Delivery Trip) — the thirty-sixth supported doctype, and the THIRD Datetime-dated
    # doctype (after Work Order and Asset Movement) — the first on a genuinely NEW date-fieldname
    # pattern (departure_time, sole member, no collision). Full source-cited finding in the module
    # docstring's own "Breadth (Delivery Trip)" section above (dossier at
    # docs/plans/dossiers/delivery_trip.md — genuine gaps closed there: the "23 fields" framing
    # conflates the raw array length with the real 15-field count; §7 misses on_trash/on_discard
    # firing the identical mutation; the honesty-grade placement of that mutation and the
    # structural cascade-order collision it creates are pinned from frappe/model/document.py,
    # neither in the dossier at all). Summary:
    #   * party_field=None — company IS present (reqd) — NOT companyless. driver/employee are
    #     metadata only, never a GL party.
    #   * status CONFIRMED REAL (Select, read_only, in_standard_filter, 5 options) but maintained
    #     entirely by this doctype's OWN db_set (update_status) — a lighter, self-document cousin
    #     of the cross-document bypass grades, never disclosed as a runtime risk. grand_total
    #     CONFIRMED ABSENT with no substitute of any kind.
    #   * date_field="departure_time" (Datetime, reqd, NO default, allow_on_submit CONFIRMED
    #     ABSENT) — a genuinely NEW fourteenth date pattern (sole member, no fieldname collision,
    #     unlike Asset Movement's own nominal collision with transaction_date). The
    #     _posting_date_of projection applies unchanged (keys on the value's own shape, never the
    #     fieldname).
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — DeliveryTrip(Document) overrides neither submit() nor
    #     cancel() anywhere (delivery_trip.py:14; 16 methods total, only on_submit/on_cancel hooks
    #     at lines 71-72/77-79).
    #   * Ledger preview: UNCALLABLE category, bare Document MRO — no make_gl_entries anywhere.
    #     Joins the skip tuple in tools.py, now its TWENTIETH member: (DUNNING,
    #     LANDED_COST_VOUCHER, BLANKET_ORDER, JOB_CARD, BOM, WORK_ORDER, PACKING_SLIP,
    #     COST_CENTER_ALLOCATION, SUPPLIER_SCORECARD_PERIOD, QUALITY_INSPECTION,
    #     INSTALLATION_NOTE, SHIPMENT, SALES_FORECAST, PROJECT_UPDATE, MAINTENANCE_VISIT,
    #     MAINTENANCE_SCHEDULE, ASSET_MAINTENANCE_LOG, BANK_GUARANTEE, ASSET_MOVEMENT,
    #     DELIVERY_TRIP).
    #   * validate() (57-63): TWO throws gated on _action=="submit" — "driver must be set" is
    #     deterministic off the draft (doomed-submit flag); validate_delivery_note_not_draft()
    #     needs a live read of each linked Delivery Note's own docstatus (prose; names surfaced
    #     data-driven).
    #   * THE CENTRAL FINDING: on_cancel's update_delivery_notes(delete=True) force-clears five
    #     fields on every linked Delivery Note via note_doc.save() with
    #     ignore_validate_update_after_submit=True — read against frappe/model/document.py's own
    #     _save/check_docstatus_transition/validate_update_after_submit: this is the SANCTIONED
    #     update_after_submit save path (permission checks, freshness, link validation,
    #     before/on_update_after_submit hooks, and version history ALL still run; only the custom
    #     validate() hook and the one allow_on_submit field-lock check are skipped — the latter
    #     being the flag's entire, narrow purpose) — placed ABOVE every bypass grade this campaign
    #     has named (db_update/db_set/raw SQL), not a bypass at all.
    #   * THE SHARPEST FINDING, CORRECTED 2026-07-21 by supervisor verification: the original
    #     claim (this Delivery Trip's OWN cancel step raises after the cascade cancels the note
    #     first) is UNREACHABLE. on_cancel runs BEFORE check_no_back_links_exist
    #     (document.py:1450-1452), so a LEAF cancel of this Delivery Trip NATIVELY SUCCEEDS as a
    #     self-unlinking cancel — update_delivery_notes(delete=True) clears the linked notes'
    #     fields before any back-link check runs. The real collision lands on the DEPENDENT
    #     instead: cancelling a Delivery Note while its trip is still submitted hits frappe's own
    #     check_if_doc_is_linked(method="Cancel"), which walks child-table Link fields too —
    #     Delivery Stop.delivery_note (istable=1) still points the submitted trip at that note,
    #     and Delivery Note's own on_cancel exemption tuple (delivery_note.py:500-505) does not
    #     cover Delivery Trip — so this broker's own dependents-first cascade order makes the
    #     cascade raise frappe.LinkExistsError at the NOTE step, before this Delivery Trip's own
    #     cancel step is ever reached. Net (at landing time): this broker had NO WORKING CANCEL
    #     PATH for a submitted Delivery Trip with a submitted linked Delivery Note — the leaf
    #     plan_cancel was refused outright by the blast-radius check, and plan_cascade_cancel
    #     failed at execute at the note step above. Two structural fixes named, neither chosen —
    #     open design decision for John. The originally-claimed ValidationError("Cannot edit
    #     cancelled document") (document.py:1149-1150) survives as a weaker SECOND lock, reachable
    #     only if the note step were somehow already past.
    #   * RULED 2026-07-21 — John's ruling 1: the self-unlinking-doctype exception (option (a)),
    #     belted. DELIVERY_TRIP now lives in SELF_UNLINKING_DOCTYPES; the leaf plan_cancel gate
    #     treats its own submitted incoming links as non-blocking instead of refusing outright,
    #     and a post-execute readback (self_unlink_readback) attests the self-unlink actually
    #     happened, reporting any note still linked. plan_cascade_cancel is UNCHANGED — still
    #     structurally dead at the note step above; no cascade-order exception (option (b)) built.
    #   * Cascade: GENUINELY NOT A LEAF — a full-tree grep for '"options": "Delivery Trip"' over
    #     both checkouts finds TWO hits: its own amended_from, and Delivery Note's own real,
    #     static, submittable delivery_trip Link. ERPNext's own on_cancel enforces no gate at all
    #     — this broker is stricter than ERPNext in POSTURE here (native ERPNext would silently
    #     SUCCEED as a self-unlinking cancel, per the corrected finding above, never collide),
    #     pinned by the real Maintenance Schedule precedent, zero cascade.py changes needed.
    #   * Side surface: FIVE @frappe.whitelist() callables (matching the dossier's own count) —
    #     process_route and notify_customers both carry a genuine submitted-state mutation
    #     surface; nothing granted.
    DELIVERY_TRIP: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                    "date_field": "departure_time"},
    # Breadth (Asset Value Adjustment) — Wave 5's fifth row, the thirty-seventh supported doctype:
    # the campaign's FIRST sibling-document FACTORY row. Full source-cited finding in the module
    # docstring above (dossier at docs/plans/dossiers/asset_value_adjustment.md — corrections:
    # company is present but NOT reqd (the THIRD such row after Purchase Invoice/Quality
    # Inspection, never before this consequential); ignore_permissions is set on BOTH the sibling
    # JE's submit AND cancel, the dossier names only cancel; update_asset() arms a THIRD sibling-
    # document channel via reschedule_depreciation, entirely unnamed by the dossier). The
    # load-bearing pins:
    #   * party_field=None — company IS present (asset_value_adjustment.json line 28) but carries
    #     no reqd key — the third such row (after Purchase Invoice/Quality Inspection), but the
    #     FIRST where that carries a real consequence: a blank company is a doomed submit (the
    #     synchronously-built Journal Entry's own company field IS reqd, disclosed data-driven).
    #   * date_field="date" — a real, required Date field, REJOINS Project Update's own eleventh
    #     date pattern (second member, never a new one).
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — AssetValueAdjustment(Document) overrides neither
    #     submit() nor cancel() (asset_value_adjustment.py:21; hooks only, lines 66/76).
    #   * Ledger preview: UNCALLABLE, bare Document MRO — joins the skip tuple (21st member). The
    #     document's OWN preview is honestly empty even though submitting it DOES post real GL —
    #     through the synchronously-created sibling Journal Entry, never under this document's
    #     own name.
    #   * THE SIBLING JOURNAL ENTRY: SYNCHRONOUS (built + submitted inside this on_submit call,
    #     name written back via db_set before on_submit returns) — a materially different, WEAKER
    #     disclosure need than Asset's own SCHEDULED depreciation JEs (this broker sees the whole
    #     channel inside the call it already governs; nothing arrives later, outside any call).
    #   * THE PERMISSION-ONLY BYPASS on the sibling JE's cancel (also present, undisclosed by the
    #     dossier, on its submit): ignore_permissions=True short-circuits Document.has_permission
    #     for ANY permtype (document.py:407-408) — skips ONLY the ACL/authorization gate; every
    #     hook, validation, check_no_back_links_exist, and version-history write on the JE's own
    #     cancel runs exactly as a normal cancel would. Distinct in KIND (not degree) from every
    #     data-integrity bypass this campaign has named (Maintenance Visit's db_update, Asset
    #     Movement's/AML's db_set, QI's raw SQL) and from Delivery Trip's sanctioned
    #     update_after_submit save (which skips validate() + one field lock while running
    #     permission checks normally).
    #   * update_asset() — THREE distinct write mechanisms: (1) row.db_update()/asset.db_update()
    #     on the linked Asset's own finance_books/value_after_depreciation — the Maintenance Visit
    #     db_update bypass grade; (2) reschedule_depreciation — a THIRD sibling-document channel:
    #     may CANCEL an existing submitted Asset Depreciation Schedule (with
    #     should_not_cancel_depreciation_entries=True, explicitly preserving already-posted
    #     depreciation JEs) and CREATE+SUBMIT a new one, synchronously; needs a live read of the
    #     Asset's own calculate_depreciation/finance_books state, disclosed as prose; (3)
    #     asset.set_status() — a raw db_set, the Asset Movement/AML bypass grade.
    #   * THE CLOSED-BOOKS SCOPE GAP: this doctype can never appear as an Accounting Period's own
    #     closed_documents entry (get_doctypes_for_closing restricts the UI to
    #     period_closing_doctypes members only, and this doctype is absent from that list) — the
    #     doctype-specific branch of this broker's own belt is a structural no-op for it. The
    #     sibling Journal Entry IS a period_closing_doctypes member and is natively gated for
    #     real, on the identical date value — a genuine disclosed scope gap: this plan can read
    #     "ok" while the synchronously-created sibling JE is refused at execute by ERPNext's own
    #     native gate.
    #   * Cascade: CONFIRMED ZERO incoming edges (a leaf for its own target scans) — but its own
    #     "asset" Link field is one of Asset's own 18 Link -> Asset edges, so a submitted Asset
    #     Value Adjustment appears as a dependent NODE inside an Asset's own plan_cascade_cancel
    #     graph — the Asset Movement precedent, two directions of one relationship.
    #   * Side surface NOT granted: ONE whitelist callable (get_value_of_accounting_dimensions,
    #     read-only).
    ASSET_VALUE_ADJUSTMENT: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                             "date_field": "date"},
    # Breadth (Payment Order) — Wave 5's sixth row, the thirty-eighth supported doctype. Full
    # source-cited finding in the module docstring above (dossier at
    # docs/plans/dossiers/payment_order.md — corrections: party_field is None, not "party" — the
    # dossier's own §1 calls it a static header field, but party carries a real depends_on
    # (payment_order_type=='Payment Request') and is never read/written by payment_order.py's own
    # code at all, the same "no single static fieldname is ever unconditionally the party" rule
    # this table's Blanket Order/Bank Guarantee entries already state, here for the FIRST single
    # (not dual) conditional field; company is present+reqd but never mentioned by the dossier at
    # all; "Override present: YES" is the Bank Guarantee dossier-error class repeated — on_submit/
    # on_cancel are plain hooks, and this doctype carries no validate() at all, zero throws
    # anywhere; the period_closing_doctypes citation is stale (326-345, not 117-133) though the
    # absence conclusion holds). Summary:
    #   * party_field=None — company IS present (reqd) — NOT companyless.
    #   * status/grand_total CONFIRMED ABSENT, no substitute of any kind (Asset Movement shape).
    #   * date_field="posting_date" — REJOINS the largest pattern in this table as its
    #     FOURTEENTH member, the plain unremarkable case.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — PaymentOrder(Document) defines exactly two methods,
    #     on_submit/on_cancel, both hooks; no validate(), no def submit/def cancel anywhere.
    #   * Ledger preview: UNCALLABLE, bare Document MRO — joins the skip tuple (22nd member).
    #   * THE CENTRAL FINDING: update_payment_status (both directions) writes a status value
    #     directly onto a referenced Payment Request/Payment Entry via raw frappe.db.set_value —
    #     no validate/hooks/version-history/permission check on the target. Cancel writes
    #     "Initiated" UNCONDITIONALLY, never reading the target's current value — a genuine
    #     regression risk for a Payment Request whose own status has since moved past that (its
    #     own field carries six further values); Payment Entry's mirror field has only two values,
    #     no equivalent loss.
    #   * Cascade: genuinely NOT A LEAF — three real incoming submittable edges (Journal Entry,
    #     Payment Entry, Payment Request, all confirmed is_submittable=1), discovered by the
    #     standing generic mechanism, zero cascade.py code needed. The JE edge is created by this
    #     doctype's OWN whitelisted make_payment_records as a DRAFT (je.save(), never
    #     je.submit()) — a draft dependent never gates a cancel.
    #   * Side surface: THREE whitelisted callables (get_mop_query/get_supplier_query read-only;
    #     make_payment_records MUTATES — builds+saves, never submits, a draft Journal Entry, no
    #     dedup against a repeat call) — nothing granted toward any of them.
    PAYMENT_ORDER: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                    "date_field": "posting_date"},
    # Breadth (Share Transfer) — a full-attention landing (not a numbered wave row), the
    # thirty-ninth supported doctype. Full source-cited finding in the module docstring above
    # (dossier at docs/plans/dossiers/share_transfer.md, pre-verified at
    # docs/plans/dossiers/share_transfer.verify.md — the addendum's two corrections re-verified:
    # party_field conditions were SWAPPED in the original dossier, and Shareholder is NOT
    # submittable at all; plus one finding beyond the addendum's own scope, a doomed-submit gate
    # in the Purchase branch of basic_validations()). Summary:
    #   * party_field=None — from_shareholder/to_shareholder is a conditional pair, but a
    #     GENUINELY NEW sub-variant of the Blanket Order/Bank Guarantee dual-pair shape: those
    #     two are mutually exclusive (exactly one visible per state); Share Transfer's own
    #     three-state transfer_type shows BOTH simultaneously for Transfer. company IS present
    #     and reqd — NOT companyless.
    #   * status/grand_total CONFIRMED ABSENT; amount (Currency, read_only) is the correct
    #     stand-in, self-computed at share_transfer.py:198.
    #   * date_field="date" — REJOINS the standing date_field="date" pattern (Project Update,
    #     Asset Value Adjustment) as its THIRD member, a purely nominal collision.
    #   * submit_via=SUBMIT_VIA_RUN_METHOD — ShareTransfer(Document) defines 11 methods, none
    #     shadowing submit()/cancel(); on_submit/on_cancel are plain hooks.
    #   * Ledger preview: UNCALLABLE, bare Document MRO — joins the skip tuple (23rd member).
    #   * Cascade: a genuinely ISOLATED LEAF — the strictest shape this campaign has found. No
    #     other doctype links to Share Transfer (one hit, the self-referencing amended_from,
    #     confirmed across both checkouts), AND Share Transfer's own outgoing Links all point at
    #     non-submittable/non-transactional doctypes (Shareholder, Share Type, Account, Company)
    #     — it can never appear as a node in ANY cascade graph, its own or anyone else's. No
    #     cascade.py wiring needed at all.
    #   * THE DOOMED-SUBMIT FINDING (beyond the addendum): a Purchase-type transfer with a
    #     populated from_shareholder but a blank from_folio_no throws frappe.DoesNotExistError
    #     during validate() (share_transfer.py:170-177's own from_folio_no/to_shareholder field
    #     mismatch, traced through frappe.get_doc("Shareholder", None) -> load_from_db() ->
    #     document.py:294-297) before any share_balance mutation runs — genuinely reachable
    #     whenever the seller's own Shareholder has never had a folio_no assigned.
    #   * folio_no_validation() (share_transfer.py:222-240) writes a Shareholder's folio_no on
    #     EVERY save, not only submit — a draft-save through any channel already mutates a
    #     sibling before a marker ever exists.
    #   * Side surface: ONE whitelist callable — make_jv_entry (builds an unsaved Journal Entry
    #     dict, never inserted/submitted). The broker grants nothing toward it.
    SHARE_TRANSFER: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                     "date_field": "date"},
    # BOM Creator added 2026-07-21 (a full-attention landing, John's ruling 2 built first) — the
    # fortieth supported doctype, same run_method submit/cancel surface (confirmed: no def
    # submit()/def cancel() override anywhere in bom_creator.py — only on_submit/on_cancel/
    # before_submit HOOKS, 613-line full read). party_field=None — no Customer/Supplier/Party
    # field anywhere across all 40 enumerated fields (bom_creator.json); it operates purely on
    # Item and manufacturing operations. status CONFIRMED REAL (Draft/Submitted/In Progress/
    # Completed/Failed/Cancelled, read_only, no_copy, default "Draft") alongside a separate
    # error_log (Text, read_only) populated only on the Failed branch (bom_creator.py:317-322).
    # No grand_total — raw_material_cost (Currency, in_list_view) is the correct stand-in,
    # alongside item_code/currency (the fortieth doctype's own _list_fields branch). company IS
    # present and reqd (bom_creator.json) — NOT companyless. date_field=None — THE THIRD DATELESS
    # DOCTYPE (after BOM/Packing Slip): zero Date/Datetime fields across all 40, confirmed by a
    # direct field-type enumeration, not eyeballed; BOM Creator is absent from hooks.py's
    # period_closing_doctypes, so the closed-books chain's declared-dateless pass is equal to
    # ERPNext, never a weakening. THE CENTRAL FINDING (John's ruling 2, two-phase PROVE): on_submit
    # enqueues create_boms() on frappe's "short" queue (timeout 600s) — the BOM tree is built by a
    # background worker, after this broker's submit response already answered; registered in
    # ENQUEUE_ON_SUBMIT_DOCTYPES (above), the ledger's own outcome narrows to
    # "committed_pending_async" rather than a plain "committed" until a later sweep resolves the
    # real Completed/Failed result (tools.py's _QueuedConsequenceEffects/
    # _sweep_queued_consequences). create_bom()'s own idempotency guard (bom_creator.py:328-337)
    # is keyed to self.name, so an AMENDED draft (a new document name) builds a full SECOND BOM
    # tree with nothing detecting the duplication — disclosed at plan_submit for an amended draft
    # specifically (_bom_creator_submit_risk_flags). on_cancel (bom_creator.py:160-161) is
    # synchronous, a single self.set_status(True) db_set, no enqueue — but does NOT cascade to any
    # BOMs the job already built (dossier confirmed): those BOM documents remain submitted,
    # pointing at the now-cancelled creator via their own bom_creator field. CASCADE: NOT a leaf —
    # a full two-checkout grep for '"options": "BOM Creator"' returns exactly two hits, this
    # doctype's own self-referencing amended_from (bom_creator.json:228, auto-exempted from the
    # back-link check by frappe itself) and BOM's own real, submittable, non-required bom_creator
    # Link (bom.json:600-608) — so once create_boms() has built at least one submitted BOM, this
    # broker's OWN blast-radius gate (mirroring frappe's check_no_back_links_exist,
    # document.py:1450-1452) refuses a leaf cancel of the creator exactly as it would for any other
    # doctype with a submitted incoming link; BOM Creator is NOT registered in
    # SELF_UNLINKING_DOCTYPES (its on_cancel never touches the BOM's own bom_creator field, unlike
    # Delivery Trip's update_delivery_notes) — plan_cascade_cancel (cancelling the built BOM(s)
    # first, then the creator) is the governed path once any BOM exists, disclosed at plan_cancel.
    # Native ledger_preview UNCALLABLE (BOMCreator(Document) directly, no make_gl_entries anywhere
    # — joins the skip tuple, 24th member); BOM Creator posts no GL of any kind, ever — its only
    # products are BOM documents (manufacturing, not accounting). CAVEAT: 8 whitelisted callables
    # (add_boms forces a submit(); enqueue_create_boms is an UNGOVERNED re-trigger of create_boms(),
    # gated only by submit permission, that can re-fire after a Completed/Failed terminal status
    # — the idempotency guard bounds but does not prevent this; edit_bom_creator/add_item/
    # add_sub_assembly/delete_node all mutate a submitted document's rows via save() with no
    # docstatus check; get_default_bom/get_children are read-only) — this grant covers
    # submit/cancel/reads only; it does NOT extend to those methods, and no tool is built for any
    # of them.
    BOM_CREATOR: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD, "date_field": None},
    # Budget added 2026-07-21 (a full-attention landing off the pre-verification addendum,
    # docs/plans/dossiers/budget.verify.md) — the forty-first supported doctype, same run_method
    # submit/cancel surface (confirmed: zero submit/cancel/on_submit/on_cancel/before_submit/
    # on_trash anywhere in budget.py's Budget class — the SIXTH member of the "no on_submit/
    # on_cancel hook of any kind" family, after Blanket Order/Cost Center Allocation/Supplier
    # Scorecard Period/Sales Forecast/Project Update, and by far its richest: validate() alone
    # spans 8 sub-validations plus a separate before_save()/on_update() pair regenerating the
    # budget_distribution table on every save). party_field=None — cost_center/project is a dual
    # CONDITIONAL pair gated on budget_against (depends_on "== 'Cost Center'"/"== 'Project'"),
    # spliced WHOLE. status/grand_total CONFIRMED ABSENT; budget_amount/budget_distribution_total
    # exist but carry no in_list_view flag, so neither substitutes in the list tier — a genuinely
    # bare branch. in_list_view is exactly [budget_against, company, account]. company IS present
    # and reqd — NOT companyless. date_field="budget_start_date" — THE FIRST HIDDEN date_field
    # this campaign has pinned (hidden:1, schema-optional, but validate()-FORCED non-blank via the
    # reqd from_fiscal_year Link on every persisted document — see the module docstring's own
    # "fifth wrinkle" and the SUPPORTED_DOCTYPES header comment above); the FIFTEENTH distinct
    # date-fieldname pattern, no collision. Ledger preview UNCALLABLE (Budget(Document) directly,
    # no make_gl_entries anywhere — joins the skip tuple, 25th member). Cascade: a genuine LEAF —
    # a full two-checkout grep for '"options": "Budget"' returns exactly one hit, the doctype's
    # own self-referencing amended_from (revision_of is fieldtype Data, NOT a Link, so it never
    # widens this scan). Whitelist count: exactly 1 (module-level revise_budget — cancel + copy +
    # insert, a CLEAN mutation grade, no raw SQL/db_set bypass). THE CONTROL-PLANE FINDING:
    # submitting a Budget ARMS a belt governing FUTURE submits of OTHER documents (PO/MR's own
    # on_submit through buying_controller.py's validate_budget(), which branches on Accounts
    # Settings.use_legacy_budget_controller — FALSY, the schema default, routes through
    # budget_controller.BudgetValidation, NOT the legacy validate_expense_against_budget()
    # general_ledger.py's own two call sites use unconditionally; BOTH engines are live
    # simultaneously, BOTH synchronous, never async — the dossier's "Async channel" RED FLAG is
    # corrected), until this Budget is cancelled — cancelling DISARMS it immediately, since both
    # engines filter on docstatus == 1 (confirmed by direct source read). Disclosed data-driven in
    # _budget_submit_risk_flags/_budget_cancel_risk_flags. period_closing_doctypes: Budget
    # confirmed ABSENT (hooks.py:326-345); its one other hooks.py appearance
    # (accounting_dimension_doctypes) is unrelated, never a lifecycle hook.
    BUDGET: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
             "date_field": "budget_start_date"},
    # Timesheet added 2026-07-21 (a Sonnet landing agent off the pre-verification addendum,
    # docs/plans/dossiers/timesheet.verify.md) — the forty-second supported doctype.
    # party_field="customer" — a plain, singular, header-level Link, no reqd key, no dual/Dynamic
    # shape. submit_via=SUBMIT_VIA_RUN_METHOD — the dossier's own "client_rpc" conclusion is
    # WRONG per the standing law (SUBMIT_VIA_CLIENT_RPC needs a genuine submit()/cancel()
    # override; Timesheet's on_submit is an ordinary throwing HOOK, confirmed by a 25-def
    # class-body grep with zero overrides). status PRESENT (7 options; set_status() is THREE
    # SEQUENTIAL UNCONDITIONAL ifs, not if/elif — a truthy sales_invoice always wins last and
    # forces "Completed", overriding a per_billed>=100 "Billed" result). grand_total ABSENT, two
    # stand-ins (total_billable_amount/total_billed_amount) neither in_list_view-flagged, so
    # neither substitutes. in_list_view is exactly {start_date, per_billed} — forces its own new
    # _list_fields branch. date_field="start_date" — JOINS the existing start_date exclusivity
    # set as its THIRD member (Supplier Scorecard Period/Bank Guarantee), both start_date/end_date
    # recomputed by set_dates() on EVERY validate() (draft saves AND submit, not "on submit"
    # alone — the addendum's own timing correction). Ledger preview UNCALLABLE
    # (Timesheet(Document) directly, no make_gl_entries anywhere incl. timesheet_detail.py —
    # joins the skip tuple, 26th member). company PRESENT BUT OPTIONAL — the FOURTH such row
    # (after Purchase Invoice/Quality Inspection/Asset Value Adjustment, not the first); the
    # 0fdf91d _locks_for guard already covers a blank instance ERPNext-EQUAL, so NOT added to the
    # companyless tally and NOT REG_UNPINNED-gated — unlike Asset Value Adjustment, a blank
    # company here has NO submit-time consequence of its own (company is read exactly once, by
    # the whitelisted make_sales_invoice, never by validate()/on_submit). THE SECOND WRITER (the
    # sharpest finding): Sales Invoice's own on_submit/before_cancel both call update_time_sheet()
    # (sales_invoice.py:828-837), which recalculates this Timesheet's billing fields, calls
    # set_status(), then timesheet.db_update_all() with ignore_validate_update_after_submit=True
    # — a RAW WHOLE-DOCUMENT write (parent + every time_logs child row, frappe/model/
    # base_document.py:849-851, self-documented "DOES NOT VALIDATE AND CALL TRIGGERS"), wider
    # than Maintenance Visit's own single-document db_update and the FIRST case where a document
    # rewrites ITS OWN submitted state from outside another doctype's lifecycle. Cascade: NOT a
    # leaf — a full two-checkout grep for '"options": "Timesheet"' returns exactly 2 hits, the
    # doctype's own self-referencing amended_from and Sales Invoice Timesheet.time_sheet (a REAL
    # Link on a child table, istable=1, embedded in submittable Sales Invoice) — frappe's own
    # get_submitted_linked_docs walks child tables generically (get_parent_if_child_table_doc=
    # True), so a submitted Sales Invoice naming this Timesheet blocks a leaf plan_cancel exactly
    # like a header-level Link would; plan_cascade_cancel is the governed path, zero cascade.py
    # changes. Whitelist count: exactly 7 module-level (6 read-only + make_sales_invoice, which
    # only builds a NEW unsaved Sales Invoice, never touches the source) + 2 non-whitelisted
    # portal helpers. "Payslip" is a live status option with ZERO in-tree writer (HR/Payroll
    # moved to the separate hrms app in v14) — Sales Invoice's own validate_time_sheets_are_
    # submitted() still reads for it (sales_invoice.py:938-943). NOT in period_closing_doctypes
    # (hooks.py:326-345). No async/enqueue/scheduler channel.
    TIMESHEET: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD,
                "date_field": "start_date"},
    # Contract added 2026-07-21 (a Sonnet landing agent off the pre-verification addendum,
    # docs/plans/dossiers/contract.verify.md) — the forty-third supported doctype. Full
    # source-cited finding in the module docstring's own "Breadth (Contract)" section above.
    # party_field=None — a Dynamic Link pair (party_type Select Customer/Supplier/Employee reqd +
    # party_name Dynamic Link reqd), the Quotation/Quality Inspection shape. status PRESENT AND
    # DOUBLED (status + fulfilment_status, both allow_on_submit) but a WRITABILITY ILLUSION post-
    # submit: before_update_after_submit (contract.py:67-69, fires on every save of an
    # already-submitted Contract per frappe/model/document.py:1135-1138/:1410-1411)
    # unconditionally recomputes both from start_date/end_date/is_signed, discarding a direct
    # write to either field on the very next save — is_signed is the real lever. grand_total
    # ABSENT, no substitute. date_field="signed_on" (Datetime, allow_on_submit=1, no reqd, never
    # set by any code anywhere) — THE PIN this landing made (the dossier left it open): the
    # Datetime shape needs the existing _posting_date_of projection (Work Order/Asset Movement
    # precedent); genuinely, verifiably blank-capable on a submitted Contract (NOT forced non-
    # blank the way Budget's/Timesheet's own schema-optional date pins are), so this broker is
    # STRICTER than ERPNext here — a blank signed_on discloses at plan (Envelope E6) and REFUSES
    # at execute (check_red_line's "no posting_date" branch, the Project Update/Asset Maintenance
    # Log machinery, zero new code) even though ERPNext itself would submit it; and because
    # signed_on is allow_on_submit with no recompute hook, the pinned date can genuinely MOVE
    # post-submit — the first date_field pin in this campaign combining genuine blank-capability
    # with allow_on_submit (Work Order's/Shipment's own allow_on_submit date pins are both reqd=1
    # and can never be blank; Project Update's/Asset Maintenance Log's own blank-capable date
    # pins are neither allow_on_submit). Zero practical consequence today since Contract is
    # companyless (_locks_for returns {} before any lock read). submit_via=SUBMIT_VIA_RUN_METHOD
    # — no submit()/cancel() override in the 9-method class body. THE SCHEDULER: update_status_
    # for_contracts (contract.py:129-144, daily_maintenance, hooks.py:473) filters
    # is_signed=True AND docstatus=1 EXPLICITLY — the campaign's FIRST CONFIRMED SUBMITTED-STATE
    # scheduler mutator (the mirror of Asset Maintenance Log's own draft-only-in-practice
    # scheduler), flipping status only between Active/Inactive via a raw frappe.db.set_value
    # (no validate/hooks/permission/version). THE STATUS/DOCSTATUS DISCONNECT: the only path that
    # ever writes status="Cancelled" is on_discard, hard-guarded to docstatus=0 by frappe's own
    # discard() — so a submitted Contract's status can only ever cycle Active<->Inactive
    # (or Unsigned) for life; a cancelled-docstatus Contract keeps displaying its last computed
    # status forever. company CONFIRMED ABSENT — the SEVENTH companyless doctype (after Packing
    # Slip/Supplier Scorecard Period/Shipment/Project Update/Asset Maintenance Log/Bank
    # Guarantee); signed_by_company (Link->User, stores the submitting user) and signee_company
    # (Signature) are decoy fieldnames, neither is the registry company. Ledger preview
    # UNCALLABLE (Contract(Document) directly, no make_gl_entries anywhere) — joins the skip
    # tuple, 27th member. Cascade: a CASCADE LEAF — the only Link -> Contract field anywhere in
    # the v16 tree is its own self-referencing amended_from. Whitelist count: 0 (not a first —
    # at least five prior rows already landed at zero). NOT in period_closing_doctypes
    # (hooks.py:326-345, correcting the dossier's own :473-490 citation, which is actually where
    # the scheduler above is registered).
    CONTRACT: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
              "date_field": "signed_on"},
    # Pick List added 2026-07-21 (a Sonnet landing agent off the pre-verification addendum,
    # docs/plans/dossiers/pick_list.verify.md) — the forty-fourth supported doctype.
    # party_field="customer" — a plain, always-real, always-queryable header Link (in_list_view=1,
    # no reqd); depends_on: eval:doc.purpose==='Delivery' is a pure Desk-form DISPLAY directive,
    # never a data-model condition — a genuinely different shape from every prior "conditional
    # party" row (Blanket Order/Bank Guarantee/Payment Order/Share Transfer all carry
    # party_field=None instead). submit_via=SUBMIT_VIA_RUN_METHOD — no def submit()/def cancel()
    # override anywhere in the 1893-line pick_list.py (grep-clean). status PRESENT (Select,
    # reqd=1, hidden=1, read_only=1, 6 options) — confirmed real and list-tier-includable despite
    # being form-hidden (pick_list_list.js's own get_indicator reads doc.status directly, the
    # Delivery Trip precedent generalized one step further: hidden AND not in_list_view, a new
    # combination distinct from Contract's own hidden-but-in_list_view status). grand_total
    # ABSENT, no substitute (in_list_view confirmed exactly {company, customer}). DATELESS — the
    # FOURTH NO_DATE_FIELD member (BOM/Packing Slip/BOM Creator/Pick List; the addendum's own
    # "third" was stale, written before BOM Creator's own same-morning landing) — zero Date/
    # Datetime fields across the 35 parent (pick_list.json) AND 37 child (pick_list_item.json)
    # fields, both independently json.load-enumerated. company reqd=1 — NOT companyless, standard
    # wrong-books belt applies. THE CASCADE QUESTION (the row's decisive fact — both the dossier
    # and the addendum concluded LEAF and were WRONG): Delivery Note and Sales Invoice are BOTH
    # is_submittable=1 (json.load-confirmed); their own child tables (Delivery Note Item/Sales
    # Invoice Item, istable=1) carry real Link fields to Pick List (against_pick_list) — frappe's
    # own linked_with.py resolves a child-table hit back to its submittable PARENT under
    # get_parent_if_child_table_doc=True + a docstatus==1 filter (the exact mechanism this
    # morning's Timesheet landing already proved for Sales Invoice Timesheet.time_sheet) — so a
    # submitted DN or SI holding against_pick_list rows blocks a leaf plan_cancel. Stock Entry.
    # pick_list is a THIRD, simpler, independent edge: a direct HEADER-LEVEL Link on submittable
    # Stock Entry, needing no child-table resolution at all. Pick List is NOT a cascade leaf on
    # three independent, confirmed counts (exactly 4 "options": "Pick List" hits across BOTH
    # checkouts: Stock Entry.pick_list, DN Item.against_pick_list, SI Item.against_pick_list, and
    # Pick List's own self-referencing amended_from); plan_cascade_cancel is the governed path.
    # Ledger preview UNCALLABLE (PickList -> TransactionBase -> StatusUpdater -> Document, the
    # addendum's own MRO correction, 4-deep, the FOURTH doctype of this shape after Installation
    # Note/Maintenance Visit/Maintenance Schedule; no make_gl_entries anywhere — joins the skip
    # tuple, 28th member, correcting the addendum's stale "22nd"). THE SUBMIT-THROW SURFACE IS 12
    # SITES, NOT 2 (the dossier's own undercount, corrected per the addendum): 4 deterministic-
    # from-draft (validate_for_qty :705-709, validate_warehouses missing-warehouse :171-178,
    # validate_picked_items scan_mode :265-273, the on_submit-called legacy-serial-batch throw
    # :318-323) + 8 cross-document/live-state, staying prose (validate_stock_qty x2, check_
    # serial_no_status, validate_expired_batches, validate_warehouses company-mismatch,
    # validate_sales_order_percentage, validate_sales_order, validate_picked_qty via
    # update_reference_qty). THE TWO-JAW TRAP: on_update_after_submit throws if has_reserved_
    # stock() (100% draft-deterministic) but on_cancel never throws and explicitly preserves
    # Serial and Batch Bundle/Stock Reservation Entry/Delivery Note via ignore_linked_doctypes —
    # can't edit once reserved, CAN cancel-and-orphan. THE RESERVATION MACHINERY: create_stock_
    # reservation_entries/cancel_stock_reservation_entries (whitelisted, callable on a submitted
    # doc) are a multi-hop delegation through Sales Order's own method into stock_reservation_
    # entry.create_stock_reservation_entries_for_so_items (typed Literal["Pick List", "Purchase
    # Receipt"] — Purchase Receipt shares this path TODAY) — a FOURTH cross-document mutation
    # honesty grade (multi-hop delegation through another doctype's own chain), nothing granted.
    # Whitelist count: exactly 10 (3 instance + 7 module), confirmed. NOT in period_closing_
    # doctypes (hooks.py:326-345). No enqueue/scheduler channel anywhere in pick_list.py.
    PICK_LIST: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD,
               "date_field": None},
    # Asset Repair added 2026-07-21 (a Sonnet landing agent off the pre-verification addendum,
    # docs/plans/dossiers/asset_repair.verify.md) — the forty-fifth supported doctype. Full
    # source-cited finding in the module docstring's own "Breadth (Asset Repair)" section above.
    # party_field=None — asset is a fixed-asset reference, never a GL party. status="repair_status"
    # PRESENT (3 options: Pending/Completed/Cancelled); grand_total ABSENT, total_repair_cost
    # stand-in. date_field="completion_date" — the GL-posting-date field (GL entries stamp
    # posting_date: self.completion_date at three sites), NOT the unconditionally-reqd failure_date
    # — completion_date is Datetime, mandatory_depends_on repair_status=="Completed" only, REJOINING
    # Asset Maintenance Log's own "completion_date" fieldname as its SECOND member (a nominal
    # collision only — AML's copy is a plain Date, this one is Datetime and conditionally
    # mandatory). LANDING RISK #1 (the addendum's sharpest, now a named test): a SUBMITTED Asset
    # Repair with repair_status=="Cancelled" can carry a genuinely EMPTY completion_date (check_
    # repair_status blocks submit only for "Pending") — the existing _posting_date_of/check_
    # red_line machinery already refuses this as "no posting_date," zero new code. submit_via=
    # SUBMIT_VIA_RUN_METHOD — 28-def class body, zero submit()/cancel() overrides. THE MRO
    # CORRECTION: AssetRepair -> AccountsController -> TransactionBase -> StatusUpdater ->
    # Document (the addendum's own fabricated "StockController, DocumentController" chain,
    # corrected). Ledger preview CALLABLE (make_gl_entries is AssetRepair's own method) — the
    # Asset precedent, NOT the skip tuple (unchanged at 28); projected_gl is honest-empty per TWO
    # independent causes (capitalize_repair_cost falsy; OR capitalize_repair_cost truthy but
    # total_repair_cost <= 0), disclosed data-driven. THE STOCK ENTRY AUTO-SUBMIT: unconditional
    # (no capitalize_repair_cost gate), no try/except — a failed Stock Entry submit fails the
    # whole Asset Repair submit with it. THE ASSET-STATUS SIDE-WRITE (the addendum's sharpest
    # catch, LANDED SUBMIT-ONLY): validate()'s own update_status() raw-writes the linked Asset's
    # status via frappe.db.set_value/Asset.set_status()'s own db_set, ungated by capitalize_
    # repair_cost — but frappe's own run_before_save_methods() only calls run_method("validate")
    # for _action in ("save", "submit"), NEVER for "cancel" (document.py:1402-1409) — this
    # overturns the addendum's own Correction #1/Landing Risk #2 framing ("fires ... on cancel
    # alike"): it fires on submit only. Cascade: genuinely NOT a leaf — the SIMPLEST non-leaf
    # shape this campaign has found: Stock Entry.asset_repair is a direct, header-level,
    # read_only Link on submittable Stock Entry (stock_entry.json:705) — a full two-checkout grep
    # for '"options": "Asset Repair"' returns exactly 2 hits (that edge + this doctype's own
    # self-referencing amended_from); plan_cascade_cancel orders the Stock Entry first, zero
    # cascade.py changes, the Pick List precedent. company present but NOT reqd (fetch_from
    # asset.company) — the FIFTH such row (after PI/QI/AVA/Timesheet), but genuinely CONSEQUENTIAL
    # under two independent conditions (armed GL gate -> get_asset_account throws on blank
    # company; non-empty stock_items -> the auto-created Stock Entry's own reqd company throws
    # MandatoryError) — the AVA honesty grade, not Timesheet's zero-consequence one, though
    # reachable on this SAME doctype depending on the draft's own fields. Whitelist: exactly 4,
    # all read-only (get_downtime/get_purchase_invoice/get_expense_accounts/get_unallocated_
    # repair_cost), nothing granted. IS in period_closing_doctypes (hooks.py:339) — natively
    # EQUAL to this broker's own check, the Asset precedent.
    ASSET_REPAIR: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                   "date_field": "completion_date"},
    # Invoice Discounting added 2026-07-21 (a Sonnet landing agent off the pre-verification
    # addendum, docs/plans/dossiers/invoice_discounting.verify.md — its own Correction 1 carried a
    # wrong field-type claim, caught by the supervisor before dispatch and re-verified here) — the
    # forty-sixth supported doctype. Full source-cited finding in the module docstring's own
    # "Breadth (Invoice Discounting)" section above. party_field=None — a CHILD-TABLE party shape
    # (Discounted Invoice.customer per row, the RFQ family), never a header-level column.
    # status="status" PRESENT (JSON read_only:1, THREE runtime write paths — its own set_status(),
    # its own cancel-tail db_set, AND the cross-document db_set via Journal Entry below);
    # grand_total ABSENT, total_amount stand-in. date_field="posting_date" — REJOINS the large
    # existing set unchanged, no Datetime, no projection needed. submit_via=SUBMIT_VIA_RUN_METHOD —
    # confirmed by reading all 380 lines of invoice_discounting.py, no submit()/cancel() override.
    # MRO CORRECTION: InvoiceDiscounting -> AccountsController -> TransactionBase -> StatusUpdater
    # -> Document (the dossier's fabricated "StockController" chain, corrected). Ledger preview
    # CALLABLE (make_gl_entries is its own method) — the THIRD such row (Asset, Asset Repair,
    # Invoice Discounting), NOT the skip tuple (unchanged at 28); projected_gl is honest-empty PER
    # INVOICE ROW (not per document) when that row's own outstanding_amount is zero/blank.
    # THE SUPERVISOR'S OWN CATCH, CONFIRMED: reference_name on Journal Entry Account is a real
    # Dynamic Link (fieldtype "Dynamic Link", options "reference_type"), NOT the "free Data field"
    # the addendum's Correction 1 claimed. THE CASCADE QUESTION, settled from source: Invoice
    # Discounting is NOT a leaf — this broker's own get_submitted_linked_docs walks Dynamic Link
    # fields via the SAME live-distinct-value mechanism the Landed Cost Voucher/Dunning landings
    # already proved (linked_with.py's get_references_across_doctypes_by_dynamic_link_field), and
    # frappe's own native check_no_back_links_exist (document.py:1571-1578) independently agrees
    # via check_if_doc_is_dynamically_linked — a submitted Journal Entry naming this document via
    # reference_type='Invoice Discounting' blocks a leaf plan_cancel and is ordered first by
    # plan_cascade_cancel, zero cascade.py changes; the addendum's "a grep will never find it" is
    # true only of the campaign's STATIC GREP METHOD, never of the runtime. THE INCOMING MUTATOR
    # (disclosed in both directions, consistent with the Journal Entry retrofit, commit 3fa3303):
    # JournalEntry.on_submit/on_cancel -> update_invoice_discounting (journal_entry.py:459-501) ->
    # InvoiceDiscounting.set_status's own "if status:" branch (invoice_discounting.py:101-108) — a
    # raw db_set this document's own hooks never reach — cascading into every linked Sales
    # Invoice's status too. BYPASS TAXONOMY: the SAME db_set-via-target's-own-method family as
    # Asset/Asset Repair's "Asset-status raw side-write" (the caller uses the TARGET's own public
    # method, never reaching around it), NOT Maintenance Visit's/AML's generic db_update-bypass
    # family — but a genuinely new, deeper wrinkle: a CHAINED, third-party-triggered self-bypass (3
    # doctypes deep: Journal Entry -> Invoice Discounting -> N x Sales Invoice), "delegated
    # self-bypass, chained". company PRESENT AND reqd:1 (unlike Asset Repair's optional company) —
    # the standard wrong-books belt applies unconditionally. Whitelist: exactly 3
    # (create_disbursement_entry/close_loan both return unsaved Journal Entries, get_invoices a
    # pure read), nothing granted. IS in period_closing_doctypes (hooks.py, 7th entry) — natively
    # EQUAL to this broker's own check, the THIRD such row after Asset/Asset Repair. in_list_view
    # confirmed {posting_date, company} forces the 36th special _list_fields branch.
    INVOICE_DISCOUNTING: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                          "date_field": "posting_date"},
    # Asset Capitalization added 2026-07-21 (a Sonnet landing agent off the pre-verification
    # addendum, docs/plans/dossiers/asset_capitalization.verify.md — whose own headline RED FLAG,
    # a target-asset cost double-count on re-submit, is ITSELF REFUTED here from source) — the
    # forty-seventh supported doctype. Full source-cited finding in the module docstring's own
    # "Breadth (Asset Capitalization)" section above. party_field=None — no Customer/Supplier/
    # Party Link anywhere in the 39-field enumeration. status/grand_total ABSENT, total_value
    # stand-in; in_list_view confirmed EXACTLY {posting_date} — forces the 37th special
    # _list_fields branch. date_field="posting_date" — a plain Date, paired with a separate
    # posting_time (Time) — the Stock Entry/Stock Reconciliation Date+Time pair precedent, not a
    # new pattern. submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading all 882 lines of
    # asset_capitalization.py, zero submit()/cancel() overrides. MRO CORRECTION (the addendum's
    # own, re-verified): AssetCapitalization -> StockController -> AccountsController ->
    # TransactionBase -> StatusUpdater -> Document, 5-deep — one link deeper than Asset Repair's/
    # Invoice Discounting's own 4-deep chains (StockController is the added link) — the CALLABLE
    # category's deepest MRO yet. Ledger preview CALLABLE (make_gl_entries is its own method,
    # asset_capitalization.py:388-398 — the addendum corrected the dossier's :388-426 conflation
    # with the separate get_gl_entries builder at :400-426) — NOT the skip tuple (unchanged at
    # 28). THE DOUBLE-COUNT RED FLAG IS REFUTED: update_target_asset's docstatus-branch (add on
    # submit / subtract on cancel, asset_capitalization.py:554-573) is symmetric by construction,
    # and Document.validate_amended_from (frappe/model/document.py:613-618) blocks any amendment
    # until the original's own cancel has already landed — no window for a double add exists. THE
    # SIBLING-DOCUMENT FACTORY: the SECOND confirmed instance of Asset Value Adjustment's own
    # reschedule_depreciation factory, reached via consumed-asset depreciation
    # (get_gl_entries_for_consumed_asset_items -> depreciate_asset, depreciation.py:477-487) rather
    # than AVA's own update_asset() — armed off the draft's own asset_items rows (presence),
    # calculate_depreciation itself a cross-document read, prose per the standing rule. THE ASYNC
    # CHANNEL: repost_future_sle_and_gle (both directions) can arm "Repost Item Valuation," an
    # ERPNext-scheduled channel this broker does not govern (the Asset precedent) — the dossier's
    # "no async channels" claim does not ship; a RETROACTIVE GAP was found and flagged (not
    # silently fixed) across Stock Entry/Stock Reconciliation/Delivery Note/Purchase Receipt, all
    # of which call the identical mechanism with no risk-flag disclosure of their own. Cascade:
    # genuine LEAF for incoming links (full two-checkout grep for '"options": "Asset
    # Capitalization"' returns only its own amended_from) but a DEPENDENT NODE, on two edges
    # (target_asset + asset_capitalization_asset_item.asset), inside an Asset's own cascade graph
    # (already enumerated there) — zero cascade.py changes. company PRESENT AND reqd:1 — standard
    # wrong-books belt. Whitelist: exactly 9, nothing granted. IS in period_closing_doctypes
    # (hooks.py:338) — the 12th entry, NOT the addendum's stale "13th" (Asset Repair is the 13th,
    # at line 339) — natively EQUAL to this broker's own check, the FOURTH such row.
    ASSET_CAPITALIZATION: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                          "date_field": "posting_date"},
    # Production Plan added 2026-07-21 (a Sonnet landing agent off the pre-verification addendum,
    # docs/plans/dossiers/production_plan.verify.md — unusually strong, re-verified every dossier
    # line cite, but whose own "26 branches" tally was stale) — the forty-eighth supported
    # doctype. Full source-cited finding in the module docstring's own "Breadth (Production Plan)"
    # section above. party_field=None — the only party-shaped field, customer, is a conditional
    # UI filter (depends_on get_items_from=="Sales Order"), never a GL party. status PRESENT (8
    # options incl. Closed/Material Requested) but NOT in_list_view; grand_total ABSENT, no
    # substitute. date_field="posting_date" — a plain Date, rejoins the large existing set.
    # submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading all 2261 lines of
    # production_plan.py, zero submit()/cancel() overrides. Ledger preview UNCALLABLE
    # (ProductionPlan(Document) directly, no make_gl_entries anywhere) — joins the skip tuple, its
    # 29th member. Cascade: GENUINELY NOT A LEAF — a full two-checkout grep for '"options":
    # "Production Plan"' finds THREE real external edges (Work Order.production_plan, a direct
    # header Link; Purchase Order Item.production_plan and Material Request Item.production_plan,
    # both child-table Links resolving to submittable parents via frappe's own linked_with
    # mechanism) — the dossier's own "leaf" claim overturned, its own §8 text self-contradicted.
    # THE CANCEL-ORDERING WRINKLE: on_cancel runs BEFORE check_no_back_links_exist
    # (frappe/model/document.py:1450-1452) — this broker's own blast-radius gate refuses BEFORE
    # any on_cancel side effect runs, stricter than a raw native cancel. TWO AUTO-SUBMIT CHANNELS:
    # Material Request via a caller-supplied, non-persisted submit_material_request flag (prose,
    # not draft-deterministic); Stock Reservation Entry, UNCONDITIONAL once the real reserve_stock
    # Check field is set (data-driven off the draft) — the dossier said "auto-submitted: NO" in
    # three places, OVERTURNED. THE FORWARD CHECK: reserve_stock propagates onto every Work Order
    # this plan creates (create_work_order, :934) — Work Order's OWN landing (21st doctype) never
    # discloses this SRE channel — A CONFIRMED RETROACTIVE GAP, reported not silently fixed.
    # Whitelist: 17 (9 instance + 8 module, the dossier's 10/7 split corrected on both tiers and
    # membership), nothing granted. company PRESENT AND reqd:1 — standard wrong-books belt. NOT
    # in period_closing_doctypes — broker equal-or-stricter, the standing posture. No async/
    # enqueue anywhere.
    PRODUCTION_PLAN: {"party_field": None, "submit_via": SUBMIT_VIA_RUN_METHOD,
                      "date_field": "posting_date"},
    # Subcontracting Order added 2026-07-21 (a Sonnet landing agent off the pre-verification
    # addendum, docs/plans/dossiers/subcontracting_order.verify.md — its central finding, THE
    # SEVEN-PATH MUTATOR MAP, re-verified byte-for-byte) — the forty-ninth supported doctype.
    # Full source-cited finding in the module docstring's own "Breadth (Subcontracting Order)"
    # section above. party_field="supplier" (Link, reqd:1, label "Job Worker" — a plain GL
    # party). status PRESENT (8 options); total (NOT grand_total — a genuine stand-in fieldname,
    # depends_on purchase_order) PRESENT. date_field="transaction_date" — rejoins the existing
    # set, no new pattern. submit_via=SUBMIT_VIA_RUN_METHOD — confirmed by reading every class in
    # the MRO (SubcontractingOrder/SubcontractingController/StockController/AccountsController/
    # TransactionBase), zero submit()/cancel() overrides anywhere. Ledger preview CALLABLE,
    # honest-empty — make_gl_entries genuinely exists in the MRO (StockController, never
    # overridden) but on_submit never calls update_stock_ledger, so no Stock Ledger Entry row is
    # ever posted under this voucher's own name; the SO/PO shape, NOT the skip tuple. Cascade:
    # GENUINELY NOT A LEAF on TWO independent submittable-referencer families — Stock Entry's own
    # direct header Link, AND Subcontracting Receipt Item/Supplied Item's child-table Links
    # resolving to submittable Subcontracting Receipt via frappe's own linked_with mechanism — a
    # correction to the dossier's/addendum's own "non-submittable, never appears" framing. THE
    # SEVEN-PATH MUTATOR MAP: six of seven paths that rewrite this order's own status/supplied
    # items carry ZERO write-permission check, firing as ordinary submit/cancel side effects of
    # Subcontracting Receipt and Stock Entry — including a RAW module-level frappe.db.set_value
    # into the child table with no Document instantiation at all, and a status path that
    # deliberately bypasses the whitelisted boundary per an upstream source comment quoted
    # verbatim at stock_entry.py:4060-4061. reserve_raw_materials fires as an on_submit side
    # effect (not merely a callable) and auto-submits Stock Reservation Entries unconditionally
    # via the SAME StockReservation class Production Plan's own landing already proved does so —
    # but cancel is NOT symmetric (on_cancel never reverses them, an orphan risk this landing
    # names). Whitelist: exactly 4 (2 instance + 2 module), nothing granted — the ONLY
    # permission-checked mutator is the whitelisted update_subcontracting_order_status. company
    # PRESENT AND reqd:1 — standard wrong-books belt, companyless tally unchanged at 6. NOT in
    # period_closing_doctypes (hooks.py:326-345, confirmed absent) — broker equal-or-stricter,
    # the standing posture.
    SUBCONTRACTING_ORDER: {"party_field": "supplier", "submit_via": SUBMIT_VIA_RUN_METHOD,
                           "date_field": "transaction_date"},
    # Subcontracting Inward Order added 2026-07-22 (a Sonnet landing agent off the
    # pre-verification addendum, docs/plans/dossiers/subcontracting_inward_order.verify.md — its
    # own center, THE ELEVEN-ROW MUTATOR MAP plus THE NEW BYPASS CLASS, re-verified byte-for-
    # byte) — the fiftieth supported doctype. Full source-cited finding in the module docstring's
    # own "Breadth (Subcontracting Inward Order)" section above. party_field="customer" (Link,
    # reqd:1 — a plain GL party). status PRESENT (8 options, read_only:1 AND reqd:1); NO
    # grand_total/total substitute anywhere — six in_list_view:1 per_* Percent columns are the
    # entire operational picture. date_field="transaction_date" — rejoins the existing set as its
    # TENTH member. submit_via=SUBMIT_VIA_RUN_METHOD — confirmed, zero submit()/cancel()
    # overrides anywhere in the 567-line file. Ledger preview CALLABLE-but-unconditionally-EMPTY
    # — the POS Invoice false-positive shape (make_gl_entries genuinely exists in the MRO,
    # 7 nodes, ties Subcontracting Order for deepest, but this voucher never writes its own Stock
    # Ledger Entry — all real movement is deferred onto four spawned Stock Entry documents). NOT
    # a cascade leaf — two DIRECT header-level Links (Work Order, Stock Entry), no child-table
    # resolution needed. THE ELEVEN-ROW MUTATOR MAP: four other doctypes (Stock Entry across five
    # purposes, Work Order, Sales Invoice, Sales Order) rewrite this order's own status/child
    # rows through five mechanisms — db_set, bulk_update, raw querybuilder UPDATE (twice, two
    # different doctypes), and a cross-document status force-close — only the whitelisted status
    # API carries any write-permission check. THE NEW BYPASS CLASS: three call sites
    # (frappe.delete_doc of docstatus=1 received_items/secondary_items rows) are legal only
    # because neither child doctype sets is_submittable — a genuinely new taxonomy slot, named
    # deliberately wherever a tool reads those child tables as a snapshot. Whitelist: exactly 6
    # (5 instance + 1 module), nothing granted. company PRESENT AND reqd:1 — standard wrong-books
    # belt, companyless tally unchanged at 6. NOT in period_closing_doctypes (hooks.py:326-345,
    # confirmed absent) — broker equal-or-stricter, the standing posture.
    SUBCONTRACTING_INWARD_ORDER: {"party_field": "customer", "submit_via": SUBMIT_VIA_RUN_METHOD,
                                  "date_field": "transaction_date"},
    # Subcontracting Receipt added 2026-07-22 (a Sonnet landing agent off the pre-verification
    # addendum, docs/plans/dossiers/subcontracting_receipt.verify.md — 2 corrections + 5 landing
    # risks, all re-verified byte-for-byte) — THE ROOF ROW, the fifty-first and FINAL GOVERN
    # doctype. Full source-cited finding in the module docstring's own "Breadth (Subcontracting
    # Receipt)" section above. party_field="supplier" (Link, reqd:1, label "Job Worker" — a plain
    # GL party). status PRESENT (6 real options + a leading blank, read_only:1 AND reqd:1 but
    # backend-writable via a RAW frappe.db.set_value, not even self.db_set); total (NOT
    # grand_total — a genuine stand-in fieldname) PRESENT. date_field="posting_date" — the DN/PR/
    # SE/AC default path, zero Datetime fields on the doctype at all. submit_via=
    # SUBMIT_VIA_RUN_METHOD — confirmed, zero submit()/cancel() overrides across the full 4-node
    # ancestor MRO (7 nodes total incl. leaf). Ledger preview CALLABLE, genuine BOTH-LEDGERS (own
    # get_gl_entries override + an unconditional update_stock_ledger override) — but SCR is
    # confirmed ABSENT from ERPNext's own native-preview SLE-seeding whitelist
    # (stock_controller.py:2109, unlike Purchase Receipt/Delivery Note/Stock Entry), and its own
    # GL-building arithmetic subtracts the resulting None BEFORE any flt() coercion — a
    # conditional live-crash risk under perpetual inventory, THE SIXTH ledger-preview shape this
    # campaign has found, disclosed data-driven, knowledge-pinned pending live-prove. THE CANCEL
    # BACK-LINK GATE: Purchase Receipt.subcontracting_receipt is a real Link absent from SCR's own
    # ignore_linked_doctypes, so cancelling an SCR whose auto-created (save-only) sibling Purchase
    # Receipt has since been submitted is refused at the framework layer, independent of the
    # SCO-closed throw. THE SCO WRITEBACK: FOUR channels reach back into Subcontracting Order's
    # own status/child rows/Bin state from this doctype's submit/cancel, zero permission-checked
    # — two more than Subcontracting Order's own landing enumerated from this side. Whitelist:
    # exactly 6, nothing granted, plus an explicit note on the inherited get_current_stock.
    # company PRESENT AND reqd:1 — standard wrong-books belt, companyless tally unchanged at 6.
    # IS in period_closing_doctypes (hooks.py:344, the list's own LAST entry) — natively EQUAL to
    # this broker's own closed-books check, the FIFTH such row.
    SUBCONTRACTING_RECEIPT: {"party_field": "supplier", "submit_via": SUBMIT_VIA_RUN_METHOD,
                             "date_field": "posting_date"},
}


def _list_fields(doctype, party_field, date_field="posting_date"):
    """The list-tier field set. For every doctype except Journal Entry, Material Request, Stock
    Entry, and Quotation (the four doctypes forcing their own branch, below): the doctype's own
    party field spliced in (``customer`` for Sales Invoice, ``supplier`` for Purchase Invoice,
    ``party`` for Payment Entry, ``customer`` again for Sales Order, ``supplier`` again for
    Purchase Order, ``customer`` again for Delivery Note, ``supplier`` again for Purchase
    Receipt, ``supplier`` again for Supplier Quotation) alongside the original baked list,
    unchanged from before this breadth increment.
    **Journal Entry is its own branch, not a one-field patch** — confirmed
    absent from ``journal_entry.json``: no header-level party field (so nothing to splice), no
    ``status`` field (``docstatus`` is Journal Entry's only status signal), and no ``grand_total``
    (its balance lives in ``total_debit``/``total_credit`` instead). ``voucher_type`` rides along
    too — the field the JE-specific plan risk flags (``tools.py``) key off, useful context for
    anyone listing journal entries.

    **Material Request is a THIRD branch, not a fit for either of the other two** — confirmed from
    ``material_request.json`` (:data:`SUPPORTED_DOCTYPES`' own comment block has the full
    source-cited finding): ``status`` IS present (unlike Journal Entry, so ``docstatus`` alone
    doesn't stand in for it — ``status`` rides along too), but ``grand_total`` is confirmed ABSENT
    (unlike Sales/Purchase Order, so the generic branch's unconditional ``"grand_total"`` would ask
    the bench for a column that doesn't exist — the same unknown-column failure class as the
    date-field swap below). ``party_field`` is never spliced in either (Material Request's
    ``party_field`` is ``None`` — no header-level party column to splice, the same shape as
    Journal Entry, for different reasons — see :data:`SUPPORTED_DOCTYPES`). In its place:
    ``material_request_type`` (context — which of the six fulfillment-mode flavors this is, the
    same role ``voucher_type`` plays for Journal Entry) and ``per_ordered``/``per_received`` (the
    doctype's own completion-tracking Percent fields, the nearest thing it has to a stable summary
    metric, standing in for the ``grand_total`` slot).

    **Stock Entry is a FOURTH branch — the first to combine ALL THREE absences at once**
    (``party_field=None`` like JE/MR, ``status`` absent like JE, AND ``grand_total`` absent like
    JE/MR — no prior doctype dropped both ``status`` and ``grand_total`` together while ALSO having
    no party column to splice): confirmed from ``stock_entry.json`` (:data:`SUPPORTED_DOCTYPES`'
    own comment block has the full source-cited finding, 87 fields enumerated, neither ``status``
    nor ``grand_total`` present). In their place: ``purpose`` (context — the 13-way fulfillment
    fork, the same role ``voucher_type``/``material_request_type`` play above — Stock Entry's own
    precedent for this shape is Material Request's, per the campaign's stock-row playbook) and
    ``total_incoming_value``/``total_outgoing_value``/``value_difference`` (the doctype's own
    inventory-value summary fields, standing in for the ``grand_total`` slot exactly as
    ``per_ordered``/``per_received`` do for Material Request).

    **Quotation is a FIFTH branch — the first with ``party_field=None`` AND ``status``/
    ``grand_total`` BOTH present**, a combination none of the other three branches fit: Journal
    Entry drops both status and grand_total; Material Request keeps status but drops grand_total;
    Stock Entry drops both. Quotation keeps BOTH (confirmed present in ``quotation.json`` —
    :data:`SUPPORTED_DOCTYPES`' own comment block has the full source-cited finding) while ALSO
    carrying no header-level party column to splice — not because the party is absent (it is a
    REQUIRED pair, ``quotation_to``/``party_name``), but because it is a Dynamic Link pair, not a
    single static fieldname the ``party_field`` slot can name (the key decision — see
    :data:`SUPPORTED_DOCTYPES`). Feeding the generic branch's literal ``party_field`` here would
    splice ``None`` itself into the requested column list, the exact hazard Material Request's/
    Stock Entry's own ``party_field=None`` tests already guard against
    (``assertNotIn(None, fields)``). In its place: ``quotation_to`` and ``party_name`` BOTH ride
    along as list-tier CONTEXT columns (the doctype and the resolved record name, together — never
    ``party_name`` alone, which would disclose a bare record name with no doctype to interpret it
    by), the same role ``material_request_type``/``purpose`` play for Material Request/Stock Entry.

    ``date_field`` (Sales Order breadth increment, joined by Purchase Order, Material Request,
    Supplier Quotation, and now Quotation) — the doctype's own transaction-date fieldname
    (:data:`SUPPORTED_DOCTYPES`' ``"date_field"``), defaulting to ``"posting_date"`` (SI/PI/PE's
    own field, and Journal Entry's branch above still hardcodes it literally since JE's own
    ``date_field`` is also ``"posting_date"``). Sales Order, Purchase Order, Material Request,
    Supplier Quotation, and Quotation are the five callers that pass ``"transaction_date"`` here —
    confirmed absent from ``sales_order.json``, ``purchase_order.json``, ``material_request.json``,
    ``supplier_quotation.json``, and ``quotation.json`` alike; asking the bench's list endpoint for
    a column the doctype's schema doesn't carry is the same unknown-column class of failure
    ``get_period_locks``' own docstring already documents for a stale ``filters`` column, so this
    is a real column swap, not cosmetic. Stock Entry and Delivery Note/Purchase Receipt
    deliberately do NOT join this set — each carries a real ``posting_date`` field, so they keep
    the default.

    **Dunning (breadth, 2026-07-21) needs no branch here at all** — confirmed present in
    ``dunning.json``: ``customer`` (party_field), ``status``, and ``grand_total`` all three, plus a
    real ``posting_date`` field (never ``transaction_date``). It rides the generic return below
    unchanged, exactly like Sales Invoice/Sales Order — the only thing genuinely new about Dunning
    lives in ``tools.py``'s risk-flag layer (the native ``ledger_preview`` RPC being uncallable for
    this doctype), never in the list-tier field set.

    **Stock Reconciliation is a SIXTH branch — NOT a reuse of Stock Entry's own, even though both
    share the exact same absence shape** (``party_field=None`` AND ``status`` absent AND
    ``grand_total`` absent — the identical combination Stock Entry's own branch was built for).
    Confirmed from ``stock_reconciliation.json``'s complete 17-field enumeration
    (:data:`SUPPORTED_DOCTYPES`' own comment block has the full source-cited finding): no
    ``customer``/``supplier``/``party`` field, no ``status`` field, no ``grand_total`` field. The
    reason this cannot simply reuse Stock Entry's return list is that the SUBSTITUTE fields differ:
    Stock Entry stands ``purpose`` (13-way) plus ``total_incoming_value``/``total_outgoing_value``/
    ``value_difference`` in for the missing columns; Stock Reconciliation carries none of those
    three value fields at all (confirmed absent) — its own nearest equivalents are ``purpose`` (a
    two-way ``""``/``"Opening Stock"``/``"Stock Reconciliation"`` fork, not a 13-way one) and
    ``difference_amount`` (a SINGLE aggregate Currency field, the one summary metric this doctype's
    schema actually carries). Splicing Stock Entry's own three value-field names into a Stock
    Reconciliation list request would ask the bench for three columns that don't exist on this
    doctype's schema at all — the same unknown-column failure class ``get_period_locks``' own
    docstring already documents. ``date_field`` stays the default ``"posting_date"`` — a real field
    (confirmed present, ``reqd: 1``, default ``"Today"``), never ``transaction_date``.

    **Landed Cost Voucher is a SEVENTH branch — the SAME absence shape as Stock Reconciliation
    (``party_field=None`` AND ``status`` absent AND ``grand_total`` absent), and again NOT a reuse**
    for the identical reason Stock Reconciliation itself was not a reuse of Stock Entry's: the
    SUBSTITUTE fields differ. Confirmed from ``landed_cost_voucher.json``'s complete 15-field
    ``field_order`` (:data:`SUPPORTED_DOCTYPES`' own comment block has the full source-cited
    finding): no ``customer``/``supplier``/``party`` field, no ``status`` field, no ``grand_total``
    field. In their place: ``distribute_charges_based_on`` (Select, ``"Qty\nAmount\nDistribute
    Manually"`` — the context column, Stock Reconciliation's own ``purpose``-precedent role) and
    ``total_taxes_and_charges`` (a SINGLE aggregate Currency field — the total being allocated,
    Stock Reconciliation's own ``difference_amount``-precedent role). ``date_field`` stays the
    default ``"posting_date"`` — a real field (confirmed present, ``reqd: 1``, default
    ``"Today"``), never ``transaction_date``.

    **Request for Quotation is an EIGHTH branch — the SAME absence shape as Material Request
    (``party_field=None``, ``status`` present, ``grand_total`` absent), and again NOT a reuse**,
    for the identical reason Stock Reconciliation/Landed Cost Voucher's own branches were not
    reuses of their same-shape predecessors: the substitute/context fields differ. Material
    Request's own three substitutes (``material_request_type``, ``per_ordered``, ``per_received``)
    are confirmed ABSENT from ``request_for_quotation.json``'s full 384-field enumeration —
    splicing them here would ask the bench for columns that don't exist on this doctype's schema,
    the same unknown-column failure class every prior "not a reuse" branch in this function avoids.
    Unlike every other new branch this campaign has built, **RFQ has no natural analog to a
    fulfillment-type fork or a completion/aggregate field** (``schedule_date`` is a second date,
    not a mode fork; ``has_unit_price_items`` is a hidden, niche flag) — so this branch carries no
    substitute/context column at all, the first genuinely bare one. ``supplier`` is deliberately
    NOT spliced in either: the real supplier data lives in the required ``suppliers`` child table
    (one-to-many), and a bare scalar column would either not exist or arbitrarily pick one row —
    the dossier's own §9 reaches the same conclusion. ``date_field`` is ``"transaction_date"`` — a
    real field (confirmed present, ``reqd: 1``, default ``"Today"``), never ``posting_date``.

    **Blanket Order is a NINTH branch — the SAME absence shape as Stock Entry/Stock
    Reconciliation/Landed Cost Voucher (``party_field=None``, ``status`` absent, ``grand_total``
    absent), and again NOT a reuse of any of the three**, for the identical "substitutes differ"
    reason every prior same-shape branch in this function was its own. Confirmed from
    ``blanket_order.json``'s complete 18-field enumeration (:data:`SUPPORTED_DOCTYPES`' own comment
    block has the full source-cited finding): no ``status``, no ``grand_total``. In their place:
    ``blanket_order_type`` (Select, ``Selling``/``Purchasing`` — the type-context column, the same
    role ``purpose``/``distribute_charges_based_on`` play for Stock Entry/Stock
    Reconciliation/Landed Cost Voucher) and ``to_date`` (the validity window's end, paired with
    ``from_date`` which already rides as ``date_field`` — the nearest thing this schema has to a
    completion/aggregate metric, standing in for the missing ``grand_total`` slot). **Uniquely among
    the four ``party_field=None`` + both-absent branches, this one ALSO splices real party context**
    — ``customer`` AND ``supplier`` both, alongside ``blanket_order_type`` (closer to Quotation's own
    type-plus-resolved-party treatment than to Stock Entry/SR/LCV's partyless one): unlike those
    three doctypes (which have no party concept at all) or Request for Quotation (whose real
    supplier data lives only in a one-to-many child table this parameter's single-fieldname shape
    can't name), Blanket Order's party genuinely lives on two scalar header Link fields — just
    conditionally which one is populated, per :data:`SUPPORTED_DOCTYPES`' own party finding above.
    Splicing both (never just one, which would be wrong for half of all Blanket Orders) discloses
    the same "type tells you which field means something" shape Quotation's ``quotation_to``/
    ``party_name`` pair already established, adapted for two static fields instead of one Dynamic
    Link. ``date_field`` is ``"from_date"`` — confirmed present, ``reqd: 1``, no default; Blanket
    Order carries neither ``posting_date`` nor ``transaction_date``, the first doctype on neither
    established pattern.

    **Job Card is a TENTH branch — the SAME shape as Material Request and Request for Quotation
    (``party_field=None``, ``status`` present, ``grand_total`` absent), and again NOT a reuse of
    either.** Confirmed from ``job_card.json``'s complete 95-field enumeration
    (:data:`SUPPORTED_DOCTYPES`' own comment block has the full source-cited finding): ``status``
    present (8 options), ``grand_total`` absent, no party field at all. Material Request's own
    substitutes (``material_request_type``/``per_ordered``/``per_received``) and RFQ's own bare,
    substitute-free branch both don't fit: Job Card's genuinely different context columns are
    ``work_order`` (Link, ``reqd: 1``, ``in_list_view: 1`` — the parent manufacturing document),
    ``operation`` (Link, ``reqd: 1``, ``in_list_view: 1`` — which BOM operation this card covers),
    and ``for_quantity`` (Float, ``in_list_view: 1`` — the target quantity) — none of which exist on
    ``material_request.json`` or ``request_for_quotation.json`` at all. ``date_field`` stays the
    default ``"posting_date"`` — confirmed present (Date, default ``"Today"``), the plainest
    date-field landing since Dunning's; Job Card carries neither ``transaction_date`` nor
    ``from_date``.

    **BOM is an ELEVENTH branch — the first with NO DATE COLUMN AT ALL, and a shape no prior
    branch fits on two axes at once.** On the absence axis it matches Stock Entry/Stock
    Reconciliation/LCV/Blanket Order (``party_field=None``, ``status`` absent, ``grand_total``
    absent — confirmed from ``bom.json``'s complete 94-field enumeration), and per the standing
    "substitutes differ" discipline it is again not a reuse: BOM's own context/summary columns
    are its five real ``in_list_view`` fields — ``item`` (Link, ``reqd: 1`` — the product this
    recipe manufactures), ``is_active``/``is_default`` (the two ``allow_on_submit`` lifecycle
    Checks standing in for the missing ``status``), ``total_cost`` (read-only Currency valuation
    snapshot, standing in for the missing ``grand_total``), and ``has_variants`` — a combination
    that exists on no prior branch. On the DATE axis it is genuinely new: every other branch
    splices a real ``date_field`` column; BOM has none (``date_field=None``, the
    declared-dateless pin — see :data:`SUPPORTED_DOCTYPES`), so this branch simply carries NO
    date column — asking the bench's list endpoint for a date column that doesn't exist would be
    the same unknown-column failure class every prior "not a reuse" branch avoids. The
    ``date_field`` parameter this function receives is ``None`` for BOM and deliberately unused
    by the branch (the same ignore-the-parameter shape Journal Entry's own branch has always had
    for the date slot, hardcoding its own field — here there is simply nothing to hardcode).

    **Work Order is a TWELFTH branch — the Material Request/RFQ/Job Card absence shape
    (``party_field=None``, ``status`` present, ``grand_total`` absent), and again NOT a reuse:
    the substitutes differ.** Confirmed from ``work_order.json``'s complete 86-field enumeration
    (:data:`SUPPORTED_DOCTYPES`' own comment block has the full source-cited finding): its three
    real ``in_list_view`` fields are ``production_item`` (Link, ``reqd`` — the item being
    manufactured), ``qty`` (Float, ``reqd`` — the quantity to manufacture), and ``bom_no``
    (Link, ``reqd`` — the recipe) — none of which exist on any prior branch's schema —
    plus ``produced_qty`` (Float, ``read_only``, deliberately NOT list-view-flagged in the
    schema; it rides here as the progress column standing beside ``qty``, the same
    substitute-by-meaning role ``per_ordered``/``per_received`` play for Material Request — and
    a dossier correction: its §2 read this field as list-view-flagged, it is not). ``status``
    itself is also not ``in_list_view``-flagged (like Job Card's own) and rides by the standing
    convention every status-bearing doctype's list already follows. ``date_field`` is
    ``"planned_start_date"`` — spliced via the parameter as usual; the bench returns the raw
    Datetime for a list read, disclosed as-is (a display value here — only the closed-books
    chain needs the date-part projection, which lives in ``tools.py``'s ``_posting_date_of``,
    never in this field list).

    **Asset is a FOURTEENTH branch — status present (the widest, 13 options), ``grand_total``
    absent, no party column — and again NOT a reuse: the substitutes differ.** Confirmed from
    ``asset.json``'s complete 76-field enumeration (:data:`SUPPORTED_DOCTYPES`' own comment
    block has the full source-cited finding): the five real ``in_list_view`` fields are
    ``asset_name``/``asset_category``/``company``/``location``/``status``, and the two value
    columns standing in for the missing ``grand_total`` are ``net_purchase_amount`` (the input
    cost) and ``total_asset_cost`` (``read_only``, populated post-submit — pre-submit rows
    honestly show it empty, matching the bench's own form behavior). ``date_field`` is
    ``"available_for_use_date"`` — spliced via the parameter as usual, a plain Date.

    **Packing Slip is a FIFTEENTH branch — the FIRST to omit ``company`` from the requested
    columns entirely, on top of the now-familiar dateless omission.** Confirmed from
    ``packing_slip.json``'s complete 22-field enumeration (:data:`SUPPORTED_DOCTYPES`' own
    comment block has the full source-cited finding): no ``status``, no ``grand_total``, no
    ``company`` field of any kind, and (the second dateless doctype) no date field either. The
    three real ``in_list_view`` columns are ``delivery_note`` (the Draft DN this slip packs),
    ``from_case_no``, and ``to_case_no`` — none of which exist on any prior branch's schema, so
    this is not a reuse of Stock Entry's/Stock Reconciliation's/LCV's/Blanket Order's own
    ``party_field=None``-absence shape either, the same "substitutes differ" discipline every
    prior branch has followed. Both the ``party_field`` and ``date_field`` parameters this
    function receives are ``None`` for Packing Slip and deliberately unused by the branch (the
    same shape BOM's own branch already established for both slots at once); asking the bench
    for a ``company`` column that does not exist would be the same unknown-column failure class
    every prior "not a reuse" branch avoids — the reason this branch cannot fall back to the
    generic tail below, which always splices ``"company"`` in.

    **Cost Center Allocation is a SIXTEENTH branch — the party_field=None/status-absent/
    grand_total-absent shape shared with Stock Entry/Stock Reconciliation/LCV/Blanket Order/BOM
    (confirmed from ``cost_center_allocation.json``'s complete 7-field enumeration — the
    smallest schema this campaign has found), but NOT a reuse: this doctype carries no
    substitute/context column at all — not even a type-fork Select or a single aggregate Currency
    field, the same "no natural analog" shape Request for Quotation's own branch established,
    here even barer (RFQ still kept ``status``; this doctype has neither).** The two real
    ``in_list_view`` columns are ``main_cost_center`` (the routing destination) and ``valid_from``
    (spliced via the ``date_field`` parameter, exactly as every dated branch splices its own
    date column — NOT the dateless slot: this is a real, required Date field, the dossier
    correction the module docstring documents in full). Unlike BOM/Packing Slip, ``company`` DOES
    exist on this schema (confirmed, ``reqd: 1``) and is spliced in literally, the same as every
    ordinary branch.

    **Supplier Scorecard Period is a SEVENTEENTH branch — Wave 4's FIRST branch with a real
    ``party_field`` to splice, forced by the SAME status/grand_total-absence shape as Stock
    Entry/Stock Reconciliation/LCV/Blanket Order/BOM/Cost Center Allocation, but not a reuse of
    any of them: no prior branch spliced a real ``party_field`` AND a real ``date_field`` while
    ALSO omitting ``company``.** Confirmed from ``supplier_scorecard_period.json``'s complete
    12-field enumeration (:data:`SUPPORTED_DOCTYPES`' own comment block has the full source-cited
    finding): ``supplier`` (the real ``party_field``, spliced literally, unlike every
    ``party_field=None`` branch above), ``total_score`` (the SCORE standing in for the missing
    ``grand_total`` — never a monetary substitute, unlike Stock Reconciliation's
    ``difference_amount``/LCV's ``total_taxes_and_charges``), and ``start_date`` (spliced via the
    ``date_field`` parameter, a REAL required Date field — never the dateless slot). ``company``
    is confirmed absent from the schema entirely (the SECOND such absence after Packing Slip's
    own branch) and is never spliced — asking the bench for a column that does not exist would be
    the same unknown-column failure class every prior "not a reuse" branch avoids.

    **Quality Inspection is an EIGHTEENTH branch — the FIRST Dynamic Link pair since Quotation's
    own FIFTH branch, forcing the SAME "both context columns ride together" precedent.** Confirmed
    from ``quality_inspection.json``'s complete 30-field enumeration (:data:`SUPPORTED_DOCTYPES`'
    own comment block has the full source-cited finding): ``reference_type``/``reference_name``
    (the Dynamic Link pair — ``reference_name`` alone carries the schema's own ``in_list_view: 1``
    flag, but per the Quotation precedent both ride the list tier together, since a Dynamic Link's
    type-half is meaningless without its name-half) plus ``item_code`` and ``inspection_type``
    (both confirmed ``in_list_view: 1`` in the same 30-field enumeration — the item under
    inspection and the inspection's own mode). Unlike Packing Slip/Supplier Scorecard Period,
    ``company`` DOES exist on this schema (confirmed present, though not ``reqd``) and is spliced
    in literally, the same as every ordinary branch — this is not a companyless row.

    **Installation Note is a NINETEENTH branch — the FIRST to combine a REAL spliced
    ``party_field`` with status present, ``company`` present, a real ``date_field``, AND a
    genuinely absent ``grand_total`` with no substitute of any kind.** Confirmed from
    ``installation_note.json``'s complete 23-field enumeration (:data:`SUPPORTED_DOCTYPES`' own
    comment block has the full source-cited finding): ``customer`` (the real party_field, spliced
    via the parameter as usual), ``status`` present (Draft/Submitted/Cancelled), ``company``
    present (reqd), ``grand_total`` absent. The ONLY additional ``in_list_view``-flagged column
    this schema carries is ``remarks`` (Small Text) — no aggregate Currency/Percent field, no
    type-fork Select, exists anywhere to stand in for the missing total (the same "no natural
    analog" shape Request for Quotation's/Cost Center Allocation's own branches established, here
    for the first time riding alongside a real party column rather than in its place).
    ``date_field="inst_date"`` — a real, required Date field, spliced via the parameter as usual —
    the NINTH distinct date-fieldname pattern this campaign has found.

    **Shipment is a TWENTIETH branch — the FIRST with TWO independent dynamic-selector pairs,
    doubling the Quotation/Quality Inspection "type and resolved value ride together" precedent.**
    Confirmed from ``shipment.json``'s complete 56-field enumeration (:data:`SUPPORTED_DOCTYPES`'
    own comment block has the full source-cited finding): ``party_field=None`` (never spliced —
    the branch hardcodes both pairs literally, the same not-a-single-fieldname shape Quotation's/
    Quality Inspection's own branches already established), ``status`` present, ``grand_total``
    absent, ``company`` absent (the THIRD companyless branch after Packing Slip/Supplier Scorecard
    Period). In place of a single party column: ``pickup_from_type`` alongside ``pickup`` (the
    schema's own ``in_list_view: 1`` resolved-value mirror for the pickup side) and
    ``delivery_to_type`` alongside ``delivery_to`` (the same mirror for the delivery side) — never
    one half of a pair without its other, applied twice over for the first time this campaign has
    found. ``value_of_goods`` (Currency, ``reqd: 1``) stands in for the missing ``grand_total``.
    ``date_field="pickup_date"`` — a real, required Date field, spliced via the parameter as
    usual — the TENTH distinct date-fieldname pattern this campaign has found.

    **Sales Forecast is a TWENTY-FIRST branch — the Material Request/RFQ/Job Card absence shape
    (``party_field=None``, ``status`` present, ``grand_total`` absent) yet AGAIN, and again NOT a
    reuse, though this time it genuinely CONVERGES.** Confirmed from ``sales_forecast.json``'s
    complete 20-field enumeration (:data:`SUPPORTED_DOCTYPES`' own comment block has the full
    source-cited finding): no natural analog for the missing ``grand_total`` exists at all —
    ``frequency``, ``demand_number``, ``parent_warehouse``, and ``from_date`` are all real fields
    but none carries ``in_list_view: 1`` — the same "no substitute of any kind" shape Request for
    Quotation's own EIGHTH branch already established. Because Sales Forecast ALSO carries a real,
    ``in_list_view`` ``company`` and exactly one ``in_list_view`` date column (``posting_date``,
    spliced via the parameter), the requested-column list this branch returns is byte-identical to
    RFQ's own (``["name", "status", "docstatus", "company", date_field, "modified"]``) — the first
    time in this campaign two independently-verified branches converge on the same output, still
    forced as its own explicit conditional per the standing one-branch-per-doctype discipline,
    never delegated or aliased.

    **Project Update is a TWENTY-SECOND branch — the NARROWEST this campaign has found: no status,
    no grand_total, no company, and exactly ONE real in_list_view field.** Confirmed from
    ``project_update.json``'s complete 9-field enumeration (:data:`SUPPORTED_DOCTYPES`' own comment
    block has the full source-cited finding): ``project`` is the ONLY field carrying
    ``in_list_view: 1`` anywhere in the schema. Unlike every prior "both absent" branch (Stock
    Entry/Stock Reconciliation/LCV/Blanket Order/BOM), this one has no completion/aggregate
    substitute of any kind to stand in for the missing ``grand_total`` — the real ``sent`` Check
    field exists but is deliberately NOT spliced in (not ``in_list_view``-flagged, and tracks an
    unrelated reminder-email side channel, never a governance-relevant state) — the same
    discipline RFQ's own bare branch established for a missing substitute. ``company`` is also
    omitted entirely (confirmed absent from the schema, the fourth companyless doctype), the same
    omission Packing Slip's/Supplier Scorecard Period's/Shipment's own branches already made.
    ``date_field`` splices in as ``"date"`` — a real field (unlike Packing Slip's own dateless
    branch), confirmed present via the parameter as usual.

    **Maintenance Visit is a TWENTY-THIRD branch — the SAME categorical shape Installation Note
    established (real spliced ``party_field``, ``status`` present, ``company`` present,
    ``grand_total`` absent), and again NOT a reuse: TWO named substitutes ride here, not
    Installation Note's one.** Confirmed from ``maintenance_visit.json``'s complete 32-field
    enumeration (:data:`SUPPORTED_DOCTYPES`' own comment block has the full source-cited
    finding): ``customer`` (the real ``party_field``, spliced via the parameter as usual),
    ``status`` present (Draft/Cancelled/Submitted), ``company`` present (``reqd``), ``grand_total``
    absent. The TWO ``in_list_view``-flagged columns this schema carries are
    ``completion_status`` (Select — Partially/Fully Completed) and ``maintenance_type`` (Select —
    Scheduled/Unscheduled/Breakdown), both orthogonal to the submit-lifecycle ``status`` column
    itself (which is not ``in_list_view``-flagged, riding by the same standing convention every
    status-bearing doctype's list already follows). ``date_field="mntc_date"`` — a real, required
    Date field with a schema default of ``"Today"``, spliced via the parameter as usual — the
    TWELFTH distinct date-fieldname pattern this campaign has found.

    **Maintenance Schedule is a TWENTY-FOURTH branch — the SAME categorical shape Installation
    Note established (real spliced ``party_field``, ``status`` present, ``company`` present,
    ``grand_total`` absent), and again NOT a reuse: its own single substitute is ``customer_name``,
    not Installation Note's ``remarks``.** Confirmed from ``maintenance_schedule.json``'s complete
    24-field enumeration (:data:`SUPPORTED_DOCTYPES`' own comment block has the full source-cited
    finding): ``customer`` (the real ``party_field`` — genuinely optional this time, the first
    ``reqd``-less party field this campaign has spliced, decided on the field's reality rather than
    its ``reqd`` flag), ``status`` present (Draft/Submitted/Cancelled), ``company`` present
    (``reqd``), ``grand_total`` absent. The ONE ``in_list_view``-flagged column this schema carries
    is ``customer_name`` (Data, denormalized from ``customer``) — spliced alongside the real
    ``party_field`` itself, never in its place, the same "both ride together" discipline
    Installation Note's own ``remarks`` branch already established. ``date_field="transaction_date"``
    — REJOINS the standing Sales Order/Purchase Order/Material Request/Supplier Quotation/
    Quotation/Request for Quotation set (see the module docstring) rather than forcing a new
    pattern.

    **Asset Maintenance Log is a TWENTY-FIFTH branch — the FIRST whose lifecycle-adjacent Select
    is not literally named ``"status"``, forcing the literal fieldname to be spliced instead of
    the standing hardcoded string.** Confirmed from ``asset_maintenance_log.json``'s complete
    23-field enumeration (:data:`SUPPORTED_DOCTYPES`' own comment block has the full source-cited
    finding): ``party_field=None`` (never spliced — no party concept at all), ``maintenance_status``
    present under its own real fieldname (never ``"status"``, which does not exist on this
    schema), ``grand_total`` absent with no substitute of any kind (the Cost Center
    Allocation/Project Update "no natural analog" shape). The only TWO ``in_list_view`` columns
    this schema carries are ``due_date`` (the read-only scheduling reference) and
    ``completion_date`` (spliced via the ``date_field`` parameter, the doctype's own operational
    date) — ``due_date`` rides alongside it literally, the same "both real Date columns ride
    together" shape Blanket Order's own ``from_date``/``to_date`` pair already established.
    ``company`` is confirmed absent from the schema entirely (the fifth companyless doctype) and
    is never spliced.

    **Bank Guarantee is a TWENTY-SIXTH branch — the Blanket Order party-splice mechanism (a
    genuine DUAL CONDITIONAL party pair spliced as list-tier context) forced together with a
    companyless, docstatus-only absence shape no prior branch combines.** Confirmed from
    ``bank_guarantee.json``'s complete 24-field enumeration (:data:`SUPPORTED_DOCTYPES`' own
    comment block has the full source-cited finding): no ``status`` field (``docstatus`` only),
    ``grand_total`` confirmed absent, ``company`` confirmed absent (the SIXTH companyless doctype
    after Packing Slip/Supplier Scorecard Period/Shipment/Project Update/Asset Maintenance Log).
    Unlike Blanket Order (which carries a real, required ``company``), this branch cannot splice
    ``company`` at all — the same unknown-column failure class every prior companyless branch
    avoids. In place of the missing ``grand_total``: ``amount`` (the sole real
    ``in_list_view``-flagged Currency field, ``reqd: 1``) — a single aggregate, the Stock
    Reconciliation ``difference_amount``/Landed Cost Voucher ``total_taxes_and_charges``
    precedent, never Blanket Order's own paired-date substitute (Bank Guarantee's own ``end_date``
    is not ``in_list_view``-flagged and is client-JS-derived only besides — deliberately NOT
    spliced). ``bg_type`` (the Select forking which of ``customer``/``supplier`` is meaningful for
    a given row) rides alongside both real party Link fields — the same "type tells you which
    field means something" role ``blanket_order_type`` plays for Blanket Order's own identical
    party-splice mechanism. ``date_field="start_date"`` — a real, required Date field, spliced via
    the parameter as usual, REJOINING Supplier Scorecard Period's own SEVENTH date-fieldname
    pattern (see the module docstring) rather than forcing a new one.

    **Asset Movement is a TWENTY-SEVENTH branch — the MINIMAL shape this campaign has found: no
    party, no status, no aggregate of any kind, not even a stand-in substitute.** Confirmed from
    ``asset_movement.json``'s complete 7-real-field enumeration (:data:`SUPPORTED_DOCTYPES`' own
    comment block has the full source-cited finding): the THREE ``in_list_view``-flagged fields
    are ``company`` (Link, ``reqd``), ``purpose`` (Select, ``reqd`` — the four-way Issue/Receipt/
    Transfer/Transfer and Issue router, the Stock Entry precedent for splicing a purpose field
    onto the list tier), and ``transaction_date`` itself (spliced via the parameter as usual — the
    SECOND Datetime-typed ``date_field`` in this campaign; the list read keeps the raw Datetime, a
    display value, exactly like Work Order's own ``planned_start_date`` — only the closed-books
    chain projects it). No ``status`` (``docstatus`` only), no ``grand_total``, and — unlike every
    prior no-aggregate branch (Stock Reconciliation's ``difference_amount``, Bank Guarantee's
    ``amount``, Blanket Order's paired ``to_date``) — no substitute column of any kind: the
    complete 7-real-field enumeration carries no other real, non-layout field left to splice.

    **Delivery Trip is a TWENTY-EIGHTH branch — a real ``status`` (unlike Asset Movement) with NO
    aggregate of any kind (like Asset Movement).** Confirmed from ``delivery_trip.json``'s complete
    15-real-field enumeration (:data:`SUPPORTED_DOCTYPES`' own comment block has the full
    source-cited finding): the two ``in_list_view``-flagged fields are ``driver_name`` (Data,
    ``read_only``) and ``departure_time`` itself (spliced via the ``date_field`` parameter — the
    THIRD Datetime-typed ``date_field`` in this campaign; the list read keeps the raw Datetime,
    exactly like Work Order's and Asset Movement's own date columns — only the closed-books chain
    projects it). ``status`` is included even though the SCHEMA never flags it ``in_list_view`` —
    ``delivery_trip_list.js`` (ERPNext's own list-view controller) explicitly declares
    ``add_fields: ["status"]`` plus a full color-coded ``get_indicator`` mapping keyed on it, a
    source beyond the dossier's own scope confirming ``status`` is a genuine ERPNext-authored list
    column. No ``grand_total`` and no substitute of any kind — the same "nothing left to splice"
    shape Asset Movement established, this time alongside a real status rather than instead of
    one.

    **Asset Value Adjustment is a TWENTY-NINTH branch — the SAME absence shape as Stock Entry/
    Stock Reconciliation/LCV/Blanket Order/BOM/Cost Center Allocation (``party_field=None``,
    ``status`` absent, ``grand_total`` absent), and again NOT a reuse: THREE real value/reference
    columns ride here, none of them a single aggregate.** Confirmed from
    ``asset_value_adjustment.json``'s complete 12-real-field enumeration
    (:data:`SUPPORTED_DOCTYPES`' own comment block has the full source-cited finding): the three
    ``in_list_view``-flagged fields are ``finance_book``/``current_asset_value``/
    ``new_asset_value`` — none a single monetary total the way Stock Reconciliation's
    ``difference_amount``/LCV's ``total_taxes_and_charges``/Bank Guarantee's ``amount`` are.
    **``asset`` itself — the field naming WHICH asset this document concerns — carries NO
    ``in_list_view`` flag on this schema** (confirmed absent) and is honestly NOT spliced, the
    same "splice only what the schema itself flags" discipline every prior branch has followed,
    even where the omission reads as counter-intuitive. ``company`` DOES exist on this schema
    (confirmed present, though not ``reqd`` — see :data:`SUPPORTED_DOCTYPES`' own comment block)
    and is spliced in literally the same as every ordinary branch — its reality, not its ``reqd``
    flag, decides the splice, the same discipline Maintenance Schedule's own optional ``customer``
    splice already established for a party field. ``date_field="date"`` — a real, required Date
    field, REJOINS Project Update's own eleventh date pattern rather than forcing a new one.

    **Payment Order is a THIRTIETH branch — real status absent, no aggregate of any kind (the
    Asset Movement shape again), but a genuinely conditional party field spliced literally by
    name for the FIRST time on a SINGLE (not dual) fieldname.** Confirmed from
    ``payment_order.json``'s complete 10-real-field enumeration (:data:`SUPPORTED_DOCTYPES`' own
    comment block has the full source-cited finding): the three ``in_list_view``-flagged fields
    are ``party`` (conditional Link -> Supplier — real and ``in_list_view: 1`` on the schema
    despite ``party_field=None``, spliced by its own literal name the same way Blanket
    Order's/Bank Guarantee's own conditional party PAIRS are — just one field here instead of
    two), ``posting_date`` (the ``date_field``), and ``company_bank`` (Link -> Bank, fetched from
    ``company_bank_account``). ``payment_order_type`` rides alongside as the CONTEXT/router
    column even though the schema never flags it ``in_list_view`` — the same "type tells you
    which field means something" role ``blanket_order_type``/``bg_type`` play for Blanket
    Order's/Bank Guarantee's own party pairs (neither of THOSE fields is ``in_list_view``-flagged
    either — splicing a router column has never depended on that flag in this table), here
    explaining why ``party`` reads blank for every Payment-Entry-typed row. ``company`` DOES
    exist (confirmed, ``reqd: 1``) and is spliced in literally, the same as every ordinary
    branch. No substitute for the missing ``grand_total`` — the Asset Movement "nothing left to
    splice" shape, confirmed by the complete 10-field enumeration. ``date_field`` stays the
    default ``"posting_date"`` — a real, present field (default ``"Today"``), REJOINING the
    largest pattern in this table rather than forcing a new one.

    **Share Transfer is a THIRTY-FIRST branch — the Asset Movement "nothing left to splice"
    absence shape (``status``/``grand_total`` both confirmed absent) with ``amount`` as the sole
    substitute, spliced alongside a dual conditional party PAIR for the first time on a
    THREE-state (not two-state) router.** Confirmed from ``share_transfer.json``'s complete
    26-field/17-data-field enumeration (:data:`SUPPORTED_DOCTYPES`' own comment block has the
    full source-cited finding): the only ``in_list_view``-flagged field is ``transfer_type``
    itself — unlike Blanket Order's/Bank Guarantee's own party pairs, NEITHER
    ``from_shareholder`` nor ``to_shareholder`` carries ``in_list_view`` on this schema, yet both
    are spliced literally by name anyway, the same "splice a conditional pair WHOLE, never one
    alone" discipline this table already follows for Blanket Order/Bank Guarantee (whose own
    router columns, ``blanket_order_type``/``bg_type``, aren't ``in_list_view``-flagged either —
    splicing has never depended on that flag for a router or a conditional pair). ``transfer_type``
    rides as the router explaining which of the pair is populated for a given row — EXCEPT for a
    Transfer-type row, where both are populated simultaneously (the genuinely new sub-variant
    named in :data:`SUPPORTED_DOCTYPES`' own comment block: Blanket Order's/Bank Guarantee's own
    pairs are mutually exclusive, never both, never neither; Share Transfer's own pair is not).
    ``company`` DOES exist (confirmed, ``reqd: 1``) and is spliced in literally, the same as every
    ordinary branch — NOT companyless. ``amount`` (Currency, ``read_only: 1``) is the confirmed
    stand-in for the missing ``grand_total``, self-computed at ``share_transfer.py:198``.
    ``date_field`` stays the default parameter value ``"date"`` (:data:`SUPPORTED_DOCTYPES`' own
    pin) — REJOINING the standing ``date_field="date"`` pattern as its third member, never a new
    one.

    **BOM Creator is a THIRTY-SECOND branch — status present, no grand_total, THE THIRD DATELESS
    doctype (after BOM/Packing Slip), and the first dateless branch that still splices a real
    company.** Confirmed from ``bom_creator.json``'s complete 40-field enumeration
    (:data:`SUPPORTED_DOCTYPES`' own comment block has the full source-cited finding): the three
    real ``in_list_view`` columns are ``item_code`` (Link -> Item, the finished good), ``currency``
    (Link -> Currency), and ``raw_material_cost`` (Currency, read_only) — the confirmed stand-in
    for the missing ``grand_total``. ``party_field`` is ``None`` and unused (no party concept at
    all). ``date_field`` is also ``None`` and unused — the SAME "both slots blank at once" shape
    BOM's/Packing Slip's own branches established, never asking the bench for a date column that
    does not exist. Unlike Packing Slip (which also omits ``company``), BOM Creator's ``company``
    IS present and required, so it is spliced in literally, the same as every ordinary branch.

    **Budget is a THIRTY-THIRD branch — the barest shape this campaign has found alongside a real
    dual conditional pair: no status, no grand_total, no substitute for either, AND no date
    spliced at all despite carrying a real, non-blank date_field.** Confirmed from
    ``budget.json``'s complete 40-field enumeration (:data:`SUPPORTED_DOCTYPES`' own comment
    block has the full source-cited finding): ``in_list_view`` is exactly ``["budget_against",
    "company", "account"]`` — neither ``cost_center``/``project`` (the conditional pair) nor
    ``budget_amount``/``budget_distribution_total`` (the only monetary fields) carry the flag.
    The conditional pair is spliced WHOLE regardless (``cost_center``, ``project``) — the same
    "splice a router's pair together, never one alone" discipline Blanket Order's/Bank
    Guarantee's/Share Transfer's own pairs already established, which has never depended on the
    ``in_list_view`` flag for a router or a pair. ``budget_amount`` is deliberately NOT spliced as
    a ``grand_total`` substitute (unlike Bank Guarantee's ``amount``/Stock Reconciliation's
    ``difference_amount``) — it carries no ``in_list_view`` flag, and an ordinary substitute
    column's splice DOES depend on that flag (the Asset Value Adjustment ``asset`` precedent: "the
    same 'splice only what the schema itself flags' discipline"). ``company`` DOES exist
    (confirmed, ``reqd: 1``) and is spliced in literally, the same as every ordinary branch. THE
    GENUINELY NEW WRINKLE: ``date_field`` (``"budget_start_date"``) is NOT spliced into this
    branch's return at all — the first branch to receive a real, present ``date_field`` parameter
    and deliberately not use it, because both ends of Budget's own validity window
    (``budget_start_date``/``budget_end_date``) are ``hidden: 1`` on the schema (see the module
    docstring's own "fifth wrinkle"); unlike Blanket Order's ``to_date``/Supplier Scorecard
    Period's ``end_date`` (each the visible other half of a window pair, spliced as list-tier
    context), Budget has no visible date of either kind to show. This is the SAME absence shape
    as Cost Center Allocation (``party_field=None``/status-absent/grand_total-absent, no
    substitute), but NOT a reuse: Cost Center Allocation splices its own real ``valid_from`` via
    the ``date_field`` parameter and carries no conditional pair; Budget splices a conditional
    pair instead and no date at all — the "substitutes differ" rule, confirmed by direct
    ``in_list_view`` re-enumeration rather than assumed from the addendum.

    **Timesheet forces a NEW branch — party PRESENT (unlike Budget) + status PRESENT +
    grand_total ABSENT with no substitute, plus ONE genuinely additional in_list_view-flagged
    column (``per_billed``) that is neither the party nor the date.** (Ordinal note, checked
    rather than inherited: a direct count of ``if doctype ==`` cases in this function shows 32
    special branches after this landing, +1 shared default = 33 total by that method — but
    Budget's own commit narrated itself as "the 33rd," while the same direct-count method put
    Budget at only the 31st special branch (32nd counting the default); a pre-existing +2 drift in
    the running narrative tally, not introduced by this landing and not silently corrected further
    here — flagged for the supervisor rather than guessed at.) Confirmed from
    ``timesheet.json``'s complete 37-field enumeration (:data:`SUPPORTED_DOCTYPES`' own comment
    block has the full source-cited finding): ``in_list_view`` is exactly ``{start_date,
    per_billed}`` — neither ``total_billable_amount`` nor ``total_billed_amount`` (the only
    monetary fields) carries the flag, so neither substitutes for ``grand_total`` (the same
    "an ordinary substitute's splice depends on the flag" discipline Budget's own landing
    reaffirmed). ``per_billed`` rides along as its own additional list-tier column instead — the
    schema's own flagged completion metric, the same role Material Request's ``per_ordered``/
    ``per_received`` and Supplier Scorecard Period's ``total_score`` play, but NOT a reuse of
    either shape (this branch keeps a real party field, which none of those three carry).
    ``status``/``company``/``date_field`` (``"start_date"``, itself ``in_list_view``-flagged, so
    it IS spliced literally — unlike Budget's own hidden pair) are all spliced exactly like every
    ordinary branch; ``company`` rides even though present-but-optional, the same AVA precedent.

    **Contract forces the 33rd branch (direct count: 32 special branches existed before this
    landing, confirmed by counting every ``if doctype ==`` line in this function — Contract is
    the 33rd, +1 shared default = 34 total).** party_field=None (a Dynamic Link pair —
    ``party_type``/``party_name`` splice WHOLE, the Quotation/Quality Inspection convention,
    never one alone). TWO Select status-type columns simultaneously (``status`` +
    ``fulfilment_status``) — a combination no prior branch carries. No ``company`` (companyless —
    the slot is simply omitted, not spliced as an empty string). ``document_name`` (the schema's
    own Dynamic Link, showing what document this Contract stems from — Quotation/Project/Sales
    Order/etc.) rides as context, the Quality Inspection ``reference_type``/``reference_name``
    role. ``contract_terms`` (Text Editor, also ``in_list_view: 1`` on the real schema) is
    DELIBERATELY NOT spliced here — no branch in this function has ever spliced a Text Editor
    field, and a full contract body is not a useful list column; excluded on that established
    convention, not an oversight (the schema's own 5 ``in_list_view`` fields are not a mandate to
    literally reproduce all 5, the same "curated columns, not a mirror" reading every existing
    branch already takes).

    Pick List forces the 34th branch (direct count: 33 special branches existed before this
    landing, re-counted rather than inherited -- matches Contract's own "33rd" framing). party
    PRESENT ("customer", spliced via the parameter as usual -- a plain, always-real header Link,
    never gated on its own depends_on UI directive, unlike Payment Order's/Blanket Order's own
    party_field=None conditional-party rows). status PRESENT and spliced despite being BOTH
    hidden:1 AND un-flagged in_list_view -- confirmed real and list-tier-worthy by
    pick_list_list.js's own get_indicator reading doc.status directly for its color-coded
    indicator (the Delivery Trip precedent -- "a schema flag isn't the only signal" -- taken one
    step further here: Delivery Trip's own status is un-flagged but NOT hidden, and Contract's own
    status is hidden but ALSO in_list_view-flagged; Pick List is the first branch combining both
    restrictions on the SAME field). grand_total confirmed ABSENT with NO substitute (in_list_view
    is exactly {company, customer} -- no aggregate field anywhere among the 35). purpose (Select,
    3-way, NOT in_list_view-flagged) rides as the router/context column explaining why customer
    reads blank on any non-"Delivery" row -- the same "splice a router even though the schema
    never flags it" discipline Payment Order's own payment_order_type/Blanket Order's own
    blanket_order_type already established. date_field is dropped entirely -- the
    declared-dateless None (the FOURTH such member, joining BOM's/Packing Slip's/BOM Creator's own
    branches), no column to splice. company (reqd, in_list_view:1) splices in literally like every
    ordinary branch.

    Asset Capitalization forces the 37th branch (direct count: 36 special branches existed before
    this landing, matching Invoice Discounting's own "36th" framing exactly). party_field=None (no
    party concept at all -- confirmed absent from the full 39-field enumeration). NO status field
    of any kind (unlike Invoice Discounting's real, if unflagged, status column) -- the closest
    prior shape is Asset Value Adjustment's own branch (no status, a value-field substitute
    spliced). grand_total confirmed ABSENT; total_value (the read-only aggregate,
    asset_capitalization.py:366) rides as the substitute -- spliced despite carrying no
    in_list_view flag of its own (in_list_view is confirmed EXACTLY {posting_date}), the same
    judgment call Invoice Discounting's own total_amount made one landing earlier, followed here
    for consistency with the freshest precedent rather than re-litigated against Budget's own
    stricter reading. date_field="posting_date" -- rejoins the large existing set unchanged, no
    new pattern. company (reqd, though not itself in_list_view-flagged) splices in literally like
    every ordinary branch -- the same "party_field=None doctypes still show company" shape
    Asset Value Adjustment/Payment Order/Share Transfer/BOM Creator/Budget already established."""
    if doctype == JOURNAL_ENTRY:
        return ["name", "docstatus", "voucher_type", "company", "posting_date",
                "total_debit", "total_credit", "modified"]
    if doctype == MATERIAL_REQUEST:
        return ["name", "status", "docstatus", "material_request_type", "company", date_field,
                "per_ordered", "per_received", "modified"]
    if doctype == STOCK_ENTRY:
        return ["name", "docstatus", "purpose", "company", date_field, "total_incoming_value",
                "total_outgoing_value", "value_difference", "modified"]
    if doctype == QUOTATION:
        return ["name", "status", "docstatus", "quotation_to", "party_name", "company",
                date_field, "grand_total", "modified"]
    if doctype == STOCK_RECONCILIATION:
        return ["name", "docstatus", "purpose", "company", date_field, "difference_amount",
                "modified"]
    if doctype == LANDED_COST_VOUCHER:
        return ["name", "docstatus", "distribute_charges_based_on", "company", date_field,
                "total_taxes_and_charges", "modified"]
    if doctype == REQUEST_FOR_QUOTATION:
        return ["name", "status", "docstatus", "company", date_field, "modified"]
    if doctype == BLANKET_ORDER:
        return ["name", "docstatus", "blanket_order_type", "customer", "supplier", "company",
                date_field, "to_date", "modified"]
    if doctype == JOB_CARD:
        return ["name", "status", "docstatus", "work_order", "operation", "for_quantity",
                "company", date_field, "modified"]
    if doctype == BOM:
        return ["name", "docstatus", "item", "is_active", "is_default", "total_cost",
                "has_variants", "company", "modified"]
    if doctype == WORK_ORDER:
        return ["name", "status", "docstatus", "production_item", "qty", "produced_qty",
                "bom_no", "company", date_field, "modified"]
    if doctype == ASSET:
        return ["name", "status", "docstatus", "asset_name", "asset_category", "location",
                "net_purchase_amount", "total_asset_cost", "company", date_field, "modified"]
    if doctype == PACKING_SLIP:
        return ["name", "docstatus", "delivery_note", "from_case_no", "to_case_no", "modified"]
    if doctype == COST_CENTER_ALLOCATION:
        return ["name", "docstatus", "main_cost_center", "company", date_field, "modified"]
    if doctype == SUPPLIER_SCORECARD_PERIOD:
        return ["name", "docstatus", party_field, "total_score", date_field, "modified"]
    if doctype == QUALITY_INSPECTION:
        return ["name", "status", "docstatus", "reference_type", "reference_name", "item_code",
                "inspection_type", "company", date_field, "modified"]
    if doctype == INSTALLATION_NOTE:
        return ["name", "status", "docstatus", party_field, "remarks", "company", date_field,
                "modified"]
    if doctype == SHIPMENT:
        return ["name", "status", "docstatus", "pickup_from_type", "pickup", "delivery_to_type",
                "delivery_to", date_field, "value_of_goods", "modified"]
    if doctype == SALES_FORECAST:
        return ["name", "status", "docstatus", "company", date_field, "modified"]
    if doctype == PROJECT_UPDATE:
        return ["name", "docstatus", "project", date_field, "modified"]
    if doctype == MAINTENANCE_VISIT:
        return ["name", "status", "docstatus", party_field, "completion_status",
                "maintenance_type", "company", date_field, "modified"]
    if doctype == MAINTENANCE_SCHEDULE:
        return ["name", "status", "docstatus", party_field, "customer_name", "company",
                date_field, "modified"]
    if doctype == ASSET_MAINTENANCE_LOG:
        return ["name", "maintenance_status", "docstatus", date_field, "due_date", "modified"]
    if doctype == BANK_GUARANTEE:
        return ["name", "docstatus", "bg_type", "customer", "supplier", date_field, "amount",
                "modified"]
    if doctype == ASSET_MOVEMENT:
        return ["name", "docstatus", "purpose", "company", date_field, "modified"]
    if doctype == DELIVERY_TRIP:
        return ["name", "status", "docstatus", "driver_name", "company", date_field, "modified"]
    if doctype == ASSET_VALUE_ADJUSTMENT:
        return ["name", "docstatus", "finance_book", "current_asset_value", "new_asset_value",
                "company", date_field, "modified"]
    if doctype == PAYMENT_ORDER:
        return ["name", "docstatus", "payment_order_type", "party", "company", date_field,
                "company_bank", "modified"]
    if doctype == SHARE_TRANSFER:
        return ["name", "docstatus", "transfer_type", "from_shareholder", "to_shareholder",
                "company", date_field, "amount", "modified"]
    if doctype == BOM_CREATOR:
        return ["name", "status", "docstatus", "item_code", "currency", "raw_material_cost",
                "company", "modified"]
    if doctype == BUDGET:
        return ["name", "docstatus", "budget_against", "cost_center", "project", "company",
                "account", "modified"]
    if doctype == TIMESHEET:
        return ["name", "status", "docstatus", party_field, "per_billed", "company", date_field,
                "modified"]
    if doctype == CONTRACT:
        return ["name", "status", "fulfilment_status", "docstatus", "party_type", "party_name",
                date_field, "document_name", "modified"]
    if doctype == PICK_LIST:
        return ["name", "status", "docstatus", "purpose", party_field, "company", "modified"]
    if doctype == ASSET_REPAIR:
        return ["name", "repair_status", "docstatus", "asset", "downtime", "total_repair_cost",
                "company", date_field, "modified"]
    if doctype == INVOICE_DISCOUNTING:
        return ["name", "status", "docstatus", date_field, "company", "total_amount", "modified"]
    if doctype == ASSET_CAPITALIZATION:
        return ["name", "docstatus", date_field, "company", "total_value", "modified"]
    if doctype == PRODUCTION_PLAN:
        return ["name", "status", "docstatus", "company", "get_items_from", date_field,
                "item_code", "customer", "modified"]
    if doctype == SUBCONTRACTING_ORDER:
        # Subcontracting Order breadth (2026-07-21): the THIRTY-NINTH special branch by direct
        # count. party_field="supplier" splices literally (a plain, always-real GL party) — the
        # generic branch's own shape. The ONE genuine divergence: the grand-total-equivalent
        # field is named "total", not "grand_total" (json.load-confirmed, depends_on
        # purchase_order) — the generic branch's literal "grand_total" would ask a real bench for
        # a column that doesn't exist on this doctype, the same unknown-column hazard Material
        # Request's own branch guards against. per_received (the doctype's own completion-percent
        # field, in_list_view:1 alongside transaction_date) rides along too, the Material
        # Request per_ordered/per_received precedent for a completion-tracking column.
        return ["name", "status", "docstatus", party_field, "company", date_field,
                "total", "per_received", "modified"]
    if doctype == SUBCONTRACTING_INWARD_ORDER:
        # Subcontracting Inward Order breadth (2026-07-22): the FORTIETH special branch by direct
        # count. party_field="customer" splices literally (a plain, always-real GL party). No
        # grand_total/total substitute exists on this doctype AT ALL — it tracks operational
        # progress only, via six in_list_view:1 Percent columns (json.load-confirmed, the full
        # 7-field in_list_view set alongside transaction_date, no substitute aggregate). All six
        # ride along — the widest operational-tracking branch this campaign has built, wider than
        # Material Request's own two-field per_ordered/per_received precedent.
        return ["name", "status", "docstatus", party_field, "company", date_field,
                "per_raw_material_received", "per_produced", "per_process_loss", "per_delivered",
                "per_raw_material_returned", "per_returned", "modified"]
    if doctype == SUBCONTRACTING_RECEIPT:
        # Subcontracting Receipt breadth (2026-07-22) — THE ROOF ROW: the FORTY-FIRST special
        # branch by direct count. party_field="supplier" splices literally (a plain, always-real
        # GL party) — the generic branch's own shape. The ONE genuine divergence, the SAME shape
        # Subcontracting Order's own branch already established: the grand-total-equivalent field
        # is named "total", not "grand_total" (json.load-confirmed, Currency, read_only) — the
        # generic branch's literal "grand_total" would ask a real bench for a column that doesn't
        # exist on this doctype. per_returned (in_list_view:1 alongside posting_date,
        # json.load-confirmed — the doctype's own EXACT in_list_view set, no third field) rides
        # along too, the Material Request/Subcontracting Order per_ordered/per_received precedent
        # for a completion-tracking column.
        return ["name", "status", "docstatus", party_field, "company", date_field,
                "total", "per_returned", "modified"]
    return ["name", "status", "docstatus", party_field, "company", date_field,
            "grand_total", "modified"]


class ErpnextError(Exception):
    """A refused/failed bench call. Carries the HTTP ``status`` (or ``None`` for a shape problem).
    Messages carry the bench's own reason but never credential material.

    ``answered`` (transport taxonomy, docs/plans/2026-07-07-transport-taxonomy.md) is truthy ONLY
    when an int HTTP status arrived together with a successfully-parsed frappe JSON body (the bench
    definitely saw and refused the call), or when the status is a pre-processing rejection (429/413,
    which trip before the handler ever runs, any body). It defaults ``False`` — a raw exception, a
    connection-level failure, or a non-JSON ("proxy-shaped") body is never assumed to be an answered
    refusal; ``spine``/``cascade`` read this attribute (via ``getattr(exc, "answered", False)``, so
    even a bare, unconverted exception classifies deny-biased as "no answer") to decide whether an
    exception from the mutating call releases the consent marker (answered) or must spend it and
    resolve via a governed readback (everything else)."""

    def __init__(self, message, status=None, answered=False):
        super().__init__(message)
        self.status = status
        self.answered = answered


_PRE_HANDLER_STATUSES = (429, 413)  # rate-limited / body-too-large — trip before dispatch (Scout A)


# Frappe stamps these keys on its V1 error envelopes (`report_error` sets ``exc_type``
# unconditionally on every error response, frappe/utils/response.py). A generic JSON-speaking
# proxy's error body ({"error": ...}, {"message": ...}) carries neither — and the list must STAY
# this narrow: "message"/"data"/"error" are exactly the keys proxies use too (redteam catch).
_FRAPPE_ENVELOPE_KEYS = ("exc_type", "_server_messages")


def _answered(status, payload):
    """The transport taxonomy's classification rule (docs/plans/2026-07-07-transport-taxonomy.md):
    an int status with a parsed JSON body carrying FRAPPE's own error-envelope evidence
    (``exc_type`` / ``_server_messages``) means the bench definitely saw and answered the call;
    429/413 are always pre-processing rejections, safe to treat as answered wherever emitted,
    body or not (status alone decides for those two). A dict WITHOUT frappe's envelope keys is a
    JSON-speaking proxy's error page — unknown progress, treated exactly like no answer
    (redteam-reproduced: a Traefik/ALB 502 with ``{"error": "Bad Gateway"}`` must NOT release
    the marker). Anything else is deny-biased ambiguity — ``False``."""
    if status in _PRE_HANDLER_STATUSES:
        return True
    return isinstance(payload, dict) and any(k in payload for k in _FRAPPE_ENVELOPE_KEYS)


def _extract_server_reason(payload):
    """Pull the human reason out of frappe's error envelope, defensively."""
    if not isinstance(payload, dict):
        return ""
    parts = []
    if payload.get("exc_type"):
        parts.append(str(payload["exc_type"]))
    raw = payload.get("_server_messages")
    if isinstance(raw, str):
        try:
            for item in json.loads(raw):
                msg = json.loads(item) if isinstance(item, str) else item
                if isinstance(msg, dict) and msg.get("message"):
                    parts.append(str(msg["message"]))
                elif isinstance(msg, str):
                    parts.append(msg)
        except (ValueError, TypeError):
            parts.append(raw)
    elif payload.get("message"):
        parts.append(str(payload["message"]))
    return ": ".join(parts)


def default_transport(method, url, headers, params=None, body=None, timeout=30):
    """The real (urllib) transport. Returns ``(status, parsed_json_or_None)``."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers={**headers, "Accept": "application/json"},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — scheme validated by registry
            return resp.status, _parse_json(resp.read())
    except urllib.error.HTTPError as exc:
        # An answered HTTP error response — handled FIRST and separately from the broadened
        # OSError catch below, even though HTTPError is itself a URLError/OSError subclass: this
        # branch means the bench (or a proxy standing in for it) actually sent a status line, so
        # it must classify via `_call`'s own status+body rule, never as "no answer".
        return exc.code, _parse_json(exc.read())
    except OSError as exc:
        # Broadened from (URLError, TimeoutError) to the whole OSError family (transport
        # taxonomy) — URLError and the builtin TimeoutError are BOTH already OSError subclasses,
        # so this collapses cleanly and additionally catches raw connection-level failures that
        # used to escape unconverted (ConnectionResetError, ConnectionAbortedError, BrokenPipeError
        # mid-read, etc.). `status=None`/`answered=False` (the defaults) — a positive-proof
        # classification already treats "no answer" as the deny-biased default, so this broadening
        # is belt-and-suspenders, not load-bearing.
        raise ErpnextError(f"cannot reach the bench: {exc}") from exc


def _parse_json(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


class ErpnextClient:
    """Shape-pinned REST calls for slice-one, authenticated as the scoped broker user."""

    def __init__(self, base_url, api_key, api_secret, transport=default_transport):
        self._base = base_url.rstrip("/")
        self._auth = f"token {api_key}:{api_secret}"
        self._transport = transport

    # --- plumbing ------------------------------------------------------------------
    def _call(self, method, path, params=None, body=None):
        status, payload = self._transport(
            method, f"{self._base}{path}", {"Authorization": self._auth},
            params=params, body=body,
        )
        if not 200 <= status < 300:
            reason = _extract_server_reason(payload) or "bench refused the call"
            raise ErpnextError(f"HTTP {status}: {reason}", status=status,
                               answered=_answered(status, payload))
        if not isinstance(payload, dict):
            # An in-range status but a non-JSON body — still proxy-shaped ambiguity in spirit
            # (something between the broker and the bench mangled the response), so this stays
            # unanswered too (`_answered` returns False here: payload is never a dict on this
            # branch, and a 2xx status can never be 429/413).
            raise ErpnextError("bench returned a non-JSON response", status=status,
                               answered=_answered(status, payload))
        return payload

    def _data(self, payload):
        if "data" not in payload:
            raise ErpnextError("bench response has no 'data' envelope")
        return payload["data"]

    @staticmethod
    def _doc_path(doctype, name):
        if not isinstance(name, str) or not name.strip():
            raise ErpnextError("a document name is required")
        return (f"/api/resource/{urllib.parse.quote(doctype, safe='')}"
                f"/{urllib.parse.quote(name, safe='')}")

    # --- read tier -----------------------------------------------------------------
    def get_document(self, doctype, name):
        """Read one document of ``doctype`` (permission-scoped item GET). Generalized in the
        Purchase Invoice breadth increment — was ``get_sales_invoice(name)``; every call site
        now passes ``doctype`` explicitly (``SALES_INVOICE``/``PURCHASE_INVOICE``)."""
        return self._data(self._call("GET", self._doc_path(doctype, name)))

    def list_documents(self, doctype, filters=None, limit=20, party_field="customer",
                       date_field="posting_date"):
        """List documents of ``doctype`` (permission-scoped). ``party_field`` selects which field
        carries the counterparty in the returned rows — ``"customer"`` for Sales Invoice/Sales
        Order/Delivery Note, ``"supplier"`` for Purchase Invoice/Purchase Order/Purchase Receipt/
        Supplier Quotation, ``"party"`` for Payment Entry, ``None`` for Journal Entry, Material
        Request, Stock Entry, and Quotation — none of which carries a single static header-level
        party field (for different reasons each — see :data:`SUPPORTED_DOCTYPES`, and note
        Quotation's own None is the ONE case where a party genuinely exists but as a Dynamic Link
        pair, not a field this parameter's single-fieldname shape can name); the caller (tools.py)
        supplies it, this client has no doctype config of its own. Generalized in the Purchase
        Invoice breadth increment — was ``list_sales_invoices(filters, limit)``.

        ``date_field`` (Sales Order breadth increment, joined by Purchase Order, Material Request,
        Supplier Quotation, and Quotation) — the doctype's own transaction-date fieldname, supplied
        by the caller from :data:`SUPPORTED_DOCTYPES`' ``"date_field"`` exactly like
        ``party_field`` already is; defaults to ``"posting_date"`` (SI/PI/PE/JE/DN/PR/SE's own
        field). Sales Order, Purchase Order, Material Request, Supplier Quotation, and Quotation
        all supply ``"transaction_date"`` — see :func:`_list_fields`."""
        params = {
            "fields": json.dumps(_list_fields(doctype, party_field, date_field)),
            "limit_page_length": str(int(limit)),
        }
        if filters:
            params["filters"] = json.dumps(filters)
        path = f"/api/resource/{urllib.parse.quote(doctype, safe='')}"
        return self._data(self._call("GET", path, params=params))

    # --- PLAN ----------------------------------------------------------------------
    def ledger_preview(self, company, doctype, docname):
        """ERPNext's native dry-run: savepoint → make_gl_entries in memory → rollback."""
        payload = self._call("POST", f"/api/method/{PREVIEW_METHOD}",
                             body={"company": company, "doctype": doctype, "docname": docname})
        if "message" not in payload:
            raise ErpnextError("preview response has no 'message' envelope")
        return payload["message"]

    # --- execute -------------------------------------------------------------------
    def submit_document(self, doctype, name, doc=None):
        """One of the two state-changing verbs, for any doctype in :data:`SUPPORTED_DOCTYPES`.

        Branches on the doctype's configured ``submit_via`` (module docstring / dict comment):

        * ``SUBMIT_VIA_RUN_METHOD`` (SI/PI/PE, unchanged, live-proven) — sends
          ``run_method=submit`` and NOTHING else — no adv_adj, no posting_date, no doc payload
          (the draft is submitted as it stands). ``doctype`` travels in the URL path, which is
          what lets ``pacioli_guard`` classify this per-doctype (e.g. ``"Purchase Invoice.submit"``).
        * ``SUBMIT_VIA_CLIENT_RPC`` (Journal Entry, new) — ``POST /api/method/frappe.client.submit``
          with ``{"doc": doc}``. ``doc`` is REQUIRED here (raises if ``None``, never silently falls
          back to the 403ing run_method shape) — frappe's ``frappe.client.submit`` reconstructs
          the document from this body (``frappe.get_doc(doc)``) rather than reading it fresh from
          the DB, so the caller must pass the SAME doc it already validated its plan/closed-books/
          freshness gates against (``tools.py``'s ``_governed_write``). This is body-doctype (the
          doctype lives in ``doc["doctype"]``, not the URL) — safe to enable only because
          ``pacioli_guard.scope.body_scoped_target`` now parses it and enforces the credential's
          per-doctype grant identically to the run_method shape (guard CHANGELOG 0.5.0). Still
          KNOWLEDGE-PINNED, NOT LIVE-VERIFIED (Gate 10, next armed window).
        """
        submit_via = SUPPORTED_DOCTYPES.get(doctype, {}).get("submit_via", SUBMIT_VIA_RUN_METHOD)
        if submit_via == SUBMIT_VIA_CLIENT_RPC:
            if not isinstance(doc, dict) or not doc:
                raise ErpnextError(
                    f"{doctype} submits via frappe.client.submit and requires the already-"
                    "fetched document body — none was supplied")
            payload = self._call("POST", f"/api/method/{_CLIENT_SUBMIT_METHOD}", body={"doc": doc})
            if "message" not in payload:
                raise ErpnextError("frappe.client.submit response has no 'message' envelope")
            return payload["message"]
        payload = self._call("POST", self._doc_path(doctype, name),
                             params={"run_method": "submit"})
        return self._data(payload)

    def cancel_document(self, doctype, name):
        """The UNDO verb.

        * ``SUBMIT_VIA_RUN_METHOD`` (SI/PI/PE, unchanged, live-proven) — the same guard-scopeable
          doc-method shape as submit (classifies per doctype, e.g. ``"Sales Invoice.cancel"``), and
          the same discipline: run_method=cancel and NOTHING else. ERPNext's own cancel-blocks
          (closed period, PCV, freeze date, reconciled links) are relied on downstream and never
          bypassed — the broker refuses first at its own closed-books check anyway.
        * ``SUBMIT_VIA_CLIENT_RPC`` (Journal Entry, new) — ``POST /api/method/frappe.client.cancel``
          with ``{"doctype": doctype, "name": name}``. Unlike submit, NO doc body is needed:
          ``frappe.client.cancel`` loads the document fresh from the DB itself
          (``frappe.get_doc(doctype, name); wrapper.cancel()``) — doctype/name are plain sibling
          params, never nested under a ``doc`` key. Body-doctype, made safe by
          ``pacioli_guard.scope.body_scoped_target`` the same way submit is (see above).
          KNOWLEDGE-PINNED, NOT LIVE-VERIFIED.
        """
        submit_via = SUPPORTED_DOCTYPES.get(doctype, {}).get("submit_via", SUBMIT_VIA_RUN_METHOD)
        if submit_via == SUBMIT_VIA_CLIENT_RPC:
            payload = self._call("POST", f"/api/method/{_CLIENT_CANCEL_METHOD}",
                                 body={"doctype": doctype, "name": name})
            if "message" not in payload:
                raise ErpnextError("frappe.client.cancel response has no 'message' envelope")
            return payload["message"]
        payload = self._call("POST", self._doc_path(doctype, name),
                             params={"run_method": "cancel"})
        return self._data(payload)

    # --- UNDO inputs -----------------------------------------------------------------
    def get_submitted_linked_docs(self, doctype, name):
        """The cancel blast-radius: submitted documents linked to this one (what ERPNext's own
        cancel dialog consults). Returns the ``docs`` list (possibly empty). Raises on an
        unreadable graph — an unverifiable blast radius refuses, it never reads as empty."""
        payload = self._call(
            "POST", "/api/method/frappe.desk.form.linked_with.get_submitted_linked_docs",
            body={"doctype": doctype, "name": name})
        if "message" not in payload:
            raise ErpnextError("linked-docs response has no 'message' envelope")
        message = payload["message"]
        if message is None:
            return []  # frappe's whole-null response — a genuinely empty graph
        # A dict message MUST carry a real `docs` list. A null/absent `docs` (even alongside a
        # non-zero `count`), or a non-list `docs`, is an UNREADABLE graph, never silently a leaf —
        # deny-biased, per the docstring: an unverifiable blast radius refuses.
        if not isinstance(message, dict) or not isinstance(message.get("docs"), list):
            raise ErpnextError("linked-docs 'message' has no readable 'docs' list — "
                               "blast radius unreadable")
        return message["docs"]

    def get_gl_entries(self, voucher_type, voucher_no):
        """The posting's live GL rows — what a cancel unwinds (presented as the plan's projected
        reversal). Reads only uncancelled rows; requires ``GL Entry`` in the credential's
        resource-doctype grant. Filters on BOTH ``voucher_type`` (= the doctype) AND
        ``voucher_no``, not ``voucher_no`` alone — a latent cross-doctype gap this increment
        closes: once Sales Invoice and Purchase Invoice share one GL Entry table, a voucher_no
        collision (however unlikely) would otherwise surface the wrong doctype's rows as this
        cancel's projected reversal. Generalized in the Purchase Invoice breadth increment — was
        ``get_gl_entries(voucher_no)``. The field list carries ``against_voucher_type``/
        ``against_voucher`` (Payment Entry breadth) — a Payment Entry cancel can unwind GL rows
        against N different invoices in one voucher, so the projected reversal needs to say which
        invoice each row is against; both are ordinary GL Entry columns populated for any
        doctype's referencing rows (SI/PI included), so this is a plain field-list addition, not a
        doctype-conditional branch.

        **Row validation (reconciliation-audit residual, 21b7f84 — "2-of-9 field pinning"),
        GL-entries-shaped: same house pattern as :meth:`get_settling_references`/
        :meth:`get_period_locks`.** A non-list body (``"data": null`` and similar) or a non-dict
        row is a structured deny, never handed through to the caller's disclosure loop. Of the 9
        requested fields, ``account``/``debit``/``credit`` are the row's actual accounting content
        — WHICH account, HOW MUCH debited/credited — the substance of the "projected reversal" a
        human consents to (single-op ``plan_cancel``) or a cascade accumulates across nodes
        (``plan_cascade_cancel``); a malformed value there must refuse, never silently reach that
        disclosure (or a future summing consumer) coerced to zero/blank. ``account`` must be a
        non-blank string; ``debit``/``credit`` must be finite non-bool numbers (``math.isfinite``,
        the same NaN-defense :func:`pacioli.reconcile.check_allocation` and
        :mod:`pacioli.consent`/:mod:`pacioli.prove` already apply) — ``0.0`` is the ordinary,
        valid value for a row's unused side, never treated as "missing". The remaining fields
        (``posting_date``, ``against``, ``party_type``, ``party``, ``against_voucher_type``,
        ``against_voucher``) are disclosure-only metadata that are legitimately blank on many real
        rows (a Cash-account row typically carries no party; only a row settling another voucher
        carries an against_voucher at all) — deliberately NOT validated, so a null/absent value
        there is tolerated, not refused."""
        params = {
            "fields": json.dumps(["posting_date", "account", "debit", "credit",
                                  "against", "party_type", "party",
                                  "against_voucher_type", "against_voucher"]),
            "filters": json.dumps([["voucher_type", "=", voucher_type],
                                   ["voucher_no", "=", voucher_no],
                                   ["is_cancelled", "=", 0]]),
            "limit_page_length": "0",
        }
        rows = self._data(self._call("GET", "/api/resource/GL%20Entry", params=params))
        if not isinstance(rows, list):
            raise ErpnextError(
                "GL Entry list read returned a non-list body; cannot verify the projected "
                "reversal, refusing")
        for row in rows:
            if not isinstance(row, dict):
                raise ErpnextError(
                    "GL Entry list row is malformed (not an object); cannot verify the "
                    "projected reversal, refusing")
            account = row.get("account")
            if not isinstance(account, str) or not account.strip():
                raise ErpnextError(
                    f"GL Entry row for {voucher_type} {voucher_no!r} has a malformed/missing "
                    f"account ({account!r}); an unverifiable reversal target refuses, it never "
                    "reads as a valid row")
            for field in ("debit", "credit"):
                value = row.get(field)
                if not isinstance(value, (int, float)) or isinstance(value, bool) \
                        or not math.isfinite(value):
                    raise ErpnextError(
                        f"GL Entry row for {voucher_type} {voucher_no!r}, account {account!r}, "
                        f"has a malformed {field} ({value!r}); an unverifiable reversal amount "
                        "refuses, it never reads as zero")
        return rows

    # --- AMEND (the corrected re-draft) ------------------------------------------------
    def get_doc_for_amend(self, doctype, name):
        """The full source document for :func:`pacioli.amend.amend_payload` — the same
        permission-scoped item GET as the read tier (an item GET returns every field, child
        tables included); named separately so the amend read is explicit in the call trace.
        Generalized in the Purchase Invoice breadth increment — was ``get_doc_for_amend(name)``
        (Sales Invoice only)."""
        return self.get_document(doctype, name)

    def find_amendments(self, doctype, name):
        """Existing amendments of ``name``: documents of ``doctype`` whose ``amended_from``
        points at it, at **any** docstatus — a draft amendment counts, and so do a submitted and
        a cancelled one (deliberately no docstatus filter). Used to refuse a second amend of the
        same cancelled document. Requires no extra grant beyond the doctype's own resource read.
        Generalized in the Purchase Invoice breadth increment — was ``find_amendments(name)``
        (Sales Invoice only)."""
        params = {
            "fields": json.dumps(["name", "docstatus"]),
            "filters": json.dumps([["amended_from", "=", name]]),
            "limit_page_length": "0",
        }
        path = f"/api/resource/{urllib.parse.quote(doctype, safe='')}"
        data = self._data(self._call("GET", path, params=params))
        if not isinstance(data, list):
            # An unreadable amendment search refuses — never reads as "no amendments" (which would
            # let a second amended draft slip past the duplicate-amend refusal). Empty is `[]`.
            raise ErpnextError("amendment search returned no readable list — cannot verify "
                               "existing amendments; refusing")
        return data

    def create_amended_draft(self, doctype, source_doc, seat=None):
        """Insert the amended DRAFT: a resource CREATE (POST to the **collection** URL for
        ``doctype``) carrying exactly the payload :func:`pacioli.amend.amend_payload` builds —
        copy → strip → ``amended_from`` set → insert as docstatus 0, plus the workflow ``seat``
        (the F1 fix — see :func:`pacioli.amend.amend_payload`) when the tool layer computed one.
        The insert is reversible (a draft deletes cleanly), which is why this verb, alone among
        the mutations, carries no consent marker (see :mod:`pacioli.amend`). A payload refusal is
        re-raised as :class:`ErpnextError` so the tool layer's structured-deny guarantee holds
        even without its own gate. Generalized in the Purchase Invoice breadth increment — was
        ``create_amended_draft(source_doc)`` (Sales Invoice's collection URL only)."""
        try:
            body = amend_payload(source_doc, seat=seat)
        except ValueError as exc:
            raise ErpnextError(str(exc)) from exc
        payload = self._call(
            "POST", f"/api/resource/{urllib.parse.quote(doctype, safe='')}", body=body)
        return self._data(payload)

    # --- WORKFLOW (CONSENT's second gate — see pacioli.workflow) ---------------------
    def get_active_workflows(self, doctype):
        """The active Workflow(s) governing ``doctype`` — two-step, because the LIST endpoint
        does not expand child tables: a name-only GET filtered to
        ``[["document_type", "=", doctype], ["is_active", "=", 1]]``, then a full-doc GET per
        name (the pure gate in :mod:`pacioli.workflow` needs the ``states``/``transitions``
        child tables). Returns ``[]`` when nothing is active — never raises for "no workflow".

        Requires a **custom read permission** on the ``Workflow`` DocType for the broker's role:
        by default it is System Manager-only, so the scoped broker credential cannot read it
        until the site's Role Permission Manager grants it — document this as a required
        scoping grant, exactly like the period-lock sources. An unreadable list OR an unreadable
        individual workflow doc both raise :class:`ErpnextError` (deny) — never read as "no
        workflow configured". Knowledge-pinned from frappe source (Workflow DocType JSON), NOT
        live-verified."""
        params = {
            "fields": json.dumps(["name"]),
            "filters": json.dumps([["document_type", "=", doctype], ["is_active", "=", 1]]),
            "limit_page_length": "0",
        }
        path = f"/api/resource/{urllib.parse.quote('Workflow', safe='')}"
        rows = self._data(self._call("GET", path, params=params))
        workflows = []
        for row in rows:
            name = row.get("name") if isinstance(row, dict) else None
            if not isinstance(name, str) or not name.strip():
                raise ErpnextError("active-workflow list row is missing a name")
            doc = self._data(self._call("GET", self._doc_path("Workflow", name)))
            # A null/empty/nameless full-doc body must RAISE here, honouring this method's own
            # deny contract — passed downstream, a single malformed body would read as "no
            # workflow" at the gate and silently disable it (the redteam-proven bypass; the pure
            # core's Malformed sentinel is the belt under this suspender).
            if not isinstance(doc, dict) or not doc \
                    or not isinstance(doc.get("name"), str) or not doc["name"].strip():
                raise ErpnextError(f"workflow doc {name!r} unreadable or malformed — refusing")
            workflows.append(doc)
        return workflows

    def get_workflow_state(self, doctype, name, state_field):
        """The document's current workflow-state value, read off ``state_field`` — which comes
        from the governing workflow's own ``workflow_state_field`` (CONFIGURABLE per workflow;
        the caller must never hardcode ``"workflow_state"``). Reuses the same permission-scoped
        item GET the read tier already makes. Raises on an unreadable document (deny); an
        **empty or missing value on a readable document is returned as-is** — frappe lazily
        backfills ``workflow_state`` on next save, so an unset state is a real document shape,
        not a read failure. The pure gate (:func:`pacioli.workflow.check_transition`) is what
        denies on it, not this call."""
        doc = self._data(self._call("GET", self._doc_path(doctype, name)))
        return doc.get(state_field)

    def apply_workflow(self, doctype, name, action):
        """Transport ONLY for a transition :mod:`pacioli.workflow` / ``tools.py`` has already
        classified as non-approving — the classification gate lives there, never here.
        ``POST /api/method/frappe.model.workflow.apply_workflow`` with
        ``{"doc": {"doctype": doctype, "name": name}, "action": action}``. Knowledge-pinned from
        ``frappe/model/workflow.py`` (2026-07-02, NOT live-verified): the server does
        ``frappe.get_doc(parse_json(doc)); doc.load_from_db()``, so only ``doctype``/``name``
        travel in the body — anything else would be misleading, not functional, so we send
        nothing else. Every workflow exception (``WorkflowStateError``/``TransitionError``/
        ``PermissionError``, the plain-``ValidationError`` self-approval block) maps to HTTP 417
        on frappe's side; a bare permission failure can also 403 — both raise
        :class:`ErpnextError` through the usual non-2xx path, never a silent no-op."""
        payload = self._call("POST", "/api/method/frappe.model.workflow.apply_workflow",
                             body={"doc": {"doctype": doctype, "name": name}, "action": action})
        if "message" not in payload:
            raise ErpnextError("apply_workflow response has no 'message' envelope")
        return payload["message"]

    # --- the closed-books inputs -----------------------------------------------------------
    def get_period_locks(self, company, doctype, posting_date):
        """Read the three period-lock boundaries for :func:`pacioli.plan.check_red_line`.
        ``doctype``/``posting_date`` are REQUIRED — no default — so a call site cannot silently
        regress to a doctype-blind read (F-S1; the prior over-refusing shape is retired, not kept
        as a fallback).

        **The frozen-till-date source (v16 spine fix, unchanged by F-S1).** ``Accounts
        Settings.acc_frozen_upto`` was migrated onto ``Company.accounts_frozen_till_date`` in
        ERPNext v16 (confirmed against ``erpnext/patches/v16_0/migrate_account_freezing_settings_
        to_company.py``, which moves the stored value, and against the doctype JSONs: the field is
        absent from ``accounts_settings.json`` on a v16 bench and present on ``company.json``) —
        ``general_ledger.check_freezing_date`` (the real enforcement) reads Company, not Accounts
        Settings. This reads BOTH: the Company doc (the v16 source) and Accounts Settings (the
        legacy v15 field, kept for a bench that hasn't migrated) — if both carry a value, the
        LATER date wins, since either could be the live boundary depending on bench version and
        neither should be silently dropped.

        **The Accounting Period check (F-S1 — doctype- and date-range-aware, the E6 over-refusal
        fixed; F-C1 — the LIST no longer filters ``disabled``, restoring v15 compatibility).**
        Two-scout source read confirmed (``accounting_period.py``, ``general_ledger.py``,
        ``hooks.py``): ERPNext blocks a posting only when its date falls BETWEEN a *specific*
        period's ``start_date``/``end_date`` (both ends inclusive) AND that period is
        ``disabled=0`` AND that period closes the posting's doctype (a ``Closed Document`` child
        row with ``closed=1`` and ``document_type`` equal to the doctype, verbatim). This method
        matches that shape instead of over-refusing every doctype past the latest period's end
        date:

        1. LIST ``Accounting Period`` filtered to ``company`` + a range that CONTAINS
           ``posting_date`` (``start_date <= posting_date <= end_date``) — normally 0 or 1 hits
           (``validate_overlap`` forbids two enabled periods overlapping in one company;
           same-company duplicates are a data-hygiene edge, not a normal shape). **No ``disabled``
           filter here (F-C1)** — ``disabled`` is a v16-only column (``accounting_period.json`` on
           a v15 bench has no such field); filtering a LIST on a column the bench's schema doesn't
           carry is an unknown-column error frappe's filter builder never validates against, which
           would turn this LIST into a hard failure on every v15 bench and refuse every governed
           op via this method's own deny-bias. ``start_date``/``end_date`` exist on both versions
           (confirmed), so the range+company filter is safe on either.
        2. For EACH hit (never just the first — equal-or-stricter than ERPNext's own unordered
           first-row read), a full-document GET (the list endpoint never expands child tables —
           the same two-step :meth:`get_active_workflows` already uses) reads ``disabled`` and
           ``closed_documents``.
        3. If that full document's ``disabled`` is truthy, the period is skipped entirely —
           BEFORE its ``closed_documents`` rows are inspected — a disabled period locks nothing
           (the F-S1/PHASE-T semantics, unchanged, just read from the item GET instead of the LIST
           filter now). If ``disabled`` is **absent** from the full document (the v15 shape — v15
           has no period-disable concept at all) it is treated as enabled, which is the CORRECT
           v15 behavior, not a fallback guess.
        4. For each hit that survives step 3, a row ``closed=1`` (or truthy-``1``) with
           ``document_type`` equal to ``doctype`` (verbatim string) sets
           ``locks["closed_period_until"]`` to THAT period's ``end_date``; otherwise the key is
           simply absent.

        **Deny-bias, unchanged in spirit, extended in surface:** an unreadable LIST or unreadable
        item GET raises (never "assume open"). A malformed period or child row — a non-ISO
        ``start_date``/``end_date`` on a hit, a ``closed_documents`` row missing
        ``document_type``, a ``closed`` value that isn't a clean ``0``/``1``/``bool``, or a
        ``disabled`` value that is PRESENT but isn't a clean ``0``/``1``/``bool``/``None`` —
        raises too: an unverifiable lock must refuse, the same class as unreadable, never silently
        skipped. (Judgment call, flagged for redteam: a malformed-but-present ``disabled`` raises
        rather than being coerced either way — coercing it to falsy/enabled risks the dangerous
        direction, a locked period silently read as open, on nothing more than a guess about what
        the bench meant by that value; raising keeps the same "can't verify, refuse" discipline
        every other field on this row already gets, at the cost of one more refusal case that has
        never been observed on a real bench.) ``posting_date`` itself is validated ISO **before**
        any network call is made (a malformed date refuses immediately, the same discipline
        :func:`pacioli.plan.check_red_line` already applies downstream — this just refuses to
        spend a round-trip building a query on a date that check would refuse anyway).

        **Deliberately NOT modeled: ``exempted_role``.** A per-period role (first matched row)
        that lets a *raw* ERPNext seat holding it bypass the period lock entirely. This broker
        does not inherit that bypass — a seat holding the exempted role could act directly against
        the bench and succeed where this broker refuses. Equal-or-stricter than ERPNext, disclosed
        here rather than silently narrower; there is no ``frappe.flags`` bypass to model, amend
        gets no exemption, and ``from_repost`` is still checked by ERPNext's own path (a real
        ERPNext quirk, not this broker's concern).

        **Cancel parity.** ERPNext blocks CANCELLING into a closed period too, via
        ``general_ledger.make_reverse_gl_entries`` (the GL-level check re-runs on cancel, unlike
        the doc-level ``validate_accounting_period_on_doc_save`` hook which does not fire on
        cancel) — this broker's uniform submit+cancel closed-books gate already matches that,
        calling this same method for both operations.

        Absent locks are simply absent from the dict (never empty strings)."""
        if not _is_iso_date(posting_date):
            raise ErpnextError(
                f"posting_date {posting_date!r} is not a valid ISO (YYYY-MM-DD) date; refusing "
                "to build a period-lock query on an unverifiable date")
        locks = {}
        company_doc = self._data(self._call("GET", self._doc_path("Company", company)))
        frozen_dates = []
        company_frozen = company_doc.get("accounts_frozen_till_date")
        if isinstance(company_frozen, str) and company_frozen.strip():
            frozen_dates.append(company_frozen.strip())

        settings = self._data(self._call(
            "GET", "/api/resource/Accounts%20Settings/Accounts%20Settings"))
        legacy_frozen = settings.get("acc_frozen_upto")
        if isinstance(legacy_frozen, str) and legacy_frozen.strip():
            frozen_dates.append(legacy_frozen.strip())

        if frozen_dates:
            # ISO YYYY-MM-DD strings — lexicographic max is chronological max (the same invariant
            # pacioli.plan's date-range checks already rely on) — honor the LATER of the two
            # sources rather than preferring one over the other.
            locks["frozen_until"] = max(frozen_dates)

        pcv = self._data(self._call(
            "GET", "/api/resource/Period%20Closing%20Voucher",
            params={"fields": json.dumps(["period_end_date"]),
                    "filters": json.dumps([["company", "=", company], ["docstatus", "=", 1]]),
                    "order_by": "period_end_date desc", "limit_page_length": "1"}))
        if pcv and pcv[0].get("period_end_date"):
            locks["pcv_until"] = str(pcv[0]["period_end_date"])

        # F-S1: doctype- and date-range-aware Accounting Period check (see docstring above). The
        # LIST filter does the coarse work (company, containing range) — a bench that filters
        # correctly can never hand back a different company's period or one that doesn't even
        # contain posting_date, so nothing further needs re-checking client-side for those two
        # dimensions. What the LIST endpoint can NOT hand back is `disabled` scoped correctly
        # across versions or the closed_documents child table (frappe never expands child tables
        # on a list read), hence the per-hit item GET below reads both.
        #
        # F-C1: deliberately NO `disabled` filter here. `disabled` is v16-only on `Accounting
        # Period` (absent from a v15 bench's schema) and frappe's filter builder has no
        # meta-validation/sanitizer (`frappe/model/db_query.py::build_filter_conditions` ->
        # `prepare_filter_condition`) — filtering on a column that doesn't exist is an
        # unknown-column failure, not "no match", which would turn this LIST into a hard error on
        # every v15 bench and (via this method's own deny-bias) refuse every governed op there.
        # `company`/`start_date`/`end_date` are confirmed present on both v15 and v16, so they stay.
        periods = self._data(self._call(
            "GET", "/api/resource/Accounting%20Period",
            params={"fields": json.dumps(["name", "start_date", "end_date"]),
                    "filters": json.dumps([["company", "=", company],
                                           ["start_date", "<=", posting_date],
                                           ["end_date", ">=", posting_date]]),
                    "limit_page_length": "0"}))
        if not isinstance(periods, list):
            # A LIST body whose "data" is present but not a list (e.g. null) is as unverifiable
            # as an unreadable one — the structured deny, not a bare TypeError out of the loop.
            raise ErpnextError(
                "Accounting Period list read returned a non-list body; "
                "cannot verify the closed-period lock, refusing")
        matched_end_date = None
        for row in periods:
            if not isinstance(row, dict):
                raise ErpnextError("Accounting Period list row is malformed (not an object)")
            name = row.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ErpnextError("Accounting Period list row is missing a name")
            start_date, end_date = row.get("start_date"), row.get("end_date")
            if not _is_iso_date(start_date) or not _is_iso_date(end_date):
                raise ErpnextError(
                    f"Accounting Period {name!r} has a malformed start_date/end_date "
                    f"({start_date!r}, {end_date!r}); refusing rather than trust an unverifiable "
                    "period boundary")
            # Full-doc GET, never re-derivable from the list row — the ONLY way to read
            # closed_documents (module docstring / get_active_workflows precedent).
            full = self._data(self._call("GET", self._doc_path("Accounting Period", name)))
            if not isinstance(full, dict) or not full:
                raise ErpnextError(f"Accounting Period {name!r} unreadable or malformed — refusing")
            # F-C1: `disabled` is read here, off the full document, not off the LIST filter (see
            # the LIST comment above). Absent (v15 — the column doesn't exist there) is treated as
            # enabled, which is the correct v15 behavior, not a guess. Present-but-not-a-clean
            # 0/1/bool is a malformed value — raise rather than coerce it either direction (a
            # judgment call: coercing toward "enabled" is safe, but coercing toward "disabled"
            # risks silently unlocking a period that closed_documents would otherwise have
            # refused, so an unparseable value gets the same "can't verify, refuse" treatment
            # every other field on this row already gets).
            disabled_raw = full.get("disabled")
            if disabled_raw not in (0, 1, True, False, None):
                raise ErpnextError(
                    f"Accounting Period {name!r} has an unparseable disabled value "
                    f"{disabled_raw!r}; refusing rather than guess whether the period is enabled")
            if disabled_raw:
                # Validated above to be one of 0/1/True/False/None — truthy here means 1 or True.
                # A disabled period locks nothing (F-S1/PHASE-T semantics) — skip it before ever
                # inspecting closed_documents; what it would otherwise close is irrelevant.
                continue
            closed_rows = full.get("closed_documents")
            if closed_rows is None:
                closed_rows = []
            if not isinstance(closed_rows, list):
                raise ErpnextError(
                    f"Accounting Period {name!r} has a malformed closed_documents child table; "
                    "refusing")
            for child in closed_rows:
                if not isinstance(child, dict) or "document_type" not in child:
                    raise ErpnextError(
                        f"Accounting Period {name!r} has a closed_documents row missing "
                        "document_type; refusing")
                closed_raw = child.get("closed")
                if closed_raw not in (0, 1, True, False):
                    raise ErpnextError(
                        f"Accounting Period {name!r} has a closed_documents row with an "
                        f"unparseable closed value {closed_raw!r}; refusing")
                if closed_raw and child.get("document_type") == doctype and matched_end_date is None:
                    # First match wins (same-company overlapping-and-both-closing-this-doctype is
                    # the data-hygiene edge the pin sheet names, not a shape this client need
                    # arbitrate further) — but keep validating every remaining row/period below;
                    # a match found here must never short-circuit validation of the rest.
                    matched_end_date = str(end_date)
        if matched_end_date is not None:
            locks["closed_period_until"] = matched_end_date
        return locks

    def get_accounts_settings(self, fields):
        """Read named ``fields`` off the site's single ``Accounts Settings`` doctype — a small,
        doctype-blind primitive (names no ERPNext DocType beyond Accounts Settings itself), added
        for the Journal Entry breadth increment's ``plan_cancel`` disclosure (``tools.py``): whether
        ``unlink_payment_on_cancellation_of_invoice`` is on changes a JE cancel's blast radius from
        "refused by the generic backlink check" to "a silent raw-SQL unlink of other submitted
        Journal Entries/Payment Entries that reference this one" (scout-je.md §2, §5). Reusable by
        any future disclosure that needs another Accounts Settings field — it takes the field list
        as a parameter rather than hardcoding one.

        Raises on an unreadable response (the same as every other read in this client) — whether
        that should refuse the caller's plan or be read as "flag unknown" is the CALLER's decision,
        made at the tool layer, not here."""
        payload = self._call(
            "GET", "/api/resource/Accounts%20Settings/Accounts%20Settings",
            params={"fields": json.dumps(list(fields))})
        return self._data(payload)

    # --- RECONCILE (F-R2: govern Payment Reconciliation) -----------------------------
    def reconcile(self, company, party_type, party, receivable_payable_account, allocations):
        """The single governed reconcile call — settles a pinned allocation set of
        payments/Journal Entries against invoices. ``docs/plans/2026-07-09-fr2-govern-
        reconciliation.md``: ``POST /api/method/run_doc_method`` with a client-constructed
        ``Payment Reconciliation`` doc body and ``method=reconcile``
        (``frappe/handler.py:272-311``; ``payment_reconciliation.js:386-394``) — the doctype's
        ``load_from_db`` stub is NOT in the write path, so the FULL doc travels in the request,
        never re-fetched server-side.

        ``allocations`` is the caller-facing row shape (mirroring ``pacioli.reconcile``'s pinned
        graph/``rows`` SEMANTIC keys: ``payment_type``/``payment_no``/``invoice_type``/
        ``invoice_no``/``allocated_amount``/``payment_unallocated``/``invoice_outstanding`` per
        row) — this method is the ONLY place those get translated into ERPNext's own wire field
        names, so a semantics swap cannot happen anywhere else.

        WIRE SHAPE — LIVE-VERIFIED (P7, 2026-07-09, real Frappe v16 sealed-lab bench; both former
        BENCH-PENDING questions answered by reproduction):

          1. The ``invoices[]`` pool IS REQUIRED. ``validate_allocation`` builds its per-invoice
             outstanding map from ``self.get("invoices")`` — with the pool absent,
             ``invoice_outstanding`` is None and the ceiling check TypeErrors (HTTP 500, the exact
             refusal 0.13.0's allocation-only shape got live). One pool row per UNIQUE invoice:
             ``{invoice_type, invoice_number, outstanding_amount}``. A ``payments[]`` pool is NOT
             read on this path and is deliberately not sent (nothing untested rides the wire).
          2. The allocation row's ``amount`` AND ``unreconciled_amount`` are BOTH the PAYMENT's
             unallocated. ``validate_allocation`` reads ``row.amount`` (the payment's available;
             unset -> 0 -> throws on row 1) and ``check_if_advance_entry_modified`` compares
             ``row.unreconciled_amount`` to the PE's LIVE ``unallocated_amount`` (the
             no-``voucher_detail_no`` branch, ``utils.py:645-647``) — 0.13.0 sent the invoice's
             outstanding there and the live bench refused: "Payment Entry has been modified after
             you pulled it". Entries are processed GROUPED per voucher with every check before the
             group's single ``save()`` (``reconcile_against_document``), so every row carries the
             plain pre-write value — no running decrement for multi-row-same-payment sets.

        The caller (``tools.py``'s ``_tool_reconcile``) is what guarantees ``allocations`` here is
        built from the PINNED plan graph alone, never forwarded from any other argument — this
        method has no opinion on where its argument came from, it only shapes the wire call.
        Duck-typed return (``message`` envelope if present, else the raw payload): unlike
        ``apply_workflow``, this does NOT raise on a missing ``message`` key — the response
        envelope shape is itself BENCH-PENDING (see above), so asserting one here would pin an
        unverified assumption. ``_call`` already raises :class:`ErpnextError`
        (``answered=...``, the transport taxonomy) on any non-2xx response; the result this
        returns is never trusted as proof of effect by the caller either way — the caller's own
        readback is (``pacioli.reconcile``'s module docstring)."""
        invoices_pool = {}
        for a in allocations:
            key = (a["invoice_type"], a["invoice_no"])
            invoices_pool.setdefault(key, a["invoice_outstanding"])
        pr = {"doctype": "Payment Reconciliation", "company": company, "party_type": party_type,
             "party": party, "receivable_payable_account": receivable_payable_account,
             "invoices": [
                 {"invoice_type": dt, "invoice_number": no, "outstanding_amount": out}
                 for (dt, no), out in invoices_pool.items()
             ],
             "allocation": [
                 {"invoice_type": a["invoice_type"], "invoice_number": a["invoice_no"],
                  "reference_type": a["payment_type"], "reference_name": a["payment_no"],
                  "allocated_amount": a["allocated_amount"], "amount": a["payment_unallocated"],
                  "unreconciled_amount": a["payment_unallocated"]}
                 for a in allocations
             ]}
        payload = self._call("POST", "/api/method/run_doc_method",
                             body={"docs": json.dumps(pr), "method": "reconcile"})
        return payload.get("message", payload) if isinstance(payload, dict) else payload

    def get_settling_references(self, doctype, name):
        """F-R1 — the settling-PE disclosure read. **Payment Ledger Entry** is the settlement
        ledger since ERPNext v14 (``update_voucher_outstanding`` reads it), and it is doctype-blind
        by construction: filtering on ``against_voucher_type``/``against_voucher_no`` surfaces
        whatever settles the target document, not just a Payment Entry — honest against
        ``auto_cancel_exempted_doctypes`` hook extensions (currently just Payment Entry in stock
        ERPNext, but the union is per-installed-app and this read never assumes the list stays
        that short).

        GL-entries-shaped (:meth:`get_gl_entries` is the template): explicit ``fields``
        (``voucher_type``/``voucher_no``/``amount``/``account_currency``), explicit ``filters``
        (``against_voucher_type``, ``against_voucher_no``, ``delinked=0`` — a delinked row is
        already severed, nothing left to disclose — and ``voucher_no != name``, which excludes the
        target document's own self-referencing rows once it is itself unlinked/unallocated),
        ``limit_page_length: "0"`` (F-V1 law: never a silent partial page).

        **Structured deny on a non-list body** — the same house pattern
        :meth:`get_period_locks` already applies to its Accounting Period LIST read: a
        ``"data": null`` (or otherwise non-list) body is valid JSON the transport layer accepts,
        but is as unverifiable as an unreadable response — raising here, rather than handing a
        non-list through to the caller's per-row disclosure loop, keeps this a structured deny
        instead of a bare ``TypeError``/silent empty read.

        Raises on an unreadable response too (the same as every other read in this client) — the
        CALLER (``tools.py``) decides this refuses the whole plan (deny-biased, pin sheet), not
        this client."""
        params = {
            "fields": json.dumps(["voucher_type", "voucher_no", "amount", "account_currency"]),
            "filters": json.dumps([["against_voucher_type", "=", doctype],
                                   ["against_voucher_no", "=", name],
                                   ["delinked", "=", 0],
                                   ["voucher_no", "!=", name]]),
            "limit_page_length": "0",
        }
        rows = self._data(self._call("GET", "/api/resource/Payment%20Ledger%20Entry", params=params))
        if not isinstance(rows, list):
            raise ErpnextError(
                "Payment Ledger Entry list read returned a non-list body; cannot verify settling "
                "references, refusing")
        for row in rows:
            # Row-shape guard, same as get_period_locks' per-row validation (redteam catch): a
            # malformed row must be a structured deny, never an AttributeError out of the
            # disclosure loop.
            if not isinstance(row, dict):
                raise ErpnextError(
                    "Payment Ledger Entry list row is malformed (not an object); cannot verify "
                    "settling references, refusing")
        return rows

    # --- THE CLOSE, HALF 2 (the Reconciliation) — period-sweep reads ------------------
    def sweep_gl_entries(self, company, since, until):
        """Fork I — the CREATION-window movement sweep.

        Unlike :meth:`get_gl_entries` (voucher-scoped: filtered on ``voucher_type``/``voucher_no``
        for one document's projected reversal), this reads EVERY GL Entry row **written** for
        ``company`` inside ``[since, until]`` — axis ``creation``, deliberately NOT
        ``posting_date``. A row can carry any ``posting_date`` (backdated, corrected, whatever the
        business needs) while ``creation`` pins exactly when it actually landed on the bench; a
        reconciliation sweep organized around when work happened, not what date it claims, has to
        read the axis that can't be backdated. ``since``/``until`` are ERPNext-clock frappe-format
        datetime strings (``YYYY-MM-DD HH:MM:SS[.ffffff]``) supplied by the caller — this method
        invents, defaults, or reformats no clock; that is the glue layer's job, not this client's.

        GL-entries-shaped (:meth:`get_gl_entries` is the template): explicit ``fields``/
        ``filters`` JSON, ``limit_page_length: "0"`` (F-V1 law — a gate-feeding LIST read must pin
        the full page, never rely on frappe's default-20 truncation), structured-deny-on-non-list-
        body before any caller loop ever sees a row.

        **Row validation — wider than get_gl_entries' because this feeds classification, not just
        disclosure:**

        * ``account`` — non-blank str (else raise): the accounting content, same as get_gl_entries.
        * ``debit``/``credit`` — finite non-bool number (``math.isfinite``, the same NaN-defense
          get_gl_entries/check_allocation/consent/prove already apply): a malformed amount must
          refuse, never read as zero.
        * ``is_cancelled`` — a clean ``int`` (``0``/``1``), never a bool, never absent (else
          raise): the governed/cancel classification downstream keys off this value directly — a
          malformed one silently read as ``0`` would misclassify a cancelled row as live.
        * ``voucher_type``/``voucher_no`` — non-blank str (else raise): the grouping key the sweep
          is organized around; a blank one is an unusable movement record.
        * ``creation``/``owner`` — non-blank str (else raise): the join anchors — back to the
          sweep window, and to who wrote the row; blank is unverifiable.
        * ``posting_date``/``modified``/``modified_by``/``party_type``/``party`` —
          disclosure-only, legitimately blank on real rows (a Cash-account row typically carries
          no party) — deliberately NOT validated; a null/absent value here is tolerated.

        Raises on an unreadable response, same as every read in this client — the CALLER decides
        what an unreadable sweep means for the Close, not this method.

        KNOWLEDGE-PINNED, NOT LIVE-VERIFIED (this module's standing residual, SPEC §7): the
        ``creation`` filter axis and full field list are confirmed against GL Entry's own
        universal fields (``creation``/``owner``/``modified``/``modified_by`` on every doctype;
        ``is_cancelled``/``party_type``/``party`` already proven live via get_gl_entries/
        get_settling_references' siblings) — not against a live creation-window sweep response."""
        params = {
            "fields": json.dumps(["voucher_type", "voucher_no", "account", "debit", "credit",
                                  "posting_date", "creation", "owner", "modified", "modified_by",
                                  "is_cancelled", "party_type", "party"]),
            "filters": json.dumps([["company", "=", company],
                                   ["creation", ">=", since],
                                   ["creation", "<=", until]]),
            "limit_page_length": "0",
        }
        rows = self._data(self._call("GET", "/api/resource/GL%20Entry", params=params))
        if not isinstance(rows, list):
            raise ErpnextError(
                "GL Entry creation-window sweep returned a non-list body; cannot verify the "
                "sweep, refusing")
        for row in rows:
            if not isinstance(row, dict):
                raise ErpnextError(
                    "GL Entry sweep row is malformed (not an object); cannot verify the sweep, "
                    "refusing")
            account = row.get("account")
            if not isinstance(account, str) or not account.strip():
                raise ErpnextError(
                    f"GL Entry sweep row has a malformed/missing account ({account!r}); an "
                    "unverifiable row refuses, it never reads as valid")
            for field in ("debit", "credit"):
                value = row.get(field)
                if not isinstance(value, (int, float)) or isinstance(value, bool) \
                        or not math.isfinite(value):
                    raise ErpnextError(
                        f"GL Entry sweep row, account {account!r}, has a malformed {field} "
                        f"({value!r}); an unverifiable amount refuses, it never reads as zero")
            is_cancelled = row.get("is_cancelled")
            if not isinstance(is_cancelled, int) or isinstance(is_cancelled, bool) \
                    or is_cancelled not in (0, 1):
                raise ErpnextError(
                    f"GL Entry sweep row, account {account!r}, has a malformed is_cancelled "
                    f"({is_cancelled!r}); a malformed value must never read as 0 (live), refusing")
            for field in ("voucher_type", "voucher_no"):
                value = row.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ErpnextError(
                        f"GL Entry sweep row, account {account!r}, has a malformed/missing "
                        f"{field} ({value!r}); the grouping key must be usable, refusing")
            for field in ("creation", "owner"):
                value = row.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ErpnextError(
                        f"GL Entry sweep row, account {account!r}, has a malformed/missing "
                        f"{field} ({value!r}); the join anchor must be verifiable, refusing")
        return rows

    def get_reposts(self, company, since, until):
        """Fork II — the Repost Accounting Ledger read. Explains a Fork-IV second generation: a
        repost re-derives a document's GL rows in place (same ``voucher_no``, freshly-written GL
        rows) — this is how the sweep's glue tells "the bench silently regenerated this voucher's
        ledger" apart from "a brand-new voucher landed in the window".

        Two-step, LIST then per-doc GET — the same shape :meth:`get_period_locks`/
        :meth:`get_active_workflows` already use, because the LIST endpoint never expands child
        tables:

        1. LIST ``/api/resource/Repost%20Accounting%20Ledger`` filtered to ``company`` + the
           creation window (``since``/``until`` — the same ERPNext-clock frappe-format datetime
           strings as :meth:`sweep_gl_entries`; this method invents no clock either), fields
           ``["name", "owner", "creation", "docstatus"]``, ``limit_page_length: "0"`` (F-V1 law),
           structured-deny-on-non-list-body, per-row validation (``name``/``owner``/``creation``
           non-blank str, ``docstatus`` a clean int — else raise) before any per-doc GET is issued.
        2. For each surviving hit, a full-document GET (:meth:`get_document`, reused rather than
           reimplemented) reads the ``vouchers`` child table: each child's ``voucher_type``/
           ``voucher_no`` non-blank str, else raise. A repost doc with no ``vouchers`` key or an
           empty list simply names no vouchers — ``[]``, not an error (a repost that touched
           nothing is a real, valid shape).

        **This read may be permission-locked** (Repost Accounting Ledger can be SysMgr-gated on a
        real bench) — this method itself just raises honestly on an unreadable/403 response, like
        every read in this client. Whether an unreadable repost read should be a non-fatal FLAG
        (the reconciliation still runs, just without repost attribution) or a whole-plan refusal
        is the CALLER's decision, made at the glue layer — never here.

        Returns ``[{"name", "owner", "creation", "docstatus",
        "vouchers": [{"voucher_type", "voucher_no"}, ...]}, ...]`` — each voucher dict carries
        only those two keys, never the raw child row's frappe bookkeeping fields (``idx``/
        ``parent``/``parenttype``/``parentfield``/the child row's own ``name``), which are not
        this read's concern.

        KNOWLEDGE-PINNED, NOT LIVE-VERIFIED (this module's standing residual, SPEC §7): Repost
        Accounting Ledger's ``vouchers`` child table shape (``voucher_type``/``voucher_no`` per
        row) is confirmed against ERPNext source, not against a live bench response."""
        params = {
            "fields": json.dumps(["name", "owner", "creation", "docstatus"]),
            "filters": json.dumps([["company", "=", company],
                                   ["creation", ">=", since],
                                   ["creation", "<=", until]]),
            "limit_page_length": "0",
        }
        rows = self._data(self._call(
            "GET", "/api/resource/Repost%20Accounting%20Ledger", params=params))
        if not isinstance(rows, list):
            raise ErpnextError(
                "Repost Accounting Ledger list read returned a non-list body; cannot verify "
                "reposts, refusing")
        reposts = []
        for row in rows:
            if not isinstance(row, dict):
                raise ErpnextError(
                    "Repost Accounting Ledger list row is malformed (not an object); cannot "
                    "verify reposts, refusing")
            for field in ("name", "owner", "creation"):
                value = row.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ErpnextError(
                        f"Repost Accounting Ledger list row has a malformed/missing {field} "
                        f"({value!r}); refusing")
            docstatus = row.get("docstatus")
            if not isinstance(docstatus, int) or isinstance(docstatus, bool):
                raise ErpnextError(
                    f"Repost Accounting Ledger {row['name']!r} has a malformed docstatus "
                    f"({docstatus!r}); refusing")
            full = self.get_document("Repost Accounting Ledger", row["name"])
            if not isinstance(full, dict):
                raise ErpnextError(
                    f"Repost Accounting Ledger {row['name']!r} unreadable or malformed — "
                    "refusing")
            voucher_rows = full.get("vouchers")
            if voucher_rows is None:
                voucher_rows = []
            if not isinstance(voucher_rows, list):
                raise ErpnextError(
                    f"Repost Accounting Ledger {row['name']!r} has a malformed vouchers child "
                    "table; refusing")
            vouchers = []
            for child in voucher_rows:
                if not isinstance(child, dict):
                    raise ErpnextError(
                        f"Repost Accounting Ledger {row['name']!r} has a malformed vouchers "
                        "child row (not an object); refusing")
                voucher_type = child.get("voucher_type")
                voucher_no = child.get("voucher_no")
                if not isinstance(voucher_type, str) or not voucher_type.strip():
                    raise ErpnextError(
                        f"Repost Accounting Ledger {row['name']!r} has a vouchers child row "
                        f"with a malformed/missing voucher_type ({voucher_type!r}); refusing")
                if not isinstance(voucher_no, str) or not voucher_no.strip():
                    raise ErpnextError(
                        f"Repost Accounting Ledger {row['name']!r} has a vouchers child row "
                        f"with a malformed/missing voucher_no ({voucher_no!r}); refusing")
                vouchers.append({"voucher_type": voucher_type, "voucher_no": voucher_no})
            reposts.append({"name": row["name"], "owner": row["owner"],
                            "creation": row["creation"], "docstatus": docstatus,
                            "vouchers": vouchers})
        return reposts
