#!/usr/bin/env python3
"""Issue #215: ``hermes review-loop setup`` — a first install in one command, safe to re-run.

The runtime paths are detected on a fabricated host layout (a uv-style Python alias, a venv, a
Hermes checkout, a rustup toolchain); the loop is installed through the real ``init`` on the
harness fixture (stub GitHub, temp HOME); the scheduler is a fake ``hermes``. ``doctor``,
``selftest`` and ``arm`` are replaced by recorders here: each has its own suite, and what this
one proves is the order, the stop on a failed check, and that a re-run changes nothing.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from test_shared_watchdog_job import FakeHermes  # noqa: E402
from review_loop import cli, config, runtime_detect  # noqa: E402

LOOP_ID = "setupwidgets"
LOOP_FILE = t.LOOPS_DIR / f"{LOOP_ID}.json"


def host_layout(root: pathlib.Path) -> dict[str, pathlib.Path]:
    """A Hermes checkout, its venv, a uv-style Python install and a rust toolchain."""
    python = root / "uv" / "python"
    real = python / "cpython-3.11.13-linux-x86_64-gnu"
    (real / "bin").mkdir(parents=True)
    (real / "bin" / "python3.11").write_text("")
    (python / "cpython-3.11-linux-x86_64-gnu").symlink_to(real.name)   # uv's minor-version alias
    source = root / "hermes-agent"
    (source / ".git").mkdir(parents=True)
    (source / "run_agent.py").write_text("")
    venv = source / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(python / "cpython-3.11-linux-x86_64-gnu" / "bin"
                                         / "python3.11")
    (venv / "bin" / "hermes").write_text("")
    rust = root / "rustup" / "toolchains" / "stable-x86_64-unknown-linux-gnu"
    (rust / "bin").mkdir(parents=True)
    (rust / "bin" / "cargo").write_text("")
    return {"source": source, "venv": venv, "runtime": python, "rust": rust}


class Detection(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.paths = host_layout(self.root)

    def test_runtime_is_the_directory_holding_both_the_alias_and_the_install(self):
        self.assertEqual(runtime_detect.runtime_for(self.paths["venv"]), self.paths["runtime"])
        settings = {key: str(value) for key, value in self.paths.items()}
        self.assertEqual(runtime_detect.problems(settings), {})
        # The install the alias resolves to is not enough: the venv's link names the alias.
        settings["runtime"] = str(self.paths["runtime"] / "cpython-3.11.13-linux-x86_64-gnu")
        self.assertIn("outside", runtime_detect.problems(settings)["runtime"])

    def test_a_plain_interpreter_gives_its_install_root(self):
        venv = self.root / "plain-venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        self.assertEqual(runtime_detect.runtime_for(venv), venv)

    def test_rustups_default_toolchain_wins_and_the_proxy_never_counts(self):
        rustup = self.root / "rustup"
        nightly = rustup / "toolchains" / "nightly-x86_64-unknown-linux-gnu"
        (nightly / "bin").mkdir(parents=True)
        (nightly / "bin" / "cargo").write_text("")
        with patch.dict(os.environ, {"RUSTUP_HOME": str(rustup), "PATH": "/nonexistent"}):
            self.assertEqual(runtime_detect._rust(), self.paths["rust"])   # stable-* before others
            (rustup / "settings.toml").write_text('default_toolchain = "nightly"\n')
            self.assertEqual(runtime_detect._rust(), nightly)
        with patch.dict(os.environ, {"RUSTUP_HOME": str(self.root / "none"),
                                     "PATH": str(self.root / ".cargo" / "bin")}):
            proxy = self.root / ".cargo" / "bin" / "cargo"
            proxy.parent.mkdir(parents=True)
            proxy.write_text("#!/bin/sh\n")
            proxy.chmod(0o755)
            self.assertIsNone(runtime_detect._rust())

    def test_a_packaged_install_finds_the_checkouts_own_venv(self):
        """The `hermes` command runs on a bundled Python outside any venv (a packaged install):
        the venv is the checkout's `venv/` (preferred over a developer's `.venv/`), and the
        runtime is computed from it (first live setup, 2026-10-03)."""
        venv = self.paths["venv"]
        (self.paths["source"] / ".venv" / "bin").mkdir(parents=True)
        (self.paths["source"] / ".venv" / "bin" / "python").write_text("")
        (self.paths["source"] / ".venv" / "bin" / "hermes").write_text("")
        bundled = self.root / "tools" / "python"
        (bundled / "bin").mkdir(parents=True)
        with patch.object(runtime_detect.sys, "prefix", str(bundled)), \
                patch.object(runtime_detect.shutil, "which", return_value=None), \
                patch.object(runtime_detect, "_source", return_value=self.paths["source"]), \
                patch.object(runtime_detect, "_rust", return_value=self.paths["rust"]):
            found = runtime_detect.detect()
        self.assertEqual(found["venv"], str(venv))
        self.assertEqual(found["runtime"], str(self.paths["runtime"]))
        self.assertEqual(runtime_detect.problems(found), {})

    def test_each_wrong_path_is_named(self):
        bad = {"source": str(self.root), "venv": str(self.root), "runtime": str(self.root / "x"),
               "rust": str(self.root)}
        self.assertEqual(set(runtime_detect.problems(bad)), {"source", "venv", "runtime", "rust"})
        self.assertEqual(set(runtime_detect.problems({})), set(runtime_detect.HOST_KEYS))


class Setup(unittest.TestCase):
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
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.paths = host_layout(self.root)
        self.runtime_file = runtime_detect.path()
        self.runtime_file.unlink(missing_ok=True)
        self.addCleanup(self.runtime_file.unlink, missing_ok=True)
        self.hermes = FakeHermes(config.home())
        for stale in (self.hermes.log, config.home() / "cron" / "jobs.json"):
            stale.unlink(missing_ok=True)
            self.addCleanup(stale.unlink, missing_ok=True)
        env = patch.dict(os.environ, {"REVIEW_LOOP_HERMES": str(self.hermes.bin)})
        env.start()
        self.addCleanup(env.stop)
        # Nothing on this machine is detected: every host path comes from the flags.
        detect = patch.object(runtime_detect, "detect", return_value={})
        detect.start()
        self.addCleanup(detect.stop)
        self.ran: list[str] = []
        self.fail_check = ""

    def recorder(self, verb):
        def run(args):
            self.ran.append(verb)
            return 1 if verb == self.fail_check else 0
        return run

    def setup_cli(self, *extra, settings=None) -> tuple[int, str]:
        with patch.object(cli, "cmd_doctor", self.recorder("doctor")), \
                patch.object(cli, "cmd_selftest", self.recorder("selftest")), \
                patch.object(cli, "cmd_arm", self.recorder("arm")):
            parser = t.parser_for(settings)
            return t.run_cli(parser.parse_args(["setup", *extra]))

    def flags(self, *extra) -> list[str]:
        return ["--yes", "--repo", t.REPO, "--id", LOOP_ID, "--host", t.HOST,
                "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                "--reviewer-profile", "reviewer-profile", "--fixer-profile", "fixer-profile",
                "--reviewer-token", str(t.SEAT_PATS[0]), "--fixer-token", str(t.SEAT_PATS[1]),
                "--read-token", t.READ_LOGIN, "--read-token-file", str(t.READ_PAT),
                *[f"--{key}={value}" for key, value in self.paths.items()], *extra]

    def shim_jobs(self):
        return [job for job in self.hermes.jobs if job.get("name") == cli.SHARED_JOB_NAME]

    def test_a_first_install_does_every_step_in_order_and_does_not_arm_unasked(self):
        rc, out = self.setup_cli(*self.flags())
        self.assertEqual(rc, 0, out)
        written = json.loads(self.runtime_file.read_text())
        self.assertEqual(written, {key: str(value) for key, value in self.paths.items()})
        self.assertEqual(self.runtime_file.stat().st_mode & 0o777, 0o600)
        loop = config.load_id(LOOP_ID)
        self.assertEqual((loop["repo"], loop["read_token"]), (t.REPO, t.READ_LOGIN))
        self.assertEqual(len(self.shim_jobs()), 1, self.hermes.jobs)
        self.assertEqual(self.ran, ["doctor", "selftest"])
        self.assertIn("not armed", out)
        # init's own next steps no longer tell the operator to hand-write the runtime file.
        self.assertNotIn("create the runtime file", out)

    def test_a_re_run_reports_and_changes_nothing(self):
        self.assertEqual(self.setup_cli(*self.flags())[0], 0)
        before = (LOOP_FILE.read_bytes(), self.runtime_file.read_bytes(), self.hermes.jobs)
        rc, out = self.setup_cli(*self.flags())
        self.assertEqual(rc, 0, out)
        self.assertEqual((LOOP_FILE.read_bytes(), self.runtime_file.read_bytes(),
                          self.hermes.jobs), before)
        self.assertIn("in place — nothing to change", out)
        self.assertIn("is configured — kept", out)
        self.assertIn("already scheduled", out)
        creates = [c for c in self.hermes.calls if c[:2] == ["cron", "create"]]
        self.assertEqual(len(creates), 1, creates)

    def test_a_failed_check_stops_before_arming_even_with_arm(self):
        for failing in ("doctor", "selftest"):
            with self.subTest(failing=failing):
                self.ran, self.fail_check = [], failing
                rc, out = self.setup_cli(*self.flags("--arm"))
                self.assertEqual(rc, 1, out)
                self.assertNotIn("arm", self.ran)
                self.assertIn("setup stopped before arming", out)

    def test_arm_runs_only_after_a_clean_pass_when_asked(self):
        rc, out = self.setup_cli(*self.flags("--arm"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ran, ["doctor", "selftest", "arm"])

    def test_a_broken_path_is_repaired_and_the_model_overrides_kept(self):
        override = {"model": "m", "upstream": "https://api.example/v1/chat/completions",
                    "key_file": "/k"}
        stale = {**{key: str(value) for key, value in self.paths.items()},
                 "rust": str(self.root / "moved"), "seats": {"reviewer": override}}
        self.runtime_file.write_text(json.dumps(stale))
        self.runtime_file.chmod(0o600)
        argv = [a for a in self.flags() if not a.startswith(
            tuple(f"--{key}=" for key in runtime_detect.HOST_KEYS))]
        with patch.object(runtime_detect, "detect", return_value={"rust": str(self.paths["rust"])}):
            rc, out = self.setup_cli(*argv)
        self.assertEqual(rc, 0, out)
        written = json.loads(self.runtime_file.read_text())
        self.assertEqual(written["rust"], str(self.paths["rust"]))
        self.assertEqual(written["source"], str(self.paths["source"]))
        self.assertEqual(written["seats"], {"reviewer": override})
        self.assertIn("(detected)", out)
        self.assertIn("(kept)", out)

    def test_a_given_venv_brings_its_own_runtime(self):
        """Only --venv given: the runtime is derived from that venv, not left for --runtime."""
        argv = [a for a in self.flags() if not a.startswith("--runtime=")]
        rc, out = self.setup_cli(*argv)
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.runtime_file.read_text())["runtime"],
                         str(self.paths["runtime"]))
        self.assertIn("(detected)", out)

    def test_a_path_that_cannot_be_found_is_named_and_nothing_is_armed(self):
        argv = [a for a in self.flags("--arm") if not a.startswith("--rust=")]
        rc, out = self.setup_cli(*argv)
        self.assertEqual(rc, 1, out)
        self.assertIn("pass --rust PATH", out)
        self.assertFalse(self.runtime_file.exists(), "a partial runtime file is never written")
        self.assertNotIn("arm", self.ran)

    def test_dry_run_writes_nothing(self):
        rc, out = self.setup_cli(*self.flags("--dry-run"))
        self.assertEqual(rc, 0, out)
        self.assertIn("would write", out)
        self.assertFalse(self.runtime_file.exists())
        self.assertFalse(LOOP_FILE.exists())
        self.assertEqual(self.hermes.jobs, [])
        self.assertEqual(self.ran, [])

    def test_init_refusing_the_answers_stops_setup(self):
        argv = [a for a in self.flags() if a not in ("--read-token", t.READ_LOGIN)]
        rc, out = self.setup_cli(*argv)
        self.assertEqual(rc, 2, out)
        self.assertIn("init refused the answers", out)
        self.assertFalse(LOOP_FILE.exists())
        self.assertEqual(self.ran, [])

    def test_a_loop_id_serving_another_repository_is_refused(self):
        self.assertEqual(self.setup_cli(*self.flags())[0], 0)
        argv = self.flags()
        argv[argv.index(t.REPO)] = "acme/other"
        rc, out = self.setup_cli(*argv)
        self.assertEqual(rc, 2, out)
        self.assertIn(f"serves {t.REPO}", out)

    def test_questions_default_to_the_settings_form(self):
        settings = {"reviewer_login": t.REVIEWER, "fixer_login": t.FIXER,
                    "reviewer_profile": "reviewer-profile", "fixer_profile": "fixer-profile",
                    "reviewer_token_file": str(t.SEAT_PATS[0]),
                    "fixer_token_file": str(t.SEAT_PATS[1]), "host": t.HOST}
        for pat in t.SEAT_PATS:
            pat.chmod(0o600)          # the settings form's token files are checked private
        typed = {"repository": t.REPO, "reader login": t.READ_LOGIN,
                 f"token file for {t.READ_LOGIN}": str(t.READ_PAT)}
        asked: list[str] = []

        def answer(prompt=""):           # Enter (the default) for everything else
            asked.append(prompt)
            return next((value for key, value in typed.items() if key in prompt), "")
        tty = io.StringIO()
        tty.isatty = lambda: True
        with patch("builtins.input", answer), \
                patch.object(sys, "stdin", tty), \
                patch.object(runtime_detect, "detect",
                             return_value={k: str(v) for k, v in self.paths.items()}):
            rc, out = self.setup_cli("--id", LOOP_ID, settings=settings)
        self.assertEqual(rc, 0, out)
        loop = config.load_id(LOOP_ID)
        self.assertEqual(loop["seats"]["reviewer"]["profile"], "reviewer-profile")
        self.assertEqual(loop["host"], t.HOST)
        self.assertIn("not armed", out)          # the arm question defaults to no
        self.assertTrue(any("Arm the repo hooks" in q for q in asked), asked)
        self.assertTrue(any(f"[{t.REVIEWER}]" in q for q in asked), asked)   # the form's default

    def test_without_a_terminal_it_asks_for_yes(self):
        tty = io.StringIO()
        tty.isatty = lambda: False
        with patch.object(sys, "stdin", tty):
            rc, out = self.setup_cli("--repo", t.REPO)
        self.assertEqual(rc, 2, out)
        self.assertIn("pass --yes", out)
        self.assertFalse(self.runtime_file.exists())


if __name__ == "__main__":
    unittest.main()
