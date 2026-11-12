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
        self.assertEqual(backup.latest_archive(), (None, None))
        self.dir.mkdir()
        self.assertEqual(backup.latest_archive(), (None, None))

    def test_the_newest_archive_wins_whatever_its_name(self):
        self.archive("diaktoros-backup-20250101-000000.tar.gz", age_days=40)
        newer = self.archive("by-hand.tar.gz", age_days=2)
        self.archive("diaktoros-backup-20250201-000000.tar.gz", age_days=9)
        (path, written), ahead = backup.latest_archive()
        self.assertIsNone(ahead)
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
        self.assertEqual(backup.latest_archive()[0][0], real)
        self.assertIsNone(backup.latest_archive()[1])

    def test_an_archive_dated_past_the_skew_allowance_is_set_apart_not_newest(self):
        """A future mtime must not win as "newest": it is returned as ``ahead`` instead (#1875)."""
        real = self.archive("real.tar.gz", age_days=20)
        future = self.archive("future.tar.gz", age_days=-3650)
        newest, ahead = backup.latest_archive()
        self.assertEqual(newest[0], real)
        self.assertEqual(ahead[0], future)
        # Within the allowance (an hour of drift between hosts) it is an ordinary newest archive.
        near = self.archive("near.tar.gz", age_days=-1 / 24)
        newest, ahead = backup.latest_archive()
        self.assertEqual(newest[0], near)
        self.assertEqual(ahead[0], future)
        # The caller's clock is the one given, not the module's.
        newest, ahead = backup.latest_archive(now=future.stat().st_mtime)
        self.assertEqual(newest[0], future)
        self.assertIsNone(ahead)

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

    def test_a_small_clock_drift_is_still_a_fresh_archive(self):
        self.archive("drift.tar.gz", age_days=-1 / 24)     # an hour ahead: within the allowance
        check = backup_checks.check_backup_fresh()
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)
        self.assertIn("0 day(s) old", check.detail)

    def test_a_future_dated_archive_is_not_a_recent_backup(self):
        """#1875 F1: an archive written under a clock years ahead is not green, now or later."""
        future = self.archive("future.tar.gz", age_days=-3650)
        mtime = future.stat().st_mtime
        check = backup_checks.check_backup_fresh()
        self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
        self.assertIn("future.tar.gz", check.detail)
        self.assertIn("in the future", check.detail)
        self.assertNotIn("no backup archive", check.detail)
        self.assertIn("hermes dk backup", check.detail)
        # The reviewer's probe: the clock is corrected to 31 days before the file's mtime, i.e. more
        # than FRESH_DAYS after the real backup, and the file must not read as a fresh one.
        with mock.patch.object(backup_checks.time, "time",
                               return_value=mtime - (backup.FRESH_DAYS + 1) * DAY):
            late = backup_checks.check_backup_fresh()
        self.assertEqual(late.status, doctor.UNKNOWN, late.detail)
        self.assertNotIn("day(s) old (within", late.detail)

    def test_a_real_archive_beside_a_future_one_decides_the_check(self):
        self.archive("future.tar.gz", age_days=-3650)
        self.archive("real.tar.gz", age_days=2)
        self.assertEqual(backup_checks.check_backup_fresh().status, doctor.VERIFIED)
        self.archive("real.tar.gz", age_days=backup.FRESH_DAYS + 5)
        stale = backup_checks.check_backup_fresh()
        self.assertEqual(stale.status, doctor.UNKNOWN, stale.detail)
        self.assertIn("real.tar.gz", stale.detail)

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

    def test_check_loop_does_not_count_a_future_dated_archive(self):
        self.archive("future.tar.gz", age_days=-3650)
        check = self.check_named()
        self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
        self.assertIn("in the future", check.detail)

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


class FutureDatedRecorded(Base):
    """#1905: the skew cutoff moves with the clock, so ``hermes dk backup`` records an archive it
    sees dated past it, and ``backup:fresh`` keeps that file apart once the clock reaches it."""

    def run_backup(self, **kw) -> str:
        import argparse
        from diaktoros import cli
        raw = {"id": "one", "repo": "acme/one", "fixers": ["fixer"], "reviewers": ["reviewer"],
               "reviewer_seat": "reviewer", "read_token": "reviewer",
               "state_dir": str(self.root / "state"),
               "seats": {"reviewer": {"route": "one-review", "profile": "reviewer"},
                         "fixer": {"route": "one-fix", "profile": "fixer"}}}
        (config.config_dir() / "one.json").write_text(json.dumps(raw))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(backup, "watchdog_job", return_value=None):
            self.assertEqual(cli.cmd_backup(argparse.Namespace(out=None, **kw)), 0)
        return out.getvalue()

    def at(self, when: float) -> doctor.Check:
        with mock.patch.object(backup_checks.time, "time", return_value=when):
            return backup_checks.check_backup_fresh()

    def test_backup_records_it_so_the_clock_reaching_it_is_not_green(self):
        future = self.archive("future.tar.gz", age_days=-3650)
        mtime = future.stat().st_mtime
        text = self.run_backup()
        self.assertIn("future.tar.gz", text)
        self.assertIn("in the future", text)
        # The clock walks from "ahead" into the allowance and past the file's date: the backup
        # just written is then years old, and the future-dated file must never read as fresh.
        for days_before in (backup.FRESH_DAYS + 1, 2, 0.5, 0, -1, -backup.FRESH_DAYS + 1):
            check = self.at(mtime - days_before * DAY)
            self.assertEqual(check.status, doctor.UNKNOWN, (days_before, check.detail))
            self.assertNotIn("(within", check.detail)

    def test_check_loop_names_a_recorded_archive_once_the_clock_reaches_it(self):
        future = self.archive("future.tar.gz", age_days=-3650)
        self.run_backup()
        for path in self.dir.glob("diaktoros-backup-*.tar.gz"):
            path.unlink()               # only the future-dated file is left
        with mock.patch.object(backup_checks.time, "time",
                               return_value=future.stat().st_mtime - DAY / 2):
            found = [c for c in doctor.check_loop(_loop(), offline=True) if c.name == "backup:fresh"]
        self.assertEqual(found[0].status, doctor.UNKNOWN, found[0].detail)
        self.assertIn("future.tar.gz", found[0].detail)
        self.assertIn("in the future when `hermes dk backup` last saw it", found[0].detail)

    def test_an_archive_within_the_allowance_is_not_recorded(self):
        self.archive("near.tar.gz", age_days=-1 / 24)
        text = self.run_backup()
        self.assertNotIn("near.tar.gz", text)
        self.assertEqual(backup.record_future_dated(), [])
        self.assertFalse((self.dir / backup.FUTURE_RECORD).exists())

    def test_a_rewritten_or_removed_archive_leaves_the_record(self):
        future = self.archive("future.tar.gz", age_days=-3650)
        self.assertEqual(backup.record_future_dated(), [future])
        self.assertEqual(backup.record_future_dated(), [])          # already recorded
        # Rewritten under a correct clock (another mtime): an ordinary archive again.
        self.archive("future.tar.gz", age_days=1)
        self.assertEqual(backup.latest_archive()[0][0], future)
        backup.record_future_dated()
        self.assertEqual(json.loads((self.dir / backup.FUTURE_RECORD).read_text()), {})

    def test_the_record_is_not_an_archive_and_is_never_pruned(self):
        self.archive("future.tar.gz", age_days=-3650)
        self.run_backup(keep=1)
        self.run_backup(keep=1)
        self.assertTrue((self.dir / backup.FUTURE_RECORD).is_file())
        newest, ahead = backup.latest_archive()
        self.assertNotEqual(newest[0].name, backup.FUTURE_RECORD)
        self.assertEqual(ahead[0].name, "future.tar.gz")

    # -- #2446 review: overlapping backups (F1), a damaged record (F2), a failed write (F3) --

    def test_overlapping_backups_keep_each_others_entries(self):
        """F1: run A scans before b.tar.gz arrives, run B records a and b, then A writes. Without
        one lock over scan/read/write, A's stale write drops b, and doctor later trusts it."""
        import threading
        a = self.archive("a.tar.gz", age_days=-3650)
        scanned, go = threading.Event(), threading.Event()
        real = backup._archives
        first = threading.Lock()

        def archives():
            found = real()
            if first.acquire(blocking=False):        # run A only: pause after its scan
                scanned.set()
                go.wait(5)
            return found

        results = {}
        with mock.patch.object(backup, "_archives", archives):
            run_a = threading.Thread(target=lambda: results.setdefault(
                "a", backup.record_future_dated()))
            run_a.start()
            self.assertTrue(scanned.wait(5))
            b = self.archive("b.tar.gz", age_days=-3649)      # arrives after A's scan
            run_b = threading.Thread(target=lambda: results.setdefault(
                "b", backup.record_future_dated()))
            run_b.start()
            run_b.join(0.5)          # without the lock B finishes here, before A writes
            go.set()
            run_a.join(5)
            run_b.join(5)
        recorded = json.loads((self.dir / backup.FUTURE_RECORD).read_text())
        self.assertEqual(sorted(recorded), ["a.tar.gz", "b.tar.gz"], results)
        self.assertEqual(recorded["b.tar.gz"], b.stat().st_mtime)
        check = self.at(b.stat().st_mtime)       # the clock reaches b: still not a recent backup
        self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
        self.assertIn(a.name, check.detail)

    def damage(self, content: bytes) -> pathlib.Path:
        future = self.archive("future.tar.gz", age_days=-3650)
        self.run_backup()
        for path in self.dir.glob("diaktoros-backup-*.tar.gz"):
            path.unlink()               # only the future-dated file is left
        (self.dir / backup.FUTURE_RECORD).write_bytes(content)
        return future

    def test_a_damaged_record_is_never_read_as_empty(self):
        """F2: malformed JSON, non-UTF-8 bytes and a wrong shape each leave ``doctor`` unknown
        through ``check_loop`` once the clock reaches the archive, and ``backup`` warns, still
        succeeds, and leaves the record's bytes as they were."""
        for content in (b"{not json", b"\xff\xfe\x00garbage", b"[1, 2]",
                        b'{"future.tar.gz": "soon"}'):
            with self.subTest(content=content):
                future = self.damage(content)
                with mock.patch.object(backup_checks.time, "time",
                                       return_value=future.stat().st_mtime):
                    found = [c for c in doctor.check_loop(_loop(), offline=True)
                             if c.name == "backup:fresh"]
                self.assertEqual(found[0].status, doctor.UNKNOWN, found[0].detail)
                self.assertIn("is damaged", found[0].detail)
                self.assertNotIn("(within", found[0].detail)
                text = self.run_backup()             # asserts exit 0
                self.assertIn("is damaged", text)
                self.assertIn("left unchanged", text)
                self.assertEqual((self.dir / backup.FUTURE_RECORD).read_bytes(), content)

    def test_a_record_that_cannot_be_written_warns_and_the_backup_succeeds(self):
        """F3: the record's write fails; ``cmd_backup`` exits 0, says so, and the archive is whole."""
        self.archive("future.tar.gz", age_days=-3650)
        real = backup.util.atomic_write

        def refuse(path, *a, **kw):
            if pathlib.Path(path).name == backup.FUTURE_RECORD:
                raise OSError(28, "No space left on device")
            return real(path, *a, **kw)

        with mock.patch.object(backup.util, "atomic_write", refuse):
            text = self.run_backup()                 # asserts exit 0
        self.assertIn("warning: could not record future-dated archives", text)
        self.assertIn("No space left on device", text)
        self.assertFalse((self.dir / backup.FUTURE_RECORD).exists())
        written = [p for p in self.dir.glob("diaktoros-backup-*.tar.gz")]
        self.assertEqual(len(written), 1)
        read, problems = backup.verify(written[0])
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
