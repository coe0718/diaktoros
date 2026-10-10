"""#601: every run-ledger connection waits LOCK_WAIT_S for a lock, and a lock timeout in a turn
is retried and says which operation it hit.

Live, 2026-10-10: issue-fix turns failed with "OperationalError: database is locked" while the
host ran heavy test suites; every connection gave up after 10 s, and the log could not say where.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import ast
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import ledger, review_receipt, run_supervisor  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WAIT_MS = ledger.LOCK_WAIT_S * 1000


class Waits(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "state" / "runs.sqlite"
        self.host = Supervisor(self.db)            # creates the ledger

    def busy_ms(self, cm):
        with cm as con:
            return con.execute("PRAGMA busy_timeout").fetchone()[0]

    def test_the_wait_is_a_minute(self):
        self.assertEqual(ledger.LOCK_WAIT_S, 60)

    def test_ledger_connect_waits_the_full_time_when_the_caller_names_no_wait(self):
        with ledger.connect(self.db) as con:
            self.assertEqual(con.execute("PRAGMA busy_timeout").fetchone()[0], WAIT_MS)

    def test_host_worker_and_receipt_connections_wait_the_full_time(self):
        worker = Supervisor(self.db, create=False)
        receipt = review_receipt.ReceiptLedger(str(self.db), "run", "gen")
        for name, cm in (("host", self.host._connect()), ("worker", worker._connect()),
                         ("receipt", receipt._connect())):
            with self.subTest(name):
                self.assertEqual(self.busy_ms(cm), WAIT_MS)

    def test_every_ledger_connection_uses_the_shared_wait(self):
        # One constant, not a number per module (the old 5, 10 and 30 s waits), and no
        # connection left on SQLite's default 5 s (#605 review: trace._rows had no timeout).
        # The only exceptions are SQLite files no other connection can hold: :memory: and a new
        # private file, marked so on its line.
        modules = ("run_supervisor", "ci_fix", "review_receipt", "stats", "cli", "migrate",
                   "backup", "trace", "ledger")
        for name in modules:
            path = ROOT / "diaktoros" / f"{name}.py"
            source = path.read_text()
            lines = source.splitlines()
            self.assertNotRegex(source, r"busy_timeout=\d", name)
            for node in ast.walk(ast.parse(source)):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "connect"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in ("sqlite3", "ledger")):
                    continue
                keywords = {k.arg: ast.unparse(k.value) for k in node.keywords}
                first = ast.unparse(node.args[0]) if node.args else ""
                where = f"{name}.py:{node.lineno}"
                with self.subTest(where):
                    if first in ("':memory:'", '":memory:"') or "private new file" in \
                            lines[node.end_lineno - 1]:
                        continue
                    if node.func.value.id == "ledger" and "opener" in keywords:
                        continue                    # the opener sets the wait itself
                    if None in keywords:
                        continue                    # ledger.connect's pass-through (**kwargs)
                    self.assertIn("LOCK_WAIT_S", keywords.get("timeout", ""), where)


class LockTimeouts(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "state" / "runs.sqlite"
        self.host = Supervisor(self.db)
        holder = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")           # another connection holds the write lock
        self.addCleanup(holder.execute, "ROLLBACK")
        # A short wait for the test; production waits LOCK_WAIT_S.
        for name, value in (("LOCK_WAIT_S", 0.05), ("BUSY_TIMEOUT", "busy_timeout=50")):
            patch = mock.patch.object(ledger, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def caught(self):
        try:
            self.host.record_thinking("run", "owner", "effort:high")   # a real ledger write
        except sqlite3.OperationalError as exc:
            return exc
        self.fail("the lock was not held")

    def test_a_lock_timeout_names_the_operation_it_hit(self):
        exc = self.caught()
        self.assertTrue(ledger.locked(exc))
        self.assertEqual(ledger.where(exc), " (during record_thinking)")

    def test_other_errors_are_not_lock_timeouts(self):
        other = sqlite3.OperationalError("no such table: runs")
        self.assertFalse(ledger.locked(other))
        self.assertEqual(ledger.where(other), "")
        self.assertEqual(ledger.where(ValueError("database is locked")), "")

    def test_a_lock_timeout_is_retried_and_other_sqlite_errors_are_not(self):
        self.assertTrue(run_supervisor.retryable(self.caught()))
        self.assertFalse(run_supervisor.retryable(sqlite3.OperationalError("no such table: runs")))


if __name__ == "__main__":
    unittest.main()
