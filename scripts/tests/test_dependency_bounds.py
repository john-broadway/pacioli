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


def test_every_adopter_facing_requirement_excludes_the_next_major():
    """The real question, asked properly: does this pin still admit the next major after its own
    floor? `<2.5` on a 1.x floor answers YES and is therefore a failure, however bounded it looks.
    """
    offenders = []
    for pkg, where, req in _runtime_requirements():
        name, spec = _spec_of(req)
        floor = _floor(spec)
        if floor is None:
            offenders.append(f"{pkg}/{where}: {req}  (no lower bound at all)")
            continue
        nxt = f"{floor.major + 1}.0.0"
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


def test_the_mcp_ceiling_excludes_the_sdk_major_that_removed_the_decorator_api():
    """Named, because this is the one that actually bit, and because the generic test above cannot
    speak for it: that test only knows about the major after the FLOOR. Lift this when the door is
    ported to mcp 2.x constructor callbacks, not before.
    """
    specs = [(p, w, s) for p, w, r in _runtime_requirements()
             for n, s in [_spec_of(r)] if n == "mcp" for w, p in [(w, p)]]
    assert specs, "mcp is the MCP door's dependency; it must stay declared"
    bad = [f"{p}/{w}" for p, w, s in specs if _admits(s, "2.0.0") or _admits(s, "2.0.0rc1")]
    assert not bad, (
        "mcp must stay excluded at 2.0.0 (and its prereleases) until "
        "pacioli.server._register_tool_handlers is ported off the decorator API "
        f"(Server.list_tools / Server.call_tool), which 2.x removed: {bad}")


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
