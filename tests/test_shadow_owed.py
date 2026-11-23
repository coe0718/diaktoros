"""An owed shadow that cannot be queued fails closed, and a refused retry keeps no snapshot
(#2634 review of #2627 and #2628).

F1: ``snapshot_shadow`` answers False both for a turn that owes no shadow and for one whose
findings snapshot could not be kept. Read as "none owed", a retry or a redelivery armed the
reviewer with no shadow row, and ``shadow.escalate`` found nothing to wait for. Now a config
read or snapshot write that fails refuses the retry, and a redelivery holds: the reviewer row is
not re-armed, or is failed when it was still to start. A turn that is truly unsampled still
re-arms with no shadow.

F2: ``retry`` took the snapshot before its checks, so a refused retry kept one (the first
snapshot of a turn wins) and a later legitimate retry's shadow was judged on old findings. A
snapshot now counts as the first only once its shadow row is in the ledger: one left by a
queueing whose insert failed and rolled back is taken again.

F3 (round 2): a new sampled reviewer row whose snapshot cannot be written is not written either:
the gate holds the event, and a later one queues both.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import pathlib
import sqlite3
import sys
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import config, gate  # noqa: E402
from diaktoros.run_supervisor import Supervisor, intake  # noqa: E402
from test_shadow_rearm import ESCALATE, HEAD, REVIEWER, SHADOW, _Ledger  # noqa: E402

OWED = getattr(intake, "SHADOW_OWED", "a shadow review is owed but could not be queued")


class OwedShadowFailsClosed(_Ledger):
    def rows(self):
        with closing(sqlite3.connect(self.sup().db)) as con:
            return con.execute("SELECT seat, state FROM runs ORDER BY seat").fetchall()

    def retry(self, loop, **patch):
        with mock.patch.object(config, "by_repo", return_value=loop, **patch):
            return Supervisor(self.sup().db).retry(self.sup().get(REVIEWER)["id"])

    def break_snapshots(self, loop):
        # A real write failure: the snapshot file's path is a directory, so its save raises.
        path = gate.state_mod.state_for(loop).dir / "shadow-findings.json"
        path.mkdir(parents=True)
        return path

    def fail_shadow_inserts(self, on=True):
        # A real insert failure inside the transaction: only shadow-seat rows are refused.
        with closing(sqlite3.connect(self.sup().db)) as con, con:
            con.execute("CREATE TRIGGER no_shadow BEFORE INSERT ON runs WHEN NEW.seat='shadow' "
                        "BEGIN SELECT RAISE(ABORT, 'disk failure'); END" if on
                        else "DROP TRIGGER no_shadow")

    def assert_reviewer_unarmed(self):
        self.assertEqual(self.rows(), [("reviewer", "failed")])

    def test_retry_refuses_when_the_snapshot_cannot_be_written(self):
        self.enqueue(self.loop())
        self.fail_reviewer()
        loop = self.loop(ESCALATE)
        self.break_snapshots(loop)
        with self.assertRaisesRegex(ValueError, "owes a shadow review.*could not be kept"):
            self.retry(loop)
        self.assert_reviewer_unarmed()

    def test_retry_refuses_when_the_loop_cannot_be_read(self):
        self.enqueue(self.loop())
        self.fail_reviewer()
        with self.assertRaisesRegex(ValueError, "owes a shadow review is unknown"):
            self.retry(None, side_effect=config.ConfigError("bad loop"))
        self.assert_reviewer_unarmed()

    def test_a_pending_redelivery_whose_snapshot_cannot_be_written_holds(self):
        self.assertEqual(self.enqueue(self.loop()), "enqueued")
        loop = self.loop(ESCALATE)
        broken = self.break_snapshots(loop)
        with self.assertRaisesRegex(RuntimeError, "reviewer turn not armed"):
            self.enqueue(loop)
        self.assert_reviewer_unarmed()
        self.assertIn(OWED, self.sup().get(REVIEWER)["error"])
        broken.rmdir()                 # the next event, once a snapshot can be kept, re-arms it
        self.assertEqual(self.enqueue(loop), "rearmed")
        self.assertEqual(self.rows(), [("reviewer", "pending"), ("shadow", "pending")])
        self.assertEqual(gate.shadows.shadow_state(loop, 8, HEAD, self.sup().db)[0], "pending")

    def test_a_failed_reviewer_whose_snapshot_cannot_be_written_is_not_re_armed(self):
        self.enqueue(self.loop())
        self.fail_reviewer()
        loop = self.loop(ESCALATE)
        self.break_snapshots(loop)
        with self.assertRaisesRegex(RuntimeError, OWED):
            self.enqueue(loop)
        self.assert_reviewer_unarmed()

    def test_a_turn_sampled_out_still_re_arms_without_a_shadow(self):
        loop = self.loop(ESCALATE)
        with mock.patch.object(gate.shadows, "shadow_sampled", return_value=False):
            self.assertEqual(self.enqueue(loop), "enqueued")
            self.assertEqual(self.enqueue(loop), "pending")
            self.fail_reviewer()
            self.assertEqual(self.retry(loop), "pending")
        self.assertEqual(self.rows(), [("reviewer", "pending")])

    def test_a_refused_retry_keeps_no_snapshot_for_a_later_one(self):
        # F2's probe: a retry refused for the row's state, then findings change, then a retry.
        self.enqueue(self.loop())
        loop = self.loop(ESCALATE)
        st = gate.state_mod.state_for(loop)
        with self.assertRaisesRegex(ValueError, "run is pending"):
            self.retry(loop)
        self.assertIsNone(st.shadow_findings_get(8, HEAD, ""))
        st.findings_put(8, {"prior_finding": "new"})
        self.fail_reviewer()
        self.assertEqual(self.retry(loop), "pending")
        self.assertEqual(st.shadow_findings_get(8, HEAD, ""), {"prior_finding": "new"})
        self.assertEqual(self.sup().get(SHADOW)["state"], "pending")

    def test_a_retry_whose_shadow_row_cannot_be_written_keeps_no_snapshot(self):
        self.enqueue(self.loop())
        self.fail_reviewer()
        loop, budget = self.loop(ESCALATE), config.turn_budget
        with mock.patch.object(config, "turn_budget", side_effect=lambda lp, seat: 0
                               if seat == "shadow" else budget(lp, seat)), \
                self.assertRaisesRegex(ValueError, "shadow row"):
            self.retry(loop)
        self.assertIsNone(gate.state_mod.state_for(loop).shadow_findings_get(8, HEAD, ""))
        self.assert_reviewer_unarmed()


    def assert_judged_on_new_findings(self, loop):
        st = gate.state_mod.state_for(loop)
        self.assertEqual(st.shadow_findings_get(8, HEAD, ""), {"prior_finding": "new"})
        self.assertEqual(self.rows(), [("reviewer", "pending"), ("shadow", "pending")])

    def test_a_retry_rolled_back_after_its_snapshot_leaves_none_to_win(self):
        # Round 2, F2's probe: the snapshot is kept, then the shadow's insert fails and the
        # re-arm rolls back. The next retry's shadow is judged on the findings at that retry.
        self.enqueue(self.loop())
        self.fail_reviewer()
        loop = self.loop(ESCALATE)
        self.fail_shadow_inserts()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "disk failure"):
            self.retry(loop)
        self.assert_reviewer_unarmed()
        gate.state_mod.state_for(loop).findings_put(8, {"prior_finding": "new"})
        self.fail_shadow_inserts(on=False)
        self.assertEqual(self.retry(loop), "pending")
        self.assert_judged_on_new_findings(loop)

    def test_a_redelivery_rolled_back_after_its_snapshot_leaves_none_to_win(self):
        self.enqueue(self.loop())
        self.fail_reviewer()
        loop = self.loop(ESCALATE)
        self.fail_shadow_inserts()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "disk failure"):
            self.enqueue(loop)
        self.assert_reviewer_unarmed()
        gate.state_mod.state_for(loop).findings_put(8, {"prior_finding": "new"})
        self.fail_shadow_inserts(on=False)
        self.assertEqual(self.enqueue(loop), "rearmed")
        self.assert_judged_on_new_findings(loop)

    def test_a_new_reviewer_whose_snapshot_cannot_be_written_is_held_not_queued(self):
        # Round 2, F3's probe, through the gate: no reviewer row, the event held in the queue.
        loop = self.loop(ESCALATE)
        st, broken = gate.state_mod.state_for(loop), self.break_snapshots(loop)
        with mock.patch.object(config, "by_repo", return_value=loop), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                self.assertRaises(SystemExit):
            gate.block_pr_agent(loop, st, "reviewer", 8, HEAD)
        self.assertEqual(self.rows(), [])
        held = st.queue_items("reviewer")[gate.seat_key(loop, 8)]
        self.assertIn(f"reviewer turn not armed: {OWED}", held["reason"])
        broken.rmdir()                 # the next event, once a snapshot can be kept, queues both
        self.assertEqual(self.enqueue(loop), "enqueued")
        self.assertEqual(self.rows(), [("reviewer", "pending"), ("shadow", "pending")])
        self.assertEqual(gate.shadows.shadow_state(loop, 8, HEAD, self.sup().db)[0], "pending")


if __name__ == "__main__":
    unittest.main()
