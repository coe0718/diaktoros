"""Issue #1066: an explicit ``null`` (or non-numeric) ``cooldown_h`` or ``marker_grace_min``
in a loop's config must not abort the sweep that watches it.

``config.normalize`` only fills defaults, so a raw ``"cooldown_h": null`` survives loading
(the key exists, so ``.get("cooldown_h", 6)`` returns ``None``), and the sweep crashed with
``TypeError: float() argument must be... not NoneType`` instead of using the default. The
safe ``or 6`` pattern already used by the stall paths (watchdog.py:956/963/995) is what the
budget/health/stall reads now use too.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)

import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import gh, state as state_mod  # noqa: E402
from diaktoros import watch_mode  # noqa: E402
from diaktoros import watch_health  # noqa: E402
from scripts import watchdog  # noqa: E402

DEAD = gh.Response(None, "HTTP 401 Bad credentials", 401, {})


class NullGraceTest(unittest.TestCase):
    """A loop whose config carries explicit nulls still gets watched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Explicit nulls: the keys exist, so a plain .get(key, default) returns None.
        self.loop = {"id": "widgets", "repo": "acme/widgets", "read_token": "rev-coach",
                     "state_dir": str(pathlib.Path(self.tmp.name) / "state"),
                     "cooldown_h": None, "marker_grace_min": None,
                     "ttl_min": 45, "inflight_ttl_min": 10,
                     "seats": {"reviewer": {"route": "widgets-review"},
                               "fixer": {"route": "widgets-fix"}}}
        self.st = state_mod.state_for(self.loop)
        self.now = time.time()

    def sweep(self, hooks=(None, "HTTP 401 Bad credentials"), probe=DEAD):
        def open_prs(loop, errors=None):
            return None
        with mock.patch.object(watch_mode, "TEST", False), \
             mock.patch.object(watchdog.gate, "hooks_read", return_value=hooks), \
             mock.patch.object(watchdog.gh, "auth_probe", return_value=probe), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(watchdog.gh, "open_prs", side_effect=open_prs), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"), \
             mock.patch.object(watchdog.time, "time", return_value=self.now):
            return watchdog.sweep_loop(self.loop, self.st)

    def test_sweep_uses_default_cooldown_and_marker_grace_instead_of_crashing(self):
        # Before the fix this raised TypeError in read_health before any stall scan ran.
        lines = self.sweep()
        alert = [line for line in lines if "cannot read GitHub" in line]
        self.assertEqual(len(alert), 1, lines)
        self.assertIn("as rev-coach: HTTP 401", alert[0])

    def test_read_failed_counts_and_alerts_on_the_default_cooldown(self):
        with mock.patch.object(watch_mode, "TEST", False):
            watch: dict = {}
            for _ in range(watch_health.READ_FAILURE_SWEEPS - 1):
                lines, alerted = watch_health._read_failed(
                    self.loop, watch, self.now, "rev-coach", "HTTP 502 bad gateway", 502, "")
                self.assertEqual(lines, [])  # below the consecutive threshold, quiet
            lines, alerted = watch_health._read_failed(
                self.loop, watch, self.now, "rev-coach", "HTTP 502 bad gateway", 502, "")
            self.assertEqual(alerted, True)
            self.assertTrue(any("cannot read GitHub" in line for line in lines), lines)

    def test_read_failed_alerts_a_401_at_once_on_the_default_cooldown(self):
        with mock.patch.object(watch_mode, "TEST", False):
            lines, alerted = watch_health._read_failed(
                self.loop, {}, self.now, "rev-coach", "HTTP 401 Bad credentials", 401, "")
            self.assertEqual(alerted, True)
            self.assertTrue(any("cannot read GitHub" in line for line in lines), lines)

    def test_sweep_budget_spent_alerts_after_the_streak_on_the_default_cooldown(self):
        with mock.patch.object(watch_mode, "TEST", False):
            for _ in range(watch_health.READ_FAILURE_SWEEPS - 1):
                lines = watch_health.sweep_budget_spent(self.loop, self.st, 5.0, TimeoutError("slow"))
                self.assertEqual(lines, [])
            lines = watch_health.sweep_budget_spent(self.loop, self.st, 5.0, TimeoutError("slow"))
            self.assertTrue(any("sweep(s) in a row" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
