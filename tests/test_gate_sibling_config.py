#!/usr/bin/env python3
"""A sibling loop file the loader refuses must not stop a healthy loop's gate (#56 review).

Runs the real gate scripts through the harness fixture (stub GitHub, disposable HOME). The
refused sibling is exactly the shape #56 made refusable: a loop file whose read_token was dropped.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402


class SiblingConfigTest(unittest.TestCase):
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
        t.reset(prs={"7": t.pr(7)})

    def sibling(self, repo: str, **changes) -> pathlib.Path:
        cfg = json.loads((t.LOOPS_DIR / "widgets.json").read_text())
        cfg.update({"id": "legacy", "repo": repo, "state_dir": str(t.TMP / "legacy-state"),
                    "seats": {"reviewer": {"profile": "reviewer-profile", "route": "legacy-review"},
                              "fixer": {"profile": "fixer-profile", "route": "legacy-fix"}}})
        cfg.pop("read_token")
        cfg.update(changes)
        path = t.LOOPS_DIR / "legacy.json"
        path.write_text(json.dumps(cfg))
        return path

    def test_a_refused_sibling_does_not_stop_a_healthy_loops_gate(self):
        payload = t.pr_payload(7)
        baseline = t.run("gate_reviewer.py", payload)
        self.assertNotIn("Traceback", baseline[2])
        t.reset(prs={"7": t.pr(7)})
        self.sibling("acme/legacy")
        kind, out, err = t.run("gate_reviewer.py", payload)
        self.assertNotIn("Traceback", err)
        self.assertEqual(kind, baseline[0], (out, err))
        self.assertIn("skipping legacy.json (loop for acme/legacy): ", err)
        self.assertIn("'read_token' is not set", err)
        self.assertEqual(err.count("skipping legacy.json"), 1)

    def test_the_cron_watchdog_sweeps_every_loop_past_a_refused_file(self):
        # Cron runs the watchdog with no --loop: the refused sibling is one named warning, and
        # the healthy loop's sweep still runs (it stamps its last run).
        self.sibling("acme/legacy")
        swept = t.state_file("watchdog.log")        # the healthy loop's sweep writes here
        swept.unlink(missing_ok=True)
        out, _, err = t.run("watchdog.py", None)
        self.assertNotIn("Traceback", err)
        self.assertIn("⚠️ Review loop [legacy] watchdog failed: ConfigError: ", out)
        self.assertIn("'read_token' is not set", out)
        self.assertNotIn("⚠️ Review loop watchdog failed", out)   # not the whole sweep
        self.assertTrue(swept.exists() and swept.read_text().strip(), out)

    def test_drain_without_loop_names_a_refused_file_on_stderr(self):
        self.sibling("acme/legacy")
        out, _, err = t.run("watchdog.py", None, "--drain", "--seat", "reviewer")
        self.assertNotIn("Traceback", err)
        self.assertIn("⚠️ Review loop [legacy] not drained: ConfigError: ", err)

    def test_a_refused_file_that_may_own_this_repo_fails_closed_without_a_traceback(self):
        self.sibling(t.REPO)                       # same repo: ownership cannot be settled
        kind, out, err = t.run("gate_reviewer.py", t.pr_payload(7))
        self.assertEqual(kind, "SILENT", (out, err))
        self.assertNotIn("Traceback", err)
        self.assertIn(f"loop config refused for {t.REPO}", err)
        self.assertIn("unattended writes denied until it loads", err)


if __name__ == "__main__":
    unittest.main()
