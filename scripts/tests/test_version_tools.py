"""Unit + drift tests for scripts/version_tools.py.

Pacioli is a TWO-package monorepo (broker=`pacioli`, guard=`pacioli-guard`) with
INDEPENDENT versions, so the tool is per-package. These tests cover both the
always-on consistency gate (against the real repo) and the set/check mechanics
(against tmp sandboxes).
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import version_tools  # noqa: E402


# --------------------------------------------------------------------------
# The always-on gate against the real repo.
# --------------------------------------------------------------------------

def test_live_repo_is_version_consistent():
    problems = version_tools.check_consistency(REPO_ROOT)
    assert problems == [], "version drift:\n" + "\n".join(problems)


# --------------------------------------------------------------------------
# Sandbox builders — a minimal two-package repo the checker passes cleanly.
# --------------------------------------------------------------------------

def _pyproject(name: str, v: str) -> str:
    return (
        '[build-system]\nrequires = ["hatchling"]\n\n'
        f'[project]\nname = "{name}"\nversion = "{v}"\n'
    )


def _server_json(v: str) -> str:
    return (
        "{\n"
        '  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",\n'
        '  "name": "io.github.x/pacioli",\n'
        f'  "version": "{v}",\n'
        '  "packages": [\n'
        "    {\n"
        '      "registryType": "pypi",\n'
        '      "identifier": "pacioli",\n'
        f'      "version": "{v}"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )


def _lhm(v: str, tool_has_version: bool = False) -> str:
    """The LobeHub manifest: a top-level version, then a tools array. Optionally
    give a tool an inner "version" field to prove set_version leaves it untouched."""
    tool = '{ "name": "t1", "description": "d" }'
    if tool_has_version:
        tool = '{ "name": "t1", "description": "d", "version": "9.9.9" }'
    return (
        "{\n"
        '  "identifier": "x",\n'
        f'  "version": "{v}",\n'
        f'  "tools": [\n    {tool}\n  ]\n'
        "}\n"
    )


def _sandbox(tmp_path: Path, broker_v: str = "0.30.1", guard_v: str = "0.6.2",
            lhm_tool_has_version: bool = False) -> Path:
    b = tmp_path / "broker"
    (b / "pacioli").mkdir(parents=True)
    (b / "pyproject.toml").write_text(_pyproject("pacioli", broker_v), encoding="utf-8")
    (b / "pacioli" / "__init__.py").write_text(f'__version__ = "{broker_v}"\n', encoding="utf-8")
    (b / "CHANGELOG.md").write_text(
        f"# Changelog — Pacioli (broker)\n\n## {broker_v} — 2026-07-17 — a change\n", encoding="utf-8"
    )
    (tmp_path / "server.json").write_text(_server_json(broker_v), encoding="utf-8")
    (tmp_path / "lhm.plugin.json").write_text(
        _lhm(broker_v, tool_has_version=lhm_tool_has_version), encoding="utf-8"
    )

    g = tmp_path / "guard"
    (g / "pacioli_guard").mkdir(parents=True)
    (g / "pyproject.toml").write_text(_pyproject("pacioli-guard", guard_v), encoding="utf-8")
    (g / "pacioli_guard" / "__init__.py").write_text(f'__version__ = "{guard_v}"\n', encoding="utf-8")
    (g / "CHANGELOG.md").write_text(
        f"# Changelog — Pacioli Guard\n\n## {guard_v} — 2026-07-10 — a change\n", encoding="utf-8"
    )
    return tmp_path


# --------------------------------------------------------------------------
# check_consistency — the clean case + each drift it must catch.
# --------------------------------------------------------------------------

def test_sandbox_is_consistent(tmp_path):
    assert version_tools.check_consistency(_sandbox(tmp_path)) == []


def test_flags_broker_pyproject_init_mismatch(tmp_path):
    root = _sandbox(tmp_path)
    (root / "broker" / "pacioli" / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    problems = version_tools.check_consistency(root)
    assert any("broker" in p and "!=" in p for p in problems)


def test_flags_guard_pyproject_init_mismatch(tmp_path):
    root = _sandbox(tmp_path)
    (root / "guard" / "pacioli_guard" / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    problems = version_tools.check_consistency(root)
    assert any("guard" in p and "!=" in p for p in problems)


def test_flags_server_json_top_mismatch(tmp_path):
    root = _sandbox(tmp_path)
    text = (root / "server.json").read_text(encoding="utf-8")
    (root / "server.json").write_text(
        text.replace('"version": "0.30.1",', '"version": "9.9.9",', 1), encoding="utf-8"
    )
    problems = version_tools.check_consistency(root)
    assert any("server.json" in p and "top-level" in p for p in problems)


def test_flags_server_json_package_mismatch(tmp_path):
    root = _sandbox(tmp_path)
    text = (root / "server.json").read_text(encoding="utf-8")
    # The packages[0].version has no trailing comma — drift only that one.
    (root / "server.json").write_text(
        text.replace('"version": "0.30.1"\n', '"version": "9.9.9"\n', 1), encoding="utf-8"
    )
    problems = version_tools.check_consistency(root)
    assert any("server.json" in p and "packages[0]" in p for p in problems)


def test_flags_lhm_top_version_mismatch(tmp_path):
    root = _sandbox(tmp_path)
    text = (root / "lhm.plugin.json").read_text(encoding="utf-8")
    (root / "lhm.plugin.json").write_text(
        text.replace('"version": "0.30.1"', '"version": "9.9.9"', 1), encoding="utf-8"
    )
    problems = version_tools.check_consistency(root)
    assert any("lhm.plugin.json" in p and "!=" in p for p in problems)


def test_flags_missing_broker_changelog_entry(tmp_path):
    root = _sandbox(tmp_path)
    (root / "broker" / "CHANGELOG.md").write_text(
        "# Changelog — Pacioli (broker)\n\n## Unreleased\n", encoding="utf-8"
    )
    problems = version_tools.check_consistency(root)
    assert any("broker" in p and "CHANGELOG" in p for p in problems)


# --------------------------------------------------------------------------
# set_version — ripples the right surfaces, per package, and only those.
# --------------------------------------------------------------------------

def test_set_broker_ripples_pyproject_init_and_manifests(tmp_path):
    root = _sandbox(tmp_path)
    version_tools.set_version(root, "broker", "0.31.0")
    assert version_tools.read_pyproject_version(root, "broker") == "0.31.0"
    assert version_tools.read_init_version(root, "broker") == "0.31.0"
    server_versions = version_tools.read_json_all_versions(root, "server.json")
    assert server_versions and all(v == "0.31.0" for _label, v in server_versions)
    assert version_tools.read_json_top_version(root, "lhm.plugin.json") == "0.31.0"


def test_set_broker_leaves_guard_alone(tmp_path):
    root = _sandbox(tmp_path)
    version_tools.set_version(root, "broker", "0.31.0")
    assert version_tools.read_pyproject_version(root, "guard") == "0.6.2"
    assert version_tools.read_init_version(root, "guard") == "0.6.2"


def test_set_guard_ripples_pyproject_and_init_only(tmp_path):
    root = _sandbox(tmp_path)
    version_tools.set_version(root, "guard", "0.7.0")
    assert version_tools.read_pyproject_version(root, "guard") == "0.7.0"
    assert version_tools.read_init_version(root, "guard") == "0.7.0"
    # broker's manifests must be untouched (they belong to broker, not guard).
    assert version_tools.read_json_top_version(root, "lhm.plugin.json") == "0.30.1"


def test_set_broker_leaves_inner_tool_version_untouched(tmp_path):
    # The lhm manifest's tool schemas may carry their own "version" — only the
    # top-level version is the package version. Blanket-replacing would corrupt them.
    root = _sandbox(tmp_path, lhm_tool_has_version=True)
    version_tools.set_version(root, "broker", "0.31.0")
    text = (root / "lhm.plugin.json").read_text(encoding="utf-8")
    assert '"version": "0.31.0"' in text  # top-level moved
    assert '"version": "9.9.9"' in text   # the tool's inner version did NOT


def test_set_version_is_idempotent(tmp_path):
    root = _sandbox(tmp_path)
    version_tools.set_version(root, "broker", "0.30.1")
    assert version_tools.check_consistency(root) == []


def test_set_unknown_package_raises(tmp_path):
    root = _sandbox(tmp_path)
    try:
        version_tools.set_version(root, "nope", "1.0.0")
    except (KeyError, ValueError):
        return
    raise AssertionError("expected set_version to reject an unknown package name")


# --------------------------------------------------------------------------
# check_release — stricter: tag match + top released heading.
# --------------------------------------------------------------------------

def test_release_check_passes_when_aligned(tmp_path):
    root = _sandbox(tmp_path)
    assert version_tools.check_release(root, "broker", "0.30.1") == []


def test_release_check_flags_tag_mismatch(tmp_path):
    root = _sandbox(tmp_path)
    problems = version_tools.check_release(root, "broker", "0.30.2")
    assert any("tag" in p for p in problems)


def test_release_check_flags_changelog_not_top(tmp_path):
    root = _sandbox(tmp_path)
    # An out-of-order heading above the current version.
    (root / "broker" / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.31.0 — 2026-07-18 — later\n\n## 0.30.1 — 2026-07-17 — now\n",
        encoding="utf-8",
    )
    problems = version_tools.check_release(root, "broker", "0.30.1")
    assert any("CHANGELOG top" in p for p in problems)
