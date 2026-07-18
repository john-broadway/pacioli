#!/usr/bin/env bash
# pacioli release tool — make the MECHANICAL parts of a per-package release deterministic.
#
# Pacioli is a TWO-package monorepo, so a release names its package:
#   broker -> PyPI `pacioli`        (tag vX.Y.Z),      MCP registry + LobeHub
#   guard  -> PyPI `pacioli-guard`  (tag guard-vX.Y.Z)
#
# Sets the version in the ONE source (pyproject → __init__ + manifests via version_tools.py),
# regenerates the broker's LobeHub manifest, then runs the local gate (consistency + release-check
# + lint + version tests + leak-audit + gitleaks). Writes NO prose: the CHANGELOG entry stays yours.
# NEVER pushes — stops at "ready".
#
# Usage: scripts/release.sh <broker|guard> X.Y.Z     e.g.  scripts/release.sh broker 0.31.0
set -uo pipefail

usage() { printf 'usage: release.sh <broker|guard> X.Y.Z\n' >&2; }

PKG="${1:-}"
V="${2:-}"
case "$PKG" in broker|guard) : ;; *) usage; exit 2 ;; esac
[ -n "$V" ] || { usage; exit 2; }

# Honest semver: pre-1.0 stays 0.x; a major>=1 must be intentional.
case "$V" in
  0.*) : ;;
  [1-9]*|*[!0-9.a-z-]*)
    if [ "${PACIOLI_RELEASE_FORCE_MAJOR:-}" != "1" ]; then
      printf 'release: refusing "%s" — pre-1.0 discipline keeps it 0.x; set PACIOLI_RELEASE_FORCE_MAJOR=1 to override.\n' "$V" >&2
      exit 1
    fi ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || { printf 'release: cannot cd to repo root\n' >&2; exit 1; }

# Repo-level tooling runs in the pacioli-root .venv (ruff + pytest); version_tools parses
# pyproject with tomllib (stdlib >=3.11). Prefer the tooling venv's interpreter, fall back to python3.
TOOLVENV=".venv"
PY="${PACIOLI_PY:-}"
[ -n "$PY" ] || { [ -x "$TOOLVENV/bin/python" ] && PY="$TOOLVENV/bin/python" || PY="python3"; }
"$PY" -c 'import tomllib' 2>/dev/null || {
  printf 'release: %s lacks tomllib — create the tooling venv (uv venv && uv pip install pytest ruff) or set PACIOLI_PY to a python>=3.11.\n' "$PY" >&2
  exit 1
}

if [ "$PKG" = broker ]; then CL="broker/CHANGELOG.md"; TAG="v$V"; else CL="guard/CHANGELOG.md"; TAG="guard-v$V"; fi

printf '== release: setting %s version %s ==\n' "$PKG" "$V"
"$PY" scripts/version_tools.py set "$PKG" "$V" || { printf 'release: version set failed\n' >&2; exit 1; }

if ! grep -q "^## $V " "$CL" && ! grep -q "^## $V\$" "$CL"; then
  printf 'release: NOTE — %s has no "## %s" entry yet. Write it (your words) before tagging.\n' "$CL" "$V"
fi

printf '\n== gate ==\n'
RC=0
"$PY" scripts/version_tools.py check || RC=1
"$PY" scripts/version_tools.py release-check "$PKG" "$V" || RC=1

# The broker's LobeHub manifest is GENERATED (tool surface + version) — regenerate and fail on
# drift, so a version bump can never ship a stale tool array or a mismatched version banner.
if [ "$PKG" = broker ]; then
  if [ -x broker/.venv/bin/python ]; then
    ( cd broker && .venv/bin/python scripts/gen_lobehub_manifest.py ) >/dev/null \
      || { printf 'release: gen_lobehub_manifest.py failed\n' >&2; RC=1; }
    git diff --exit-code --stat lhm.plugin.json \
      || { printf 'release: lhm.plugin.json drifted — commit the regenerated manifest.\n' >&2; RC=1; }
  else
    printf 'release: broker/.venv missing — cannot regenerate lhm.plugin.json (run: cd broker && uv venv && uv pip install -e ".[server,a2a]").\n' >&2
    RC=1
  fi
fi

# The version-tools mechanics + the live drift gate, and lint — both in the tooling venv.
if [ -x "$TOOLVENV/bin/pytest" ] && [ -x "$TOOLVENV/bin/ruff" ]; then
  "$TOOLVENV/bin/pytest" scripts/tests -q -ra || RC=1
  "$TOOLVENV/bin/ruff" check . || RC=1   # full repo — match CI's `ruff check .`
else
  printf 'release: tooling venv incomplete — run: uv venv && uv pip install pytest ruff pip-audit\n' >&2
  RC=1
fi

# pip-audit the broker's [server,a2a] dependency closure (guard + the pure cores have no runtime
# deps). Missing tools => warn (CI's pip-audit job is the authoritative gate); a real vuln => fail.
if [ "$PKG" = broker ]; then
  if [ -x broker/.venv/bin/python ] && [ -x "$TOOLVENV/bin/pip-audit" ]; then
    REQS="$(mktemp)"
    uv pip freeze --python broker/.venv/bin/python 2>/dev/null | grep -vE '^-e |pacioli' > "$REQS"
    "$TOOLVENV/bin/pip-audit" -r "$REQS" || RC=1
    rm -f "$REQS"
  else
    printf 'release: skipping pip-audit — need broker/.venv (with extras) + pip-audit in the tooling venv.\n' >&2
  fi
fi

"$PY" scripts/release_leak_audit.py audit || RC=1   # model the public tree; refuse internal-infra leaks

# Public CI also runs gitleaks (entropy rules the shape-audit doesn't model). Run the same scan
# over the MODELED public tree when gitleaks is available.
if command -v gitleaks >/dev/null 2>&1; then
  GLTMP="$(mktemp -d)"
  if T="$("$PY" scripts/release_leak_audit.py build-tree 2>/dev/null | tail -1)" \
     && [ -n "$T" ] && git archive "$T" | tar -x -C "$GLTMP"; then
    gitleaks detect --no-git --source "$GLTMP" --no-banner --redact --exit-code=1 || RC=1
  else
    printf 'release: could not model the public tree for gitleaks\n' >&2; RC=1
  fi
  rm -rf "$GLTMP"
else
  printf 'release: WARNING — gitleaks not installed; public CI runs it and WILL fail on entropy hits this gate never saw.\n' >&2
fi

printf '\n----------------------------------------\n'
if [ "$RC" -eq 0 ]; then
  if [ "$PKG" = broker ]; then
    PUBLISH_EXTRA=$'  6. mcp-publisher publish            # validates + pushes server.json to the official MCP registry\n  7. (LobeHub) npx -y @lobehub/market-cli plugin publish --dir .'
  else
    PUBLISH_EXTRA='  6. (guard ships no MCP/LobeHub manifest — PyPI + gh release only)'
  fi
  cat <<EOF
release: $PKG $TAG set, gate GREEN.
NEXT (Claude does the git; John's go for the public push):
  1. write the "## $V" $CL entry (human prose)
  2. commit, then: git tag $TAG   (internal gitea: git push origin main --tags)
  3. publish to github via the curated FF tree (strips .gitea/, refuses leaks):
       T=\$($PY scripts/release_leak_audit.py build-tree) || exit 1
       C=\$(git commit-tree "\$T" -p github/main -m "release: $PKG $V")
       git push github "\$C:main"          # fast-forward, NEVER --force
  4. gh release create $TAG                # release event fires release-pypi.yml (SBOM + provenance)
  5. approve the gated PyPI publish job    (John's click — tokenless OIDC, "pypi" environment)
$PUBLISH_EXTRA
release.sh never pushes.
EOF
else
  printf 'release: GATE NOT GREEN — fix findings above before tagging.\n'
fi
exit "$RC"
