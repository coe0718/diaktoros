"""The receipt barrier beside the ledger (``ledger.owe``; #2634 review F8, #2643).

Each change is a line appended to the claim lock's file and synced. A short write is finished,
not taken as done; a write that makes no progress raises. An update cut short never loses an id
already marked. An add cut short may have stopped inside its id, so it holds every id it begins
until the writer that cut it ends it whole; a clear cut short is not read. The barrier fails
closed.
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

    def cut(self, data: bytes):
        with open(self.path, "ab") as f:        # a crash mid-line: no newline
            f.write(data)

    def held(self, *ids, cuts=()):
        self.assertEqual(ledger._owing(self.lock), (set(ids), list(cuts)))

    def test_a_short_write_is_finished(self):
        real = os.pwrite

        def one_byte(fd, data, at):        # writes, and reports, one byte at a time
            return real(fd, data[:1], at)
        ledger.owe(self.lock, "run-a")
        with mock.patch.object(ledger.os, "pwrite", one_byte):
            ledger.owe(self.lock, "run-b")
        self.held("run-a", "run-b")

    def test_a_write_that_makes_no_progress_raises(self):
        with mock.patch.object(ledger.os, "pwrite", return_value=0), \
                self.assertRaises(OSError):
            ledger.owe(self.lock, "run-a")

    def test_an_add_cut_inside_its_id_holds_every_id_it_begins(self):
        # #2643: "+56ad" may be any id that starts 56ad; "+" alone, any id at all.
        ledger.owe(self.lock, "run-a")
        self.cut(b"+56ad")
        self.held("run-a", cuts=["56ad"])
        ledger.owe(self.lock, "run-c")          # another writer ends it: it stays a cut add
        self.held("run-a", "run-c", cuts=["56ad"])
        ledger.owe(self.lock, "run-a", False)
        ledger.owe(self.lock, "run-c", False)
        self.held(cuts=["56ad"])                # and the log is not emptied
        self.cut(b"+")
        self.held(cuts=["56ad", ""])

    def test_an_add_cut_before_its_newline_holds_its_id(self):
        self.cut(b"+run-a")                     # #2634 review F8: all but the newline
        self.held(cuts=["run-a"])

    def test_the_writer_that_cut_its_add_ends_it_whole(self):
        self.cut(b"+run-")
        ledger.owe(self.lock, "run-a", own_cut=True)
        self.held("run-a")
        ledger.owe(self.lock, "run-a", False)
        self.assertEqual(self.path.read_bytes(), b"")

    def test_an_own_cut_that_is_not_its_own_stays(self):
        self.cut(b"+run-b")                     # not a beginning of "+run-a"
        ledger.owe(self.lock, "run-a", own_cut=True)
        self.held("run-a", cuts=["run-b"])

    def test_an_ending_cut_short_lifts_nothing(self):
        self.cut(b"+run-")
        ledger.owe(self.lock, "run-a")
        self.cut(b"*run")                        # the end of a cut add, itself cut short
        ledger.owe(self.lock, "run-b")
        self.held("run-a", "run-b", cuts=["run-"])

    def test_a_clear_cut_short_leaves_its_id_held(self):
        ledger.owe(self.lock, "run-a")
        ledger.owe(self.lock, "run-b")
        self.cut(b"-run-a")
        self.held("run-a", "run-b")
        ledger.owe(self.lock, "run-c")
        self.held("run-a", "run-b", "run-c")

    def test_clearing_the_last_id_empties_the_file(self):
        ledger.owe(self.lock, "run-a")
        ledger.owe(self.lock, "run-b")
        ledger.owe(self.lock, "run-a", False)
        self.held("run-b")
        ledger.owe(self.lock, "run-b", False)
        self.assertEqual(self.path.read_bytes(), b"")

    def test_an_id_the_log_cannot_hold_is_refused(self):
        for bad in ("", "a\nb", "a~"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ledger.owe(self.lock, bad)


if __name__ == "__main__":
    unittest.main()
