#!/usr/bin/env python3
"""Issue #213: ``hermes dk triage`` turns issue triage on and off for one loop.

In-process against the real parser on the harness fixture (stub GitHub, temp HOME): the route
it writes subscribes to ``issues`` and runs the triage gate, the shim lands in the triage
profile, refusals write nothing, and the repo hooks it plans include the issues hook.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from diaktoros import cli, config, doctor, gate_shims, gh, routes  # noqa: E402

LOOP_ID = "triaged"
LOOP_FILE = t.LOOPS_DIR / f"{LOOP_ID}.json"
ROUTE = f"{LOOP_ID}-triage"


class TriageVerb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={})
        # The harness's own loop configures the same repo, and a repo has one loop (#63).
        (t.LOOPS_DIR / "widgets.json").unlink(missing_ok=True)
        LOOP_FILE.unlink(missing_ok=True)
        self.addCleanup(LOOP_FILE.unlink, missing_ok=True)
        self.addCleanup(lambda: routes.restore_entries({ROUTE: None}))
        (config.profiles_root() / "arbiter").mkdir(parents=True, exist_ok=True)
        rc, out = self.cli("init", "--repo", t.REPO, "--id", LOOP_ID, "--host", t.HOST,
                           "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                           "--reviewer-profile", "reviewer-profile",
                           "--fixer-profile", "fixer-profile",
                           "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                           "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *t.READER_ARGS)
        self.assertEqual(rc, 0, out)

    def cli(self, *argv) -> tuple[int, str]:
        return t.run_cli(t.parser_for().parse_args(list(argv)))

    def enable(self, *extra) -> tuple[int, str]:
        return self.cli("triage", "--loop", LOOP_ID, "--enable", "--triage-profile", "arbiter",
                        "--author", "Owner", "--labels", "bug,docs,P1", *extra)

    def test_enable_writes_the_block_the_issues_route_and_the_shim(self):
        rc, out = self.enable()
        self.assertEqual(rc, 0, out)
        loop = config.load_id(LOOP_ID)
        self.assertEqual(loop["triage"]["route"], ROUTE)
        self.assertEqual(loop["triage"]["labels"], ["bug", "docs", "P1"])
        entry = routes.route(ROUTE)
        self.assertEqual((entry["events"], entry["script"]), (["issues"], "gate_triage.py"))
        self.assertEqual(routes.route_profile(entry), "arbiter")
        self.assertEqual(gate_shims.contract_drift(loop, "triage", entry), [])
        shim = gate_shims.home_for("arbiter") / "scripts" / "gate_triage.py"
        self.assertTrue(shim.is_file(), out)
        self.assertIn("apply --loop", out)          # no admin token: says how to add the hook
        rc, out = self.cli("triage", "--loop", LOOP_ID)
        self.assertIn("issue triage: on", out)
        self.assertIn("labels as rev-coach", out)

    def test_refusals_and_dry_run_write_nothing(self):
        before = LOOP_FILE.read_bytes()
        for argv in (("--triage-profile", "nobody"), ("--labels", "bug,{x}"), ("--login", t.READ_LOGIN)):
            with self.subTest(argv=argv):
                rc, out = self.cli("triage", "--loop", LOOP_ID, "--enable", "--triage-profile", "arbiter",
                                   "--author", "owner", "--labels", "bug", *argv)
                self.assertEqual(rc, 2, out)
                self.assertIn("refused", out)
        rc, out = self.enable("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual(LOOP_FILE.read_bytes(), before)
        self.assertIsNone(routes.route(ROUTE))

    def test_disable_removes_the_route_and_shim_and_keeps_the_seats(self):
        self.assertEqual(self.enable()[0], 0)
        rc, out = self.cli("triage", "--loop", LOOP_ID, "--disable")
        self.assertEqual(rc, 0, out)
        loop = config.load_id(LOOP_ID)
        self.assertEqual(loop["triage"], {})
        self.assertIsNone(routes.route(ROUTE))
        self.assertFalse((gate_shims.home_for("arbiter") / "scripts" / "gate_triage.py").exists())
        self.assertTrue(routes.route(f"{LOOP_ID}-review"))
        self.assertIn("left", out)                  # the hook is named, not silently orphaned

    def test_the_hooks_and_doctor_cover_the_issues_route(self):
        self.assertEqual(self.enable()[0], 0)
        loop = config.load_id(LOOP_ID)
        posted = []

        def api(loop_, path, method="GET", body=None, login=None):
            if method == "POST":
                posted.append(body["events"])
                return {"id": len(posted)}
            return []
        with mock.patch.object(gh, "api", side_effect=api):
            cli._install_hooks(loop, "admin")
        self.assertEqual(posted, [["pull_request"], ["pull_request_review"], ["issues"]])
        names = [check.name for check in doctor.check_routes(loop)]
        self.assertIn(f"route:{ROUTE}", names)
        triage = {check.name: check for check in doctor.check_triage(loop)}
        self.assertEqual(triage["profile:triage"].status, doctor.VERIFIED)

    def test_dry_run_names_the_hook_reconciliation_keeps(self):
        admin = ("--admin-token", t.REVIEWER)
        url = routes.url_for(ROUTE, t.HOST) if routes.route(ROUTE) else f"{t.HOST}/webhooks/{ROUTE}"
        old = {"id": 9, "active": True, "config": {"url": f"https://old.example/webhooks/{ROUTE}"}}
        current = {"id": 3, "active": True, "config": {"url": url}}
        with mock.patch.object(cli, "_hook_listing", return_value=[old, current]), \
                mock.patch.object(routes, "route_name_of", return_value=ROUTE):
            rc, out = self.enable("--dry-run", *admin)
        self.assertEqual(rc, 0, out)
        self.assertIn("keep hook 3 (active)", out)
        self.assertNotIn("keep hook 9", out)

    def test_hook_wording_follows_what_happened(self):
        admin = ("--admin-token", t.REVIEWER)
        url = routes.url_for(ROUTE, t.HOST) if routes.route(ROUTE) else f"{t.HOST}/webhooks/{ROUTE}"
        existing = {"id": 7, "active": True, "config": {"url": url}}
        with mock.patch.object(cli, "_hook_listing", return_value=[existing]), \
                mock.patch.object(routes, "route_name_of", return_value=ROUTE):
            rc, out = self.enable("--dry-run", *admin)
        self.assertEqual(rc, 0, out)
        self.assertIn("keep hook 7 (active)", out)
        self.assertNotIn("(paused)", out)
        with mock.patch.object(cli, "_hook_listing", return_value=[]):
            rc, out = self.enable("--dry-run", *admin)
        self.assertIn("create the repo hook (paused; next, arm)", out)

        def reuse(loop, login, dry_run, outcome=None):
            outcome.update(created=[], kept={"triage": existing})
            return 0
        with mock.patch.object(cli, "_ensure_hooks", side_effect=reuse):
            rc, out = self.enable(*admin)
        self.assertIn("hook 7 kept (active)", out)
        self.assertNotIn("created paused", out)
        self.assertNotIn("next: hermes dk arm", out)

        def create(loop, login, dry_run, outcome=None):
            outcome.update(created=["triage"], kept={})
            return 0
        with mock.patch.object(cli, "_ensure_hooks", side_effect=create):
            rc, out = self.enable(*admin)
        self.assertIn("hook created (paused)", out)
        self.assertIn("next: hermes dk arm", out)


if __name__ == "__main__":
    unittest.main()
