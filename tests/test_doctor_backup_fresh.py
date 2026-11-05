"""#1081: ``doctor`` warns when no ``hermes dk backup`` archive is recent.

``check_watchdog_last_run`` proves the watchdog runs; ``backup:fresh`` is the matching proof that
a backup *happens*. It is a warning (``unknown``), never a failure, so a loop with no backup yet
still passes ``doctor`` and only ``--strict`` turns the warning into exit 1. These tests drive it
through ``doctor.check_loop`` (the entry point every ``doctor`` surface calls) and ``report``, and
use the real ``backup`` command so the directory it writes and the one the check reads cannot drift.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import backup, config, doctor  # noqa: E402
from diaktoros.doctor import backup_checks  # noqa: E402

DAY = 86400


def _loop() -> dict:
    return config.normalize({
        "id": "widgets", "repo": "acme/widgets", "fixers": ["fixer"], "reviewers": ["reviewer"],
        "seats": {"reviewer": {"profile": "rev", "route": "r"},
                  "fixer": {"profile": "fix", "route": "f"}},
        "read_token": "reader"})


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        patch = mock.patch.dict(os.environ, {
            "HERMES_HOME": str(self.root / "hermes"),
            "DIAKTOROS_CONFIG_DIR": str(self.root / "loops")})
        patch.start()
        self.addCleanup(patch.stop)
        (self.root / "hermes").mkdir()
        (self.root / "loops").mkdir()
        self.dir = self.root / "hermes" / "backups"

    def archive(self, name: str, age_days: float = 0, size: int = 10) -> pathlib.Path:
        """A file in the backups directory last written ``age_days`` ago."""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / name
        path.write_bytes(b"x" * size)
        when = time.time() - age_days * DAY
        os.utime(path, (when, when))
        return path


class LatestArchive(Base):
    """``backup.latest_archive``: what counts as an archive, and which is newest."""

    def test_no_directory_and_an_empty_one_are_none(self):
        self.assertIsNone(backup.latest_archive())
        self.dir.mkdir()
        self.assertIsNone(backup.latest_archive())

    def test_the_newest_archive_wins_whatever_its_name(self):
        self.archive("diaktoros-backup-20250101-000000.tar.gz", age_days=40)
        newer = self.archive("by-hand.tar.gz", age_days=2)
        self.archive("diaktoros-backup-20250201-000000.tar.gz", age_days=9)
        path, written = backup.latest_archive()
        self.assertEqual(path, newer)
        self.assertAlmostEqual(time.time() - written, 2 * DAY, delta=60)

    def test_what_is_not_an_archive_is_ignored(self):
        self.archive("notes.txt", age_days=0)
        self.archive("empty.tar.gz", age_days=0, size=0)
        (self.dir / "a-directory.tar.gz").mkdir()
        real = self.archive("real.tar.gz", age_days=20)
        # A symlink, even to a real and newer file, is not an archive this install wrote.
        outside = self.root / "elsewhere.tar.gz"
        outside.write_bytes(b"x" * 10)
        (self.dir / "link.tar.gz").symlink_to(outside)
        self.assertEqual(backup.latest_archive()[0], real)

    def test_an_unreadable_directory_raises_rather_than_reading_as_none(self):
        self.dir.mkdir()
        with mock.patch.object(backup.os, "scandir", side_effect=PermissionError("denied")):
            with self.assertRaises(OSError):
                backup.latest_archive()

    def test_default_out_and_the_check_use_one_directory(self):
        self.assertEqual(backup.default_out().parent, backup.backups_dir())
        self.assertEqual(backup.backups_dir(), self.dir)


class BackupFresh(Base):
    """``check_backup_fresh``: warns on none, on stale and on unreadable; verified when fresh."""

    def test_no_archive_warns_and_names_the_command(self):
        check = backup_checks.check_backup_fresh()
        self.assertEqual(check.name, "backup:fresh")
        self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
        self.assertFalse(check.failed)
        self.assertIn("hermes dk backup", check.detail)
        self.assertIn(str(self.dir), check.detail)

    def test_a_recent_archive_is_verified(self):
        self.archive("diaktoros-backup-recent.tar.gz", age_days=3)
        check = backup_checks.check_backup_fresh()
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)
        self.assertIn("diaktoros-backup-recent.tar.gz", check.detail)

    def test_a_stale_archive_warns_with_its_age(self):
        self.archive("diaktoros-backup-old.tar.gz", age_days=backup.FRESH_DAYS + 5)
        check = backup_checks.check_backup_fresh()
        self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
        self.assertIn(f"{backup.FRESH_DAYS + 5} days old", check.detail)
        self.assertIn("hermes dk backup", check.detail)

    def test_the_boundary_is_inclusive_of_the_limit(self):
        self.archive("a.tar.gz", age_days=backup.FRESH_DAYS - 0.01)
        self.assertEqual(backup_checks.check_backup_fresh().status, doctor.VERIFIED)
        self.archive("a.tar.gz", age_days=backup.FRESH_DAYS + 0.01)
        self.assertEqual(backup_checks.check_backup_fresh().status, doctor.UNKNOWN)

    def test_a_clock_skewed_future_mtime_is_not_an_error(self):
        self.archive("future.tar.gz", age_days=-2)
        check = backup_checks.check_backup_fresh()
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)
        self.assertIn("0 day(s) old", check.detail)

    def test_an_unreadable_directory_is_unknown_not_missing(self):
        with mock.patch.object(backup.os, "scandir", side_effect=PermissionError("denied")):
            check = backup_checks.check_backup_fresh()
        self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
        self.assertIn("cannot read", check.detail)
        self.assertNotIn("no backup archive", check.detail)

    def test_the_check_writes_nothing(self):
        backup_checks.check_backup_fresh()
        self.assertFalse(self.dir.exists())


class ThroughDoctor(Base):
    """The entry points: ``check_loop`` carries the check, ``report`` turns it into an exit code."""

    def check_named(self) -> doctor.Check:
        found = [c for c in doctor.check_loop(_loop(), offline=True) if c.name == "backup:fresh"]
        self.assertEqual(len(found), 1, "check_loop must emit backup:fresh exactly once")
        return found[0]

    def test_check_loop_emits_it_and_follows_the_archive(self):
        self.assertEqual(self.check_named().status, doctor.UNKNOWN)
        self.archive("diaktoros-backup-now.tar.gz", age_days=0)
        self.assertEqual(self.check_named().status, doctor.VERIFIED)

    def test_check_loop_orders_it_beside_the_watchdog_line(self):
        names = [c.name for c in doctor.check_loop(_loop(), offline=True)]
        self.assertEqual(names.index("backup:fresh"), names.index("watchdog:run") + 1)

    def report(self, strict: bool) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = doctor.report(_loop(), [backup_checks.check_backup_fresh()], strict=strict)
        return rc, buf.getvalue()

    def test_a_never_backed_up_install_warns_but_passes_doctor(self):
        rc, out = self.report(strict=False)
        self.assertEqual(rc, 0, out)
        self.assertIn("⚠️ backup:fresh", out)

    def test_strict_turns_the_warning_into_a_failure(self):
        rc, out = self.report(strict=True)
        self.assertEqual(rc, 1, out)

    def test_a_fresh_backup_passes_even_strict(self):
        self.archive("diaktoros-backup-now.tar.gz")
        rc, out = self.report(strict=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("✅ backup:fresh", out)

    def test_the_backup_command_satisfies_the_check(self):
        """A real ``hermes dk backup`` with no ``--out`` writes where the check looks."""
        import argparse
        from diaktoros import cli
        raw = {"id": "one", "repo": "acme/one", "fixers": ["fixer"], "reviewers": ["reviewer"],
               "reviewer_seat": "reviewer", "read_token": "reviewer",
               "state_dir": str(self.root / "state"),
               "seats": {"reviewer": {"route": "one-review", "profile": "reviewer"},
                         "fixer": {"route": "one-fix", "profile": "fixer"}}}
        (config.config_dir() / "one.json").write_text(json.dumps(raw))
        self.assertEqual(backup_checks.check_backup_fresh().status, doctor.UNKNOWN)
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(backup, "watchdog_job", return_value=None):
            self.assertEqual(cli.cmd_backup(argparse.Namespace(out=None)), 0)
        check = backup_checks.check_backup_fresh()
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)


if __name__ == "__main__":
    unittest.main()
