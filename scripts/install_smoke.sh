#!/usr/bin/env bash
# pacioli install-smoke — prove the SHIPPED broker artifact installs clean into a FRESH venv
# and its agent surface loads. The achievable "prove-it-real" slice: the broker fail-closes without
# an ERPNext credential, so we don't boot the server — we prove the entry point runs, the `serve`
# door is wired, and the TOOLS table loads (a credential-less crawl of an empty TOOLS is the F-grade
# failure this guards against).
#
#   local (default)  build the wheel from the CURRENT tree (uv build) and smoke THAT — a pre-release
#                    gate on the tree you're about to ship.
#   --published      pip-install pacioli[server] from PyPI instead (post-release confirmation).
#
# Usage:
#   scripts/install_smoke.sh                 # local wheel, current tree
#   scripts/install_smoke.sh --published     # PyPI, latest pacioli
#   scripts/install_smoke.sh --published 0.30.1
#
# Scoped to the BROKER (`pacioli`). Guard (`pacioli-guard`) is a Frappe bench app, not a plain-venv
# pip install — its live proof is the scoped-token bench run, not this.
set -uo pipefail

MODE=local
PINVER=""
case "${1:-}" in
  --published) MODE=published; PINVER="${2:-}" ;;
  "") : ;;
  *) printf 'usage: install_smoke.sh [--published [X.Y.Z]]\n' >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || { printf 'smoke: cannot cd to repo root\n' >&2; exit 1; }

TREE_VER="$(grep -m1 '^version' broker/pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')"

SMOKE="$(mktemp -d)"
cleanup() { rm -rf "$SMOKE"; }
trap cleanup EXIT

printf '== install-smoke (%s) — fresh venv ==\n' "$MODE"
uv venv "$SMOKE/venv" -q || { printf 'smoke: venv create failed\n' >&2; exit 1; }
PY="$SMOKE/venv/bin/python"
PACIOLI="$SMOKE/venv/bin/pacioli"

if [ "$MODE" = local ]; then
  printf '== building broker wheel from the current tree ==\n'
  # Build from a pristine state. setuptools' incremental ./build/ dir re-packs whatever it copied
  # on a prior run — including modules the current config EXCLUDES (e.g. tests) — so a stale build/
  # would make the local smoke diverge from CI's fresh-checkout build. These are regenerable caches.
  rm -rf broker/build broker/*.egg-info
  if ! ( cd broker && uv build --wheel --out-dir "$SMOKE/dist" ) >"$SMOKE/build.log" 2>&1; then
    printf 'smoke: uv build failed:\n' >&2; cat "$SMOKE/build.log" >&2; exit 1
  fi
  WHEEL="$(ls "$SMOKE"/dist/*.whl 2>/dev/null | head -1)"
  [ -n "$WHEEL" ] || { printf 'smoke: no wheel produced\n' >&2; exit 1; }
  printf 'built: %s\n' "$(basename "$WHEEL")"
  uv pip install -q --python "$PY" "${WHEEL}[server]" \
    || { printf 'smoke: install of local wheel failed\n' >&2; exit 1; }
else
  SPEC="pacioli[server]"; [ -n "$PINVER" ] && SPEC="pacioli[server]==$PINVER"
  printf '== installing %s from PyPI ==\n' "$SPEC"
  uv pip install -q --python "$PY" "$SPEC" \
    || { printf 'smoke: install from PyPI failed\n' >&2; exit 1; }
fi

# What version did we actually land, and what do we require it to be?
INSTALLED="$("$PY" -c 'from pacioli import __version__; print(__version__)' 2>&1)"
if   [ "$MODE" = local ]; then WANT="$TREE_VER"
elif [ -n "$PINVER" ];    then WANT="$PINVER"
else                           WANT="$INSTALLED"   # published-latest: self-consistency only
fi

printf '\n== assertions ==\n'
RC=0

GOT_VER="$("$PACIOLI" --version 2>&1)"
if [ "$GOT_VER" = "pacioli $WANT" ] && [ "$INSTALLED" = "$WANT" ]; then
  printf '  ok   entry point + version: %s\n' "$GOT_VER"
else
  printf '  FAIL version: `pacioli --version`=%q, __version__=%q, want %q\n' "$GOT_VER" "$INSTALLED" "$WANT"; RC=1
fi

if "$PACIOLI" serve --help >/dev/null 2>&1; then
  printf '  ok   `pacioli serve` door is wired (not booted — needs a credential)\n'
else
  printf '  FAIL `pacioli serve --help` did not run (the [server] door is not wired)\n'; RC=1
fi

GOT_TOOLS="$("$PY" -c 'from pacioli.server import TOOLS; print(len(TOOLS))' 2>&1)"
if printf '%s' "$GOT_TOOLS" | grep -qE '^[0-9]+$' && [ "$GOT_TOOLS" -ge 1 ]; then
  printf '  ok   tool surface loads offline: %s tools\n' "$GOT_TOOLS"
else
  printf '  FAIL tool surface did not load: %s\n' "$GOT_TOOLS"; RC=1
fi

# The distributed wheel must NOT carry the test suite (bloat + a stray importable pacioli.tests).
if "$PY" -c 'import pacioli.tests' >/dev/null 2>&1; then
  printf '  FAIL the wheel ships the test suite (pacioli.tests is importable) — exclude it in packages.find\n'; RC=1
else
  printf '  ok   test suite not shipped (pacioli.tests absent from the artifact)\n'
fi

printf '\n----------------------------------------\n'
if [ "$RC" -eq 0 ]; then
  printf 'install-smoke: PASS — the %s pacioli[server] artifact installs clean and its surface loads.\n' \
    "$([ "$MODE" = local ] && echo 'freshly-built' || echo 'published')"
else
  printf 'install-smoke: FAIL — see assertions above.\n'
fi
exit "$RC"
