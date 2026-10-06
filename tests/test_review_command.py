"""``hermes review-loop review --pr N`` (#375): the operator asks for a fresh review.

The real reviewer gate decides (fed a ready_for_review built from the live PR), so the command
cannot start a review the gate would not, and a refusal names the gate's own reason. Driven
against the harness's fake GitHub.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402


def gate_only(outcome):
    """A ``subprocess.run`` stand-in that replaces only the gate script's run (gh etc. stay real)."""
    import subprocess
    real = subprocess.run

    def run(cmd, *a, **kw):
        if any("gate_reviewer" in str(c) for c in cmd):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return real(cmd, *a, **kw)
    return run


class Review(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def cli(self, *argv):
        return t.run_cli(t.parser_for({}).parse_args(list(argv)))

    def test_an_eligible_head_is_queued_through_the_real_gate(self):
        t.reset(prs={"7": t.pr(7)})
        rc, out = self.cli("review", "--loop", "widgets", "--pr", "7")
        self.assertEqual(rc, 0, out)
        # The harness has no private runtime file, so the gate holds the turn in the queue: the
        # "waiting" branch, with the gate's own reason.
        self.assertIn("#7 @ aaaaaaa: review queued, waiting — isolated worker unavailable", out)
        self.assertNotIn("for the isolated worker", out)
        self.assertTrue(t.held("reviewer", 7, t.HEAD_A))          # the gate's own hold

    def test_a_new_ledger_row_reports_the_isolated_worker(self):
        import subprocess
        from unittest import mock
        t.reset(prs={"7": t.pr(7)})
        counts = iter([0, 1])      # the ledger count before the gate, and after it
        with mock.patch("review_loop.cli._reviewer_runs", side_effect=lambda *a: next(counts)), \
             mock.patch("subprocess.run", side_effect=gate_only(subprocess.CompletedProcess([], 0, "", ""))):
            rc, out = self.cli("review", "--loop", "widgets", "--pr", "7")
        self.assertEqual(rc, 0, out)
        self.assertIn("#7 @ aaaaaaa: review queued for the isolated worker", out)
        self.assertNotIn("waiting", out)

    def test_a_gate_timeout_is_its_own_outcome(self):
        import subprocess
        from unittest import mock
        t.reset(prs={"7": t.pr(7)})
        with mock.patch("subprocess.run", side_effect=gate_only(subprocess.TimeoutExpired("gate", 120))):
            rc, out = self.cli("review", "--loop", "widgets", "--pr", "7")
        self.assertEqual(rc, 2, out)
        self.assertIn("the gate did not finish", out)

    def test_loop_defaults_to_the_only_loop(self):
        t.reset(prs={"7": t.pr(7)})
        rc, out = self.cli("review", "--pr", "7")
        self.assertIn("#7 @ aaaaaaa:", out)

    def test_the_gate_declines_with_its_own_reason(self):
        t.reset(prs={"7": t.pr(7, draft=True)})
        rc, out = self.cli("review", "--loop", "widgets", "--pr", "7")
        self.assertEqual(rc, 1, out)
        self.assertIn("no review started — draft PR", out)
        self.assertFalse(t.held("reviewer", 7, t.HEAD_A))
        t.reset(prs={"7": t.pr(7, author="outsider")})
        rc, out = self.cli("review", "--loop", "widgets", "--pr", "7")
        self.assertEqual(rc, 1, out)
        self.assertIn("is not a fixer", out)

    def test_cannot_ask_without_a_loop_or_a_pr(self):
        t.reset(prs={})
        rc, out = self.cli("review", "--loop", "widgets", "--pr", "7")
        self.assertEqual(rc, 2, out)
        self.assertIn("could not be read", out)
        rc, out = self.cli("review", "--loop", "nope", "--pr", "7")
        self.assertEqual(rc, 2, out)


if __name__ == "__main__":
    unittest.main()
