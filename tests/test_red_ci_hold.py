"""A red required check holds the review of a PR no fixer takes (#1110, seen on #1103).

On a review-only PR the reviewer used to review a red head anyway: its verdict only said "CI
failed", spent a round of the cap, and closed the head to re-review, so a flaky check that passed
on a rerun left nothing but an empty commit. Now the turn is held, nothing spent, until the checks
pass at the same head (a rerun) or a new head supersedes it, past the CI wait as well. A fixer's
PR is unchanged: the review is how the fixer hears of red CI.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ci_fix as tcf  # noqa: E402
import test_seat_models as sm  # noqa: E402
from diaktoros import ci, ci_fix, ledger, observer, run_supervisor  # noqa: E402
from diaktoros.run_supervisor import CI_HOLD  # noqa: E402

RED = ci.CIState(failed=["verticals"], passed=["lint"])
GREEN = ci.CIState(passed=["verticals", "lint"])


class Hold(sm.Base):
    run_seat = sm.Worker.run_seat

    def review(self, checks, author="boss", **loop_extra):
        loop = {**self.loop, "review_only": ["boss"], **loop_extra}
        pr = {"number": 7, "state": "open", "draft": False, "user": {"login": author},
              "head": {"sha": sm.HEAD, "ref": "fix-7"}, "base": {"ref": "main"}}
        with mock.patch.object(ci, "read", return_value=checks), \
             mock.patch.object(observer, "notify"):
            seen, row, _ = self.run_seat("reviewer", loop=loop, pr=pr)
        return seen, row

    def test_a_red_review_only_head_is_held_with_nothing_spent(self):
        for after_ci in (False, True):
            with self.subTest(review_after_ci=after_ci):
                seen, row = self.review(RED, review_after_ci=after_ci)
                self.assertEqual(seen, {})                      # no model call
                self.assertEqual(row[0], "waiting")
                self.assertTrue(row[1].startswith(f"{CI_HOLD} on {sm.HEAD[:7]}"), row[1])
                self.assertIn("verticals", row[1])
                self.assertIn("re-run them on GitHub", row[1])
                with ledger.connect(self.root / "ledger.sqlite") as con:
                    self.assertEqual(con.execute("SELECT retries FROM runs").fetchone()[0], 0)

    def test_it_outlasts_the_ci_wait(self):
        with mock.patch.object(run_supervisor, "CI_WAIT_MAX_S", 0):
            seen, row = self.review(RED)
        self.assertEqual((seen, row[0]), ({}, "waiting"))

    def test_a_rerun_that_passes_is_reviewed(self):
        for wait in (3600, 0):
            with self.subTest(wait=wait), mock.patch.object(run_supervisor, "CI_WAIT_MAX_S", wait):
                seen, row = self.review(GREEN)
                self.assertEqual((seen["role"], row[0]), ("reviewer", "succeeded"))

    def test_a_fixers_red_head_is_still_reviewed(self):
        # fix_ci off: the review is how the fixer hears of red CI.
        seen, row = self.review(RED, author="fix")
        self.assertEqual((seen["role"], row[0]), ("reviewer", "succeeded"))


class Notice(unittest.TestCase):
    PRS = tcf.Sweep.PRS
    run_sweep = tcf.Sweep.run_sweep

    def test_a_review_only_prs_notice_says_the_review_waits_for_green(self):
        with mock.patch.object(self, "PRS", [{**self.PRS[0], "user": {"login": "boss"}}]):
            notices, queued = self.run_sweep({"review_only": ["boss"]})
        self.assertEqual(queued, [])
        self.assertIn("the review waits for green", notices[0][1]["next_turn"])
        self.assertIn("re-run", notices[0][1]["next_turn"])


if __name__ == "__main__":
    unittest.main()
