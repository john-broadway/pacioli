"""Every adopter-facing dependency must bound its MAJOR version in the PUBLISHED metadata.

Earned 2026-08-11, twice in one day, and the second time was this file.

**The bug.** `mcp` 2.0.0 (2026-07-28) removed the decorator registration API
(``Server.list_tools`` / ``Server.call_tool``) that ``pacioli.server._register_tool_handlers`` is
built on, and `broker/pyproject.toml` declared ``mcp>=1.0`` with no ceiling. Every published
version 0.30.0 through 0.37.0 could not start its MCP door on a fresh install made on or after
that date. A lockfile, a venv, and even a green CI protect the build. Only a bound in the
published metadata protects an adopter.

**The bug in the fix.** 0.37.1 added this file, and its first version asked
``"<2" in requirement`` — a SUBSTRING test. ``mcp>=1.0,<2.5`` contains ``<2``, so the guard named
after the break went green on a pin that resolves mcp 2.0.0 and reproduces the break byte for
byte. It also read distribution names case-sensitively, so ``Cryptography>=42`` slipped the
advisory floor entirely; and it asked only whether SOME upper bound existed, so ``a2a-sdk<3``
passed while admitting the very major it was written to exclude. An adversarial review found all
three. **A guard that answers the easy question is worse than no guard, because it is believed.**

This paragraph narrates the 0.37.1-era bug, not the pin as it now stands: 0.38.0 ported the door
itself, so mcp 2.0.0 no longer reproduces a break today. `SUPPORTED_MAJORS` below now admits it
deliberately, and `test_the_mcp_range_admits_both_supported_majors_and_excludes_the_next_unknown_one`
asserts exactly that.

So this version asks the only question that matters, with a real parser rather than string
matching: **given what this requirement admits, is the version we know breaks us still allowed?**

Deliberately dependency-light: `packaging` is the reference implementation of PEP 440/508 and it
arrives with `pytest`, which the `version-consistency` CI job already installs. Do not
reimplement version comparison here with tuples or regexes. That is what went wrong.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO = Path(__file__).resolve().parent.parent.parent
# Globbed, not a hardcoded list: a third package added later is guarded on arrival rather than
# silently exempt. `*/pyproject.toml` is exactly the two package roots today (broker, guard).
PYPROJECTS = sorted(REPO.glob("*/pyproject.toml"))


def _runtime_requirements() -> list[tuple[str, str, str]]:
    """(package, where, requirement) for everything an adopter resolves at INSTALL time."""
    out: list[tuple[str, str, str]] = []
    for path in PYPROJECTS:
        pkg = path.parent.name
        proj = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
        out += [(pkg, "dependencies", r) for r in proj.get("dependencies") or []]
        for extra, reqs in (proj.get("optional-dependencies") or {}).items():
            out += [(pkg, f"optional-dependencies.{extra}", r) for r in reqs]
    return out


def _build_requirements() -> list[tuple[str, str, str]]:
    """(package, where, requirement) for BUILD-time deps. Adopter-facing on an sdist build."""
    out: list[tuple[str, str, str]] = []
    for path in PYPROJECTS:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        out += [(path.parent.name, "build-system.requires", r)
                for r in data.get("build-system", {}).get("requires") or []]
    return out


def _spec_of(req: str) -> tuple[str, SpecifierSet]:
    """Parsed (canonical name, specifier). `Requirement` handles extras, markers and URLs, so a
    marker can never be mistaken for a version bound the way a whole-string regex did."""
    r = Requirement(req)
    return canonicalize_name(r.name), r.specifier


def _floor(spec: SpecifierSet) -> Version | None:
    """Lowest version the specifier admits, from its lower-bound operators."""
    lows = [Version(s.version.rstrip(".*")) for s in spec if s.operator in (">=", ">", "==", "~=")]
    return min(lows) if lows else None


def _admits(spec: SpecifierSet, version: str) -> bool:
    """Prereleases counted IN deliberately: `<2` must exclude `2.0.0rc1` too, and a floor must not
    be satisfiable by `50.0.0.dev1`. The default (prereleases excluded) would score both wrong."""
    return spec.contains(Version(version), prereleases=True)


# Dependencies proven to run on more than one major, measured rather than assumed. This is DATA a
# reader can see, not a name a reader has to trust: the rule below stays the same for every entry
# here, rather than carving any of them out of the rule.
#
#   mcp        1.x and 2.x BOTH proven against the real SDK (full 265-tool catalog, both majors
#              green).
#   anyio      3.x and 4.x BOTH proven 2026-08-24, and this entry exists BECAUSE the ceiling rule
#              below demanded it. Lowering anyio's floor to its measured 3.4.0 turned a quiet
#              single-major pin into an explicit two-major claim, and the gate refused to let that
#              claim stay implicit -- which is the behaviour this table was designed for. Measured
#              on `[rest]` ALONE (no mcp, so nothing else imposed a floor), full REST suite green
#              at 3.4.0, 3.6.2, 3.7.1, 4.0.0, 4.2.0 and 4.5.0. Note `[server]` and `[a2a]` never
#              actually resolve that low: mcp imposes >=4.5 and starlette >=3.6.2, so the widened
#              floor is inert there and load-bearing only on `[rest]`.
#   starlette  0.x and 1.x BOTH proven 2026-08-24. starlette crossed a real 0.x -> 1.x boundary,
#              and `a2a.py` imports it directly, so a `<2` ceiling over a `0.49.1` floor claims a
#              major nobody had run. Measured instead: `pacioli[a2a]` installed ALONE on py3.13
#              (mcp absent, so nothing is carried in by a neighbouring extra), 76 a2a tests green
#              at starlette 0.49.1 and green again at 1.6.0. The 0.x leg is mutation-proven
#              EXERCISED, not merely green: hiding the installed `starlette` package turns 27 of
#              those 76 red. A suite that never reaches a dependency proves nothing about which
#              version of it is installed.
SUPPORTED_MAJORS: dict[str, int] = {"mcp": 2, "starlette": 1, "anyio": 4}


# The LOWEST version of each dependency measured GREEN, on a real resolution, with the door driven.
# A declared floor may sit at or below its entry here; above it is a restriction with no evidence,
# and the only thing such a floor can do is exclude installs that work while CI -- which resolves
# the newest of everything -- stays green and tells you nothing.
#
# Earned 2026-08-24, when three of the floors in this repo turned out to be guesses wearing the
# clothes of measurements. `starlette` was set to 0.49.1 on a justification two lenses refuted,
# then to 0.48 because that is what one arbitrary `fastapi==0.118.0` pin happened to resolve to;
# both silently locked out working installs. `anyio` and `uvicorn` on `[rest]` carried floors
# inherited from what `mcp` DECLARES -- but `[rest]` installs no mcp, so on the one extra where
# nothing else enforced them, our invented numbers were the binding constraint.
#
# To RAISE an entry here you need a measured RED, not a preference. Put the failing version and
# what failed in the comment beside it.
MEASURED_FLOORS: dict[str, str] = {
    "anyio": "3.4.0",        # `[rest]` ALONE, no mcp present; full REST suite green
    "uvicorn": "0.20.0",     # `[rest]` ALONE; real-socket leg green, AND the exposed-socket guard
                             # still refused a forged Host from a LAN socket, so `scope["server"]`
                             # is reported correctly that far back
    "starlette": "0.20.4",   # twelve versions through 1.6.0, all green, real resolutions
    "pyjwt": "2.0.0",        # a2a-sdk[signing]'s own floor; `PyJWK.from_dict` present in every 2.x
    "mcp": "1.10.0",         # the oldest mcp this package's own door suite passes on
}
# `cryptography` is deliberately absent: its floor is a POLICY floor (PYSEC-2026-3552), argued in
# broker/pyproject.toml, and must NOT be relaxed toward measured evidence.


def test_no_floor_sits_above_the_evidence_for_it():
    """A declared floor must admit the lowest version measured green.

    The sibling checks in this file all ask about CEILINGS, because an unbounded major is the loud
    failure. This asks the quiet one. A floor set too high breaks nothing we can see: the package
    still builds, CI still passes, every test here still goes green, and the only casualty is an
    adopter whose stack pins that dependency lower -- who gets an unresolvable install and no
    explanation. That is precisely how `pacioli[a2a]` became uninstallable alongside FastAPI.
    """
    offenders = []
    for pkg, where, req in _runtime_requirements():
        name, spec = _spec_of(req)
        measured = MEASURED_FLOORS.get(name)
        if measured is None:
            continue
        if not _admits(spec, measured):
            offenders.append(
                f"{pkg}/{where}: {req}  (excludes {name} {measured}, measured green)")
    assert not offenders, (
        "these floors sit ABOVE the lowest version measured green, so they exclude installs that "
        "work and nothing in CI will ever notice:\n  " + "\n  ".join(offenders) +
        "\n\nEither lower the floor, or raise MEASURED_FLOORS with a measured RED beside it.")


def test_every_adopter_facing_requirement_excludes_the_next_major():
    """The real question, asked properly: does this pin still admit the major AFTER the highest
    one this package is proven to run on? For almost every dependency that highest-proven major is
    just its floor's own major, so `<2.5` on a 1.x floor answers YES and is therefore a failure,
    however bounded it looks. `mcp` answers the same question with a different highest-proven
    input, `SUPPORTED_MAJORS["mcp"]`, because it is proven across two consecutive majors rather
    than one: the ceiling must still exclude the major past THAT, not past the floor's major.
    """
    offenders = []
    for pkg, where, req in _runtime_requirements():
        name, spec = _spec_of(req)
        floor = _floor(spec)
        if floor is None:
            offenders.append(f"{pkg}/{where}: {req}  (no lower bound at all)")
            continue
        highest_proven = max(SUPPORTED_MAJORS.get(name, 0), floor.major)
        nxt = f"{highest_proven + 1}.0.0"
        if _admits(spec, nxt):
            offenders.append(f"{pkg}/{where}: {req}  (admits {name} {nxt})")
    assert not offenders, (
        "these requirements still admit the next major of a dependency, so a breaking upstream "
        "release silently breaks every NEW install off PyPI while every lockfile-backed check "
        "stays green:\n  " + "\n  ".join(offenders)
    )


def test_build_requirements_are_bounded():
    """Weaker on purpose, and the weakness is the point being stated rather than hidden. A build
    backend ships majors far more often than it breaks us, so demanding next-major tightness here
    would be churn with no safety. Demanding SOME ceiling is not: `setuptools>=77.0` had drifted
    seven majors past its floor with nothing watching.
    """
    offenders = [f"{pkg}/{where}: {req}" for pkg, where, req in _build_requirements()
                 if not any(s.operator in ("<", "<=", "==", "~=") for s in _spec_of(req)[1])]
    assert not offenders, (
        "build-system requirements need an upper bound; an sdist build is adopter-facing:\n  "
        + "\n  ".join(offenders)
    )


def test_the_starlette_range_admits_both_supported_majors_and_excludes_the_next_unknown_one():
    """The sibling of the mcp test, for the second entry in SUPPORTED_MAJORS.

    Added 2026-08-24 after a mutation lens narrowed the `[a2a]` pin to `starlette>=1.0,<2` and the
    whole repo-level suite stayed green: the dual-major claim lived only in a comment and in a
    commit message, measured once by hand. `mcp` had a test asserting both directions; starlette
    did not, so nothing stopped a future edit from silently dropping the 0.x half.

    The FLOOR is asserted here too, and it is the half this line keeps getting wrong. It was first
    `0.49.1`, justified by a claim two lenses refuted. Then `0.48`, which read like measurement but
    was just whatever one arbitrary `fastapi==0.118.0` pin resolved to. Both silently excluded
    installs that work. The floor is now the bottom of the evidence: twelve versions from 0.20.4 to
    1.6.0 pass on real resolutions, so anything above 0.20 is a restriction we cannot support with
    a measurement. Raise it only when a version is measured RED.
    """
    specs = [(p, w, s) for p, w, r in _runtime_requirements()
             for n, s in [_spec_of(r)] if n == "starlette" for w, p in [(w, p)]]
    assert specs, (
        "starlette is imported directly by a2a.py and must stay declared by the extra that gates "
        "it; an undeclared dependency is invisible to every other test in this file")
    for p, w, s in specs:
        assert _admits(s, "0.48.0"), (
            f"{p}/{w} no longer admits starlette 0.48.0, which real adopter stacks land on: "
            "FastAPI caps starlette <0.49.0 across a recent range. (The absolute floor is "
            "enforced for every dependency by MEASURED_FLOORS above.)")
        assert _admits(s, "1.6.0"), f"{p}/{w} no longer admits starlette 1.x, which the door runs on"
        assert not _admits(s, "2.0.0"), (
            f"{p}/{w} admits starlette 2.0.0, an unknown major. Measure the door on it first.")
        assert not _admits(s, "2.0.0rc1"), (
            f"{p}/{w} admits a prerelease of starlette 2.0.0, an unknown major.")


def test_the_mcp_range_admits_both_supported_majors_and_excludes_the_next_unknown_one():
    """0.37.1 capped mcp at `<2` because 2.x removed the decorator registration API. 0.38.0 ports
    the door to run on both, so the cap moves to `<3` and this test changes shape with it.

    It asserts BOTH directions on purpose. An accidental re-narrowing to `<2` silently drops 2.x
    support and every other test here would stay green, because they only ask about ceilings."""
    specs = [(p, w, s) for p, w, r in _runtime_requirements()
             for n, s in [_spec_of(r)] if n == "mcp" for w, p in [(w, p)]]
    assert specs, "mcp is the MCP door's dependency; it must stay declared"
    for p, w, s in specs:
        assert _admits(s, "1.29.0"), f"{p}/{w} no longer admits mcp 1.x, which the door supports"
        assert _admits(s, "2.0.0"), f"{p}/{w} no longer admits mcp 2.x, which the door supports"
        assert not _admits(s, "3.0.0"), (
            f"{p}/{w} admits mcp 3.0.0, an unknown major. Port the door first, then widen.")
        assert not _admits(s, "3.0.0rc1"), (
            f"{p}/{w} admits a prerelease of mcp 3.0.0, an unknown major. Port the door first, "
            "then widen.")


def test_the_mcp_floor_excludes_versions_this_package_cannot_run_on():
    """The other end of the range, which 0.37.1 shipped without checking and got wrong.

    Two measured facts, both re-checkable by installing the version and running the suite:
      * `mcp.server.streamable_http_manager` does not exist before 1.8.0, so `serve --http` could
        not start anywhere in 1.0-1.7 while `[server]` advertised it.
      * `pacioli/tests/test_server.py` is RED below 1.10.0, where the SDK gained `validate_input`.
    A floor is a promise that everything at or above it works. `>=1.0` was never that promise.
    """
    specs = [(p, w, s) for p, w, r in _runtime_requirements()
             for n, s in [_spec_of(r)] if n == "mcp" for w, p in [(w, p)]]
    bad = [f"{p}/{w} admits mcp {v}" for p, w, s in specs
           for v in ("1.0.0", "1.7.1", "1.9.4") if _admits(s, v)]
    assert not bad, (
        "the mcp floor admits versions this package does not work on (HTTP door missing below "
        f"1.8.0; door suite red below 1.10.0): {bad}")


def test_the_a2a_extra_declares_the_extra_that_carries_starlette():
    """`a2a.py` imports `a2a.server.routes.*`, which needs starlette + sse-starlette. The `signing`
    extra does not carry them. Through 0.37.1 they arrived only as a transitive of `[server]`'s
    mcp, so `pip install 'pacioli[a2a]'` on its own built an ImportError instead of a door, and
    CI could not see it because CI installs `.[server,a2a]` together.
    """
    missing = []
    for pkg, where, req in _runtime_requirements():
        r = Requirement(req)
        if canonicalize_name(r.name) == "a2a-sdk" and "http-server" not in r.extras:
            missing.append(f"{pkg}/{where}: {req}  (extras={sorted(r.extras)})")
    assert not missing, (
        "a2a-sdk must be requested with the 'http-server' extra, which is what supplies starlette "
        "and sse-starlette to the A2A door:\n  " + "\n  ".join(missing))


def test_the_cryptography_floor_excludes_the_advisory_range():
    """A floor carrying a security property needs its own named test, because the ceiling tests
    above cannot see a floor at all.

    PYSEC-2026-3552 (PKCS#7 EnvelopedData Bleichenbacher oracle) affects cryptography
    >=44.0.0,<50.0.0, fixed in 50.0.0. RAISE this when a future advisory forces it. Never lower it
    to make a resolution problem go away. A version literal on purpose: this must fail offline, in
    CI, and in a year, without depending on an advisory service still answering.
    """
    bad = []
    for pkg, where, req in _runtime_requirements():
        name, spec = _spec_of(req)
        if name != "cryptography":
            continue
        # `50.0.0.dev1` included: a prerelease below the fix is still below the fix, and the
        # first version of this test scored it as satisfying the floor.
        bad += [f"{pkg}/{where} admits cryptography {v}"
                for v in ("44.0.0", "49.0.0", "50.0.0.dev1") if _admits(spec, v)]
    assert not bad, (
        "these admit a cryptography inside PYSEC-2026-3552's affected range (>=44.0.0,<50.0.0), "
        f"or a prerelease below the 50.0.0 fix: {bad}")
