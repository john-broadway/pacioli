# Pacioli — the two doors (product & market shape)

> **Ruling (2026-07-11): "two doors, one product." The law.** One product — Guard + the broker —
> reached through two doors: retrofit onto an existing ERPNext, or a hardened all-in-one deploy with
> ERPNext included. Same core both ways; the ERP is either yours-already or bundled. This note is the
> durable record of that decision.

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

## The discipline that keeps the double honest

The Guard and broker **inside the appliance must be byte-identical to the standalone packages** — same
version, same code, config on top, never a fork of Pacioli's *own* product. The moment the appliance's
Pacioli drifts from the shelf Pacioli, it's two things wearing one name and the double breaks. **One
Pacioli, entered two ways.**

