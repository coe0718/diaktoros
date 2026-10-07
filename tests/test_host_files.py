"""#425 stage 4: the host files take the new name, and `migrate` moves an install's old ones.

Each host file (`config.HOST_FILES`) is used where it already is: an install not yet migrated
keeps its `review-loops.d`, `review-loop-runs.sqlite` and the rest; a fresh one gets the new
names. `migrate` moves them with the install paused and no run in flight, and the shared watchdog
job and its shim move as a pair through the scheduler's own CLI (faked here).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import cli, config, migrate, pacing, run_supervisor  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

LOOP = {"id": "one", "repo": "owner/one", "base": "main", "cap": 3, "fixers": ["fixer"],
        "reviewers": ["reviewer"], "reviewer_seat": "reviewer", "read_token": "reviewer",
        "seats": {"reviewer": {"route": "one-review", "profile": "reviewer"},
                  "fixer": {"route": "one-fix", "profile": "fixer"}}}


class Home(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DIAKTOROS_CONFIG_DIR", None)

    def old_install(self):
        """An install from before the rename: every host file under its old name."""
        (self.home / "review-loops.d").mkdir()
        state = self.home / "state" / "review-loops" / "one"
        state.mkdir(parents=True)
        (state / "pending.json").write_text("{}")
        (self.home / "review-loops.d" / "one.json").write_text(
            json.dumps({**LOOP, "state_dir": str(state)}))
        (self.home / "review-loop-runtime.json").write_text("{}")
        (self.home / "state" / "review-loop-gate-failures").mkdir()
        (self.home / "state" / "review-loop-seat-locks").mkdir()
        (self.home / "state" / "review-loop-pacing.json").write_text("{}")
        sup = Supervisor(self.home / "state" / "review-loop-runs.sqlite")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d1", "owner/one", 7, "a" * 40, "reviewer")
        return sup


class Paths(Home):
    def test_a_fresh_install_gets_the_new_names(self):
        self.assertEqual(config.config_dir(), self.home / "diaktoros.d")
        self.assertEqual(run_supervisor.production_ledger(),
                         self.home / "state" / "diaktoros-runs.sqlite")
        self.assertEqual(config.default_state_dir("one"), self.home / "state" / "diaktoros" / "one")
        self.assertEqual(pacing.path(), self.home / "state" / "diaktoros-pacing.json")
        self.assertEqual((config.watchdog_shim().name, config.watchdog_job_name()),
                         ("diaktoros-watchdog.py", "diaktoros watchdog"))

    def test_an_install_not_yet_migrated_keeps_reading_its_files(self):
        self.old_install()
        (self.home / "scripts").mkdir()
        (self.home / "scripts" / "review-loop-watchdog.py").write_text("")
        self.assertEqual(config.config_dir(), self.home / "review-loops.d")
        self.assertEqual(config.load_id("one")["repo"], "owner/one")
        self.assertEqual(Supervisor(run_supervisor.production_ledger()).get("d1")["pr"], 7)
        self.assertEqual(config.default_state_dir("two"),
                         self.home / "state" / "review-loops" / "two")
        self.assertEqual((config.watchdog_shim().name, config.watchdog_job_name()),
                         ("review-loop-watchdog.py", "review loop watchdog"))

    def test_the_new_name_wins_once_it_exists(self):
        (self.home / "review-loops.d").mkdir()
        (self.home / "diaktoros.d").mkdir()
        self.assertEqual(config.config_dir(), self.home / "diaktoros.d")

    def test_both_default_state_roots_count_as_default(self):
        for root in ("diaktoros", "review-loops"):
            self.assertTrue(config.is_default_state_dir(self.home / "state" / root / "one", "one"))
        self.assertFalse(config.is_default_state_dir("/elsewhere/one", "one"))


class Doctor(Home):
    def test_doctor_names_the_old_files_and_the_command_that_moves_them(self):
        from diaktoros import doctor
        self.assertEqual(doctor.check_host_names().status, doctor.VERIFIED)
        self.old_install()
        check = doctor.check_host_names()
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("review-loops.d", check.detail)
        self.assertIn("hermes dk migrate", check.fix)


class Move(Home):
    def step(self, dry_run=False):
        return migrate.files_step(cli._write_config, dry_run=dry_run)

    def test_every_file_moves_and_the_loop_follows_and_a_rerun_is_a_no_op(self):
        self.old_install()
        lines = self.step()
        self.assertTrue(any("moved" in line for line in lines), lines)
        for new in ("diaktoros.d/one.json", "diaktoros-runtime.json",
                    "state/diaktoros-runs.sqlite", "state/diaktoros/one/pending.json",
                    "state/diaktoros-gate-failures", "state/diaktoros-seat-locks",
                    "state/diaktoros-pacing.json"):
            self.assertTrue((self.home / new).exists(), new)
        for old in ("review-loops.d", "review-loop-runtime.json", "state/review-loop-runs.sqlite",
                    "state/review-loops", "state/review-loop-gate-failures",
                    "state/review-loop-seat-locks", "state/review-loop-pacing.json"):
            self.assertFalse((self.home / old).exists(), old)
        loop = config.load_id("one")
        self.assertEqual(loop["state_dir"], str(self.home / "state" / "diaktoros" / "one"))
        self.assertEqual(Supervisor(run_supervisor.production_ledger()).get("d1")["pr"], 7)
        self.assertEqual(self.step(), ["files: every host file already has its new name"])

    def test_dry_run_moves_nothing(self):
        self.old_install()
        lines = self.step(dry_run=True)
        self.assertTrue(any("would move" in line for line in lines))
        self.assertTrue((self.home / "review-loops.d" / "one.json").exists())
        self.assertFalse((self.home / "diaktoros.d").exists())

    def test_a_run_in_flight_refuses_before_anything_moves(self):
        self.old_install()
        con = sqlite3.connect(self.home / "state" / "review-loop-runs.sqlite")
        con.execute("UPDATE runs SET state='running'")
        con.commit()
        con.close()
        self.assertIn("REFUSED", self.step()[0])
        self.assertTrue((self.home / "review-loops.d").exists())
        self.assertTrue((self.home / "state" / "review-loop-runs.sqlite").exists())

    def test_both_names_present_is_left_for_a_person(self):
        self.old_install()
        (self.home / "diaktoros-runtime.json").write_text("{}")
        lines = self.step()
        self.assertTrue(any("REFUSED — both" in line and "runtime" in line for line in lines), lines)
        self.assertTrue((self.home / "review-loop-runtime.json").exists())
        self.assertTrue((self.home / "diaktoros.d" / "one.json").exists())   # the rest moved


class Watchdog(Home):
    def setUp(self):
        super().setUp()
        (self.home / "scripts").mkdir()
        (self.home / "scripts" / "review-loop-watchdog.py").write_text("old")
        (self.home / "cron").mkdir()
        self.jobs = [{"id": "w1", "name": "review loop watchdog", "script": "review-loop-watchdog.py",
                      "no_agent": True, "schedule": {"kind": "interval", "minutes": 15},
                      "deliver": "telegram"}]
        self.save()
        self.commands: list = []

    def save(self):
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": self.jobs}))

    def hermes(self, cmd, **kw):
        self.commands.append(cmd[1:])
        if cmd[1:3] == ["cron", "create"] and self.fail_create == "silently":
            return subprocess.CompletedProcess(cmd, 0, "", "")      # says yes, stores nothing
        if cmd[1:3] == ["cron", "create"] and not self.fail_create:
            self.jobs.append({"id": "w2", "name": cmd[cmd.index("--name") + 1],
                              "script": cmd[cmd.index("--script") + 1], "no_agent": True,
                              "schedule": cmd[3], "deliver": cmd[cmd.index("--deliver") + 1]})
        elif cmd[1:3] == ["cron", "remove"]:
            self.jobs = [job for job in self.jobs if job["id"] != cmd[3]]
        else:
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        self.save()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def move(self, fail_create=False):
        self.fail_create = fail_create
        with mock.patch.object(cli.subprocess, "run", side_effect=self.hermes), \
                mock.patch.object(cli, "_hermes_bin", return_value="hermes"):
            return cli._move_watchdog_job(dry_run=False)

    def test_the_job_and_shim_move_as_a_pair_with_the_old_schedule(self):
        lines = self.move()
        self.assertEqual([job["name"] for job in self.jobs], ["diaktoros watchdog"])
        self.assertEqual(self.jobs[0]["script"], "diaktoros-watchdog.py")
        self.assertEqual((self.jobs[0]["schedule"], self.jobs[0]["deliver"]), ("15m", "telegram"))
        self.assertTrue((self.home / "scripts" / "diaktoros-watchdog.py").exists())
        self.assertFalse((self.home / "scripts" / "review-loop-watchdog.py").exists())
        self.assertEqual(config.watchdog_job_name(), "diaktoros watchdog")
        self.assertFalse(any("NOT FINISHED" in line for line in lines), lines)
        self.assertEqual(self.move(), [])                 # nothing old left: a no-op

    def test_a_failed_create_keeps_the_old_pair_live(self):
        for how in (True, "silently"):
            with self.subTest(how=how):
                self.check_failed_create(how)

    def check_failed_create(self, how):
        lines = self.move(fail_create=how)
        self.assertIn("NOT FINISHED", lines[-1])
        self.assertEqual([job["name"] for job in self.jobs], ["review loop watchdog"])
        self.assertFalse((self.home / "scripts" / "diaktoros-watchdog.py").exists())
        self.assertEqual(config.watchdog_job_name(), "review loop watchdog")
        self.assertNotIn(["cron", "remove", "w1"], self.commands)


class Command(Home):
    def test_migrate_moves_the_files_under_the_pause(self):
        self.old_install()
        seen, real = {}, migrate.files_step

        def files(write_loop, *, dry_run):
            seen["paused"] = migrate.migrating() is not None
            return real(write_loop, dry_run=dry_run)
        with mock.patch.object(migrate, "files_step", side_effect=files), \
                mock.patch.object(migrate, "repo_step", return_value=[]), \
                mock.patch.object(migrate, "shim_step", return_value=[]), \
                mock.patch.object(migrate, "doctor_step", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_migrate(argparse.Namespace(dry_run=False, rename_loop=None,
                                                    admin_token=None))
        self.assertEqual(rc, 0, out.getvalue())
        self.assertTrue(seen["paused"])
        self.assertTrue((self.home / "diaktoros.d" / "one.json").exists())


if __name__ == "__main__":
    unittest.main()
