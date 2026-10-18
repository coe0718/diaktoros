"""Fixer scope lock (#482): a fix round outside its findings' files holds for a maintainer."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import fcntl
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import broker_ipc, config, findings, gh, observer, safe_push, scope_lock  # noqa: E402
from diaktoros import state as state_mod  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402
from test_safe_push import HEAD, NEW_HEAD, REPO, FakeGitHub, manifest  # noqa: E402

FINDINGS = "F1: src/a.py:12: leaks the handle\n"


class ScopeLock(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root),
                                           "DIAKTOROS_CONFIG_DIR": str(self.root / "review-loops.d")})
        env.start()
        self.addCleanup(env.stop)
        tokens = {}
        for login in ("read", "review", "fix"):
            path = self.root / (login + ".pat")
            path.write_text("DUMMY_SECRET_" + login)
            tokens[login] = str(path)
        self.loop = {"repo": REPO, "id": "l", "base": "main", "state_dir": str(self.root),
                     "unattended_fixer_push": True, "fixer_scope_lock": True,
                     "fixers": ["fix"], "reviewers": ["review"], "tokens": tokens,
                     "read_token": "read", "reviewer_seat": "review",
                     "seats": {"reviewer": {"login": "review"}, "fixer": {"login": "fix"}}}
        self.fake = FakeGitHub()
        self.pushed = []
        self.notices = []
        for patch in (
                mock.patch.object(gh, "api", side_effect=self.fake.api),
                mock.patch.object(gh, "reviews", return_value=[{
                    "id": 41, "state": "CHANGES_REQUESTED", "commit_id": HEAD,
                    "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "review"}}]),
                mock.patch.object(safe_push, "_git_cas", side_effect=self.cas),
                mock.patch.object(observer, "notify",
                                  side_effect=lambda *a, **k: self.notices.append((a, k)))):
            patch.start()
            self.addCleanup(patch.stop)
        findings.record(self.loop, 7, "9" * 40, FINDINGS)
        self.st = state_mod.state_for(self.loop)

    def cas(self, loop, repo, branch, head, files, message, login, identity, *, before_push):
        before_push(NEW_HEAD)
        self.pushed.append([p for p, _ in files])
        self.fake.branch_head = self.fake.pr_head = NEW_HEAD
        return NEW_HEAD

    def push(self, path):
        return safe_push.push(self.loop, repo=REPO, number=7, head=HEAD, role="fixer",
                              branch="fix-7", manifest=manifest(path))

    def test_in_scope_push_goes_through(self):
        for path in ("src/a.py", "tests/test_a.py", "docs/a.md"):
            self.fake.branch_head = self.fake.pr_head = HEAD
            self.assertEqual(self.push(path)["outcome"], "published")
        self.assertEqual(self.st.scope_holds(), {})

    def test_out_of_scope_push_holds_with_files_listed(self):
        with self.assertRaises(scope_lock.ScopeHold) as ctx:
            self.push("src/helper.py")
        self.assertIn("src/helper.py", str(ctx.exception))
        self.assertEqual(self.pushed, [])                 # nothing was written
        self.assertEqual(self.st.scope_holds()[7]["files"], ["src/helper.py"])
        self.assertEqual(len(self.notices), 1)
        self.assertIn("src/helper.py", self.notices[0][1]["outcome"])
        with self.assertRaises(scope_lock.ScopeHold):      # same hold: no second notice
            self.push("src/helper.py")
        self.assertEqual(len(self.notices), 1)

    def test_release_lets_it_through(self):
        with self.assertRaises(scope_lock.ScopeHold):
            self.push("src/helper.py")
        self.assertEqual(scope_lock.release(self.loop, 7)["released"], True)
        self.assertEqual(self.push("src/helper.py")["outcome"], "published")
        self.assertEqual(self.pushed, [["src/helper.py"]])

    def test_release_covers_only_the_files_shown(self):
        with self.assertRaises(scope_lock.ScopeHold):
            self.push("src/helper.py")
        scope_lock.release(self.loop, 7)
        with self.assertRaises(scope_lock.ScopeHold):
            self.push("src/other.py")

    def test_compare_release_holds_the_writers_lock_through_save(self):
        hold = {"head": HEAD, "files": ["src/helper.py"], "released": False}
        self.st.scope_hold_set(7, hold)
        self.st.scope_hold_set(8, hold)
        expected = self.st.scope_hold_snapshot(7)
        real_save = self.st._save_strict

        def save(path, data):
            # An independently opened descriptor uses the same flock as another worker.
            # Nonblocking acquisition makes a missing compare/write lock fail without sleeps.
            with (self.st.dir / "state.lock").open("r+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return real_save(path, data)

        with mock.patch.object(self.st, "_save_strict", side_effect=save) as saved:
            released = self.st.scope_hold_release(7, expected)
            self.assertIsNotNone(released)
            assert released is not None
            self.assertTrue(released["released"])
        saved.assert_called_once()
        self.assertEqual(self.st.scope_holds()[8], hold)
        with (self.st.dir / "state.lock").open("r+") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_compare_release_never_recreates_a_dropped_hold(self):
        self.st.scope_hold_set(7, {"head": HEAD, "files": ["src/helper.py"], "released": False})
        expected = scope_lock.snapshot(self.loop, 7)
        self.st.scope_hold_drop(7)
        self.assertIsNone(scope_lock.release(self.loop, 7, expected=expected))
        self.assertNotIn(7, self.st.scope_holds())

    def test_off_by_default_no_check(self):
        self.loop["fixer_scope_lock"] = False
        self.assertEqual(self.push("src/helper.py")["outcome"], "published")
        self.assertIs(config.DEFAULTS["fixer_scope_lock"], False)
        self.assertFalse(config.fixer_scope_lock({}))

    def test_no_open_finding_means_no_scope(self):
        findings.record(self.loop, 8, "9" * 40, "")
        self.assertEqual(scope_lock.extras(self.st.findings_get(8), ["src/x.py"]), [])

    def test_broker_push_holds_then_release_goes_through(self):
        """The broker's push operation (RunBroker._dispatch) is the enforcing entry point."""
        import json
        from diaktoros import ledger
        from diaktoros.run_supervisor import Supervisor
        db = self.root / "runs.sqlite"
        sup = Supervisor(db)
        sup.enqueue("fix", REPO, 7, HEAD, "fixer")
        with ledger.connect(db) as con:
            con.execute("UPDATE runs SET state='running',launch_intent=1,push_admitted=1")
        scope = broker_ipc.RunScope(REPO, 7, HEAD, "fixer", "fix-7", sup.get("fix")["id"], str(db))
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        payload = json.dumps({"operation": "push", "manifest": manifest("src/helper.py")}).encode()
        with mock.patch.object(config, "by_repo", return_value=self.loop):
            with self.assertRaises(scope_lock.ScopeHold):
                server._dispatch(payload)
            self.assertEqual(self.pushed, [])
            self.assertFalse(sup.post_write_hold(REPO, 7))   # a hold, not a possible write
            scope_lock.release(self.loop, 7)
            server._used = False                             # the retry's fresh broker
            self.assertEqual(server._dispatch(payload)["outcome"], "published")

    def test_setting_is_on_every_path(self):
        self.assertIn("fixer_scope_lock", settings_form.SETTINGS_SCHEMA)
        self.assertIn("  fixer_scope_lock:", (ROOT / "plugin.yaml").read_text())
        self.assertIs(settings_form.apply_settings(
            dict(config.DEFAULTS), {"fixer_scope_lock": True})["fixer_scope_lock"], True)


if __name__ == "__main__":
    unittest.main()
