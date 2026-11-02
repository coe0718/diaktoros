"""A shadow review is judged on the findings ledger it was queued with (#218, seen on #1005).

The shadow is queued beside the real reviewer turn and shown only the reviews posted before then.
It runs later (after CI), by which time the real review of the same head may have recorded new
findings. The broker judged the shadow against that live ledger: on #1005 the reviewer's F1, which
the shadow never saw, refused its APPROVE ("cannot APPROVE with open finding(s) F1") and the
recorded shadow verdict became a forced block. The gate now snapshots the ledger when it queues the
shadow, and the no-write broker judges the shadow against that snapshot.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_selftest as ts  # noqa: E402
from diaktoros import broker, broker_ipc, gate, review_receipt, state as state_mod  # noqa: E402

VEX_SAME_ROUND = {"findings": {"F1": {"state": "open", "text": "a.py:1: x", "at": time.time()}},
                  "last_head": ts.HEAD}


class Snapshot(unittest.TestCase):
    setUp = ts.NoWriteBrokerTests.setUp
    serve = ts.NoWriteBrokerTests.serve

    def approve(self):
        server = self.serve(require_receipt=True, no_write=True)
        with mock.patch.object(broker, "perform", side_effect=AssertionError), \
                mock.patch.object(review_receipt, "submit", side_effect=AssertionError):
            return broker_ipc.request("review", verdict="APPROVE", body="LGTM\nNot verified: nothing",
                                      socket_path=str(server.socket_path)), server

    def test_a_shadow_is_judged_on_the_ledger_it_was_queued_with(self):
        st = state_mod.state_for(self.fx.loop)
        st.findings_put(7, VEX_SAME_ROUND)               # the real review, meanwhile
        st.shadow_findings_put(7, ts.HEAD, {})           # what the shadow was queued with
        answer, server = self.approve()
        self.assertTrue(answer.get("ok"), answer)
        self.assertEqual(server.recorded[0]["verdict"], "APPROVE")

    def test_without_a_snapshot_the_live_ledger_still_applies(self):
        state_mod.state_for(self.fx.loop).findings_put(7, VEX_SAME_ROUND)
        answer, _ = self.approve()
        self.assertFalse(answer.get("ok"))
        self.assertIn("cannot APPROVE with open finding(s) F1", answer.get("error", ""))

    def test_queueing_takes_the_snapshot_once(self):
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        supervisor = mock.Mock()
        gate.snapshot_shadow(loop, 7, ts.HEAD)          # as the reviewer's turn is queued
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})
        st.findings_put(7, VEX_SAME_ROUND)               # the real review records its findings
        gate.snapshot_shadow(loop, 7, ts.HEAD)           # a redelivery after the review
        gate.enqueue_shadow(loop, supervisor, 7, ts.HEAD)    # #1647: on the APPROVE
        supervisor.submit.assert_called_once()
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})    # the first one stands
        self.assertIsNone(st.shadow_findings_get(7, "b" * 40))


class Retention(unittest.TestCase):
    """A snapshot outlives its shadow run: the 300-entry cap never evicts one in use (#1013)."""

    setUp = ts.NoWriteBrokerTests.setUp
    serve = ts.NoWriteBrokerTests.serve

    def fill(self, st, n):
        for i in range(n):
            st.shadow_findings_put(1000 + i, ts.HEAD, {})

    def ledger(self, *runs, seat="shadow"):
        """A run ledger holding ``seat`` rows ``(pr, state)``, as the production one would."""
        import pathlib
        import sqlite3
        import tempfile
        from contextlib import closing
        from diaktoros.run_supervisor import Supervisor
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db = pathlib.Path(temp.name) / "runs.sqlite"
        sup = Supervisor(db, fixture_mode=True)
        sup._spawn = lambda: None
        for pr, state in runs:
            delivery = f"acme/widgets:{pr}:{ts.HEAD}:{seat}"
            sup.enqueue(delivery, "acme/widgets", pr, ts.HEAD, seat)
            with closing(sqlite3.connect(db)) as con, con:
                con.execute("UPDATE runs SET state=? WHERE delivery=?", (state, delivery))
        return db

    def test_the_cap_spares_a_snapshot_whose_shadow_is_still_open(self):
        st = state_mod.state_for(self.fx.loop)
        self.fill(st, st.SHADOW_FINDINGS_KEPT)
        st.shadow_findings_put(5, ts.HEAD, {}, {(1000, ts.HEAD)})
        self.assertEqual(st.shadow_findings_get(1000, ts.HEAD), {})   # the oldest, still in use
        self.assertIsNone(st.shadow_findings_get(1001, ts.HEAD))      # the oldest one that is not
        self.assertEqual(st.shadow_findings_get(5, ts.HEAD), {})

    def test_an_unknown_set_of_open_runs_evicts_nothing(self):
        st = state_mod.state_for(self.fx.loop)
        self.fill(st, st.SHADOW_FINDINGS_KEPT + 3)
        self.assertEqual(st.shadow_findings_get(1000, ts.HEAD), {})

    def test_queueing_through_the_gate_keeps_the_snapshot_of_a_delayed_shadow(self):
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        self.fill(st, st.SHADOW_FINDINGS_KEPT)
        db = self.ledger((1000, "waiting"), (1001, "succeeded"))
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=db):
            gate.snapshot_shadow(loop, 7, ts.HEAD)
        self.assertEqual(st.shadow_findings_get(1000, ts.HEAD), {})   # delayed, still open
        self.assertIsNone(st.shadow_findings_get(1001, ts.HEAD))      # ended: evicted
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})

    def test_a_delayed_shadow_is_still_judged_on_its_snapshot_after_the_cap(self):
        st = state_mod.state_for(self.fx.loop)
        st.findings_put(7, VEX_SAME_ROUND)
        st.shadow_findings_put(7, ts.HEAD, {}, set())
        self.fill(st, st.SHADOW_FINDINGS_KEPT)
        st.shadow_findings_put(8, ts.HEAD, {}, {(7, ts.HEAD)})
        answer, server = Snapshot.approve(self)
        self.assertTrue(answer.get("ok"), answer)

    def test_a_snapshot_waiting_for_its_reviewers_approve_is_kept(self):
        """#1647: the snapshot is taken as the reviewer is queued, and its shadow comes after the
        APPROVE. While that reviewer's run is open, the cap never evicts it."""
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        st.shadow_findings_put(7, ts.HEAD, {}, set())          # the oldest: reviewer 7's
        self.fill(st, st.SHADOW_FINDINGS_KEPT - 1)
        db = self.ledger((7, "waiting"), seat="reviewer")
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=db):
            gate.snapshot_shadow(loop, 8, ts.HEAD)
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})       # its reviewer is open
        self.assertIsNone(st.shadow_findings_get(1000, ts.HEAD))       # the oldest that is not

    def test_the_approval_queues_under_the_lock_so_the_snapshot_is_never_lost(self):
        """The snapshot read and the submit share one state lock: a put that would evict it waits
        until the shadow's row exists, and then keeps it."""
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        st.shadow_findings_put(7, ts.HEAD, {}, set())
        lock = str(st.dir / "state.lock")
        held = []
        sup = mock.Mock()
        sup.submit.side_effect = lambda *a, **k: held.append(
            (getattr(state_mod._HELD, "paths", None) or {}).get(lock, 0)) or "enqueued"
        self.assertEqual(gate.enqueue_shadow(loop, sup, 7, ts.HEAD), "enqueued")
        self.assertEqual(held, [1])                     # submitted with the state lock held

    def test_no_snapshot_no_shadow(self):
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        supervisor = mock.Mock()
        with mock.patch.object(state_mod.LoopState, "shadow_findings_put",
                               side_effect=OSError("disk full")):
            self.assertEqual(gate.enqueue_shadow(loop, supervisor, 7, ts.HEAD), "no snapshot")
        supervisor.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
