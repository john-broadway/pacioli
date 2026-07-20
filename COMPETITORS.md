# Pacioli — competitor landscape

> **Two tiers, kept honest:**
> - **Tier A — source-verified** (5 OSS repos *cloned and read at code level* 2026-06-30, commit SHAs +
>   file:line in session notes; 2 commercial assessed from docs). High confidence.
> - **Tier B — discovery sweep** (4 Sonnet agents, 2026-06-30, across the MCP registries, GitHub/PyPI/npm,
>   iPaaS platforms, and the Frappe ecosystem). Profiles from **READMEs/docs only — NOT code-verified.**
>
> **The field is large and fast-moving: ~40+ ERPNext AI/agent/MCP tools across 5 categories**, mostly
> thin REST wrappers, dev-tooling, skill-packs, or in-Desk chatbots. This is a 2026-06-30 snapshot — the
> ecosystem adds repos weekly (several found here were created/updated in the last 30 days). The earlier
> "6 servers, governed tier empty" claim was **far too small and too strong**; this supersedes it.
>
> **Re-swept 2026-07-20** — see "2026-07-20 re-sweep delta" at the bottom for what moved since 06-30.

---

## Tier A — source-verified core (read at code level)

| Project | License | Type | Tools | Governance — *verified in code* | Sovereign |
|---|---|---|---|---|---|
| **Casys-AI/mcp-erpnext** | MIT | standalone TS/Deno | **120** ✓ | **none** (exposes payroll raw) | ✅ |
| **FAC** (buildswithpaul) | AGPL-3.0 | Frappe app | 24 | **real audit log** + `has_permission` + workflow-aware; *but* permissive, dry-run on `create` only, **no cancel/undo**, single-site, ships **`exec()`** → *"audited but not governed"* | ✅ |
| **mascor/frappe-mcp-server** | MIT | Frappe HTTP app · v0.0.1 dormant | 8 | **real deny-by-default allowlist + audit + field-strip** — *but* `ignore_permissions=True` **delete bug**; no submit/cancel | ✅ |
| **rakeshgangwar/erpnext-mcp-server** | MIT | standalone TS | 11 | none; `call_method` code-exec hatch; **single-target confirmed** | ✅ |
| **ManotLuijiu/erpnext_mcp_server** | MIT | Frappe app | 7 (+hidden) | none; "read-only" entrypoint only, ships raw-SQL server too | ✅ |
| **frappe/mcp (official)** | MIT | library (WSGI) | **0 ERPNext tools** | **none shipped** — framework; audit/RBAC/sandbox proposed (#33170), not in code | ✅ |
| **Composio ERPNext** | closed | cloud relay | 52 (docs) | *claims* RBAC+audit+SOC2 — unverifiable | ❌ cloud |
| **Definable.ai ERPNext** | closed | cloud relay | 50 (docs) | *claims* per-call log — unverifiable | ❌ cloud |

---

## Tier B — the full discovery landscape (~40+, discovery-only)

### B1 · Other OSS MCP servers — data access (~20+)

| Project | Lang | ★ | Notable (per README — unverified) |
|---|---|---|---|
| **appliedrelevance/frappe-mcp-server** | Py · FastMCP/Docker | 14 | custom filter language bypassing MCP JSON limits; financial statements; CRUD+methods. **Most mature new one → verify** |
| **Codenetic-tech/frappe-js-mcp-server** | TS | 0 | **50+ tools** — Socket.io real-time, **bg-job force-execute**, workflow transitions, PDF. Deepest surface → verify |
| **yazelin/erpnext-mcp** (PyPI `erpnext-mcp` 0.5.0) | Py | 1 | 20+ tools incl. **submit/cancel** + doc conversion (Quotation→SO) → verify |
| **Kai-Oesterling/erpnext-mcp-server** | JS | 0 | **creates new Workflow state machines** (not just runs them) → verify |
| **vyogotech/frappe-mcp-server** | **Go** | 1 | only Go impl; aggregation queries (SUM/COUNT/grouping) |
| **joykamlomo/erpnext-mcp** | — | 0 | claims **~82 tools** (2nd-largest after Casys) |
| **Sena-IT/frappe-mcp-server** | TS | 1 | CRUD + multi-source schema hints |
| **PROJECXIO/revenyu-mcp** | Py | — | a **WSGI MCP runtime for Frappe** (same niche as official frappe/mcp) |
| **danielsebastianc/frappe-api-mcp** | Node | 2 | single raw-HTTP passthrough tool (pure escape hatch) |
| **mkhoa**, **No-Smoke/erpnext-mcp-bridge** (wraps FAC), a **Rust** binary (via `anvie` wrapper), + **~20 low-signal/fresh/fork repos** (0★, many <3 months old) | — | 0 | thin CRUD clones; not individually profiled |

### B2 · Dev/ops-tooling MCP (not ERP-data — distinct niche)
- **SajmustafaKe/frappe-dev-mcp-server** (MIT) — DocType/code scaffolding, bench commands (for *developers*).
- **kallusuvaidyam/frappe_mcp** — bench & site manager over ngrok (ops).
- **sharat9703/mcp-erpnext-taiga-gitlab-redmine** — multi-system release automation (ERPNext+GitLab+Redmine).

### B3 · Commercial / cloud-relay MCP
- **Pipedream** ERPNext MCP — exists but a **stub** (auth, no pre-built actions). (Pipedream → acquired by Workday.)
- **Relevance AI** — ERPNext via the Pipedream embed (inherits the stub).
- **StackOne** — ERPNext connector **"coming soon"**: API + **MCP + A2A** + a **"Defender" prompt-injection layer**, finance-ops focus. The one entrant *positioning on security* — **still unshipped today**, worth watching.
- **kkwangchaoyi/erpnext-mcp-server** — "Amazon Quick SMB Finance Agent, **AWS Partner** 2026."
- (Composio, Definable.ai → Tier A.)

### B4 · iPaaS with an ERPNext agent path
- **n8n** — the ERPNext node is **officially an AI-Agent tool** (the agent's LLM calls it); full CRUD, API-key, self-host or cloud, **no submit-gate**. Mainstream + ungoverned.
- **Pabbly Connect** — ERPNext↔AI wiring, but linear automation, *not* true agent tool-calling.
- **Negative space (ERPNext ABSENT):** Zapier, Make, Activepieces, LangChain-native, LlamaIndex, Lindy, Gumloop, Klavis, Vectorshift, Stack AI. *(Coverage gap: **Workato** reportedly has a Frappe/ERPNext connector — not checked.)*

### B5 · Non-MCP in-Desk AI Frappe apps (~11)
- **Marketplace:** **ChatNext**/Hybrowlabs (206 installs, copilot), **Ask ALYF**/ALYF GmbH (**Agent mode with mandatory user approval before each mutation, incl. submit/cancel** — *the closest non-MCP analog to a CONSENT gate; verify*), InstaGPT, **Noreli North** (AI Assistant + **Advanced Compliance** — captures approvals/changes on Invoice/JE/Payment submission), Zikpro AI Invoice OCR.
- **GitHub:** **KorucuTech/Kai** (48★ — **CrewAI multi-agent orchestration as Frappe DocTypes**; most distinctive → verify), byt3crafter/erpnext-copilot (40+ AI tools), navdeepghai/nextassist (13★), NagariaHussain/doppio_bot (75★ but **stale since 2023**), Yosef-Ali/ERPNext-AI-Agent-Project (has MCP config).

### B6 · Claude Code "skill packs" (NEW category — not MCP, agent-enablement)
- **Impertio-Studio/Frappe_Claude_Skill_Package** — **135★, 896 commits, v3.1.1** — 61 skills, ~95% Frappe surface; *prevents* AI anti-patterns (raw SQL, missing permission checks). **Highest-activity Frappe-agent thing in the whole sweep.**
- **frappe/frappe-agent-skills** — **OFFICIAL Frappe org** (62★) — 3 dev skills. (Docs, not a product.)
- **lubusIN/frappe-skills** (34★, updated today), **Dkm0315/frappe-agent** (multi-assistant plugin), + sbkolate, prilk-consulting.

### B7 · Regional / services
- **Finstein** (Chennai, certified Frappe partner) — autonomous ERPNext AI agents as a *service* (mfg/CRM), India-SME focus. **erpnextai.in** — AI extension (text gen).

---

## Coverage & honesty (what this sweep did and did NOT cover)

- **Tier B is discovery-only.** Source-verify before relying on any Tier-B governance claim. Flagged for
  follow-up: **appliedrelevance, Codenetic-tech (50+ tools), Kai-Oesterling (workflow-creation),
  KorucuTech/Kai, Ask ALYF (approval gate), yazelin**.
- **Not covered:** **Workato** (reportedly has a Frappe connector), Boomi, Tray, Power Automate; full
  `discuss.frappe.io` threads (403 throughout); Frappe blog "AI Debates 2026" (403); Glama results beyond
  page 1; Smithery/mcp.so tag pages (403). A long tail of 0★ stub repos is summarized by count, not listed.
- **Snapshot 2026-06-30.** Fast-moving — re-sweep before any public launch claim about "the landscape."

---

## 2026-07-20 re-sweep delta (README-level verification — Tier B discipline, not code-read)

> Method: one research agent, 2026-07-20 — new-entrant sweep (GitHub/PyPI/npm/registry/forum), the six
> flagged Tier-B repos verified at README level against five governance dimensions, and status checks on
> StackOne / frappe-mcp / the official registry. **Nothing below was code-verified** — same honesty rule
> as Tier B above. Items not mentioned here are unchanged from the 06-30 snapshot.

### New entrants since 06-30

- **AgentReady** (agent-ready.tech; forum showcase discuss.frappe.io/t/163678, 2026-07-20) — the only
  genuinely new named entrant. Auto-discovers an ERPNext site's DocTypes/schemas and exposes them as MCP
  tools over a fixed verb set (`search/get/create/update/submit/run_report`). Governance per its own docs:
  approval workflows, schema validation, audit logging, a policy engine — but permission scoping is
  **inherited from the connected ERP's existing roles** (ambient authority), not a separately-scoped
  least-privilege credential; no distinction in governance weight between reads and irreversible submits.
  Self-described MVP: appears cloud-hosted (Login/Start-workspace flow; no self-host option found or
  mentioned), no public GitHub repo found, no pricing, contact is a personal Gmail seeking design
  partners. Their published scope is discovery and the tool surface; a separately-scoped least-privilege
  credential layer and a tamper-evident ledger are not in what they document.
  (Don't confuse with agentready.org or agent-ready.dev — unrelated projects.)
- **erpnext-readonly-mcp** (npm, v1.0.0, 2026-06-25) — read-only-by-construction MCP server; no repo, no
  author metadata, unverifiable. Adjacent philosophy (governance by removing writes), not a competitor.
- Long tail: expected fresh 0★ clones; nothing else material. Casys bumped to 2.4.1 (2026-07-16), same entrant.

### The six flagged Tier-B repos, verified (README level)

| Repo | ★ | Last real push | deny-by-default | tamper-evident audit | plan/dry-run submit·cancel | consent gate | exec hatch |
|---|---|---|---|---|---|---|---|
| appliedrelevance/frappe-mcp-server | 14 | 2025-09-16 (~10 mo stale) | ✗ | ✗ | ✗ (no submit tool at all) | ✗ | ✓ `call_method` |
| Codenetic-tech/frappe-js-mcp-server | 0 | 2026-06-25 (dormant) | ✗ (plus `switch_user`) | ✗ | ✗ (direct submit/cancel) | ✗ | ✓ `call_method` + force-execute jobs |
| Kai-Oesterling/erpnext-mcp-server | 0 | 2026-01-04 (single-shot) | ✗ | ✗ | ✗ (direct) | ✗ | ✓-class: creates DocTypes/fields/workflows (schema mutation) |
| KorucuTech/kai | 49 | **2024-04-30 — 2+ yrs stale** (metadata touches only) | ✗ | ✗ | n/a (not MCP) | ✗ | ✓ by design (arbitrary Python TOOL backends) |
| yazelin/erpnext-mcp | 1 | 2026-05-25 (~8 wks stale) | ✗ | ✗ | ✗ (direct) | ✗ | ✓ `run_method` |
| **Ask ALYF** (alyf-de/ask_alyf) | 15 | **2026-07-20 — active TODAY** | partial (Frappe RBAC + admin excluded-DocTypes list; no separate credential layer) | ✗ (history = mutable DocType) | **✓ — `document-planner` proposal now covers submit/cancel/amend** | **✓ — explicit confirm on every write** | ✓ `run_whitelisted_method` (behind the gate) |

Corrections to 06-30 reads: appliedrelevance was "most mature → verify" — actually inactive 10 months, wordier
not maturer. KorucuTech/kai was "most distinctive → verify" — 49★ is historical interest; no code since
April 2024. **Ask ALYF is the one that moved**: 10 commits in the 8 days before this sweep (including a
migration to LangChain DeepAgents), and its propose-then-confirm mechanism now demonstrably covers the
destructive verbs. By a wide margin the most actively developed governance-adjacent thing in the field.

### Status checks

- **StackOne ERPNext connector: still "Planned," unchanged since 06-30.** Their "Defender" prompt-injection
  layer is real and moving (ONNX classifier, blog 2026-05/06) but horizontal, not wired to any ERPNext
  connector that exists yet.
- **frappe/mcp: still 0 ERPNext tools**, still "highly experimental," no resources/prompts, last push
  2026-05-29. frappe/frappe#33170 closed 2025-07-08 with the library as its answer — the proposed
  RBAC/audit/sandbox never shipped.
- **Official MCP registry** (registry.modelcontextprotocol.io): `erpnext` → 0 results; `frappe` → only a
  dev-tooling scaffolder; **`pacioli` → io.github.john-broadway/pacioli, active, 0.30.2** — the only
  ERPNext-relevant entry there (re-verified 2026-07-20).

Re-sweep before citing this landscape — it moves weekly.
