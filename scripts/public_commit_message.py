#!/usr/bin/env python3
"""Build the PUBLIC release commit message for a version, from that package's CHANGELOG entry.

Ported from proximo 2026-08-24, on John's word: "i asked for pacioli to release like proximo".

**Why this exists.** GitHub `main` here carries a curated, squashed history: one commit per
release, created with `git commit-tree` rather than by merging the internal line. Until now this
repo's `release.sh` handed the next session a hardcoded `-m "release: broker X.Y.Z"`, so the
last six releases (guard 0.14.0 + broker 0.37.0 through 0.39.1) showed no reason on the public
face; five earlier ones had carried it by hand. A visitor reading the commit list learned nothing.

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
        python scripts/public_commit_message.py broker 0.40.0 --with guard 0.15.0

The `--with` form is for a release that ships BOTH halves as ONE public commit (the
2026-08-11 precedent: guard 0.14.0 + broker 0.37.0 landed as one curated commit with
both tags on it). The subject names both versions and carries the first package's
title as the reason; the body carries both entries, each under its own heading, so
neither reason lives only on the internal mirror.

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


def title_for(version: str, changelog: str) -> str:
    """The heading's own title segment, for the SUBJECT line.

    The subject is the only text GitHub shows beside files and in the commit list; a
    body-only reason left every release here reading as bare "release: broker X.Y.Z"
    on the surfaces a visitor actually scans (proximo's twin defect, caught by John on
    proximo 0.39.0 — this is the same-hour class sweep). This repo's headings already
    carry the reason (`## X.Y.Z - date - title`, hyphen or em dash), so the subject
    reads it from there; a heading with no title refuses at release time.
    """
    m = re.search(rf"^## {re.escape(version)}(?![0-9A-Za-z._-])[^\n]*", changelog, re.M)
    assert m, "caller matched this heading already"
    parts = re.split(r"\s[—-]\s", m.group(0), maxsplit=2)
    title = parts[2].strip() if len(parts) == 3 else ""
    if not title:
        raise SystemExit(
            f"public_commit_message: the '## {version}' heading carries no title — "
            "the subject needs the one-line reason; write it into the heading "
            "(`## X.Y.Z - YYYY-MM-DD - <reason>`)")
    return title


def build(package: str, version: str, changelog: str) -> str:
    body = entry_for(version, changelog)
    if not body:
        raise SystemExit(f"public_commit_message: the {version} entry is empty")
    header = f"release: {package} {version}: {title_for(version, changelog)}"
    if len(header) > 72:
        raise SystemExit(
            f"public_commit_message: subject would be {len(header)} chars "
            f"(git's readable ceiling is ~72) — shorten the heading's title")
    return f"{header}\n\n{body}\n\n{_tail(PACKAGES[package])}\n"


def _tail(described_in: str) -> str:
    return (
        "Full history for this release, commit by commit, is on the internal mirror; this\n"
        "public branch carries one squashed commit per release by design.\n"
        f"Every change here is described in {described_in}."
    )


def build_dual(package: str, version: str, changelog: str,
               with_package: str, with_version: str, with_changelog: str) -> str:
    """One public commit carrying both halves. Subject: both versions, the first title."""
    if with_package == package:
        raise SystemExit("public_commit_message: --with must name the OTHER package")
    body = entry_for(version, changelog)
    with_body = entry_for(with_version, with_changelog)
    if not body:
        raise SystemExit(f"public_commit_message: the {package} {version} entry is empty")
    if not with_body:
        raise SystemExit(f"public_commit_message: the {with_package} {with_version} entry is empty")
    header = (f"release: {package} {version} + {with_package} {with_version}: "
              f"{title_for(version, changelog)}")
    if len(header) > 72:
        raise SystemExit(
            f"public_commit_message: subject would be {len(header)} chars "
            f"(git's readable ceiling is ~72): shorten the {package} heading's title")
    title, with_title = title_for(version, changelog), title_for(with_version, with_changelog)
    return (f"{header}\n\n"
            f"## {package} {version} - {title}\n\n{body}\n\n"
            f"## {with_package} {with_version} - {with_title}\n\n{with_body}\n\n"
            f"{_tail(f'{PACKAGES[package]} and {PACKAGES[with_package]}')}\n")


USAGE = ("usage: public_commit_message.py <broker|guard> X.Y.Z "
         "[--with <the other package> A.B.C]")


def parse_argv(argv: list[str]) -> tuple[str, str, tuple[str, str] | None]:
    """(package, version, (with_package, with_version) | None). Refuses every other shape."""
    if len(argv) == 2 and argv[0] in PACKAGES:
        return argv[0], argv[1], None
    if (len(argv) == 5 and argv[0] in PACKAGES and argv[2] == "--with"
            and argv[3] in PACKAGES and argv[3] != argv[0]):
        return argv[0], argv[1], (argv[3], argv[4])
    raise SystemExit(USAGE)


def main(argv: list[str]) -> int:
    package, version, with_ = parse_argv(argv)
    root = Path(__file__).resolve().parent.parent
    changelog = (root / PACKAGES[package]).read_text(encoding="utf-8")
    if with_ is None:
        print(build(package, version, changelog), end="")
        return 0
    with_package, with_version = with_
    with_changelog = (root / PACKAGES[with_package]).read_text(encoding="utf-8")
    print(build_dual(package, version, changelog, with_package, with_version, with_changelog),
          end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
