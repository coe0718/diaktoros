"""#569: a fix round on a head whose required checks failed gets the failing jobs' logs.

Only CI-fix turns used to get them (`ci_fix.section`). A fix round answering "required check X
failed" had the check's name and nothing else, so the fixer could not see what to fix (#537).
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import ci, ci_fix, gh, issue_facts, run_supervisor  # noqa: E402

LOOP = {"id": "w", "repo": "acme/w", "base": "main", "cap": 3, "read_token": "read",
        "seats": {}, "fixers": ["fix"], "reviewers": ["rev"], "reviewer_seat": "rev"}
RED = ci.CIState(failed=["tests (3.11)"], ids={"tests (3.11)": 41},
                 urls={"tests (3.11)": "https://example.invalid/job/41"})


class FixRoundCI(unittest.TestCase):
    def prompt(self, seat, state):
        row = {"seat": seat, "repo": "acme/w", "pr": 7, "head": "a" * 40}
        with mock.patch.object(ci, "read", return_value=state), \
                mock.patch.object(issue_facts, "section", return_value=""), \
                mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
                mock.patch.object(gh, "pr_url", return_value="https://github.com/acme/w/pull/7"), \
                mock.patch.object(ci_fix, "failing_step", return_value="Run unit tests"), \
                mock.patch.object(gh, "read_text",
                                  return_value="ok\nFAIL: test_walk (test_reader_identity)\n"), \
                mock.patch("diaktoros.gate.verdicts", return_value=[{}]), \
                mock.patch("diaktoros.gate.latest_effective_review_at_head", return_value=None):
            return run_supervisor.isolated_prompt(LOOP, row, [],
                                                  change=run_supervisor.PRChange("RECORD", "DIFF"))

    def test_a_fix_round_on_a_red_head_gets_the_failing_job_and_its_log(self):
        text = self.prompt("fixer", RED)
        self.assertIn("## Failing checks at this head", text)
        self.assertIn("Failing step: 'Run unit tests'", text)
        self.assertIn("FAIL: test_walk (test_reader_identity)", text)

    def test_a_green_head_gets_no_failing_jobs(self):
        self.assertNotIn("## Failing checks at this head", self.prompt("fixer", ci.CIState()))

    def test_unreadable_ci_gets_no_failing_jobs(self):
        self.assertNotIn("## Failing checks at this head", self.prompt("fixer", None))

    def test_the_reviewer_is_not_given_the_logs(self):
        # The reviewer already sees which checks failed; the logs are the fixer's to act on.
        self.assertNotIn("## Failing checks at this head", self.prompt("reviewer", RED))


if __name__ == "__main__":
    unittest.main()
