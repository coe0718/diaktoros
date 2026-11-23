"""#2594: a restore that picks up an interrupted migration's pause marker and then refuses before
writing anything leaves that marker as it was.

``restore`` holds migrate's marker for its run, so it overwrites the one an interrupted migration
left. Before the fix a refusal (existing state without ``--force``, a run in flight or a ledger
that cannot be read, an archive judged unsafe, a pre-restore backup that failed) cleared it on the
way out, and the install ran on again with records that may be half moved. A failure while
staging the archive's members (before any live file is swapped) counts as such a refusal too
(review on #2630); a failure once the swap began keeps restore's own marker, rolled back or not.
Uses the ``test_backup`` fixture: a real temporary home, ledger, state files and route registry.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_backup as tb  # noqa: E402
from diaktoros import cli, state as state_mod  # noqa: E402
from diaktoros.state import backup, migrate, restore_checks  # noqa: E402

# What a migrate that died left: its pid is gone, and its records may be half moved.
INTERRUPTED = {"pid": 999999, "started": 1}


class Fixture(tb.Base):
    """A backup of the starting install, and the marker an interrupted migrate left."""

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


class RefusedRestoreKeepsThePause(Fixture):
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



class FailedRestoreKeepsTheRightPause(Fixture):
    """Failures inside ``put_files``: before the swap they are refusals; after it, not."""

    def queue_fails_to_swap(self, *, rollback_fails: bool):
        """``os.replace`` that runs out of disk swapping the state file in, after the runtime file
        was swapped; with ``rollback_fails`` the runtime file's original cannot be put back."""
        queue, runtime = self.state / "queue.json", self.root / "home" / "diaktoros-runtime.json"
        real, failed = os.replace, []

        def flaky(src, dst, *a, **k):
            if Path(dst) == queue and not failed:
                failed.append(dst)
                raise OSError(28, "No space left on device")
            if rollback_fails and Path(dst) == runtime and ".pre-restore-" in Path(src).name:
                raise OSError(5, "Input/output error")
            return real(src, dst, *a, **k)
        return mock.patch.object(os, "replace", flaky)

    def assert_restores_own_marker(self, code: int, text: str) -> None:
        self.assertEqual(code, 2, text)
        held = migrate.migrating()
        self.assertIsNotNone(held, "a failure after the swap began must stay paused")
        self.assertNotEqual(held, INTERRUPTED)
        self.assertEqual(held.get("pid"), os.getpid(), "restore's own marker stays")

    def staging_fails(self):
        return mock.patch.object(backup, "_stage",
                                 side_effect=OSError(28, "No space left on device"))

    def test_a_full_disk_while_staging_keeps_the_interrupted_pause(self):
        self.plant()
        live = (self.state / "queue.json").read_text()
        with self.staging_fails():
            code, text = self.restore(force=True)
        self.assert_refused_and_still_paused(code, text, "No space left on device")
        self.assertIn("nothing was written", text)
        self.assertEqual((self.state / "queue.json").read_text(), live)

    def full_after_holds_write(self):
        """The marker's disk fills once ``hold`` has written restore's own marker."""
        real, writes = state_mod._atomic_write, []

        def full(path, data):
            if Path(path) == migrate.marker_path() and writes:
                raise OSError(28, "No space left on device")
            writes.append(path)
            return real(path, data)
        return mock.patch.object(state_mod, "_atomic_write", full)

    def restore_on_a_full_disk(self, during_staging=None) -> None:
        self.plant()

        def stage(*_args, **_kwargs):
            if during_staging:
                during_staging()
            raise OSError(28, "No space left on device")
        with mock.patch.object(backup, "_stage", side_effect=stage), self.full_after_holds_write():
            code, text = self.restore(force=True)
        self.assert_refused_and_still_paused(code, text, "No space left on device")
        self.assertFalse(migrate._kept_path().exists())

    def test_a_disk_that_stays_full_keeps_the_interrupted_pause(self):
        # Review on #2630: putting the old marker back must not need a write on the full disk.
        self.restore_on_a_full_disk()

    def test_a_refused_second_holder_leaves_the_kept_marker_alone(self):
        # Review round 3 on #2630: only the lock's holder removes the kept marker.
        def contend():
            with self.assertRaises(migrate.MigrationBusy), migrate.hold():
                pass
            self.assertTrue(migrate._kept_path().exists())
        self.restore_on_a_full_disk(contend)

    def test_without_hard_links_the_marker_is_kept_as_a_copy(self):
        with mock.patch.object(os, "link", side_effect=OSError(1, "Operation not permitted")):
            self.restore_on_a_full_disk()

    def test_a_marker_that_cannot_be_kept_refuses_before_replacing_it(self):
        # Review round 4 on #2630: neither a link nor a copy, so ``hold`` writes nothing.
        self.plant()
        with mock.patch.object(os, "link", side_effect=OSError(1, "Operation not permitted")), \
                mock.patch.object(migrate, "atomic_write",
                                  side_effect=OSError(28, "No space left on device")), \
                mock.patch.object(backup, "_stage") as stage:
            code, text = self.restore(force=True)
        self.assert_refused_and_still_paused(code, text, "No space left on device")
        stage.assert_not_called()
        self.assertFalse(migrate._kept_path().exists())

    def test_a_full_disk_while_staging_with_no_interrupted_run_leaves_no_pause(self):
        with self.staging_fails():
            code, text = self.restore(force=True)
        self.assertEqual(code, 2, text)
        self.assertIsNone(migrate.migrating())

    def test_a_read_that_fails_before_staging_keeps_the_interrupted_pause(self):
        # The archive checks and the pre-restore snapshot run before any live write too.
        for where in ("unsafe", "existing"):
            with self.subTest(where=where):
                self.plant()
                target = restore_checks if where == "unsafe" else backup
                with mock.patch.object(target, where, side_effect=OSError(5, "I/O error")):
                    code, text = self.restore(force=True)
                self.assert_refused_and_still_paused(code, text, "I/O error")
                self.assertIn("nothing was written", text)

    def test_a_swap_that_fails_and_is_rolled_back_keeps_restores_own_marker(self):
        self.plant()
        with self.queue_fails_to_swap(rollback_fails=False):
            code, text = self.restore(force=True)
        self.assertNotIn("nothing was written", text)
        self.assert_restores_own_marker(code, text)

    def test_a_swap_whose_rollback_fails_keeps_restores_own_marker(self):
        self.plant()
        with self.queue_fails_to_swap(rollback_fails=True):
            code, text = self.restore(force=True)
        self.assertIn("could not be undone for", text)
        self.assert_restores_own_marker(code, text)


if __name__ == "__main__":
    unittest.main()
