"""Review on #2576, F4: ``merge_hold.take`` reads its clock once the writer lock is held, as
``confirm`` does. Taking the lock may wait up to ``ledger.LOCK_WAIT_S``; another push's hold that
lapsed during that wait holds nothing, so the take succeeds, and its own hold is stamped with the
time it was taken under the lock (a pre-lock stamp would start it already partly spent).

The sqlite ledger is real; the wait is simulated by moving the module's clock forward as
``BEGIN IMMEDIATE`` runs.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from diaktoros import run_supervisor
from diaktoros.run_supervisor import merge_hold

REPO, PR, WAIT = "acme/widgets", 7, 60.0
STATES = ("queued", "running")


class TakeAfterLock(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "runs.sqlite"
        run_supervisor.Supervisor(self.db)

    def take_after_wait(self, age: float):
        """Another push's hold, ``age`` seconds old; then a take whose lock is granted ``WAIT``
        seconds later. What it returned, and its hold's stamp against the time it began."""
        old, _ = merge_hold.take(self.db, REPO, PR, "h0", STATES)
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE merge_holds SET created=created-? WHERE token=?", (age, old))
        skew, real_connect = [0.0], merge_hold.ledger.connect

        def wait_at_lock(sql):
            if sql.startswith("BEGIN IMMEDIATE"):
                skew[0] = WAIT

        @contextlib.contextmanager
        def connect(*args, **kwargs):
            with real_connect(*args, **kwargs) as con:
                con.set_trace_callback(wait_at_lock)
                yield con
        began = time.time()
        with mock.patch.object(merge_hold.ledger, "connect", connect), \
                mock.patch.object(merge_hold, "time",
                                  SimpleNamespace(time=lambda: time.time() + skew[0])):
            got = merge_hold.take(self.db, REPO, PR, "h1", STATES)
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            stamp = con.execute("SELECT created FROM merge_holds WHERE repo=? AND pr=?",
                                (REPO, PR)).fetchone()[0]
        return got, stamp - began

    def test_a_hold_that_lapses_while_take_waits_for_the_lock_is_replaced(self):
        # 270 s old at the start, 330 s once the lock is granted: lapsed, so it holds nothing.
        (token, why), offset = self.take_after_wait(merge_hold.HOLD_S - 30)
        self.assertEqual(why, "")
        self.assertTrue(token)
        self.assertGreaterEqual(offset, WAIT)       # stamped when taken, under the lock

    def test_a_hold_still_live_once_the_lock_is_granted_still_holds(self):     # control
        (token, why), _ = self.take_after_wait(merge_hold.HOLD_S - WAIT - 30)
        self.assertEqual((token, why), ("", "another merge of the base is being pushed"))


if __name__ == "__main__":
    unittest.main()
