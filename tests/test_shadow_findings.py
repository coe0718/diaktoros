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


if __name__ == "__main__":
    unittest.main()
