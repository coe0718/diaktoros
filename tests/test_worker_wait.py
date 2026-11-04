"""#1808 F4, #1811: ``end_lingering_workers`` ends only a process that marks itself as lingering.

Real processes name the test root on their command line, as detached workers do. A lingering
one holds the ``<ledger>.linger.<pid>`` mark ``Supervisor.linger_for_ci`` keeps (here through
the real ``workers.lingering``); the ledger's rows say nothing about which process that is.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from contextlib import closing
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from diaktoros import run_supervisor
from diaktoros.run_supervisor import workers
import worker_wait

HANG = "import time; time.sleep(60)"
# What linger_for_ci does around its wait: hold the mark (the real context manager), then wait.
LINGER = ("import sys, time; from pathlib import Path; "
          "from diaktoros.run_supervisor import workers\n"
          "with workers.lingering(Path(sys.argv[1])):\n"
          "    print('marked', flush=True); time.sleep(60)")


class EndLingeringWorkers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "runs.sqlite"

    def start(self, code, *args):
        # Named by the root on its command line, like a detached worker.
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(
            [str(Path(run_supervisor.__file__).resolve().parents[2])]
            + os.environ.get("PYTHONPATH", "").split(os.pathsep))}
        proc = subprocess.Popen([sys.executable, "-c", code, *args, str(self.root)],
                                stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def hung(self):
        return self.start(HANG)

    def lingerer(self):
        proc = self.start(LINGER, str(self.db))
        self.assertEqual(proc.stdout.readline().strip(), "marked")
        return proc

    def ledger(self, state, error=None):
        self.db.unlink(missing_ok=True)                     # a fresh ledger with one run
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE runs(id TEXT, state TEXT, error TEXT)")
            con.execute("INSERT INTO runs VALUES('r', ?, ?)", (state, error))

    def polled_waiting_row(self):
        self.ledger("waiting", f"{run_supervisor.CLAIM_READ_FAILED}: 502")

    def test_a_marked_lingering_worker_is_ended(self):
        self.polled_waiting_row()
        proc = self.lingerer()
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertEqual(proc.wait(10), -signal.SIGTERM)
        worker_wait.wait_for_workers(self.root, timeout=5)

    def test_a_hung_process_beside_a_polled_waiting_row_and_a_lingerer_still_fails_the_wait(self):
        # #1811: the ledger shows a linger, and one worker is lingering; the other process under
        # the same root is stuck. Only the marked one is ended, so the exit check fails.
        self.polled_waiting_row()
        lingerer, stuck = self.lingerer(), self.hung()
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertEqual(lingerer.wait(10), -signal.SIGTERM)
        self.assertIsNone(stuck.poll())                     # not terminated
        with self.assertRaises(AssertionError):
            worker_wait.wait_for_workers(self.root, timeout=0.3)

    def test_an_unmarked_process_beside_a_polled_waiting_row_is_not_ended(self):
        # The old ledger-only judgement ended this one: the row alone does not name the process.
        self.polled_waiting_row()
        stuck = self.hung()
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(stuck.poll())
        with self.assertRaises(AssertionError):
            worker_wait.wait_for_workers(self.root, timeout=0.3)

    def test_a_worker_with_a_run_at_work_is_left_running_and_the_wait_still_fails(self):
        self.ledger("running")
        stuck = self.hung()
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(stuck.poll())
        with self.assertRaises(AssertionError):
            worker_wait.wait_for_workers(self.root, timeout=0.3)

    def test_a_mark_of_a_process_that_is_gone_or_not_under_the_root_ends_nothing(self):
        stuck = self.hung()
        workers.linger_mark(self.db, 2 ** 22 + 1).write_text("x")      # no such process
        elsewhere = subprocess.Popen([sys.executable, "-c", HANG])      # does not name the root
        self.addCleanup(elsewhere.wait)
        self.addCleanup(elsewhere.kill)
        workers.linger_mark(self.db, elsewhere.pid).write_text("x")
        self.assertEqual(worker_wait.lingering_workers(self.root), set())
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(stuck.poll())
        self.assertIsNone(elsewhere.poll())

    def test_no_ledger_and_no_mark_is_not_a_linger(self):
        stuck = self.hung()
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(stuck.poll())

    def test_end_lingering_workers_has_no_way_to_vouch_for_a_worker(self):
        with self.assertRaises(TypeError):
            worker_wait.end_lingering_workers(self.root, settle=0.1, vouched=True)


class LingerMark(unittest.TestCase):
    def test_the_mark_exists_exactly_while_the_worker_lingers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite"
            self.assertEqual(workers.lingering_pids(db), set())
            with workers.lingering(db):
                self.assertEqual(workers.lingering_pids(db), {os.getpid()})
            self.assertEqual(workers.lingering_pids(db), set())

    def test_the_mark_is_removed_when_the_linger_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite"
            with self.assertRaises(RuntimeError), workers.lingering(db):
                raise RuntimeError("boom")
            self.assertEqual(workers.lingering_pids(db), set())

    def test_a_gone_ledger_directory_leaves_the_worker_unmarked_and_running(self):
        with workers.lingering(Path("/nonexistent-dir-1811/runs.sqlite")):
            pass


if __name__ == "__main__":
    unittest.main()
