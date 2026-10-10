"""A turn still queued or running at the head is not a stall.

The stall grace covers a turn's whole run, not its wait in the queue: behind another PR at
concurrency 1, held for CI, or paced. The sweep used to report "fixer never pushed" or "reviewer
never posted a verdict" for a turn the ledger showed queued or running (seen live alongside the
adjudicator's ruled-marker stall on #537).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import gate, gh, run_supervisor  # noqa: E402
from scripts import watchdog  # noqa: E402

LONG_AGO = "2020-01-01T00:00:00Z"


class LiveTurnIsNotAStall(fg.Base):
    def setUp(self):
        super().setUp()
        self.set_push(True)

    def sweep(self, reviews, live=None):
        """One sweep; ``live`` is (seat, state) for the ledger's turn at the head."""
        def turn_state(_db, _repo, _pr, head, seat):
            return live[1] if live and seat == live[0] and head == fg.HEAD else None
        with mock.patch.object(watchdog, "TEST", True), \
                mock.patch.object(gate, "hooks_armed", return_value=True), \
                mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
                mock.patch.object(gh, "open_prs", return_value=[fg.LIVE]), \
                mock.patch.object(gh, "reviews", return_value=reviews), \
                mock.patch.object(run_supervisor, "turn_state", side_effect=turn_state), \
                mock.patch.object(watchdog, "retry_pending_breaches"), \
                mock.patch.object(watchdog.routes, "fire"), \
                mock.patch.object(watchdog.observer, "notify"), \
                mock.patch.object(watchdog.observer, "retry", return_value=0), \
                mock.patch.object(watchdog.observer, "flush"):
            return "\n".join(watchdog.sweep_loop(self.loop, self.st))

    def arm_long_ago(self):
        self.sweep([])
        watch = self.st.watch()
        watch["heads"]["7"]["observed_at"] = time.time() - 36000
        self.st.watch_save(watch)

    def test_a_queued_or_running_review_is_not_a_reviewer_stall(self):
        self.arm_long_ago()
        self.assertIn("reviewer never posted a verdict", self.sweep([]))
        for state in ("pending", "waiting", "running"):
            self.assertNotIn("reviewer never posted a verdict",
                             self.sweep([], live=("reviewer", state)), state)
        for state in ("failed", "succeeded"):
            self.assertIn("reviewer never posted a verdict",
                          self.sweep([], live=("reviewer", state)), state)

    def test_a_queued_or_running_fix_is_not_a_fixer_stall(self):
        self.arm_long_ago()
        verdict = [{"id": 1, "state": "CHANGES_REQUESTED", "commit_id": fg.HEAD,
                    "submitted_at": LONG_AGO, "user": {"login": "reviewer", "id": 2}}]
        self.assertIn("fixer never pushed", self.sweep(verdict))
        for state in ("pending", "waiting", "running"):
            self.assertNotIn("fixer never pushed",
                             self.sweep(verdict, live=("fixer", state)), state)
        self.assertIn("fixer never pushed", self.sweep(verdict, live=("fixer", "failed")))
        # The other seat's live turn does not excuse it.
        self.assertIn("fixer never pushed", self.sweep(verdict, live=("reviewer", "running")))


if __name__ == "__main__":
    unittest.main()
