"""#2322: a backup and a ``migrate`` never interleave, so half-moved records are never published
as a clean archive.

``create`` refused while ``migrate``'s pause marker existed (#1054), but checked only before the
archive's first byte. A migrate that started during the write (and even finished, its marker gone
again) had moved records under a backup that still wrote ``migrating: None`` into its manifest:
``restore`` takes that for a clean archive and resumes the loop unpaused on them. A backup now
holds ``migrate``'s lock shared for its whole write (``backup_pause.held_off``), so a migrate that
starts meanwhile refuses and moves nothing, and a backup refuses while a migrate holds the lock.
These tests run the real ``hermes dk migrate`` and ``hermes dk backup``. Uses the ``test_backup``
fixture: a real temporary home, ledger, state files and route registry.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import fcntl
import io
import os
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_backup as tb  # noqa: E402
from diaktoros import cli  # noqa: E402
from diaktoros.state import backup, migrate  # noqa: E402
from diaktoros.cli import lifecycle as cli_lifecycle  # noqa: E402


class MigrateDuringTheWrite(tb.Base):
    def whole_move(self, *args, **kw) -> list[str]:
        """``migrate``'s repository step: one state file moved, and the migration then finishes,
        its marker removed, all while the backup is still writing (Vex, #2685 round 1)."""
        os.replace(self.state / "queue.json", self.state / "queue.moved.json")
        return []

    def migrate_patches(self):
        return (mock.patch.object(migrate, "settings_step", return_value=[]),
                mock.patch.object(migrate, "shim_step", return_value=[]),
                mock.patch.object(migrate, "files_step", return_value=[]),
                mock.patch.object(migrate, "repo_step", self.whole_move),
                mock.patch.object(migrate, "doctor_step", return_value=[]),
                mock.patch.object(cli_lifecycle, "_move_watchdog_job", return_value=[]))

    def backup_with_migrate_inside(self, out: Path | None) -> tuple[int, str, int, str]:
        """Run ``hermes dk backup``; once it has listed its files, a whole ``hermes dk migrate``
        runs (start to finish) before the backup reads on. Returns both exit codes and outputs."""
        real, ran = backup._snapshot_ledger, {}

        def racing(source, dest):
            if not ran:
                with contextlib.redirect_stdout(io.StringIO()) as said:
                    ran["code"] = cli.cmd_migrate(argparse.Namespace(dry_run=False))
                ran["text"] = said.getvalue()
            return real(source, dest)

        with contextlib.ExitStack() as stack:
            for patch in (mock.patch.object(backup, "_snapshot_ledger", racing),
                          *self.migrate_patches()):
                stack.enter_context(patch)
            code, text = self.run_cmd(cli.cmd_backup, out=str(out) if out else None, keep=10)
        self.assertIn("code", ran, "the race never ran")
        return code, text, ran["code"], ran["text"]

    def assert_migrate_refused_and_the_archive_whole(self, archive: Path, migrated: int, said: str):
        self.assertEqual(migrated, 2, said)
        self.assertIn("cannot migrate: a backup, migrate or restore is running", said)
        self.assertTrue((self.state / "queue.json").exists())         # nothing was moved
        self.assertFalse((self.state / "queue.moved.json").exists())
        manifest = backup.read_manifest(archive)
        self.assertIsNone(manifest["migrating"])
        self.assertEqual(manifest["skipped"], [], "the archive was written across a migrate")
        self.assertIn(str(self.state / "queue.json"), {e["path"] for e in manifest["files"]})

    def test_a_migrate_run_whole_during_an_out_backup_refuses_and_moves_nothing(self):
        out = self.root / "raced.tar.gz"
        code, text, migrated, said = self.backup_with_migrate_inside(out)
        self.assertEqual(code, 0, text)
        self.assertIn("backup written:", text)
        self.assert_migrate_refused_and_the_archive_whole(out, migrated, said)

    def test_a_migrate_run_whole_during_a_default_backup_refuses_and_takes_no_archive(self):
        code, text, migrated, said = self.backup_with_migrate_inside(None)
        self.assertEqual(code, 0, text)
        archives = sorted(backup.backups_dir().glob("*.tar.gz"))
        self.assertEqual(len(archives), 1, [p.name for p in archives])   # no undo of a migrate
        self.assert_migrate_refused_and_the_archive_whole(archives[0], migrated, said)

    def test_a_backup_refuses_while_a_migrate_holds_the_lock_before_its_marker(self):
        migrate.lock_path().parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(migrate.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)                 # as ``migrate.hold`` does, no marker yet
        out = self.root / "while-held.tar.gz"
        code, text = self.run_cmd(cli.cmd_backup, out=str(out), keep=10)
        self.assertEqual(code, 2, text)
        self.assertIn("cannot back up: a migration or restore is starting", text)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
