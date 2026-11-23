"""#2426 (review on #2575): a ledger COMMIT that fails is a write failure, not an unreadable ledger.

A deferred foreign key is checked only at COMMIT, so every UPDATE succeeds and the COMMIT itself
fails. The refusal must say the ledger could not be written, and no row may move.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from diaktoros.state import migrate

OLD, NEW = "owner/repo", "owner/renamed"
_CONNECT = sqlite3.connect


def _connect_with_foreign_keys(*args, **kwargs):
    con = _CONNECT(*args, **kwargs)
    con.execute("PRAGMA foreign_keys=ON")
    return con


class CommitFailure(unittest.TestCase):
    def ledger(self) -> Path:
        path = Path(tempfile.mkdtemp()) / "ledger.db"
        con = _CONNECT(path)
        con.execute("CREATE TABLE repos (name TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE runs (id INTEGER, repo TEXT REFERENCES repos(name) "
                    "DEFERRABLE INITIALLY DEFERRED, state TEXT)")
        con.execute("INSERT INTO repos VALUES (?)", (OLD,))
        con.execute("INSERT INTO runs VALUES (1, ?, 'done')", (OLD,))
        con.commit()
        con.close()
        return path

    def test_a_failed_commit_is_a_write_failure_and_moves_nothing(self):
        path = self.ledger()
        with mock.patch.object(migrate.sqlite3, "connect", _connect_with_foreign_keys):
            moved, refused = migrate._move_ledger(path, OLD, NEW, dry_run=False)
        self.assertEqual(moved, 0)
        self.assertIn("could not be written (FOREIGN KEY constraint failed)", refused)
        self.assertNotIn("could not be read", refused)
        con = _CONNECT(path)
        self.assertEqual(con.execute("SELECT repo FROM runs").fetchall(), [(OLD,)])
        con.close()


if __name__ == "__main__":
    unittest.main()
