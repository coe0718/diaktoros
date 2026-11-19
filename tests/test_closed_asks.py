"""A closed issue clears the fixer's held ask (#2051, #2245).

A fixer's question or plan (#483) is held until a maintainer's reply resumes it. Once its issue
closes nothing can resume it, so the watchdog's ``fix_hold.sweep_asks`` drops it before reading
its comments, the triage gate's ``closed`` delivery drops it at once (the live issue decides,
#679), and a resume that finds the issue no longer eligible drops it instead of retrying it
every sweep. An open issue's ask, and a resume that only could not read GitHub, stay as they are.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_issue_fix as base  # noqa: E402
BASE, REPO, issue = base.BASE, base.REPO, base.issue

from diaktoros import fix_hold, fix_reply, gh, run_supervisor  # noqa: E402
from diaktoros import state as state_mod  # noqa: E402


class ClosedAsks(base.Gate):
    """Through the real ``issue_inputs.issue_fix_issue``: ``gh.api`` answers ``self.live``."""

    def setUp(self):
        super().setUp()
        self.st = state_mod.state_for(self.loop)
        self.st.fix_ask_set(12, "question", 100, "Which API shape?")
        self.comments, self.reads = [], 0

        def comments(loop, number):
            self.reads += 1
            return self.comments, ""
        patch = mock.patch.object(gh, "issue_comments_read", comments)
        patch.start()
        self.addCleanup(patch.stop)

    def reply(self, cid=101, who="Jeremy", body="use v2"):
        self.comments.append({"id": cid, "user": {"login": who}, "body": body,
                              "created_at": "2099-01-01T00:00:00Z"})

    def sweep(self) -> list[str]:
        lines: list[str] = []
        fix_hold.sweep_asks(self.loop, self.st, lines)
        return lines

    def held(self) -> list[str]:
        return [line for line in fix_hold.status_lines(self.loop, self.st)
                if "waits for a maintainer" in line]

    def closed(self, number=12):
        return {"action": "closed", "issue": issue(number=number, state="closed"),
                "sender": {"login": "jeremy"}, "repository": {"full_name": REPO}}

    # -- #2051 / #2245: the sweep --------------------------------------------------------------

    def test_an_unanswered_ask_on_a_closed_issue_is_dropped_and_no_longer_listed(self):
        self.assertEqual(len(self.held()), 1)
        self.live = issue(state="closed")
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.st.fix_ask_open(), {})
        self.assertEqual(self.held(), [])
        self.sweep()
        self.assertEqual((self.reads, self.enqueued), (0, []))   # no comments read for it

    def test_an_answered_ask_on_a_closed_issue_is_dropped_in_one_sweep_without_a_warning(self):
        self.reply()
        self.live = issue(state="closed")
        self.assertEqual(self.sweep(), [])                         # no ⚠️ line
        self.assertEqual((self.st.fix_ask_open(), self.enqueued), ({}, []))
        self.sweep()
        self.assertEqual(self.reads, 0)                            # never re-read

    def test_a_pending_plan_on_a_closed_issue_is_dropped_too(self):
        self.st.fix_ask_drop(12)
        self.st.fix_ask_claim(12, "plan", "the plan", "run-1", fix_hold.check_ask)
        self.live = issue(state="closed")
        self.sweep()
        self.assertEqual(self.st.fix_ask_open(), {})

    def test_a_resume_that_finds_the_issue_ineligible_drops_the_ask(self):
        self.reply()
        self.live = issue(labels=())                  # open, but the fix label came off
        self.assertEqual(self.sweep(), [])
        self.assertEqual((self.st.fix_ask_open(), self.enqueued), ({}, []))
        self.sweep()
        self.assertEqual(self.reads, 1)               # dropped: a later sweep reads nothing

    def test_a_retryable_error_on_resume_keeps_the_ask_for_the_next_sweep(self):
        self.reply()
        with mock.patch("diaktoros.run_supervisor.issue_inputs.issue_fix_issue",
                        side_effect=run_supervisor.RetryableError("issue unreadable")):
            lines = self.sweep()
        self.assertEqual(len(lines), 1)
        self.assertIn("reply seen, fix not queued", lines[0])
        self.assertIn(12, self.st.fix_ask_open())
        self.sweep()                                  # GitHub is back: resumed
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:ask1")])
        self.assertEqual(self.st.fix_ask_open(), {})

    def test_an_open_issues_ask_is_untouched(self):
        self.sweep()
        self.assertIn(12, self.st.fix_ask_open())
        self.assertEqual((self.reads, len(self.held())), (1, 1))

    def test_an_unreadable_issue_keeps_the_ask(self):
        self.live = None
        self.sweep()
        self.assertIn(12, self.st.fix_ask_open())
        self.assertEqual(self.reads, 0)

    # -- #2051: the triage gate's closed delivery ----------------------------------------------

    def test_the_gates_closed_delivery_drops_the_ask(self):
        self.st.fix_ask_set(13, "question", 100, "another issue's")
        self.live = issue(state="closed")
        self.run_gate(self.closed())
        self.assertEqual(sorted(self.st.fix_ask_open()), [13])    # only the closed issue's

    def test_a_closed_delivery_for_a_live_open_issue_keeps_the_ask(self):
        self.run_gate(self.closed())                  # the payload says closed; GitHub says open
        self.assertIn(12, self.st.fix_ask_open())

    # -- #2245: a maintainer's comment on a closed issue's ask -------------------------------

    def test_a_comment_on_a_closed_issues_ask_drops_it_and_queues_nothing(self):
        from diaktoros import util
        self.loop = {**self.loop, "comment_webhook": True}
        self.reply(201)
        self.live = issue(state="closed")
        payload = {"action": "created", "comment": {"id": 201, "user": {"login": "Jeremy"}},
                   "issue": issue(state="closed"), "sender": {"login": "Jeremy"},
                   "repository": {"full_name": REPO}}
        recorded = []
        with mock.patch.object(util, "_DECISION_RECORDER",
                               lambda reason, decision: recorded.append((decision, reason))), \
                self.assertRaises(SystemExit):
            fix_reply.on_comment(self.loop, payload)
        decision, reason = recorded[-1]
        self.assertEqual(decision, "declined", reason)
        self.assertIn("ask dropped: the issue is closed", reason)
        self.assertEqual((self.st.fix_ask_open(), self.enqueued), ({}, []))


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the borrowed fixtures' classes run in test_issue_fix."""
    suite = unittest.TestSuite()
    for name in sorted(vars(ClosedAsks)):
        if name.startswith("test_"):
            suite.addTest(ClosedAsks(name))
    return suite


if __name__ == "__main__":
    unittest.main()
