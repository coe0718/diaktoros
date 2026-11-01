"""#780: a delivery-pending breach marker must not hide its head once the cap is no longer spent.

The watchdog retries a pending delivery only while the head is still held (live verdict count at
the cap, or an operator escalation). A raised cap or a dismissed verdict used to leave the marker
in place while the stall scan kept skipping the head. Now the retry clears the marker and the
stall scan only skips a marker that still holds. Driven through ``retry_pending_breaches`` and the
sweep itself, not only the helper.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import config, gate, gh, state as state_mod  # noqa: E402
from diaktoros import watch_mode  # noqa: E402
from scripts import watchdog  # noqa: E402

LONG_AGO = "2020-01-01T00:00:00Z"


def verdict(rid):
    return {"id": rid, "state": "CHANGES_REQUESTED", "commit_id": fg.HEAD,
            "submitted_at": LONG_AGO, "user": {"login": "reviewer", "id": 2}}


class PendingBreachNoLongerHolds(fg.Base):
    def setUp(self):
        super().setUp()
        self.set_push(True)
        self.cap = self.loop["cap"]
        self.reviews = [verdict(i) for i in range(1, self.cap + 1)]
        self.st.breach_set(7, {"pr": 7, "head": fg.HEAD, "rounds": self.cap, "cap": self.cap,
                               "reason": "review cap reached", "at": LONG_AGO,
                               "status": "delivery-pending"})

    def set_cap(self, cap):
        path = config.config_dir() / "one.json"
        data = json.loads(path.read_text())
        data["cap"] = cap
        path.write_text(json.dumps(data))
        self.loop = config.load_id("one")
        self.st = state_mod.state_for(self.loop)

    def retry(self):
        with mock.patch.object(gh, "reviews", return_value=self.reviews), \
                mock.patch.object(gate, "breach") as breach:
            watchdog.retry_pending_breaches(self.loop, self.st, [fg.LIVE])
        return breach

    def sweep(self, retry=True):
        with mock.patch.object(watch_mode, "TEST", True), \
                mock.patch.object(gate, "hooks_armed", return_value=True), \
                mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
                mock.patch.object(gh, "open_prs", return_value=[fg.LIVE]), \
                mock.patch.object(gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gh, "reviews", return_value=self.reviews), \
                mock.patch.object(gate, "breach", return_value="retry"), \
                mock.patch.object(watchdog.routes, "fire"), \
                mock.patch.object(watchdog.observer, "notify"), \
                mock.patch.object(watchdog.observer, "retry", return_value=0), \
                mock.patch.object(watchdog.observer, "flush"), \
                (mock.patch.object(watchdog, "retry_pending_breaches") if not retry
                 else contextlib.nullcontext()):
            return "\n".join(watchdog.sweep_loop(self.loop, self.st))

    def test_a_spent_cap_still_retries_and_keeps_the_marker(self):
        breach = self.retry()
        breach.assert_called_once()
        self.assertEqual(self.st.breach_get(7)["status"], "delivery-pending")

    def test_a_raised_cap_clears_the_pending_marker_instead_of_leaving_it(self):
        self.set_cap(self.cap + 2)
        breach = self.retry()
        breach.assert_not_called()
        self.assertEqual(self.st.breach_get(7), {})

    def test_a_dismissed_verdict_clears_the_pending_marker(self):
        self.reviews = self.reviews[:-1]
        self.retry()
        self.assertEqual(self.st.breach_get(7), {})

    def test_a_delivery_in_flight_is_not_cleared(self):
        marker = self.st.breach_get(7)
        self.st.breach_all()
        path = self.st.breach
        data = json.loads(Path(path).read_text())
        data[f"{fg.REPO}#7"] = {**marker, "delivery_token": "t", "delivery_at": time.time()}
        Path(path).write_text(json.dumps(data))
        self.set_cap(self.cap + 2)
        self.retry()
        self.assertEqual(self.st.breach_get(7)["status"], "delivery-pending")

    def test_a_future_delivery_stamp_is_not_a_delivery_in_flight(self):
        # #795: a delivery_at ahead of the clock must not hold the lease; the retry clears it.
        marker = self.st.breach_get(7)
        data = json.loads(Path(self.st.breach).read_text())
        data[f"{fg.REPO}#7"] = {**marker, "delivery_token": "t", "delivery_at": time.time() + 86400}
        Path(self.st.breach).write_text(json.dumps(data))
        self.set_cap(self.cap + 2)
        self.retry()
        self.assertEqual(self.st.breach_get(7), {})

    def test_a_slightly_future_delivery_stamp_is_still_in_flight(self):
        # #942: modest clock skew must not let the retry clear a marker whose delivery is live.
        marker = self.st.breach_get(7)
        data = json.loads(Path(self.st.breach).read_text())
        data[f"{fg.REPO}#7"] = {**marker, "delivery_token": "t", "delivery_at": time.time() + 5}
        Path(self.st.breach).write_text(json.dumps(data))
        self.set_cap(self.cap + 2)
        self.retry()
        self.assertEqual(self.st.breach_get(7)["status"], "delivery-pending")

    def test_the_stall_scan_no_longer_skips_a_marker_that_does_not_hold(self):
        # Reaches the scan with the marker still present (retry stubbed out): the head is
        # reported, not skipped as "a failed delivery".
        self.set_cap(self.cap + 2)
        self.sweep(retry=False)           # arm the stall clock
        watch = self.st.watch()
        watch["heads"]["7"]["observed_at"] = time.time() - 36000
        self.st.watch_save(watch)
        self.assertIn("#7 ", self.sweep(retry=False))
        self.assertEqual(self.st.breach_get(7)["status"], "delivery-pending")

    def test_the_sweep_clears_the_marker_and_reports_the_head(self):
        self.set_cap(self.cap + 2)
        self.sweep()
        self.assertEqual(self.st.breach_get(7), {})
        watch = self.st.watch()
        watch["heads"]["7"]["observed_at"] = time.time() - 36000
        self.st.watch_save(watch)
        self.assertIn("fixer never pushed", self.sweep())

    def test_a_marker_that_still_holds_stays_invisible_to_the_stall_scan(self):
        self.sweep(retry=False)
        watch = self.st.watch()
        watch["heads"]["7"]["observed_at"] = time.time() - 36000
        self.st.watch_save(watch)
        self.assertNotIn("#7 ", self.sweep(retry=False))


if __name__ == "__main__":
    unittest.main()
