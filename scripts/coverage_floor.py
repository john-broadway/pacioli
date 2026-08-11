#!/usr/bin/env python3
"""Per-module coverage floors — on BOTH statements and branches, separately — plus the checks
that keep this gate from passing blind.

**Why this exists rather than `fail_under`.** `fail_under` is one repo-wide number. Pacioli is
~35k lines of source in which a few hundred lines actually decide whether a write happens: the
credential gate, the consent gate, the receipt chain, the spine. A blanket average lets any one
of those rot behind a large, well-covered neighbour. The floor here is declared per module in
`scripts/coverage-floors.json`, and every declared module must clear both of its own lines.

**Why TWO metrics and not one.** The first version of this gate scored `summary.percent_covered`
and called it a branch floor. It is not: under `branch = true`, coverage.py's `percent_covered`
is the BLENDED ``(covered_lines + covered_branches) / (num_statements + num_branches)``. An
independent review caught it on the very module this campaign is aimed at —
``pacioli_consent_marker.py`` measured ``percent_covered`` **53.8** while
``percent_branches_covered`` was **0.0** (0 of 2 branches ever taken), and it cleared a floor
sold as a branch floor. Statement mass masks branch loss, so the two are gated apart:

* ``statements`` — ``percent_statements_covered``: was this LINE ever executed.
* ``branches``   — ``percent_branches_covered``: was this DECISION ever taken both ways.

Neither can hide behind the other. A module with no branches reports 100 on the branch metric,
which ratchets to a floor of 100 — so the first uncovered branch it ever grows reds the gate.

**The four ways a coverage gate passes blind, all refused here.** This project has already
shipped a security gate that audited an empty environment and reported green
(`feedback_a-blocking-gate-only-blocks-when-it-runs`), so each refusal is explicit AND is driven
red in `scripts/tests/test_coverage_floor.py` — including THROUGH THE PROCESS, because the exit
code is the only thing CI ever observes:

1. **Wrong subject** — a statement-mode report cannot answer a branch floor at all. Refused via
   ``meta.branch_coverage``.
2. **Absent subject** — a declared module missing from the report (renamed, excluded, never
   imported). Refused. Keys are matched EXACTLY: an earlier version fell back to a unique path
   suffix, which would silently score a vendored or duplicated copy of the module instead.
3. **Empty subject** — a report with no files, or no report at all.
4. **Silent exit** — the refusal is computed but the process still exits 0. Pinned by
   subprocess tests, since a library-level test cannot see a `return 0` in ``main()``.

Consumed by:
  - ``scripts/tests/test_coverage_floor.py``   (units, bite-checks, and process exit codes)
  - ``.github/workflows/ci.yml``               (public mirror — the compat matrix)
  - ``.gitea/workflows/ci.yml``                (internal forge — where development lands)

Usage::

    python scripts/coverage_floor.py check   <broker|guard> <coverage.json>
    python scripts/coverage_floor.py ratchet <broker|guard> <coverage.json>

``ratchet`` raises floors to the measured numbers and **never lowers one** — a red gate is not
fixed by moving the line, and a ratchet that can slip backwards is not a ratchet.

Stdlib only, matching ``scripts/version_tools.py``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FLOORS_FILE = REPO_ROOT / "scripts" / "coverage-floors.json"

PACKAGES = ("broker", "guard")

# metric name in the floors file -> the coverage.py summary field it reads
METRICS = {
    "statements": "percent_statements_covered",
    "branches": "percent_branches_covered",
}

USAGE = "usage: coverage_floor.py <check|ratchet> <broker|guard> <coverage.json>"


class FloorError(Exception):
    """A refusal. The message is the operator-facing explanation."""


def load_floors(path=FLOORS_FILE):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError as exc:
        raise FloorError(f"no floors file at {path} — this gate has nothing to check") from exc
    except json.JSONDecodeError as exc:
        raise FloorError(f"floors file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise FloorError(f"floors file {path} declares no packages")
    return data


def load_report(path):
    try:
        data = json.loads(Path(path).read_text())
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise FloorError(
            f"no coverage report at {path} — the suite did not run, or did not write one. "
            f"A missing report is a FAILURE here, never a skip."
        ) from exc
    except json.JSONDecodeError as exc:
        raise FloorError(f"coverage report {path} is not valid JSON: {exc}") from exc

    meta = data.get("meta") or {}
    if not meta.get("branch_coverage"):
        raise FloorError(
            f"coverage report {path} is STATEMENT-mode. These floors gate branch coverage as "
            f"well, and a statement-mode report carries no branch data at all — it cannot "
            f"answer the question. Re-run with `--cov-branch` (or `branch = true` under "
            f"[tool.coverage.run], which is where this repo sets it)."
        )

    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise FloorError(
            f"coverage report {path} contains NO files. An empty report cannot clear a floor; "
            f"it means nothing was measured."
        )
    return files


def _module_floors(package, floors_path):
    floors = load_floors(floors_path).get(package)
    if not floors:
        raise FloorError(f"floors file declares no modules for {package!r}")
    return floors


def _measured(files, module):
    """``{metric: percent}`` for one module, or ``None`` if it is absent from the report.

    EXACT key match only. The report's keys are produced by the same command CI runs, so a
    module that is not there under its declared name has genuinely not been measured — and
    guessing at a path suffix would score a different file that merely shares a name.
    """
    entry = files.get(module)
    if entry is None:
        return None
    summary = entry.get("summary", {})
    return {name: summary.get(field) for name, field in METRICS.items()}


def check(package, report_path, floors_path=FLOORS_FILE):
    """Return a list of human-readable failures. Empty list means the gate passed."""
    if package not in PACKAGES:
        raise FloorError(f"unknown package {package!r} — expected one of {', '.join(PACKAGES)}")

    floors = _module_floors(package, floors_path)
    files = load_report(report_path)

    failures = []
    for module, declared in sorted(floors.items()):
        actual = _measured(files, module)
        if actual is None:
            failures.append(
                f"{module}: ABSENT from the coverage report. A module that is not measured has "
                f"not passed — it was renamed, excluded, or never imported."
            )
            continue
        for metric in sorted(METRICS):
            floor = declared.get(metric)
            got = actual.get(metric)
            if floor is None:
                failures.append(f"{module}: no {metric} floor declared — every module gates both")
                continue
            if got is None:
                failures.append(
                    f"{module}: the report carries no {metric} figure, so the {metric} floor "
                    f"of {floor}% cannot be evaluated."
                )
                continue
            if got + 1e-9 < floor:
                failures.append(
                    f"{module}: {metric} {got:.1f}% is below its floor of {floor}% "
                    f"(short by {floor - got:.1f} points)."
                )
    return failures


def ratchet(package, report_path, floors_path=FLOORS_FILE):
    """Raise floors to the measured numbers. Never lowers. Returns (raised, held) descriptions."""
    if package not in PACKAGES:
        raise FloorError(f"unknown package {package!r} — expected one of {', '.join(PACKAGES)}")

    all_floors = load_floors(floors_path)
    floors = _module_floors(package, floors_path)
    files = load_report(report_path)

    raised, held = [], []
    for module, declared in sorted(floors.items()):
        actual = _measured(files, module)
        if actual is None:
            raise FloorError(
                f"{module}: ABSENT from the coverage report — refusing to ratchet against a "
                f"report that does not contain every declared module."
            )
        for metric in sorted(METRICS):
            got = actual.get(metric)
            if got is None:
                raise FloorError(
                    f"{module}: the report carries no {metric} figure — refusing to ratchet "
                    f"against a report that cannot answer every declared metric."
                )
            floor = declared.get(metric, 0)
            new = int(got)  # floor to the integer: never ratchet onto a number that can flake
            if new > floor:
                declared[metric] = new
                raised.append(f"{module} {metric}: {floor}% -> {new}% (measured {got:.1f}%)")
            else:
                held.append(f"{module} {metric}: held at {floor}% (measured {got:.1f}%)")

    all_floors[package] = floors
    Path(floors_path).write_text(json.dumps(all_floors, indent=2, sort_keys=True) + "\n")
    return raised, held


def main(argv):
    if len(argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2

    verb, package, report_path = argv
    try:
        if verb == "check":
            failures = check(package, report_path)
            if failures:
                print(f"COVERAGE FLOOR: {len(failures)} shortfall(s) in {package}:",
                      file=sys.stderr)
                for line in failures:
                    print(f"  - {line}", file=sys.stderr)
                print("\nThese modules decide whether a write happens. Raise the tests, not the "
                      "floor.", file=sys.stderr)
                return 1
            print(f"coverage floor: {package} clears every declared module, "
                  f"on statements AND branches.")
            return 0

        if verb == "ratchet":
            raised, held = ratchet(package, report_path)
            for line in raised:
                print(f"raised  {line}")
            for line in held:
                print(f"held    {line}")
            print(f"\n{len(raised)} raised, {len(held)} held. Floors never lower.")
            return 0

        print(f"unknown verb {verb!r} — expected check or ratchet\n{USAGE}", file=sys.stderr)
        return 2
    except FloorError as exc:
        print(f"COVERAGE FLOOR REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
