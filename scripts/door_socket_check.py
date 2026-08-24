# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Prove the INSTALLED broker's HTTP door binds a real socket and answers over real TCP.

Run by ``scripts/install_smoke.sh`` inside the smoke venv. This is the leg
``scripts/door_check.py`` deliberately does not cover.

**Why both exist.** ``door_check.py`` drives the MCP stack in-process over ASGI, which proves
everything ``serve_http`` builds and nothing it *runs on*. ``uvicorn.run``, the bind, the CLI
plumbing that reaches them, and the console script itself all sit below that line. The suite
cannot close it either: nothing in the 3400-test broker suite opens a socket, and a socket test
is the one kind that can flake, so it belongs on the prove-it-real rail rather than in the unit
suite. This starts the SHIPPED ``pacioli`` console script as a subprocess and talks to it over
loopback TCP.

**No credential is involved.** ``serve_http`` needs ``PACIOLI_REGISTRY`` to assemble a broker,
but the store and ERPNext client are lazy closures resolved at call time, so a registry naming an
unreachable ``https://erp.invalid`` target is enough to boot. Only ``tools/list`` is exercised,
which never reaches either closure. Nothing is planned, consented or spent, and no ERPNext is
contacted. The registry written here holds no secret: the credential fields are ``env:`` refs to
variables that are never set.

Takes the door to check as its one argument, ``mcp`` (default) or ``rest``, because the two ship
under different extras and "an extra only ever installed alongside another is carried, not
verified" -- the REST leg must run in a venv that has ``pacioli[rest]`` and nothing else.

Two legs per door, both against a real port:

1. **loopback, no token** -- the door binds and answers a real request.
2. **token configured** -- an unauthenticated POST is answered 401 by the bearer gate before the
   door's own layer, and the correct bearer is admitted, on the same real socket.

Exits 0 and prints one line per leg; exits 1 with the reason on stderr. Exits 2 if the
environment cannot support the check at all, so "could not test" can never read as "passed".
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

_BOOT_TIMEOUT = 30.0          # generous: a cold import of the 265-tool catalog is not instant
_TOKEN = "socket-check-token-not-a-real-secret"

# The MCP layer requires both content types on Accept; without them it answers 406 before doing
# any work. Worth naming, because a 406 from a request that omitted them looks like a refusal by
# the door and is not one.
_MCP_HEADERS = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18"}
_INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "socket-check", "version": "0"}}}


def _free_port():
    """Ask the OS for a port, then hand it to the child.

    Racy in principle, since the port is released before the child claims it. Accepted because
    the alternative is a fixed port, which collides with a developer's own running door and with
    a parallel run of this same script. The child's own bind failure is caught below either way.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port, proc, timeout=_BOOT_TIMEOUT):
    """Wait until something accepts on the port, or the child dies. Returns True/False."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        with socket.socket() as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _post(port, body, headers, path="/mcp"):
    """One real HTTP POST over TCP. Returns (status, body_text). stdlib only, no httpx: the two
    mcp majors are typed to different httpx distributions and this check must not care."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST",
                                 data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _console_script():
    """The installed ``pacioli`` entry point beside this interpreter.

    Deliberately the console script and not ``python -m pacioli``: the package ships no
    ``__main__`` (measured, that invocation fails), and the console script is what an adopter
    actually runs, so it is the honest subject for a shipped-artifact check.
    """
    exe = Path(sys.executable).parent / "pacioli"
    return exe if exe.exists() else None


def _start(env, port, auth=None, door="mcp"):
    flag = {"mcp": "--http", "rest": "--rest"}[door]
    argv = [str(_console_script()), "serve", flag,
            "--bind", "127.0.0.1", "--port", str(port)]
    if auth:
        argv += ["--auth", auth]
    return subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)


def _stop(proc):
    proc.terminate()
    try:
        return proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate(timeout=10)


def _base_env(state_dir):
    registry = state_dir / "targets.toml"
    registry.write_text('[targets.smoke]\nbase_url = "https://erp.invalid"\n'
                        'api_key = "env:PACIOLI_SOCKET_CHECK_UNSET_KEY"\n'
                        'api_secret = "env:PACIOLI_SOCKET_CHECK_UNSET_SECRET"\n'
                        'default = true\n', encoding="utf-8")
    env = dict(os.environ)
    env.update({"PACIOLI_REGISTRY": str(registry), "PACIOLI_STATE_DIR": str(state_dir)})
    # Make sure the credential refs really are unresolvable, so a stray value in the ambient
    # environment cannot turn this into a check that quietly had credentials after all.
    env.pop("PACIOLI_SOCKET_CHECK_UNSET_KEY", None)
    env.pop("PACIOLI_SOCKET_CHECK_UNSET_SECRET", None)
    return env


def _leg_lists_over_tcp(state_dir):
    """The door binds a real port and answers tools/list over it."""
    port = _free_port()
    proc = _start(_base_env(state_dir), port)
    try:
        if not _wait_for_port(port, proc):
            out, err = _stop(proc)
            return None, f"the door never bound {port} (exit={proc.returncode}): {err.strip()[:400]}"
        status, body = _post(port, _INITIALIZE, dict(_MCP_HEADERS))
        if status != 200:
            return None, f"initialize over TCP answered {status}: {body[:300]}"
        if "pacioli" not in body:
            return None, f"initialize did not name the server: {body[:300]}"
        return f"bound :{port}, initialize answered 200 over real TCP", None
    finally:
        if proc.poll() is None:
            _stop(proc)


def _leg_refuses_without_bearer(state_dir):
    """With a token configured, an unauthenticated POST is 401 on the real socket."""
    port = _free_port()
    env = _base_env(state_dir)
    env["PACIOLI_SOCKET_CHECK_TOKEN"] = _TOKEN
    proc = _start(env, port, auth="env:PACIOLI_SOCKET_CHECK_TOKEN")
    try:
        if not _wait_for_port(port, proc):
            out, err = _stop(proc)
            return None, f"the door never bound {port} (exit={proc.returncode}): {err.strip()[:400]}"
        # Identical request three ways, so the ONLY variable is the Authorization header.
        missing, _ = _post(port, _INITIALIZE, dict(_MCP_HEADERS))
        if missing != 401:
            return None, f"an unauthenticated POST got {missing}, not 401"
        wrong, _ = _post(port, _INITIALIZE,
                         dict(_MCP_HEADERS, Authorization="Bearer not-the-token"))
        if wrong != 401:
            return None, f"a wrong bearer got {wrong}, not 401"
        # And the gate must ADMIT the right one, or a gate that refuses everything would pass
        # both legs above. 200 rather than merely "not 401": a 500 is not an admission.
        good, body = _post(port, _INITIALIZE,
                           dict(_MCP_HEADERS, Authorization=f"Bearer {_TOKEN}"))
        if good != 200 or "pacioli" not in body:
            return None, (f"the correct bearer did not get a served answer: {good} "
                          f"{body[:200]}")
        return f"bound :{port}, no bearer 401 / wrong bearer 401 / correct bearer 200", None
    finally:
        if proc.poll() is None:
            _stop(proc)


# --- the REST door -------------------------------------------------------------------------
# Plain JSON over HTTP, no MCP handshake and no Accept dance. /v1/reconcile is used because it is
# a fixed governed verb that needs no doctype and no document: dispatch answers it structurally
# without a live bench, so the leg proves the door and never the books.
_REST_HEADERS = {"Content-Type": "application/json"}
_REST_PATH = "/v1/reconcile"


def _rest_leg_answers_over_tcp(state_dir):
    port = _free_port()
    proc = _start(_base_env(state_dir), port, door="rest")
    try:
        if not _wait_for_port(port, proc):
            _out, err = _stop(proc)
            return None, f"the REST door never bound {port} (exit={proc.returncode}): {err.strip()[:400]}"
        status, body = _post(port, {}, dict(_REST_HEADERS), path=_REST_PATH)
        if status != 200:
            return None, f"{_REST_PATH} over TCP answered {status}, not 200: {body[:300]}"
        # A governed refusal is a 200 carrying ok:false. Either verdict proves the door routed to
        # the spine and rendered its answer; what must NOT happen is a 404 (never routed) or a
        # 500 (glue broke).
        if '"ok"' not in body:
            return None, f"the REST door answered 200 without a spine verdict: {body[:300]}"
        return f"bound :{port}, {_REST_PATH} answered 200 with a spine verdict over real TCP", None
    finally:
        if proc.poll() is None:
            _stop(proc)


def _rest_leg_refuses_without_bearer(state_dir):
    port = _free_port()
    env = _base_env(state_dir)
    env["PACIOLI_SOCKET_CHECK_TOKEN"] = _TOKEN
    proc = _start(env, port, auth="env:PACIOLI_SOCKET_CHECK_TOKEN", door="rest")
    try:
        if not _wait_for_port(port, proc):
            _out, err = _stop(proc)
            return None, f"the REST door never bound {port} (exit={proc.returncode}): {err.strip()[:400]}"
        missing, _ = _post(port, {}, dict(_REST_HEADERS), path=_REST_PATH)
        if missing != 401:
            return None, f"an unauthenticated REST POST got {missing}, not 401"
        wrong, _ = _post(port, {}, dict(_REST_HEADERS, Authorization="Bearer not-the-token"),
                         path=_REST_PATH)
        if wrong != 401:
            return None, f"a wrong bearer got {wrong}, not 401"
        good, body = _post(port, {}, dict(_REST_HEADERS, Authorization=f"Bearer {_TOKEN}"),
                           path=_REST_PATH)
        if good != 200 or '"ok"' not in body:
            return None, f"the correct bearer did not get a served answer: {good} {body[:200]}"
        return f"bound :{port}, no bearer 401 / wrong bearer 401 / correct bearer 200", None
    finally:
        if proc.poll() is None:
            _stop(proc)


_DOORS = {"mcp": (_leg_lists_over_tcp, _leg_refuses_without_bearer),
          "rest": (_rest_leg_answers_over_tcp, _rest_leg_refuses_without_bearer)}


def main():
    door = sys.argv[1] if len(sys.argv) > 1 else "mcp"
    if door not in _DOORS:
        print(f"usage: door_socket_check.py [{'|'.join(_DOORS)}]", file=sys.stderr)
        return 2
    try:
        import pacioli.server  # noqa: F401
    except ImportError as exc:
        print(f"UNPROVEN: pacioli is not importable here ({exc})", file=sys.stderr)
        return 2
    if _console_script() is None:
        print(f"UNPROVEN: no `pacioli` console script beside {sys.executable}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        for leg in _DOORS[door]:
            note, problem = leg(state)
            if problem:
                print(problem, file=sys.stderr)
                return 1
            print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
