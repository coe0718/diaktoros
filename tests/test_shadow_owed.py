"""An owed shadow that cannot be queued fails closed, and a refused retry keeps no snapshot
(#2634 review of #2627 and #2628).

F1: ``snapshot_shadow`` answers False both for a turn that owes no shadow and for one whose
findings snapshot could not be kept. Read as "none owed", a retry or a redelivery armed the
reviewer with no shadow row, and ``shadow.escalate`` found nothing to wait for. Now a config
read or snapshot write that fails refuses the retry, and a redelivery holds: the reviewer row is
not re-armed, or is failed when it was still to start. A turn that is truly unsampled still
re-arms with no shadow.

F2: ``retry`` took the snapshot before its checks, so a refused retry kept one (the first
snapshot of a turn wins) and a later legitimate retry's shadow was judged on old findings.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
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


if __name__ == "__main__":
    unittest.main()
