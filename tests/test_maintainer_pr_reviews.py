"""#515: a maintainer's PR review bodies and inline review comments reach the seats through the
shared reader (``issue_facts.section``), a stranger's never; the conflict and CI-fix prompts
carry the same section; every seat prompt builder that carries PR or issue text calls it."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import inspect
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import gh, issue_facts, run_supervisor  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402

LOOP = {"repo": "acme/w", "id": "l", "base": "main", "read_token": "read", "cap": 3,
        "seats": {}, "reviewers": ["rev"], "triage": {"maintainers": ["Boss"]}}


def who(login):
    return {"login": login}


def api(loop, path, **kw):
    if path.endswith("/pulls/7"):
        return {"number": 7, "body": "Fixes #5"}
    if path.endswith("/issues/5"):
        return {"number": 5, "title": "T5", "body": "BODY5"}
    return None


def issue_comments(loop, number):
    return [], ""


def reviews(loop, number, login=None):
    return [{"user": who("boss"), "state": "COMMENTED", "body": "MAINT-REVIEW-BODY",
             "submitted_at": "2026-01-02T00:00:00Z"},
            {"user": who("stranger"), "state": "COMMENTED", "body": "STRANGER-REVIEW",
             "submitted_at": "2026-01-02T00:00:00Z"}], ""


def inline(loop, number):
    return [{"user": who("boss"), "path": "a.py", "line": 3, "body": "MAINT-INLINE",
             "created_at": "2026-01-03T00:00:00Z"},
            {"user": who("stranger"), "path": "a.py", "line": 4, "body": "STRANGER-INLINE",
             "created_at": "2026-01-03T00:00:00Z"}], ""


class Base(unittest.TestCase):
    def setUp(self):
        for name, fn in (("api", api), ("issue_comments_read", issue_comments),
                         ("reviews_read", reviews), ("review_comments_read", inline),
                         ("pr_url", lambda loop, n: "https://github.com/acme/w/pull/7")):
            patch = mock.patch.object(gh, name, side_effect=fn)
            patch.start()
            self.addCleanup(patch.stop)

    def assert_maintainers_only(self, text):
        self.assertIn("MAINT-REVIEW-BODY", text)
        self.assertIn("MAINT-INLINE", text)
        self.assertIn("`a.py` line 3", text)
        self.assertNotIn("STRANGER-REVIEW", text)
        self.assertNotIn("STRANGER-INLINE", text)


class ReviewBodies(Base):
    def test_section_shows_maintainer_review_bodies_and_inline_comments_only(self):
        self.assert_maintainers_only(issue_facts.section(LOOP, 7))

    def test_conflict_prompt_carries_the_maintainer_section(self):
        row = {"turn_key": run_supervisor.CONFLICT_KEY + "b" * 40, "repo": "acme/w", "pr": 7,
               "head": "a" * 40}
        self.assert_maintainers_only(turn_prompts.conflict_prompt(LOOP, row, {"conflicted": ["x.py"]}))

    def test_ci_fix_prompt_carries_the_maintainer_section(self):
        row = {"repo": "acme/w", "pr": 7, "head": "a" * 40}
        with mock.patch("diaktoros.ci_fix.section", return_value="\nJOBS"):
            text = turn_prompts.ci_fix_prompt(LOOP, row, None, ["test"])
        self.assert_maintainers_only(text)
        self.assertTrue(text.endswith("JOBS"))


class EveryBuilderCallsTheReader(unittest.TestCase):
    # Every seat prompt builder that carries PR or issue text. A new one belongs here.
    BUILDERS = (turn_prompts.isolated_prompt, turn_prompts.conflict_prompt,
                turn_prompts.ci_fix_prompt, turn_prompts.issue_fix_prompt)

    def test_each_builder_calls_the_shared_reader(self):
        for fn in self.BUILDERS:
            with self.subTest(fn.__name__):
                src = inspect.getsource(fn)
                self.assertTrue("issue_facts.section(" in src
                                or "issue_facts.comments_section(" in src)

    def test_no_seat_prompt_is_rendered_outside_a_listed_builder(self):
        src = inspect.getsource(turn_prompts)
        names = {fn.__name__ for fn in self.BUILDERS} | {"triage_prompt"}
        for chunk in src.split("\ndef ")[1:]:
            if "render_isolated(" in chunk:
                self.assertIn(chunk.split("(", 1)[0], names)
        turns = (Path(run_supervisor.__file__).parent / "turns.py").read_text()
        self.assertNotIn("render_isolated(", turns)


if __name__ == "__main__":
    unittest.main()
