"""Every third-party module a shipped module IMPORTS must be DECLARED by the extra that gates it.

Earned 2026-08-24, and the sibling gate is the reason it had to be a separate file.

**The blind spot.** ``test_dependency_bounds`` reads DECLARATIONS and asks whether each one bounds
its major. It is structurally incapable of noticing a dependency that was never declared at all:
there is no requirement string for it to parse. So the hazard the pyproject's own comment block
names for ``uvicorn`` -- "an undeclared, unbounded, inherited dependency, which is the exact hazard
the rest of this block exists to close" -- could reappear in a neighbouring extra and every gate in
the repo would stay green.

It had. Measured 2026-08-24, on the fold that was about to be released:

* ``server.py`` imports ``anyio`` directly; ``[server]`` did not declare it. It arrived from
  ``mcp``, which declares ``anyio>=4.5`` with NO ceiling. An anyio 5.0 would have broken
  ``serve --http`` and ``serve --stdio`` on every fresh install, exactly as mcp 2.0.0 did.
* ``a2a.py`` imports ``anyio``, ``starlette`` and ``jwt`` directly; ``[a2a]`` declared none of
  them. They arrived from ``a2a-sdk[http-server]``, which declares ``starlette`` with no bound of
  ANY kind, not even a floor.

**How this composes.** This file only asks whether a dependency is declared in the right place. It
deliberately does not check bounds, because the moment a dependency IS declared,
``test_dependency_bounds`` starts policing its ceiling automatically. One gate makes the blind spot
visible; the other polices what it reveals. Keep them separate: a single test that did both would
have to skip the bounds question for anything undeclared, which is how the blind spot survived.

**Ground truth owes nothing to the code under test.** ``DOOR_EXTRAS`` and ``IMPORT_TO_DIST`` are
hand-written. Deriving either from the source (or from the installed environment) would mean
breaking the source could empty both sides and stay green -- the circularity that made a REST route
test pass with ``_slug()`` replaced by the identity function. Both maps are also asserted EXHAUSTIVE
below, so an entry that stops matching reality reds instead of quietly covering nothing.
"""
from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO = Path(__file__).resolve().parent.parent.parent
PYPROJECTS = sorted(REPO.glob("*/pyproject.toml"))

# Which extra gates each shipped door module. Hand-written: these three are the only modules in the
# repo that may touch a third-party import at all, and each one is reachable ONLY through its own
# extra (`serve --http`/`--stdio`, `serve --rest`, `serve --a2a`). A module added to this map is a
# new door; a door added without a map entry reds on the stdlib-only rule below, which is the
# fail-closed direction.
DOOR_EXTRAS = {
    "broker/pacioli/server.py": "server",
    "broker/pacioli/rest.py": "rest",
    "broker/pacioli/a2a.py": "a2a",
}

# Import name -> distribution name on PyPI. Hand-written because the two differ often enough
# (`jwt` ships in `PyJWT`, `a2a` ships in `a2a-sdk`) that guessing is how a guard goes vacuous.
# An import root that is NOT in this map is a hard failure, never a skip: a checker that fails
# open into a plausible answer is worse than one that crashes.
IMPORT_TO_DIST = {
    "a2a": "a2a-sdk",
    "anyio": "anyio",
    "cryptography": "cryptography",
    "jwt": "pyjwt",
    "mcp": "mcp",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
}

# Modules the HOST supplies, which must never be declared as dependencies.
#
# `pacioli_guard` is a Frappe app: a bench installs it INTO an existing frappe runtime, and that
# runtime imports the app, not the other way round. `frappe` is not resolvable from PyPI in the
# shape a bench provides, so declaring it would be a false statement about how guard installs --
# and guard's `dependencies = []` is the accurate one. This is an exemption from DECLARATION, not
# from scrutiny: the entry is listed here so the exemption is visible and has to be argued, rather
# than being an invisible hole in the walk.
HOST_PROVIDED = {"frappe"}


def _source_root(pyproject: Path) -> Path:
    """The shipped package directory for a package root, from its declared distribution NAME.

    `pacioli` -> `broker/pacioli`, `pacioli-guard` -> `guard/pacioli_guard`. Derived from the name
    rather than hardcoded so a third package is walked on arrival instead of being silently exempt,
    which is the same reason the sibling globs `*/pyproject.toml` instead of listing two paths.
    """
    name = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["name"]
    root = pyproject.parent / name.replace("-", "_")
    assert root.is_dir(), (
        f"{pyproject}: declares name {name!r} but {root} is not a directory, so this walk would "
        f"silently cover nothing. Teach `_source_root` the real layout rather than skipping it.")
    return root


def _third_party_imports(path: Path) -> set[str]:
    """Root module names imported by a file that are neither stdlib nor first-party.

    Walks the WHOLE tree, not just module level: `server.py` imports anyio inside a function and
    `a2a.py` imports every one of its third-party modules lazily inside functions. A module-level
    scan would have reported this package as importing nothing at all.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            # `level > 0` is a relative import, which is first-party by construction.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return {r for r in roots
            if r not in sys.stdlib_module_names and not r.startswith("pacioli")}


def _shipped_modules() -> list[tuple[str, Path]]:
    """(repo-relative path, absolute path) for every shipped .py file, tests excluded."""
    out: list[tuple[str, Path]] = []
    for pyproject in PYPROJECTS:
        for path in sorted(_source_root(pyproject).rglob("*.py")):
            if "tests" in path.parts:
                continue
            out.append((path.relative_to(REPO).as_posix(), path))
    return out


def _declared(pyproject: Path, extra: str) -> set[str]:
    """Canonical distribution names declared by one extra of one package."""
    proj = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    reqs = (proj.get("optional-dependencies") or {}).get(extra) or []
    return {canonicalize_name(Requirement(r).name) for r in reqs}


def test_every_door_module_import_is_declared_by_its_own_extra():
    """A door's extra must supply everything that door imports, with no help from a neighbour.

    `pacioli[a2a]` alone has to build a working A2A door. Through 0.37.1 it did not: starlette
    reached `a2a.py` only because `[server]`'s mcp happened to depend on it, so installing the
    extra by itself produced an ImportError instead of a door. CI installs `.[server,a2a]`
    together and could never see it. This asks the question CI cannot.
    """
    offenders = []
    for rel, path in _shipped_modules():
        extra = DOOR_EXTRAS.get(rel)
        if extra is None:
            continue
        pyproject = REPO / rel.split("/")[0] / "pyproject.toml"
        declared = _declared(pyproject, extra)
        for root in sorted(_third_party_imports(path)):
            if root in HOST_PROVIDED:
                continue
            dist = IMPORT_TO_DIST.get(root)
            assert dist is not None, (
                f"{rel} imports {root!r}, which is not in IMPORT_TO_DIST. Add the import->dist "
                f"mapping; this guard refuses to guess, because guessing is how it goes vacuous.")
            if canonicalize_name(dist) not in declared:
                offenders.append(
                    f"{rel}: imports {root!r} ({dist}) but [{extra}] does not declare it")
    assert not offenders, (
        "these modules import a distribution their own extra does not declare, so it arrives only "
        "as somebody else's transitive: undeclared, unbounded by us, and invisible to the bounds "
        "gate because there is no requirement string for it to read:\n  " + "\n  ".join(offenders))


def test_the_non_door_modules_import_nothing_third_party():
    """The pure cores, the store, the spine and the human CLI are stdlib-only, as claimed.

    `broker/pyproject.toml` states this in prose ("The pure cores + human CLI are stdlib-only") and
    the whole extras design rests on it: if a core module grew a third-party import, it would be
    unreachable from the base install and `pip install pacioli` would ship a broken CLI. Prose is
    not a gate. This is.
    """
    offenders = []
    for rel, path in _shipped_modules():
        if rel in DOOR_EXTRAS:
            continue
        for root in sorted(_third_party_imports(path)):
            if root in HOST_PROVIDED:
                continue
            offenders.append(f"{rel}: imports {root!r}")
    assert not offenders, (
        "these modules are not doors, so nothing installs a dependency for them, yet they import "
        "one. Either the import belongs behind a door, or this module is a new door and needs a "
        "DOOR_EXTRAS entry plus an extra that declares what it needs:\n  " + "\n  ".join(offenders))


def test_the_walk_actually_covers_something():
    """A walk that finds no files passes every other test in this file, silently.

    Found by a mutation lens 2026-08-24: pointing `_source_root` at a real but `.py`-less
    directory made `test_every_door_module_import_is_declared_by_its_own_extra` report zero
    offenders and go GREEN. The suite still reddened, but only through an unrelated collateral
    assertion, not through the check that was supposed to notice. A gate whose subject went empty
    is the same failure as a gate that was never run: it can only refuse what it was given.
    """
    shipped = _shipped_modules()
    per_root: dict[str, int] = {}
    for rel, _ in shipped:
        per_root[rel.split("/")[0]] = per_root.get(rel.split("/")[0], 0) + 1

    declared_roots = {p.parent.name for p in PYPROJECTS}
    assert set(per_root) == declared_roots, (
        f"the walk covered {set(per_root)} but the repo declares {declared_roots}; a package "
        f"whose source root stopped resolving is silently exempt from every check here")
    # PER ROOT, not on the total. The first version of this assertion checked only the total and a
    # mutation lens walked straight past it: pointing ONE package's source root at a `.py`-less
    # directory still left the OTHER package contributing enough files to clear a global
    # threshold. A per-package floor is what actually notices a package going dark.
    for root, count in sorted(per_root.items()):
        assert count >= 5, (
            f"{root} contributed only {count} shipped module(s); its source root has lost its "
            f"subject, and every import check against it is now vacuously true")
    for rel in DOOR_EXTRAS:
        assert _third_party_imports(REPO / rel), (
            f"{rel} is mapped as a door but imports nothing third-party. Either the walk is "
            f"broken or this is no longer a door; both make its entry cover nothing.")


def test_each_extra_is_read_separately_not_unioned():
    """`_declared` must answer for ONE extra, never for the whole file.

    A mutation lens rewrote it to union every extra's requirements together and the suite stayed
    green -- because with the tree in its correct state, every distribution IS declared somewhere,
    so a union still satisfies each door. That reintroduces precisely the defect this file was
    written to prevent: `pacioli[a2a]` alone shipping an ImportError because starlette arrived
    only through `[server]`'s mcp. The check cannot be "declared somewhere"; it must be "declared
    by the extra that gates this module", and only a NEGATIVE assertion pins that.
    """
    broker = REPO / "broker" / "pyproject.toml"
    server, a2a, rest = (_declared(broker, e) for e in ("server", "a2a", "rest"))

    assert canonicalize_name("mcp") in server
    assert canonicalize_name("a2a-sdk") in a2a

    # The negatives are the whole point: these ARE declared in the file, just not in this extra.
    assert canonicalize_name("a2a-sdk") not in server, (
        "`_declared` is leaking other extras into [server]; a union makes every door's check "
        "unfalsifiable")
    assert canonicalize_name("mcp") not in a2a, "`_declared` is leaking [server] into [a2a]"
    assert canonicalize_name("mcp") not in rest, (
        "[rest] must not carry mcp: the REST door's whole point is that it needs no MCP SDK, and "
        "the install smoke installs it alone to prove it")


def test_both_ground_truth_maps_are_exhaustive():
    """Neither map may carry an entry that has stopped matching reality.

    A stale map is how a guard quietly stops covering things: `DOOR_EXTRAS` pointing at a renamed
    file would exempt the real one from the stdlib-only rule, and an `IMPORT_TO_DIST` entry for a
    dependency nobody imports any more is a declaration this file would keep demanding forever.
    """
    shipped = {rel for rel, _ in _shipped_modules()}
    missing = sorted(set(DOOR_EXTRAS) - shipped)
    assert not missing, (
        f"DOOR_EXTRAS names files that are not shipped modules: {missing}. If a door was renamed, "
        f"rename it here too, or the real file silently falls under the stdlib-only rule.")

    imported: set[str] = set()
    for _, path in _shipped_modules():
        imported |= _third_party_imports(path)
    unused = sorted(set(IMPORT_TO_DIST) - imported)
    assert not unused, (
        f"IMPORT_TO_DIST maps imports nothing imports any more: {unused}. Drop the entry, and "
        f"check whether the extra still needs to declare that distribution at all.")

    stale_host = sorted(HOST_PROVIDED - imported)
    assert not stale_host, (
        f"HOST_PROVIDED exempts modules nothing imports: {stale_host}. An exemption that covers "
        f"nothing is a hole waiting for a future import to fall into.")
