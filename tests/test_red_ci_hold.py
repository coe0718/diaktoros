"""A red required check holds the review of a PR no fixer takes (#1110, seen on #1103).

On a review-only PR the reviewer used to review a red head anyway: its verdict only said "CI
failed", spent a round of the cap, and closed the head to re-review, so a flaky check that passed
on a rerun left nothing but an empty commit. Now the turn is held, nothing spent, until the checks
pass at the same head (a rerun) or a new head supersedes it, past the CI wait as well. A fixer's
PR is held the same way once its CI fixes are done; only with CI fixes off is it reviewed red,
since that review is how the fixer hears of the failure.
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
from diaktoros import ci, ci_fix, ledger, observer, run_supervisor, stats  # noqa: E402
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

    def test_status_shows_the_whole_remedy_for_one_failed_check(self):
        # stats cuts a held line at WHY_MAX: the remedy comes before the check names (#1124).
        self.review(RED)
        view = stats.now(self.root / "ledger.sqlite", self.loop["repo"])
        why = [r["why"] for r in view["runs"]]
        self.assertEqual(len(why), 1)
        self.assertIn("the review starts when they pass (re-run them on GitHub) or a new head "
                      "is pushed", why[0])

    def test_a_long_held_line_is_cut_at_a_word_with_an_ellipsis(self):
        # #1700: a bare [:WHY_MAX] slice left the red-CI hold ending in a dangling '; n'.
        _, row = self.review(RED)
        error = row[1] + "; and one more check, named at length"
        self.assert_cut_at_a_boundary(error)
        self.assertEqual(stats._why("waiting", "held: short"), "held: short")

    def test_a_cut_that_trims_a_hyphen_still_reads_as_a_boundary(self):
        # #2103: _clip trims a trailing hyphen or em dash as well as " ;,:", so a cut that
        # lands just after "ok-" ends at "ok" with "-" next: a boundary, not a mid-word cut.
        error = "held: " + "a " * 70 + "ok- " + "z" * 50
        self.assertEqual(self.assert_cut_at_a_boundary(error), "-")

    def assert_cut_at_a_boundary(self, error: str) -> str:
        """``error``'s held line is cut to WHY_MAX with an ellipsis, at one of the boundaries
        ``stats._clip`` trims; returns the character the cut stopped before."""
        self.assertGreater(len(error), stats.WHY_MAX)
        why = stats._why("waiting", error)
        self.assertLessEqual(len(why), stats.WHY_MAX)
        self.assertTrue(why.endswith("…"), why)
        cut = why[:-1]
        self.assertTrue(error.startswith(cut), why)
        self.assertIn(error[len(cut)], stats.CLIP_TRIM, why)    # ends at a boundary, never mid-word
        return error[len(cut)]

    def test_it_outlasts_the_ci_wait(self):
        with mock.patch.object(run_supervisor, "CI_WAIT_MAX_S", 0):
            seen, row = self.review(RED)
        self.assertEqual((seen, row[0]), ({}, "waiting"))

    def test_it_explains_as_a_wait_with_no_time_limit(self):
        # #674, review of #2398 F3: unlike the other CI waits this one never times out, so its
        # explanation promises only the event, before the CI wait and past it alike.
        from diaktoros.run_supervisor import views
        for wait in (3600, 0):
            with self.subTest(wait=wait), mock.patch.object(run_supervisor, "CI_WAIT_MAX_S", wait):
                seen, _ = self.review(RED)
                self.assertEqual(seen, {})
                rows = views.read_only_view(self.root / "ledger.sqlite", "acme/widgets", 7)
                self.assertEqual(len(rows), 1)
                result = views.hold_explanation(rows[0])
                self.assertEqual(
                    (result["reason_code"], result["waiting_for"], result["automatic_resume"]),
                    ("ci_failed_wait", "failed_checks_pass_or_new_head", "after_event"))
                self.assertIn("with no time limit", result["text"])
                self.assertEqual(result["next_eligible_start"], rows[0]["retry_at"])

    def test_a_rerun_that_passes_is_reviewed(self):
        for wait in (3600, 0):
            with self.subTest(wait=wait), mock.patch.object(run_supervisor, "CI_WAIT_MAX_S", wait):
                seen, row = self.review(GREEN)
                self.assertEqual((seen["role"], row[0]), ("reviewer", "succeeded"))

    def test_a_fixers_red_head_is_held_once_its_ci_fixes_are_done(self):
        # The CI-fix budget spent, or this head's CI-fix turn ended without a new head.
        on = {"fix_ci": True, "unattended_fixer_push": True, "fixers": ["fix"]}
        for rows in ([{"head": f"o{i}", "state": "succeeded"} for i in range(3)],
                     [{"head": sm.HEAD, "state": "succeeded"}]):
            for wait in (3600, 0):
                with self.subTest(rows=len(rows), wait=wait), \
                        mock.patch.object(ci_fix, "rows", return_value=rows), \
                        mock.patch.object(run_supervisor, "CI_WAIT_MAX_S", wait):
                    seen, row = self.review(RED, author="fix", **on)
                    self.assertEqual((seen, row[0]), ({}, "waiting"))
                    self.assertIn("the fixer's CI fixes are done", row[1])
        with mock.patch.object(ci_fix, "rows", return_value=rows):
            seen, row = self.review(GREEN, author="fix", **on)
        self.assertEqual((seen["role"], row[0]), ("reviewer", "succeeded"))

    def test_a_fixers_red_head_is_still_reviewed_with_ci_fixes_off(self):
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
