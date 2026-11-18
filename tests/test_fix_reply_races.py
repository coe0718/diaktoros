"""The reply wake's sweep and a webhook delivery deciding about one issue at once (#2549 review).

Both run ``fix_reply.wake`` from GitHub reads that take a while, in different processes; the
newest reads decide (``fix_reply_state``). Each test interleaves a real delivery
(``fix_reply.on_comment``) into a real sweep at a fixed point, on ``test_fix_reply``'s ledger.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
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


class LedgerReadFails(replies.Replies):
    def test_a_ledger_read_failing_mid_sweep_never_stops_the_other_holds(self):
        # Vex on #2549 (F8): a ledger read after the first raised out of ``fix_hold.sweep``.
        self.reply(201, "jeremy")
        with mock.patch.object(gh, "open_prs_read", offline):
            self.sweep()                        # guarded: the next sweep reads the ledger twice
        real = self.sup.issue_fix_comment_waits
        for failing in (1, 2, 3):           # 3: the old head read it again after the wakes
            calls = []

            def flaky(*args):
                calls.append(1)
                if len(calls) == failing:
                    raise RuntimeError("ledger unavailable")
                return real(*args)
            with self.subTest(failing=failing), mock.patch.object(gh, "open_prs_read", offline), \
                    mock.patch.object(self.sup, "issue_fix_comment_waits", flaky):
                self.sweep()                    # raises nothing
                self.assertEqual(fix_reply.status_lines(self.st), [fix_reply.guarded_line(12)])
        self.assertEqual(self.enqueued, [])

    def test_a_spent_window_never_drops_a_sighting_made_after_the_sweep_read_it(self):
        # The 14-day cutoff drops only the sighting it found spent, not a fresh one made since.
        self.reply(201, "jeremy")
        with mock.patch.object(gh, "open_prs_read", offline):
            self.sweep()
        spent, fresh, real = fix_reply.kept.guard(self.st)["12"], {"at": time.time()}, fix_reply.kept.guard
        reads = []

        def resighted(st):              # a delivery re-sights it just after the sweep's wake read
            reads.append(1)
            found = real(st)
            if len(reads) == 2:
                st.kv_set(st.dir / fix_reply.GUARD, {"12": fresh})
            return found
        later = SimpleNamespace(time=lambda: spent["at"] + (fix_reply.WAIT_DAYS + 1) * 86400)
        with mock.patch.object(gh, "open_prs_read", offline), \
                mock.patch.object(fix_reply, "time", later), \
                mock.patch.object(fix_reply.kept, "guard", resighted):
            self.sweep()
        self.assertEqual(fix_reply.kept.guard(self.st), {"12": fresh})


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the borrowed fixtures' classes run in their own modules."""
    suite = unittest.TestSuite()
    for cls in (WebhookSettlesTheGuard, LedgerReadFails):
        for name in sorted(vars(cls)):
            if name.startswith("test_"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
