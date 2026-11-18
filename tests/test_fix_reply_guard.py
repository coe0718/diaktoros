"""The reply wake's guards (#2019, #2021, #2022), on ``test_fix_reply``'s ledger fixture.

#2021/#2022: a maintainer's reply never queues a fix of an issue an open PR already says it
fixes, the guard ``fix_hold.hand_off`` and ``fix_hold._release`` apply. #2019: an issue whose
GitHub read fails on one sweep keeps its "waits for a maintainer's reply" line.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fix_reply as replies  # noqa: E402

from diaktoros import fix_hold, fix_reply, gh  # noqa: E402
from diaktoros import run_supervisor  # noqa: E402
from diaktoros.run_supervisor import issue_inputs  # noqa: E402

BASE = replies.BASE


class OpenPrGuard(replies.Replies):
    def test_an_open_pr_that_fixes_the_issue_stops_the_reply_wake(self):
        # #2021, #2022: the wake queued a second fix turn beside the open fixing PR.
        self.open_prs = [{"number": 3, "body": "Fixes #120"}, {"number": 5, "body": "fixes #12"}]
        self.reply(201, "jeremy")
        self.sweep()
        self.assertEqual((self.enqueued, self.notices), ([], []))
        self.assertNotIn(fix_reply.line(12), fix_hold.status_lines(self.loop, self.st))

    def test_an_unreadable_pr_listing_queues_nothing_and_the_next_sweep_retries(self):
        self.reply(201, "jeremy")
        with mock.patch.object(gh, "open_prs_read", lambda loop: (None, "offline")):
            self.sweep()
        self.assertEqual(self.enqueued, [])
        self.open_prs = [{"number": 5, "body": "see #12"}, {"number": 6, "body": None}]
        self.sweep()                                   # a PR that only mentions it is no fix
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])


class UnreadableKeepsTheWait(replies.Replies):
    def test_an_unreadable_issue_or_comments_read_keeps_the_waiting_line(self):
        # #2019: ``sweep`` kept only ``waiting`` outcomes, so a failed read dropped the line.
        unreadable = mock.Mock(side_effect=run_supervisor.RetryableError("502"))
        with mock.patch.object(issue_inputs, "issue_fix_issue", unreadable):
            self.sweep()                               # unknown never adds a wait either
        self.assertEqual(fix_reply.status_lines(self.st), [])
        self.sweep()
        self.assertIn(fix_reply.line(12), fix_hold.status_lines(self.loop, self.st))
        for name, patch in (
                ("issue", mock.patch.object(issue_inputs, "issue_fix_issue", unreadable)),
                ("comments", mock.patch.object(gh, "issue_comments_read",
                                               lambda loop, n: (None, "502")))):
            with self.subTest(name), patch:
                self.live = {**self.live, "updated_at": replies.iso(replies.TURN + 700)}
                self.sweep()
                self.assertIn(fix_reply.line(12), fix_hold.status_lines(self.loop, self.st))
                self.assertEqual(fix_hold.explain_lines(self.st, 12), [fix_reply.line(12)])


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the borrowed fixtures' classes run in their own modules."""
    suite = unittest.TestSuite()
    for cls in (OpenPrGuard, UnreadableKeepsTheWait):
        for name in sorted(vars(cls)):
            if name.startswith("test_"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
