"""#539: ``explain`` shows the CI-fix count; the watchdog sweep does not call a PR with a CI-fix turn
queued or running a stall."""
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
from diaktoros import ci_fix, gate, gh  # noqa: E402
from scripts import watchdog  # noqa: E402


class Visibility(fg.Base):
    def setUp(self):
        super().setUp()
        self.set_push(True)
        self.loop = {**self.loop, "fix_ci": True, "ci_fix_cap": 3}

    def explain(self, rows):
        local = {"held": {}, "queued_seat": "", "queued_reason": "", "inflight_review": False,
                 "inflight_fix": False, "marker": {}, "parked": False, "delivery_status": "",
                 "capacity": {"reviewer": (0, 1), "fixer": (0, 1)}, "stale_queues": [],
                 "seat": "", "queue": "", "inflight": "", "escalation": "", "sweep": ""}
        with mock.patch.object(gate, "_explain_state", return_value=local), \
             mock.patch.object(ci_fix, "rows", return_value=rows):
            return gate.explain(self.loop, self.st, 7, {"pr": fg.LIVE, "reviews": [],
                                                        "armed": True, "read_at": time.time()})

    def test_explain_shows_the_ci_fix_count(self):
        rows = [{"head": "x", "state": "succeeded"}, {"head": "y", "state": "failed"}]
        self.assertIn("CI fixes: 2/3 spent", self.explain(rows)["budget"])
        self.assertIn("CI fixes: 0/3 spent", self.explain([])["budget"])

    def sweep(self, rows):
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=[fg.LIVE]), \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch.object(ci_fix, "rows", return_value=rows), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire"), \
             mock.patch.object(watchdog.observer, "notify"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"):
            return "\n".join(watchdog.sweep_loop(self.loop, self.st))

    def test_a_queued_or_running_ci_fix_is_not_a_stall(self):
        self.sweep([])                                    # arms, baselines
        watch = self.st.watch()
        watch["heads"]["7"]["observed_at"] = time.time() - 36000
        self.st.watch_save(watch)
        self.assertIn("reviewer never posted a verdict", self.sweep([]))
        for state in ("pending", "running"):
            self.assertNotIn("reviewer never posted a verdict",
                             self.sweep([{"head": fg.HEAD, "state": state}]), state)
        # a CI-fix turn that ended does not excuse the silence
        self.assertIn("reviewer never posted a verdict",
                      self.sweep([{"head": fg.HEAD, "state": "failed"}]))


if __name__ == "__main__":
    unittest.main()
