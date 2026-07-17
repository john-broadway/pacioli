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
- **StackOne** — ERPNext connector **"coming soon"**: API + **MCP + A2A** + a **"Defender" prompt-injection layer**, finance-ops focus. The one entrant *positioning on security* — **vaporware today**, worth watching.
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

## Strategic findings from the sweep (these matter more than the count)

1. **The official MCP registry is EMPTY of ERPNext.** `registry.modelcontextprotocol.io`,
   `modelcontextprotocol/servers`, `punkpeye/awesome-mcp-servers` → **zero** entries. All activity is on
   *aggregator* directories (Glama 20+17, PulseMCP, mcp.so, Smithery) that auto-index GitHub. **The
   canonical distribution slot is unclaimed** — the exact opening Proximo took.
2. **Frappe is NOT shipping a first-party AI product** — only the `frappe/mcp` framework library and the
   `frappe-agent-skills` docs repo. **No "Frappe Copilot"/"Frappe AI" incumbent to displace.**
3. **Demand exists** — forum threads *"Official MCP Server from Frappe?"* (162735) and others (couldn't be
   read in full — discuss.frappe.io 403s — but the titles confirm the ask). Partially answers our earlier
   "no demand signal" open question: there *is* latent demand for an official/governed MCP.
4. **Governance shows up PIECEMEAL, never combined.** mascor (allowlist+audit), FAC (audit), **Ask ALYF
   (approval gate before mutations)**, Noreli North (compliance capture), StackOne (planned "Defender").
   **No MCP server combines these into a trust spine.**
5. **Raven confirmed: no MCP, no external API, no roadmap for either.** "Not-MCP" stands.

## Revised gap + wedge (honest, post-sweep)

- **"Nobody has governance" is DEAD** — don't pitch it. Allowlists, audit logs, and an approval gate
  (Ask ALYF) already exist somewhere in the field.
- **Still genuinely open** (no one combines them, in an MCP server, sovereign, at breadth):
  1. **Tamper-*evident* PROVE** — every existing log is mutable; none hash-chained/keyed.
  2. **PLAN / dry-run on `submit`/`cancel`** (not just `create`).
  3. **Graph-aware UNDO** (cancel+amend in dependency order).
  4. **CONSENT gate inside an MCP server** — Ask ALYF has the gate but it's an in-Desk app, not MCP.
  5. **No arbitrary-`exec()` tool** (FAC/rakeshgangwar/ManotLuijiu/Codenetic-style power tools ship them).
  6. **Multi-site / multi-company contextvar routing.**
  7. **The official-registry slot** — be the first governed ERPNext MCP in the canonical registry.
- **Closest analogs to watch:** **Ask ALYF** (approval-gated, but in-app non-MCP), **StackOne** (security-
  positioned, vaporware), **FAC** (audited MCP, self-hosted), **mascor** (allowlist+audit PoC).

> **Bottom line (post-sweep):** the field is crowded with *capability* (40+ tools, several adding deeper
> power — workflow creation, real-time, force-execute jobs) and sprinkled with *isolated* governance
> primitives — but **no one ships the trust-by-construction *combination* in an MCP server**, the
> **official registry slot is open**, **Frappe isn't building it themselves**, and **the community is
> asking for it.** That is a sharper, better-grounded opening than "the tier is empty" — and it survives
> scrutiny because it names exactly who already does each piece.

---

## Coverage & honesty (what this sweep did and did NOT cover)

- **Tier B is discovery-only.** Source-verify before relying on any Tier-B governance claim. Flagged for
  follow-up: **appliedrelevance, Codenetic-tech (50+ tools), Kai-Oesterling (workflow-creation),
  KorucuTech/Kai, Ask ALYF (approval gate), yazelin**.
- **Not covered:** **Workato** (reportedly has a Frappe connector), Boomi, Tray, Power Automate; full
  `discuss.frappe.io` threads (403 throughout); Frappe blog "AI Debates 2026" (403); Glama results beyond
  page 1; Smithery/mcp.so tag pages (403). A long tail of 0★ stub repos is summarized by count, not listed.
- **Snapshot 2026-06-30.** Fast-moving — re-sweep before any public launch claim about "the landscape."
