#!/usr/bin/env python3
"""Single source of truth for "where Pacioli's version lives" + a drift checker.

Pacioli is a TWO-package monorepo with INDEPENDENT versions, so this tool is
per-package:

  broker (`pacioli`)        pyproject → broker/pacioli/__init__.py, server.json
                            (every version field), lhm.plugin.json (top-level
                            version), broker/CHANGELOG.md (release heading)
  guard  (`pacioli-guard`)  pyproject → guard/pacioli_guard/__init__.py,
                            guard/CHANGELOG.md (release heading)

server.json / lhm.plugin.json are the broker's MCP-registry + LobeHub manifests;
guard ships no manifest. The CHANGELOG heading is human-authored (dated, prose)
and only VERIFIED here — set never rewrites it.

Consumed by:
  - scripts/tests/test_version_tools.py  (the always-on gate + set/check units)
  - .github/workflows, .gitea/workflows  (python scripts/version_tools.py check)
  - scripts/release.sh                   (set on release, release-check at cut)

Stdlib only — tomllib ships on the project's Python floor (>=3.11).
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Package:
    name: str
    pyproject: str
    init: str
    changelog: str
    # JSON manifests where EVERY "version" field equals this package's version.
    json_all: tuple[str, ...] = field(default_factory=tuple)
    # JSON manifests where only the FIRST "version" field is the package version
    # (the rest belong to nested tool schemas and must be left alone).
    json_first: tuple[str, ...] = field(default_factory=tuple)


PACKAGES: dict[str, Package] = {
    "broker": Package(
        name="broker",
        pyproject="broker/pyproject.toml",
        init="broker/pacioli/__init__.py",
        changelog="broker/CHANGELOG.md",
        json_all=("server.json",),
        json_first=("lhm.plugin.json",),
    ),
    "guard": Package(
        name="guard",
        pyproject="guard/pyproject.toml",
        init="guard/pacioli_guard/__init__.py",
        changelog="guard/CHANGELOG.md",
    ),
}

_INIT_RE = re.compile(r'(?m)^__version__\s*=\s*"([^"]+)"')
# Pacioli CHANGELOG heading:  ## 0.30.1 — 2026-07-17 — official MCP registry ...
# Capture the leading version token (digit-led, up to the first whitespace).
_HEADING_RE = re.compile(r'(?m)^##\s+(\d[^\s]*)')
_PYPROJECT_RE = re.compile(r'(?m)^version\s*=\s*"[^"]*"')
_INIT_SET_RE = re.compile(r'(?m)^__version__\s*=\s*"[^"]*"')
_JSON_VERSION_RE = re.compile(r'"version"\s*:\s*"[^"]*"')


def _pkg(name: str) -> Package:
    try:
        return PACKAGES[name]
    except KeyError:
        raise ValueError(f"unknown package {name!r}; expected one of {sorted(PACKAGES)}") from None


def read_pyproject_version(root: Path, package: str) -> str:
    data = tomllib.loads((root / _pkg(package).pyproject).read_text(encoding="utf-8"))
    return data["project"]["version"]


def read_init_version(root: Path, package: str) -> str:
    init = _pkg(package).init
    m = _INIT_RE.search((root / init).read_text(encoding="utf-8"))
    if not m:
        raise ValueError(f"no __version__ found in {init}")
    return m.group(1)


def read_json_all_versions(root: Path, rel: str) -> list[tuple[str, str]]:
    """(label, version) for every version field in a manifest: the top-level
    `version` plus each `packages[].version`."""
    data = json.loads((root / rel).read_text(encoding="utf-8"))
    versions: list[tuple[str, str]] = []
    if "version" in data:
        versions.append((f"{rel} top-level version", data["version"]))
    for i, pkg in enumerate(data.get("packages", [])):
        if "version" in pkg:
            ident = pkg.get("identifier", f"packages[{i}]")
            versions.append((f"{rel} packages[{i}] ({ident}) version", pkg["version"]))
    return versions


def read_json_top_version(root: Path, rel: str) -> str:
    """The top-level `version` of a manifest (ignores any nested tool versions)."""
    data = json.loads((root / rel).read_text(encoding="utf-8"))
    if "version" not in data:
        raise ValueError(f"no top-level version in {rel}")
    return data["version"]


def read_changelog_headings(root: Path, package: str) -> list[str]:
    text = (root / _pkg(package).changelog).read_text(encoding="utf-8")
    return [h.strip() for h in _HEADING_RE.findall(text)]


def top_released_changelog_version(root: Path, package: str) -> str | None:
    headings = read_changelog_headings(root, package)
    return headings[0] if headings else None


def _check_package(root: Path, package: str) -> list[str]:
    pkg = _pkg(package)
    problems: list[str] = []
    py = read_pyproject_version(root, package)
    init = read_init_version(root, package)
    if py != init:
        problems.append(f"[{package}] pyproject version {py!r} != __init__ __version__ {init!r}")
    for rel in pkg.json_all:
        for label, v in read_json_all_versions(root, rel):
            if v != py:
                problems.append(f"[{package}] {label} {v!r} != pyproject version {py!r}")
    for rel in pkg.json_first:
        v = read_json_top_version(root, rel)
        if v != py:
            problems.append(f"[{package}] {rel} top-level version {v!r} != pyproject version {py!r}")
    if py not in set(read_changelog_headings(root, package)):
        problems.append(
            f"[{package}] CHANGELOG has no '## {py}' heading for version {py!r} "
            f"(add the release entry — a bare '## Unreleased' does not satisfy this)"
        )
    return problems


def check_consistency(root: Path) -> list[str]:
    """Always-on checks across BOTH packages. Returns problems; empty == consistent."""
    problems: list[str] = []
    for name in PACKAGES:
        problems.extend(_check_package(root, name))
    return problems


def check_release(root: Path, package: str, tag_version: str) -> list[str]:
    """Release-time checks for ONE package: consistency + tag == pyproject and
    the version is the TOP released CHANGELOG heading (catches stale/out-of-order)."""
    problems = _check_package(root, package)
    py = read_pyproject_version(root, package)
    if tag_version != py:
        problems.append(f"[{package}] git tag version {tag_version!r} != pyproject version {py!r}")
    top = top_released_changelog_version(root, package)
    if top != py:
        problems.append(f"[{package}] CHANGELOG top released heading {top!r} != version {py!r}")
    return problems


def _subn_one(path: Path, pattern: re.Pattern[str], repl: str, what: str) -> None:
    new, n = pattern.subn(repl, path.read_text(encoding="utf-8"), count=1)
    if n != 1:
        raise ValueError(f"expected exactly one {what} in {path.name}, found {n}")
    path.write_text(new, encoding="utf-8")


def set_version(root: Path, package: str, version: str) -> None:
    """Rewrite one package's version in pyproject, __init__, and its manifests.
    The CHANGELOG heading is human-authored and NOT rewritten (verified by check)."""
    pkg = _pkg(package)
    _subn_one(root / pkg.pyproject, _PYPROJECT_RE, f'version = "{version}"', "top-level version=")
    _subn_one(root / pkg.init, _INIT_SET_RE, f'__version__ = "{version}"', "__version__=")
    for rel in pkg.json_all:
        sj = root / rel
        new, n = _JSON_VERSION_RE.subn(f'"version": "{version}"', sj.read_text(encoding="utf-8"))
        if n == 0:
            raise ValueError(f'expected at least one "version" field in {rel}, found 0')
        sj.write_text(new, encoding="utf-8")
    for rel in pkg.json_first:
        _subn_one(root / rel, _JSON_VERSION_RE, f'"version": "{version}"', 'first "version" field')


def _main(argv: list[str]) -> int:
    if argv[:1] == ["check"]:
        problems = check_consistency(REPO_ROOT)
        if problems:
            print("version drift:")
            for p in problems:
                print(f"  - {p}")
            return 1
        versions = ", ".join(f"{n}={read_pyproject_version(REPO_ROOT, n)}" for n in PACKAGES)
        print(f"version consistent: {versions}")
        return 0
    if len(argv) == 3 and argv[0] == "release-check":
        package, tag = argv[1], argv[2]
        tag = tag[1:] if tag.startswith("v") else tag
        problems = check_release(REPO_ROOT, package, tag)
        if problems:
            print("release drift:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(f"release consistent: {package}={read_pyproject_version(REPO_ROOT, package)}")
        return 0
    if len(argv) == 3 and argv[0] == "set":
        set_version(REPO_ROOT, argv[1], argv[2])
        print(f"set {argv[1]} version -> {argv[2]}")
        return 0
    print(
        "usage: version_tools.py [check | release-check <package> vX.Y.Z | set <package> X.Y.Z]\n"
        f"       packages: {', '.join(PACKAGES)}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
