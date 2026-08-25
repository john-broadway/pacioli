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

Vre="${V//./\\.}"   # escape the version's dots so they anchor literally in the grep below
if ! grep -q "^## $Vre " "$CL" && ! grep -q "^## $Vre\$" "$CL"; then
  printf 'release: NOTE — %s has no "## %s" entry yet. Write it (your words) before tagging.\n' "$CL" "$V"
fi

printf '\n== gate ==\n'
RC=0
"$PY" scripts/version_tools.py check || RC=1
"$PY" scripts/version_tools.py release-check "$PKG" "$V" || RC=1

# The broker's LobeHub manifest is GENERATED (tool surface + version) — regenerate it so a bump
# never ships a stale tool array or version banner. A version bump legitimately CHANGES lhm, so a
# resulting diff is expected release output, not a failure — note it (commit it with the release).
# The hard consistency gate is `version_tools.py check` above (lhm version == pyproject).
if [ "$PKG" = broker ]; then
  if [ -x broker/.venv/bin/python ]; then
    ( cd broker && .venv/bin/python scripts/gen_lobehub_manifest.py ) >/dev/null \
      || { printf 'release: gen_lobehub_manifest.py failed\n' >&2; RC=1; }
    git diff --quiet lhm.plugin.json \
      || printf 'release: NOTE — lhm.plugin.json regenerated for %s; commit it with the release.\n' "$V"
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
    # --color never is load-bearing, not cosmetic. uv colourises its own output when it decides a
    # terminal is attached, and the escape codes land IN the requirements file
    # ("\e[1ma2a-sdk\e[0m==1.1.1"), which pip-audit cannot parse. The audit then does not run at
    # all. Caught 2026-07-30 cutting 0.34.2: the same command had produced a clean file hours
    # earlier, so this depends on the environment rather than on anything in the repo.
    uv pip freeze --color never --python broker/.venv/bin/python 2>/dev/null \
      | grep -vE '^-e |pacioli' > "$REQS"
    # Refuse on an unparseable file rather than letting a green line stand for an audit that never
    # happened. A requirements line is `name==version`; anything else here means the generator, not
    # the dependency, is broken.
    # Counted, not `grep -q`. In some shells `grep` is a wrapper function (Claude Code routes it
    # to ugrep) whose -q exit code does not reflect -v selection, so a -q guard silently never
    # fires while -c and -n are correct. Measured 2026-07-30. Count, then compare.
    REQS_BAD="$(command grep -cvE '^[A-Za-z0-9._-]+==[^[:space:]]+$' "$REQS" 2>/dev/null || true)"
    if [ ! -s "$REQS" ] || [ "${REQS_BAD:-1}" != "0" ]; then
      printf 'release: pip-audit input is not clean requirements — the audit did NOT run.\n' >&2
      command grep -nvE '^[A-Za-z0-9._-]+==[^[:space:]]+$' "$REQS" 2>/dev/null | head -3 | cat -A >&2
      RC=1
    else
      "$TOOLVENV/bin/pip-audit" -r "$REQS" || RC=1
    fi
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

# The ADOPTER'S PATH, on the exact tree about to ship: build the wheel, install it into a throwaway
# venv, and prove the artifact actually works — entry point, the MCP door driven by a real client,
# and the HTTP door on a real socket. Broker only; guard is a Frappe bench app, not a plain-venv
# install, and install_smoke.sh scopes itself the same way.
#
# It is IN the gate as of 2026-08-17 because it was documented as "rail 8" and invoked by nothing:
# not CI, not this script. A check that runs only when someone remembers is a check that will be
# forgotten, and the defect class it exists to catch — an artifact that installs clean and cannot
# serve — has already shipped once. Costs roughly a minute, which is the price of not finding that
# out from an adopter.
if [ "$PKG" = broker ]; then
  SMOKELOG="$(mktemp)"
  if bash scripts/install_smoke.sh >"$SMOKELOG" 2>&1; then
    sed -n 's/^  ok   /release: install-smoke ok — /p' "$SMOKELOG"
  else
    printf 'release: install-smoke FAILED — the artifact this tree builds does not work:\n' >&2
    cat "$SMOKELOG" >&2
    RC=1
  fi
  rm -f "$SMOKELOG"
fi

printf '\n----------------------------------------\n'
if [ "$RC" -eq 0 ]; then
  if [ "$PKG" = broker ]; then
    PUBLISH_EXTRA=$' 10. mcp-publisher publish            # validates + pushes server.json to the official MCP registry\n 11. (LobeHub) npx -y @lobehub/market-cli plugin update --dir .   # update, NEVER 'publish' — in market-cli >=0.0.40 'publish' means a NEW listing and demands a gitUrl (proximo 0.31.1 ripple hit this)'
  else
    PUBLISH_EXTRA=' 10. (guard ships no MCP/LobeHub manifest — PyPI + gh release only)'
  fi
  cat <<EOF
release: $PKG $TAG set, gate GREEN.
NEXT (Claude does the git; John's go for the public push):
  1. write the "## $V" $CL entry (human prose) — it becomes the PUBLIC commit body in step 3
  2. commit, then: git tag $TAG
       internal gitea:  git push origin main && git push origin $TAG
       NEVER --tags. On a curated mirror old tags diverge local-vs-remote, so --tags is
       rejected forever and fails a push that already landed.
  3. build the curated public commit (strips .gitea/, refuses leaks):
       T=\$($PY scripts/release_leak_audit.py build-tree) || exit 1
       M=\$($PY scripts/public_commit_message.py $PKG $V) || exit 1   # the CHANGELOG entry IS the reason
       PUB=\$(printf '%s' "\$M" | git commit-tree "\$T" -p github/main -F -)
  4. PROVE the tree BEFORE public main moves. The curated commit is minted fresh, so the
     push would be CI's FIRST look at it — that is how 0.39.0 put a red X on public main
     (2026-08-24, coverage floor). Preflight the SAME tree on a throwaway public ref:
       git push github "\$PUB:refs/heads/preflight-$V"
       gh workflow run ci.yml -R john-broadway/pacioli --ref preflight-$V       # the job that reds
       gh workflow run codeql.yml -R john-broadway/pacioli --ref preflight-$V   # scorecard can't dispatch
       echo "\$(git rev-parse "\$PUB^{tree}")  <run url>" >> "\$(git rev-parse --git-dir)/proven-trees"
       git push github ":refs/heads/preflight-$V"
     The box pre-push guard (stage 0b, guard.requireProvenTree) refuses an unproven tree on
     public main. Re-running commit-tree mints a NEW commit but the SAME tree — the tree is
     the identity the record keys on.
  5. git push github "\$PUB:main"        # fast-forward, NEVER --force
  6. tag the PUBLIC line — the CURATED TWIN, never the local tag:
       git push github "\$PUB:refs/tags/$TAG"
       The local tag points at the INTERNAL commit; its history is the internal line, not the
       curated one. Pushing it publishes every internal commit. That is not hypothetical: it is
       how v0.24.0 exposed 592 commits (2026-08-09), and the pre-push guard caught the same
       mistake again during the 0.39.0 publish (2026-08-24).
  7. gh release create $TAG --target "\$PUB" --title "$TAG: <the one-line reason>" --notes-file <notes>
       A bare version number is not a title, and 90 bytes is not a body — v0.39.0 shipped that
       way while every release before it carried real notes (the defect John flagged on proximo
       the same day: "released 38 with no doc or desc"). Title = "$TAG: <reason>", no em dash.
       End the notes with a "## Where to read more" block linking README.md / SECURITY.md /
       $CL / the package README, plus the pip install line.
  8. approve the gated PyPI publish job    (John's click — tokenless OIDC, "pypi" environment)
  9. verify what actually PUBLISHED, do not assume:  scripts/install_smoke.sh --published $V
       (release-pypi.yml runs this itself now; this is the by-hand form)
$PUBLISH_EXTRA
release.sh never pushes.
EOF
else
  printf 'release: GATE NOT GREEN — fix findings above before tagging.\n'
fi
exit "$RC"
