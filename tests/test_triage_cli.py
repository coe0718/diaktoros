#!/usr/bin/env python3
"""Issue #213: ``hermes review-loop triage`` turns issue triage on and off for one loop.

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
from review_loop import cli, config, doctor, gate_shims, gh, routes  # noqa: E402

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
        return self.cli("triage", "--loop", LOOP_ID, "--enable", "--profile", "arbiter",
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
        for argv in (("--profile", "nobody"), ("--labels", "bug,{x}"), ("--login", t.READ_LOGIN)):
            with self.subTest(argv=argv):
                rc, out = self.cli("triage", "--loop", LOOP_ID, "--enable", "--profile", "arbiter",
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


if __name__ == "__main__":
    unittest.main()
