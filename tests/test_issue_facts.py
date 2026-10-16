"""#511: both seats read the closing issue and its maintainers' comments; the broker checks the
reviewer's Requirements section. GitHub is mocked; nothing real is touched."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import gh, issue_facts, run_supervisor  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402
from diaktoros.run_supervisor import pr_facts  # noqa: E402

LOOP = {"repo": "acme/w", "id": "l", "base": "main", "read_token": "read",
        "triage": {"maintainers": ["Boss"]}}


def comment(login, body):
    return {"user": {"login": login}, "body": body, "created_at": "2026-01-01T00:00:00Z"}


class Fake:
    """PR 7 closes #5; #9 exists; #5 has a maintainer's and a stranger's comment."""

    def __init__(self, pr_body="Fixes #5"):
        self.pr_body = pr_body

    def api(self, loop, path, **kw):
        if path.endswith("/pulls/7"):
            return {"number": 7, "body": self.pr_body}
        for n in (5, 9):
            if path.endswith(f"/issues/{n}"):
                return {"number": n, "title": f"T{n}", "body": f"BODY{n} Done when: x"}
        return None

    def comments(self, loop, number):
        return [comment("boss", "MAINTAINER-REQ"), comment("stranger", "STRANGER-TEXT")], ""


class IssueFacts(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        for target, repl in ((gh, "api"), (gh, "issue_comments_read")):
            fn = self.fake.api if repl == "api" else self.fake.comments
            patch = mock.patch.object(target, repl, side_effect=fn)
            patch.start()
            self.addCleanup(patch.stop)

    def test_section_has_body_and_maintainer_comment_but_not_a_strangers(self):
        text = issue_facts.section(LOOP, 7)
        self.assertIn("BODY5", text)
        self.assertIn("MAINTAINER-REQ", text)
        self.assertNotIn("STRANGER-TEXT", text)

    def test_pr_closing_no_issue_is_unaffected(self):
        self.fake.pr_body = "just a change"
        self.assertEqual(issue_facts.section(LOOP, 7), "")
        self.assertEqual(issue_facts.check(LOOP, 7, "APPROVE", "looks good"), "")

    def test_missing_requirements_section_is_refused(self):
        self.assertIn("Requirements", issue_facts.check(LOOP, 7, "REQUEST_CHANGES", "F1: a.py: x"))

    def test_approve_with_not_met_is_refused_but_request_changes_passes(self):
        body = "## Requirements\n- item one: met (tests/a.py::t)\n- item two: not met\n"
        self.assertIn("not met", issue_facts.check(LOOP, 7, "APPROVE", body))
        self.assertEqual(issue_facts.check(LOOP, 7, "REQUEST_CHANGES", body), "")

    def test_all_met_approves(self):
        body = "Requirements\n- one: met (a.py:3)\n- two: met (test_x)\n"
        self.assertEqual(issue_facts.check(LOOP, 7, "APPROVE", body), "")

    def test_deferred_needs_an_existing_issue(self):
        ok = "## Requirements\n- one: deferred to #9\n"
        self.assertEqual(issue_facts.check(LOOP, 7, "APPROVE", ok), "")
        bad = "## Requirements\n- one: deferred to #404\n"
        self.assertIn("#404", issue_facts.check(LOOP, 7, "APPROVE", bad))

    def test_unreadable_pr_refuses_rather_than_skipping(self):
        with mock.patch.object(gh, "api", return_value=None):
            self.assertIn("could not read", issue_facts.check(LOOP, 7, "APPROVE", "x"))


class Prompts(unittest.TestCase):
    def test_issue_fix_prompt_shows_maintainer_comments_not_strangers(self):
        fake = Fake()
        issue = {"number": 5, "state": "open", "user": {"login": "o"}, "title": "T5",
                 "body": "BODY5", "labels": [], "html_url": "https://github.com/acme/w/issues/5"}
        with mock.patch.object(run_supervisor, "issue_fix_issue", return_value=issue), \
                mock.patch.object(gh, "issue_comments_read", side_effect=fake.comments):
            text = turn_prompts.issue_fix_prompt(LOOP, {"repo": "acme/w", "pr": 5, "head": "c" * 40})
        self.assertIn("MAINTAINER-REQ", text)
        self.assertNotIn("STRANGER-TEXT", text)
        self.assertLess(text.index("BODY5"), text.index("MAINTAINER-REQ"))

    def test_fixer_round_prompt_carries_the_closing_issue_facts(self):
        # #511: isolated_prompt (fixer, fix round) appends the same issue section.
        fake = Fake()
        row = {"seat": "fixer", "repo": "acme/w", "pr": 7, "head": "a" * 40}
        change = pr_facts.PRChange("RECORD", "DIFF")
        loop = {**LOOP, "cap": 3, "seats": {}}
        with mock.patch.object(gh, "api", side_effect=fake.api), \
                mock.patch.object(gh, "issue_comments_read", side_effect=fake.comments), \
                mock.patch("diaktoros.gate.verdicts", return_value=[{}]), \
                mock.patch("diaktoros.gate.latest_effective_review_at_head", return_value=None), \
                mock.patch("diaktoros.gh.pr_url", return_value="https://github.com/acme/w/pull/7"), \
                mock.patch("diaktoros.ci.read", return_value=None), \
                mock.patch("diaktoros.ci.section", return_value=""):
            text = turn_prompts.isolated_prompt(loop, row, [], change=change)
        self.assertIn("BODY5", text)
        self.assertIn("MAINTAINER-REQ", text)
        self.assertNotIn("STRANGER-TEXT", text)

    def test_656_fixture_deferral_in_pr_comment_is_visible_and_accepted_by_the_broker(self):
        # #666/#656: split into #658 lives only in a maintainer's PR comment. The seat sees it,
        # and the broker's check accepts `deferred to #658` (issue exists) but not a made-up one.
        base = Fake().api

        def api(loop, path, **kw):
            if path.endswith("/issues/658"):
                return {"number": 658, "title": "split", "body": "x", "state": "open"}
            return base(loop, path, **kw)

        def comments(loop, number):
            if number == 7:
                return [comment("boss", "PR-DEFERRAL digest coverage split into #658")], ""
            return [], ""
        with mock.patch.object(gh, "api", side_effect=api), \
                mock.patch.object(gh, "issue_comments_read", side_effect=comments):
            self.assertIn("split into #658", issue_facts.section(LOOP, 7))
            body = "## Requirements\n- digest coverage: deferred to #658\n"
            self.assertEqual(issue_facts.check(LOOP, 7, "APPROVE", body), "")
            bad = "## Requirements\n- digest coverage: deferred to #999\n"
            self.assertIn("#999", issue_facts.check(LOOP, 7, "APPROVE", bad))

    def test_reviewer_prompt_carries_maintainer_pr_comment_not_a_strangers(self):
        # #666: a deferral posted on the PR (#656) reaches the reviewer; a stranger's does not.
        fake = Fake()

        def comments(loop, number):
            if number == 7:
                return [comment("boss", "PR-DEFERRAL split into #658"),
                        comment("stranger", "PR-STRANGER-TEXT")], ""
            return [], ""
        row = {"seat": "reviewer", "repo": "acme/w", "pr": 7, "head": "a" * 40}
        loop = {**LOOP, "cap": 3, "seats": {}}
        with mock.patch.object(gh, "api", side_effect=fake.api), \
                mock.patch.object(gh, "issue_comments_read", side_effect=comments), \
                mock.patch("diaktoros.gate.verdicts", return_value=[{}]), \
                mock.patch("diaktoros.gate.latest_effective_review_at_head", return_value=None), \
                mock.patch("diaktoros.gh.pr_url", return_value="https://github.com/acme/w/pull/7"), \
                mock.patch("diaktoros.ci.read", return_value=None), \
                mock.patch("diaktoros.ci.section", return_value=""):
            text = turn_prompts.isolated_prompt(loop, row, [], change=pr_facts.PRChange("RECORD", "DIFF"))
        self.assertIn("PR-DEFERRAL", text)
        self.assertIn("Maintainers' comments on the PR #7", text)
        self.assertNotIn("PR-STRANGER-TEXT", text)


if __name__ == "__main__":
    unittest.main()
