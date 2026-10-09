"""#539: failed CI gets its own budget — the fixer takes a red head first, up to ``ci_fix_cap``
CI-fix turns per PR, spending no review verdict; then the reviewer reviews it.

The first review after CI-fix commits is shown them, so it checks none weakened a test.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
import test_review_after_ci as rac  # noqa: E402
import test_seat_models as sm  # noqa: E402
from diaktoros import ci, ci_fix, cli, config, gh, observer  # noqa: E402
from diaktoros.run_supervisor import CI_HOLD  # noqa: E402

RED = ci.CIState(failed=["tests (3.11)"], passed=["lint"])
LOOP = {"repo": "acme/widgets", "cap": 3, "read_token": "reader", "base": "main",
        "fixers": ["fix"], "required_checks": [], "fix_ci": True,
        "unattended_fixer_push": True, "review_only": []}


class FixerFirst(unittest.TestCase):
    def first(self, rows=(), loop=None, author="fix", failed=("tests",)):
        queued = []
        with mock.patch.object(ci_fix, "rows", return_value=list(rows)), \
                mock.patch("diaktoros.gate.enqueue_isolated",
                           side_effect=lambda *a, **kw: queued.append(kw) or "enqueued"):
            got = ci_fix.fixer_first(loop or LOOP, number=7, head="h", author=author,
                                     failed=list(failed))
        return got, queued

    def test_a_first_red_head_is_queued_for_the_fixer_now(self):
        got, queued = self.first()
        self.assertTrue(got)
        self.assertEqual(queued, [{"turn_key": ci_fix.KEY}])

    def test_a_live_turn_at_the_head_keeps_it_with_the_fixer(self):
        got, queued = self.first(rows=[{"head": "h", "state": "running"}])
        self.assertEqual((got, queued), (True, []))

    def test_a_finished_turn_at_the_head_lets_the_reviewer_review(self):
        # The fixer found nothing to push: the red head must not wait forever.
        got, queued = self.first(rows=[{"head": "h", "state": "succeeded"}])
        self.assertEqual((got, queued), (False, []))

    def test_a_spent_budget_lets_the_reviewer_review(self):
        got, queued = self.first(rows=[{"head": f"o{i}", "state": "succeeded"} for i in range(3)])
        self.assertEqual((got, queued), (False, []))

    def test_not_the_fixers_or_ci_fixes_off_never_hold_a_review(self):
        self.assertEqual(self.first(author="someone")[0], False)
        self.assertEqual(self.first(loop={**LOOP, "fix_ci": False})[0], False)
        self.assertEqual(self.first(failed=())[0], False)


class ReviewerHold(sm.Worker):
    """The production worker: a reviewer turn on a red fixer's head is held, not launched."""

    def review_turn(self, rows=()):
        loop = {**self.loop, "review_after_ci": True, "fix_ci": True,
                "unattended_fixer_push": True, "fixers": ["fix"]}
        pr = {"number": 7, "head": {"sha": sm.HEAD, "ref": "fix-7"}, "user": {"login": "fix"},
              "state": "open", "draft": False}
        queued = []
        with mock.patch.object(ci, "read", return_value=RED), \
                mock.patch.object(ci_fix, "rows", return_value=list(rows)), \
                mock.patch("diaktoros.gate.enqueue_isolated",
                           side_effect=lambda *a, **kw: queued.append(kw) or "enqueued"), \
                mock.patch.object(observer, "notify"):
            seen, row, _ = self.run_seat("reviewer", loop=loop, pr=pr)
        return seen, row, queued

    def test_red_ci_goes_to_the_fixer_and_no_review_is_spent(self):
        seen, row, queued = self.review_turn()
        self.assertEqual(seen, {}, "no review turn launched")
        self.assertEqual(row[0], "waiting")
        self.assertIn("the fixer is taking them first", row[1])
        self.assertTrue(row[1].startswith(CI_HOLD))
        self.assertEqual(queued, [{"turn_key": ci_fix.KEY}])

    def test_once_the_budget_is_spent_the_reviewer_reviews_the_red_head(self):
        seen, row, queued = self.review_turn(rows=[{"head": f"o{i}", "state": "succeeded"}
                                           for i in range(3)])
        self.assertEqual((seen.get("role"), row[0], queued), ("reviewer", "succeeded", []))


class History(unittest.TestCase):
    def test_the_review_after_ci_fixes_is_shown_their_test_changes(self):
        rows = [{"head": "a" * 40, "state": "succeeded", "push_confirmed": 1.0},
                {"head": "b" * 40, "state": "failed", "push_confirmed": None}]
        commits = [{"sha": "c" * 40, "parents": [{"sha": "a" * 40}]}]
        detail = {"files": [{"filename": "tests/test_x.py", "status": "modified",
                             "additions": 1, "deletions": 9},
                            {"filename": "diaktoros/x.py", "status": "modified"}]}
        with mock.patch.object(ci_fix, "rows", return_value=rows), \
                mock.patch.object(gh, "fetch", return_value=(commits, "")), \
                mock.patch.object(gh, "api", return_value=detail):
            text = ci_fix.fix_history(LOOP, 7)
        self.assertIn("weakened, skipped or removed a test", text)
        self.assertIn("`ccccccc` (CI fix of `aaaaaaa`): tests changed: tests/test_x.py "
                      "(modified, +1 -9)", text)
        self.assertNotIn("diaktoros/x.py", text)

    def test_no_ci_fix_push_no_section(self):
        with mock.patch.object(ci_fix, "rows", return_value=[{"head": "a", "state": "failed",
                                                             "push_confirmed": None}]):
            self.assertEqual(ci_fix.fix_history(LOOP, 7), "")

    def test_unreadable_commits_say_so(self):
        rows = [{"head": "a" * 40, "state": "succeeded", "push_confirmed": 1.0}]
        with mock.patch.object(ci_fix, "rows", return_value=rows), \
                mock.patch.object(gh, "fetch", return_value=(None, "HTTP 500")):
            self.assertIn("could not be read", ci_fix.fix_history(LOOP, 7))


class PromptHook(unittest.TestCase):
    def prompt(self, seat):
        from diaktoros import issue_facts, run_supervisor
        row = {"seat": seat, "repo": "acme/w", "pr": 7, "head": "a" * 40}
        loop = {**LOOP, "repo": "acme/w", "id": "w", "seats": {}, "reviewers": ["rev"],
                "reviewer_seat": "rev"}
        with mock.patch.object(ci, "read", return_value=ci.CIState(passed=["t"])), \
                mock.patch.object(ci_fix, "fix_history", return_value="\n\nHISTORY-SECTION"), \
                mock.patch.object(issue_facts, "section", return_value=""), \
                mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
                mock.patch.object(gh, "pr_url", return_value="https://github.com/acme/w/pull/7"), \
                mock.patch("diaktoros.gate.verdicts", return_value=[{}]), \
                mock.patch("diaktoros.gate.latest_effective_review_at_head", return_value=None):
            return run_supervisor.isolated_prompt(loop, row, [],
                                                  change=run_supervisor.PRChange("R", "D"))

    def test_the_reviewer_is_shown_the_ci_fix_history_and_the_fixer_is_not(self):
        self.assertIn("HISTORY-SECTION", self.prompt("reviewer"))
        self.assertNotIn("HISTORY-SECTION", self.prompt("fixer"))


class Setting(rac.Cli):
    """ci_fix_cap moves from every path (#539, the every-setting rule)."""

    # rac.Cli's own tests run in their module; only its init/cli helpers are borrowed here.
    test_init_starts_from_the_form_and_the_flag_wins = None
    test_set_and_apply_move_it = None
    test_setup_hands_init_the_answer = None

    def cap_written(self):
        return json.loads(rac.LOOP_FILE.read_text()).get("ci_fix_cap")

    def test_validation_and_default(self):
        self.assertEqual(config.ci_fix_cap(config.normalize(rac.raw())), 3)
        self.assertEqual(config.ci_fix_cap(config.normalize(rac.raw(ci_fix_cap=5))), 5)
        for bad in (0, 11, "x", True, 2.5):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.normalize(rac.raw(ci_fix_cap=bad))

    def test_form_init_set_apply_and_setup(self):
        self.assertNotIn("ci_fix_cap", config.apply_settings(rac.raw(), {}))
        self.assertEqual(config.apply_settings(rac.raw(), {"ci_fix_cap": "4"})["ci_fix_cap"], 4)
        self.assertIn("ci_fix_cap", config.SETTINGS_SCHEMA)
        rc, out = self.init("--ci-fix-cap", "2")
        self.assertEqual((rc, self.cap_written()), (0, 2), out)
        rc, out = self.cli("set", "--loop", rac.LOOP_ID, "--ci-fix-cap", "5")
        self.assertEqual((rc, self.cap_written()), (0, 5), out)
        rc, out = self.cli("set", "--loop", rac.LOOP_ID, "--ci-fix-cap", "0")
        self.assertEqual((rc, self.cap_written()), (0, None), out)
        rc, out = self.cli("apply", "--loop", rac.LOOP_ID, settings={"ci_fix_cap": "7"})
        self.assertEqual((rc, self.cap_written()), (0, 7), out)
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--ci-fix-cap", "6"])
        argv, _ = cli._setup_init_argv(args, t.REPO, rac.LOOP_ID, interactive=False)
        self.assertIn("--ci-fix-cap=6", argv)

    def test_doctor_names_the_budget(self):
        from diaktoros import doctor
        on = config.normalize(rac.raw(fix_ci=True, unattended_fixer_push=True, ci_fix_cap=4))
        self.assertIn("up to 4 CI-fix turn(s) per PR", doctor.check_fix_ci(on).detail)


if __name__ == "__main__":
    unittest.main()
