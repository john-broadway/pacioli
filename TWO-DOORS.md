# Pacioli — the two doors (product & market shape)

> **Ruling (2026-07-11): "two doors, one product." The law.** One product — Guard + the broker —
> reached through two doors: retrofit onto an existing ERPNext, or a hardened all-in-one deploy with
> ERPNext included. Same core both ways; the ERP is either yours-already or bundled. This note is the
> durable record of that decision and the market read behind it. Grounds on `COMPETITORS.md` (the
> 40+ sweep) and the workshop-internal research verdict (a 73-agent adversarial review); extends them with the
> appliance angle those two do not cover.

## The law

The idea is named for the father of the *double*, so it comes in two — and the two balance, the way
a debit balances a credit:

| Door | Who | What they do |
|---|---|---|
| **1 — retrofit** | already runs ERPNext | `bench get-app pacioli_guard` + `pip install pacioli` onto the books they already run. Harden it, gain Pacioli. |
| **2 — appliance** | no ERP, or wants trust-native from day one | the full Pacioli install: ERPNext bundled and hardened, governed from the first posting. |

**The heart is identical in both: Guard (the grant side) + the broker (the act side).** Two doors,
one product, one law — *nothing moves without its recorded counterpart.* Doubles all the way up
(two artifacts, two doors), not just all the way down (intent↔outcome, plan↔act, propose↔dispose).

## Door 2 is a distribution, NOT a fork

The appliance pins upstream ERPNext as a **dependency it does not own** — bump the pin when Frappe
ships a patch, re-test the thin layer against it. That is "maintain a dependency" (one person can),
never "maintain a fork" (merge hell forever; and it would hand the "who maintains this?" doubt a real
point). **The line that keeps it a distribution:** harden through config, DB grants, Frappe's
extension points, and companion apps — **never by editing ERPNext's source.** The day a core source
file is edited is the day a fork has quietly begun.

Door 2 is also where Pacioli's deepest trust guarantees become *real* — the ones a pure layer on
someone else's live install can never force:
- **Ledger genuinely append-only** — deny the app's DB user `UPDATE`/`DELETE` on the GL tables (a
  database grant; not a line of ERPNext source touched). Closes the tamper ceiling the layer discloses
  as unclosable-from-outside.
- **System Console off** for the seat; escape-hatch fields (`exempted_role`, frozen-entries modifiers)
  disabled; the seat scoped by default. The `doctor` probes and the tight-role seat are already the
  hardening spec — the appliance is mostly *assembling, locking, and shipping* what's built.

## How it plays (market read — 2026-07-11, focused)

**Door 1 (layer): verdicted GO** (workshop-internal research verdict). Field crowded with *capability*, empty of
the governance *combination*; the wedge is the **credential-layer bypass** (same token that hits
`/api/method/X` hits `/api/resource/<DocType>` and walks around every tool-/role-layer control); demand
is real and long-standing; Frappe isn't building it natively (MEDIUM confidence — the one thing to
keep watching); the canonical registry slot is open.

**Door 2 (appliance): emptier than Door 1.** The ERPNext deployment space is entirely (a) managed
hosting (Frappe Cloud, partner-managed, DeployFrappe) and (b) DIY hardening guides. **No one ships a
governance-hardened, trust-native, agent-safe ERPNext distribution.** And the incumbents' "security"
pitch — *"your existing ERPNext permissions apply, every action logged, ISO 27001"* — is exactly the
model the verdict proved is bypassable at the credential layer. The market's security story has the
precise hole Pacioli fills, sitting in the incumbent's storefront (the official-marketplace AI path,
FAC, the sweep already graded *"audited but not governed"*).

**Three strategic reads:**
1. **The appliance is the moat.** Every one of the 40+ competitors is a tool-layer or in-Desk app.
   The deepest guarantees (DB-level append-only, console off) require shipping *infra* — high ground a
   software-layer competitor can't take with more tools.
2. **The appliance is the hedge on the layer's one risk.** If Frappe ever ships native per-credential
   scoping (the verdict's "keep watching" soft spot), it narrows Door 1's wedge — but does nothing to
   Door 2, because "a scope object" is not "a sovereign, hardened, tamper-evident, PLAN/CONSENT/UNDO
   trust-native distribution." **Door 2 insures Door 1.**
3. **Position around Frappe Cloud, not against it.** Frappe Cloud = easy, cloud, official hosting.
   The appliance = sovereign, governed, hardened self-host — a different axis. Guard+broker can even
   run *on* Frappe Cloud, so Door 1 is compatible with the incumbent while Door 2 is a genuine
   alternative to its trust posture. No head-on war; a clear reason to exist.

## The discipline that keeps the double honest

The Guard and broker **inside the appliance must be byte-identical to the standalone packages** — same
version, same code, config on top, never a fork of Pacioli's *own* product. The moment the appliance's
Pacioli drifts from the shelf Pacioli, it's two things wearing one name and the double breaks. **One
Pacioli, entered two ways.**

## Before any public Door-2 claim

- `COMPETITORS.md` is a 2026-06-30 snapshot in a field that adds repos weekly — **re-sweep.**
- **Run a dedicated appliance-competitor scan** (is anyone quietly building a governed/hardened ERP
  distribution?) — tonight's look was focused (verified sweep + two searches), not exhaustive.
- Un-checked tail: Workato/Boomi/Tray enterprise-iPaaS ERPNext connectors; full `discuss.frappe.io`
  threads (403 throughout the prior sweep).
