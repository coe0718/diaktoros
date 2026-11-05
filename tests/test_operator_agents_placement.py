"""#1339: the tests of behaviour that existed before ``diaktoros.authorship`` must live in a module
that imports without it, so a fail-then-pass proof on a head without that module runs them by name
instead of erroring the whole module on an import.

This guards where those tests live, not what they check: each named test is found by its
``def`` in ``tests/test_*.py``, and every module that defines it is imported in a child process in
which ``diaktoros.authorship`` cannot be imported. Moving one of them back into a module that imports
``authorship`` (as ``test_authorship.py`` does) turns this red."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

# The tests of code that existed before ``authorship.py`` (issue_facts.comments_section,
# pr_hold.decide with explain's hold line, cmd_explain's printing loop).
PRE_EXISTING = (
    "test_an_agents_comment_reaches_the_seats_labelled_and_a_strangers_does_not",
    "test_without_the_setting_the_agents_comment_is_left_out",
    "test_an_agents_hold_counts_and_explain_names_it",
    "test_cmd_explain_prints_the_authorship_line",
)

# Run in the child: make ``diaktoros.authorship`` unimportable, then import the module named in
# argv[1]. A module-level import of it raises ImportError and the child exits non-zero.
BLOCKED_IMPORT = (
    "import importlib, sys\n"
    "sys.modules['diaktoros.authorship'] = None\n"
    "importlib.import_module(sys.argv[1])\n"
)


def definers(name: str) -> list[str]:
    """The test modules (by module name) whose source defines ``def <name>(``."""
    pattern = re.compile(rf"^\s+def {re.escape(name)}\(", re.M)
    return sorted(path.stem for path in TESTS.glob("test_*.py")
                  if pattern.search(path.read_text(encoding="utf-8")))


def imports_without_authorship(module: str) -> subprocess.CompletedProcess:
    """Import ``module`` in a child process where ``diaktoros.authorship`` cannot be imported."""
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([str(TESTS), str(ROOT), os.environ.get("PYTHONPATH", "")])}
    return subprocess.run([sys.executable, "-c", BLOCKED_IMPORT, module], cwd=str(TESTS), env=env,
                          capture_output=True, text=True, timeout=120)


class Placement(unittest.TestCase):
    def test_pre_existing_behaviour_tests_import_without_authorship(self):
        for name in PRE_EXISTING:
            with self.subTest(test=name):
                modules = definers(name)
                self.assertTrue(modules, f"no test module defines {name}")
                for module in modules:
                    result = imports_without_authorship(module)
                    self.assertEqual(result.returncode, 0,
                                     f"{module} (defines {name}) does not import without "
                                     f"diaktoros.authorship:\n{result.stderr[-600:]}")

    def test_the_guard_sees_a_module_that_imports_authorship(self):
        """The block is real: ``test_authorship`` imports ``authorship`` and fails here."""
        self.assertNotEqual(imports_without_authorship("test_authorship").returncode, 0)


if __name__ == "__main__":
    unittest.main()
