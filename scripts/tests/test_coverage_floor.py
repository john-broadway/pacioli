"""Unit tests, BITE-CHECKS and PROCESS-LEVEL exit codes for scripts/coverage_floor.py.

A coverage gate is itself a gate, so it earns nothing by exiting 0. Every refusal it claims to
make is driven here against a doctored report and asserted RED, because *a gate you never saw
refuse is not yet a gate*, and because this repo has already shipped a security gate that
audited an empty environment and reported green.

**Why the subprocess tests exist.** An independent review of the first version pointed out that
every test here called ``check()``/``ratchet()`` as library functions, so the module could be
mutated to ``return 0`` from ``main()`` on failure and the whole suite stayed green — while CI,
which observes nothing but the exit code, would pass a failing gate. That is the same shape as
the 2026-08-10 pip-audit defect this gate was written in answer to: the mechanism right, the
wiring unproven. The ``TestProcessExitCodes`` class closes it.

The five ways this gate could pass blind, one test each:

  1. a module genuinely below either floor        -> ``test_bite_*``
  2. a STATEMENT-mode report scored against       -> ``test_refuses_statement_mode_report``
     floors that gate branches (wrong subject)
  3. a declared module ABSENT from the report     -> ``test_absent_module_fails_never_skips``
  4. a report with no files, or no report at all  -> ``test_refuses_empty_report`` /
                                                     ``test_refuses_missing_report``
  5. a refusal computed but exited 0              -> ``TestProcessExitCodes``
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import coverage_floor  # noqa: E402

SCRIPT = REPO_ROOT / "scripts" / "coverage_floor.py"


# --------------------------------------------------------------------------
# Sandbox builders — a minimal report/floors pair the checker passes cleanly.
# --------------------------------------------------------------------------

def _report(files, branch=True):
    """``files`` maps module -> (statements_pct, branches_pct)."""
    return {
        "meta": {"format": 3, "version": "7.15.4", "branch_coverage": branch},
        "files": {
            name: {"summary": {
                "percent_covered": (st + br) / 2,   # the BLENDED figure the gate must not use
                "percent_statements_covered": st,
                "percent_branches_covered": br,
                "num_statements": 10,
                "num_branches": 4,
            }}
            for name, (st, br) in files.items()
        },
    }


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def _floors(tmp_path, mapping):
    return _write(tmp_path, "floors.json", mapping)


def _pair(statements, branches):
    return {"statements": statements, "branches": branches}


# --------------------------------------------------------------------------
# The always-on gate against the real repo.
# --------------------------------------------------------------------------

def test_committed_floors_are_wellformed():
    floors = coverage_floor.load_floors()
    assert set(floors) == set(coverage_floor.PACKAGES)
    for package, modules in floors.items():
        assert modules, f"{package} declares no modules"
        for module, declared in modules.items():
            assert module.endswith(".py"), f"{package}:{module} is not a module path"
            assert set(declared) == set(coverage_floor.METRICS), (
                f"{package}:{module} must declare BOTH metrics — a statements-only floor is how "
                f"branch loss hid behind statement mass in the first version of this gate"
            )
            for metric, floor in declared.items():
                assert isinstance(floor, int), f"{package}:{module} {metric} floor is not an int"
                assert 0 <= floor <= 100, f"{package}:{module} {metric} floor {floor} out of range"


def test_the_gate_reads_the_split_metrics_never_the_blended_one():
    """`percent_covered` under branch mode is (lines + branches) blended. Reading it is what let
    guard's marker controller clear a 'branch floor' at 0.0% branch coverage."""
    assert "percent_covered" not in coverage_floor.METRICS.values()
    assert set(coverage_floor.METRICS.values()) == {
        "percent_statements_covered", "percent_branches_covered",
    }


def test_every_decision_module_carries_a_floor():
    floors = coverage_floor.load_floors()
    for required in ("pacioli_guard/enforce.py", "pacioli_guard/act.py", "pacioli_guard/scope.py",
                     "pacioli_guard/mint.py"):
        assert required in floors["guard"], f"{required} must carry a coverage floor"
    for required in ("pacioli/spine.py", "pacioli/prove.py", "pacioli/plan.py",
                     "pacioli/consent.py", "pacioli/store.py", "pacioli/a2a.py",
                     "pacioli/amend.py", "pacioli/server.py"):
        assert required in floors["broker"], f"{required} must carry a coverage floor"


# --------------------------------------------------------------------------
# 1. The bite-check: below EITHER floor must go RED.
# --------------------------------------------------------------------------

def test_bite_statements_below_floor_fails(tmp_path):
    report = _write(tmp_path, "cov.json", _report({"pacioli/spine.py": (80.0, 99.0)}))
    floors = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(90, 78)}})

    failures = coverage_floor.check("broker", report, floors)

    assert len(failures) == 1
    assert "statements 80.0%" in failures[0] and "short by 10.0" in failures[0]


def test_bite_branches_below_floor_fails_even_when_statements_are_high(tmp_path):
    """The finding that forced the two-metric split: statement mass masking branch loss."""
    report = _write(tmp_path, "cov.json", _report({"pacioli/spine.py": (99.0, 10.0)}))
    floors = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(90, 78)}})

    failures = coverage_floor.check("broker", report, floors)

    assert len(failures) == 1
    assert "branches 10.0%" in failures[0]


def test_module_exactly_at_both_floors_passes(tmp_path):
    report = _write(tmp_path, "cov.json", _report({"pacioli/spine.py": (90.0, 78.0)}))
    floors = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(90, 78)}})
    assert coverage_floor.check("broker", report, floors) == []


def test_a_missing_metric_floor_is_itself_a_failure(tmp_path):
    report = _write(tmp_path, "cov.json", _report({"pacioli/spine.py": (99.0, 99.0)}))
    floors = _floors(tmp_path, {"broker": {"pacioli/spine.py": {"statements": 90}}})

    failures = coverage_floor.check("broker", report, floors)

    assert len(failures) == 1 and "no branches floor declared" in failures[0]


def test_a_report_without_branch_figures_cannot_satisfy_a_branch_floor(tmp_path):
    payload = _report({"pacioli/spine.py": (99.0, 99.0)})
    del payload["files"]["pacioli/spine.py"]["summary"]["percent_branches_covered"]
    report = _write(tmp_path, "cov.json", payload)
    floors = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(90, 78)}})

    failures = coverage_floor.check("broker", report, floors)

    assert len(failures) == 1 and "carries no branches figure" in failures[0]


def test_every_failing_module_is_named_not_just_the_first(tmp_path):
    report = _write(tmp_path, "cov.json", _report({
        "pacioli/spine.py": (10.0, 10.0), "pacioli/prove.py": (20.0, 20.0),
        "pacioli/plan.py": (99.0, 99.0),
    }))
    floors = _floors(tmp_path, {"broker": {
        "pacioli/spine.py": _pair(90, 78), "pacioli/prove.py": _pair(97, 97),
        "pacioli/plan.py": _pair(96, 96),
    }})

    failures = coverage_floor.check("broker", report, floors)

    assert len(failures) == 4  # two modules x two metrics
    assert any("spine" in f for f in failures) and any("prove" in f for f in failures)
    assert not any("plan" in f for f in failures)


# --------------------------------------------------------------------------
# 2. Wrong subject.
# --------------------------------------------------------------------------

def test_refuses_statement_mode_report(tmp_path):
    report = _write(tmp_path, "cov.json",
                    _report({"pacioli/spine.py": (99.0, 99.0)}, branch=False))
    floors = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(90, 78)}})

    with pytest.raises(coverage_floor.FloorError) as exc:
        coverage_floor.check("broker", report, floors)
    assert "STATEMENT-mode" in str(exc.value)


# --------------------------------------------------------------------------
# 3. Absent subject — EXACT key match, no suffix guessing.
# --------------------------------------------------------------------------

def test_absent_module_fails_never_skips(tmp_path):
    report = _write(tmp_path, "cov.json", _report({"pacioli/plan.py": (99.0, 99.0)}))
    floors = _floors(tmp_path, {"broker": {
        "pacioli/plan.py": _pair(96, 96), "pacioli/spine.py": _pair(90, 78),
    }})

    failures = coverage_floor.check("broker", report, floors)

    assert len(failures) == 1
    assert "pacioli/spine.py" in failures[0] and "ABSENT" in failures[0]


def test_a_same_named_module_at_another_path_does_NOT_satisfy_the_floor(tmp_path):
    """The first version resolved a unique path SUFFIX, so a vendored or duplicated copy of a
    module would be scored in place of the real one. Keys are matched exactly now."""
    report = _write(tmp_path, "cov.json", _report({"vendor/pacioli/spine.py": (99.0, 99.0)}))
    floors = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(90, 78)}})

    failures = coverage_floor.check("broker", report, floors)

    assert len(failures) == 1 and "ABSENT" in failures[0]


# --------------------------------------------------------------------------
# 4. Empty or missing subject.
# --------------------------------------------------------------------------

def test_refuses_empty_report(tmp_path):
    report = _write(tmp_path, "cov.json", _report({}))
    floors = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(90, 78)}})

    with pytest.raises(coverage_floor.FloorError) as exc:
        coverage_floor.check("broker", report, floors)
    assert "NO files" in str(exc.value)


def test_refuses_missing_report(tmp_path):
    floors = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(90, 78)}})

    with pytest.raises(coverage_floor.FloorError) as exc:
        coverage_floor.check("broker", tmp_path / "nope.json", floors)
    assert "never a skip" in str(exc.value)


def test_refuses_a_directory_where_a_report_should_be(tmp_path):
    floors = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(90, 78)}})
    (tmp_path / "adir").mkdir()

    with pytest.raises(coverage_floor.FloorError):
        coverage_floor.check("broker", tmp_path / "adir", floors)


def test_refuses_unknown_package(tmp_path):
    report = _write(tmp_path, "cov.json", _report({"pacioli/spine.py": (99.0, 99.0)}))
    floors = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(90, 78)}})

    with pytest.raises(coverage_floor.FloorError):
        coverage_floor.check("nonesuch", report, floors)


# --------------------------------------------------------------------------
# 5. THE PROCESS. CI sees the exit code and nothing else.
# --------------------------------------------------------------------------

class TestProcessExitCodes:
    """Every one of these runs the real script as a subprocess. A `return 0` slipped into
    `main()` keeps every library-level test above green and would be caught only here."""

    @staticmethod
    def _run(*args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                              capture_output=True, text=True)

    def test_a_partial_report_against_the_REAL_floors_exits_nonzero(self, tmp_path):
        """A toy report naming one module cannot satisfy the committed floors, which declare
        every module in the package. Absence must reach CI as a failure, not a quiet pass."""
        report = _write(tmp_path, "cov.json", _report({"pacioli/spine.py": (99.0, 99.0)}))
        proc = self._run("check", "broker", report)
        assert proc.returncode == 1
        assert "ABSENT" in proc.stderr

    def test_a_shortfall_exits_nonzero(self, tmp_path):
        """The load-bearing one: a computed refusal must reach CI as a non-zero exit."""
        report = _write(tmp_path, "cov.json", _report({"pacioli/spine.py": (1.0, 1.0)}))
        proc = self._run("check", "broker", report)
        assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert "COVERAGE FLOOR" in proc.stderr

    def test_a_missing_report_exits_nonzero(self, tmp_path):
        proc = self._run("check", "broker", tmp_path / "nope.json")
        assert proc.returncode == 1
        assert "REFUSED" in proc.stderr

    def test_an_empty_report_exits_nonzero(self, tmp_path):
        report = _write(tmp_path, "cov.json", _report({}))
        proc = self._run("check", "broker", report)
        assert proc.returncode == 1
        assert "NO files" in proc.stderr

    def test_a_statement_mode_report_exits_nonzero(self, tmp_path):
        report = _write(tmp_path, "cov.json",
                        _report({"pacioli/spine.py": (99.0, 99.0)}, branch=False))
        proc = self._run("check", "broker", report)
        assert proc.returncode == 1
        assert "STATEMENT-mode" in proc.stderr

    def test_an_unparseable_report_exits_nonzero(self, tmp_path):
        bad = tmp_path / "cov.json"
        bad.write_text("{not json")
        proc = self._run("check", "broker", bad)
        assert proc.returncode == 1

    def test_bad_usage_exits_nonzero(self):
        assert self._run("check").returncode == 2
        assert self._run("bogus", "broker", "x.json").returncode == 2

    def test_the_usage_line_survives_a_docstring_edit(self):
        """The first version printed `__doc__.splitlines()[-6]` — a magic index that silently
        changed or raised whenever the docstring's tail moved."""
        proc = self._run("check")
        assert "usage: coverage_floor.py" in proc.stderr
        assert "Traceback" not in proc.stderr


# --------------------------------------------------------------------------
# The ratchet's safety property.
# --------------------------------------------------------------------------

def test_ratchet_raises_but_never_lowers(tmp_path):
    report = _write(tmp_path, "cov.json", _report({
        "pacioli/spine.py": (95.6, 99.0),   # both above -> raise
        "pacioli/prove.py": (40.0, 40.0),   # both below -> HOLD
    }))
    floors_path = _floors(tmp_path, {"broker": {
        "pacioli/spine.py": _pair(90, 78), "pacioli/prove.py": _pair(97, 97),
    }})

    raised, held = coverage_floor.ratchet("broker", report, floors_path)

    after = json.loads(floors_path.read_text())["broker"]
    assert after["pacioli/spine.py"] == {"statements": 95, "branches": 99}
    assert after["pacioli/prove.py"] == {"statements": 97, "branches": 97}
    assert len(raised) == 2 and len(held) == 2


def test_ratchet_can_raise_one_metric_while_holding_the_other(tmp_path):
    report = _write(tmp_path, "cov.json", _report({"pacioli/spine.py": (99.0, 10.0)}))
    floors_path = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(90, 78)}})

    coverage_floor.ratchet("broker", report, floors_path)

    after = json.loads(floors_path.read_text())["broker"]["pacioli/spine.py"]
    assert after == {"statements": 99, "branches": 78}


def test_ratchet_leaves_the_other_packages_block_untouched(tmp_path):
    report = _write(tmp_path, "cov.json", _report({"pacioli/spine.py": (99.0, 99.0)}))
    floors_path = _floors(tmp_path, {
        "broker": {"pacioli/spine.py": _pair(90, 78)},
        "guard": {"pacioli_guard/act.py": _pair(91, 90)},
    })

    coverage_floor.ratchet("broker", report, floors_path)

    assert json.loads(floors_path.read_text())["guard"] == {
        "pacioli_guard/act.py": {"statements": 91, "branches": 90}}


def test_ratchet_floors_to_the_integer_so_it_cannot_flake(tmp_path):
    report = _write(tmp_path, "cov.json", _report({"pacioli/spine.py": (90.99, 90.99)}))
    floors_path = _floors(tmp_path, {"broker": {"pacioli/spine.py": _pair(0, 0)}})

    coverage_floor.ratchet("broker", report, floors_path)

    assert json.loads(floors_path.read_text())["broker"]["pacioli/spine.py"] == {
        "statements": 90, "branches": 90}


def test_ratchet_refuses_an_absent_module(tmp_path):
    report = _write(tmp_path, "cov.json", _report({"pacioli/plan.py": (99.0, 99.0)}))
    floors_path = _floors(tmp_path, {"broker": {
        "pacioli/plan.py": _pair(96, 96), "pacioli/spine.py": _pair(90, 78),
    }})

    with pytest.raises(coverage_floor.FloorError) as exc:
        coverage_floor.ratchet("broker", report, floors_path)
    assert "ABSENT" in str(exc.value)
