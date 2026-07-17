# Copyright (c) 2026, John Broadway and contributors
# License: Apache-2.0
"""Version discipline: __init__.__version__ must equal pyproject's version (no drift)."""
import pathlib
import tomllib
import unittest

import pacioli


class TestVersionMatchesPyproject(unittest.TestCase):
    def test_no_drift(self):
        pyproject = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"
        if not pyproject.exists():
            self.skipTest("pyproject.toml not present (installed wheel, not the source tree) — "
                          "the drift guard only applies in-repo")
        with open(pyproject, "rb") as f:
            pyproject_version = tomllib.load(f)["project"]["version"]
        self.assertEqual(pacioli.__version__, pyproject_version)


if __name__ == "__main__":
    unittest.main()
