"""#1187: an App token mint is charged to a gate's time budget like every gh call. The App's own
HTTP call took a fixed 30 s timeout and the JWT signer up to two 20 s subprocesses, inside a gate
whose whole budget may be 30 s, and the signer re-probed the runtime Python on every mint."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros import gh, github_app  # noqa: E402

LOOP = {"repo": "acme/w", "apps": {"r[bot]": {"app_id": 7, "key_file": "/k.pem"}}}


class Budget(unittest.TestCase):
    def setUp(self):
        github_app.forget()
        self.addCleanup(github_app.forget)
        self.addCleanup(gh.end_gate)

    def test_signing_gets_what_is_left_of_the_budget_and_none_once_spent(self):
        seen = []

        def run(argv, **kw):
            seen.append(kw["timeout"])
            return subprocess.CompletedProcess(argv, 0, b"sig", b"")
        with mock.patch.object(github_app, "signer", return_value=("openssl", pathlib.Path("/x"))), \
                mock.patch.object(github_app.subprocess, "run", side_effect=run):
            gh.begin_gate(time.monotonic() + 0.5)
            github_app.sign(pathlib.Path("/k.pem"), b"data")
            self.assertLessEqual(seen[-1], 0.5)
            gh.begin_gate(time.monotonic() - 1)
            with self.assertRaises(gh.GateBudgetExceeded):
                github_app.sign(pathlib.Path("/k.pem"), b"data")
        self.assertEqual(len(seen), 1)                     # never started once spent

    def test_a_signing_timeout_past_the_deadline_is_an_overrun(self):
        def run(argv, **kw):
            gh.begin_gate(time.monotonic() - 1)            # the budget runs out meanwhile
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        with mock.patch.object(github_app, "signer", return_value=("openssl", pathlib.Path("/x"))), \
                mock.patch.object(github_app.subprocess, "run", side_effect=run):
            gh.begin_gate(time.monotonic() + 0.5)
            with self.assertRaises(gh.GateBudgetExceeded):
                github_app.sign(pathlib.Path("/k.pem"), b"data")

    def test_the_apps_own_call_is_budgeted(self):
        seen = []

        def urlopen(req, timeout):
            seen.append(timeout)
            raise OSError("down")
        with mock.patch.object(github_app, "jwt", return_value="j"), \
                mock.patch.object(github_app.gh.envnames, "get", return_value=None), \
                mock.patch("diaktoros.config.host_guards.guard_network"), \
                mock.patch("urllib.request.urlopen", side_effect=urlopen):
            gh.begin_gate(time.monotonic() + 0.5)
            response = github_app._as_app(LOOP, "r[bot]", "/app")
            self.assertLessEqual(seen[-1], 0.5)
            self.assertTrue(response.error)
            gh.begin_gate(time.monotonic() - 1)
            with self.assertRaises(gh.GateBudgetExceeded):
                github_app._as_app(LOOP, "r[bot]", "/app")

    def test_a_probe_cut_short_by_the_budget_is_not_cached(self):
        # #1278: one tight-budget timeout cached "no signer" for the process's life.
        answers = [subprocess.TimeoutExpired("probe", 0.1), subprocess.CompletedProcess([], 0, b"", b"")]

        def run(argv, **kw):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch.object(github_app, "_runtime_python", return_value=pathlib.Path("/py")), \
                mock.patch.object(github_app.shutil, "which", return_value=None), \
                mock.patch.object(github_app.subprocess, "run", side_effect=run):
            self.assertEqual(github_app.signer(), ("", None))          # cut short: no answer
            self.assertEqual(github_app.signer()[0], "cryptography")   # asked again, and cached
            self.assertEqual(github_app.signer()[0], "cryptography")

    def test_the_signer_is_probed_once_per_process(self):
        probes = []
        with mock.patch.object(github_app, "_runtime_python", return_value=pathlib.Path("/py")), \
                mock.patch.object(github_app.subprocess, "run",
                                  side_effect=lambda argv, **kw: probes.append(argv)
                                  or subprocess.CompletedProcess(argv, 0, b"", b"")):
            self.assertEqual(github_app.signer()[0], "cryptography")
            github_app.signer()
            self.assertEqual(len(probes), 1)
            github_app.forget()                            # a rotated key or a test re-probes
            github_app.signer()
            self.assertEqual(len(probes), 2)


if __name__ == "__main__":
    unittest.main()
