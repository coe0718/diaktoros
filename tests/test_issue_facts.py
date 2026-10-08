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
            text = run_supervisor.issue_fix_prompt(LOOP, {"repo": "acme/w", "pr": 5, "head": "c" * 40})
        self.assertIn("MAINTAINER-REQ", text)
        self.assertNotIn("STRANGER-TEXT", text)
        self.assertLess(text.index("BODY5"), text.index("MAINTAINER-REQ"))


if __name__ == "__main__":
    unittest.main()
