"""A same-head retarget whose fresh review was queued is routine progress, not a stall (#2153).

After a parent merges, GitHub retargets its stacked child to the base at the same head; the
watchdog quarantines the old reviews and enqueues one fresh review (#23). When that enqueue
worked, the notice is a routine ``retargeted``, not an urgent "stalled". Only an enqueue that must
retry next sweep stays ``stall`` (urgent). Either way it is one notice per retarget: a sweep that
crashed before saving its listing replays the retarget, and the replay may pick the other event,
whose observer key differs (the key includes the event). The notices go through the real
observer ledger; only the POST is stubbed. Driven through the real sweep, as
``test_fresh_review_after_retarget`` drives it.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import copy
import types
import unittest
from unittest import mock

from diaktoros import observer, watch_stacked
from tests import test_fresh_review_after_retarget as fresh  # its class, not its tests
from tests.test_stacked_situation import A, C

REAL_NOTIFY = observer.notify       # the sweep fixture mocks the module's own


class RetargetNotice(unittest.TestCase):
    setUp = fresh.FreshReviewTest.setUp
    fake_enqueue = fresh.FreshReviewTest.fake_enqueue
    live = fresh.FreshReviewTest.live

    def sweep(self, listing):
        """One sweep whose stacked step notifies through the real observer ledger."""
        self.loop["observer"] = {"route": "feed"}
        real = types.SimpleNamespace(notify=REAL_NOTIFY, recorded=observer.recorded,
                                     key_for=observer.key_for)
        with mock.patch.object(watch_stacked, "observer", real), \
                mock.patch.object(observer, "_deliver", return_value=True):
            return fresh.FreshReviewTest.sweep(self, listing)

    def retarget(self):
        """Merge the parent and retarget the child; the watch as it was before the retarget."""
        self.sweep([self.parent, self.child])
        before = copy.deepcopy(self.st.watch())
        self.parent.update(state="closed", merged=True)
        self.child["base"].update(ref="main", sha=A)
        self.sweep([self.child])
        return before

    def replay(self, before):
        """The retarget's sweep again, as after a crash before it saved its listing."""
        self.st.watch_save(before)
        self.sweep([self.child])

    def notices(self):
        """The retarget notices in the observer ledger: ``(event, urgent, outcome)``."""
        entries = observer._read(self.st.observations)["entries"].values()
        return [(e["event"], e["urgent"], e["outcome"]) for e in entries
                if e.get("identity") == f"retarget:{C}"]

    def test_a_queued_fresh_review_is_routine_and_only_a_retry_is_an_urgent_stall(self):
        self.retarget()
        self.assertEqual(len(self.enqueued), 1)
        (event, urgent, outcome), = self.notices()
        self.assertEqual((event, urgent), ("retargeted", False))
        self.assertIn("fresh review enqueued", outcome)
        for table in (observer.EVENTS, observer.LABEL, observer.EMOJI):
            self.assertIn("retargeted", table)
        self.assertIn("retargeted", observer.ROUTINE_EVENTS)
        self.doCleanups()                       # the same retarget, its enqueue failing
        self.setUp()
        self.enqueue_error = FileNotFoundError("diaktoros-runtime.json")
        self.retarget()
        (event, urgent, outcome), = self.notices()
        self.assertEqual((event, urgent), ("stall", True))
        self.assertIn("fresh review retry", outcome)

    def test_a_replay_that_queues_the_retried_review_sends_no_second_notice(self):
        self.enqueue_error = FileNotFoundError("diaktoros-runtime.json")
        before = self.retarget()
        self.assertEqual([n[0] for n in self.notices()], ["stall"])
        self.enqueue_error = None
        self.replay(before)                     # picks ``retargeted`` now: no second notice
        self.assertEqual(len(self.enqueued), 2)
        self.assertEqual([n[0] for n in self.notices()], ["stall"])

if __name__ == "__main__":
    unittest.main()
