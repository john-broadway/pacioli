# Security Policy

Pacioli governs an AI agent's access to a real business's books. Its whole design
premise is that every write is previewable, consented, recorded, and reversible —
*no debit without a credit* — so taking its security seriously is the point, not an
afterthought. Reports are genuinely welcome.

## The two-layer trust model (read this first)

Pacioli's protection comes from two layers that do not fail the same way. Don't
confuse them.

1. **The hard floor: the `pacioli_guard`-scoped credential + ERPNext's own RBAC.**
   Guard binds any API credential to an allowlist of methods/DocTypes,
   **deny-by-default**, through Frappe's public `auth_hooks` extension point — no
   core fork. This is enforced **server-side, inside Frappe/ERPNext**, on *every*
   request that credential makes, not by any line of the broker's code. It holds
   even if the broker process is fully compromised — a prompt-injected agent, a
   poisoned dependency, a shell in the MCP client: whatever the scoped credential
   isn't allowed to call, nothing acting as the broker can call either. This is the
   only layer that assumes the broker's own process might be hostile.
2. **The broker's in-process spine — PLAN → CONSENT → PROVE (+ UNDO).** Every write
   builds and records a preview before it commits (**PLAN**), binds a single-use
   marker to that recorded plan with replay refused (**CONSENT**), lands in a
   hash-chained tamper-evident receipt ledger (**PROVE**), and is reversible through
   ERPNext's own cancel/amend (**UNDO**), deny-by-default beyond the governed
   surface. These raise the bar **within** the broker's trust domain — but they run
   in the same process as the agent they constrain, so they are a productivity and
   accountability layer, not a sandbox.

Layer 1 is why Pacioli is safe to hand to an agent at all. Layer 2 is what makes that
agent *productive and accountable* — previews, single-use consent, receipts — without
pretending the broker sandboxes ERPNext.

## The hard precondition (not optional hardening)

**The broker's own ERPNext credential must itself be `pacioli_guard`-scoped to exactly
the calls it makes.** Unscoped, anything holding that raw credential calls ERPNext's
REST API directly and **bypasses PLAN/CONSENT/PROVE entirely** — Layer 2 becomes
decorative and only Layer 1 that you never configured is left. Scope the broker's
credential with Guard before pointing an agent at it.

## The doors admit; the spine decides

The broker exposes more than one transport — an MCP stdio/HTTP door and an A2A
(Agent2Agent) door. **Every door routes through the one spine** (the same governed
call path); a transport never makes an authorization decision, and there is no second
mutate path. Any door that could reach a write without going through PLAN/CONSENT/PROVE
is in scope for a report.

## Supported versions

Pacioli is pre-1.0; security fixes land on the **latest release only**. There is no
back-port branch.

| Package | Supported |
|---|---|
| `pacioli` / `pacioli-guard` — the latest [PyPI](https://pypi.org/project/pacioli/) release | ✅ |
| anything older | ❌ — upgrade |

## Reporting a vulnerability

**Please do not open a public issue for a security report.**

Use GitHub's private vulnerability reporting — open a report directly at
**https://github.com/john-broadway/pacioli/security/advisories/new** — or go to the
repository's **Security** tab → **Report a vulnerability**. That opens a private
advisory thread visible only to you and the maintainer.

Pacioli is independently maintained — expect a serious, best-effort response, not a
contractual SLA. Disclosure is coordinated with you: a fix and an advisory go out
together, with credit unless you ask otherwise.

## What's most worth your attention

- **Spine bypass.** Any write path that commits *without* a recorded PLAN, reuses or
  replays a spent CONSENT marker, mutates without extending the PROVE ledger's hash
  chain, or forges a verifying chain.
- **Guard scope bypass.** A way for a credential to call a method or DocType outside
  its `pacioli_guard` allowlist, or to defeat the deny-by-default posture — this is the
  Layer-1 floor, and any hole in it is high severity.
- **Door → spine gap.** A path through the MCP or A2A door that reaches a write while
  skipping the governed call spine, or an auth/rebind bypass on the network faces.
- **Credential handling.** A path where an ERPNext credential, token, or other secret is
  logged, echoed into the receipt ledger, or otherwise persisted in cleartext.

## Honest scope notes

- **Slice-one is deliberately narrow.** The broker governs one write end-to-end today
  (submit a Sales Invoice) and denies the rest by default. "A tool I wanted isn't
  exposed" is scope, not a vulnerability.
- **The scoped credential is the hard floor.** Running the broker with an unscoped or
  over-broad ERPNext credential against the guidance above is operator misconfiguration,
  not a Pacioli vulnerability — though reports of Pacioli *encouraging* such a setup are
  welcome.
- **The in-process spine is not a sandbox.** PLAN/CONSENT/PROVE previews, gates, and
  records; it does not sandbox the ERPNext API. A report that a governed write did the
  thing it planned, with a consent grant and an audit record, is working as designed.

## Verifying authenticity

- **PyPI (`pacioli`, `pacioli-guard`):** published via GitHub Actions OIDC Trusted
  Publishing — no long-lived API token sits in the release path.
- Release artifacts built by the current pipeline carry a CycloneDX SBOM and a sigstore
  build-provenance attestation attached to the GitHub release.

If a downloaded artifact fails verification, treat it as untrusted and report it here.
