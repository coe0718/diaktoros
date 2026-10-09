"""#474: doctor checks the GitHub-side contract — token expiry and scopes, labels, branch protection."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import config, doctor, gh  # noqa: E402

NOW = 1_790_000_000.0


def stamp(days: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(NOW + days * 86400))


class Environment(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root / "hermes"),
                                             "DIAKTOROS_CONFIG_DIR": str(self.root / "loops")})
        patch.start()
        self.addCleanup(patch.stop)
        tokens = {}
        for login in ("rev", "fix", "reader"):
            path = self.root / login
            path.write_text("pat\n")
            path.chmod(0o600)
            tokens[login] = str(path)
        self.loop = config.normalize({
            "id": "w", "repo": "acme/w", "fixers": ["fix"], "reviewers": ["rev"],
            "seats": {"reviewer": {"profile": "p1", "route": "r", "login": "rev"},
                      "fixer": {"profile": "p2", "route": "f", "login": "fix"}},
            "read_token": "reader", "tokens": tokens, "state_dir": str(self.root / "state"),
            "required_checks": ["ci"],
            "triage": {"route": "t", "profile": "p3", "authors": ["x"], "labels": ["bug", "docs"]}})
        self.headers = {gh.TOKEN_EXPIRY_HEADER: stamp(90), "x-oauth-scopes": "repo"}
        self.labels = [{"name": "bug"}, {"name": "Docs"}]
        self.protection = {"contexts": ["ci"]}

    def fake(self, loop, path, method="GET", body=None, login=None):
        if path == "/user":
            return gh.Response({"login": login}, "", 200, dict(self.headers))
        if "/labels" in path:
            return gh.Response(self.labels, "", 200, {})
        if "required_status_checks" in path:
            if self.protection is None:
                return gh.Response(None, "HTTP 404", 404, {})
            return gh.Response(self.protection, "", 200, {})
        return gh.Response(None, "HTTP 404", 404, {})

    def run_check(self, entry="loop", offline=False):
        with mock.patch.object(gh, "request", side_effect=self.fake), \
             mock.patch.object(time, "time", return_value=NOW):
            if entry == "loop":
                checks = doctor.check_loop(self.loop, offline)
            else:
                checks = doctor.check_environment(self.loop, offline)
        return {c.name: c for c in checks}

    def test_expiry_far_passes(self):
        c = self.run_check()["token-expiry:rev"]
        self.assertEqual(c.status, doctor.VERIFIED)

    def test_expiry_inside_14_days_warns(self):
        self.headers[gh.TOKEN_EXPIRY_HEADER] = stamp(13)
        c = self.run_check()["token-expiry:rev"]
        self.assertEqual(c.status, doctor.UNKNOWN)
        self.assertFalse(c.failed)
        self.assertIn("rotate", c.fix)

    def test_expiry_exactly_14_days_warns(self):
        self.headers[gh.TOKEN_EXPIRY_HEADER] = stamp(14)
        c = self.run_check()["token-expiry:rev"]
        self.assertEqual(c.status, doctor.UNKNOWN)
        self.headers[gh.TOKEN_EXPIRY_HEADER] = stamp(14.01)
        self.assertEqual(self.run_check()["token-expiry:rev"].status, doctor.VERIFIED)

    def test_expiry_past_fails(self):
        self.headers[gh.TOKEN_EXPIRY_HEADER] = stamp(-1)
        c = self.run_check()["token-expiry:rev"]
        self.assertTrue(c.failed)

    def test_scopes_missing_fails_and_enough_passes(self):
        self.headers["x-oauth-scopes"] = "gist"
        c = self.run_check()["token-scopes:rev"]
        self.assertTrue(c.failed)
        self.assertIn("repo", c.detail)
        self.headers["x-oauth-scopes"] = "repo, gist"
        self.assertEqual(self.run_check()["token-scopes:rev"].status, doctor.VERIFIED)

    def test_fine_grained_scopes_skipped(self):
        del self.headers["x-oauth-scopes"]
        self.assertEqual(self.run_check()["token-scopes:rev"].status, doctor.SKIPPED)

    def test_labels_missing_fails_present_passes_case_insensitive(self):
        self.assertEqual(self.run_check()["labels"].status, doctor.VERIFIED)
        self.labels = [{"name": "bug"}]
        c = self.run_check()["labels"]
        self.assertTrue(c.failed)
        self.assertIn("docs", c.detail)
        self.assertIn("gh label create", c.fix)

    def test_branch_protection_match_is_silent_mismatch_reported(self):
        self.assertNotIn("branch-protection", self.run_check())
        self.protection = {"contexts": ["lint"]}
        c = self.run_check()["branch-protection"]
        self.assertEqual(c.status, doctor.UNKNOWN)
        self.assertIn("ci", c.detail)
        self.assertIn("lint", c.detail)
        self.protection = None          # unprotected base: loop requires "ci" but GitHub does not
        self.assertIn("branch-protection", self.run_check())

    def test_offline_reads_nothing(self):
        with mock.patch.object(gh, "request", side_effect=AssertionError("network")):
            checks = doctor.check_environment(self.loop, True)
        self.assertTrue(checks)
        self.assertTrue(all(c.status == doctor.UNKNOWN for c in checks))

    def test_check_loop_runs_them(self):
        names = self.run_check("loop")
        for name in ("token-expiry:rev", "token-expiry:fix", "token-expiry:reader", "labels"):
            self.assertIn(name, names)


if __name__ == "__main__":
    unittest.main()
