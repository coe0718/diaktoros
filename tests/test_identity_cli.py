"""#1163 step 6: `hermes dk identity` moves one identity of an existing loop to a GitHub App (or
back to a PAT). The App is proven on GitHub before anything is written; the old login stays on
the allowlist so open PRs keep their verdicts and authorship; a run in flight refuses it."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_cli as app_cli  # noqa: E402
import test_token_file_settings as tfs  # noqa: E402
from diaktoros import gh  # noqa: E402

BOT_REV, BOT_FIX, BOT_READ = "acme-reviewer[bot]", "acme-fixer[bot]", "acme-reader[bot]"


class Identity(app_cli.Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.run_cli(self.app_argv(reviewer=tfs.REV,
                                                    tokens=[(tfs.READER, self.pats["read"]),
                                                            (tfs.REV, self.pats["rev"]),
                                                            (tfs.FIX, self.pats["fix"])]))[0], 0)
        self.resolved = lambda loop, login, fetch=None: {"login": login, "id": 4242}
        patch = mock.patch.object(gh, "principal", side_effect=lambda *a, **k: self.resolved(*a, **k))
        patch.start()
        self.addCleanup(patch.stop)

    def identity(self, *argv):
        return self.run_cli(["identity", "--loop", "widgets", *argv])

    def test_the_reviewer_moves_to_an_app_keeping_the_old_login_allowed(self):
        key = self.key("rev")
        rc, out = self.identity("--seat", "reviewer", "--app", f"{BOT_REV}=5416187:{key}")
        self.assertEqual(rc, 0, out)
        loop = self.loop_json()
        self.assertEqual((loop["seats"]["reviewer"]["login"], loop["reviewer_seat"]),
                         (BOT_REV, BOT_REV))
        self.assertEqual(loop["apps"][BOT_REV], {"app_id": 5416187, "key_file": str(key)})
        self.assertEqual(loop["reviewers"], [tfs.REV, BOT_REV])     # the old login stays
        self.assertIn(tfs.REV, loop["tokens"])                       # and its PAT, until removed
        self.assertIn(f"reviewer: {tfs.REV} → {BOT_REV} (App 5416187", out)
        self.assertIn("hermes dk protect --loop widgets", out)       # the merge check is on now
        self.assert_no_leak()

    def test_the_fixer_and_the_reader_move_and_back_to_a_pat(self):
        rc, out = self.identity("--seat", "fixer", "--app", f"{BOT_FIX}=7:{self.key('fix')}")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.loop_json()["fixers"], [tfs.FIX, BOT_FIX])
        rc, out = self.identity("--seat", "read", "--app", f"{BOT_READ}=8:{self.key('read')}")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.loop_json()["read_token"], BOT_READ)
        rc, out = self.identity("--seat", "fixer", "--pat", f"{tfs.FIX}={self.pats['fix']}")
        self.assertEqual(rc, 0, out)
        loop = self.loop_json()
        self.assertEqual(loop["seats"]["fixer"]["login"], tfs.FIX)
        self.assertNotIn(tfs.FIX, loop["apps"])

    def test_nothing_is_written_unless_github_resolves_the_login(self):
        before = self.loop_file().read_bytes()
        key = self.key("rev")
        for resolved in (lambda *a, **k: None, lambda *a, **k: {"login": "someone-else", "id": 1},
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not installed"))):
            with self.subTest(resolved=resolved):
                self.resolved = resolved
                rc, out = self.identity("--seat", "reviewer", "--app", f"{BOT_REV}=1:{key}")
                self.assertEqual(rc, 2, out)
                self.assertIn("nothing written", out)
        self.assertEqual(self.loop_file().read_bytes(), before)

    def test_dry_run_proves_it_and_writes_nothing(self):
        before = self.loop_file().read_bytes()
        rc, out = self.identity("--seat", "reviewer", "--app", f"{BOT_REV}=1:{self.key('rev')}",
                                "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("dry run", out)
        self.assertIn("resolved on GitHub as acme-reviewer[bot]", out)
        self.assertEqual(self.loop_file().read_bytes(), before)

    def test_a_run_in_flight_refuses_it(self):
        from diaktoros import config, ledger
        from diaktoros.run_supervisor import Supervisor
        sup = Supervisor(config.home() / "state" / "diaktoros-runs.sqlite")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d", "acme/widgets", 7, "a" * 40, "reviewer")
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running'")
        before = self.loop_file().read_bytes()
        rc, out = self.identity("--seat", "reviewer", "--app", f"{BOT_REV}=1:{self.key('rev')}")
        self.assertEqual(rc, 2, out)
        self.assertIn("in flight", out)
        self.assertEqual(self.loop_file().read_bytes(), before)
        rc, out = self.identity("--seat", "fixer", "--app", f"{BOT_FIX}=7:{self.key('fix')}")
        self.assertEqual(rc, 0, out)                                 # another identity is free

    def test_usage_refusals(self):
        self.assertEqual(self.identity("--seat", "reviewer")[0], 2)            # neither
        rc, out = self.identity("--seat", "reviewer", "--app", f"{BOT_REV}=1:{self.key('rev')}",
                                "--pat", f"{tfs.REV}={self.pats['rev']}")
        self.assertEqual(rc, 2, out)
        rc, out = self.identity("--seat", "adjudicator", "--app", f"x[bot]=1:{self.key('adj')}")
        self.assertEqual(rc, 2, out)
        self.assertIn("no adjudicator route", out)


if __name__ == "__main__":
    unittest.main()
