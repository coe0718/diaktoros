"""#904: ``hermes dk update`` lists the commits and merged PRs an update picks up, and with
``--apply`` installs it, runs doctor, and refuses while a run is live."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import cli, config, doctor, gh, migrate  # noqa: E402
from diaktoros.cli import update as cli_update  # noqa: E402

OLD = "7292c9d" + "0" * 33
NEW = "ab12cd3" + "0" * 33


def commit(message, sha="f" * 40):
    return {"sha": sha, "commit": {"message": message}}


class UpdateCommand(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = pathlib.Path(temp.name)
        patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(root / "hermes"),
                                             "DIAKTOROS_CONFIG_DIR": str(root / "loops")})
        patch.start()
        self.addCleanup(patch.stop)
        self.loop = config.normalize({
            "id": "w", "repo": "acme/w", "fixers": ["fix"], "reviewers": ["rev"],
            "seats": {"reviewer": {"profile": "p1", "route": "r", "login": "rev"},
                      "fixer": {"profile": "p2", "route": "f", "login": "fix"}},
            "read_token": "reader", "tokens": {}, "state_dir": str(root / "state")})
        self.compare = {"status": "ahead", "ahead_by": 3, "commits": [
            commit("Fix a thing (#876)", "1" * 40),
            commit("direct push to main", "2" * 40),
            commit("Merge pull request #898 from x/y\n\nthe fail-then-pass proof resolves test IDs",
                   NEW)]}
        self.fail_compare = ""
        self.calls = []
        for target, value in (
                (mock.patch.object(cli_update, "_one_loop", side_effect=lambda a: self.loop), 0),
                (mock.patch.object(doctor, "installed_commit", side_effect=self.installed), 0),
                (mock.patch.object(gh, "request", side_effect=self.fake), 0)):
            target.start()
            self.addCleanup(target.stop)
        self.installed_sha = OLD

    def installed(self):
        return self.installed_sha

    def fake(self, loop, path, method="GET", body=None, login=None):
        self.calls.append(path)
        if "/compare/" in path:
            if self.fail_compare:
                return gh.Response(None, self.fail_compare, 500, {})
            return gh.Response(self.compare, "", 200, {})
        if "/commits/" in path:
            return gh.Response({"sha": NEW}, "", 200, {})
        return gh.Response(None, "HTTP 404", 404, {})

    def run_update(self, *argv):
        parser = argparse.ArgumentParser()
        cli_update.add_update(parser.add_subparsers())
        args = parser.parse_args(["update", *argv])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = args.func(args)
        return rc, out.getvalue()

    # --- the check ---
    def test_lists_commit_count_prs_and_install_line(self):
        rc, out = self.run_update()
        self.assertEqual(rc, 0, out)
        self.assertIn("3 commits, 2 PRs", out)
        self.assertIn("#898 the fail-then-pass proof resolves test IDs", out)
        self.assertIn("#876 Fix a thing", out)
        self.assertLess(out.index("#898"), out.index("#876"))        # newest first
        self.assertIn(f"hermes plugins install coe0718/diaktoros --force --ref {NEW}", out)
        self.assertIn(f"installed: {OLD[:7]}", out)

    def test_ref_is_the_target_and_the_default_check_installs_nothing(self):
        with mock.patch.object(cli_update.subprocess, "run") as run:
            rc, out = self.run_update("--ref", "v0.3.0")
        self.assertEqual(rc, 0)
        run.assert_not_called()
        self.assertTrue(any(p.endswith(f"/compare/{OLD}...v0.3.0") for p in self.calls), self.calls)
        self.assertIn("target:    v0.3.0", out)

    def test_up_to_date(self):
        self.compare = {"status": "identical", "ahead_by": 0, "commits": []}
        rc, out = self.run_update()
        self.assertIn("picks up:  nothing", out)

    def test_shares_doctors_comparison(self):
        # the count update prints is the one doctor's plugin-version reports
        with mock.patch.object(doctor, "compare_plugin",
                               wraps=doctor.compare_plugin) as shared:
            self.run_update()
        shared.assert_called_once()
        self.compare = {"status": "ahead", "ahead_by": 3, "commits": []}

        def fake(loop, path, method="GET", body=None, login=None):
            if path.endswith("/releases/latest"):
                return gh.Response({"tag_name": "v0.2.0"}, "", 200, {})
            if "/compare/v0.2.0..." in path:
                return gh.Response({"status": "ahead"}, "", 200, {})
            return self.fake(loop, path)
        with mock.patch.object(gh, "request", side_effect=fake):
            check = doctor.check_plugin_version(self.loop)
        self.assertIn("3 commits behind main", check.detail)

    def test_partial_compare_says_so(self):
        self.compare = {"status": "ahead", "ahead_by": 400,
                        "commits": [commit("A (#1)", "1" * 40)]}
        rc, out = self.run_update()
        self.assertIn("400 commits", out)
        self.assertIn("partial", out)

    # --- offline / unreadable ---
    def test_unreadable_compare_guesses_no_count_and_prints_the_install_line(self):
        self.fail_compare = "URLError: no network"
        rc, out = self.run_update()
        self.assertEqual(rc, 0, out)
        self.assertIn("unknown", out)
        self.assertNotRegex(out, r"\d+ commits")
        self.assertIn("hermes plugins install coe0718/diaktoros --force --ref", out)

    def test_not_a_checkout_is_unknown_and_never_compares(self):
        self.installed_sha = ""
        rc, out = self.run_update()
        self.assertEqual(rc, 0)
        self.assertIn("installed: unknown", out)
        self.assertFalse(any("/compare/" in p for p in self.calls))
        self.assertIn("hermes plugins install coe0718/diaktoros --force --ref", out)

    # --- --apply ---
    def apply(self, busy=0, installed_after=NEW, install_rc=0, doctor_rc=0, runs_error=None):
        ran = []

        def fake_run(cmd, **kw):
            ran.append(cmd)
            self.installed_sha = installed_after if "install" in cmd else self.installed_sha
            return mock.Mock(returncode=install_rc if "install" in cmd else doctor_rc)
        flight = (mock.patch.object(migrate, "runs_in_flight", side_effect=runs_error)
                  if runs_error else mock.patch.object(migrate, "runs_in_flight", return_value=busy))
        with flight, mock.patch.object(cli_update.cli_common, "_hermes_bin", return_value="/bin/hermes"), \
             mock.patch.object(cli_update.subprocess, "run", side_effect=fake_run):
            rc, out = self.run_update("--apply")
        return rc, out, ran

    def test_apply_installs_then_runs_doctor_and_reports_the_new_commit(self):
        rc, out, ran = self.apply()
        self.assertEqual(rc, 0, out)
        self.assertEqual(ran[0], ["/bin/hermes", "plugins", "install", "coe0718/diaktoros",
                                  "--force", "--ref", NEW])
        self.assertEqual(ran[1][:4], ["/bin/hermes", "dk", "doctor", "--loop"])
        self.assertIn(f"installed now: {NEW[:7]}", out)

    def test_apply_refuses_while_a_run_is_live(self):
        rc, out, ran = self.apply(busy=2)
        self.assertEqual(rc, 2)
        self.assertEqual(ran, [])
        self.assertIn("2 run(s) are in flight", out)

    def test_apply_refuses_when_the_ledger_cannot_be_read(self):
        rc, out, ran = self.apply(runs_error=migrate.RunsUnknown("ledger unreadable"))
        self.assertEqual(rc, 2)
        self.assertEqual(ran, [])
        self.assertIn("ledger unreadable", out)

    def test_apply_with_a_failed_install_runs_no_doctor(self):
        rc, out, ran = self.apply(install_rc=1)
        self.assertEqual(rc, 1)
        self.assertEqual(len(ran), 1)
        self.assertIn("update FAILED", out)

    def test_apply_fails_when_the_commit_is_not_the_expected_one(self):
        rc, out, ran = self.apply(installed_after=OLD)
        self.assertEqual(rc, 1)
        self.assertIn("expected", out)

    def test_apply_fails_when_doctor_fails(self):
        rc, out, ran = self.apply(doctor_rc=1)
        self.assertEqual(rc, 1)

    def test_apply_offline_installs_nothing(self):
        self.fail_compare = "URLError: no network"
        with mock.patch.object(gh, "request", return_value=gh.Response(None, "URLError", None, {})):
            rc, out, ran = self.apply()
        self.assertEqual(rc, 2)
        self.assertEqual(ran, [])
        self.assertIn("not applied", out)
        self.assertIn("hermes plugins install coe0718/diaktoros --force --ref", out)

    def test_apply_holds_the_migrate_lock(self):
        with mock.patch.object(migrate, "hold", side_effect=migrate.MigrationBusy("a migrate is running")):
            with mock.patch.object(cli_update.subprocess, "run") as run, \
                 mock.patch.object(cli_update.cli_common, "_hermes_bin", return_value="/bin/hermes"):
                rc, out = self.run_update("--apply")
        self.assertEqual(rc, 2)
        run.assert_not_called()

    # --- helpers ---
    def test_merged_prs(self):
        got = cli_update.merged_prs([commit("A (#1)"), commit("Merge pull request #2 from a/b\n\nT2"),
                                     commit("no pr"), commit("A again (#1)")])
        self.assertEqual(got, [(1, "A again"), (2, "T2")])      # newest first, one entry per PR

    def test_registered_on_the_real_parser(self):
        class Ctx:
            def register_cli_command(self, name, summary, setup, **kw):
                self.setup = setup
        ctx = Ctx()
        cli.register_cli(ctx, settings={})
        parser = argparse.ArgumentParser()
        ctx.setup(parser)
        args = parser.parse_args(["update", "--apply", "--ref", "v1"])
        self.assertIs(args.func, cli_update.cmd_update)


if __name__ == "__main__":
    unittest.main()
