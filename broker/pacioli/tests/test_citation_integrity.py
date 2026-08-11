# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""A citation is a claim. These check the ones that can be checked mechanically.

Two classes of citation appear in this codebase's prose, and only one of them is safe:

* **Upstream citations** — *"confirmed by enumerating all 130 fields in `quotation.json`"*,
  *"frappe 16.27.1 `model/document.py`"*. These name a FIXED external version. They can be wrong,
  but they cannot ROT: nothing we do changes what that file said at that version. The 2026-08-11
  docstring pass triaged ~190 of these as out of scope, and that triage stands.

* **SELF-citations** — *"pinned by ``test_foo``"*, *"see ``tools.py:1744-1779``"*. These name our
  own code, and they rot on every edit. **The triage rule had no clause for them**, which an
  independent review caught by finding both shapes live:

  - `store.py` cited ``test_honest_ceiling_key_holder_rewrite_BEFORE_the_pinned_position_not_caught``.
    No such test existed. Two near-misses with different spellings did. A *"pinned by X"* claim
    where X is ungreppable is worse than no citation at all: it tells a reader the property is
    guarded and gives them nothing to check.
  - `spine.py` cited ``_Effects.cancel, tools.py:1744-1779``. The real method is ~8,000 lines away;
    the cited range is unrelated module prose.

So: a cited test name must resolve, and line-number self-citations are banned outright, because
nothing can keep them true.
"""
import ast
import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
TESTS = PACKAGE / "tests"

#: Modules whose prose is checked. The whole package except the tests themselves.
SOURCE_FILES = sorted(p for p in PACKAGE.glob("*.py") if p.name != "__init__.py")


def _prose(path):
    """Every docstring and comment in a module, as one string."""
    source = path.read_text()
    chunks = [line for line in source.splitlines() if line.lstrip().startswith("#")]
    tree = ast.parse(source)
    for node in [tree] + [n for n in ast.walk(tree)
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]:
        doc = ast.get_docstring(node)
        if doc:
            chunks.append(doc)
    return "\n".join(chunks)


def _known_test_names():
    """Every test function AND every test module name in the suite."""
    names = set()
    for path in TESTS.glob("test_*.py"):
        names.add(path.stem)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name.startswith("test_"):
                names.add(node.name)
    return names


class TestEveryCitedTestNameResolves(unittest.TestCase):
    """A `test_…` name in prose is a promise that the reader can go read it."""

    #: Long enough to be a real citation rather than an incidental word. The two real defects were
    #: 60+ characters; this floor keeps the check meaningful without matching prose like "test_".
    MIN_LENGTH = 20

    def test_no_module_cites_a_test_that_does_not_exist(self):
        known = _known_test_names()
        self.assertGreater(len(known), 100, "guard-the-guard: the test index must be populated, "
                                            "or every citation below 'resolves' vacuously")

        dangling = {}
        for path in SOURCE_FILES:
            for cited in re.findall(r"\btest_[a-zA-Z0-9_]+", _prose(path)):
                if len(cited) < self.MIN_LENGTH:
                    continue
                # `module.test_name` citations are written as a dotted pair; check the tail.
                if cited in known:
                    continue
                dangling.setdefault(path.name, set()).add(cited)

        self.assertEqual(
            dangling, {},
            f"prose cites test(s) that do not exist: {dangling}. A 'pinned by X' claim where X "
            f"cannot be found tells a reader the property is guarded and hands them nothing to "
            f"check. Fix the spelling or drop the citation.")


class TestNoSelfCitationCarriesALineNumber(unittest.TestCase):
    """`ourfile.py:1744-1779` is true for exactly as long as nobody edits that file."""

    OWN_MODULES = tuple(sorted(p.stem for p in SOURCE_FILES))

    #: The one place a line-number self-citation is allowed: a note RECORDING that a citation had
    #: rotted. Pinned by `test_the_exemption_only_covers_a_recorded_rot` so it cannot be widened.
    EXEMPT_MARKER = "until 2026-"

    def test_no_module_cites_its_own_package_by_line_number(self):
        pattern = re.compile(r"\b(" + "|".join(self.OWN_MODULES) + r")\.py:\d+")
        offenders = {}
        for path in SOURCE_FILES:
            for line in _prose(path).splitlines():
                if pattern.search(line) and self.EXEMPT_MARKER not in line:
                    offenders.setdefault(path.name, []).append(line.strip()[:100])

        self.assertEqual(
            offenders, {},
            f"line-number citations into our own package: {offenders}. They rot on the next edit "
            f"and cannot be kept true — cite by NAME (function, class, or method) instead.")

    def test_the_exemption_only_covers_a_recorded_rot(self):
        """Guard-the-guard: the exemption exists for one historical note. If it ever covers more
        than a couple of lines it has become a loophole rather than a record."""
        pattern = re.compile(r"\b(" + "|".join(self.OWN_MODULES) + r")\.py:\d+")
        exempted = [line for path in SOURCE_FILES for line in _prose(path).splitlines()
                    if pattern.search(line) and self.EXEMPT_MARKER in line]
        self.assertLessEqual(len(exempted), 3,
                             f"the historical-note exemption now covers {len(exempted)} lines; "
                             f"that is a loophole, not a record: {exempted}")

    def test_the_guard_is_reading_real_data(self):
        self.assertIn("spine", self.OWN_MODULES)
        self.assertGreater(len(SOURCE_FILES), 15)
