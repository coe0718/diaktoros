"""Every fix comes with a test that fails without it, and the reviewer blocks when it is missing.

On #427 a P1 crash window was fixed "verified by reading": reverting the fix would have passed
every test. The seats had only been told to *run* the tests a change touches. These pin the rule
into each isolated prompt that writes code, and into the reviewer's list of what blocks. The
route prompts (``REVIEWER``/``FIXER``) are a route's identity and are deliberately left alone.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import prompts  # noqa: E402


def flat(text: str) -> str:
    return " ".join(text.split())


class FixNeedsATest(unittest.TestCase):
    def test_the_fixer_must_prove_the_finding_with_a_test(self):
        text = flat(prompts.ISOLATED_FIXER)
        self.assertIn("Every fix comes with a test that fails without it and passes with it", text)
        self.assertIn("reproduces exactly what the finding describes", text)
        self.assertIn('A fix "verified by reading" with no such test is unfinished', text)
        self.assertIn("cannot be tested in this repository, say why", text)

    def test_an_issue_fix_and_a_ci_fix_add_one_too(self):
        self.assertIn("Add a test that fails without your change and passes with it",
                      flat(prompts.ISOLATED_ISSUE_FIX))
        self.assertIn("add a test that fails without your fix", flat(prompts.ISOLATED_CI_FIX))

    def test_the_reviewer_blocks_a_fix_with_no_test(self):
        text = flat(prompts.ISOLATED_REVIEWER)
        blocks = text[text.index("**blocks** —"):text.index("**issue** —")]
        self.assertIn("a fix or a change in behavior with no test that would fail without it", blocks)
        self.assertIn("docs, wording and comment-only changes are exempt", blocks)
        self.assertIn("check that a test now reproduces that finding", blocks)

    def test_a_justified_untestable_fix_is_weighed_not_blocked(self):
        # #435: the fixer may explain why a defect cannot be tested; the reviewer must weigh
        # that reason rather than block on the missing test regardless.
        text = flat(prompts.ISOLATED_REVIEWER)
        blocks = text[text.index("**blocks** —"):text.index("**issue** —")]
        self.assertIn("a fixer who explains *why* the defect cannot be tested", blocks)
        self.assertIn("when it holds up, accept it and say so instead of blocking", blocks)
        self.assertIn("a reason that does not hold up blocks", blocks)

    def test_route_prompts_are_untouched(self):
        for template in (prompts.REVIEWER, prompts.FIXER):
            self.assertNotIn("fails without", template)


if __name__ == "__main__":
    unittest.main()
