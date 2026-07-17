# Pacioli Broker — slice-one spec (governed Sales Invoice submit)

> **Status: the slice-one design doc (2026-07-01), since SUPERSEDED by the shipped system.** It
> captured the thin first vertical — one DocType (Sales Invoice), one governed write — sitting on top
> of `pacioli_guard`'s credential-scope floor, sharpened by the trust-spine pattern (the same spine
> Proximo expresses for infra) and a Sonnet review that found real holes. Four doctypes,
> cascade/amend, the workflow-SoD gate, governed reconciliation, and the Close have shipped and been
> live-proven since; the current surface lives in `../README.md`, `README.md#honest-scope`, and
> `../SCOPED-TOKEN-PROOF.md`. Kept for its design rationale. Publishing still waits for John's go.

Named for Luca Pacioli — double-entry as a system of *provable* controls over records. The broker
lets an AI act on the ledger without ever breaking the trail or the balance.

---

## 1. What slice-one is

A standalone, pip-installable **MCP server** (stdio transport, runs on the user's machine) that talks
to an ERPNext bench over its documented REST/RPC surface. It is **not** a bench app — that's `pacioli_guard`.
Two artifacts under one `pacioli/` repo:

- `guard/` — installed *into* the bench; the credential floor (built, live-proven).
- `broker/` — runs *outside* the bench; the governed broker (this spec).

Slice-one governs exactly one write — **submitting a Sales Invoice** — through the full shape
`PLAN → CONSENT → execute → PROVE`, plus a minimal read tier. Everything else is deny-by-default.

## 2. The hard rule that makes the model valid

The broker authenticates to ERPNext as a **scoped, least-privilege user that holds neither
Administrator nor System Manager**. The literal **Administrator** user (and bench / DB access)
bypasses *every* ERPNext permission check outright (frappe keys that bypass to the username, not to
any role); **System Manager** holds no runtime bypass but can administer the governance *away* over
the REST surface — write Custom DocPerm rows, mint API keys, run arbitrary code via the System
Console — so running as either would void the model the broker relies on. This is not optional and
not deferrable; it is the precondition for every pillar below, and `pacioli doctor`'s roles probe
enforces it at readiness time.

**And the broker binds itself to the floor (fractal sovereignty).** The broker is sovereign in its
scope *and* bound by the law: its own ERPNext credential is **`pacioli_guard`-scoped** to exactly the
{read, preview-method, submit-method} calls it needs. Without this, anything holding the broker's raw
credential (the agent's own shell reading the target registry, a compromised MCP client, a
supply-chain-tampered broker) can `curl` ERPNext's REST API directly and post an invoice with **zero**
PLAN, CONSENT or PROVE — the exact bypass Pacioli's whole pitch is against. So:

- **Slice-one requires the broker's credential be `pacioli_guard`-scoped.** Not "recommended later."
- If a deployment genuinely runs the broker un-scoped, the README must say, in plain words: *the trust
  spine is bypassable by anyone holding the raw credential until `pacioli_guard` is installed and scoping it.*

This is composition-by-law, not product coupling: the broker honours the same floor it sits on. `pacioli_guard`
remains independently useful for any credential (agent, Zapier, cron, vendor); the broker is one
consumer that chooses to bind itself.

## 3. The five pillars in slice-one — pattern shape → ERPNext mechanism → what's deferred

| Pillar | Slice-one shape (the pattern's answer) | Rides (ERPNext) | Deferred |
|---|---|---|---|
| **PLAN** | Full. Load the draft (`docstatus=0`), run `validate`, return the projected GL impact + risk flags, record the plan. Bind the plan to the doc's `modified` version (TOCTOU). | native `show_accounting_ledger_preview` (savepoint → `make_gl_entries` → rollback) | linked-master drift (credit limit, tax template, FX) — documented, not silently covered |
| **CONSENT** | The **marker**: a single-use, out-of-band, human-minted grant the agent *cannot* derive. A state machine `live→reserved→consumed` (or `→live` on failure): submit **reserves** the marker and the glue **CAS-claims it before execute**, so a concurrent submit can't double-fire on one grant. **Not** a `confirm` flag the agent sets on its own call. | out-of-band grant (ported from Proximo's `consent.py`); atomic CAS claim | full ERPNext **Workflow**-engine SoD (`apply_workflow`, `has_approval_access`) → increment 3 |
| **PROVE** | Append-only, hash-chained, HMAC-keyed receipt of every mutation, with a head seam built for **off-box pinning**. Pending-then-finalize across the execute boundary (crash gap). | own independent ledger, alongside ERPNext's own Version/Audit-Trail | **the off-box anchor itself** (the seal above the machine) → increment 2. *Until it lands, PROVE is NOT called "tamper-evident."* |
| **DIAGNOSE** | Minimal read: get / list Sales Invoice, permission-scoped, confidentiality-flagged. | `frappe.get_all` / REST under the caller's `has_permission` | broader read tier, health/evidence tools |
| **UNDO** | Not shipped. No `delete` on submitted docs, ever. | — | graph-aware cancel+amend, fail-closed at the reversibility edges → increment 4 |

**The closed books.** Where ERPNext has closed the books — a closed Accounting Period, a Period-
Closing-Voucher boundary, a company `accounts_frozen_till_date` — the broker **refuses and says so**.
It must **never** set `adv_adj=True` and must **never** pass a **future `posting_date`** to slip an
entry past the ruling-off (all three date-gates are pure `posting_date`-range checks anchored to a
*stored* date, not "now" — a future date silently escapes them). The machine does not write in a
closed book by its own hand: it refuses and escalates instead of forcing past a block.

**CONTAIN** (rate + blast-radius + live-revoke) is named across the spine but **not built** in
slice-one. Its ERPNext shape is a timed, live revocation of the broker's credential. Noted for the
envelope; out of scope here.

## 3a. CONSENT addendum — the Workflow-SoD gate (2026-07-03, live-proven against a v16 bench)

Increment 3 (§8) lands as a **two-gate model**, not a replacement of the marker:

- **Gate one — the marker (§3 CONSENT row, unchanged).** Out-of-band human consent, single-use,
  CAS-claimed before execute. Governs every submit/cancel regardless of whether a company has
  configured a Workflow.
- **Gate two — Workflow-SoD** (`broker/pacioli/workflow.py`, wired into `tools.py`'s
  `_governed_write`). When a company has configured an **active ERPNext Workflow** on the
  doctype, that workflow becomes the company's own separation-of-duties law — **ridden, never
  replaced or invented on top of**:
  - `submit` is refused whenever *any* active workflow governs the doctype, naming the workflow
    and the approving role(s); the broker may still perform **non-approving** transitions itself
    (`request_workflow_transition`) — belt (the broker refuses the approving move) and suspenders
    (frappe's own `has_approval_access` / role check on `apply_workflow`).
  - `cancel` is refused only when the company's own workflow configuration maps a state to
    `doc_status "2"` — otherwise cancel stays marker-governed exactly as today; the broker governs
    by what the company configured, never by assuming a duality frappe doesn't itself enforce.
  - No workflow configured on the doctype = this gate passes silently; both directions stay
    marker-governed exactly as before this increment.
  - More than one active workflow found for one doctype is treated as **ambiguous configuration**
    (frappe does not verify one-active-workflow-per-doctype as enforced) — a deny-biased refusal
    naming every workflow found, never "pick the first."

**Honest limits, stated exactly (mirrors `pacioli/workflow.py`'s module docstring):**
1. **Frappe does not enforce Workflow on a direct `docstatus` change** — `validate_workflow` only
   fires when the document's `workflow_state` field itself changes on save, so a plain submit
   that never touches that field passes frappe with nothing but the generic DocType submit
   permission. This gate is therefore the **only** thing stopping *this broker* from submitting
   around a company's configured approval chain; it does not and cannot make ERPNext itself
   enforce the workflow against any other calling path (the bench admin console, a script, a
   report button, a different integration). Bench-side (guard-side) enforcement of Workflow
   against every calling path remains a separate future increment, not implied by anything here.
2. **Self-approval is not blocked by frappe by default** — `allow_self_approval` on a Workflow
   Transition defaults to `"1"`; `has_approval_access` denies self-approval only when the
   transition explicitly sets it falsy AND the user isn't Administrator. This gate does not
   refuse a self-approvable configured workflow (CONSENT enforces the company's own rules, it
   does not invent stricter ones) — `workflow_status` and `plan_submit`'s risk flags surface it
   honestly (`sod_report`) instead of silently reading "workflow-governed" as "separated duties."
3. **Every frappe REST shape this gate depends on was read from source (frappe/frappe branch
   version-15) and then live-proven against a real ERPNext v16 bench (Gate 5, 2026-07-02).**
   `apply_workflow`'s contract (only `doctype`/`name` matter in the body; the server reloads from
   db), the `Workflow`/`Workflow Document State`/`Workflow Transition` fieldnames, `Workflow`
   being System-Manager-read by default, `allow_self_approval` defaulting to `"1"`, and the
   HTTP-417 mapping for the self-approval refusal were all introspected on the live v16 bench and
   held. The eight-leg proof (a valid marker still refused at the workflow stage; a non-approving
   transition performed and the approving one refused broker-side; frappe's own 417 self-approval
   block on a direct `apply_workflow`; cancel not over-blocked when unmapped to `doc_status "2"`;
   the chain verifying with zero orphans; `pacioli doctor`'s probe FAIL→PASS across the grant) is
   recorded in `SCOPED-TOKEN-PROOF.md` PHASE G.

## 3b. Breadth addendum — Purchase Invoice (2026-07-03, built, NOT live-verified)

Slice-one named "one DocType (Sales Invoice)" as the deliberate first cut (§1); this addendum
generalizes the tool surface to a second doctype — **Purchase Invoice** — without weakening any
gate. Every mechanism in §2–3a already generalized cleanly from ERPNext source: submit/cancel ride
the same doc-method surface for any doctype, `show_accounting_ledger_preview` already takes
`doctype` as a plain argument, period locks are company-level (doctype-independent), and amend's
resource-CRUD shape is per-doctype by construction. Nothing here is a new mechanism — it is the
existing five pillars threaded through a second `doctype` value.

**Shape.** The five SI-baked tool handlers became one generic implementation each
(get/list/submit/cancel/amend "document"), with the five existing `*_sales_invoice` tools kept as
thin wrappers (behaviour unchanged; the `submit`/`cancel` *descriptions* gained one clause noting
the new cross-doctype refusal) and five new `*_purchase_invoice` siblings wrapping the same
handlers — no duplicated logic, per the accepted design (rejected alternative: duplicating
handler *logic* per doctype). The four generically-named doc-scoped tools (`plan_submit`,
`plan_cancel`, `workflow_status`, `request_workflow_transition`) gained an optional
`pacioli_doctype` argument, default `"Sales Invoice"` (today's behaviour unchanged when omitted).

**The security-critical addition.** A `Plan` is now bound to a `doctype`, exactly as it is already
bound to a `docname` and an `op` (§3's TOCTOU + the cross-op guard). `pacioli/plan.py`'s new
`check_doctype(plan, doctype)` is wired into `_governed_write` alongside `check_docname`/
`check_op`, before the Workflow-SoD gate and the spine: a plan built for Sales Invoice can never
authorize a Purchase Invoice submit/cancel, or vice versa, even with an otherwise-valid marker in
hand. This is the direct extension of the closed-books/wrong-books discipline (§2, §4) to a second axis
of "which books" — not just *whose* company, now also *which* document type.

**The broker's own doctype allowlist.** `pacioli/erpnext.py`'s `SUPPORTED_DOCTYPES` (Sales
Invoice, Purchase Invoice — each with its `party_field`) is the broker's own "built and tested for
these" gate, checked at the tool layer before any network call. It is distinct from, and
belt-and-suspenders alongside, `pacioli_guard`'s per-credential `resource_doctypes` grant — either
layer refusing is sufficient; neither substitutes for the other. Nothing here changes the guard.

**Genuine per-doctype differences, threaded rather than assumed:** the list tier's counterparty
field (`customer` vs `supplier`); the GL-entries read now filters on `voucher_type` as well as
`voucher_no` (closing a latent cross-doctype read gap once two doctypes can share one `GL Entry`
table); `pacioli doctor`'s workflow-read probe now checks Workflow-DocType readability
independently per supported doctype (a company's Role Permission Manager grant can differ per
doctype on a live bench).

**Honest limits.** *(As written at the addendum's date. Limit 1 has since been discharged: the
Purchase Invoice vertical was live-proven at Gate 6, 2026-07-03, and regression-re-proven at the
Gate 10 close-out, 2026-07-07 — `SCOPED-TOKEN-PROOF.md`.)*
1. **Not verified against a live bench.** Every Purchase Invoice request shape here is
   knowledge-pinned from ERPNext's documented REST conventions — the same generic surface Sales
   Invoice already rode before Gate 5 (§3a) confirmed it against a real bench. It has not itself
   been exercised against a bench. Live falsification of the Purchase Invoice path is a distinct,
   future bench gate — this addendum does not borrow the Sales Invoice bench confirmation and
   apply it here by association.
2. **Every receipt carries `doctype`.** The submit/cancel intent body is built inside
   `spine.governed_submit`, which records `plan.doctype` on the intent — a one-line, doctype-agnostic
   addition (the spine records another field the plan already carries, exactly like `docname`); the
   outcome's `result` carries it too, as do the amend and workflow-transition receipts this module
   builds directly. The ledger is self-describing on both intent and outcome for every doctype, and
   the doctype is *also* recoverable via `plan_id` from the doctype-columned plan store. None of this
   is the gate — `check_doctype` enforces independently of what any receipt records; PROVE is audit,
   not enforcement.

## 3c. Breadth addendum — Payment Entry + Journal Entry (2026-07-06, built, NOT live-verified)

§3b generalized the tool surface to a second doctype riding the existing five pillars unchanged.
This addendum adds a **third and fourth** doctype — Payment Entry and Journal Entry — using the
same recipe (`SUPPORTED_DOCTYPES` gains an entry, five thin wrapper tools reuse the existing
generic handlers, no duplicated logic; **18 → 23 → 28 tools**) — and, for the first time in this
breadth story, one genuinely **new** mechanism, not only existing gates threaded through a new
doctype value.

**A cross-cutting correctness fix landed the same increment, not doctype-specific.**
`get_period_locks`'s frozen-till-date read now matches what §3's closed-books row always *named*
(`accounts_frozen_till_date`) but the implementation had drifted from: it read only the legacy
`Accounts Settings.acc_frozen_upto` (a v15 field ERPNext v16 migrated off entirely), so the lock
was silently always-absent against a v16 bench for **every** doctype already shipped, not just the
two landing here. It now reads `Company.accounts_frozen_till_date` too and honors whichever date
is later. **BREAKING:** a new `Company` read grant (README Preconditions; `pacioli doctor` gained
`probe_company_read`, same 403-is-FAIL inversion as the Workflow-read probe).

**Payment Entry** has exactly one header-level party field (`party_field="party"`; an Internal
Transfer payment carries no party at all — surfaced as an absent field like any other doctype's
missing value, not a special case). Two advisory disclosures thread the existing risk-flag/
response shape, both read from the draft's own cached `references` child rows: `plan_submit`
flags a nonzero `exchange_gain_loss` reference (ERPNext's own ledger-preview call creates AND
submits a real, separate Journal Entry mid-preview, GL rows not shown in the projection) and any
reference already at zero/negative `outstanding_amount` (ERPNext itself only warns, never
throws); `plan_cancel` discloses the full blast radius — one voucher can revert
`outstanding_amount` on N invoices at once.

**Journal Entry has no header-level party field at all** — only per-line party in its `accounts`
child table (confirmed from `journal_entry.json`). `party_field=None`, and `_list_fields` is now
doctype-branched, not just party-field-patched: Journal Entry also lacks `status` and
`grand_total` (`docstatus` and `total_debit`/`total_credit` stand in). This is the first doctype
where "one party field" and "one status/grand_total shape" stopped being safe universal
assumptions baked into the earlier breadth recipe.

**Journal Entry is the first doctype to earn its own gate, not just ride the existing five
pillars unchanged.** Its ERPNext controller carves out a real, source-confirmed bypass of
Pacioli's founding law ("no debit without a credit") that Sales/Purchase Invoice and Payment
Entry never could: `voucher_type == "Exchange Gain Or Loss"` skips the debit==credit check at two
independent ERPNext gates (`validate_total_debit_and_credit`,
`general_ledger.process_debit_credit_difference`), because it's meant to be produced only by
ERPNext's own FX-revaluation tooling, not authored via API. Pacioli **refuses it outright** at
both `plan_submit` and `submit_journal_entry` — deliberately **not** at cancel, since ERPNext's
own machinery routinely auto-cancels these Journal Entries as a side effect of cancelling
whatever they reference, and refusing that cancel would block legitimate ERPNext-driven cleanup.
Belt-and-suspenders, an **independent balance check** sums the draft's own `accounts` child-row
debit/credit fields itself — never trusting ERPNext's cached `total_debit`/`total_credit` — at
both `plan_submit` (before any marker can even be minted) and `submit_journal_entry`
(logically redundant once `check_fresh` re-runs on an unmodified draft, re-checked anyway — every
gate here is re-checked at the write moment, the standing pattern). No lone, unbalanced entry
reaches this broker's consent path — not even the one ERPNext itself would let through a side
door. Two more advisory disclosures: a standing note that Journal Entry's `on_submit`-only checks
(cheque info, credit limit, invoice-discounting status) are invisible to the native preview, plus
a conditional Bank/Cash Entry missing-`cheque_no`/`cheque_date` flag (Bank Entry: ERPNext-
enforced; Cash Entry: the broker's own precaution, worded honestly as such, not an ERPNext rule);
`plan_cancel` reads `Accounts Settings.unlink_payment_on_cancellation_of_invoice` (a new,
deny-biased read — an unreadable settings doc refuses the whole plan) and flags cancel's blast
radius accordingly, plus a standing flag that cancelling a Journal Entry auto-cancels any
system-generated Exchange Gain Or Loss Journal Entry that references it, with no separate
consent.

**Cascade coverage flips, mechanically.** `cascade.py`'s `"modeled"`/`"generic"` label is driven
entirely by `SUPPORTED_DOCTYPES` membership (§ handed off to `tools.py`), so adding Payment Entry
and Journal Entry relabels any cascade node of either doctype from `"generic"` to `"modeled"` with
**zero code change in `cascade.py`**. The suite's own placeholder examples of "not yet modeled"/
"unsupported" doctypes (Journal Entry as the unsupported-doctype exemplar, Payment Entry as the
cascade `"generic"` exemplar) were repointed to Delivery Note, which remains genuinely unmodeled.

**Honest limits.** *(As written at the addendum's date. Limit 1 has since been discharged:
Payment Entry live-proven at Gate 8 and Journal Entry — governance legs, then submit/cancel via the
body-doctype transport — at Gate 9 + the Gate 10 close-out, 2026-07-07; the EG refusal and the
independent balance check both fired live — `SCOPED-TOKEN-PROOF.md` PHASES K/L/M.)*
1. **Not verified against a live bench** — the same qualifier as §3b, now doubled: every Payment
   Entry and Journal Entry request shape here, *including the Exchange-Gain-Or-Loss refusal and
   the independent balance check*, is knowledge-pinned from ERPNext v16 source reading (see
   `scout-pe.md`/`scout-je.md` in the build record), not exercised against a real bench. Live
   falsification of both paths is a distinct future bench gate — staged in
   `docs/plans/2026-07-06-gates-8-9-phase-j.md` (Gate 8: Payment Entry vertical; Gate 9: Journal
   Entry vertical; PHASE J: the cascade-cancel bench proof still owed from 0.7.0) — this addendum
   does not borrow any prior bench confirmation and apply it here by association.
2. **The two-allowlist model is unchanged.** Neither doctype needed a change to the
   `SUPPORTED_DOCTYPES` / `pacioli_guard` `resource_doctypes` split §3b describes — only a third
   and fourth entry in the broker's own dict.
3. **The frozen-books fix is a correctness improvement, not a new guarantee.** It closes a gap
   that made the lock read as always-absent on a v16 bench; it does not add any boundary ERPNext
   itself doesn't already enforce, and it has not itself been live-falsified against a real
   Company doc with a set `accounts_frozen_till_date`.

## 4. Tool surface (deny-by-default)

**Read tier (always on):** `get_sales_invoice(name)`, `list_sales_invoices(filters)` — permission-
scoped; confidentiality flags on PII/payroll-adjacent fields (least-exposure).

**Governed-write tier (Sales Invoice submit only):**
1. `plan_submit(name)` → PLAN. Returns `{plan_id, projected_gl, risk_flags, doc_version}`; records
   the plan bound to `doc_version` (the doc's `modified`).
2. `submit(name, plan_id, marker)` → gates + execute + PROVE, in this order:
   - the doc's current `modified` still equals the plan's `doc_version` (else fail closed, demand re-plan), **and**
   - no closed-books block applies (`now_date`), **and**
   - `marker` is a live, single-use, out-of-band grant bound to `plan_id` and un-expired (`now_epoch`);
   - **claim**: CAS the marker `live→reserved` (a concurrent submit loses here), **then**
   - write the **intent** PROVE receipt (durable, before the irreversible submit), **then** execute, **then**
   - on success: record a **committed** outcome + `commit` the marker; on failure: record a **failed**
     outcome + `release` the marker (grant spared). Only a *committed* outcome finalizes the intent —
     an uncertain failure leaves an orphan for the reconciliation sweep.

No generic `delete`. `submit` is the only state-changing verb in slice-one.

**Multi-site/company:** a `pacioli_target=` travels with each call (registry of targets, auth by
reference — Proximo's model). Seam plumbed from day one so wrong-company books are structurally
impossible; slice-one is proven against one target. Per-company = one least-privilege credential per
company (a company *param* on a shared cred is not enforced by ERPNext).

## 5. Honest scope (customer-facing copy is slice-one-scoped, not full-vision)

The evergreen pitch ("planned, consented, proven, undone the way accountants undo") describes the
*vision*. Slice-one ships less, and the README/PyPI copy states exactly what's here:

- **PLAN** — full (native preview), a *preview* not a guarantee (server-side validation still runs at submit).
- **CONSENT** — real out-of-band human marker, single-use *and concurrency-safe* (reserved + CAS-claimed
  before execute; released on a failed submit). *Not* full Workflow SoD yet.
- **PROVE** — hash-chained receipt, **on-box only** → tamper-evident against API users, **NOT** against
  anyone with file access on this host (including the agent this ledger watches). `verify_chain` detects
  mid-chain tampering/reorder/drop but **not tail-truncation or a full wipe** without the off-box head
  anchor (pass `expected_head` once increment 2 pins it). Off-box anchor pending.
- **UNDO** — **not shipped.** Reversal is not available in slice-one.

Never call PROVE "tamper-evident," and never imply UNDO exists, until each is real.

*(This §5 block is the original slice-one-scoped honesty scope and is not kept current release-by-
release — see `README.md`/`CHANGELOG.md` for the actually-shipped state; PROVE's off-box anchor
and UNDO both shipped well past what's written above. One note added here because code across this
package cites "SPEC §5" for the on-box honesty limit: **since 0.21.0, the seal history
(`seal_events`) shares this exact off-box anchor discipline** — `pacioli anchor write`/`check` (and
`seal-status --anchor`) pin/verify a `(seal_head, seal_count)` pair alongside the receipt head;
same audit-time DETECTION, same on-box limit, never real-time prevention. See `store.py`'s
`seal_state` docstring and `docs/plans/2026-07-15-seal-anchor-slice.md` for the full mechanism.)*

**Delegated guarantees (enforced by the glue / ERPNext, NOT the pure cores — each on the live-proof list §7):**
- Store durability is **built** in `store.py` (SQLite): `BEGIN IMMEDIATE` serialises the append
  critical section, and the marker CAS-claim + atomic outcome-settle are real — redteam-verified with
  multi-process races (zero double-spends, zero forks). The pure cores assume it; the store delivers it.
- Resubmit-idempotency is delegated: a stale plan can't authorize (TOCTOU), a spent marker won't match,
  and ERPNext's one-way `docstatus` blocks re-submitting a posted doc. Verify all three live.
- The seal key must live off-box / unreadable by the agent; until then a compromised broker can forge
  receipts. This is the point of increment 2, not a slice-one guarantee.

## 6. Structure (flat — mirrors `guard/`)

```
pacioli/
├── guard/            ← the bench app (built)
│   └── pacioli_guard/ ← module: scope.py enforce.py hooks.py scoping/ tests/
└── broker/           ← the MCP server (this spec)
    ├── SPEC.md · README.md · pyproject.toml · license.txt
    └── broker/       ← module
        ├── prove.py     pure: hash-chain receipt ledger (append/verify/head/pending→finalize)
        ├── consent.py   pure: the marker — mint(OOB) / verify / atomic single-use consume / TTL
        ├── plan.py      pure: plan record + TOCTOU version binding + risk-flag shape
        ├── spine.py     pure: PLAN→CONSENT→execute→PROVE orchestration (glue injected as callables)
        ├── erpnext.py   glue: REST/RPC to a bench (preview, submit, get/list) — the only frappe-facing code
        ├── server.py    glue: MCP stdio server + tool defs
        ├── registry.py  glue: TOML target registry, auth-by-reference
        └── tests/       bench-free unit tests for the pure cores
```

Same discipline as `pacioli_guard`: the **pure cores** (`prove`, `consent`, `plan`, `spine`) carry the
security-critical logic with **no frappe/network import**, unit-tested without a bench. The **glue**
(`erpnext`, `server`, `registry`) is thin and is proven live.

## 7. Falsification plan (trust checked, not tidy)

Pure-core logic is proven by unit tests. **The glue is proven only against a live bench** — source-
reading is not proof. This project's own history: every Guard bug (Basic-auth bypass, v2 slash-name
fail-open, alt-source fail-open) was found by live redteam, **none** by source-reading.

The single most likely estimate-blower, and the first thing to falsify: **`show_accounting_ledger_preview`
has never been called live over REST as a scoped non-Admin user.** It may need a permission tier the
minimal role lacks, or not be whitelisted for external callers. **Prove PLAN live against a sealed test bench before
building the rest of the vertical on it.** (Requires John to start the CT + arm the operator — his hand;
the build proceeds bench-free until then.)

**Live-proof checklist (each is a delegated/unverified assumption today):**
1. `show_accounting_ledger_preview` callable + permission-passable for the scoped non-Admin user.
2. ERPNext rejects a re-submit of a `docstatus=1` doc, and `submit` bumps `modified` (so a stale retry
   fails `check_fresh`) — the two assumptions the crash-residual's "bounded" claim rests on.
3. `store.py`'s serialised append (`BEGIN IMMEDIATE`) + atomic CAS-claim/settle are built and
   race-tested; the remaining live check is the deployment's SQLite config (`busy_timeout`, WAL vs
   journal) under true multi-process load, plus the off-box key handling of increment 2.
4. The broker's own credential is `pacioli_guard`-scoped to exactly its calls (else the whole spine is bypassable, §2).

## 8. Build sequence

1. **Pure cores, TDD** (bench-free, this session): `prove` → `consent` → `plan` → `spine`.
2. **Live-falsify PLAN**: call the native preview as the scoped non-Admin user on a sealed test bench. Settle the
   contract before building glue on it.
3. **Glue**: `erpnext` (preview/submit/get/list), `registry` (targets), `server` (MCP tool defs).
4. **Live proof**: the full `plan_submit → submit(marker)` path on the real bench, green end-to-end.
5. **Redteam (rule-of-three, Sonnet), fix-as-found**, before "done." Then increments 2–4 — status
   as of 2026-07-06: off-box PROVE anchor (**built**, §7 of the README), UNDO/amend (**built**),
   Workflow CONSENT (**built + live-proven against a v16 bench, §3a / Gate 5**),
   cascade/graph-aware cancel (**built, 0.7.0** — design in
   `docs/specs/2026-07-03-cascade-cancel-design.md`, live proof staged as PHASE J in
   `docs/plans/2026-07-06-gates-8-9-phase-j.md`), breadth to Payment Entry + Journal Entry
   (**built, §3c / 0.8.0** — live proof staged as Gates 8/9 in the same plan doc).
