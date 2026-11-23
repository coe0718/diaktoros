"""A reviewer turn stopped for the shadow review it owes, and its neighbours (#2634 review,
round 4).

F5: an event never turns an operator's stop into one it re-arms, whichever stop came first.
F6: ``hermes dk retry``'s planner (the CLI's and the Desktop's) and ``status``/``explain`` offer
the stopped turn, which re-arms with its shadow. A lost worker's stop keeps that cause too.
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
from diaktoros import config, retry_action  # noqa: E402
from diaktoros.run_supervisor import OPERATOR_CANCEL, views, workers  # noqa: E402
from test_shadow_owed_race import OWED, _Race  # noqa: E402
from test_shadow_rearm import REVIEWER  # noqa: E402


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
