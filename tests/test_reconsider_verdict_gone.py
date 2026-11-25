"""#2023: a reconsider whose verdict is gone by the gate's own read says it became a plain review.

``hermes dk review --reconsider`` refuses a head with no verdict, then the reviewer gate reads the
reviews again. If the verdict was deleted between the two reads, the gate queues a plain review
(its fresh turn key, not ``RECONSIDER_KEY``), whose prompt never shows the reason, and the
command takes the reason back. It used to report "review queued" and record a reconsider as if
the reason went with the turn; it now says the review is a plain one, records a review, and
still leaves no reason behind.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
import test_review_entry_point as entry  # noqa: E402

from diaktoros import gate, one_shot_action as actions  # noqa: E402
from diaktoros.run_supervisor import RECONSIDER_KEY  # noqa: E402

REASON = "the requirement in the issue must be met, not deferred"


class VerdictGone(entry.Harness):
    def setUp(self):
        self.keys = []          # the turn keys the gate hands the ledger
        for patch in (mock.patch("diaktoros.gate.enqueue_isolated",
                                 side_effect=lambda loop, seat, n, head, turn_key='': (
                                     self.keys.append(turn_key) or "enqueued")),
                      mock.patch.object(actions, "_reviewer_runs", side_effect=self.runs)):
            patch.start()
            self.addCleanup(patch.stop)

    def runs(self, repo, number, head, turn_key=None):
        """The ledger's reviewer runs at this head, from what the gate enqueued."""
        return len([key for key in self.keys if turn_key is None or key == turn_key])

    def reconsider(self, gone=False):
        """A reconsider; ``gone``: the verdict is deleted once the command's own check read it."""
        real, reads = gate.reviewed_at_head, iter([True])
        with mock.patch.object(gate, "reviewed_at_head", side_effect=lambda *a: (
                next(reads, False) if gone else real(*a))):
            return actions.review(self.loop(), 7, reconsider=REASON)

    def operator_actions(self):
        from diaktoros import run_supervisor
        from diaktoros.state import ledger
        with ledger.connect(run_supervisor.production_ledger()) as con:
            return [row[0] for row in con.execute(
                "SELECT action FROM operator_actions WHERE pr=7")]

    def reason(self):
        from diaktoros import state as state_mod
        return state_mod.state_for(self.loop()).reconsider_get(7, t.HEAD_A)

    def test_a_verdict_deleted_before_the_gate_reads_it_is_a_plain_review(self):
        t.reset(prs={"7": entry.approved()})
        result = self.reconsider(gone=True)
        self.assertEqual(result["code"], 0, result["lines"])
        self.assertNotIn(RECONSIDER_KEY, self.keys)
        self.assertEqual(len(self.keys), 1, self.keys)
        self.assertIn("review queued for the isolated worker as a plain review (the head had no "
                      "verdict when the gate read it: your reason is not in its prompt)",
                      result["lines"][-1])
        self.assertEqual(self.operator_actions(), ["review"])
        self.assertFalse(self.reason())                 # no prompt reads it: taken back

    def test_a_reconsider_the_gate_keys_says_nothing_of_a_plain_review(self):
        t.reset(prs={"7": entry.approved()})
        result = self.reconsider()
        self.assertEqual(self.keys, [RECONSIDER_KEY])
        self.assertNotIn("plain review", result["lines"][-1])
        self.assertIn("review queued for the isolated worker (", result["lines"][-1])
        self.assertEqual(self.operator_actions(), ["reconsider"])
        self.assertEqual(self.reason()["reason"], REASON)


if __name__ == "__main__":
    unittest.main()
