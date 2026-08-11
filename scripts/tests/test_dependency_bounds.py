"""Every adopter-facing dependency must bound its MAJOR version in the PUBLISHED metadata.

Earned 2026-08-11, and this is the second time in twelve days that the same upstream major broke
one of ours. `mcp` 2.0.0 shipped 2026-07-28 and removed the decorator registration API
(``Server.list_tools`` / ``Server.call_tool``) that ``pacioli.server._register_tool_handlers`` is
built on. `broker/pyproject.toml` declared ``mcp>=1.0`` with no ceiling, so from that date every
fresh ``pip install 'pacioli[server]'`` resolved an SDK the MCP door could not start against. Every
published version, 0.30.0 through 0.37.0, on a new install. Fixed in 0.37.1 by the bound this file
now guards.

Nothing in this repo could see it. The local venv held mcp 1.28.1, so every local run was green and
honest about a world no adopter was in. CI DID resolve mcp 2.0.0 and was also green, because no test
started the door until 0.37.0 added one. **A lockfile, or a venv, protects the build. Only a bound in
the published metadata protects an adopter.**

The reason this test exists rather than a note: proximo took the identical break on 2026-07-30 and
the lesson was written down, capped, and guarded THERE. Pacioli was not swept, and shipped unbounded
for twelve more days. A rule that lives in one repo's memory is not a rule, it is a coincidence.

Deliberately stdlib-only (`tomllib`) and run by the `version-consistency` CI job, so it gates every
push without needing either package installed.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
PYPROJECTS = {"broker": REPO / "broker" / "pyproject.toml", "guard": REPO / "guard" / "pyproject.toml"}

# A bound that closes the CURRENT major: `<2`, `<2.0`, `<2.0.0`, or `~=1.2` / `==1.*`.
_BOUNDED = re.compile(r"<\s*\d|~=|==\s*\d+\.\*")

# The ONE deliberate exemption, and it is not a convenience. `cryptography` is reached only through
# `serialization` and `asymmetric.ec`, its most stable public surface, and its floor here tracks a
# security advisory (PYSEC-2026-3552, fixed in 50.0.0). A major cap would make US the thing standing
# between an adopter and the next advisory fix, which is the failure mode recorded against proximo on
# 2026-08-06: a ceiling added to protect them held them inside a vulnerable range instead. The price
# of the exemption is that its FLOOR gets a test of its own, below.
_FLOOR_GUARDED = {"cryptography"}


def _dist_name(req: str) -> str:
    """`a2a-sdk[signing]>=1.1,<2` -> `a2a-sdk`."""
    return re.split(r"[<>=!~\[;\s]", req.strip(), maxsplit=1)[0]


def _requirements() -> list[tuple[str, str, str]]:
    """(package, where, requirement) for everything an adopter can resolve."""
    out: list[tuple[str, str, str]] = []
    for pkg, path in PYPROJECTS.items():
        data = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        out += [(pkg, "dependencies", r) for r in data.get("dependencies") or []]
        for extra, reqs in (data.get("optional-dependencies") or {}).items():
            out += [(pkg, f"optional-dependencies.{extra}", r) for r in reqs]
    return out


def test_every_adopter_facing_requirement_bounds_its_major():
    unbounded = [f"{pkg}/{where}: {req}" for pkg, where, req in _requirements()
                 if _dist_name(req) not in _FLOOR_GUARDED and not _BOUNDED.search(req)]
    assert not unbounded, (
        "these requirements leave the next major open, so a breaking upstream release silently "
        "breaks every NEW install off PyPI while every lockfile-backed check stays green "
        "(e.g. 'mcp>=1.0,<2'):\n  " + "\n  ".join(unbounded)
    )


def test_the_mcp_bound_excludes_the_sdk_major_that_removed_the_decorator_api():
    """Named explicitly, because this is the one that actually bit.

    The test above only asks whether SOME upper bound closes the next major, so it would stay green
    if someone widened this to `<3` while the door still registers handlers the 1.x way. Lift this
    test when the door is ported to the 2.x `MCPServer` / `add_request_handler` shape, not before.
    """
    reqs = [r for _, _, r in _requirements() if _dist_name(r) == "mcp"]
    assert reqs, "mcp is the MCP door's dependency; it must stay declared"
    assert all("<2" in r.replace(" ", "") for r in reqs), (
        "mcp must stay capped below 2.0.0 until pacioli.server._register_tool_handlers is ported "
        f"off the decorator API (Server.list_tools / Server.call_tool), which 2.x removed: {reqs}")


def test_the_floor_guarded_deps_stay_at_or_above_their_advisory_floor():
    """The price of every entry in `_FLOOR_GUARDED`, since the major-bound test cannot see a floor.

    A version literal on purpose: this must fail offline, in CI, and in a year, without depending on
    an advisory service still serving the same JSON. RAISE a floor here when a future advisory forces
    it. Never lower one to make a resolution problem go away.
    """
    floors = {"cryptography": (50, 0, 0)}   # PYSEC-2026-3552, fixed in 50.0.0
    offenders: list[str] = []
    for pkg, where, req in _requirements():
        name = _dist_name(req)
        if name not in floors:
            continue
        m = re.search(r">=\s*([0-9]+(?:\.[0-9]+)*)", req)
        # Pad to three components before comparing. `>=50` parses to `(50,)`, and `(50,) < (50,0,0)`
        # is True, so an unpadded compare reports the CURRENT correct floor as an offender. Caught
        # by running the green case: the mutation rows alone would have looked like a working gate.
        got = (tuple(int(p) for p in m.group(1).split(".")) + (0, 0, 0))[:3] if m else ()
        if not got or got < floors[name]:
            offenders.append(f"{pkg}/{where}: {req}")
    assert not offenders, (
        "these requirements admit a version below a floor that exists because of a published "
        f"advisory {floors}:\n  " + "\n  ".join(offenders)
    )
