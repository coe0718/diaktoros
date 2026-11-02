"""#1649: a seat's prompt puts what is stable for a PR first, so the provider's prompt cache can
reuse it across rounds; what changes with the round or the head comes after, newest last."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest
from unittest import mock

from diaktoros import ci, gh, issue_facts, prompts
from diaktoros.run_supervisor import pr_facts, turn_prompts

REPO = "acme/widgets"
H1, H2 = "a" * 40, "b" * 40
LOOP = {"id": "w", "repo": REPO, "base": "main", "cap": 3, "read_token": "read",
        "fixers": ["fix"], "reviewers": ["review"], "reviewer_seat": "review",
        "seats": {"reviewer": {"login": "review", "agent": "Rex"},
                  "fixer": {"login": "fix", "agent": "Dee"}}}


def prompt(seat, head, ci_state, record):
    row = {"seat": seat, "repo": REPO, "pr": 7, "head": head}
    change = pr_facts.PRChange(f"## The change under review\nhead {head}", "")
    with mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
            mock.patch.object(ci, "read", return_value=ci_state), \
            mock.patch.object(issue_facts, "section", return_value="\n\n## Issue #1 closed\nbody"), \
            mock.patch.object(turn_prompts.pr_facts, "pr_record", return_value=record), \
            mock.patch.object(turn_prompts, "reproduction_section", return_value=""), \
            mock.patch("diaktoros.ci_tests.read", return_value=None), \
            mock.patch("diaktoros.ci_tests.section", return_value=""), \
            mock.patch("diaktoros.ci_fix.fix_history", return_value=""), \
            mock.patch("diaktoros.ci_fix.still_red", return_value=""):
        return turn_prompts.isolated_prompt(LOOP, row, [], None, change)


class StableFirst(unittest.TestCase):
    def test_prefix_is_identical_across_rounds_and_order_is_pinned(self):
        for seat in ("reviewer", "fixer"):
            with self.subTest(seat):
                one = prompt(seat, H1, ci.CIState(passed=["t"]), "RECORD one")
                two = prompt(seat, H2, ci.CIState(failed=["t"]), "RECORD one two")
                cut = one.index(turn_prompts.THIS_ROUND)
                self.assertEqual(one[:cut], two[:cut])
                self.assertNotIn(H1, one[:cut])
                self.assertNotIn(H2, two[:cut])
                self.assertLess(one.index("## Issue #1 closed"), cut)
                order = [one.index(s) for s in (
                    "## Issue #1 closed", turn_prompts.THIS_ROUND, "## CI at this head",
                    "## The change under review", "## PR record")]
                self.assertEqual(order, sorted(order))
                self.assertTrue(one.rstrip().endswith("RECORD one"))

    def test_templates_carry_no_round_fact(self):
        for role in ("reviewer", "fixer"):
            self.assertNotRegex(prompts.ISOLATED[role], r"\{(head|round|cap|reviewer)\}")


if __name__ == "__main__":
    unittest.main()
