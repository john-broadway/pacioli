# Contributing to Pacioli

Thanks for considering a contribution. Pacioli governs an AI agent's access to a
real business's **books**, so it holds itself to a higher-than-usual bar on
**safety** and **honesty** — and contributions are held to the same bar. The
spirit, in one line: *no debit without a credit — leave the spine at least as
strong as you found it.*

This is a small, independently-maintained project run on best-effort time.
Please be patient with review, and thank you for helping.

## Before you start

- **Security issues are not public.** If you've found a vulnerability, do **not**
  open an issue or a PR — follow [`SECURITY.md`](./SECURITY.md) (GitHub's private
  vulnerability reporting).
- **For anything non-trivial, open an issue first.** A short discussion saves a
  rejected PR. Obvious fixes (typos, clear bugs) can go straight to a PR.

## Two artifacts, one law

Pacioli is a monorepo of two composed-but-separate packages — get to know the one
you're touching:

- **`broker/`** (`pacioli`) — the governed MCP + A2A broker. A standalone
  `pip install pacioli` server; every write goes **PLAN → CONSENT → PROVE**
  (+ ERPNext's own cancel/amend as UNDO), deny-by-default.
- **`guard/`** (`pacioli-guard`) — the credential floor. A Frappe/ERPNext bench
  app binding any API credential to an allowlist of methods/DocTypes,
  deny-by-default, via Frappe's public `auth_hooks`. No core fork.

## Development setup

Pacioli uses [`uv`](https://docs.astral.sh/uv/). Each package owns its own venv,
and repo-level tooling gets a third — never borrow one for another's job:

```bash
git clone https://github.com/john-broadway/pacioli.git
cd pacioli
( cd broker && uv venv && uv pip install -e '.[server,a2a]' pytest )    # the broker
( cd guard  && uv venv && uv pip install -e . pytest )                  # the guard
uv venv --python 3.12 && uv pip install pytest ruff pip-audit          # tooling (needs py>=3.11: version_tools uses tomllib)
```

Run the same checks CI runs — a PR that doesn't pass them won't merge:

```bash
( cd broker && .venv/bin/python -m pytest pacioli/tests -q )        # broker suite
( cd guard  && .venv/bin/python -m pytest pacioli_guard/tests -q )  # guard suite
.venv/bin/ruff check .                                              # lint, full repo
.venv/bin/python scripts/version_tools.py check                    # version single-source
.venv/bin/python -m pytest scripts/tests -q                        # version-tools drift gate
```

The cores are bench-free: you do **not** need a live ERPNext bench to develop.
(The live vertical is proven separately against a real Frappe bench as a scoped
non-Administrator — `SCOPED-TOKEN-PROOF.md`; you won't normally touch it.)

## The spine — please don't weaken it

Pacioli's whole reason to exist is that an agent can act on the books *without
being able to forge or hide an entry.* Keep these intact:

- **PLAN** — every write builds and records a preview before it can commit.
  Don't add a write path that skips the plan.
- **CONSENT** — a single-use marker binds one recorded plan; replay is refused.
- **PROVE** — every act lands in the hash-chained, tamper-evident receipt ledger
  (the trial balance). Don't add a path that mutates without recording.
- **UNDO** — heterogeneous, via ERPNext's own cancel/amend where the DocType
  supports it.
- **deny-by-default** — the broker governs one write today; everything beyond it
  is denied, not silently allowed.

Two things are deliberate, not accidental — don't flip them to be more convenient
and less safe:

- **The door admits; the spine decides.** Any transport (MCP, A2A, …) routes
  through the *one* spine. Never transport-lock a governed action, and never let
  a door make an authorization decision — that belongs to the spine.
- **The broker's own credential must itself be `pacioli_guard`-scoped.** This is
  a hard precondition, not optional hardening: an unscoped credential calls
  ERPNext's REST API directly and bypasses PLAN/CONSENT/PROVE entirely.

## Submitting a change

1. Fork, and branch from `main`.
2. Make the change **with tests** — new behavior needs a test; a bug fix needs a
   test that fails before the fix and passes after.
3. Get the checks above green.
4. Add a line to the touched package's `CHANGELOG.md` (`broker/` or `guard/`).
5. Open the PR and fill in the template.

Honest, small, well-tested PRs get reviewed fastest.
