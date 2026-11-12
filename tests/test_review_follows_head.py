"""A review dropped by a head move is asked for again at the new head (#2468).

A reviewer turn queued at one head is cancelled when the head moves before it starts
(``run_supervisor.REVIEW_HEAD_MOVED``). The fixer's own push requests the next review; nobody
else's does (a review-only author's push, a base merge by the host or by "Update branch"), and the
reviewer gate ignores pushes. So the watchdog delivers it for the new head, once per head, as it
delivers an unannounced pending request (#532). And the review-only update does not push its
base merge under a turn of the PR that is queued or running, as ``update_behind`` does not.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import base_merge, gate, gh, review_only_conflict, run_supervisor  # noqa: E402
from diaktoros import watch_conflicts, watch_mode  # noqa: E402
from diaktoros.run_supervisor import views  # noqa: E402
from scripts import watchdog  # noqa: E402

OLD = "c" * 40
REPO = fg.REPO


class MovedReview(unittest.TestCase):
    """``views.moved_review`` reads the PR's newest reviewer turn from the ledger."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "runs.sqlite"
        run_supervisor.Supervisor(self.db)
        self.n = 0

    def row(self, head, state, error="", seat="reviewer", pr=7, age=0.0):
        self.n += 1
        when = time.time() - 1000 + self.n - age
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs (id, delivery, repo, pr, head, seat, state, attempts, "
                        "error, created, updated, turn_key, push_admitted, retries) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, '', 0, 0)",
                        (f"r{self.n}", f"d{self.n}", REPO, pr, head, seat, state, error, when, when))

    def moved(self, head=fg.HEAD):
        return views.moved_review(self.db, REPO, 7, head)

    def test_a_review_cancelled_by_a_head_move_names_its_head(self):
        self.row(OLD, "cancelled", run_supervisor.REVIEW_HEAD_MOVED)
        self.assertEqual(self.moved(), OLD)

    def test_only_the_newest_reviewer_row_counts(self):
        self.row(OLD, "cancelled", run_supervisor.REVIEW_HEAD_MOVED)
        self.row(fg.HEAD, "waiting")                   # queued at the new head: it answers
        self.assertIsNone(self.moved())
        self.row("d" * 40, "succeeded")                # a later review that ran
        self.assertIsNone(self.moved())

    def test_other_cancellations_and_other_seats_do_not_count(self):
        self.row(OLD, "cancelled", "PR closed before the review started")
        self.assertIsNone(self.moved())
        self.row(OLD, "cancelled", run_supervisor.REVIEW_HEAD_MOVED, seat="fixer")
        self.assertIsNone(self.moved())
        self.row(OLD, "cancelled", run_supervisor.REVIEW_HEAD_MOVED, pr=8)
        self.assertIsNone(self.moved())

    def test_a_cancellation_at_the_head_itself_is_not_a_move_to_it(self):
        self.row(fg.HEAD, "cancelled", run_supervisor.REVIEW_HEAD_MOVED)
        self.assertIsNone(self.moved())

    def test_a_missing_ledger_is_none(self):
        self.assertIsNone(views.moved_review(self.db.parent / "absent.sqlite", REPO, 7, fg.HEAD))

    def test_both_writers_use_the_one_reason(self):
        # The claim's read and the worker's sweep of waiting rows write the reason the check
        # reads: one constant, so the wording cannot drift from what ``moved_review`` matches.
        root = Path(__file__).resolve().parents[1] / "diaktoros" / "run_supervisor"
        for name in ("turns.py", "workers.py"):
            text = (root / name).read_text()
            self.assertIn("run_supervisor.REVIEW_HEAD_MOVED", text, name)
            self.assertNotIn("'PR head moved before the review started'", text, name)


class SweepDeliversIt(fg.Base):
    """The watchdog's sweep, as ``test_stall_live_turn`` drives it."""

    def setUp(self):
        super().setUp()
        self.set_push(True)
        self.kicks = []
        self.moved = None

    def sweep(self, reviews, grace_s=None):
        def kick(_loop, pr, head, sender, tag):
            self.kicks.append((pr["number"], head, tag))
            return True
        with mock.patch.object(watch_mode, "TEST", grace_s is None), \
                mock.patch.object(watchdog.config, "stall_grace_s", return_value=grace_s or 0), \
                mock.patch.object(gate, "hooks_armed", return_value=True), \
                mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
                mock.patch.object(gh, "open_prs", return_value=[fg.LIVE]), \
                mock.patch.object(gh, "reviews", return_value=reviews), \
                mock.patch.object(views, "turn_state", return_value=None), \
                mock.patch.object(views, "moved_review", side_effect=lambda *a: self.moved), \
                mock.patch.object(watchdog.review_kick, "kick", side_effect=kick), \
                mock.patch.object(watchdog, "retry_pending_breaches"), \
                mock.patch.object(watchdog.routes, "fire"), \
                mock.patch.object(watchdog.observer, "notify"), \
                mock.patch.object(watchdog.observer, "retry", return_value=0), \
                mock.patch.object(watchdog.observer, "flush"):
            return "\n".join(watchdog.sweep_loop(self.loop, self.st))

    def arm(self, grace_s=None):
        """The first sweep records the head; the next one judges it."""
        self.sweep([], grace_s=grace_s)
        self.assertEqual(self.kicks, [])

    def observed_long_ago(self):
        watch = self.st.watch()
        watch["heads"]["7"]["observed_at"] = time.time() - 36000
        self.st.watch_save(watch)

    def test_a_review_the_head_move_cancelled_is_delivered_for_the_new_head_once(self):
        self.arm()
        self.moved = OLD
        self.sweep([])
        self.assertEqual(self.kicks, [(7, fg.HEAD, "sweep-review-7")])
        self.sweep([])
        self.assertEqual(len(self.kicks), 1, "once per head")

    def test_without_a_cancelled_review_nothing_is_delivered(self):
        # fg.LIVE has no pending requested reviewer and the reviewer is not an App: the #532
        # path stays quiet too, as before.
        self.arm()
        self.sweep([])
        self.assertEqual(self.kicks, [])

    def test_a_head_with_a_verdict_is_not_asked_again(self):
        self.arm()
        self.moved = OLD
        self.sweep([fg.verdict(state="APPROVED")])
        self.assertEqual(self.kicks, [])

    def test_it_waits_one_sweep_interval_after_the_head_is_seen(self):
        self.arm(grace_s=3600)
        self.moved = OLD
        self.sweep([], grace_s=3600)
        self.assertEqual(self.kicks, [], "a head just seen may still move again")
        self.observed_long_ago()
        self.sweep([], grace_s=3600)
        self.assertEqual(self.kicks, [(7, fg.HEAD, "sweep-review-7")])


class ReviewOnlyUpdateWaits(fg.Base):
    """``watch_conflicts._review_only_next`` with ``review_only_update`` on."""

    def setUp(self):
        super().setUp()
        self.set_push(True)
        self.loop = {**self.loop, "review_only": ["tuck"], "review_only_update": True}
        self.pushed = []

    def next_step(self, busy, state="behind"):
        facts = {"number": 7, "head": fg.HEAD, "branch": "fix-7", "fork": False, "merged": [],
                 "conflicted": [], "workflows": False, "error": "", "clean": True}

        def update(_loop, number, head, _facts):
            self.pushed.append((number, head))
            return "e" * 40, ""
        with mock.patch.object(base_merge, "in_flight", return_value=busy), \
                mock.patch.object(review_only_conflict, "assess", return_value=facts), \
                mock.patch.object(review_only_conflict, "update", side_effect=update), \
                mock.patch.object(watch_conflicts.observer, "notify"):
            return watch_conflicts._review_only_next(
                self.loop, self.st, 7, fg.HEAD, "tuck", "b" * 40,
                {7: {**fg.LIVE, "mergeable_state": state}})

    def test_no_merge_is_pushed_under_a_queued_or_running_turn(self):
        self.assertIsNone(self.next_step("a reviewer turn is waiting"))
        self.assertEqual(self.pushed, [])

    def test_the_merge_is_pushed_once_nothing_is_in_flight(self):
        self.assertIsNone(self.next_step(""))
        self.assertEqual(self.pushed, [(7, fg.HEAD)])

    def test_a_real_conflict_still_gets_its_notice_but_no_push_while_busy(self):
        message = self.next_step("a reviewer turn is running", state="dirty")
        self.assertEqual(self.pushed, [])
        self.assertTrue(message)


if __name__ == "__main__":
    unittest.main()
