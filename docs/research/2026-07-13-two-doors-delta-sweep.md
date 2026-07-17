# Two-Doors delta sweep — 2026-07-13

> Re-sweep + first dedicated appliance scan, run against the `TWO-DOORS.md` "before any public
> Door-2 claim" checklist. Method: deep-research harness — 5 search angles, 16 sources fetched,
> 72 claims extracted, top 25 adversarially verified (3 sonnet voters per claim, ≥2 refutations
> kill; haiku scouts). **15 confirmed · 10 refuted · 0 unverified.** Baselines under test:
> `COMPETITORS.md` (2026-06-30) and `TWO-DOORS.md` (2026-07-11).

## Verdict

**The "appliance is the moat / Door 2 is empty" thesis SURVIVES this sweep** — at high
confidence *for the candidates checked*, which is a point-check universal negative, not an
exhaustive market scan. Nothing found ships or announces a governance-hardened, trust-native,
agent-safe ERPNext/Frappe distribution or appliance.

## Confirmed findings (all 3-0 votes unless noted)

1. **No appliance exists among checked candidates.** Frappe Cloud (site/bench/server-level
   controls only, no per-credential API scoping claimed), DeployFrappe (pure perimeter
   security — firewall/IP-allowlist/SSL; zero AI/MCP/governance claims anywhere on its site),
   Frappe first-party tooling, and every MCP/agent project reviewed. Sources:
   frappe.io/cloud/security, deployfrappe.com, github.com/{mascor,frappe/mcp,KorucuTech/kai}.
2. **mascor/frappe-mcp-server is the closest precedent — named loudly so it isn't "discovered"
   later.** Code-verified deny-by-default DocType allowlist (`mcp_server/utils.py::
   check_doctype_allowlist()`) + field/filter restrictions. But: an *installed Frappe app*
   (self-describes "not a complete Frappe distribution or appliance"), audit claimed but NOT
   code-verified, no consent gate / no plan-dry-run / no undo — 2 of the 4-5 primitives.
   Stale: single commit burst 2026-01-21, 3★, nothing since. Not a refutation; a watch item.
3. **Frappe first-party: still not building it.** frappe/mcp remains a "highly experimental"
   generic tool-registration framework (todo-app example, `allow_guest` + OAuth2 only, last
   confirmed push 2026-05-29). "Frappe has shipped an official ERPNext MCP" — refuted.
   "#33170 is a committed roadmap with a timeline" — refuted 0-3. Door 1's watched risk
   (native per-credential scoping) stays MEDIUM, unchanged.
4. **No confirmed movement in the tracked field since 2026-06-30** (rakeshgangwar 2026-04-25,
   frappe/mcp 2026-05-29, KAI 2024-04-30, mascor 2026-01-21 — all pre-baseline). No entrant
   confirmed to combine deny-by-default + consent + tamper-evident audit + plan + undo.
   *(Medium confidence — see coverage gaps.)*
5. **rakeshgangwar stays a pure pass-through** (its own docs list governance as future-tense
   "Proposed Improvements"); **KAI is orchestration, zero governance primitives in code**
   (grep for approval/consent/audit/deny/rbac/scope: zero hits), 2+ years stale;
   **Boomi's pre-built agent catalog excludes ERPNext/Frappe**; **ChangAI**'s privacy story
   is embeddings-local only (SQL generation still goes to Gemini/Claude — broader claim
   refuted 0-3), and it's a chatbot, not governance.

## Coverage gaps — what this sweep did NOT clear (honest scope)

- **~Half the Q2 watchlist produced no claims that survived verification:** StackOne (did the
  connector ship? 1-2 split, unresolved), Casys-AI (120-tools-no-governance characterization
  1-2 split — one search snippet claimed it now records denials in trace; needs a primary
  re-check), appliedrelevance, Codenetic-tech, FAC/buildswithpaul, Ask ALYF agent mode,
  Impertio-Studio. **Status genuinely unknown, not confirmed-unchanged.**
- **iPaaS tail one-quarter answered:** Boomi only. Workato, Tray.ai, Power Automate produced
  zero verified claims either way — still the checklist's open item.
- **Official MCP-registry emptiness was NOT re-confirmed** this round (no surviving claim
  either way; last verified 2026-06-30).
- **The broader hardened-ERP/Docker-image space outside the named candidates** generated no
  claims — coverage gap, not a clear.

## Effect on the TWO-DOORS.md checklist

- ~~Re-sweep COMPETITORS.md~~ → **done for the governed/appliance question**; tracked-repo
  deltas confirmed-none where evidence survived. (Full 40-tool refresh not re-run.)
- ~~Dedicated appliance-competitor scan~~ → **done at incumbent level** (Frappe Cloud,
  DeployFrappe, first-party, MCP field). Broader distro/image space still open.
- iPaaS tail → **Boomi closed (excludes ERPNext); Workato/Tray/Power Automate still open.**
- discuss.frappe.io: now fetchable (403s gone) — ChangAI + demand threads read this round.

## Open questions carried forward

1. Casys-AI governance posture — primary-source re-check (split vote both ways).
2. StackOne ERPNext connector — shipped or still vaporware? (split vote).
3. Workato / Tray.ai / Power Automate ERPNext presence — zero claims generated.
4. FAC, appliedrelevance, Codenetic, ALYF, Impertio current status — not reached.
5. Registry slot — re-verify empty before any "first governed ERPNext MCP in the canonical
   registry" claim.

> Run record: 98 agents (haiku scouts / sonnet verify+synthesis), 0 errors, ~78 min,
> full output `journal.jsonl` in session transcript dir. Sweep date 2026-07-13.
