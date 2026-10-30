"""#601: every run-ledger connection waits LOCK_WAIT_S for a lock, and a lock timeout in a turn
is retried and says which operation it hit.

Live, 2026-10-10: issue-fix turns failed with "OperationalError: database is locked" while the
host ran heavy test suites; every connection gave up after 10 s, and the log could not say where.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import ast
import re
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
        # Found, not listed (#676): a list kept naming `cli` after its ledger connections moved
        # to cli_operations, and nothing checked the module that had them.
        # A ``ledger.read_only`` open counts: it carries the wait itself (B5).
        paths = sorted(path for path in (ROOT / "diaktoros").rglob("*.py")
                       if re.search(r"\b(?:sqlite3|ledger)\.connect\(|\bread_only\(",
                                    path.read_text()))
        names = {path.relative_to(ROOT / "diaktoros").with_suffix("").as_posix() for path in paths}
        # #644: the ledger reads the escalation selector needs moved with it from cli/operations
        # to the shared one_shot_action owner, so the enumerated set moved too.
        self.assertLessEqual({"run_supervisor/__init__", "ci_fix", "review_receipt", "stats", "migrate",
                              "backup", "trace", "ledger", "one_shot_action"}, names)
        for path in paths:
            name = path.relative_to(ROOT / "diaktoros").with_suffix("").as_posix()
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


class ReadOnly(unittest.TestCase):
    """Duplication audit (B5): every read-only open is ``ledger.read_only``, with the path quoted.
    A hand-written ``file:{db}?mode=ro`` read ``a#b/runs.sqlite`` as the file ``a`` (the rest is
    a URI fragment), created it, and failed on its missing tables."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "a#b?c" / "runs.sqlite"
        Supervisor(self.db).enqueue("d1", "acme/widgets", 7, "a" * 40, "reviewer")

    def test_a_hash_in_the_path_reads_that_ledger_and_creates_nothing(self):
        from diaktoros import stats
        before = sorted(p.name for p in self.root.iterdir())
        for immutable in (False, True):
            with self.subTest(immutable=immutable):
                con = ledger.read_only(self.db, immutable=immutable)
                try:
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
                    with self.assertRaises(sqlite3.OperationalError):
                        con.execute("DELETE FROM runs")              # and it is read-only
                finally:
                    con.close()
        # A reader that went through it: the report sees the run, not an empty stray file.
        self.assertEqual(review_receipt.partial_view(self.db, "nope"), "")
        self.assertIsNotNone(stats.ledger(self.db, "acme/widgets", 0))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    def test_a_missing_ledger_is_never_created_in_either_mode(self):
        # #1471 review: ``immutable=1`` alone opens a missing file by creating it.
        gone = self.root / "gone" / "runs.sqlite"
        gone.parent.mkdir()
        for immutable in (False, True):
            with self.subTest(immutable=immutable), self.assertRaises(sqlite3.OperationalError):
                ledger.read_only(gone, immutable=immutable)
        # The report reader checks, then opens: a ledger removed in between is absent, not new.
        with mock.patch.object(Path, "is_file", return_value=True):
            self.assertIsNone(run_supervisor.views.read_only_connect(gone))
        self.assertEqual(list(gone.parent.iterdir()), [])

    @staticmethod
    def uri_sites(source: str) -> list[tuple[int, str | None]]:
        """Each string in ``source`` naming a read-only URI option, with its enclosing function."""
        sites = []

        def visit(node, func):
            for child in ast.iter_child_nodes(node):
                if (isinstance(child, ast.Constant) and isinstance(child.value, str)
                        and re.search(r"mode=ro|immutable=1", child.value)):
                    sites.append((child.lineno, func))
                inner = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                visit(child, child.name if inner else func)

        visit(ast.parse(source), None)
        return sites

    def stray(self, path: Path, source: str) -> list[tuple[int, str | None]]:
        """The read-only URI strings in ``source`` outside ``ledger.read_only`` itself."""
        allowed = {"read_only"} if path == ROOT / "diaktoros" / "ledger.py" else set()
        return [site for site in self.uri_sites(source) if site[1] not in allowed]

    def test_no_read_only_uri_is_written_outside_ledger_read_only(self):
        for path in [*(ROOT / "diaktoros").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]:
            with self.subTest(path.relative_to(ROOT).as_posix()):
                self.assertEqual(self.stray(path, path.read_text()), [])
        # The exemption is the helper, not its file (#1471 review).
        ledger_py = ROOT / "diaktoros" / "ledger.py"
        extra = ledger_py.read_text() + '\n\ndef another(path):\n    return f"file:{path}?mode=ro"\n'
        self.assertEqual([func for _, func in self.stray(ledger_py, extra)], ["another"])


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

    def test_a_frame_in_a_subpackage_names_the_operation_too(self):
        # #717: where() once counted only files directly in diaktoros/, so a timeout inside
        # cli/, config/ or run_supervisor/ lost its "(during ...)" since those became packages.
        source = ROOT / "diaktoros" / "cli" / "operations.py"
        namespace = {"sqlite3": sqlite3}
        exec(compile("def a_cli_operation():\n"
                     "    raise sqlite3.OperationalError('database is locked')\n",
                     str(source), "exec"), namespace)
        try:
            namespace["a_cli_operation"]()
        except sqlite3.OperationalError as exc:
            self.assertEqual(ledger.where(exc), " (during a_cli_operation)")

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
