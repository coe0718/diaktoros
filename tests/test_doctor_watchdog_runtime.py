"""#67: doctor must notice a stopped watchdog and a runtime file whose paths no longer exist.

Two new checks close gaps where a syntactically-perfect install silently fails in real use:
``check_watchdog_last_run`` fails when the watchdog has not swept within 2× its schedule
(a broken shim, a paused job, a dead scheduler), and ``check_runtime_paths`` reports each
runtime-file path that a Hermes upgrade left behind. Both follow the existing ``check_*``
pattern: a ``Check`` per finding, with a detail and a fix.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
from diaktoros import config, doctor  # noqa: E402
from diaktoros.doctor import cron_checks  # noqa: E402


def _loop(**extra) -> dict:
    raw = {"id": "widgets", "repo": "acme/widgets", "fixers": ["fixer"],
           "reviewers": ["reviewer"],
           "seats": {"reviewer": {"profile": "rev", "route": "r"},
                     "fixer": {"profile": "fix", "route": "f"}},
           "read_token": "reader"}
    raw.update(extra)
    return config.normalize(raw)


def _git(path: pathlib.Path, *args: str) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": "/dev/null", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["/usr/bin/git", "-C", str(path), *args], check=True, env=env,
                   capture_output=True)


def _git_repo(path: pathlib.Path, commit=(), untracked=()) -> None:
    """A real Git repository holding ``commit`` in HEAD and ``untracked`` only on disk."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "README.md").write_text("x")
    for name in commit:
        (path / name).write_text("")
    _git(path, "add", "README.md", *commit)
    _git(path, "commit", "-q", "-m", "init")
    for name in untracked:
        (path / name).write_text("")


class WatchdogLastRun(unittest.TestCase):
    """``check_watchdog_last_run``: stale last_run fails; fresh or unknown does not."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        patch = mock.patch.dict(os.environ, {
            "HERMES_HOME": str(self.root / "hermes"),
            "DIAKTOROS_CONFIG_DIR": str(self.root / "loops")})
        patch.start()
        self.addCleanup(patch.stop)
        (self.root / "loops").mkdir()
        self.loop = _loop(state_dir=str(self.root / "state"))

    def cron_store(self, minutes: int = 15):
        """Write a minimal cron store with the watchdog job at ``minutes`` interval."""
        cron = self.root / "hermes" / "cron"
        cron.mkdir(parents=True, exist_ok=True)
        (cron / "jobs.json").write_text(json.dumps({"jobs": [{
            "id": "job1", "name": doctor.watchdog_job_name(self.loop),
            "script": doctor.SHIM_NAME, "no_agent": True, "enabled": True,
            "state": "scheduled",
            "schedule": {"kind": "interval", "minutes": minutes},
            "schedule_display": f"every {minutes}m",
            "next_run_at": "2030-01-01T00:00:00Z"}]}))

    def watch_file(self, last_run: str | None):
        """Write watchdog.json with the given last_run (or none)."""
        state = self.root / "state"
        state.mkdir(parents=True, exist_ok=True)
        data = {"last_run": last_run} if last_run else {}
        (state / "watchdog.json").write_text(json.dumps(data))

    def test_stale_last_run_fails(self):
        self.cron_store(minutes=15)
        stale = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(time.time() - 40 * 60))   # 40m > 2× 15m
        self.watch_file(stale)
        check = cron_checks.check_watchdog_last_run(self.loop)
        self.assertEqual(check.status, doctor.MISMATCH, check.detail)
        self.assertIn("older than 2×", check.detail)
        self.assertIn("watchdog", check.name)

    def test_fresh_last_run_passes(self):
        self.cron_store(minutes=15)
        fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5 * 60))
        self.watch_file(fresh)
        check = cron_checks.check_watchdog_last_run(self.loop)
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)

    def test_missing_last_run_is_unknown(self):
        self.cron_store(minutes=15)
        self.watch_file(None)
        check = cron_checks.check_watchdog_last_run(self.loop)
        self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
        self.assertIn("no last_run", check.detail)

    def test_unparseable_last_run_is_unknown(self):
        self.cron_store(minutes=15)
        self.watch_file("not-a-timestamp")
        check = cron_checks.check_watchdog_last_run(self.loop)
        self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
        self.assertIn("not a parseable timestamp", check.detail)

    def test_no_schedule_in_cron_store_is_unknown(self):
        self.watch_file(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        check = cron_checks.check_watchdog_last_run(self.loop)
        self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
        self.assertIn("schedule interval", check.detail)

    def test_boundary_2x_schedule_passes(self):
        self.cron_store(minutes=15)
        # Exactly 2× the 15m schedule = 30m: passes (not older than 2×).
        boundary = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                 time.gmtime(time.time() - 30 * 60 + 5))
        self.watch_file(boundary)
        check = cron_checks.check_watchdog_last_run(self.loop)
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)


class RuntimePaths(unittest.TestCase):
    """``check_runtime_paths``: every runtime-file path is validated; missing file is reported."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        patch = mock.patch.dict(os.environ, {
            "HERMES_HOME": str(self.root / "hermes"),
            "DIAKTOROS_CONFIG_DIR": str(self.root / "loops")})
        patch.start()
        self.addCleanup(patch.stop)
        (self.root / "loops").mkdir()
        self.loop = _loop(state_dir=str(self.root / "state"))

    def write_runtime(self, **overrides):
        """Create a valid runtime file with real directories; override any path key."""
        source = self.root / "hermes-agent"
        if not (source / ".git").exists():
            _git_repo(source, commit=("run_agent.py",))
        venv = self.root / "venv"
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin" / "python").write_text("")
        (venv / "bin" / "hermes").write_text("")
        runtime = self.root / "python"
        (runtime / "bin").mkdir(parents=True, exist_ok=True)
        rust = self.root / "rust"
        (rust / "bin").mkdir(parents=True, exist_ok=True)
        (rust / "bin" / "cargo").write_text("")
        settings = {"source": str(source), "venv": str(venv),
                    "runtime": str(runtime), "rust": str(rust)}
        settings.update(overrides)
        path = self.root / "hermes" / "diaktoros-runtime.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings))
        path.chmod(0o600)
        return settings

    def checks_by_name(self):
        return {c.name: c for c in doctor.check_runtime_paths(self.loop)}

    def test_all_valid_paths_pass(self):
        self.write_runtime()
        checks = self.checks_by_name()
        for key in ("source", "venv", "runtime", "rust"):
            self.assertEqual(checks[f"runtime:{key}"].status, doctor.VERIFIED,
                             checks[f"runtime:{key}"].detail)

    def test_missing_source_is_reported(self):
        self.write_runtime(source=str(self.root / "gone"))
        checks = self.checks_by_name()
        self.assertEqual(checks["runtime:source"].status, doctor.ABSENT)
        self.assertIn("does not exist", checks["runtime:source"].detail)
        self.assertIn("source", checks["runtime:source"].detail)
        # Other paths still verify independently.
        self.assertEqual(checks["runtime:venv"].status, doctor.VERIFIED)

    def test_missing_venv_is_reported(self):
        self.write_runtime(venv=str(self.root / "gone"))
        checks = self.checks_by_name()
        self.assertEqual(checks["runtime:venv"].status, doctor.ABSENT)
        self.assertIn("does not exist", checks["runtime:venv"].detail)

    def test_missing_runtime_is_reported(self):
        self.write_runtime(runtime=str(self.root / "gone"))
        checks = self.checks_by_name()
        self.assertEqual(checks["runtime:runtime"].status, doctor.ABSENT)
        self.assertIn("does not exist", checks["runtime:runtime"].detail)

    def test_missing_rust_is_reported(self):
        self.write_runtime(rust=str(self.root / "gone"))
        checks = self.checks_by_name()
        self.assertEqual(checks["runtime:rust"].status, doctor.ABSENT)
        self.assertIn("does not exist", checks["runtime:rust"].detail)

    def test_source_without_run_agent_is_mismatch(self):
        empty = self.root / "empty-source"
        empty.mkdir()
        self.write_runtime(source=str(empty))
        checks = self.checks_by_name()
        self.assertEqual(checks["runtime:source"].status, doctor.MISMATCH)
        self.assertIn("run_agent.py", checks["runtime:source"].detail)

    def test_source_without_git_is_mismatch(self):
        # run_agent.py alone is not enough: turns snapshot the source with git (selftest agrees).
        plain = self.root / "plain-source"
        plain.mkdir()
        (plain / "run_agent.py").write_text("")
        self.write_runtime(source=str(plain))
        checks = self.checks_by_name()
        self.assertEqual(checks["runtime:source"].status, doctor.MISMATCH)
        self.assertIn(".git", checks["runtime:source"].detail)

    def test_source_with_untracked_run_agent_is_mismatch(self):
        # #924: `.git` exists but run_agent.py is not in HEAD, so trusted_turn's HEAD-only
        # snapshot lacks Hermes and denies every turn.
        repo = self.root / "untracked-source"
        _git_repo(repo, untracked=("run_agent.py",))
        self.write_runtime(source=str(repo))
        check = self.checks_by_name()["runtime:source"]
        self.assertEqual(check.status, doctor.MISMATCH, check.detail)
        self.assertIn("not committed", check.detail)
        # The turn's own snapshot agrees: it is refused.
        from diaktoros import trusted_turn
        fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with self.assertRaises(trusted_turn.TurnDenied):
            trusted_turn._export_committed_source(fd, self.root / "snap")

    def test_source_with_stale_gitdir_pointer_is_mismatch(self):
        stale = self.root / "stale-source"
        stale.mkdir()
        (stale / "run_agent.py").write_text("")
        (stale / ".git").write_text(f"gitdir: {self.root / 'removed'}\n")
        self.write_runtime(source=str(stale))
        check = self.checks_by_name()["runtime:source"]
        self.assertEqual(check.status, doctor.MISMATCH, check.detail)
        self.assertIn("no readable committed HEAD", check.detail)

    def test_source_with_committed_run_agent_is_snapshotted(self):
        # The positive twin: committed in HEAD, doctor verifies and the snapshot succeeds.
        self.write_runtime()
        self.assertEqual(self.checks_by_name()["runtime:source"].status, doctor.VERIFIED)
        from diaktoros import trusted_turn
        fd = os.open(self.root / "hermes-agent", os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        trusted_turn._export_committed_source(fd, self.root / "snap")
        self.assertTrue((self.root / "snap" / "run_agent.py").is_file())

    def test_venv_without_python_is_mismatch(self):
        bare = self.root / "bare-venv"
        bare.mkdir()
        self.write_runtime(venv=str(bare))
        checks = self.checks_by_name()
        self.assertEqual(checks["runtime:venv"].status, doctor.MISMATCH)
        self.assertIn("bin/python", checks["runtime:venv"].detail)

    def test_venv_without_hermes_is_mismatch(self):
        # The seat turn runs <venv>/bin/hermes; bin/python alone must not read verified (#787).
        self.write_runtime()
        (self.root / "venv" / "bin" / "hermes").unlink()
        checks = self.checks_by_name()
        self.assertEqual(checks["runtime:venv"].status, doctor.MISMATCH)
        self.assertIn("bin/hermes", checks["runtime:venv"].detail)

    def test_rust_without_cargo_is_mismatch(self):
        bare = self.root / "bare-rust"
        bare.mkdir()
        self.write_runtime(rust=str(bare))
        checks = self.checks_by_name()
        self.assertEqual(checks["runtime:rust"].status, doctor.MISMATCH)
        self.assertIn("bin/cargo", checks["runtime:rust"].detail)

    def test_missing_runtime_file_is_reported(self):
        # No runtime file at all: the file itself is reported as absent.
        checks = doctor.check_runtime_paths(self.loop)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].name, "runtime:file")
        self.assertEqual(checks[0].status, doctor.ABSENT)
        self.assertIn("no runtime file", checks[0].detail)


if __name__ == "__main__":
    unittest.main()
