"""Issue #183: one watchdog sweep per loop at a time (non-blocking per-loop flock)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)

import fcntl
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import gh, state as state_mod  # noqa: E402
from scripts import watchdog  # noqa: E402

DEAD = gh.Response(None, 'HTTP 401 {"message":"Bad credentials"}', 401, {})


class SweepLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = {"id": "widgets", "repo": "acme/widgets", "read_token": "rev-coach",
                     "state_dir": str(pathlib.Path(self.tmp.name) / "state"), "cooldown_h": 6,
                     "ttl_min": 45, "inflight_ttl_min": 10,
                     "seats": {"reviewer": {"route": "widgets-review"},
                               "fixer": {"route": "widgets-fix"}}}
        self.st = state_mod.state_for(self.loop)

    def patches(self, probe):
        return (mock.patch.object(watchdog, "TEST", False),
                mock.patch.object(watchdog.gate, "hooks_read",
                                  return_value=(None, "HTTP 401 Bad credentials")),
                mock.patch.object(watchdog.gh, "auth_probe", side_effect=probe),
                mock.patch.object(watchdog.route_intent, "heal", return_value=[]),
                mock.patch.object(watchdog.gh, "open_prs"),
                mock.patch.object(watchdog.observer, "retry", return_value=0),
                mock.patch.object(watchdog.observer, "flush"))

    def run_with(self, probe, body):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self.patches(probe):
                stack.enter_context(p)
            return body()

    def test_two_concurrent_sweeps_send_one_alert(self):
        inside, release = threading.Event(), threading.Event()

        def probe(*a, **k):
            inside.set()
            self.assertTrue(release.wait(10))
            return DEAD

        results = {}

        def first():
            results["first"] = watchdog.sweep_loop(self.loop, self.st)

        def body():
            t = threading.Thread(target=first)
            t.start()
            self.assertTrue(inside.wait(10))
            with mock.patch.object(watchdog, "log") as log:
                results["second"] = watchdog.sweep_loop(self.loop, self.st)   # skips cleanly
            log.assert_called_once_with("sweep already running for widgets")
            release.set()
            t.join(10)

        self.run_with(probe, body)
        alerts = [l for l in results["first"] + results["second"] if "cannot read GitHub" in l]
        self.assertEqual(len(alerts), 1, results)
        self.assertEqual(results["second"], [])

    def test_lock_released_when_sweep_raises(self):
        def boom(*a, **k):
            raise RuntimeError("boom")

        def body():
            with self.assertRaises(RuntimeError):
                watchdog.sweep_loop(self.loop, self.st)
        self.run_with(boom, body)
        fd = open(self.st.dir / "sweep.lock", "a+")
        self.addCleanup(fd.close)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)    # raises if still held

    def test_sequential_sweeps_both_run(self):
        self.run_with(lambda *a, **k: DEAD, lambda: watchdog.sweep_loop(self.loop, self.st))
        self.run_with(lambda *a, **k: DEAD, lambda: watchdog.sweep_loop(self.loop, self.st))


if __name__ == "__main__":
    unittest.main()
