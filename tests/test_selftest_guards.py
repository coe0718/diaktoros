"""Regressions for two selftest test guards, run on controlled inputs (#2532, #2617).

Each test runs the real guard test from ``test_selftest`` the way the suite does, with only its
input swapped: a source snippet for the step-3 core-name guard, a fake operator home for the
bwrap probe. So reverting either fix turns these red, though the tree itself stays clean.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import test_selftest as ts

GUARD = "test_the_inference_step_reads_the_selftest_core_through_selftest"
CORE = 'PASS, FAIL, WARN, SKIP = "pass", "fail", "warn", "skip"\nclass Report:\n    pass\n'


def run(case: unittest.TestCase) -> list[str]:
    """The failures and errors of one real test, run as the suite runs it."""
    result = unittest.TestResult()
    case.run(result)
    return [text for _, text in result.failures + result.errors]


class CoreNameGuard(unittest.TestCase):
    def guard(self, own: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            core, mine = pathlib.Path(tmp) / "selftest.py", pathlib.Path(tmp) / "inference.py"
            core.write_text(CORE)
            mine.write_text(own)
            with mock.patch.object(ts, "INFERENCE_CORE", core), mock.patch.object(ts, "INFERENCE_OWN", mine):
                return run(ts.InferenceStepModule(GUARD))

    def test_each_way_of_binding_a_core_name_again_fails_the_guard(self):
        shapes = {"FAIL, WARN = 'fail', 'warn'\n": "FAIL", "[PASS, *rest] = ['pass']\n": "PASS",
                  "SKIP: str = 'skip'\n": "SKIP", "from .selftest import PASS\n": "PASS",
                  "from .selftest import Report as Report\n": "Report", "def Report():\n    pass\n": "Report",
                  "from .selftest import PASS as MY_PASS\n": "PASS", "from .selftest import Report as R\n": "Report",
                  "from diaktoros.selftest import PASS as P\n": "PASS"}
        for source, name in shapes.items():
            with self.subTest(source):
                failures = self.guard(source)
                self.assertEqual(len(failures), 1)
                self.assertIn(f"'{name}'", failures[0])

    def test_imports_and_names_that_do_not_clash_pass(self):
        fine = ("import json\nfrom pathlib import Path\nfrom . import selftest\nfrom .other import PASS as OK\n"
                "from .other.selftest import PASS as OK2\nfrom ..selftest import Report as R\n"
                "PASSED = 1\ndef check_seats():\n    pass\n")
        self.assertEqual(self.guard(fine), [])


class OperatorNode(unittest.TestCase):
    def test_the_bwrap_probe_ignores_an_operators_node_under_their_real_home(self):
        # #2617: an operator's ~/.hermes/node made node_report the first subprocess call.
        with tempfile.TemporaryDirectory() as tmp:
            node = pathlib.Path(tmp) / ".hermes" / "node" / "bin" / "node"
            node.parent.mkdir(parents=True)
            node.write_text("#!/bin/sh\necho v22.0.0\n")
            node.chmod(0o755)
            with mock.patch.dict(os.environ, {"HERMES_REAL_HOME": tmp}):
                failures = run(ts.ChecklistTests("test_bwrap_probe_uses_launch_path_and_the_binary_found_there"))
        self.assertEqual(failures, [])


if __name__ == "__main__":
    sys.exit(unittest.main())
