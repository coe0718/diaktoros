"""#1649: a seat's prompt puts what is stable for a PR first, so the provider's prompt cache can
reuse it across rounds; what changes with the round or the head comes after, newest last."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest
from unittest import mock

from diaktoros import ci, gh, issue_facts, prompts, trusted_turn
from diaktoros.run_supervisor import pr_facts, turn_prompts

REPO = "acme/widgets"
H1, H2 = "a" * 40, "b" * 40
LOOP = {"id": "w", "repo": REPO, "base": "main", "cap": 3, "read_token": "read",
        "fixers": ["fix"], "reviewers": ["review"], "reviewer_seat": "review",
        "seats": {"reviewer": {"login": "review", "agent": "Rex"},
                  "fixer": {"login": "fix", "agent": "Dee"}}}


def prompt(seat, head, ci_state, record, reviews=(), diff="", body="the PR body"):
    row = {"seat": seat, "repo": REPO, "pr": 7, "head": head}
    change = pr_facts.PRChange(f"## The change under review\nhead {head}", diff,
                               description=pr_facts.description_section(body))
    with mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
            mock.patch.object(ci, "read", return_value=ci_state), \
            mock.patch.object(issue_facts, "section", return_value="\n\n## Issue #1 closed\nbody"), \
            mock.patch.object(turn_prompts.pr_facts, "pr_record", return_value=record), \
            mock.patch.object(turn_prompts, "reproduction_section", return_value=""), \
            mock.patch("diaktoros.ci_tests.read", return_value=None), \
            mock.patch("diaktoros.ci_tests.section", return_value=""), \
            mock.patch("diaktoros.ci_fix.fix_history", return_value=""), \
            mock.patch("diaktoros.ci_fix.still_red", return_value=""):
        return turn_prompts.isolated_prompt(LOOP, row, list(reviews), None, change)


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

    def test_round_head_cap_and_reviewer_are_in_this_round_with_the_right_values(self):
        def verdict(n, head, who):
            return {"id": n, "user": {"login": who}, "state": "CHANGES_REQUESTED",
                    "commit_id": head, "submitted_at": f"2026-11-0{n}T00:00:00Z", "body": "x"}
        one = [verdict(1, H1, "review")]
        two = one + [verdict(2, H2, "review")]
        for seat, first, second in (("reviewer", "round **2 of 3**", "round **3 of 3**"),
                                    ("fixer", "round **1 of 3**", "round **2 of 3**")):
            with self.subTest(seat):
                a = prompt(seat, H1, ci.CIState(), "R", one if seat == "reviewer" else one)
                b = prompt(seat, H2, ci.CIState(), "R", two)
                for text, line, head in ((a, first, H1), (b, second, H2)):
                    tail = text[text.index(turn_prompts.THIS_ROUND):]
                    self.assertIn(line, tail)
                    self.assertIn(f"head **{head}**", tail)
                if seat == "fixer":
                    self.assertIn("requested by **review**", b)

    def test_fixer_registry_follows_this_round_and_the_description_precedes_it(self):
        diff = ("diff --git a/diaktoros/observer.py b/diaktoros/observer.py\n"
                "--- a/diaktoros/observer.py\n+++ b/diaktoros/observer.py\n@@ -1 +1,2 @@\n+    'waved',\n")
        text = prompt("fixer", H1, ci.CIState(), "R", diff=diff)
        order = [text.index(s) for s in ("## PR description", turn_prompts.THIS_ROUND,
                                         "## Registries (host-written checklist)",
                                         "## CI at this head")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("## Registries", prompt("reviewer", H1, ci.CIState(), "R", diff=diff))
        # The description is stable: a changed body is the only thing that moves the prefix.
        self.assertEqual(text.count("the PR body"), 1)

    def test_the_query_prefix_survives_a_changed_build_note(self):
        """F1: the host's build note goes just above "This round", not in front of the prefix."""
        for seat in ("reviewer", "fixer"):
            with self.subTest(seat):
                one = prompt(seat, H1, ci.CIState(), "R")
                two = prompt(seat, H2, ci.CIState(), "R")
                a = trusted_turn.compose_prompt(one, "Build environment: ready (3 crates)")
                b = trusted_turn.compose_prompt(two, "Build environment: NOT available")
                cut = a.index("Build environment: ")
                self.assertEqual(a[:cut], b[:cut])
                self.assertEqual(a[:cut], one[:one.index(turn_prompts.THIS_ROUND)])
                self.assertLess(cut, a.index(turn_prompts.THIS_ROUND))
        self.assertEqual(trusted_turn.compose_prompt("P", "N"), "N\n\nP")  # no section: first
        self.assertEqual(trusted_turn.compose_prompt("P", ""), "P")

    def test_a_retry_nudge_does_not_move_the_prefix(self):
        one = prompt("fixer", H1, ci.CIState(), "R")
        nudged = prompts.before_this_round(one, "NUDGE\n\n")
        cut = one.index(turn_prompts.THIS_ROUND)
        self.assertEqual(nudged[:cut], one[:cut])

    def test_templates_carry_no_round_fact(self):
        for role in ("reviewer", "fixer"):
            self.assertNotRegex(prompts.ISOLATED[role], r"\{(head|round|cap|reviewer)\}")


if __name__ == "__main__":
    unittest.main()
