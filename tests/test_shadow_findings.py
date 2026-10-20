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
        gate.enqueue_shadow(loop, supervisor, 7, ts.HEAD)
        supervisor.submit.assert_called_once()
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})
        st.findings_put(7, VEX_SAME_ROUND)
        gate.enqueue_shadow(loop, supervisor, 7, ts.HEAD)  # a redelivery after the review
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})    # the first one stands
        self.assertIsNone(st.shadow_findings_get(7, "b" * 40))


class Retention(unittest.TestCase):
    """A snapshot outlives its shadow run: the 300-entry cap never evicts one in use (#1013)."""

    setUp = ts.NoWriteBrokerTests.setUp
    serve = ts.NoWriteBrokerTests.serve

    def fill(self, st, n):
        for i in range(n):
            st.shadow_findings_put(1000 + i, ts.HEAD, {})

    def ledger(self, *runs):
        """A run ledger holding shadow rows ``(pr, state)``, as the production one would."""
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
            delivery = f"acme/widgets:{pr}:{ts.HEAD}:shadow"
            sup.enqueue(delivery, "acme/widgets", pr, ts.HEAD, "shadow")
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
            gate.enqueue_shadow(loop, mock.Mock(), 7, ts.HEAD)
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

    def test_a_second_enqueue_in_the_gap_before_submit_cannot_evict_the_first_snapshot(self):
        """299 open snapshots; B's enqueue starts while A's run is not yet in the ledger."""
        import threading
        from diaktoros.run_supervisor import Supervisor
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        self.fill(st, st.SHADOW_FINDINGS_KEPT - 1)
        db = self.ledger(*[(1000 + i, "waiting") for i in range(st.SHADOW_FINDINGS_KEPT - 1)])
        sup = Supervisor(db, fixture_mode=True)
        sup._spawn = lambda: None
        seen = {}
        second = threading.Thread(target=lambda: seen.update(
            b=gate.enqueue_shadow(loop, sup, 8, ts.HEAD)))
        real_submit = sup.submit

        def submit_a(*args, **kwargs):
            if not second.is_alive() and not seen:
                second.start()              # B arrives after A's snapshot, before A's row
                second.join(0.5)            # unfixed, B finishes here and evicts A's snapshot
            return real_submit(*args, **kwargs)

        sup.submit = submit_a
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=db):
            a = gate.enqueue_shadow(loop, sup, 7, ts.HEAD)
            second.join(30)
        self.assertEqual(a, "enqueued")
        self.assertEqual(seen.get("b"), "enqueued")
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})      # A's run is open: kept
        self.assertEqual(st.shadow_findings_get(8, ts.HEAD), {})

    def test_no_snapshot_no_shadow(self):
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        supervisor = mock.Mock()
        with mock.patch.object(state_mod.LoopState, "shadow_findings_put",
                               side_effect=OSError("disk full")):
            self.assertEqual(gate.enqueue_shadow(loop, supervisor, 7, ts.HEAD), "no snapshot")
        supervisor.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
