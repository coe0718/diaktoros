"""#754: a blocking finding that asks for the PR's own work escalates for a ruling.

When a changes-requested verdict's blocking finding (an ``F<n>`` line of the review's own body)
names, as a literal same-repo ``#N``, the PR itself or an issue it closes (``Closes #N`` in the
webhook's PR body, or the ``diaktoros/issue-N`` head branch), the fixer gate parks the head for
a ruling through ``gate.breach`` instead of queueing a fix round. Anything else (an unrelated
number, ``#N`` inside a longer number, ``other/repo#N``) falls back to the fixer.
Driven through ``scripts/gate_fixer.main``; GitHub is mocked, state is real.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import time
import unittest
from unittest import mock

import test_escalate as te
import test_fixer_gating as fg  # noqa: E402
from diaktoros import explain, findings, gate, issue_facts  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402

HEAD = fg.HEAD


def changes(body: str, rid: int = 6) -> dict:
    return te.verdict(rid) | {"state": "changes_requested", "body": body}


class Cycle(te.Base):
    def pr(self, body: str = "", ref: str = "fix-7") -> dict:
        return {**fg.LIVE, "body": body, "head": {**fg.LIVE["head"], "ref": ref}}

    def gate(self, finding: str, pr: dict):
        """One changes-requested verdict (the first of the cap) through the fixer gate."""
        self.reviews = [te.verdict(6)]
        return self.fixer_gate(changes(finding, 6), self.reviews, pr=pr, real_breach=True)

    def assert_escalated(self, finding: str, pr: dict, cited: str):
        err, enqueue, breach, _ = self.gate(finding, pr)
        enqueue.assert_not_called()                                  # no fix round
        breach.assert_called_once()
        marker = self.st.breach_get(7)
        self.assertEqual(marker["head"], HEAD)
        self.assertEqual(marker["escalated"]["by"], gate.CYCLE_ESCALATION)
        self.assertIn(cited, marker["reason"])
        self.assertTrue(gate.escalated(marker, HEAD))
        self.assertIn("handed to adjudication instead of a fix", err)
        return marker

    def assert_to_the_fixer(self, finding: str, pr: dict):
        err, enqueue, breach, _ = self.gate(finding, pr)
        breach.assert_not_called()
        enqueue.assert_called_once()                                 # today's behaviour
        self.assertEqual(self.st.breach_get(7), {})

    # -- the fixtures of #660 and #685, and the branch case -----------------------------------

    def test_660_a_finding_naming_the_prs_own_closing_issue_escalates(self):
        self.assert_escalated("Summary.\n\nF1 [requirement]: blocked until #12 is done\n",
                              self.pr("Unblocks the widget.\n\nCloses #12"), "#12")

    def test_685_a_finding_that_needs_this_pr_first_escalates(self):
        self.assert_escalated("F1: needs a PR first: #7 must land before this can\n",
                              self.pr("Fixes #30"), "#7")

    def test_the_issue_of_a_diaktoros_issue_branch_counts(self):
        self.assert_escalated("F1: src/a.py:3: the work of #42 is still missing\n",
                              self.pr("no closing line", ref="diaktoros/issue-42"), "#42")

    # -- negatives: each one still goes to the fixer ------------------------------------------

    def test_an_unrelated_issue_number_goes_to_the_fixer(self):
        self.assert_to_the_fixer("F1: src/a.py:3: same bug as #99\n", self.pr("Closes #12"))

    def test_a_number_inside_a_longer_number_goes_to_the_fixer(self):
        self.assert_to_the_fixer("F1: src/a.py:3: see #120 and #77\n", self.pr("Closes #12"))

    def test_another_repositorys_reference_goes_to_the_fixer(self):
        self.assert_to_the_fixer("F1: src/a.py:3: needs other/repo#12 and x/y#7 first\n",
                                 self.pr("Closes #12"))

    def test_a_mention_outside_the_blocking_findings_goes_to_the_fixer(self):
        # The Requirements section restates the closing issue on every review of such a PR.
        self.assert_to_the_fixer("## Requirements\n- #12 item: not met\n\nF1: src/a.py:3: off by one\n",
                                 self.pr("Closes #12"))

    def test_2548_a_recorded_findings_state_line_naming_the_closing_issue_goes_to_the_fixer(self):
        # A state line with a plain-space reason is a state of a recorded id, as the broker reads
        # it, not a new finding: the real new finding goes to the fixer.
        for state in ("F2: fixed in #12", "F2: withdrawn pending #12"):
            with self.subTest(state):
                self.st.findings_put(7, {"findings": {"F2": {"state": "open", "text": "x"}}})
                self.assert_to_the_fixer(f"F1 [correctness]: src/a.py:1: real problem\n{state}\n",
                                         self.pr("Closes #12"))

    def test_2573_a_new_finding_the_broker_already_recorded_still_escalates(self):
        # The broker records a review before the gate reads it: F1's own text must not make its
        # ``fixed-size`` read as the state ``fixed`` (review on #2573).
        review = "F1 [correctness]: fixed-size buffer breaks #12\nF2: fixed in #12\n"
        earlier = {"findings": {"F2": {"state": "open", "text": "src/a.py:1: x"}}}
        self.st.findings_put(7, findings.apply(earlier, HEAD, review))
        self.assertEqual(self.st.findings_get(7)["findings"]["F1"]["state"], "open")
        self.assert_escalated(review, self.pr("Closes #12"), "#12")

    # -- once per head, and explained --------------------------------------------------------

    def test_it_escalates_once_per_head(self):
        finding = "F1: needs #7 first\n"
        self.assert_escalated(finding, self.pr(), "#7")
        err, enqueue, breach, _ = self.gate(finding, self.pr())    # redelivered verdict
        breach.assert_not_called()
        enqueue.assert_not_called()
        self.assertIn("escalated to adjudication", err)

    def test_explain_gives_the_reason(self):
        self.assert_escalated("F1: blocked until #12 is done\n", self.pr("Closes #12"), "#12")
        facts = {"pr": self.live, "reviews": self.reviews, "armed": True, "read_at": time.time()}
        with mock.patch.object(explain, "_ruled", return_value=None):
            report = explain.explain(self.loop, self.st, 7, facts)
        self.assertIn("a blocking finding names #12", report["escalation"])
        self.assertEqual(report["next"]["kind"], "adjudication", report["next"])
        self.assertIn("a blocking finding asks for the PR's own work", report["next"]["action"])
        self.assertNotIn("operator's escalation", report["next"]["action"])

    def test_the_ruling_prompt_names_the_cycle(self):
        marker = self.assert_escalated("F1: blocked until #12\n", self.pr("Closes #12"), "#12")
        self.assertIn("names this PR itself or an issue it closes",
                      turn_prompts.adjudication_situation(marker))
        self.assertIn("#12", turn_prompts.adjudication_reason(marker))


class Matcher(unittest.TestCase):
    loop = {"repo": "o/r"}

    def cited(self, review: str, body: str = "Closes #12", ref: str = "fix") -> list[int]:
        return issue_facts.cited_own_work(self.loop, 7, body, ref, review)

    def test_literal_same_repo_numbers_only(self):
        self.assertEqual(self.cited("F1: #12 and #7 and #99"), [12, 7])
        self.assertEqual(self.cited("F1: #123 #1200 o/r#12 x#12"), [])
        self.assertEqual(self.cited("F1: (#12)."), [12])

    def test_open_findings_count_and_settled_ones_do_not(self):
        self.assertEqual(self.cited("F1: open — still needs #12", ), [12])
        self.assertEqual(self.cited("F1: fixed — #12 landed"), [])
        self.assertEqual(self.cited("F1: withdrawn — #12"), [])

    def test_2548_known_ids_read_state_lines_as_states(self):
        known = {"F2": {"state": "open"}}
        review = "F1: src/a.py:1: real problem\nF2: fixed in #12\n"
        self.assertEqual(issue_facts.cited_own_work(self.loop, 7, "Closes #12", "fix", review,
                                                    known), [])
        self.assertEqual(issue_facts.cited_own_work(self.loop, 7, "Closes #12", "fix",
                                                    "F2: open until #12\n", known), [12])

    def test_2573_recorded_before_drops_only_this_reviews_own_new_ids(self):
        review = "F1: fixed-size buffer\nF2: fixed in #12\n"
        entry = findings.apply({"findings": {"F2": {"state": "open", "text": "x"}}}, "h", review)
        self.assertEqual(sorted(findings.recorded_before(entry, review)), ["F2"])
        self.assertEqual(sorted(findings.recorded_before(entry, "F1: fixed\n")), ["F1", "F2"])
        self.assertEqual(findings.recorded_before({}, None), {})

    def test_unreadable_inputs_read_as_nothing(self):
        self.assertEqual(issue_facts.cited_own_work(self.loop, 7, None, None, None), [])
        self.assertEqual(self.cited("F1: #42", body="", ref="diaktoros/issue-42"), [42])
        self.assertEqual(self.cited("F1: #42", body="", ref="feature/issue-42"), [])


if __name__ == "__main__":
    unittest.main()
