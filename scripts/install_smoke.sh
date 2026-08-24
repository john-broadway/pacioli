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
  printf '== installing %s from PyPI (--refresh) ==\n' "$SPEC"
  # --refresh is not optional here: uv caches indexes and wheels, so minutes after a release this
  # would happily "verify" the PREVIOUS version's artifact out of cache and report the new one as
  # confirmed. The whole point of --published is to look at what PyPI is actually serving now.
  REFRESH=--refresh
  uv pip install -q --refresh --python "$PY" "$SPEC" \
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
  printf '  ok   `pacioli serve` subcommand is wired (argparse only; says nothing about the door)\n'
else
  printf '  FAIL `pacioli serve --help` did not run (the CLI subcommand is missing)\n'; RC=1
fi

# The door ITSELF. The check above is blind to a dead one. Measured 2026-08-16 against the 0.38.0
# wheel: a venv with no `mcp` at all, and a venv whose door advertised zero tools, BOTH passed every
# other assertion in this file. argparse prints help without reaching `build_server`, and `serve()`
# assembles the broker before it builds, so a credential-less run never touches the builder.
# `scripts/door_check.py` drives real in-process MCP round trips instead, two legs: the door lists
# the full catalog, and one tool call reaches the broker and renders back. It runs from $SMOKE so
# nothing in the repo root or cwd can shadow the installed package.
if DOOR="$(cd "$SMOKE" && "$PY" "$ROOT/scripts/door_check.py" 2>&1)"; then
  printf '  ok   the door BUILDS, LISTS and DISPATCHES: %s\n' "$DOOR"
else
  printf '  FAIL the door did not build, list or dispatch: %s\n' "$DOOR"; RC=1
fi

# The HTTP door on a REAL SOCKET. Everything above is in-process: it proves what serve_http
# builds and nothing it runs on, so uvicorn, the bind, and the console script an adopter actually
# types all sit below the line. This starts the installed `pacioli serve --http` on an ephemeral
# loopback port and talks to it over TCP. Exit 2 means it could not test, which must never read
# as a pass; exit 1 is a real failure.
SOCK="$(cd "$SMOKE" && "$PY" "$ROOT/scripts/door_socket_check.py" 2>&1)"; SOCK_RC=$?
case "$SOCK_RC" in
  # One line, deliberately: release.sh greps `^  ok   ` out of this output, so a multi-line
  # assertion would show up there as a heading with its evidence silently dropped.
  0) printf '  ok   the HTTP door binds a real socket: %s\n' \
       "$(printf '%s' "$SOCK" | awk 'NR>1{printf "; "} {printf "%s", $0}')" ;;
  2) printf '  FAIL the socket check could not run (UNPROVEN, not a pass): %s\n' "$SOCK"; RC=1 ;;
  *) printf '  FAIL the HTTP door did not serve over a real socket: %s\n' "$SOCK"; RC=1 ;;
esac

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

# THE REST DOOR, IN ITS OWN VENV WITH ITS EXTRA ALONE. Not a stylistic separation: `pacioli[rest]`
# declares uvicorn AND anyio precisely because uvicorn does not depend on anyio and `mcp` does, so
# checking REST inside the [server] venv above would prove nothing about the extra an adopter
# actually installs. That is the "an extra only ever installed alongside another is CARRIED, not
# verified" law, and it has shipped a dead door here twice.
printf '\n== rest door: a second venv, [rest] ALONE ==\n'
uv venv "$SMOKE/rvenv" -q || { printf 'smoke: rest venv create failed\n' >&2; exit 1; }
RPY="$SMOKE/rvenv/bin/python"
if [ "$MODE" = local ]; then
  RSPEC="${WHEEL}[rest]"
else
  RSPEC="pacioli[rest]"; [ -n "$PINVER" ] && RSPEC="pacioli[rest]==$PINVER"
fi
if uv pip install -q ${REFRESH:-} --python "$RPY" "$RSPEC"; then
  # mcp MUST be absent here, or the leg is measuring the [server] world again by accident.
  if "$RPY" -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("mcp") is None else 1)'; then
    printf '  ok   [rest] installs alone (no mcp present, so anyio is really its own dependency)\n'
  else
    printf '  FAIL mcp is present in the [rest]-only venv; this leg proves nothing about the extra\n'; RC=1
  fi
  RSOCK="$(cd "$SMOKE" && "$RPY" "$ROOT/scripts/door_socket_check.py" rest 2>&1)"; RSOCK_RC=$?
  case "$RSOCK_RC" in
    0) printf '  ok   the REST door binds a real socket: %s\n' \
         "$(printf '%s' "$RSOCK" | awk 'NR>1{printf "; "} {printf "%s", $0}')" ;;
    2) printf '  FAIL the REST socket check could not run (UNPROVEN, not a pass): %s\n' "$RSOCK"; RC=1 ;;
    *) printf '  FAIL the REST door did not serve over a real socket: %s\n' "$RSOCK"; RC=1 ;;
  esac
else
  printf '  FAIL pacioli[rest] did not install on its own\n' >&2; RC=1
fi

# THE A2A DOOR, IN ITS OWN VENV WITH ITS EXTRA ALONE. Through 0.37.1 `pacioli[a2a]` alone built
# an import error instead of a door (starlette arrived only via [server]'s mcp), and that class
# was invisible to ci.yml because its jobs install `.[server,a2a]` together — THIS leg is what
# closes it (pypi-smoke runs this script daily, and release.sh gates on it). Until 2026-08-24 it
# was run BY HAND when someone remembered — the 08-24 letter and a claims lens both named the
# gap. Manual is not a gate.
printf '\n== a2a door: a third venv, [a2a] ALONE ==\n'
uv venv "$SMOKE/avenv" -q || { printf 'smoke: a2a venv create failed\n' >&2; exit 1; }
APY="$SMOKE/avenv/bin/python"
if [ "$MODE" = local ]; then
  ASPEC="${WHEEL}[a2a]"
else
  ASPEC="pacioli[a2a]"; [ -n "$PINVER" ] && ASPEC="pacioli[a2a]==$PINVER"
fi
if uv pip install -q ${REFRESH:-} --python "$APY" "$ASPEC"; then
  # mcp MUST be absent here, or the leg is measuring the [server] world again by accident —
  # the exact carried-not-verified accident that hid the missing starlette for six releases.
  if "$APY" -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("mcp") is None else 1)'; then
    printf '  ok   [a2a] installs alone (no mcp present, so its declared deps really are its own)\n'
  else
    printf '  FAIL mcp is present in the [a2a]-only venv; this leg proves nothing about the extra\n'; RC=1
  fi
  ASOCK="$(cd "$SMOKE" && "$APY" "$ROOT/scripts/door_socket_check.py" a2a 2>&1)"; ASOCK_RC=$?
  case "$ASOCK_RC" in
    0) printf '  ok   the A2A door binds a real socket: %s\n' \
         "$(printf '%s' "$ASOCK" | awk 'NR>1{printf "; "} {printf "%s", $0}')" ;;
    2) printf '  FAIL the A2A socket check could not run (UNPROVEN, not a pass): %s\n' "$ASOCK"; RC=1 ;;
    *) printf '  FAIL the A2A door did not serve over a real socket: %s\n' "$ASOCK"; RC=1 ;;
  esac
else
  printf '  FAIL pacioli[a2a] did not install on its own\n' >&2; RC=1
fi

printf '\n----------------------------------------\n'
if [ "$RC" -eq 0 ]; then
  printf 'install-smoke: PASS — the %s artifact serves on all three extras: [server] (MCP stdio door + HTTP door on a real socket), [rest] ALONE and [a2a] ALONE (each single-extra door on a real socket, mcp absent from both single-extra venvs).\n' \
    "$([ "$MODE" = local ] && echo 'freshly-built' || echo 'published')"
else
  printf 'install-smoke: FAIL — see assertions above.\n'
fi
exit "$RC"
