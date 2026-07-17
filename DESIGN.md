# Pacioli — design sketch

<!-- Name CONFIRMED: Pacioli (John, 2026-06-30). PyPI `pacioli` is FREE (bare name available — better than Proximo, whose bare name was reserved). npm `pacioli` taken (irrelevant; Python pkg). A few tiny dormant GitHub repos share the name, none in our domain. -->

> **Status (2026-07-01): DIRECTION LOCKED + first primitive PROVEN.** The credential-layer
> boundary is proven live in a real Frappe v16 bench — both as a core patch AND (the shippable form)
> as an **installable Frappe app enforcing via `auth_hooks`, no core fork** (`SCOPED-TOKEN-PROOF.md`).
> An adversarial 73-agent deep-research pass returned **GO**: the gap is real (primary-source, and
> frappe's own OAuth2 author confirms scopes don't restrict access), no prior art at the credential
> layer, no imminent native fix found (workshop-internal research verdict). The agent-broker design below (5 pillars)
> is still valid — it is now the **tip**, not the whole product.
>
> **DIRECTION LOCKED — app-first.** Pacioli leads with the **credential-layer least-privilege boundary**,
> which governs *every* API credential on the site — an MCP/A2A agent, a Zapier/n8n flow, an integration,
> a script, a vendor token, a cron job — not just agents. That's a **security/compliance product for any
> ERPNext shop** (least-privilege API keys + leaked-key blast-radius cap + audit), and the *demand we
> found in the research came from integration builders, not AI users.* The MCP broker is the sharp tip
> for those going agentic. Governs **programmatic** access; frappe roles already handle desk/browser users.
>
> **NAME LOCKED — Pacioli.** The reframe *strengthens* the fit: Pacioli's real invention wasn't accounting,
> it was a **system of provable controls and integrity over records** (double-entry = self-checking,
> tamper-evident). Trust-by-construction over the books → trust-by-construction over *access to* the books.
> The name carries the domain + ethos; the **tagline carries the function** — same split as Proximo.
> **Tagline: "Least-privilege API governance for ERPNext. Agents included."**
> Family: **Pacioli** (brand) · **Pacioli Guard** (the credential-scope app — the universal foundation) ·
> **Pacioli MCP** (the agent front door — the tip).
>
> Prior recon-sketch note (2026-06-30): the ERPNext analog to how Proximo was scoped before a line of code.
> Sources: the tiered deep-research report (run `wexq5ynqo`) + the 2026-07-01 adversarial verify (`wjtvmx7bf`).

---

> **The ERPNext MCP you can hand the books.**
>
> The ones that exist make you choose: a read-only inspector that's safe because it can't touch
> anything — or a loaded gun pointed at a company's general ledger. The broad, write-capable servers
> expose `submit` (an *irreversible* GL posting), `cancel`, and permanent
> `delete` as **raw, unguarded tool calls**. Pacioli refuses the trade. Every move that touches
> the books is **planned** (see the journal entry before it posts), **consented** (it passes
> through the company's own approvals and segregation-of-duties, never around them), **proven**
> (a tamper-evident receipt of every posting), and **undone the way accountants undo** — by
> cancel-and-amend, never by quietly deleting a posted document. **Hand an AI agent the books;
> keep the audit trail.**

*Named for Luca Pacioli, the friar who codified double-entry bookkeeping in 1494 — the discipline
that every debit has a credit, and the books must always balance and always be auditable. That is
the whole design: an agent may act on the ledger, but never in a way that breaks the trail or the
balance, and every posting answers for itself.*

---

## 1. Why Pacioli exists — the unoccupied tier

The deep-research sweep found **six** ERPNext/Frappe MCP servers in the first pass (a culture/debate
follow-up surfaced **two more — commercial/hosted: Composio and Definable.ai**) plus adjacent AI layers.
**Source-verified — see `COMPETITORS.md` (every OSS repo cloned + read at code level, 2026-06-30; it
supersedes the disputed sweep).** The honest picture: *some* governance primitives **do** exist —
**FAC** and **mascor** ship real per-call audit logs, and **mascor** has a real deny-by-default
allowlist. But **every existing audit log is a mutable Frappe DocType (not tamper-evident); nobody
dry-runs a `submit`/`cancel`, nobody has graph-aware UNDO, a CONSENT gate on postings, or multi-site
routing; and the broad servers ship arbitrary-code-exec tools.** What's unoccupied is the
**trust-by-construction *combination*** — unforgeable PROVE + planned + reversible + consented +
no-exec + multi-site, at breadth and sovereign. The nearest competitor (FAC) is **"audited but not
governed."**

| Project | Maintainer | Read/Write | Tools | Auth | Governance posture |
|---|---|---|---|---|---|
| **mcp-erpnext** | Casys-AI | R/W incl. Submit/Cancel | **120** (14 categories) | API key/secret | **None** — thin REST wrapper |
| **erpnext-mcp-server** | rakeshgangwar | R/W incl. submit/cancel/**delete** | ~dozens (TS) | API key/secret | **None** — destructive ops raw |
| **frappe-mcp-server** | mascor | R/W (generic CRUD) | **7** generic | API key/secret | **Deny-by-default allowlist** — closest *safety* posture, but no Submit/Cancel awareness, no undo |
| **erpnext_mcp_server** | ManotLuijiu | **Read-only** | 7 | env | Safe **by scope**, not by architecture |
| **Frappe_Assistant_Core (FAC)** | buildswithpaul · **AGPL-3.0** | R/W incl. submit, `run_workflow`, **`exec()`** | **24** (verified) | **OAuth2 + API key**, runs as user | **Audit log REAL** + `has_permission` + workflow-aware; *but* permissive (no deny-default), dry-run on `create` only, **no cancel/undo**, single-site → **"audited but not governed"** |
| **frappe/mcp (official)** | Frappe · MIT | library (WSGI) | **0 ERPNext tools** | inherits Frappe | **None shipped (verified)** — it's a framework; audit/RBAC/rate-limit/sandbox were *proposed* (#33170), none in code |
| **Composio** (ERPNext toolkit) | Composio · commercial/hosted | R/W incl. Submit/Cancel | toolkit | hosted/OAuth | *Claims* an audit trail — **unverified**; thin commercial wrapper |
| **Definable.ai** | Definable · commercial/hosted | R/W incl. Submit/Cancel | ~50 | hosted | Per-call logging *claimed* — **unverified** |

**Adjacent, but not MCP** (different architecture, same user need):

- **Raven** (The Commit Company, AGPL-3.0) — **primarily a team chat/messaging app** ("Slack for
  Frappe"; its own repo description: *"Simple, open source team messaging platform"*, topic
  `chat-application`, 723★). It does **Document Alerts**, and in **v2** added **AI Agents** (no-code
  bot builder, function-calling incl. submit/cancel). So it's a chat app with a bolt-on agent feature
  — **not an AI-agent platform, and not MCP**: embedded in ERPNext Desk, no MCP endpoint, unreachable
  by an external MCP client. Adjacent — competes for the *need*, not the socket; not a direct competitor.
- **changAI** (ERPGulf) — in-Desk chat, CUD via Frappe ORM. Not MCP/A2A. No audit/undo.
- **NextAI** (erpnextai, Custom Non-Commercial) — a UI button that drops Gemini-generated text into
  form fields. Not a programmatic agent layer at all.

> **Correction — FAC now source-verified (`COMPETITORS.md`):** the deep-research *refuted* FAC's audit
> log (and its code-exec tools); **the code proves all three real.** "Refuted" meant "couldn't confirm,"
> not "absent" — exactly the trap the advisor flagged. FAC is the closest competitor and is *audited
> but not governed*; Pacioli's edge over it is **trust-spine depth, not sovereignty** (FAC is
> self-hosted too — sovereignty is the wedge against the *cloud* tools, Composio/Definable).

**The gap, in one line:** every server we could verify delegates safety entirely to ERPNext's
API-level permissions and adds no *tool-level* trust layer. (FAC is the one unverified maybe — §9.1.)
That layer is the product.

## 2. Philosophy — trust by construction, for the books

Proximo's bet on Proxmox: *"safe and reversible"* is the whole game, because a homelab has one
operator and the worst case is a wrecked VM you can restore from a snapshot.

**ERP breaks that frame.** On the books, "safe and reversible" is **necessary but not sufficient**,
for two reasons infra never has to face:

1. **Actions have accounting and legal weight.** A submitted Sales Invoice posts to the General
   Ledger, moves stock, and feeds a tax filing. It is reversible only in the disciplined,
   *auditable* way accountants allow — never by a silent delete.
2. **The oldest control on earth is segregation of duties.** The person who *raises* an invoice may
   not be the person who *approves* it. A perfectly reversible payment that **no authorized human
   approved** isn't a bug — it's fraud-shaped. Infra with one operator never needs this gate; ERP is
   built around it.

So Pacioli is Proximo's spine **plus one pillar** — and the new pillar is the differentiator the
market leaves wide open.

## 3. The spine — five pillars, mapped to ERPNext's own primitives

The good news: ERPNext already thinks this way. Each pillar lands on a native primitive rather than
being bolted on.

| Pillar | What it does | ERPNext primitive it rides | Status |
|---|---|---|---|
| **PLAN** | Dry-run before anything posts: build the document as a **Draft** (`docstatus=0`), run Frappe's `validate`, and return the preview — the would-be journal/GL impact, the stock movement, the linked docs — *recorded* before any submit. A submit can't fire without its plan built first. | Draft state + `validate()` hooks; `Sales Invoice` etc. expose the projected GL entries pre-submit | ✅ verified vs source (§3a) |
| **PROVE** | Append-only, hash-chained, **keyed (HMAC)** audit ledger of every mutation: caller, tool, args, target site/company, docname, the `docstatus` transition, result. Tamper-evident; head-pinnable off-box. **FAC and mascor *do* log (verified) — but to *mutable* Frappe DocTypes; none is hash-chained/keyed/tamper-evident. That unforgeability is PROVE's real edge** (`COMPETITORS.md`). | Sits alongside ERPNext's own Version/Audit-Trail DocTypes; Pacioli keeps its **own** independent receipt | ✅ verified vs source (§3a) |
| **UNDO** | **Graph-aware, the way accountants undo.** Never a raw delete of a submitted doc. Reversal = **Cancel (`docstatus=2`) + Amend**, walked in dependency order across linked Payments / Stock Entries / GL. Respects ledger immutability — a posted entry is reversed by a counter-entry, not erased. | `docstatus` 0→1→2 *is* the model; `amend` creates the successor; immutable ledger is canon **since v13** | ✅ verified vs source (§3a) |
| **DIAGNOSE** | Read-only evidence + health: doc status, outstanding balances, stuck workflows, failed background jobs — with **confidentiality awareness** (payroll, salaries, PII are least-exposure; a read is not neutral here). | `frappe.get_all` / report queries under the caller's permissions | ✅ verified vs source (§3a) |
| **CONSENT** *(new)* | Deny-by-default allowlist (like mascor) but **governed and broad**. Material, money-moving, or period-affecting actions pass **through** ERPNext's Workflow engine + `has_permission()` — never around them — with a human gate. The agent can *prepare* a submission; a person (or the configured Workflow) *consents* to the posting. | **Workflow** engine (multi-step approval states) + role **Permission** model + `frappe.has_permission()` | ✅ verified vs source (§3a) |

**Structural property, ported straight from Proximo:** **contextvar-routed multi-site / multi-company.**
The target site (and company) travels *with the call*, so PLAN and SUBMIT always hit the same books,
and the wrong company's ledger is **structurally impossible** to touch. No ERPNext MCP server we could confirm has this today (one
server's single-target claim didn't survive verification — treat the field as unconfirmed, not proven-absent).

> **Honesty note (load-bearing), borrowed from Proximo's discipline:** PLAN's projected GL impact is
> a *preview*, not a guarantee — server-side validations and Workflow conditions still run at submit.
> CONSENT enforces *the company's own* approval rules; it does not invent new ones. And UNDO is bounded
> by ERPNext's own reversibility: where the platform forbids cancellation (a closed accounting period,
> a reconciled payment), Pacioli must refuse and say so — not pretend it can undo.

## 3a. Validated against ERPNext source (2026-06-30)

Three Sonnet agents read `frappe/frappe` + `frappe/erpnext` source (docs.frappe.io 403'd, so they read the
code). Every pillar maps to a real, cited mechanism — with the honest caveats:

- **PLAN ✅ native — better than assumed.** ERPNext already ships pre-submit preview:
  `erpnext.controllers.stock_controller.show_accounting_ledger_preview` / `show_stock_ledger_preview`
  (engine `ledger_preview.py`): savepoint → set `docstatus=1` in memory → `make_gl_entries` → read the
  rows → **rollback** (nothing persists). We get a draft's exact GL/SLE impact with *no* custom dry-run.
- **PROVE ⚠️ feasible, requires an off-box anchor.** ERPNext's "immutable ledger" is an *app convention*
  (insert reversals; an **opt-in** `enable_immutable_ledger` checkbox, **off by default**) with **no
  DB-level enforcement**; its "Audit Trail" DocType is a *diff viewer*, not a log. An on-box hash-chain
  is tamper-evident only against API users — a System Manager / bench / DB shell can rewrite it if the
  HMAC key is on-box. **True tamper-evidence needs the chain head pinned off-box — exactly Proximo's
  `expected_head`.** Same honesty note carries over.
- **UNDO ✅ feasible, fail-closed.** Dependency graph via
  `frappe.desk.form.linked_with.get_submitted_linked_docs` (recursive downstream tree); cancel in
  reverse order, catch `frappe.LinkExistsError`. Amend has **no native method** — orchestrate it:
  cancel → `frappe.copy_doc` → set `amended_from` explicitly → `insert`. ERPNext **enforces** hard
  cancel-blocks inside `make_reverse_gl_entries` — **closed Accounting Period**, **Period-Closing-Voucher
  boundary**, company **`accounts_frozen_till_date`**. *Pacioli surfaces these and refuses; it must
  **never** use the `adv_adj=True` flag or a future `posting_date` to slip past them.*
- **CONSENT ⚠️ native, but weaker than first read — SoD is opt-in per transition, and the engine is
  bypassable.** *(Corrected 2026-07-02 against `frappe/frappe` version-15 source; the 2026-06-30 read
  overclaimed.)* `apply_workflow(doc, action)` does park a draft pending approval (stays `docstatus=0`)
  with the `Workflow Action` inbox (role-scoped) — that part holds. Two corrections:
  **(1) self-approval is ALLOWED by default** — `allow_self_approval` on Workflow Transition defaults
  to `1`; `has_approval_access` only blocks when a designer explicitly unchecks it (and never for
  Administrator). **(2) the workflow engine does not guard `docstatus`** — `validate_workflow` fires
  only when the *workflow-state field* changes, so a direct REST submit that leaves that field
  untouched passes on the generic doctype `submit` permission alone, skipping the workflow's role,
  condition, and self-approval checks entirely (`frappe/permissions.py` has zero workflow awareness).
  So Pacioli does not merely *ride* ERPNext's SoD — **the broker's own workflow gate is the
  enforcement on the agent's path** (refuse direct submit when an active Workflow governs the
  doctype; perform non-approving transitions only), and it surfaces honestly when a company's
  workflow permits self-approval. Guard-side (bench-side) workflow enforcement for scoped
  credentials is a named future increment. **Both corrections were live-confirmed on a real
  ERPNext v16 bench (2026-07-02, Gate 5 / `SCOPED-TOKEN-PROOF.md` PHASE G):** a valid minted marker
  was still refused at the broker's workflow gate, and a direct `apply_workflow` self-approval by
  the document's creator was refused by frappe with HTTP 417 — the broker's belt and frappe's
  suspenders both hold in the realistic broker-is-creator case.
- **DIAGNOSE ✅** — reads through `has_permission`-scoped queries; REST enforces the user's role + record
  (User) permissions server-side, and **`ignore_permissions=True` is a server-only Python flag an external
  caller cannot set.**
- **Multi-site ✅ Proximo-identical** (`base_url` + `token` per call → hostname → that site's own DB).
  **Multi-company ⚠️** — scope with **one least-privilege credential per company**; a company *parameter*
  on shared/admin creds is **not** enforced (the framework does not company-scope a raw credential).

**Load-bearing principle (the meta-finding):** *all* ERPNext governance is **app-layer**, so the
*principal the broker runs as* is the foundation. The literal **Administrator** user (and bench/DB
access) bypasses every permission check outright — frappe hardcodes that bypass to the *username*,
not to any role (`frappe/permissions.py:107,304,544`). **System Manager** holds no such runtime
bypass, but it is frappe's bench-admin role: over the REST surface it can write Custom DocPerm rows
(grant itself any permission), mint API keys (`generate_keys`, `only_for` System Manager), and —
with server scripts enabled — run arbitrary server-side code via the System Console (`execute_code`,
`only_for` System Manager/Administrator). Either way the principal can escape or dismantle the model.
So **Pacioli operates as a scoped, least-privilege ERPNext user that holds neither Administrator nor
System Manager, by hard rule** — `pacioli doctor`'s roles probe enforces this at readiness time.
The tamper-evident PROVE ledger (off-box-anchored) is precisely what catches what the permission
model cannot.

**Bottom line:** no blockers. Every pillar has a verified ERPNext mechanism; the only place ERPNext
doesn't give us enough is PROVE's tamper-evidence against root — and that's solved the way Proximo already
solves it, with an off-box head anchor.

## 4. Tool design

Two tiers, deny-by-default:

- **Read tier (always on, DIAGNOSE):** get/list/search/report, permission-scoped. Confidentiality
  flags on payroll/PII DocTypes.
- **Governed-write tier (opt-in, allowlisted per DocType):** every write routes through the spine.
  The shape of a posting call is always **PLAN → CONSENT → execute → PROVE**:
  1. `plan_submit(doctype, name)` → returns the draft's projected GL/stock impact + risk flags, records the plan.
  2. CONSENT gate: if the DocType/Workflow requires approval, the tool **stops** and surfaces the
     pending state — it does not self-approve. A `confirm`/approval step (human, or the Workflow
     transition by an authorized user) is required.
  3. execute via the REST/RPC surface.
  4. PROVE: the receipt lands in the hash-chained ledger with the `docstatus` transition.
- **Reversal is its own verb:** `cancel_and_amend(name)` — never a generic `delete` on submitted docs.
  `delete` is reserved for drafts, and even then it's audited.

Mascor proved the allowlist works (18/18 functional + 15/15 security tests on 7 tools). Pacioli's
bet is **allowlist + governed breadth** — the safety posture mascor has, at the coverage Casys-AI has,
with the spine neither has.

## 5. Auth + sovereignty

Sovereign (the user owns keys + data) and agnostic (self-host *and* Frappe Cloud, version-spanning):

- **Auth surfaces, all supported:** API key/secret (`Authorization: token <key>:<secret>`), token,
  and **OAuth2 / PKCE** (FAC proved the LLM-as-real-user pattern — the agent authenticates as an
  ERPNext user, never sees the password, and every call is scoped to that user's roles). Secrets
  **by reference**, never inlined — Proximo's rule.
- **Sovereign:** runs on the user's machine / their bench; no Pacioli-hosted relay; no vendor lock-in.
- **Agnostic:** wraps the documented **REST (v1/v2 split, Frappe v15+) + whitelisted RPC** surface, so
  it spans ERPNext versions and works against self-hosted *or* Frappe Cloud without a bench app
  install where possible. Model-provider-agnostic by virtue of being an MCP server (any MCP client).

## 6. Multi-site / multi-company routing

Ported from Proximo's `proximo_target` model: a TOML registry of sites/companies, each with its own
auth-by-reference, and a per-call `pacioli_target=` (and company scoping). The target binds PLAN and
SUBMIT to the same books; PROVE records *which* books. Kind-checked. **Per-company scoping is enforced
with one least-privilege credential per company** — validated (§3a): a company *parameter* on shared/admin
creds is NOT enforced by ERPNext, so the registry maps each (site, company) to its own scoped user. This is the single biggest
*structural* differentiator — and **no competitor we could confirm offers the target-with-the-call
guarantee.** Pacioli makes Frappe's native multi-tenancy (one bench, many sites) first-class and
cross-company mistakes structurally impossible by construction.

## 7. Good citizenship in Frappe's world

The Frappe community is open-source-purist (Frappe Framework is **MIT**; ERPNext is **GPL-3.0**;
newer first-party apps and Raven are **AGPL-3.0** — a deliberate anti-"maker-taker" tightening),
bootstrapped (12 years independent; one mission-aligned 2020 investment from Zerodha/Rainmatter),
and community-governed. They will **distrust** an AI layer that bypasses their
permission model or quietly mutates the ledger; they will **embrace** one that works *with* their
primitives. Pacioli's posture is built for that:

- Operate **inside** `has_permission()` and the **Workflow** engine — never a privilege end-run.
- Never delete posted documents; honor `docstatus` and the immutable ledger as canon.
- Be loud about the opt-in write edges (Proximo's "say so plainly" rule).
- License compatibly and ship it community-ready (docs, SETUP, honest scope).

*(This is the condensed posture for an architecture doc. John asked to learn ERPNext's culture the
way we learned Proxmox's — a fuller standalone **culture brief** (Frappe company history, community
norms, the AI-on-ERP-data debate) is a separate deliverable, not folded in here.)*

## 8. Distribution

The pipeline Proximo already ran: **Anthropic MCP registry** + **awesome-mcp-servers** PR + **mcp.so**,
PyPI + GHCR, an `[a2a]` extra for the Agent2Agent face routing through the same trust core (no second
mutate path). **Strategic opening (verified in the sweep — `COMPETITORS.md`): the official MCP registry,
`modelcontextprotocol/servers`, and `awesome-mcp-servers` have ZERO ERPNext entries.** All ~40 existing
tools live only on auto-indexing aggregators (Glama/PulseMCP) — **the canonical registry slot is unclaimed,**
and **Frappe ships no first-party AI product** to fill it. Discovery is solved by being *the governed one*
**and** the first ERPNext entry in the official registry, in a field of thin wrappers.

## 9. Open questions / next probes (before any build)

1. ~~Re-scope FAC by hand~~ **DONE → `COMPETITORS.md`.** Verified: real audit log + OAuth2 +
   `has_permission` + workflow-aware, but permissive, create-only dry-run, no cancel/undo, single-site,
   ships `exec()`. "Audited but not governed." (Remaining unknowns: Composio/Definable are closed
   cloud relays — can't read their source, but their *cloud-relay* model already loses on sovereignty.)
2. **Official `frappe/mcp` real state** — is it a published package, a bench app, or an in-repo
   experiment, and what does it expose by default? It's the incumbent-by-name.
3. **Commercial connectors — partially found.** Composio's ERPNext toolkit and Definable.ai both
   expose submit/cancel and *claim* audit trails (unverified marketing copy). Hand-verify their real
   governance depth (a *log* is not a trust spine); also check LangChain / Make / Zapier ERPNext nodes.
4. **ERPNext reversibility edges** — confirm exactly where cancel/amend is *forbidden* (closed
   periods, reconciled payments, stock already consumed) so UNDO fails closed and honest.
5. ~~The name~~ — **CONFIRMED: Pacioli** (John, 2026-06-30). Availability checked: **PyPI `pacioli`
   FREE** (cleaner than Proximo's reserved bare name), npm `pacioli` taken (irrelevant for a Python
   pkg), only small dormant GitHub namesakes (none in our lane). Clear to use as `pacioli`.
