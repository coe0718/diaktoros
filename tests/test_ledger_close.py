"""Every ledger connection is closed when its block ends, not when the collector gets to it.

``with sqlite3.connect(...) as con:`` commits or rolls back and leaves the handle open; a
long-lived broker, proxy or supervisor then holds a growing set of open files on the ledger.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import gc
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
import warnings

from review_loop import review_receipt
from review_loop.run_supervisor import Supervisor

HEAD = 'a' * 40
REPO = 'acme/widgets'


def ledger_fds(db: Path) -> list[str]:
    """This process's descriptors open on the ledger or its WAL/SHM files."""
    found = []
    for fd in os.listdir('/proc/self/fd'):
        try:
            target = os.readlink(f'/proc/self/fd/{fd}')
        except OSError:
            continue
        if target.startswith(str(db)):
            found.append(target)
    return found


class LedgerConnectionsClose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'runs.sqlite'

    def exercise(self):
        sup = Supervisor(self.db)
        sup.enqueue('d1', REPO, 7, HEAD, 'reviewer')
        self.assertIsNotNone(sup.get('d1'))
        sup.status()
        sup.post_write_hold(REPO, 7)
        run_id = sup.get('d1')['id']
        with sup._connect() as con:
            con.execute("UPDATE runs SET state='running', generation='g' WHERE id=?", (run_id,))
        ledger = review_receipt.ReceiptLedger(str(self.db), run_id, 'g')
        ledger.claim(2)
        with self.assertRaises(review_receipt.ReceiptDenied):  # a failing transaction closes too
            review_receipt.ReceiptLedger(str(self.db), run_id, 'other').claim(2)
        review_receipt.confirmed_receipts(str(self.db), REPO, 7, HEAD, 'b' * 40)

    def test_no_connection_outlives_its_block(self):
        opened = []
        real = sqlite3.connect

        def tracking(*args, **kwargs):
            con = real(*args, **kwargs)
            opened.append(con)   # a strong reference: only close() can release the handle
            return con

        with mock.patch('sqlite3.connect', tracking):
            self.exercise()
        self.assertGreater(len(opened), 5)
        if os.path.isdir('/proc/self/fd'):
            self.assertEqual(ledger_fds(self.db), [])
        still_open = []
        for con in opened:
            try:
                con.execute('SELECT 1')
            except sqlite3.ProgrammingError:
                continue
            still_open.append(con)
            con.close()
        self.assertEqual(still_open, [], f'{len(still_open)} of {len(opened)} left open')

    def test_no_resource_warning(self):
        caught = []
        hook = sys.unraisablehook
        sys.unraisablehook = lambda u: caught.append(u.exc_value)
        try:
            with warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter('always', ResourceWarning)
                self.exercise()
                gc.collect()
        finally:
            sys.unraisablehook = hook
        leaks = [str(w.message) for w in seen if issubclass(w.category, ResourceWarning)]
        leaks += [repr(e) for e in caught if isinstance(e, ResourceWarning)]
        self.assertEqual(leaks, [])


class CloseFails(sqlite3.Connection):
    """A connection whose close() releases the handle and then reports a failure."""

    def close(self):
        super().close()
        raise sqlite3.OperationalError('close failed')


class ConnectHelper(unittest.TestCase):
    """``ledger.connect`` keeps ``with con:`` transaction semantics and adds the close."""

    def setUp(self):
        from review_loop import ledger
        self.connect = ledger.connect
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'x.sqlite')
        with self.connect(self.db) as con:
            con.execute('CREATE TABLE t(v)')

    def rows(self):
        with self.connect(self.db) as con:
            return [r[0] for r in con.execute('SELECT v FROM t')]

    def test_commits_and_closes(self):
        with self.connect(self.db) as con:
            con.execute('INSERT INTO t VALUES (1)')
        self.assertRaises(sqlite3.ProgrammingError, con.execute, 'SELECT 1')
        self.assertEqual(self.rows(), [1])

    def test_rolls_back_and_closes_on_error(self):
        with self.assertRaises(RuntimeError):
            with self.connect(self.db) as con:
                con.execute('INSERT INTO t VALUES (1)')
                raise RuntimeError('boom')
        self.assertRaises(sqlite3.ProgrammingError, con.execute, 'SELECT 1')
        self.assertEqual(self.rows(), [])

    def test_explicit_begin_immediate_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.connect(self.db, isolation_level=None) as con:
                con.execute('BEGIN IMMEDIATE')
                con.execute('INSERT INTO t VALUES (2)')
                raise RuntimeError('boom')
        self.assertEqual(self.rows(), [])

    def test_row_factory_pragmas_and_uri(self):
        with self.connect(f'file:{self.db}?mode=ro', uri=True, row_factory=sqlite3.Row,
                          pragmas=('busy_timeout=1234',)) as con:
            self.assertEqual(con.execute('PRAGMA busy_timeout').fetchone()[0], 1234)
            self.assertIsInstance(con.execute('SELECT 1 AS one').fetchone(), sqlite3.Row)
            self.assertRaises(sqlite3.OperationalError, con.execute, 'INSERT INTO t VALUES (3)')

    def test_a_failing_close_never_replaces_the_error_in_flight(self):
        with self.assertRaises(RuntimeError) as caught:
            with self.connect(self.db, factory=CloseFails) as con:
                con.execute('INSERT INTO t VALUES (1)')
                raise RuntimeError('boom')
        self.assertEqual(str(caught.exception), 'boom')
        self.assertIn('closing the ledger connection also failed',
                      ' '.join(getattr(caught.exception, '__notes__', [])))
        self.assertRaises(sqlite3.ProgrammingError, con.execute, 'SELECT 1')
        self.assertEqual(self.rows(), [])   # still rolled back

    def test_a_failing_close_with_nothing_in_flight_is_raised(self):
        with self.assertRaisesRegex(sqlite3.OperationalError, 'close failed'):
            with self.connect(self.db, factory=CloseFails) as con:
                con.execute('INSERT INTO t VALUES (1)')
        self.assertEqual(self.rows(), [1])   # committed before the close

    def test_failed_pragma_still_closes(self):
        opened = []
        real = sqlite3.connect
        with mock.patch('sqlite3.connect', lambda *a, **k: opened.append(real(*a, **k)) or opened[-1]):
            with self.assertRaises(sqlite3.OperationalError):
                with self.connect(self.db, pragmas=('not a pragma',)):
                    pass
        self.assertRaises(sqlite3.ProgrammingError, opened[0].execute, 'SELECT 1')


if __name__ == '__main__':
    unittest.main()
