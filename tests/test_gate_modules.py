"""The gate package (#1082): ``gate.py`` is ``gate/__init__.py``, one level down.

The gate starts repository scripts by path, found from its own file. Each start is best effort
(a failure is only logged), so a path left pointing at the old flat layout would fail quietly.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import conflict_scan, gate  # noqa: E402


class ScriptsTheGateStarts(unittest.TestCase):
    def started(self, call, how: str) -> pathlib.Path:
        with mock.patch.object(gate.subprocess, how) as start:
            start.return_value = mock.MagicMock(stdout="", stderr="", returncode=0)
            call()
        return pathlib.Path(start.call_args.args[0][1])

    def test_each_script_the_gate_starts_is_the_repositorys_own(self):
        loop = {"id": "widgets"}
        with mock.patch.object(gate.gh, "remaining", return_value=None):
            drain = self.started(lambda: gate.drain_seat(loop, "reviewer"), "run")
        with mock.patch.object(conflict_scan, "request"):
            scan = self.started(lambda: gate.schedule_conflict_scan(loop, None), "Popen")
        with mock.patch.object(gate, "log"):
            cleanup = self.started(lambda: gate.reclaim(loop, 7, "merged"), "run")
        self.assertEqual([drain, scan, cleanup], [ROOT / "scripts" / "watchdog.py",
                                                  ROOT / "scripts" / "watchdog.py",
                                                  ROOT / "scripts" / "cleanup.py"])
        for path in (drain, scan, cleanup):
            self.assertTrue(path.is_file(), path)


if __name__ == "__main__":
    unittest.main()
