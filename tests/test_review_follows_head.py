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
from diaktoros.run_supervisor import merge_hold, turns, views  # noqa: E402
from scripts import watchdog  # noqa: E402

OLD = "c" * 40
REPO = fg.REPO


def new_ledger(case) -> Path:
    """A real run ledger, as the host prepares it, in a temp dir the test removes."""
    temp = tempfile.TemporaryDirectory()
    case.addCleanup(temp.cleanup)
    db = Path(temp.name) / "runs.sqlite"
    run_supervisor.Supervisor(db)
    return db


def queue(db, state="pending", seat="reviewer", pr=7, head=None, n=[0]):
    """A turn of the PR in ``state``; its row, as a claim reads it."""
    n[0] += 1
    now = time.time()
    with contextlib.closing(sqlite3.connect(db)) as con, con:
        con.execute("INSERT INTO runs (id, delivery, repo, pr, head, seat, state, attempts, error, "
                    "created, updated, turn_key, push_admitted, retries) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, '', ?, ?, '', 0, 0)",
                    (f"q{n[0]}", f"dq{n[0]}", REPO, pr, head or fg.HEAD, seat, state, now, now))
    return read_row(db, f"q{n[0]}")


def read_row(db, run_id):
    with contextlib.closing(sqlite3.connect(db)) as con:
        con.row_factory = sqlite3.Row
        return con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def claim(db, row):
    """The worker's claim transaction (``_claim_commit``) for ``row``, its reads all clear."""
    return run_supervisor.Supervisor(db)._claim_commit(row, turns._ClaimRead())


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

    def retire(self, n):
        # The worker's sweep of waiting rows cancels an old turn late: its update time is now.
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state='cancelled', error=?, updated=? WHERE id=?",
                        (run_supervisor.REVIEW_HEAD_MOVED, time.time(), f"r{n}"))

    def test_a_turn_retired_late_is_not_the_newest(self):
        self.row(OLD, "waiting")
        self.row("d" * 40, "succeeded")                # a newer review ran meanwhile
        self.retire(1)
        self.assertIsNone(self.moved())

    def test_a_live_turn_at_the_head_still_answers_after_a_late_retirement(self):
        self.row(OLD, "waiting")
        self.row(fg.HEAD, "waiting")
        self.retire(1)
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
        # A head seen after the loop armed: both clocks start when it was seen.
        watch = self.st.watch()
        watch["heads"]["7"]["observed_at"] = watch["heads"]["7"]["first_seen_at"] = time.time() - 36000
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

    def first_seen_long_ago(self):
        watch = self.st.watch()
        watch["heads"]["7"]["first_seen_at"] = time.time() - 36000
        self.st.watch_save(watch)

    def test_a_head_already_there_when_the_loop_armed_is_delivered_too(self):
        # The head the first sweep found has no observed_at: the #532 path passes it over. The
        # ledger's cancellation is the proof a review was asked for; one interval after the loop
        # first saw the head, it is delivered, once.
        self.arm(grace_s=3600)
        self.assertIsNone(self.st.watch()["heads"]["7"]["observed_at"])
        self.moved = OLD
        self.sweep([], grace_s=3600)
        self.assertEqual(self.kicks, [])
        self.first_seen_long_ago()
        self.sweep([], grace_s=3600)
        self.sweep([], grace_s=3600)
        self.assertEqual(self.kicks, [(7, fg.HEAD, "sweep-review-7")])

    def test_a_head_record_from_before_first_seen_at_is_delivered_from_arming(self):
        # Watchdog state written before first_seen_at existed, or with an unreadable clock: the
        # interval runs from arming instead (as ``ready``'s grace does), so it still ends.
        for legacy in (None, "not a clock"):
            with self.subTest(first_seen_at=legacy):
                self.setUp()
                self.arm(grace_s=3600)
                watch = self.st.watch()
                if legacy is None:
                    del watch["heads"]["7"]["first_seen_at"]
                else:
                    watch["heads"]["7"]["first_seen_at"] = legacy
                self.st.watch_save(watch)
                self.moved = OLD
                self.sweep([], grace_s=3600)
                self.assertEqual(self.kicks, [], "armed just now: the interval has not run")
                watch = self.st.watch()
                watch["armed_since"] = time.time() - 36000
                self.st.watch_save(watch)
                self.sweep([], grace_s=3600)
                self.sweep([], grace_s=3600)
                self.assertEqual(self.kicks, [(7, fg.HEAD, "sweep-review-7")])

    def test_a_head_already_there_without_a_cancelled_review_stays_untouched(self):
        self.arm(grace_s=3600)
        self.first_seen_long_ago()
        self.sweep([], grace_s=3600)
        self.assertEqual(self.kicks, [])


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

    def test_a_turn_queued_during_the_dry_merge_stops_the_push(self):
        # Idle at the first look; a reviewer turn is queued while ``assess`` runs. The push takes
        # its hold only where no turn is in flight, so it sees the turn and pushes nothing.
        db = new_ledger(self)
        facts = {"number": 7, "head": fg.HEAD, "branch": "fix-7", "fork": False, "merged": [],
                 "conflicted": [], "workflows": False, "error": "", "clean": True}

        def assess(*_a, **_kw):
            queue(db)
            return facts

        def update_branch(_loop, *, number, head, before_push=None, **_kw):
            if before_push is not None:
                before_push("e" * 40, "b" * 40, "")
            self.pushed.append((number, head))
            return {"new_head": "e" * 40}
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=db), \
                mock.patch.object(review_only_conflict, "assess", side_effect=assess), \
                mock.patch.object(review_only_conflict.safe_push, "update_branch",
                                  side_effect=update_branch), \
                mock.patch.object(watch_conflicts.observer, "notify"):
            watch_conflicts._review_only_next(self.loop, self.st, 7, fg.HEAD, "tuck", "b" * 40,
                                              {7: {**fg.LIVE, "mergeable_state": "behind"}})
        self.assertEqual(self.pushed, [])

    def test_a_real_conflict_still_gets_its_notice_but_no_push_while_busy(self):
        message = self.next_step("a reviewer turn is running", state="dirty")
        self.assertEqual(self.pushed, [])
        self.assertTrue(message)


class MergeHold(unittest.TestCase):
    """``run_supervisor.merge_hold`` against the worker's own claim transaction, on a real ledger:
    the push and a claim are serialized by the ledger's writer lock, whichever comes first."""

    def setUp(self):
        self.db = new_ledger(self)

    def take(self):
        return merge_hold.take(self.db, REPO, 7, fg.HEAD, base_merge.IN_FLIGHT)

    def test_a_turn_admitted_after_the_hold_is_not_claimed_until_it_is_released(self):
        token, busy = self.take()
        self.assertTrue(token)
        self.assertEqual(busy, "")
        row = queue(self.db)                      # admitted after the push's last look
        self.assertIsNone(claim(self.db, row))
        waiting = read_row(self.db, row["id"])
        self.assertEqual((waiting["state"], waiting["error"]), ("waiting", merge_hold.WAITING))
        self.assertGreater(waiting["retry_at"], time.time())
        merge_hold.release(self.db, REPO, 7, token)
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state='pending' WHERE id=?", (row["id"],))
        self.assertIsNotNone(claim(self.db, read_row(self.db, row["id"])))

    def test_a_turn_claimed_or_running_first_stops_the_push(self):
        for state in ("pending", "waiting", "claimed", "running"):
            with self.subTest(state=state):
                self.setUp()
                queue(self.db, state=state)
                self.assertEqual(self.take(), ("", f"a reviewer turn is {state}"))
        self.setUp()
        claimed = claim(self.db, queue(self.db))  # the claim wins the race
        self.assertIsNotNone(claimed)
        self.assertEqual(self.take(), ("", "a reviewer turn is claimed"))

    def test_another_live_hold_stops_a_second_push(self):
        self.assertTrue(self.take()[0])
        self.assertEqual(self.take(), ("", "another merge of the base is being pushed"))

    def test_a_stale_hold_holds_nothing_and_is_replaced(self):
        token, _ = self.take()
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE merge_holds SET created=?", (time.time() - merge_hold.HOLD_S - 1,))
        self.assertIsNotNone(claim(self.db, queue(self.db, pr=7)))
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("DELETE FROM runs")
        fresh, busy = self.take()
        self.assertTrue(fresh)
        merge_hold.release(self.db, REPO, 7, token)          # the stale push's token: not ours
        self.assertEqual(self.take(), ("", "another merge of the base is being pushed"))

    def test_another_pr_is_not_held(self):
        self.take()
        self.assertIsNotNone(claim(self.db, queue(self.db, pr=8)))


if __name__ == "__main__":
    unittest.main()
