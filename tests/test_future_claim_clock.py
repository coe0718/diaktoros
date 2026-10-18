"""Review on #937: the sweep's dead-claim check reads its own clock.

``now`` is taken at the top of the sweep, before its GitHub reads and hand-offs. A claim taken
while the sweep ran is dated after that ``now``: aged against it, the claim looked dated in the
future and was reported dead. A claim a little ahead of the clock (``FUTURE_SLACK_S``) is not.
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
from diaktoros import gate, gh, state as state_mod  # noqa: E402

KEY = f"{fg.REPO}#7"


class Clock:
    """``time`` for the watchdog, with a hand that moves the sweep's clock forward."""

    def __init__(self):
        self.offset = 0.0

    def time(self):
        return time.time() + self.offset

    def __getattr__(self, name):
        return getattr(time, name)


class SweepClock(fg.Base):
    def sweep(self, during):
        from scripts import watchdog
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", side_effect=lambda *a, **k: during() or []), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire"), \
             mock.patch.object(watchdog.observer, "notify"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"):
            return "\n".join(watchdog.sweep_loop(self.loop, self.st))

    def claim_at(self, at):
        self.st.acquire("reviewer", KEY, fg.HEAD, "working", run="r1")
        data = state_mod.LoopState._load(self.st, self.st.locks, {})
        data["reviewer"][KEY]["at"] = at
        state_mod._atomic_write(self.st.locks, data)

    def test_a_claim_taken_while_the_sweep_ran_is_not_reported_dead(self):
        from scripts import watchdog
        clock = Clock()
        clock.offset = -300.0                         # the sweep starts five minutes back ...

        def slow_reads():
            clock.offset = 0.0                        # ... its reads take those five minutes
            self.claim_at(time.time())                # and a gate claims the seat meanwhile
        with mock.patch.object(watchdog, "time", clock):
            self.sweep(lambda: None)                  # the first sweep only snapshots heads
            text = self.sweep(slow_reads)
        self.assertIn(KEY, self.st.live_locks("reviewer"))      # the claim reached the check
        self.assertNotIn("in the future", text)
        self.assertNotIn("slot held", text)

    def test_a_claim_a_little_ahead_is_quiet_and_far_ahead_is_named(self):
        self.sweep(lambda: None)                      # the first sweep only snapshots heads
        self.claim_at(time.time() + 30.0)
        self.assertNotIn("in the future", self.sweep(lambda: None))
        self.claim_at(time.time() + 120.0)
        self.assertIn(f"claim on {KEY} is dated", self.sweep(lambda: None))


if __name__ == "__main__":
    unittest.main()
