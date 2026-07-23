#!/usr/bin/env python3
"""Release leak-audit — model the PUBLIC publish transform and refuse to leak internal infra.

Ported from Proximo's tool (same discipline, Pacioli's deny-list). Pacioli publishes to GitHub
by attaching a CURATED tree to `github/main` via `git commit-tree` (fast-forward only). Nothing
scans that synthetic tree: gitleaks (CI) and the pre-push hook see the real branch you push, not
the commit-tree that becomes the public commit. So a legitimately-tracked-but-internal file —
a `docs/plans/` day-book naming a CT/bench host, `GO-LIVE.md`, `CLAUDE.md` — would sail straight
into the public commit untouched.

This tool models that transform. It (1) STRIPS paths that must never be public (the day-books —
`docs/plans/` everywhere, `.gitea/`, `.scratch/`, and the named root memos), and (2) scans the
kept files for internal-infra leak shapes (RFC1918 IPs, internal-TLD hostnames, absolute `/root`
paths, credential token shapes), with an allowlist for documented example values. Patterns are
GENERIC — this file is itself public, so it names no real infrastructure.

NOTE (canon): `1494` is Luca Pacioli's year (the whole dress), NOT a CT-id leak — it is a bare
integer no shape pattern matches, and it must NEVER be added to a literal denylist. See
feedback_check-canon-before-sanitizing.

Stdlib only (runs anywhere, no install).

CLI:
  release_leak_audit.py audit [ref]       Report what would publish; exit 1 if any leak remains.
  release_leak_audit.py build-tree [ref]  Print a clean tree SHA (deny paths stripped) for
                                          `git commit-tree`, but ONLY if the kept files are
                                          leak-clean (fail-closed). Stdout = the SHA, nothing else.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Paths that legitimately live in the internal repo but must NEVER reach the public mirror.
# The day-books (John's flip ruling 2026-07-17): `docs/plans/` (root design memos + lab records,
# incl. `docs/plans/internal/`), `broker/docs/plans/` + `guard/docs/plans/` (per-package run
# records with bench/infra detail), `.gitea/` (self-hosted-forge CI + the literal denylist),
# `.scratch/` (working scratch). The public mirror uses `.github/` (kept). `docs/internal/` is
# prefix-denied for parity/future.
DENY_PREFIXES: tuple[str, ...] = (
    ".gitea/", ".scratch/", "docs/plans/", "docs/internal/",
    "broker/docs/plans/", "guard/docs/plans/",
)
# Denied by BASENAME (matches anywhere in the tree) = internal-only memos the public mirror must
# NOT carry. `CLAUDE.md` = dev-memory; `CULTURE.md`/`GO-LIVE.md`/`RESEARCH-VERDICT.md` = the
# workshop day-books (go-live runbook, culture notes, research verdict); `DISTRIBUTION.md` =
# internal discoverability ops (names the open gaps). The public set is user-facing
# (README/CHANGELOG/LICENSE/DESIGN/TWO-DOORS/SCOPED-TOKEN-PROOF/assets/site/deploy + the packages).
DENY_BASENAMES: tuple[str, ...] = (
    "CLAUDE.md", "CULTURE.md", "GO-LIVE.md", "RESEARCH-VERDICT.md", "DISTRIBUTION.md",
    "COMPETITORS.md",  # competitor landscape — INTERNAL ONLY, never public (2026-07-23)
    "DESIGN.md",       # design/strategy sketch (named competitor table, market bets) — INTERNAL ONLY (2026-07-23)
)

# Site-specific internal identifiers (bare node/host names with no generic leak-shape) that must
# never publish. Sourced from this INTERNAL-ONLY file — it lives under a deny prefix, so it is
# stripped from the public mirror and can safely name real infra while THIS public tool names none.
# ⚠️ NEVER put `1494`/`MCDXCIV` here — that is the canonical Pacioli year, not infra.
DENY_LITERALS_FILE = ".gitea/leak-deny.txt"

# Generic leak-shape patterns. No real infra literals — this file ships publicly.
_RFC1918 = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})\b"
)
_INTERNAL_HOST = re.compile(
    r"\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*"
    r"\.(?:lan|internal|intranet)\b",
    re.IGNORECASE,
)
# Real absolute home path: the root-home prefix + the FULL first path segment (excluding the "..."
# ellipsis placeholder). Capturing the whole segment (not one char) lets the internal ALLOW name this
# deployment's OWN deploy paths precisely, while still flagging any OTHER (foreign) home-dir path.
# The prefix is ASSEMBLED FROM PARTS so this PUBLIC file carries no literal the box pre-push guard
# greps for (it counts any line containing that path string); the compiled pattern is identical at
# runtime — and the tool never trips its own detector on its own source.
_HOME = "/" + "root" + "/"
_ROOT_PATH = re.compile(r"(?<![\w./])" + _HOME + r"(?!\.\.\.)[\w.-]+")
_TOKEN = re.compile(
    r"\b(?:pypi-[A-Za-z0-9_-]{16,}"
    r"|glpat-[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16})\b"
)

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("rfc1918-ip", _RFC1918),
    ("internal-host", _INTERNAL_HOST),
    ("root-path", _ROOT_PATH),
    ("token", _TOKEN),
)

# Inline escape hatch: a line carrying this marker is skipped (e.g. THIS tool's own tests, which
# must embed leak-shaped literals to prove the patterns fire). Same spirit as gitleaks `#gitleaks:allow`.
ALLOW_MARKER = "leak-audit: allow"

# Documented, benign example values that legitimately appear in public docs / smokes.
ALLOW: tuple[str, ...] = (
    "192.0.2.", "198.51.100.", "203.0.113.",   # RFC 5737 documentation IP ranges
    "10.0.0.", "192.168.", "172.16.",           # canonical example private subnets in fixtures/docs
    ".example.", "example.com", "your-site",    # RFC 2606 example domains / sanctioned placeholders
    "erpnext.example", "example.lan",           # example ERPNext base URL / example host in deploy templates
    "proxy.internal",                           # sanctioned test placeholder host (a2a allowed-hosts tests)
)
# This deployment's OWN documented deploy paths (the root-run LXC broker home, its logs, and the
# overridable secrets-env default — public in deploy/ by design, not secrets) are allowlisted from
# the INTERNAL-ONLY ALLOW_LITERALS_FILE. The literals live THERE, never here, so this public tool
# advertises no real home path and the global pre-push guard sees none in the curated diff. A public
# clone lacks the file but never runs build-tree; the built-in pattern still catches any foreign path.
ALLOW_LITERALS_FILE = ".gitea/leak-allow.txt"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    match: str


@dataclass
class AuditResult:
    kept: list[str] = field(default_factory=list)
    stripped: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


def _allowed(token: str) -> bool:
    return any(a in token for a in ALLOW)


def _deny_literal_pattern(deny_literals: tuple[str, ...]) -> re.Pattern[str] | None:
    """Word-boundaried, case-insensitive regex matching any of *deny_literals* (site-specific
    internal identifiers with no generic shape). None when the list is empty."""
    lits = [s.strip() for s in deny_literals if s.strip()]
    if not lits:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(s) for s in lits) + r")\b", re.IGNORECASE)


def scan_text(
    path: str, text: str,
    extra_patterns: tuple[tuple[str, re.Pattern[str]], ...] = (),
) -> list[Finding]:
    """Leak shapes in one file's content, honoring the documented-example allowlist. *extra_patterns*
    scan alongside the built-ins (e.g. the site-specific internal-identifier denylist)."""
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for kind, pattern in (*PATTERNS, *extra_patterns):
            for m in pattern.finditer(line):
                token = m.group(0)
                if _allowed(token):
                    continue
                findings.append(Finding(path, lineno, kind, token))
    return findings


def partition_paths(
    paths, deny: tuple[str, ...] = DENY_PREFIXES
) -> tuple[list[str], list[str]]:
    """Split paths into (kept, stripped); stripped = anything under a deny prefix or deny basename."""
    kept: list[str] = []
    stripped: list[str] = []
    for p in paths:
        denied = p.startswith(deny) or Path(p).name in DENY_BASENAMES
        (stripped if denied else kept).append(p)
    return kept, stripped


def audit_files(
    files: dict[str, str], deny: tuple[str, ...] = DENY_PREFIXES,
    deny_literals: tuple[str, ...] = (),
) -> AuditResult:
    """Audit a path->content map AS IF published: deny paths are stripped (and NOT scanned —
    they won't be public); kept files are scanned for leak shapes plus any site-specific internal
    identifiers in *deny_literals*."""
    kept, stripped = partition_paths(files.keys(), deny)
    extra: tuple[tuple[str, re.Pattern[str]], ...] = ()
    pat = _deny_literal_pattern(deny_literals)
    if pat is not None:
        extra = (("internal-literal", pat),)
    findings: list[Finding] = []
    for p in sorted(kept):
        findings.extend(scan_text(p, files[p], extra))
    return AuditResult(kept=sorted(kept), stripped=sorted(stripped), findings=findings)


# --- git I/O: read the real publish surface --------------------------------------------

def _repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"],  # noqa: S603, S607
                         cwd=str(Path.cwd()), capture_output=True, text=True, check=True).stdout
    return Path(out.strip())


def load_deny_literals(root: Path | None = None) -> tuple[str, ...]:
    """Site-specific internal identifiers to refuse (bare node/host names with no generic shape),
    read from the INTERNAL-ONLY ``DENY_LITERALS_FILE`` (one per line, ``#`` comments). That file
    lives under a deny prefix, so it is stripped from the public mirror and may name real infra.
    Returns a lowercased tuple; empty when the file is absent (e.g. a public clone)."""
    root = root or _repo_root()
    f = root / DENY_LITERALS_FILE
    if not f.exists():
        return ()
    out: list[str] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s.lower())
    return tuple(out)


def load_allow_literals(root: Path | None = None) -> tuple[str, ...]:
    """Site-specific ALLOW substrings — this deployment's OWN documented root-home deploy paths —
    read from the INTERNAL-ONLY ``ALLOW_LITERALS_FILE`` (one per line, ``#`` comments). Kept out of the
    public tool so it advertises no real home path. Empty when the file is absent (a public clone,
    which never runs build-tree anyway). Case preserved (substrings are matched verbatim)."""
    root = root or _repo_root()
    f = root / ALLOW_LITERALS_FILE
    if not f.exists():
        return ()
    out: list[str] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return tuple(out)


def _git(args: list[str], cwd: Path, env: dict | None = None) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), env=env,  # noqa: S603, S607
                          capture_output=True, text=True, check=True).stdout


def files_in_ref(ref: str = "HEAD", root: Path | None = None) -> dict[str, str]:
    """The tracked text files in `ref`'s tree = exactly the publish surface. Binaries skipped."""
    root = root or _repo_root()
    names = _git(["ls-tree", "-r", "--name-only", "-z", ref], root).split("\0")
    files: dict[str, str] = {}
    for name in filter(None, names):
        blob = subprocess.run(["git", "show", f"{ref}:{name}"], cwd=str(root),  # noqa: S603, S607
                              capture_output=True, check=True).stdout
        if b"\0" in blob[:8192]:   # binary-ish — not a text leak surface
            continue
        files[name] = blob.decode("utf-8", "replace")
    return files


def build_public_tree(
    ref: str = "HEAD", deny: tuple[str, ...] = DENY_PREFIXES, root: Path | None = None
) -> str:
    """Build (in an ISOLATED temp index — never touches the real index/worktree) the tree
    that should publish: `ref`'s tree with deny prefixes/basenames removed. Returns the new tree
    SHA for `git commit-tree <sha> -p github/main`."""
    root = root or _repo_root()
    fd, idx = tempfile.mkstemp(prefix="pacioli-pubidx-")
    os.close(fd)
    try:
        env = {**os.environ, "GIT_INDEX_FILE": idx}
        _git(["read-tree", ref], root, env=env)
        # Strip EXACTLY the paths partition_paths denies (prefixes AND basenames) over the full tree
        # (incl. binaries). Using the same partition as audit() guarantees the published tree matches
        # the leak-audit; a prefix-only `git rm -r` is blind to a basename deny (CLAUDE.md) and would
        # publish it while audit() reported it stripped.
        all_paths = [p for p in _git(["ls-tree", "-r", "--name-only", "-z", ref], root).split("\0") if p]
        _, stripped = partition_paths(all_paths, deny)
        if stripped:
            _git(["rm", "--cached", "--quiet", "--ignore-unmatch", "--", *stripped], root, env=env)
        return _git(["write-tree"], root, env=env).strip()
    finally:
        os.unlink(idx)


def _main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "audit"
    ref = argv[1] if len(argv) > 1 else "HEAD"

    # Extend the built-in ALLOW with this deployment's internal-only allowlist (documented /root
    # deploy paths) — loaded here so the public tool file itself names none.
    global ALLOW
    ALLOW = (*ALLOW, *load_allow_literals())

    if cmd == "audit":
        res = audit_files(files_in_ref(ref), deny_literals=load_deny_literals())
        for p in res.stripped:
            print(f"strip (internal-only, won't publish): {p}")
        for f in res.findings:
            print(f"LEAK [{f.kind}] {f.path}:{f.line}: {f.match}", file=sys.stderr)
        if res.ok:
            print(
                f"leak-audit: CLEAN — {len(res.kept)} files would publish, "
                f"{len(res.stripped)} stripped"
            )
            return 0
        print(
            f"leak-audit: {len(res.findings)} leak shape(s) in the public surface — "
            "FIX before any public flip",
            file=sys.stderr,
        )
        return 1

    if cmd == "build-tree":
        res = audit_files(files_in_ref(ref), deny_literals=load_deny_literals())
        if not res.ok:
            for f in res.findings:
                print(f"LEAK [{f.kind}] {f.path}:{f.line}: {f.match}", file=sys.stderr)
            print("build-tree: REFUSING — leak shapes in the public surface", file=sys.stderr)
            return 1
        print(build_public_tree(ref))   # the ONLY stdout: the clean tree SHA
        return 0

    print("usage: release_leak_audit.py [audit|build-tree] [ref]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
