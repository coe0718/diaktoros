"""A reviewer turn stopped for the shadow review it owes, and its neighbours (#2634 review,
round 4).

F5: an event never turns an operator's stop into one it re-arms, whichever stop came first.
F7 (round 5): an operator's stop before the receipt claim posts no review.
F6: ``hermes dk retry``'s planner (the CLI's and the Desktop's) and ``status``/``explain`` offer
the stopped turn, which re-arms with its shadow. A lost worker's stop keeps that cause too.
F4 (round 6): a stop that cannot be written either still refuses the receipt, until the turn's
shadow row is in; round 7: so does one for a shadow owed but unkept, and a barrier written short.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import attribution, broker, config, gh, retry_action, review_receipt  # noqa: E402
from diaktoros.run_supervisor import OPERATOR_CANCEL, views, workers  # noqa: E402
from diaktoros.state import ledger  # noqa: E402
from test_shadow_owed_race import OWED, AShadowRowThatCannotBeWritten, _Race  # noqa: E402
from test_shadow_rearm import HEAD, REVIEWER  # noqa: E402


class AnOperatorsStop(_Race):
    """Review F5: an event never turns an operator's stop into one it re-arms."""

    def test_an_event_after_the_operators_stop_keeps_its_cause(self):
        loop = self.queued_unsampled()
        broken = self.break_snapshots(loop)
        with self.race("running", cancel=True):
            self.assertEqual(self.enqueue(loop), "duplicate running")
        self.assertTrue(self.stopped().startswith(OPERATOR_CANCEL))
        broken.rmdir()
        self.assertTrue(self.enqueue(loop).startswith("duplicate cancelled: cancelled by an operator"))

    def test_the_operators_stop_after_the_events_is_the_operators(self):
        loop = self.queued_unsampled()
        broken = self.break_snapshots(loop)
        with self.race("running"), self.assertRaisesRegex(RuntimeError, OWED):
            self.enqueue(loop)
        self.assertEqual(self.sup().cancel(self.sup().get(REVIEWER)["id"])["state"], "stopping")
        self.assertTrue(self.stopped().startswith(OPERATOR_CANCEL))
        broken.rmdir()
        self.assertTrue(self.enqueue(loop).startswith("duplicate cancelled: cancelled by an operator"))

    def test_an_operators_stop_before_the_claim_posts_no_review(self):
        # Review F7: the receipt refuses any asked stop, an operator's included, not only one
        # for the shadow; the review is never claimed or sent.
        self.assertEqual(self.enqueue(self.loop()), "enqueued")
        with closing(sqlite3.connect(self.sup().db)) as con, con:
            con.execute("UPDATE runs SET state='running', owner='w', generation='g1', lease=?, "
                        "launch_intent=? WHERE delivery=?", (time.time() + 600, time.time(),
                                                             REVIEWER))
        row = self.sup().get(REVIEWER)
        self.assertEqual(self.sup().cancel(row["id"])["state"], "stopping")
        sent = []
        scope = mock.Mock(repo="acme/widgets", number=8, head=HEAD, branch="fix-8")
        with mock.patch.object(broker, "authorize", return_value="rev"), \
                mock.patch.object(gh, "principal", return_value={"id": 5, "login": "rev"}), \
                mock.patch.object(gh, "api", side_effect=lambda *a, **k: sent.append(k)), \
                mock.patch.object(review_receipt, "generation_for", return_value="g1"), \
                mock.patch.object(attribution, "stamp", side_effect=lambda lp, body, **k: body), \
                self.assertRaisesRegex(review_receipt.ReceiptDenied, "asked to stop"):
            review_receipt.submit({"read_token": "reader"}, scope, review_receipt.ReceiptLedger(
                self.sup().db, row["id"], "g1"), "APPROVE", "Ship it.")
        self.assertEqual([k for k in sent if k.get("method") == "POST"], [])
        with closing(sqlite3.connect(self.sup().db)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM review_receipts").fetchone(), (0,))


class AStopThatCannotBeWritten(_Race):
    """Review F4, round 6: the COMMIT fails and so does the stop's UPDATE of the reviewer row,
    while receipt inserts still work (Vex's probe). The barrier beside the ledger holds."""
    failing_commit = AShadowRowThatCannotBeWritten.failing_commit

    def claim(self):
        review_receipt.ReceiptLedger(self.sup().db, self.sup().get(REVIEWER)["id"], "g1").claim(5)

    def test_its_receipt_is_refused_until_the_shadow_row_is_in(self):
        loop = self.queued_unsampled()
        self.refuse_stops()
        with self.race("running"), self.failing_commit(), \
                self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
            self.enqueue(loop)
        self.assertEqual(self.rows(), [("reviewer", "running")])
        self.assertIsNone(self.sup().get(REVIEWER)["cancel_requested"])
        with self.assertRaisesRegex(review_receipt.ReceiptDenied, "owes a shadow"):
            self.claim()
        with closing(sqlite3.connect(self.sup().db)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM review_receipts").fetchone(), (0,))
        # Control: the next event writes the shadow row beside it, and then the claim stands.
        self.assertEqual(self.enqueue(loop), "duplicate running")
        self.assertEqual(self.rows(), [("reviewer", "running"), ("shadow", "pending")])
        self.claim()

    def refuse_stops(self):
        with closing(sqlite3.connect(self.sup().db)) as con, con:
            con.execute("CREATE TRIGGER no_stop BEFORE UPDATE OF cancel_requested ON runs WHEN "
                        "NEW.cancel_requested IS NOT NULL BEGIN SELECT RAISE(ABORT, 'row'); END")

    def test_an_unkept_shadow_whose_stop_fails_still_refuses_the_receipt(self):
        # Round 7, F4: the snapshot cannot be kept (shadow is a str), and the stop fails too.
        loop = self.queued_unsampled()
        broken = self.break_snapshots(loop)
        self.refuse_stops()
        with self.race("running"), self.assertRaisesRegex(sqlite3.IntegrityError, "row"):
            self.enqueue(loop)
        self.assertEqual(self.rows(), [("reviewer", "running")])
        with self.assertRaisesRegex(review_receipt.ReceiptDenied, "owes a shadow"):
            self.claim()
        broken.rmdir()                    # control: the next event's shadow row lifts it
        self.assertEqual(self.enqueue(loop), "duplicate running")
        self.claim()

    def test_a_barrier_written_short_still_holds(self):
        # Round 7, F8: Vex's probe wrote all but the barrier's last byte and reported that.
        loop = self.queued_unsampled()
        self.refuse_stops()
        real, short = ledger.os.pwrite, [True]

        def pwrite(fd, data, at):
            if short[0] and len(data) > 1:
                short[0] = False
                return real(fd, data[:-1], at)
            return real(fd, data, at)
        with self.race("running"), self.failing_commit(), \
                mock.patch.object(ledger.os, "pwrite", pwrite), \
                self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
            self.enqueue(loop)
        self.assertFalse(short[0])
        with self.assertRaisesRegex(review_receipt.ReceiptDenied, "owes a shadow"):
            self.claim()

    def test_a_stop_that_is_written_clears_the_barrier(self):
        loop = self.queued_unsampled()
        with self.race("running"), self.failing_commit(), \
                self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
            self.enqueue(loop)
        lock = pathlib.Path(self.sup().db).with_name(pathlib.Path(self.sup().db).name
                                                     + ".claims.lock")
        self.assertEqual(lock.read_bytes(), b"")
        with self.assertRaisesRegex(review_receipt.ReceiptDenied, "asked to stop"):
            self.claim()


class RetriedThroughThePlanner(_Race):
    """Review F6: `hermes dk retry` and the Desktop offer a turn stopped for its shadow."""

    def test_the_planner_offers_it_and_re_arms_it_with_its_shadow(self):
        loop = self.queued_unsampled()
        broken = self.break_snapshots(loop)
        with self.race("running"), self.assertRaisesRegex(RuntimeError, OWED):
            self.enqueue(loop)
        self.stopped()
        broken.rmdir()
        with closing(sqlite3.connect(self.sup().db)) as con:  # status, explain, Desktop list
            con.row_factory = sqlite3.Row
            self.assertEqual([r["seat"] for r in views.runs_view(con, "acme/widgets", 8)],
                             ["reviewer"])
        with mock.patch.object(config, "by_repo", return_value=loop):
            done = retry_action.perform(loop, 8, "reviewer", ledger=self.sup().db,
                                        read_pr=lambda: True)
        self.assertEqual([entry["state"] for entry in done["rearmed"]], ["cancelled"], done)
        self.assertEqual(self.rows(), [("reviewer", "pending"), ("shadow", "pending")])


class ALostWorker(_Race):
    def test_its_stop_ends_cancelled_for_the_shadow_and_an_event_re_arms_it(self):
        loop = self.queued_unsampled()
        broken = self.break_snapshots(loop)
        with self.race("running"), self.assertRaisesRegex(RuntimeError, OWED):
            self.enqueue(loop)
        with closing(sqlite3.connect(self.sup().db)) as con, con:
            con.execute("UPDATE runs SET lease=?, pid=1 WHERE delivery=?",
                        (time.time() - 1, REVIEWER))
        with mock.patch.object(workers, "process_state", return_value="gone"):
            self.sup().recover()
        row = self.sup().get(REVIEWER)
        self.assertEqual(row["state"], "cancelled")
        self.assertTrue(row["error"].startswith(OWED), row["error"])
        broken.rmdir()
        self.assertEqual(self.enqueue(loop), "rearmed")
        self.assertEqual(self.rows(), [("reviewer", "pending"), ("shadow", "pending")])


if __name__ == "__main__":
    unittest.main()
