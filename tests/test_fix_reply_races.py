"""The reply wake's sweep and a webhook delivery deciding about one issue at once (#2549 review).

Both run ``fix_reply.wake`` from GitHub reads that take a while, in different processes; the
newest reads decide (``fix_reply_state``). Each test interleaves a real delivery
(``fix_reply.on_comment``) into a real sweep at a fixed point, on ``test_fix_reply``'s ledger.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fix_reply as replies  # noqa: E402
from test_fix_reply_guard import cutoff, offline  # noqa: E402

from diaktoros import fix_hold, fix_reply, gh  # noqa: E402

BASE = replies.BASE


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

        def wake_then_deliver(loop, st, row, *rest):
            outcome = real(loop, st, row, *rest)
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

        def wake(loop, st, row, *rest):
            outcome = real(loop, st, row, *rest)
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

        def wake_then_deliver(loop, st, row, *rest):
            outcome = real(loop, st, row, *rest)
            if not self.comments:
                self.reply(201, "jeremy")
                self.assertEqual(self.deliver(self.comment())[0], "accepted")
            return outcome
        with mock.patch.object(fix_reply, "wake", wake_then_deliver):
            self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])
        self.assertEqual(fix_reply.status_lines(self.st), [])
        self.assertEqual(fix_hold.explain_lines(self.st, 12), [])

    def during_a_sweeps_read(self, patch_name):
        """Vex on #2549 (F6, F7): a delivery reads and settles issue #12 while the sweep's wake
        is in its ``patch_name`` read; the sweep's read then answers as from before it."""
        real, before, delivering = getattr(gh, patch_name), {
            "issue_comments_read": ([], ""), "open_prs_read": (None, "offline")}[patch_name], []

        def read(*args):
            if delivering:
                return real(*args)
            delivering.append(1)
            if not self.comments:
                self.reply(201, "jeremy")
            self.deliver(self.comment())
            return before
        with mock.patch.object(gh, patch_name, read):
            self.sweep()

    def test_a_sweep_never_writes_back_a_wait_a_delivery_found_fixed(self):
        # F6: the delivery saw the reply and an open PR that fixes the issue (ineligible); the
        # sweep, from its older comments read, wrote "waits for a maintainer's reply" back.
        self.live = {**self.live, "updated_at": replies.iso(replies.TURN + 700)}
        self.open_prs = [{"number": 5, "body": "Fixes #12"}]
        self.during_a_sweeps_read("issue_comments_read")
        self.assertEqual(self.enqueued, [])
        self.assertEqual(fix_reply.status_lines(self.st), [])
        self.assertEqual(fix_hold.explain_lines(self.st, 12), [])

    def test_a_sweeps_stale_outage_never_guards_a_fix_a_delivery_queued(self):
        # F7: the delivery read the listing and queued the fix; the sweep's listing read, begun
        # earlier, failed after it and recorded the reply as waiting on the listing.
        self.live = {**self.live, "updated_at": replies.iso(replies.TURN + 700)}
        self.reply(201, "jeremy")
        self.during_a_sweeps_read("open_prs_read")
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])
        self.assertEqual(fix_reply.status_lines(self.st), [])
        self.assertEqual(fix_hold.explain_lines(self.st, 12), [])

    def after_the_sweeps_ledger_read(self, patch_name):
        """Vex on #2549 (F10): a delivery queues the fix after the sweep's ledger read returned
        its row and before the sweep's wake; the wake's ``patch_name`` read then fails or lags."""
        real, stale, delivered = self.sup.issue_fix_comment_waits, {
            "issue_comments_read": ([], ""), "open_prs_read": (None, "offline")}[patch_name], []
        live, began = getattr(gh, patch_name), []

        def ledger(*args):
            rows = real(*args)
            if not began:                       # the sweep's own read: the delivery lands now
                began.append(1)
                self.reply(201, "jeremy")
                self.assertEqual(self.deliver(self.comment())[0], "accepted")
                delivered.append(1)
            return rows
        with mock.patch.object(self.sup, "issue_fix_comment_waits", ledger), \
                mock.patch.object(gh, patch_name, lambda *a: stale if delivered else live(*a)):
            self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])
        self.assertEqual(fix_reply.status_lines(self.st), [])
        self.assertEqual(fix_hold.explain_lines(self.st, 12), [])

    def test_a_late_wake_never_guards_a_fix_a_delivery_queued_after_its_ledger_read(self):
        self.live = {**self.live, "updated_at": replies.iso(replies.TURN + 700)}
        self.after_the_sweeps_ledger_read("open_prs_read")

    def test_a_late_wake_never_restores_a_wait_a_delivery_settled_after_its_ledger_read(self):
        self.after_the_sweeps_ledger_read("issue_comments_read")

    def test_a_late_delivery_never_guards_a_fix_a_sweep_queued_after_its_ledger_read(self):
        # F10 the other way round: the sweep queues the fix after the delivery's ledger read.
        self.live = {**self.live, "updated_at": replies.iso(replies.TURN + 700)}
        self.reply(201, "jeremy")
        real, live, swept = self.sup.issue_fix_comment_waits, gh.open_prs_read, []

        def ledger(*args):
            rows = real(*args)
            if not swept:                       # the delivery's read: a sweep runs now
                swept.append(1)
                self.sweep()
                swept.append(2)
            return rows
        with mock.patch.object(self.sup, "issue_fix_comment_waits", ledger), \
                mock.patch.object(gh, "open_prs_read",
                                  lambda *a: (None, "offline") if len(swept) > 1 else live(*a)):
            self.deliver(self.comment())
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])
        self.assertEqual(fix_reply.status_lines(self.st), [])
        self.assertEqual(fix_hold.explain_lines(self.st, 12), [])

    def test_a_failed_publication_never_unversions_a_sighting_a_stale_sweep_could_erase(self):
        # Vex on #2549 (F11, F13): while a sweep is paused after its ledger read, a delivery
        # records the reply (listing down), then a second delivery's write fails. The sweep
        # resumes from comments read before the reply. Nothing the failed write left may let that
        # stale decision erase the sighting, or the reply is lost once its comment's cutoff passes.
        self.live = {**self.live, "updated_at": replies.iso(replies.TURN + 700)}
        real, comments, began = self.sup.issue_fix_comment_waits, gh.issue_comments_read, []

        def disk_error(st, path, data):         # every process's state object: the webhook's too
            raise OSError("transient disk error")

        def ledger(*args):
            rows = real(*args)
            if not began:                       # the sweep's own read: it pauses here
                began.append(1)
                self.reply(201, "jeremy")
                self.deliver(self.comment())    # recorded, with its stamp
                with mock.patch.object(type(self.st), "kv_set_strict", disk_error):
                    self.deliver(self.comment())    # its publication fails
                began.append(2)
            return rows

        def stale(*args):                       # the sweep's read, from before the reply
            return ([], "") if len(began) > 1 else comments(*args)
        with cutoff(-60), mock.patch.object(gh, "open_prs_read", offline), \
                mock.patch.object(gh, "issue_comments_read", stale), \
                mock.patch.object(self.sup, "issue_fix_comment_waits", ledger):
            self.sweep()
        self.assertEqual(fix_reply.status_lines(self.st), [fix_reply.guarded_line(12)])
        with cutoff(60):                        # past the comment's cutoff, listing back
            self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])
        self.assertEqual(fix_reply.status_lines(self.st), [])


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the borrowed fixtures' classes run in their own modules."""
    suite = unittest.TestSuite()
    for cls in (WebhookSettlesTheGuard,):
        for name in sorted(vars(cls)):
            if name.startswith("test_"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
