"""#1241: when the reader cannot see the repository's hooks, doctor and selftest name what an App
reader needs (a repository permission), not a PAT scope it has no way to hold (live App-mode
trial, dk-app-spike)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros import config, doctor, gh, route_intent, routes, selftest  # noqa: E402

PAT_READER = {"id": "w", "repo": "acme/w", "base": "main", "read_token": "reader",
              "tokens": {"reader": "/t"}, "apps": {}, "host": "https://gw.example"}
APP_READER = {**PAT_READER, "read_token": "rd[bot]", "tokens": {},
              "apps": {"rd[bot]": {"app_id": 9, "key_file": "/k"}}}
APP_FIX = "the repository permission Webhooks: Read"


class FixLines(unittest.TestCase):
    def test_doctor_hooks_listing(self):
        with mock.patch.object(config, "hook_roles", return_value=["reviewer"]), \
                mock.patch.object(route_intent, "routes_of", return_value={"reviewer": "w-review"}), \
                mock.patch.object(routes, "url_for", return_value="https://gw.example/p/x"), \
                mock.patch.object(gh, "api", return_value=None):
            [app] = doctor.check_hooks(APP_READER, False)
            [pat] = doctor.check_hooks(PAT_READER, False)
        self.assertIn(APP_FIX, app.fix)
        self.assertNotIn("read:repo_hook", app.fix)
        self.assertIn("read:repo_hook", pat.fix)

    def test_doctor_hooks_detail_and_branch_protection_fix(self):
        # #1255: the hooks detail named a PAT scope beside an App fix; branch-protection's unread
        # fix named no permission at all.
        with mock.patch.object(config, "hook_roles", return_value=["reviewer"]), \
                mock.patch.object(route_intent, "routes_of", return_value={"reviewer": "w-review"}), \
                mock.patch.object(routes, "url_for", return_value="https://gw.example/p/x"), \
                mock.patch.object(gh, "api", return_value=None):
            [app] = doctor.check_hooks(APP_READER, False)
        self.assertNotIn("read:repo_hook", app.detail)
        self.assertIn("Webhooks: Read", app.detail)              # the App's own need, named
        unread = gh.Response(None, "HTTP 403", 403, {})
        with mock.patch.object(gh, "request", return_value=unread):
            check = doctor.check_branch_protection({**APP_READER, "required_checks": ["tests"]},
                                                   False)
        self.assertIn("Administration: Read", check.fix)

    def test_doctor_hook_deliveries(self):
        with mock.patch.object(gh, "fetch", return_value=(None, "HTTP 403")):
            app = doctor.check_deliveries(APP_READER, 5, "w-review")
            pat = doctor.check_deliveries(PAT_READER, 5, "w-review")
        self.assertIn(APP_FIX, app.fix)
        self.assertIn("read:repo_hook", pat.fix)

    def test_selftest_hook_signatures(self):
        fixes = {}
        for name, loop in (("app", APP_READER), ("pat", PAT_READER)):
            report = selftest.Report(out=io.StringIO())
            with mock.patch.object(gh, "hooks_read", return_value=(None, "HTTP 403")):
                selftest.check_hook_signatures(report, loop)
            fixes[name] = report.results[-1][4]
        self.assertIn(APP_FIX, fixes["app"])
        self.assertIn("read:repo_hook", fixes["pat"])


if __name__ == "__main__":
    unittest.main()
