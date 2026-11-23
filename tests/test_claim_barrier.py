"""The receipt barrier beside the ledger (``ledger.owe``; #2634 review, rounds 7-8, F8).

Each change is a line appended to the claim lock's file and synced. A short write is finished,
not taken as done; a write that makes no progress raises. An update cut short never loses an id
already marked, an add cut short is read as ended, and a clear cut short is not read, so
the barrier fails closed.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from diaktoros.state import ledger


class Barrier(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ledger.db.claims.lock"
        self.lock = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, self.lock)

    def test_a_short_write_is_finished(self):
        real = os.pwrite

        def one_byte(fd, data, at):        # writes, and reports, one byte at a time
            return real(fd, data[:1], at)
        ledger.owe(self.lock, "run-a")
        with mock.patch.object(ledger.os, "pwrite", one_byte):
            ledger.owe(self.lock, "run-b")
        self.assertEqual(ledger._owing(self.lock), {"run-a", "run-b"})

    def test_a_write_that_makes_no_progress_raises(self):
        with mock.patch.object(ledger.os, "pwrite", return_value=0), \
                self.assertRaises(OSError):
            ledger.owe(self.lock, "run-a")

    def test_an_add_cut_short_loses_no_id_already_marked(self):
        ledger.owe(self.lock, "run-a")
        with open(self.path, "ab") as f:
            f.write(b"+run-b-cut")             # a crash mid-line: no newline
        self.assertEqual(ledger._owing(self.lock), {"run-a", "run-b-cut"})   # read as ended
        ledger.owe(self.lock, "run-c")
        self.assertEqual(ledger._owing(self.lock), {"run-a", "run-b-cut", "run-c"})

    def test_an_add_cut_before_its_newline_holds_its_id(self):
        # Round 8, F8: all but the newline written, then nothing more.
        with open(self.path, "ab") as f:
            f.write(b"+run-a")
        self.assertEqual(ledger._owing(self.lock), {"run-a"})

    def test_a_clear_cut_short_leaves_its_id_held(self):
        ledger.owe(self.lock, "run-a")
        ledger.owe(self.lock, "run-b")
        with open(self.path, "ab") as f:
            f.write(b"-run-a")                  # a crash before its newline
        self.assertEqual(ledger._owing(self.lock), {"run-a", "run-b"})

    def test_clearing_the_last_id_empties_the_file(self):
        ledger.owe(self.lock, "run-a")
        ledger.owe(self.lock, "run-b")
        ledger.owe(self.lock, "run-a", False)
        self.assertEqual(ledger._owing(self.lock), {"run-b"})
        ledger.owe(self.lock, "run-b", False)
        self.assertEqual(self.path.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
