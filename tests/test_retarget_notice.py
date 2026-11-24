"""A same-head retarget whose fresh review was queued is routine progress, not a stall (#2153).

After a parent merges, GitHub retargets its stacked child to the base at the same head; the
watchdog quarantines the old reviews and enqueues one fresh review (#23). When that enqueue
worked, the notice is a routine ``retargeted``, not an urgent "stalled". Only an enqueue that must
retry next sweep stays ``stall`` (urgent). The notice identity (``retarget:{head}``) is unchanged.
Driven through the real sweep, as ``test_fresh_review_after_retarget`` drives it.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import types
import unittest
from unittest import mock

from diaktoros import observer, watch_stacked
from tests import test_fresh_review_after_retarget as fresh  # its class, not its tests
from tests.test_stacked_situation import C


class RetargetNotice(unittest.TestCase):
    setUp = fresh.FreshReviewTest.setUp
    fake_enqueue = fresh.FreshReviewTest.fake_enqueue
    live = fresh.FreshReviewTest.live

    def retarget(self):
        """Merge the parent and retarget the child; the retarget notices the step sent."""
        sent = []
        record = types.SimpleNamespace(notify=lambda *a, **kw: sent.append((a, kw)))
        with mock.patch.object(watch_stacked, "observer", record):
            fresh.FreshReviewTest.merge_parent_and_retarget(self)
        return [(a[2], a[4], kw) for a, kw in sent
                if str(kw.get("identity", "")).startswith("retarget:")]

    def sweep(self, listing):
        return fresh.FreshReviewTest.sweep(self, listing)

    def test_an_enqueued_fresh_review_is_a_routine_retargeted_notice(self):
        (event, head, kw), = self.retarget()
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual((event, head, kw["identity"]), ("retargeted", C, f"retarget:{C}"))
        self.assertFalse(observer.is_urgent(event, kw["outcome"]))
        self.assertIn("fresh review enqueued", kw["outcome"])
        for table in (observer.EVENTS, observer.LABEL, observer.EMOJI):
            self.assertIn("retargeted", table)
        self.assertIn("retargeted", observer.ROUTINE_EVENTS)

    def test_an_enqueue_that_must_retry_is_still_an_urgent_stall(self):
        self.enqueue_error = FileNotFoundError("diaktoros-runtime.json")
        (event, head, kw), = self.retarget()
        self.assertEqual((event, head, kw["identity"]), ("stall", C, f"retarget:{C}"))
        self.assertTrue(observer.is_urgent(event, kw["outcome"]))
        self.assertIn("fresh review retry", kw["outcome"])


if __name__ == "__main__":
    unittest.main()
