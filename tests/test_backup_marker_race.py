"""#2322: a backup that races a ``migrate`` never publishes half-moved records as a clean archive.

``create`` refuses while ``migrate``'s pause marker exists (#1054), but it checked only before the
archive's first byte. A backup that passed that check and then raced a migrate, which wrote its
marker and moved state while the archive was being written, read the half-moved records and
still wrote ``migrating: None`` into its manifest: ``restore`` takes that for a clean archive and
resumes the loop unpaused on them. These tests run the real ``hermes dk migrate`` (its pause, its
own backup) on a second thread inside the backup's write, and check the archive is refused.
Uses the ``test_backup`` fixture: a real temporary home, ledger, state files and route registry.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import os
import sys
import threading
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_backup as tb  # noqa: E402
from diaktoros import cli  # noqa: E402
from diaktoros.state import backup, migrate  # noqa: E402
from diaktoros.cli import lifecycle as cli_lifecycle  # noqa: E402


class MigrateDuringTheWrite(tb.Base):
    def setUp(self):
        super().setUp()
        self.moved, self.finish = threading.Event(), threading.Event()
        self.addCleanup(self.finish.set)          # a failed test never leaves migrate waiting
        self.migrated: dict = {}

    def half_move(self, *args, **kw) -> list[str]:
        """``migrate``'s repository step, cut off halfway: one state file is moved under the
        marker, then it waits (the marker still held) until the racing backup is done."""
        os.replace(self.state / "queue.json", self.state / "queue.moved.json")
        self.moved.set()
        self.finish.wait(10)
        return []

    def run_migrate(self) -> None:
        try:
            self.migrated["code"] = cli.cmd_migrate(argparse.Namespace(dry_run=False))
        except BaseException as exc:                       # noqa: BLE001  the test reads it
            self.migrated["error"] = repr(exc)
            self.moved.set()

    def backup_racing_migrate(self, out: Path | None) -> tuple[int, str]:
        """Run ``hermes dk backup``; once it has passed the marker check and listed its files,
        a real ``migrate`` starts on another thread, takes the pause and moves state, and the
        backup goes on reading. Returns the backup's exit code and output."""
        real, backup_thread = backup._snapshot_ledger, threading.get_ident()
        thread = threading.Thread(target=self.run_migrate, daemon=True)

        def racing(source, dest):
            if threading.get_ident() == backup_thread and not self.moved.is_set():
                thread.start()
                self.assertTrue(self.moved.wait(10), "migrate never reached its move")
                self.assertNotIn("error", self.migrated)
                self.assertIsNotNone(migrate.migrating())    # the marker is held now
            return real(source, dest)

        with contextlib.ExitStack() as stack:      # held until migrate is done, not just the backup
            for patch in (contextlib.redirect_stdout(io.StringIO()),     # migrate's own report
                          mock.patch.object(backup, "_snapshot_ledger", racing),
                          mock.patch.object(migrate, "settings_step", return_value=[]),
                          mock.patch.object(migrate, "shim_step", return_value=[]),
                          mock.patch.object(migrate, "files_step", return_value=[]),
                          mock.patch.object(migrate, "repo_step", self.half_move),
                          mock.patch.object(migrate, "doctor_step", return_value=[]),
                          mock.patch.object(cli_lifecycle, "_move_watchdog_job", return_value=[])):
                stack.enter_context(patch)
            code, text = self.run_cmd(cli.cmd_backup, out=str(out) if out else None, keep=10)
            self.finish.set()
            thread.join(10)
        self.assertTrue(self.moved.is_set(), "the race never ran")
        self.assertEqual(self.migrated.get("code"), 0, self.migrated)
        return code, text

    def assert_refused(self, code: int, text: str) -> None:
        self.assertEqual(code, 2, text)
        self.assertIn("cannot back up: the install is paused by a migration or restore", text)
        self.assertNotIn("backup written:", text)

    def test_an_out_archive_written_across_a_migrate_is_refused_and_deleted(self):
        out = self.root / "raced.tar.gz"
        code, text = self.backup_racing_migrate(out)
        if out.exists():         # before the fix: the half-moved records, recorded as clean
            manifest = backup.read_manifest(out)
            self.fail(f"the archive was kept with migrating={manifest['migrating']!r} and "
                      f"skipped={manifest['skipped']!r}: {text}")
        self.assert_refused(code, text)

    def test_a_default_archive_written_across_a_migrate_is_refused(self):
        code, text = self.backup_racing_migrate(None)
        self.assert_refused(code, text)
        self.assertFalse((self.state / "queue.json").exists())      # the move did happen
        # Left: only migrate's own archive, its undo, taken under its marker before the move.
        archives = sorted(backup.backups_dir().glob("*.tar.gz"))
        self.assertEqual(len(archives), 1, [p.name for p in archives])
        manifest = backup.read_manifest(archives[0])
        self.assertIn(str(self.state / "queue.json"), {e["path"] for e in manifest["files"]})
        self.assertEqual(manifest["skipped"], [])


if __name__ == "__main__":
    unittest.main()
