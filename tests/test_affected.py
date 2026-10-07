"""tests/affected.py: the tests that depend on a change, for a fixer turn (#362).

Each case is one of this week's red CIs: the change, and the test elsewhere that broke.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import affected  # noqa: E402


class Select(unittest.TestCase):
    def test_this_weeks_red_cis_are_picked_before_the_harness(self):
        """Each change, and the module elsewhere it broke."""
        for change, broken in (("diaktoros/review_receipt.py", "test_attribution"),   # #358
                               ("diaktoros/config.py", "test_setup"),                # #335
                               ("diaktoros/config.py", "test_triage_cli"),           # #335
                               ("scripts/gate_reviewer.py", "test_gate_shims"),        # #356
                               ("scripts/watchdog.py", "test_merged_tree_blockers"),   # #378
                               ("scripts/watchdog.py", "test_route_self_heal")):       # #378
            with self.subTest(change=change):
                order = affected.select([change])
                self.assertIn(broken, order)
                self.assertEqual(order[0], "test_home_guard")                          # #336/#337
                self.assertIn("run_tests", order)                                      # #343

    def test_direct_dependents_then_the_harness_then_the_rest(self):
        order = affected.select(["diaktoros/review_receipt.py"])
        harness = order.index("run_tests")
        self.assertLess(order.index("test_review_receipt"), harness)
        self.assertLess(order.index("test_attribution"), harness)
        self.assertGreater(len(order), harness + 1)            # transitive layers follow

    def test_a_test_only_change_runs_that_test_and_the_guard(self):
        self.assertEqual(affected.select(["tests/test_stats.py"]), ["test_stats", "test_home_guard"])
        self.assertEqual(affected.select(["./tests/test_stats.py", ""]),
                         ["test_stats", "test_home_guard"])

    def test_docs_helpers_and_unknown_paths(self):
        self.assertEqual(affected.select(["docs/commands.md"]),
                         ["test_home_guard", "test_commands_doc", "run_tests"])
        helper = affected.select(["tests/_ci_green.py"])
        self.assertIn("test_broker_ipc", helper)
        self.assertNotIn("test_stats", helper)
        self.assertEqual(affected.select(["LICENSE"]), ["test_home_guard"])

    def test_relative_imports_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = pathlib.Path(tmp) / "m.py"
            module.write_text("from . import ci, gh\nfrom .config import x\n"
                              "from diaktoros import stats\nimport diaktoros.broker\n")
            self.assertEqual(affected._module_imports(module),
                             {"ci", "gh", "config", "stats", "broker"})


class Run(unittest.TestCase):
    def run_with(self, order, codes, budget, ticks):
        clock = iter(ticks)
        with mock.patch.object(affected.subprocess, "run",
                               side_effect=[subprocess.CompletedProcess([], c, "out", "err")
                                            for c in codes]) as run, \
             redirect_stdout(io.StringIO()) as out:
            rc = affected.run(order, budget, clock=lambda: next(clock))
        return rc, out.getvalue(), run

    def test_stops_starting_modules_past_the_budget_and_names_them(self):
        rc, out, run = self.run_with(["test_a", "run_tests", "test_b"], [0, 0], 100,
                                     [0, 0, 50, 150])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 2)
        self.assertIn("not run (past the 100 s budget; CI runs them): test_b", out)
        harness = run.call_args_list[1].args[0]
        self.assertTrue(harness[-1].endswith("run_tests.py"))

    def test_a_failure_fails_the_check_and_shows_its_output(self):
        rc, out, _ = self.run_with(["test_a", "test_b"], [1, 0], 100, [0, 0, 1, 2])
        self.assertEqual(rc, 1)
        self.assertIn("FAILED test_a", out)
        self.assertIn("outerr", out)

    def test_runs_like_ci_from_the_root_and_an_empty_module_is_no_failure(self):
        """Live (#400 merge): run from tests/, a subprocess `python -m diaktoros…` failed; and
        unittest's exit 5 (no tests in the module) was counted as a failure."""
        rc, out, run = self.run_with(["test_a", "test_b"], [0, 5], 100, [0, 0, 1, 2])
        self.assertEqual(rc, 0, out)
        command, kwargs = run.call_args_list[0].args[0], run.call_args_list[0].kwargs
        self.assertEqual(kwargs["cwd"], affected.ROOT)
        self.assertEqual(command[1:5], ["-m", "unittest", "discover", "-q"])
        self.assertEqual(command[-2:], ["-p", "test_a.py"])

    def test_changed_arrives_as_one_space_separated_argument(self):
        with mock.patch.object(affected, "run", return_value=0) as run:
            affected.main(["--run", "diaktoros/ci.py tests/test_ci_gate.py"])
        self.assertIn("test_ci_gate", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
