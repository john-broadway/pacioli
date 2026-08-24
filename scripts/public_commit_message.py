#!/usr/bin/env python3
"""Build the PUBLIC release commit message for a version, from that package's CHANGELOG entry.

Ported from proximo 2026-08-24, on John's word: "i asked for pacioli to release like proximo".

**Why this exists.** GitHub `main` here carries a curated, squashed history: one commit per
release, created with `git commit-tree` rather than by merging the internal line. Until now this
repo's `release.sh` handed the next session a hardcoded `-m "release: broker X.Y.Z"`, so every
word of reasoning lived on the internal mirror and the public face of the project showed no
reason for any push. A visitor reading the commit list learned nothing.

Proximo fixed exactly this and named the cost: six releases shipped with a bare message before
anyone noticed. That fix never travelled to pacioli, which is the "a rule that lives in one repo
is a coincidence" shape — the same shape that let one dependency defect break both repos
separately in August.

The CHANGELOG entry for the version IS the explanation, already written and already gated by
`version_tools.py check` (which refuses a release until a real `## X.Y.Z` heading exists). This
turns it into the commit body so the two cannot drift: there is no second place to write it.

**Two differences from proximo's copy**, both forced by this repo's shape:

* Pacioli is a TWO-package monorepo, so the package is an argument and selects which CHANGELOG
  is read. `release: broker 0.39.0` and `release: guard 0.14.0` are different releases.
* The headings here are ``## 0.39.0 - 2026-08-24 - title`` rather than proximo's ``## [0.39.0]``,
  and historical entries use an em dash as the separator instead of a hyphen. The match is on the
  version token alone, so both forms work and neither is load-bearing.

Usage:  python scripts/public_commit_message.py broker 0.39.0

Prints to stdout. Exits non-zero printing NOTHING if the version has no entry, so a caller piping
this into `git commit-tree` fails loud rather than committing an empty body — the failure mode
this script exists to end.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PACKAGES = {"broker": "broker/CHANGELOG.md", "guard": "guard/CHANGELOG.md"}


def entry_for(version: str, changelog: str) -> str:
    """The CHANGELOG body for `version`, without its own heading.

    Matched on the version token with a boundary after it, so `0.39.0` never picks up
    `0.39.0rc1` or `0.39.01`. Everything up to the next `## ` heading is the entry.
    """
    start = re.search(rf"^## {re.escape(version)}(?![0-9A-Za-z._-])[^\n]*\n", changelog, re.M)
    if not start:
        raise SystemExit(
            f"public_commit_message: no '## {version}' entry in this CHANGELOG. "
            f"Write the release entry first — a bare '## Unreleased' does not satisfy it.")
    rest = changelog[start.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return (rest[:nxt.start()] if nxt else rest).strip()


def build(package: str, version: str, changelog: str) -> str:
    body = entry_for(version, changelog)
    if not body:
        raise SystemExit(f"public_commit_message: the {version} entry is empty")
    header = f"release: {package} {version}"
    tail = (
        "Full history for this release, commit by commit, is on the internal mirror; this\n"
        "public branch carries one squashed commit per release by design.\n"
        f"Every change here is described in {PACKAGES[package]}."
    )
    return f"{header}\n\n{body}\n\n{tail}\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in PACKAGES:
        raise SystemExit("usage: public_commit_message.py <broker|guard> X.Y.Z")
    package, version = argv
    root = Path(__file__).resolve().parent.parent
    print(build(package, version, (root / PACKAGES[package]).read_text(encoding="utf-8")), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
