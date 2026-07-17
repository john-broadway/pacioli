# Cascade Cancel — design

**Status:** approved (2026-07-03). Implementation pending.
**Scope:** one implementation plan. Adds a governed `cascade_cancel` op to the broker.

## Problem

Today the broker governs a **leaf-node** cancel only. `plan_cancel` calls
`get_submitted_linked_docs` and, the moment any submitted document links to the target, it refuses —
naming the links and telling the human to "cancel those first (cascade cancel is not governed in this
slice)". This mirrors ERPNext, which itself will not cancel a document that still has submitted
dependents. So unwinding a real posting — a Sales Invoice with a Payment Entry and a Delivery Note
hanging off it — is impossible through the broker: the human must drop to the ERPNext desk and cancel
each dependent by hand, ungoverned.

Cascade cancel closes that gap: the broker cancels a document **and** its submitted dependent graph,
in dependency order, under the same PLAN → CONSENT → PROVE spine — without ever taking an irreversible
action the human did not explicitly authorize.

## Non-goals (YAGNI)

- **No un-cancel / rollback.** An ERPNext cancel is irreversible; a cascade cannot be "rolled back".
- **No auto-resubmit.** Re-creating corrected drafts after a cascade is `amend`, a separate op.
- **No parallelism.** Dependencies force a strict cancel order; the cascade is sequential.

## The three decided keystones

1. **Consent = one marker over a frozen, fully-enumerated graph.** `plan_cascade_cancel` walks the
   full transitive dependent closure, topologically sorts it, and pins the exact ordered list into
   **one** plan. A single human-minted marker authorizes **exactly that frozen list**. The human sees
   every document and the order before minting. This preserves the core principle — the human consents
   to a specific, fully-enumerated irreversible action — with the "action" now a named set, not one doc.
   (Rejected: a marker per doc — operationally brutal, no added safety over an enumerated plan; a root
   marker with auto-descend — the machine acting on documents the human never saw.)

2. **Partial failure = preflight-all, then fail-stop.** A cascade is **non-atomic** at the ERPNext
   layer (each cancel is its own committed transaction; there is no cross-doc transaction). Before
   touching any document, validate every check the broker *can* make statically. Then execute in
   order; if a document still fails mid-run, **stop immediately**, record precisely what was cancelled
   and where it stopped, and never continue past the failure.

3. **Graph doctype scope = any doctype, labeled coverage.** The cancel path is already fully
   doctype-generic (`get_document`, `get_period_locks`, `cancel` via `run_method`, `get_gl_entries` by
   `voucher_type`, `get_submitted_linked_docs`). `SUPPORTED_DOCTYPES` gates only *submit* (party-field
   / preview specifics). So a cascade may cancel **any** doctype in the graph via the generic verb;
   every node still gets the doctype-agnostic guarantees (freshness, period-lock red-line, reversing-GL
   preview, ERPNext's own cancel-blocks). The plan **labels** each node `modeled` (Sales/Purchase
   Invoice) vs `generic` (cancelled via the generic path, relying on ERPNext's blocks + the generic
   checks), so the coverage depth is honest and visible.

## Architecture

A third op, `cascade_cancel`, alongside `submit` / `cancel`. It reuses the existing pillars and cores;
it does **not** fork the spine's security ordering. Two new tools, two new cores, one client helper.

### New op and plan shape

- `plan.op` gains a third legal value, `"cascade_cancel"`. `check_op` refuses a cascade marker on a
  plain submit/cancel and vice versa — the existing cross-op binding, unchanged.
- A cascade plan pins an **ordered list of nodes** rather than a single `(doctype, docname)`. Each
  node records: `doctype`, `docname`, `doc_version` (the doc's `modified`), `posting_date`, `company`,
  `coverage` (`"modeled"` | `"generic"`), and its projected reversing-GL rows. The plan's top-level
  `doctype`/`docname` remain the **target** (the last node cancelled), so existing plan-store shape and
  `check_docname`/`check_doctype` against the target still hold; the ordered graph lives in a new plan
  field (`graph`, a list). `projected_reversal` becomes the per-node GL rolled up for display.

### Graph discovery — `plan_cascade_cancel(name, pacioli_doctype=...)` (read-only)

1. Load the target; refuse unless `docstatus == 1`; company/books pin check (existing `_tool_plan_cancel`
   logic).
2. **Transitive dependent closure.** BFS from the target: for each discovered node, call
   `get_submitted_linked_docs(doctype, name)`; add unseen submitted dependents; repeat until no new
   nodes. `get_submitted_linked_docs` already raises on an unreadable graph — an unreadable node at
   **any** depth refuses the whole cascade (never read as empty). Deny-biased throughout.
3. **Cycle guard.** If BFS revisits a node via a back-edge that would violate acyclicity, refuse naming
   the node. (ERPNext dependency graphs are DAGs; a cycle is a fail-closed refusal, not a guess.)
4. **Size cap.** Refuse if the closure size exceeds `PACIOLI_CASCADE_MAX` (**default 25**,
   env-configurable via `runtime`), naming the count — an unexpectedly huge unwind is a refusal, not a
   silent mega-cancel.
5. **Topological sort.** Order so every dependent precedes the document it depends on; the **target is
   cancelled last**. Ties broken deterministically (e.g. by `(doctype, docname)`) so the plan is stable.
6. **Static preflight (see below).** If any node fails a static gate, refuse the whole plan — never
   record a plan (and thus never let a marker be minted) for a cascade that cannot even start cleanly.
7. Record the plan (`op="cascade_cancel"`, the ordered `graph` pinned). Return the ordered list with
   per-node coverage labels + reversing GL + the total, plus a one-line note stating the preflight limit
   (below) so the human is not misled into thinking a minted marker guarantees completion.

### Workflow-SoD across the graph (a required gate — added 2026-07-03 after review)

The single-op cancel path (`_governed_write`) refuses a cancel that a company's active ERPNext
**Workflow** governs — `workflow.check_submit_gate(workflow.find_active(get_active_workflows(dt)),
"cancel")`, deny-biased (ambiguous / malformed / unreadable config all refuse). A cascade must honor
that gate for **every** node, or it becomes a laundering path around Separation-of-Duties: a document
whose cancel `cancel_sales_invoice` refuses could be cancelled through `cascade_cancel`. (This gap was
caught in review — the original spec omitted Workflow from the cascade path entirely.)

Rule: in `plan_cascade_cancel`, run the same cancel-gate for every node's doctype (cached per
doctype — the gate governs a doctype, not a document). If **any** node is workflow-governed for
cancel (or its config is ambiguous/malformed/unreadable), **refuse the whole cascade** at
`stage="workflow"`, naming the node, the workflow, and the approving role — and point the human at
`request_workflow_transition` for that document. Re-run the same gate at `cascade_cancel` execute
time (TOCTOU, exactly as the single-op path checks at execute). A cascade is thus governed by the
union of all its nodes' gates; it never weakens any single node's governance.

### Wrong-books pin across the graph (a required gate — added 2026-07-03 after final review)

The single-op cancel path refuses a document whose `company` differs from the pinned target's company
(the "wrong books" refusal — the framework's standing invariant that a target's books are structurally
unreachable). A dependent graph can legitimately span companies (ERPNext supports inter-company
documents on one site behind one credential — internal Sales/Purchase Invoice pairs, inter-company
Journal Entries). So the cascade must apply the same pin to **every** node, or it launders a
cross-company cancel: a company-A-pinned target's cascade could cancel a company-B dependent under the
one marker, with the only remaining protection being the human noticing the `company` field in the
returned graph — consent-by-inspection, the exact weakening the mechanical pin exists to prevent.
(This gap was caught in the final whole-branch review — the first cut pinned only the target node.)

Rule: when `target.company` is set, `plan_cascade_cancel` refuses the whole cascade (naming the node)
if **any** node's `company` differs — plan-time suffices because per-node freshness pins every node to
its planned version, but a one-loop belt re-check on the rebuilt graph runs at `cascade_cancel` execute
time too, for symmetry with the Workflow-SoD gate.

### Preflight — honest about ERPNext's limits

ERPNext has **no true dry-cancel**. Preflight is therefore *every check the broker can make without
mutating*: `docstatus == 1` on each node, freshness (each node's `modified` matches the plan),
period-lock red-line on each node (`posting_date` vs the company's `get_period_locks`), graph
readability, cycle, and the size cap. This catches the common blocker — a locked accounting period —
while everything is still reversible. ERPNext-internal blocks that only surface when `cancel` actually
runs (e.g. a reconciled Payment Entry, a linked document ERPNext guards at cancel time) **cannot** be
detected statically; those surface at execute and trigger fail-stop. The plan output and the README
state this plainly; nothing claims preflight guarantees the cascade completes.

### Execution — `cascade_cancel(name, plan_id, marker)`

Governed by the existing spine discipline, extended to a list:

1. Resolve the cascade plan; `check_docname`/`check_op`/`check_doctype` against the **target**
   (existing gates). Refuse a non-cascade plan.
2. **Re-discover the graph** and refuse if it changed in **any** way since the plan — a new submitted
   link appeared (grew), or a node is gone / already cancelled out-of-band (shrank). The frozen set the
   human consented to must still be exactly the set on the bench. Any drift is a stale-plan refusal →
   re-plan, consistent with the freshness discipline (an out-of-band cancel also bumps that node's
   `modified`, so per-node freshness would reject it anyway; catching it here as a whole-graph mismatch
   gives the clearer message). Strict by construction — no "skip the already-done" leniency to reason
   about.
3. **Preflight every node BEFORE consent and BEFORE any cancel** — this is the design's approved shape
   (`run_cascade`), not a per-node interleave. Loop the whole graph checking each node's freshness (its
   `modified` unchanged since plan) and its period-lock red-line; if ANY node fails, refuse the whole
   cascade recording nothing and leaving the marker untouched (a clean no-op — gates precede consent,
   exactly like the single-op spine). This is what stops a locked node #2 from stranding an
   already-cancelled node #1. (Freshness is anchored to the plan's frozen versions; an in-loop re-check
   would false-fail the target, whose `modified` is bumped the instant a dependent is cancelled.)
4. **Reserve + CAS-claim the marker once**, only after the whole graph preflights clean — closing the
   double-execute race exactly as the single-op spine does. Then **for each node in topological order:**
   a. write a per-node **intent** receipt, tagged `cascade_id` (the plan_id) + `seq` (position) +
      `doctype`/`docname`/`coverage`.
   b. `cancel_document(doctype, docname)` → docstatus 1 → 2.
   c. write the per-node **committed** outcome (with `final_marker` set only on the terminal node).
   d. **on any cancel failure at this node:** write the node's **failed** outcome (its intent stays an
      orphan for reconciliation), settle the marker (below), **STOP**, do not touch later nodes.
5. **Settle the marker** (exactly once, on the terminal outcome): `committed` iff **≥ 1** node was
   cancelled; `released` iff **0** were
   (a clean no-op failure on the first node — identical to today's single-op release). A partial spends
   the marker: it authorized real irreversible work, so releasing it would be dishonest; the human
   re-plans + re-mints for the remaining nodes (the graph genuinely changed).

### Result shape

```
{ "ok": <true iff ALL N cancelled>,
  "cancelled": [ {doctype, docname, seq}, ... ],   # nodes 1..k that reached docstatus 2
  "stopped_at": { "doctype", "docname", "seq", "reason" } | null,   # present iff partial
  "total": N,
  "cascade_id": <plan_id> }
```

`ok` is true **only** if every node cancelled. A partial is `ok:false` with the exact boundary — the
truthful picture, never a smoothed "success".

### PROVE / receipts

Per-node intent+outcome pairs, each carrying `cascade_id` + `seq`, appended to the same hash-chained
ledger. `prove_verify` sees the cascade as a linked run; a full cascade of N nodes adds `2·N` receipts
with zero orphans; a run that cancels `k` nodes and then fails on the next adds `2·k + 2` receipts
(the `k` committed intent/outcome pairs, plus the failed node's own intent **and** its `failed`
outcome), of which exactly **one** — the failed node's intent — is an orphan (only a `committed`
outcome finalizes an intent), caught by `prove_orphans`. Honest and reconcilable; no cascade-level
receipt is invented — the run is fully described by its per-node pairs plus the shared `cascade_id`.

## Components (units, each independently testable)

| Unit | Where | Responsibility | Depends on |
|---|---|---|---|
| graph discovery + topo-sort + cycle/cap | new `pacioli/cascade.py` (pure) | build the ordered node list from a link-fetcher callback; cycle guard; cap; deterministic sort | injected `linked_docs(dt,name)` fetcher (pure/testable) |
| cascade execution core | `pacioli/spine.py` (extend) or `pacioli/cascade.py` | drive per-node fresh→red-line→intent→execute→outcome, fail-stop, marker settle rule | existing `consent`, `check_fresh`, `check_red_line`, injected effects |
| plan model | `pacioli/plan.py` (extend) | `op="cascade_cancel"`; `graph` field; `check_op` third value | — |
| tools | `pacioli/tools.py` | `_tool_plan_cascade_cancel`, `_tool_cascade_cancel`, register in `tool_names()` | cascade core, erpnext client, store |
| client (reuse) | `pacioli/erpnext.py` | already generic: `get_submitted_linked_docs`, `get_gl_entries`, `cancel_document`, `get_period_locks` | — |
| cap config | `pacioli/runtime.py` | `PACIOLI_CASCADE_MAX` (default 25) | env |

Keeping graph discovery pure (a `linked_docs` callback in, an ordered node list or a structured refusal
out) means the hard logic — closure, cycle, cap, topo order — is unit-tested with no bench, mirroring how
`plan.py`/`spine.py`/`consent.py` are already pure cores with the glue in `tools.py`/`runtime.py`.

## Testing

- **Pure unit (no bench):** closure over a fixture link-graph; cycle → refuse; cap exceeded → refuse
  naming count; topo order (dependents before parents, target last, deterministic ties); unreadable
  node → refuse (never empty); graph-changed-since-plan (grew OR shrank) → refuse; fail-stop leaves exactly the right
  committed/orphan receipt shape; marker committed-iff-≥1 / released-iff-0; `ok` true iff all N.
- **Bench live-proof (a future gate, à la PHASE H/I):** a real SI with a Payment Entry + Delivery Note
  dependent → `plan_cascade_cancel` enumerates the ordered graph with coverage labels + reversing GL;
  a minted marker cancels the whole graph child→…→target (all docstatus 2, reversing GL in the bench);
  a period-locked node → whole plan refused untouched (preflight); a mid-cascade ERPNext block → fail-stop
  with the precise boundary and a partial ledger; `prove_verify` shows the linked run.

## Open item deferred to implementation

- Exact topo-sort tie-break key and whether `graph` stores full edges or just the ordered node list
  (ordered list suffices for execution; edges only matter for display) — settle in the plan.
