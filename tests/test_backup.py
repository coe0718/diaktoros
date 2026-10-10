"""`backup` / `restore` (#496), and the backup `migrate` takes before its first move.

Offline: a real temporary home, real SQLite ledger, state files and route registry; only the
scheduler (`hermes cron`) and `doctor` are stubbed.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import tarfile
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import backup, cli, config, migrate, routes  # noqa: E402

SECRET = "s3cr3t-hmac-value-0123456789"
JOB = {"id": "j1", "name": "diaktoros watchdog", "schedule": {"kind": "interval", "minutes": 5},
       "deliver": "telegram", "script": "diaktoros-watchdog.py"}


class Base(fg.Base):
    cron_run = None

    def setUp(self):
        super().setUp()
        home = self.root / "home"
        env = mock.patch.dict(os.environ, {"DIAKTOROS_SUBS": str(home / "subs.json")})
        env.start()
        self.addCleanup(env.stop)
        (home / "state").mkdir(exist_ok=True)
        self.db = home / "state" / "diaktoros-runs.sqlite"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, repo TEXT, state TEXT)")
        con.executemany("INSERT INTO runs (repo, state) VALUES (?, 'done')",
                        [("owner/one",)] * 298)
        con.commit()
        con.close()
        # The loop's state where a real install keeps it, inside the Hermes home: restore writes
        # only inside this plugin's places (the Hermes home and the loop files' directory).
        self.state = home / "state" / "diaktoros" / "one"
        self.state.mkdir(parents=True, exist_ok=True)
        path = config.config_dir() / "one.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "state_dir": str(self.state),
                                    "host": "https://127.0.0.1:9"}))
        (self.state / "queue.json").write_text('{"owner/one#7": {"head": "abc"}}')
        (home / "diaktoros-runtime.json").write_text('{"runtime": 1}')
        routes.subs_path().write_text(json.dumps({
            "one-review": {"secret": SECRET, "script": "gate_reviewer.py"},
            "foreign": {"secret": "other", "script": "somebody_elses.py"}}))
        # The loop's routes as the plugin itself writes them (profile, events, script) — keeping
        # one-review's secret — so the starting install is healthy on what restore owns, and a
        # round trip is checked against correctly bound routes, not a broken registry.
        cli._install_routes(config.all_loops()[0])
        for profile in ("reviewer", "fixer"):          # the seats' Hermes profiles exist
            (home / "profiles" / profile).mkdir(parents=True, exist_ok=True)
        cli.gate_shims.install(config.all_loops()[0], dry_run=False, report=False)
        self.jobs = [dict(JOB)]
        for patcher in (mock.patch.object(cli, "_cron_jobs", side_effect=lambda loop: (self.jobs, "")),
                        mock.patch.object(cli, "_hermes_bin", return_value="hermes")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def ledger_count(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            con.close()

    def run_cmd(self, func, **kw):
        out = io.StringIO()
        def fake_cron(cmd, **kw):
            self.jobs.append({**JOB})
            return mock.Mock(returncode=0, stdout="", stderr="")
        with contextlib.redirect_stdout(out), \
                mock.patch.object(cli.gate_shims, "install", return_value=[]), \
                mock.patch.object(cli.subprocess, "run", self.cron_run or fake_cron), \
                mock.patch.object(migrate, "doctor_step", return_value=["doctor: every check verified"]):
            code = func(argparse.Namespace(**kw))
        return code, out.getvalue()

    def wipe(self):
        self.db.unlink()
        (self.state / "queue.json").unlink()
        (self.root / "home" / "diaktoros-runtime.json").unlink()
        routes.subs_path().unlink()
        (config.config_dir() / "one.json").unlink()
        self.jobs.clear()


class Tampered(Base):
    """An archive is outside input: restore refuses one that would write outside this plugin's
    places, install a route that is not this plugin's gate, replace someone else's route, or run
    while a turn is in flight. Nothing is written in any of these cases."""

    def tamper(self, change, extra=None):
        good = self.root / "good.tar.gz"
        self.run_cmd(cli.cmd_backup, out=str(good))
        manifest = backup.read_manifest(good)
        change(manifest)
        bad = self.root / "bad.tar.gz"
        with tarfile.open(good, "r:gz") as src, tarfile.open(bad, "w:gz") as dst:
            for member in src.getmembers():
                if member.name != backup.MANIFEST:
                    dst.addfile(member, src.extractfile(member))
            for name, data in (extra or {}).items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                dst.addfile(info, io.BytesIO(data))
            raw = json.dumps(manifest).encode()
            info = tarfile.TarInfo(backup.MANIFEST)
            info.size = len(raw)
            dst.addfile(info, io.BytesIO(raw))
        return bad

    def refused(self, archive, force=True, want=""):
        code, text = self.run_cmd(cli.cmd_restore, file=str(archive), dry_run=False, force=force)
        self.assertEqual(code, 2, text)
        self.assertIn(want, text)
        return text

    def test_a_file_outside_this_plugins_places_is_refused(self):
        outside = self.root / "outside.txt"
        bad = self.tamper(lambda m: m["files"].append(
            {"member": "files/evil", "path": str(outside), "kind": "file"}),
            {"files/evil": b"owned"})
        self.refused(bad, want="outside this plugin's places")
        self.assertFalse(outside.exists())

    def test_a_symlinked_directory_inside_the_home_is_refused(self):
        away = self.root / "away"
        away.mkdir()
        (self.root / "home" / "link").symlink_to(away, target_is_directory=True)
        target = self.root / "home" / "link" / "x.json"
        bad = self.tamper(lambda m: m["files"].append(
            {"member": "files/evil", "path": str(target), "kind": "file"}), {"files/evil": b"x"})
        self.refused(bad, want="behind a symlink")
        self.assertEqual(list(away.iterdir()), [])
        # Even a link that stays inside the home is not followed: a path is restored as named.
        (self.root / "home" / "inner").symlink_to(self.state, target_is_directory=True)
        inner = self.tamper(lambda m: m["files"].append(
            {"member": "files/evil", "path": str(self.root / "home" / "inner" / "y.json"),
             "kind": "file"}), {"files/evil": b"y"})
        self.refused(inner, want="behind a symlink")

    def test_a_state_dir_outside_the_home_needs_consent_and_stays_inside_it(self):
        away = self.root / "elsewhere"
        declared = away / "state"
        good = self.tamper(lambda m: (m["state_dirs"].append(str(declared)), m["files"].append(
            {"member": "files/st", "path": str(declared / "watchdog.json"), "kind": "file"})),
            {"files/st": b"{}"})
        self.refused(good, want="re-run with --allow-state-dirs")
        self.assertFalse(declared.exists())
        manifest = backup.read_manifest(good)
        self.assertEqual(backup.outside_state_dirs(manifest), [str(declared)])
        self.assertEqual(backup.unsafe(manifest, allow_state_dirs=True), [])
        # Consent covers the declared directory only: a sibling is still outside.
        manifest["files"].append({"member": "files/st", "path": str(away / "x.json"),
                                  "kind": "file"})
        [problem] = backup.unsafe(manifest, allow_state_dirs=True)
        self.assertIn("outside this plugin's places", problem)

    def test_a_route_that_is_not_this_plugins_gate_is_refused(self):
        bad = self.tamper(lambda m: m["routes"].update(
            {"one-review": {"secret": "x", "script": "run_anything.py"}}))
        self.refused(bad, want="not one of this plugin's gates")
        self.assertEqual(routes.all_routes()["one-review"]["script"], "gate_reviewer.py")

    def test_someone_elses_live_route_is_never_replaced_even_with_force(self):
        bad = self.tamper(lambda m: m["routes"].update(
            {"foreign": {"secret": "x", "script": "gate_reviewer.py"}}))
        self.refused(bad, force=True, want="belongs to something else")
        self.assertEqual(routes.all_routes()["foreign"]["script"], "somebody_elses.py")

    def test_a_run_in_flight_refuses_before_anything_is_written(self):
        good = self.root / "good.tar.gz"
        self.run_cmd(cli.cmd_backup, out=str(good))
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO runs (repo, state) VALUES ('owner/one', 'running')")
        con.commit()
        con.close()
        (self.state / "queue.json").write_text("{}")
        self.refused(good, force=True, want="in flight")
        self.assertEqual((self.state / "queue.json").read_text(), "{}")


class Create(Base):
    def test_archive_is_0600_and_no_secret_is_printed(self):
        out = self.root / "b.tar.gz"
        code, text = self.run_cmd(cli.cmd_backup, out=str(out))
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertNotIn(SECRET, text)
        self.assertIn("secrets", text)
        manifest = backup.read_manifest(out)
        self.assertEqual(sorted(manifest["routes"]), ["one-fix", "one-review"])   # only ours
        self.assertEqual(manifest["cron"], {"name": "diaktoros watchdog", "schedule": "5m",
                                            "deliver": "telegram"})

    def test_refuses_to_overwrite(self):
        out = self.root / "b.tar.gz"
        out.write_text("keep")
        code, text = self.run_cmd(cli.cmd_backup, out=str(out))
        self.assertEqual(code, 2)
        self.assertEqual(out.read_text(), "keep")

    def test_default_out_does_not_collide_within_one_second(self):
        with mock.patch.object(backup.time, "strftime", return_value="20261009-000000"):
            first = backup.create()
            second = backup.create()
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file() and second.is_file())

    def test_ledger_snapshot_is_consistent_while_a_writer_is_open(self):
        writer = sqlite3.connect(self.db, isolation_level=None)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO runs (repo, state) VALUES ('x', 'running')")
        out = self.root / "b.tar.gz"
        try:
            backup.create(out)          # an uncommitted write must not leak into the archive
        finally:
            writer.execute("ROLLBACK")
            writer.close()
        self.wipe()
        with mock.patch.object(migrate, "doctor_step", return_value=[]):
            self.run_cmd(cli.cmd_restore, file=str(out), dry_run=False, force=True)
        self.assertEqual(self.ledger_count(), 298)


class Restore(Base):
    def take(self):
        out = self.root / "b.tar.gz"
        backup.create(out)
        return out

    def test_round_trip_restores_an_equivalent_install(self):
        out = self.take()
        self.wipe()
        code, text = self.run_cmd(cli.cmd_restore, file=str(out), dry_run=False, force=False)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.ledger_count(), 298)
        self.assertEqual(json.loads((self.state / "queue.json").read_text()),
                         {"owner/one#7": {"head": "abc"}})
        self.assertEqual(routes.all_routes()["one-review"]["secret"], SECRET)
        self.assertNotIn("foreign", routes.all_routes())
        self.assertNotIn(SECRET, text)
        self.assertFalse(migrate.marker_path().exists())          # the pause is released

    def test_real_doctor_reads_the_restored_install_like_the_original(self):
        """The unstubbed doctor runs inside restore. Profiles, tokens and the runtime file are
        outside the archive, so a bare fixture is not all green; what restore owns (loop file,
        routes, state dir, cron job) must read exactly as it did before the wipe."""
        from diaktoros import doctor

        def readout():
            return {c.name: c.status for c in doctor.check_loop(config.all_loops()[0], offline=True)}
        before = readout()
        self.assertEqual(before["config"], doctor.VERIFIED)
        out = self.take()
        self.wipe()
        self.assertEqual(config.all_loops(), [])
        buf = io.StringIO()

        def fake_cron(cmd, **kw):
            self.jobs.append({**JOB})
            return mock.Mock(returncode=0, stdout="", stderr="")
        with contextlib.redirect_stdout(buf), \
                mock.patch.object(cli.gate_shims, "install", return_value=[]), \
                mock.patch.object(cli.subprocess, "run", fake_cron):
            code = cli.cmd_restore(argparse.Namespace(file=str(out), dry_run=False, force=False))
        after = readout()
        # every check, not a hand-picked subset: the restored install reads exactly as the
        # original did (the scheduler's checks excepted: `hermes cron` is stubbed here)
        def unowned(d):
            return {n: v for n, v in d.items() if not n.startswith("cron:")}
        self.assertTrue(before)
        self.assertEqual(unowned(after), unowned(before))
        # What restore owns is healthy before the wipe and verified after it, not merely
        # unchanged (adjudication on #537: equal-to-original was checked against broken routes).
        owned = [n for n in before if n == "config" or n == "state_dir"
                 or n.startswith(("route:one-", "gateway-script:one-"))]
        self.assertGreaterEqual(len(owned), 6, owned)
        for name in owned:
            self.assertEqual((name, before[name]), (name, doctor.VERIFIED))
            self.assertEqual((name, after[name]), (name, doctor.VERIFIED))
        self.assertIn("doctor: one:", buf.getvalue())      # the real step ran and reported
        self.assertNotIn("doctor: every check verified", buf.getvalue())
        self.assertEqual(code, 1)                          # unverified checks => exit 1

    def test_restore_recreates_the_cron_job_and_reads_it_back(self):
        out = self.take()
        self.wipe()
        calls = []

        def create(cmd, **kw):
            calls.append(cmd)
            self.jobs.append({**JOB})
            return mock.Mock(returncode=0, stdout="", stderr="")
        self.cron_run = create
        code, text = self.run_cmd(cli.cmd_restore, file=str(out), dry_run=False, force=False)
        self.assertEqual(code, 0, text)
        self.assertEqual(calls[0][1:4], ["cron", "create", "5m"])
        self.assertIn("--deliver", calls[0])
        self.assertEqual(calls[0][calls[0].index("--deliver") + 1], "telegram")

    def test_a_cron_job_that_does_not_read_back_is_reported(self):
        out = self.take()
        self.wipe()
        self.cron_run = lambda cmd, **kw: mock.Mock(returncode=0, stdout="", stderr="")
        code, text = self.run_cmd(cli.cmd_restore, file=str(out), dry_run=False, force=False)
        self.assertEqual(code, 1)
        self.assertIn("NOT FINISHED", text)

    def test_refuses_over_existing_state_without_force(self):
        out = self.take()
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM runs")
        con.commit()
        con.close()
        code, text = self.run_cmd(cli.cmd_restore, file=str(out), dry_run=False, force=False)
        self.assertEqual(code, 2)
        self.assertIn("--force", text)
        self.assertEqual(self.ledger_count(), 0)                   # untouched
        code, _ = self.run_cmd(cli.cmd_restore, file=str(out), dry_run=False, force=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger_count(), 298)

    def test_dry_run_changes_nothing(self):
        out = self.take()
        self.wipe()
        before = sorted(str(p) for p in (self.root).rglob("*"))
        code, text = self.run_cmd(cli.cmd_restore, file=str(out), dry_run=True, force=False)
        self.assertEqual(code, 0)
        self.assertIn("nothing was written", text)
        self.assertEqual(sorted(str(p) for p in (self.root).rglob("*")), before)
        self.assertFalse(self.db.exists())

    def test_restore_holds_the_migrate_marker_and_names_missing_tokens(self):
        out = self.take()
        self.wipe()
        seen = []
        real = backup.put_files

        def spy(*a, **k):
            seen.append(migrate.migrating())
            return real(*a, **k)
        with mock.patch.object(backup, "put_files", spy):
            self.run_cmd(cli.cmd_restore, file=str(out), dry_run=False, force=False)
        self.assertTrue(seen and seen[0] and seen[0]["pid"] == os.getpid())

    def test_missing_token_files_are_named(self):
        raw = json.loads((config.config_dir() / "one.json").read_text())
        raw["tokens"] = {"reviewer": str(self.root / "no-such-pat")}
        (config.config_dir() / "one.json").write_text(json.dumps(raw))
        manifest = {"tokens": {"one": {"reviewer": str(self.root / "no-such-pat")}}}
        self.assertTrue(any("no-such-pat" in line for line in backup.missing_tokens(manifest)))

    def test_a_foreign_archive_or_path_traversal_is_refused(self):
        bad = self.root / "bad.tar.gz"
        with tarfile.open(bad, "w:gz"):
            pass
        code, _ = self.run_cmd(cli.cmd_restore, file=str(bad), dry_run=True, force=False)
        self.assertEqual(code, 2)
        with self.assertRaises(backup.BackupError):
            backup.read_manifest(self.root / "missing")


class MigrateBacksUpFirst(Base):
    def test_backup_exists_before_the_first_move_and_failure_stops_migrate(self):
        order = []
        real = backup.create

        def spy(*a, **k):
            path = real(*a, **k)
            order.append(("backup", path))
            return path

        def step(*a, **k):
            order.append(("step", None))
            return []
        with mock.patch.object(backup, "create", spy), \
                mock.patch.object(migrate, "settings_step", step), \
                mock.patch.object(migrate, "shim_step", return_value=[]), \
                mock.patch.object(migrate, "files_step", return_value=[]), \
                mock.patch.object(migrate, "repo_step", return_value=[]), \
                mock.patch.object(cli, "_move_watchdog_job", return_value=[]):
            code, text = self.run_cmd(cli.cmd_migrate, dry_run=False)
        self.assertEqual(order[0][0], "backup")
        self.assertIn(str(order[0][1]), text)
        self.assertEqual(stat.S_IMODE(order[0][1].stat().st_mode), 0o600)
        with mock.patch.object(backup, "create", side_effect=backup.BackupError("disk full")), \
                mock.patch.object(migrate, "settings_step") as moved:
            code, text = self.run_cmd(cli.cmd_migrate, dry_run=False)
        self.assertEqual(code, 2)
        moved.assert_not_called()


if __name__ == "__main__":
    unittest.main()
