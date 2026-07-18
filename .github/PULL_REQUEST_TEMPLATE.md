<!-- Thanks for contributing to Pacioli. Keep the spine at least as strong as you found it:
     no debit without a credit. -->

## What & why

<!-- What does this change, and why? Link any issue, e.g. Closes #123 -->

## Which package

- [ ] `broker/` (`pacioli` — the MCP + A2A door)
- [ ] `guard/` (`pacioli-guard` — the credential floor)
- [ ] repo-level (`scripts/`, CI, docs)

## Checks

- [ ] Broker suite green — `( cd broker && .venv/bin/python -m pytest pacioli/tests -q )` (if touched)
- [ ] Guard suite green — `( cd guard && .venv/bin/python -m pytest pacioli_guard/tests -q )` (if touched)
- [ ] `.venv/bin/ruff check .` is clean
- [ ] `.venv/bin/python scripts/version_tools.py check` is clean
- [ ] New behavior has a test; a bug fix has a test that fails before / passes after
- [ ] Touched package's `CHANGELOG.md` updated (or N/A — say why)

## The spine

- [ ] No write path bypasses **PLAN** (a preview is built + recorded before the change)
- [ ] No write path skips the **PROVE** ledger, and **CONSENT** markers stay single-use (no replay)
- [ ] deny-by-default preserved — nothing beyond the governed surface is silently allowed
- [ ] The door admits, the spine decides — no transport-lock, no door making an auth decision
- [ ] No secret, token, internal hostname, or private IP in the diff

## Notes

<!-- Anything reviewers should know: breaking changes, follow-ups, things you're unsure about. -->
