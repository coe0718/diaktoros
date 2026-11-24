"""#2462: ``hermes dk backup`` waits a bounded time for the backups directory's lock.

``record_future_dated`` (#1905) holds an exclusive ``flock`` on the backups directory while it
scans, reads and rewrites the record. It runs after the archive is written, while ``cmd_backup``
still holds that archive's own lock; a blocking wait there meant a holder that never let go kept
every later backup from its prune, its report and its exit. Past ``backup_fresh.LOCK_WAIT_S`` the
backup now warns that nothing was recorded and exits 0, the archive whole and the record as it was.
Uses ``test_doctor_backup_fresh``'s fixture: a real temporary home and the real ``backup`` command.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import fcntl
import os
import pathlib
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_doctor_backup_fresh as fresh  # noqa: E402
from diaktoros.state import backup, backup_fresh  # noqa: E402

WAIT_S = 0.5            # the bound under test, short so the test is quick


class BoundedWait(fresh.Base):
    run_backup = fresh.FutureDatedRecorded.run_backup

    def hold_directory(self) -> int:
        """Another open file description holding the lock a backup's record takes, as a backup
        that was stopped inside ``record_future_dated`` would."""
        fd = os.open(self.dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def test_a_lock_held_past_the_bound_warns_and_the_backup_exits_0(self):
        self.archive("future.tar.gz", age_days=-3650)       # one the backup would record
        holder = self.hold_directory()
        done, result = threading.Event(), {}

        def run():
            try:
                result["text"] = self.run_backup()            # asserts exit 0
            except BaseException as exc:                      # noqa: BLE001  re-raised below
                result["error"] = exc
            finally:
                done.set()

        # ``create``: before the fix the module had no bound to patch, and the wait was forever.
        with mock.patch.object(backup_fresh, "LOCK_WAIT_S", WAIT_S, create=True):
            started = time.monotonic()
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            finished = done.wait(WAIT_S + 5)
            took = time.monotonic() - started
            fcntl.flock(holder, fcntl.LOCK_UN)       # lets an unbounded wait end, for the report
            thread.join(10)
        self.assertTrue(finished, "the backup was still waiting for the backups directory's lock")
        if "error" in result:
            raise result["error"]
        self.assertGreaterEqual(took, WAIT_S)
        text = result["text"]
        self.assertIn("backup written: ", text)
        self.assertIn("warning: could not record future-dated archives", text)
        self.assertIn("doctor may later count one as a recent backup", text)
        self.assertNotIn("recorded, so doctor", text)       # it never claims a record it did not write
        self.assertFalse((self.dir / backup_fresh.FUTURE_RECORD).exists())
        written = list(self.dir.glob("diaktoros-backup-*.tar.gz"))
        self.assertEqual(len(written), 1)
        self.assertEqual(backup.verify(written[0])[1], [])

    def test_a_lock_let_go_within_the_bound_is_waited_for(self):
        """The bound only ends a wait that outlasts it: a holder that lets go in time is waited
        for, and the record is written (#1905 review F1 still holds)."""
        future = self.archive("future.tar.gz", age_days=-3650)
        holder = self.hold_directory()
        timer = threading.Timer(0.2, fcntl.flock, (holder, fcntl.LOCK_UN))
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(backup_fresh, "LOCK_WAIT_S", 5.0, create=True):
            self.assertEqual(backup_fresh.record_future_dated(), [future])
        self.assertTrue((self.dir / backup_fresh.FUTURE_RECORD).is_file())


if __name__ == "__main__":
    unittest.main()
