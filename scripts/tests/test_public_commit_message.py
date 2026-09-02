"""The public release commit must carry its reason — in the SUBJECT, not only the body.

The generator shipped untested and with a bare hardcoded subject ("release: broker X.Y.Z"),
which is the only text GitHub shows beside files and in the commit list. Proximo's twin was
caught by John on its 0.39.0 ("why did we stop commenting on our gh releases?"); this repo's
headings already carry the reason as a title segment, so the subject reads it from there.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from public_commit_message import (  # noqa: E402
    build, build_dual, entry_for, parse_argv, title_for)

SAMPLE = """# Changelog

## Unreleased

## 0.9.0 - 2026-01-02 - the gate learns to say no

**A headline.** Some prose.

- A bullet that explains the change.

## 0.8.0 — 2026-01-01 — an em-dash era heading

- The older one, which must NOT leak into 0.9.0's message.
"""


def test_entry_is_the_version_section_only():
    got = entry_for("0.9.0", SAMPLE)
    assert "A bullet that explains the change." in got
    assert "must NOT leak" not in got


def test_subject_carries_the_headings_own_title():
    msg = build("broker", "0.9.0", SAMPLE)
    first, blank, *_ = msg.splitlines()
    assert first == "release: broker 0.9.0: the gate learns to say no", (
        "the SUBJECT must carry the one-line reason — it is the only text GitHub "
        "shows beside files (every pacioli release to date shipped it bare)")
    assert blank == ""


def test_em_dash_separated_headings_parse_too():
    assert title_for("0.8.0", SAMPLE) == "an em-dash era heading"


def test_a_title_containing_the_separator_stays_whole():
    s = SAMPLE.replace("the gate learns to say no", "half a fix - and its guard too")
    assert title_for("0.9.0", s) == "half a fix - and its guard too"


def test_a_heading_with_no_title_refuses_instead_of_shipping_bare():
    s = SAMPLE.replace(" - the gate learns to say no", "")
    with pytest.raises(SystemExit) as e:
        build("broker", "0.9.0", s)
    assert "no title" in str(e.value)


def test_an_overlong_subject_refuses():
    s = SAMPLE.replace(
        "the gate learns to say no",
        "a title that rambles far past any reasonable subject ceiling and just keeps on going")
    with pytest.raises(SystemExit) as e:
        build("broker", "0.9.0", s)
    assert "72" in str(e.value)


GUARD_SAMPLE = """# Changelog — Pacioli Guard

## 0.5.0 - 2026-01-02 - the guard half of the same release

- A guard bullet, which must appear under its own heading in the body.

## 0.4.0 - 2025-12-01 - older guard

- Old guard text that must NOT appear.
"""


def test_dual_subject_names_both_versions_and_carries_the_first_title():
    """The 2026-08-11 shape (guard 0.14.0 + broker 0.37.0 as one public commit), now generated:
    a subject a visitor scans must name BOTH versions that sit on this sha."""
    msg = build_dual("broker", "0.9.0", SAMPLE, "guard", "0.5.0", GUARD_SAMPLE)
    lines = msg.splitlines()
    assert lines[0] == "release: broker 0.9.0 + guard 0.5.0: the gate learns to say no"
    assert lines[1] == ""
    assert "A bullet that explains the change." in msg
    assert "## broker 0.9.0 - the gate learns to say no" in msg, "both halves carry a heading"
    assert "## guard 0.5.0 - the guard half of the same release" in msg
    assert "A guard bullet, which must appear" in msg
    assert "Old guard text" not in msg and "must NOT leak" not in msg


def test_dual_body_order_is_first_package_then_the_other():
    msg = build_dual("broker", "0.9.0", SAMPLE, "guard", "0.5.0", GUARD_SAMPLE)
    assert msg.index("A bullet that explains") < msg.index("## guard 0.5.0")


def test_dual_tail_names_both_changelogs():
    msg = build_dual("broker", "0.9.0", SAMPLE, "guard", "0.5.0", GUARD_SAMPLE)
    assert "broker/CHANGELOG.md and guard/CHANGELOG.md" in msg


def test_dual_refuses_when_the_other_entry_is_missing():
    with pytest.raises(SystemExit) as e:
        build_dual("broker", "0.9.0", SAMPLE, "guard", "9.9.9", GUARD_SAMPLE)
    assert "9.9.9" in str(e.value)


EMPTY_ENTRY = """# C

## 0.5.0 - 2026-01-02 - a title

## 0.4.0 - 2025-12-01 - older

- text
"""


def test_a_present_but_EMPTY_entry_refuses_in_both_forms():
    """The missing-entry tests only ever exercised entry_for's own refusal; a heading with
    nothing under it reached the body guards, and deleting those guards stayed green (lens)."""
    with pytest.raises(SystemExit) as e:
        build("guard", "0.5.0", EMPTY_ENTRY)
    assert "entry is empty" in str(e.value)
    with pytest.raises(SystemExit) as e:
        build_dual("broker", "0.9.0", SAMPLE, "guard", "0.5.0", EMPTY_ENTRY)
    assert "entry is empty" in str(e.value)
    with pytest.raises(SystemExit) as e:
        build_dual("guard", "0.5.0", EMPTY_ENTRY, "broker", "0.9.0", SAMPLE)
    assert "entry is empty" in str(e.value)


def test_the_tail_is_one_helper_for_both_forms():
    single = build("broker", "0.9.0", SAMPLE)
    dual = build_dual("broker", "0.9.0", SAMPLE, "guard", "0.5.0", GUARD_SAMPLE)
    assert "described in broker/CHANGELOG.md." in single
    assert "described in broker/CHANGELOG.md and guard/CHANGELOG.md." in dual
    assert "one squashed commit per release by design" in single and "one squashed commit per release by design" in dual


def test_dual_refuses_the_same_package_twice():
    with pytest.raises(SystemExit) as e:
        build_dual("broker", "0.9.0", SAMPLE, "broker", "0.9.0", SAMPLE)
    assert "OTHER package" in str(e.value)


def test_dual_overlong_subject_refuses():
    # 42 chars: fits the single subject (23 + 42 = 65), not the dual one (37 + 42 = 79)
    s = SAMPLE.replace("the gate learns to say no", "a title that fits alone but not beside two")
    build("broker", "0.9.0", s)   # single form: fits
    with pytest.raises(SystemExit) as e:
        build_dual("broker", "0.9.0", s, "guard", "0.5.0", GUARD_SAMPLE)
    assert "72" in str(e.value)


@pytest.mark.parametrize("argv", [
    ["broker", "0.9.0", "--with", "guard"],            # missing the other version
    ["broker", "0.9.0", "--with", "broker", "0.9.0"],  # same package twice
    ["broker", "0.9.0", "--and", "guard", "0.5.0"],    # wrong flag
    ["broker", "0.9.0", "guard", "0.5.0"],             # no flag
    ["nope", "0.9.0"],
    [],
])
def test_parse_argv_refuses_every_other_shape(argv):
    with pytest.raises(SystemExit):
        parse_argv(argv)


def test_parse_argv_accepts_the_two_shapes():
    assert parse_argv(["guard", "0.5.0"]) == ("guard", "0.5.0", None)
    assert parse_argv(["broker", "0.9.0", "--with", "guard", "0.5.0"]) == (
        "broker", "0.9.0", ("guard", "0.5.0"))


def test_missing_entry_refuses():
    with pytest.raises(SystemExit):
        build("broker", "1.2.3", SAMPLE)


@pytest.mark.parametrize("package,changelog", [
    ("broker", "broker/CHANGELOG.md"),
    ("guard", "guard/CHANGELOG.md"),
])
def test_the_live_head_release_heading_carries_a_title(package: str, changelog: str):
    """Runs against the REAL changelogs, so the helper cannot pass on a fixture alone.

    Uses the newest RELEASED heading (skipping Unreleased): its title must parse, even
    where the composed subject would exceed the ceiling (historical titles ran long;
    the ceiling gates future releases at release time, not history).
    """
    root = Path(__file__).resolve().parent.parent.parent
    text = (root / changelog).read_text(encoding="utf-8")
    import re
    heads = re.findall(r"^## (\d[^\s]*)", text, re.M)
    assert heads, "no released headings found"
    assert title_for(heads[0], text), "the newest released heading must carry a title"
