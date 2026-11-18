"""The reply wake's guards (#2021, #2022), on ``test_fix_reply``'s ledger fixture.

#2021/#2022: a maintainer's reply never queues a fix of an issue an open PR already says it
fixes, the guard ``fix_hold.hand_off`` and ``fix_hold._release`` apply.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fix_reply as replies  # noqa: E402

from diaktoros import fix_hold, fix_reply, gh  # noqa: E402

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


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the borrowed fixtures' classes run in their own modules."""
    suite = unittest.TestSuite()
    for cls in (OpenPrGuard,):
        for name in sorted(vars(cls)):
            if name.startswith("test_"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
