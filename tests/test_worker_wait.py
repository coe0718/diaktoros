"""#1808 F4: ``end_lingering_workers`` ends only a worker that is waiting, never one at work.

A real process names the test root on its command line, as a detached worker does, and a real
SQLite ledger says whether any run is still pending, claimed or running.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from contextlib import closing
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from diaktoros import run_supervisor
import worker_wait


class EndLingeringWorkers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "runs.sqlite"
        # Named by the root on its command line, like a detached worker.
        self.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                      str(self.root)])
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.kill)

    def ledger(self, state, error=None):
        self.db.unlink(missing_ok=True)                     # a fresh ledger with one run
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE runs(id TEXT, state TEXT, error TEXT)")
            con.execute("INSERT INTO runs VALUES('r', ?, ?)", (state, error))

    def test_a_worker_with_a_run_at_work_is_left_running_and_the_wait_still_fails(self):
        self.ledger("running")
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(self.proc.poll())                 # not terminated
        with self.assertRaises(AssertionError):
            worker_wait.wait_for_workers(self.root, timeout=0.3)

    def test_a_worker_only_waiting_out_a_backoff_is_ended(self):
        self.ledger("waiting", f"{run_supervisor.CLAIM_READ_FAILED}: 502")
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertEqual(self.proc.wait(10), -signal.SIGTERM)
        worker_wait.wait_for_workers(self.root, timeout=5)

    def test_a_stuck_worker_with_only_finished_runs_is_left_and_the_wait_fails(self):
        # Round 2 F4: nothing waits on a polled hold, so a live process is stuck, not lingering.
        for state in ("succeeded", "failed", "cancelled", "uncertain"):
            self.ledger(state)
            worker_wait.end_lingering_workers(self.root, settle=0.3)
            self.assertIsNone(self.proc.poll(), state)
        with self.assertRaises(AssertionError):
            worker_wait.wait_for_workers(self.root, timeout=0.3)

    def test_a_waiting_run_on_no_polled_hold_is_not_a_linger(self):
        self.ledger("waiting", "held: a maintainer hold")
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(self.proc.poll())

    def test_a_caller_that_rewrote_the_ledger_can_vouch_for_the_workers(self):
        self.ledger("failed")
        worker_wait.end_lingering_workers(self.root, settle=0.3, vouched=True)
        self.assertEqual(self.proc.wait(10), -signal.SIGTERM)

    def test_no_ledger_is_not_idle(self):
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(self.proc.poll())


if __name__ == "__main__":
    unittest.main()
