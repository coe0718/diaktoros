"""#2594: a restore that picks up an interrupted migration's pause marker and then refuses before
writing anything leaves that marker as it was.

``restore`` holds migrate's marker for its run, so it overwrites the one an interrupted migration
left. Before the fix a refusal (existing state without ``--force``, a run in flight or a ledger
that cannot be read, an archive judged unsafe, a pre-restore backup that failed) cleared it on the
way out, and the install ran on again with records that may be half moved. Uses the
``test_backup`` fixture: a real temporary home, ledger, state files and route registry.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_backup as tb  # noqa: E402
from diaktoros import cli  # noqa: E402
from diaktoros.state import backup, migrate, restore_checks  # noqa: E402

# What a migrate that died left: its pid is gone, and its records may be half moved.
INTERRUPTED = {"pid": 999999, "started": 1}


class RefusedRestoreKeepsThePause(tb.Base):
    def setUp(self):
        super().setUp()
        self.archive = self.root / "b.tar.gz"
        backup.create(self.archive)          # taken while nothing holds the marker

    def plant(self) -> None:
        migrate.marker_path().parent.mkdir(parents=True, exist_ok=True)
        migrate.marker_path().write_text(json.dumps(INTERRUPTED))

    def restore(self, *, force: bool) -> tuple[int, str]:
        return self.run_cmd(cli.cmd_restore, file=str(self.archive), dry_run=False, force=force)

    def assert_refused_and_still_paused(self, code: int, text: str, why: str) -> None:
        self.assertEqual(code, 2, text)
        self.assertIn(why, text)
        self.assertEqual(migrate.migrating(), INTERRUPTED,
                         "the interrupted migration's own marker must stay, not be cleared")
        self.assertTrue(migrate.dead(migrate.migrating()))

    def test_existing_state_without_force_keeps_the_interrupted_pause(self):
        self.plant()
        code, text = self.restore(force=False)
        self.assert_refused_and_still_paused(code, text, "existing state would be overwritten")

    def test_a_run_in_flight_keeps_the_interrupted_pause(self):
        self.plant()
        with mock.patch.object(migrate, "runs_in_flight", return_value=2):
            code, text = self.restore(force=True)
        self.assert_refused_and_still_paused(code, text, "2 run(s) are in flight or uncertain")

    def test_an_unreadable_ledger_keeps_the_interrupted_pause(self):
        self.plant()
        with mock.patch.object(migrate, "runs_in_flight",
                               side_effect=migrate.RunsUnknown("the ledger could not be read")):
            code, text = self.restore(force=True)
        self.assert_refused_and_still_paused(code, text, "the ledger could not be read")

    def test_an_unsafe_archive_keeps_the_interrupted_pause(self):
        self.plant()
        with mock.patch.object(restore_checks, "unsafe", return_value=["route x: not ours"]):
            code, text = self.restore(force=True)
        self.assert_refused_and_still_paused(code, text, "REFUSED — route x: not ours")

    def test_a_failed_pre_restore_backup_keeps_the_interrupted_pause(self):
        self.plant()
        with mock.patch.object(backup, "create", side_effect=backup.BackupError("disk full")):
            code, text = self.restore(force=True)
        self.assert_refused_and_still_paused(code, text, "the backup of the live install before")

    def test_an_unreadable_ledger_with_no_interrupted_run_leaves_no_pause(self):
        # The refusal comes back as an exit, not an exception out of the hold, so restore's own
        # marker goes with it: nothing was written, so there is nothing to keep paused.
        with mock.patch.object(migrate, "runs_in_flight",
                               side_effect=migrate.RunsUnknown("the ledger could not be read")):
            code, text = self.restore(force=True)
        self.assertEqual(code, 2, text)
        self.assertIsNone(migrate.migrating())

    def test_a_restore_that_goes_ahead_still_runs_under_its_own_marker(self):
        self.plant()
        seen = []
        real = backup.put_files

        def put_files(*args, **kw):
            seen.append(migrate.migrating())
            return real(*args, **kw)

        with mock.patch.object(backup, "put_files", side_effect=put_files):
            code, text = self.restore(force=True)
        self.assertIn(code, (0, 1), text)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], INTERRUPTED, "the restore writes under its own marker")


if __name__ == "__main__":
    unittest.main()
