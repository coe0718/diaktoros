"""The reply wake's guards (#2019, #2021, #2022), on ``test_fix_reply``'s ledger fixture.

#2021/#2022: a maintainer's reply never queues a fix of an issue an open PR already says it
fixes, the guard ``fix_hold.hand_off`` and ``fix_hold._release`` apply. #2019: an issue whose
GitHub read fails on one sweep keeps its "waits for a maintainer's reply" line.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fix_reply as replies  # noqa: E402

from diaktoros import fix_hold, fix_reply, gh  # noqa: E402
from diaktoros import run_supervisor  # noqa: E402
from diaktoros.run_supervisor import issue_inputs  # noqa: E402

BASE = replies.BASE


def offline(loop):
    return None, "offline"


def cutoff(seconds):
    """The clock ``seconds`` after the ledger drops the fixer's comment (its WAIT_DAYS)."""
    at = replies.TURN + 60 + fix_reply.WAIT_DAYS * 86400 + seconds    # comment_turn's created
    return mock.patch.object(fix_reply, "time", SimpleNamespace(time=lambda: at))


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


class ListingOutage(replies.Replies):
    """Vex on #2549: a reply seen while the open PR listing is unreadable is kept, past its
    comment's WAIT_DAYS window, and status names the listing, not a missing reply."""
    offline = staticmethod(offline)

    def at(self, days):
        return mock.patch.object(fix_reply, "time", SimpleNamespace(time=lambda: time.time() + days * 86400))

    def outage_across_the_cutoff(self, *failing):
        """Vex on #2549 (F1, F3): the listing is down a minute before the comment's cutoff, then
        each of ``failing`` fails a sweep after it; the reply still queues once reads recover."""
        self.reply(201, "jeremy")
        with cutoff(-60), mock.patch.object(gh, "open_prs_read", self.offline):
            self.sweep()
        for seconds, patch in enumerate(failing, 1):
            with cutoff(seconds * 60), patch:
                self.sweep()
                self.assertEqual(fix_reply.status_lines(self.st), [fix_reply.guarded_line(12)])
        with cutoff(len(failing) * 60 + 60):
            self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])
        self.assertEqual(fix_reply.status_lines(self.st), [])

    def test_a_reply_seen_just_before_its_comments_cutoff_is_queued_just_after(self):
        self.outage_across_the_cutoff()

    def test_a_failed_issue_or_comments_read_after_the_cutoff_keeps_the_reply(self):
        self.live = {**self.live, "updated_at": replies.iso(replies.TURN + 700)}
        self.outage_across_the_cutoff(
            mock.patch.object(issue_inputs, "issue_fix_issue",
                              mock.Mock(side_effect=run_supervisor.RetryableError("502"))),
            mock.patch.object(gh, "issue_comments_read", lambda loop, n: (None, "502")))

    def test_status_says_the_listing_not_a_reply_is_awaited(self):
        self.reply(201, "jeremy")
        with mock.patch.object(gh, "open_prs_read", self.offline):
            self.sweep()
            self.sweep()
        self.assertEqual(fix_reply.status_lines(self.st), [fix_reply.guarded_line(12)])
        self.assertEqual(fix_hold.explain_lines(self.st, 12), [fix_reply.guarded_line(12)])

    def test_a_listing_down_for_another_window_ends_the_retry_and_says_so(self):
        self.reply(201, "jeremy")
        with mock.patch.object(gh, "open_prs_read", self.offline):
            self.sweep()
            with self.at(fix_reply.WAIT_DAYS + 1):
                lines = self.sweep()
        self.assertTrue(any("stayed unreadable" in x for x in lines or []), lines)
        self.assertEqual((self.enqueued, fix_reply.status_lines(self.st)), ([], []))


class WebhookSettlesTheGuard(replies.Delivery):
    def test_a_delivery_that_reads_the_listing_clears_what_status_says(self):
        # Vex on #2549 (F4): the webhook's wake queued the fix, status still named the listing,
        # then (from a sweep before the reply) the reply itself.
        self.sweep()
        self.assertEqual(fix_reply.status_lines(self.st), [fix_reply.line(12)])
        self.reply(201, "jeremy")
        with mock.patch.object(gh, "open_prs_read", offline):
            self.assertNotEqual(self.deliver(self.comment())[0], "accepted")
        self.assertEqual(fix_reply.status_lines(self.st), [fix_reply.guarded_line(12)])
        decision, reason = self.deliver(self.comment())
        self.assertEqual(decision, "accepted", reason)
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])
        self.assertEqual(fix_reply.status_lines(self.st), [])
        self.assertEqual(fix_hold.explain_lines(self.st, 12), [])

    def test_a_delivery_during_a_sweep_keeps_its_guard(self):
        # Vex on #2549 (F5): the sweep wrote its whole GUARD over one a delivery had just made.
        real, delivered = fix_reply.wake, []

        def wake_then_deliver(loop, st, row):
            outcome = real(loop, st, row)
            if not delivered:                   # the reply lands while the sweep is mid-way
                delivered.append(self.reply(201, "jeremy"))
                with mock.patch.object(gh, "open_prs_read", offline):
                    self.deliver(self.comment())
            return outcome
        with cutoff(-60), mock.patch.object(fix_reply, "wake", wake_then_deliver):
            self.sweep()
        self.assertEqual(fix_reply.status_lines(self.st), [fix_reply.guarded_line(12)])
        with cutoff(60):
            self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])

    def race(self, during):
        """Vex on #2549 (F5): with a guard already kept, the sweep reads the comments from before
        the reply while a delivery sees it, listing down, ``during`` the sweep's wake or after."""
        self.live = {**self.live, "updated_at": replies.iso(replies.TURN + 700)}
        self.reply(201, "jeremy")
        with cutoff(-120), mock.patch.object(gh, "open_prs_read", offline):
            self.sweep()
        read, real, delivering, done = gh.issue_comments_read, fix_reply.wake, [], []

        def deliver():
            delivering.append(done.append(1))
            with mock.patch.object(gh, "open_prs_read", offline):
                self.deliver(self.comment())
            delivering.clear()

        def stale(loop, n):                     # the sweep's read, from before the reply
            if delivering:
                return read(loop, n)
            if during and not done:
                deliver()
            return [], ""

        def wake(loop, st, row):
            outcome = real(loop, st, row)
            if not delivering and not done:
                deliver()
            return outcome
        with cutoff(-60), mock.patch.object(gh, "issue_comments_read", stale), \
                mock.patch.object(fix_reply, "wake", wake):
            self.sweep()
        self.assertEqual(fix_reply.status_lines(self.st), [fix_reply.guarded_line(12)])
        with cutoff(60):
            self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])

    def test_a_sighting_during_a_sweeps_wake_outlives_its_stale_read(self):
        self.race(during=True)

    def test_a_sighting_after_a_sweeps_wake_outlives_its_stale_read(self):
        self.race(during=False)

    def test_a_sweep_never_writes_back_a_wait_a_delivery_queued(self):
        # Vex on #2549 (F6): the sweep saw no reply, a delivery queued the fix, the sweep then
        # wrote "waits for a maintainer's reply" back.
        real = fix_reply.wake

        def wake_then_deliver(loop, st, row):
            outcome = real(loop, st, row)
            if not self.comments:
                self.reply(201, "jeremy")
                self.assertEqual(self.deliver(self.comment())[0], "accepted")
            return outcome
        with mock.patch.object(fix_reply, "wake", wake_then_deliver):
            self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])
        self.assertEqual(fix_reply.status_lines(self.st), [])
        self.assertEqual(fix_hold.explain_lines(self.st, 12), [])


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
    for cls in (OpenPrGuard, ListingOutage, WebhookSettlesTheGuard, UnreadableKeepsTheWait):
        for name in sorted(vars(cls)):
            if name.startswith("test_"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
