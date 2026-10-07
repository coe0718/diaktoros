"""`migrate` (#425): moving an install from the hermes-review-loop plugin to its new name.

Offline, with disposable config/state only: the settings copy goes through a fake plugin context,
GitHub is mocked, and the run ledger and state files are real ones in a temporary home.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from review_loop import cli, config, gate_shims, gh, ledger, migrate, run_supervisor  # noqa: E402
from review_loop.run_supervisor import Supervisor  # noqa: E402

OLD, NEW = fg.REPO, "owner/renamed"


class FakeCtx:
    def __init__(self, plugin_id="diaktoros", settings=None):
        self.plugin_id = plugin_id
        self.settings = dict(settings or {})

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value


def hermes_config(settings=None, legacy=None):
    entry = {}
    if settings is not None:
        entry["settings"] = settings
    if legacy is not None:
        entry["config"] = legacy
    return lambda: {"plugins": {"entries": {migrate.OLD_PLUGIN: entry}}}


class Settings(unittest.TestCase):
    def test_copies_only_known_unset_values(self):
        ctx = FakeCtx(settings={"base": "develop"})
        read = hermes_config({"cap": 5, "base": "main", "host": "", "bogus": 1},
                             legacy={"grace_min": 30, "cap": 2})
        lines = migrate.settings_step(ctx, dry_run=False, read=read)
        self.assertEqual(ctx.settings, {"base": "develop", "cap": 5, "grace_min": 30})
        self.assertIn("settings: copied cap", lines)
        self.assertIn("settings: base already set here — kept", lines)
        self.assertFalse(any("bogus" in line or "host" in line for line in lines))

    def test_dry_run_and_the_old_plugin_itself_write_nothing(self):
        ctx = FakeCtx()
        lines = migrate.settings_step(ctx, dry_run=True, read=hermes_config({"cap": 5}))
        self.assertEqual(ctx.settings, {})
        self.assertEqual(lines, ["settings: would copy cap"])
        old = FakeCtx(plugin_id=migrate.OLD_PLUGIN)
        self.assertIn("nothing to copy",
                      migrate.settings_step(old, dry_run=False, read=hermes_config({"cap": 5}))[0])
        self.assertEqual(old.settings, {})
        self.assertIn("skipped", migrate.settings_step(None, dry_run=False)[0])

    def test_an_unreadable_or_absent_config_copies_nothing(self):
        ctx = FakeCtx()
        def broken():
            raise OSError("bad yaml")
        self.assertIn("could not read", migrate.settings_step(ctx, dry_run=False, read=broken)[0])
        self.assertIn("no settings", migrate.settings_step(ctx, dry_run=False, read=dict)[0])
        self.assertEqual(ctx.settings, {})


class Swap(unittest.TestCase):
    def test_moves_the_name_its_keys_and_its_urls_only(self):
        data = {OLD: 1, f"{OLD}#7": {"url": f"https://github.com/{OLD}/pull/7", "repo": OLD},
                "other": [f"{OLD}-fork", f"https://github.com/{OLD}-fork/pull/1", "x"]}
        self.assertEqual(migrate._swap(data, OLD, NEW),
                         {NEW: 1, f"{NEW}#7": {"url": f"https://github.com/{NEW}/pull/7", "repo": NEW},
                          "other": [f"{OLD}-fork", f"https://github.com/{OLD}-fork/pull/1", "x"]})


def github(answers: dict):
    return mock.patch.object(gh, "api", side_effect=lambda loop, path, **kw: answers.get(path))


class Repo(fg.Base):
    def db(self) -> Path:
        return config.home() / "state" / "diaktoros-runs.sqlite"

    def supervisor(self) -> Supervisor:
        sup = Supervisor(self.db())
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d1", OLD, 7, fg.HEAD, "reviewer")
        return sup

    def renamed(self):
        return github({f"/repos/{OLD}": {"id": 9, "full_name": NEW},
                       f"/repos/{NEW}": {"id": 9, "full_name": NEW}})

    def run_step(self, dry_run=False):
        with self.renamed():
            return migrate.repo_step([config.load_id("one")], cli._write_moved_repo, dry_run=dry_run,
                                     ledger=self.db())

    def repos_in_ledger(self) -> list:
        with ledger.connect(self.db()) as con:
            return [row[0] for row in con.execute("SELECT repo FROM runs")]

    def test_renamed_to_needs_the_same_repository_twice(self):
        with self.renamed():
            self.assertEqual(migrate.renamed_to(self.loop), (NEW, ""))
        with github({f"/repos/{OLD}": {"id": 9, "full_name": NEW},
                     f"/repos/{NEW}": {"id": 10, "full_name": NEW}}):
            new, why = migrate.renamed_to(self.loop)
            self.assertIsNone(new)
            self.assertIn("not the same repository", why)
        with github({f"/repos/{OLD}": {"id": 9, "full_name": OLD.upper()}}):
            self.assertEqual(migrate.renamed_to(self.loop), (None, ""))
        with github({}):
            self.assertIn("could not read", migrate.renamed_to(self.loop)[1])

    def test_moves_ledger_state_and_loop_file_and_a_rerun_is_a_no_op(self):
        self.supervisor()
        self.st.queue_add("reviewer", f"{OLD}#7", fg.HEAD, f"https://github.com/{OLD}/pull/7", "q")
        lines = self.run_step(dry_run=True)
        self.assertIn("would move", lines[0])
        self.assertEqual(config.load_id("one")["repo"], OLD)          # dry run: nothing moved
        self.assertEqual(self.repos_in_ledger(), [OLD])
        self.assertEqual(list(self.st.queue_items("reviewer")), [f"{OLD}#7"])
        lines = self.run_step()
        self.assertIn(f"moved {OLD} → {NEW}", lines[0])
        self.assertEqual(config.load_id("one")["repo"], NEW)
        self.assertEqual(self.repos_in_ledger(), [NEW])
        queued = self.st.queue_items("reviewer")
        self.assertEqual(list(queued), [f"{NEW}#7"])
        self.assertEqual(queued[f"{NEW}#7"]["url"], f"https://github.com/{NEW}/pull/7")
        with github({f"/repos/{NEW}": {"id": 9, "full_name": NEW}}):
            again = migrate.repo_step([config.load_id("one")], cli._write_moved_repo, dry_run=False,
                                      ledger=self.db())
        self.assertIn("has not been renamed", again[0])

    def test_a_mixed_case_name_is_stored_as_the_loop_reads_it(self):
        # GitHub's full_name keeps the owner's casing; the loop lowercases every repo it reads,
        # and the ledger compares case-sensitively, so the moved records must be lowercase.
        self.supervisor()
        self.st.queue_add("reviewer", f"{OLD}#7", fg.HEAD, "u", "q")
        mixed = "Owner/Renamed"
        with github({f"/repos/{OLD}": {"id": 9, "full_name": mixed},
                     f"/repos/{mixed}": {"id": 9, "full_name": mixed}}):
            self.assertEqual(migrate.renamed_to(self.loop), (NEW, ""))
            migrate.repo_step([config.load_id("one")], cli._write_moved_repo, dry_run=False,
                              ledger=self.db())
        loop = config.load_id("one")
        self.assertEqual(loop["repo"], NEW)
        with ledger.connect(self.db()) as con:
            rows = con.execute("SELECT COUNT(*) FROM runs WHERE repo=?", (loop["repo"],)).fetchone()
        self.assertEqual(rows[0], 1)
        self.assertEqual(list(self.st.queue_items("reviewer")), [f"{loop['repo']}#7"])

    def test_a_run_in_flight_refuses_and_moves_nothing(self):
        self.supervisor()
        with ledger.connect(self.db()) as con:
            con.execute("UPDATE runs SET state='running'")
        self.st.queue_add("reviewer", f"{OLD}#7", fg.HEAD, "u", "q")
        lines = self.run_step()
        self.assertIn("REFUSED", lines[0])
        self.assertEqual(config.load_id("one")["repo"], OLD)
        self.assertEqual(self.repos_in_ledger(), [OLD])
        self.assertEqual(list(self.st.queue_items("reviewer")), [f"{OLD}#7"])


class Writer(fg.Base):
    def test_only_repo_moves_and_the_current_push_policy_stays(self):
        stale = config.load_id("one")                       # read while pushes were off
        self.set_push(True)                                 # the operator opted in since
        cli._write_moved_repo(stale, NEW)
        moved = config.load_id("one")
        self.assertEqual(moved["repo"], NEW)
        self.assertTrue(moved["unattended_fixer_push"])
        with self.assertRaisesRegex(config.ConfigError, "repository changed during migrate"):
            cli._write_moved_repo(stale, "owner/third")     # it no longer names the old repo
        self.assertEqual(config.load_id("one")["repo"], NEW)
        # The ordinary writer still refuses any repository change.
        with self.assertRaisesRegex(config.ConfigError, "repository changed"):
            cli._write_config({**moved, "repo": OLD})


class Shims(fg.Base):
    def test_watchdog_shim_repointed_and_a_foreign_gate_shim_reported(self):
        shim = self.root / "watchdog-shim.py"
        shim.write_text("old")
        write = mock.Mock()
        with mock.patch.object(gate_shims, "install", return_value=["gate shim rewrote: x"]):
            lines = migrate.shim_step([self.loop], write, shim, "new", dry_run=False)
        write.assert_called_once()
        self.assertIn("shims: one: gate shim rewrote: x", lines)
        with mock.patch.object(gate_shims, "install", side_effect=gate_shims.ShimError("foreign")):
            lines = migrate.shim_step([self.loop], write, shim, "old", dry_run=False)
        self.assertEqual(lines, ["shims: one: NOT rewritten — foreign"])


class Command(fg.Base):
    def test_dry_run_writes_nothing_and_a_refusal_exits_1(self):
        with mock.patch.object(migrate, "settings_step", return_value=["settings: would copy cap"]), \
             mock.patch.object(migrate, "repo_step", return_value=["repo: one: has not been renamed"]), \
             mock.patch.object(migrate, "shim_step", return_value=["shims: ok"]), \
             mock.patch.object(migrate, "doctor_step") as doctor, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_migrate(argparse.Namespace(dry_run=True)), 0)
        doctor.assert_not_called()
        self.assertIn("dry run: nothing was written", out.getvalue())
        with mock.patch.object(migrate, "settings_step", return_value=[]), \
             mock.patch.object(migrate, "repo_step", return_value=["repo: one: REFUSED — busy"]), \
             mock.patch.object(migrate, "shim_step", return_value=[]), \
             mock.patch.object(migrate, "doctor_step", return_value=["doctor: every check verified"]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_migrate(argparse.Namespace(dry_run=False)), 1)


if __name__ == "__main__":
    unittest.main()
