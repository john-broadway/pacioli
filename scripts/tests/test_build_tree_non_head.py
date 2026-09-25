"""build-tree at a NON-HEAD ref (the 0.40.0 finish caught it: `git rm --cached` refuses an
index entry that matches neither the worktree nor HEAD, so every prior run had only ever
worked because ref == HEAD). A tag is not HEAD on the day the finish step runs."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_leak_audit import build_public_tree  # noqa: E402


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def _repo_with_two_commits(tmp_path: Path) -> Path:
    root = tmp_path / "r"; root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t"); _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("public\n")
    (root / "CLAUDE.md").write_text("v1 internal\n")            # denied by basename
    (root / "deploy" / "bench").mkdir(parents=True)
    (root / "deploy" / "bench" / "driver.sh").write_text("v1\n")  # denied by prefix
    _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "A")
    (root / "CLAUDE.md").write_text("v2 internal\n")             # HEAD and worktree move past A
    (root / "deploy" / "bench" / "driver.sh").write_text("v2\n")
    _git(root, "commit", "-q", "-am", "B")
    return root


def test_build_tree_at_a_non_head_ref_strips_the_same_paths(tmp_path):
    root = _repo_with_two_commits(tmp_path)
    tree = build_public_tree("HEAD~1", root=root)          # the ref is A; HEAD is B
    built = set(_git(root, "ls-tree", "-r", "--name-only", tree).split("\n"))
    assert built == {"README.md"}, built
    # and the real index / worktree were never touched
    assert _git(root, "status", "--porcelain") == ""
    assert (root / "CLAUDE.md").read_text() == "v2 internal\n"


def test_build_tree_at_head_still_works(tmp_path):
    root = _repo_with_two_commits(tmp_path)
    tree = build_public_tree("HEAD", root=root)
    assert set(_git(root, "ls-tree", "-r", "--name-only", tree).split("\n")) == {"README.md"}
