"""A worker that claims the reviewer row before the redelivery's transaction reads it (#2634
review F4).

The gate takes the findings snapshot, then ``submit`` reads the row. In between, a worker (the
gate's own ``recover`` spawns one, and one already running claims any pending row) can claim the
row and launch it. The redelivery then found ``claimed``, ``launching`` or ``running`` and wrote
no owed shadow, so the reviewer could approve while ``shadow.escalate`` had nothing to wait for.

Each test moves the row at that boundary through a separate SQLite connection, as Vex's probe
did. A row with no write-ahead record gets its shadow on the gate's snapshot (its findings are
recorded only after its receipt claim). Unkept: a claimed row is failed, and a running one is
stopped and can post no review.

Round 4 (F4): the same when the shadow row's INSERT or the COMMIT fails. Round 5: a receipt claim
between that failed transaction and the stop waits for the stop (``ledger.claims``). An
operator's stop and ``hermes dk retry`` are in ``test_shadow_owed_stops``.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import pathlib
import sqlite3
import sys
import threading
import time
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import gate, review_receipt  # noqa: E402
from diaktoros.run_supervisor import OPERATOR_STOPPED, Supervisor, intake  # noqa: E402
from test_shadow_rearm import ESCALATE, HEAD, REVIEWER, SHADOW, _Ledger  # noqa: E402

OWED = getattr(intake, "SHADOW_OWED", "a shadow review is owed but could not be queued")


class _Race(_Ledger):
    """The race helpers; no tests of its own."""

    def rows(self):
        with closing(sqlite3.connect(self.sup().db)) as con:
            return con.execute("SELECT seat, state FROM runs ORDER BY seat").fetchall()

    def race(self, state, *, receipt=False, cancel=False):
        """Patch ``submit`` so a worker moves the reviewer row to ``state`` first, through its
        own connection, between the gate's snapshot and the redelivery's transaction (then, with
        ``cancel``, an operator stops it)."""
        real = Supervisor.submit

        def submit(sup, delivery, *a, **k):
            if delivery == REVIEWER:
                with closing(sqlite3.connect(sup.db)) as con, con:
                    con.execute("UPDATE runs SET state=?, owner='w', generation='g1', lease=?, "
                                "launch_intent=? WHERE delivery=?",
                                (state, time.time() + 600, None if state == "claimed"
                                 else time.time(), REVIEWER))
                    if receipt:
                        con.execute("INSERT INTO review_receipts(run_id,state,generation,"
                                    "principal_id,created) SELECT id,'claimed','g1',5,? FROM "
                                    "runs WHERE delivery=?", (time.time(), REVIEWER))
                if cancel:
                    sup.cancel(sup.get(REVIEWER)["id"])
            return real(sup, delivery, *a, **k)
        return mock.patch.object(Supervisor, "submit", submit)

    def queued_unsampled(self):
        self.assertEqual(self.enqueue(self.loop()), "enqueued")
        return self.loop(ESCALATE)

    def break_snapshots(self, loop):
        path = gate.state_mod.state_for(loop).dir / "shadow-findings.json"
        path.mkdir(parents=True)
        return path

    def assert_shadow_beside(self, state):
        loop = self.queued_unsampled()
        with self.race(state):
            self.assertEqual(self.enqueue(loop), f"duplicate {state}")
        self.assertEqual(self.rows(), [("reviewer", state), ("shadow", "pending")])
        kind, hold, note = gate.shadows.shadow_state(loop, 8, HEAD, self.sup().db)
        self.assertEqual(kind, "pending", (hold, note))

    def stopped(self):
        """The worker ends the turn it was asked to stop."""
        row = self.sup().get(REVIEWER)
        self.assertIsNotNone(row["cancel_requested"])
        self.assertEqual(self.sup().complete_uncertain(row["id"], "w", None, OPERATOR_STOPPED,
                                                       cancel=True), "cancelled")
        return self.sup().get(REVIEWER)["error"]


class ClaimedBeforeTheRedelivery(_Race):

    def test_a_row_a_worker_has_running_first_still_gets_its_shadow(self):
        # Vex's probe: before, no shadow row and "nothing to wait for".
        self.assert_shadow_beside("running")

    def test_a_row_a_worker_is_launching_first_still_gets_its_shadow(self):
        self.assert_shadow_beside("launching")

    def test_a_row_a_worker_claimed_first_still_gets_its_shadow(self):
        self.assert_shadow_beside("claimed")

    def test_a_row_that_may_have_written_gets_no_shadow_from_this_event(self):
        # Its receipt was claimed before this event decided: the snapshot could hold its
        # findings, so none is used (a turn queued unsampled that ran, as with no event at all).
        loop = self.queued_unsampled()
        with self.race("running", receipt=True):
            self.assertEqual(self.enqueue(loop), "duplicate running")
        self.assertEqual(self.rows(), [("reviewer", "running")])

    def test_unkept_a_row_claimed_first_is_failed_and_never_launched(self):
        loop = self.queued_unsampled()
        self.break_snapshots(loop)
        with self.race("claimed"), self.assertRaisesRegex(RuntimeError, OWED):
            self.enqueue(loop)
        row = self.sup().get(REVIEWER)
        self.assertEqual((row["state"], row["owner"]), ("failed", None))
        self.assertEqual(self.rows(), [("reviewer", "failed")])

    def test_unkept_a_row_running_first_is_stopped_posts_nothing_and_is_re_armed_later(self):
        loop = self.queued_unsampled()
        broken = self.break_snapshots(loop)
        with self.race("running"), self.assertRaisesRegex(RuntimeError, OWED):
            self.enqueue(loop)
        row = self.sup().get(REVIEWER)
        self.assertIsNotNone(row["cancel_requested"])
        with self.assertRaisesRegex(review_receipt.ReceiptDenied, "asked to stop"):
            review_receipt.ReceiptLedger(self.sup().db, row["id"], "g1").claim(5)
        # The worker stops it: cancelled for its shadow, not by an operator, so an event re-arms.
        self.assertEqual(self.sup().complete_uncertain(row["id"], "w", None, OPERATOR_STOPPED,
                                                       cancel=True), "cancelled")
        self.assertTrue(self.sup().get(REVIEWER)["error"].startswith(OWED))
        broken.rmdir()
        self.assertEqual(self.enqueue(loop), "rearmed")
        self.assertEqual(self.rows(), [("reviewer", "pending"), ("shadow", "pending")])


class AShadowRowThatCannotBeWritten(_Race):
    """Review F4 (round 4): the snapshot is kept, but the shadow row's INSERT or the COMMIT fails.
    The transaction rolls back; a worker already running the turn does not."""

    def refuse_shadow_rows(self):
        with closing(sqlite3.connect(self.sup().db)) as con, con:
            con.execute("CREATE TRIGGER no_shadow BEFORE INSERT ON runs WHEN NEW.seat='shadow' "
                        "BEGIN SELECT RAISE(ABORT, 'disk failure'); END")

    def test_a_running_turn_is_stopped_and_posts_nothing(self):
        # Vex's probe: before, IntegrityError, the row still running with no stop, its receipt
        # claimed, and shadow_state "nothing to wait for".
        loop = self.queued_unsampled()
        self.refuse_shadow_rows()
        with self.race("running"), self.assertRaisesRegex(RuntimeError, OWED):
            self.enqueue(loop)
        self.assertEqual(self.rows(), [("reviewer", "running")])
        row = self.sup().get(REVIEWER)
        with self.assertRaisesRegex(review_receipt.ReceiptDenied, "asked to stop"):
            review_receipt.ReceiptLedger(self.sup().db, row["id"], "g1").claim(5)
        self.assertTrue(self.stopped().startswith(OWED))

    def test_a_claimed_turn_is_failed_and_never_launched(self):
        loop = self.queued_unsampled()
        self.refuse_shadow_rows()
        with self.race("claimed"), self.assertRaisesRegex(RuntimeError, OWED):
            self.enqueue(loop)
        row = self.sup().get(REVIEWER)
        self.assertEqual((row["state"], row["owner"]), ("failed", None))

    def test_a_commit_that_fails_still_stops_the_running_turn(self):
        loop = self.queued_unsampled()
        with self.race("running"), self.failing_commit(), \
                self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
            self.enqueue(loop)
        self.assertEqual(self.rows(), [("reviewer", "running")])
        self.assertTrue(self.stopped().startswith(OWED))

    def test_a_claim_right_after_the_rollback_waits_for_the_stop(self):
        # Round 5: Vex's probe claimed the receipt on another connection just after the failed
        # transaction's ROLLBACK, before the stop, and it was granted: a review with no shadow.
        loop = self.queued_unsampled()
        got, threads = [], []

        def claim(run_id):
            try:
                review_receipt.ReceiptLedger(self.sup().db, run_id, "g1").claim(5)
                got.append("claimed")
            except review_receipt.ReceiptDenied as exc:
                got.append(str(exc))

        def in_the_gap():                  # unlocked, the claim lands here, before the stop
            threads.append(threading.Thread(target=claim, args=(self.sup().get(REVIEWER)["id"],)))
            threads[0].start()
            threads[0].join(1.0)
        with self.race("running"), self.failing_commit(in_the_gap), \
                self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
            self.enqueue(loop)
        threads[0].join(10)
        self.assertEqual(got, ["this reviewer turn was asked to stop: no review is posted"])
        with closing(sqlite3.connect(self.sup().db)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM review_receipts").fetchone(), (0,))
        self.assertTrue(self.stopped().startswith(OWED))

    def failing_commit(self, after_rollback=None):
        """Fail the redelivery's COMMIT once its shadow row is in, leaving its transaction to
        ``fails_closed``; run ``after_rollback`` just after that transaction's ROLLBACK."""
        real, armed = Supervisor._connect, [True]

        class Failing:
            def __init__(self, con):
                self.con, self.shadow = con, False

            def __getattr__(self, name):
                return getattr(self.con, name)

            def execute(self, sql, *args):
                self.shadow = self.shadow or bool(args and SHADOW in args[0])
                if sql == "COMMIT" and self.shadow and armed[0]:
                    armed[0] = False
                    raise sqlite3.OperationalError("disk I/O error")
                done = self.con.execute(sql, *args)
                if sql == "ROLLBACK" and self.shadow and after_rollback:
                    after_rollback()
                return done

        @contextlib.contextmanager
        def connect(sup):
            with real(sup) as con:
                yield Failing(con)
        return mock.patch.object(Supervisor, "_connect", connect)


if __name__ == "__main__":
    unittest.main()
