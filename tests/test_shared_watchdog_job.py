"""Issue #60: one shared watchdog job sweeps every loop exactly once per tick.

``hermes cron create --script`` takes only a filename under ~/.hermes/scripts/ and no
arguments, so a job cannot carry ``--loop <id>``. A per-loop job therefore runs the shared
shim with no ``--loop``, and the shim sweeps *every* loop: N jobs cost N sweeps per tick,
so N loops give N² sweeps. The fix is one shared job (``cli.SHARED_JOB_NAME``) created only
if missing, removed only with the last loop.

These tests drive ``_install_schedule`` / ``_remove_cron`` against a disposable config dir
and a fake ``hermes`` that records its argv, so they never touch the operator's scheduler.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from review_loop import cli, config, doctor

HOST = "https://gw.example"


def raw_loop(loop_id):
    return {"id": loop_id, "repo": f"owner/{loop_id}", "fixers": ["fixer"],
            "reviewers": ["reviewer"], "read_token": "reader", "host": HOST,
            "seats": {"reviewer": {"route": f"{loop_id}-review", "profile": "reviewer"},
                      "fixer": {"route": f"{loop_id}-fix", "profile": "fixer"}}}


class FakeHermes:
    """A stand-in `hermes cron create/remove` that records argv and edits the job store.

    ``create`` appends a job the way the real scheduler does (never replaces), so a duplicate
    is visible; ``remove`` deletes by id. The log lets a test count how many jobs were made.
    """

    def __init__(self, home: Path):
        self.home = home
        self.log = home / "hermes.log"
        self.bin = home / "bin" / "hermes"
        self.bin.parent.mkdir(parents=True, exist_ok=True)
        self.bin.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            f"home = pathlib.Path({str(home)!r})\n"
            "store = home / 'cron' / 'jobs.json'\n"
            "args = sys.argv[1:]\n"
            f"with open({str(self.log)!r}, 'a') as f: f.write(json.dumps(args) + '\\n')\n"
            "data = json.loads(store.read_text()) if store.exists() else {'jobs': []}\n"
            "jobs = data['jobs']\n"
            "if args[:1] == ['cron'] and len(args) >= 3:\n"
            "    if args[1] == 'remove':\n"
            "        data['jobs'] = [j for j in jobs if j.get('id') != args[2]]\n"
            "    elif args[1] == 'create':\n"
            "        name = args[args.index('--name') + 1]\n"
            "        script = args[args.index('--script') + 1]\n"
            "        jobs.append({'id': f'job{len(jobs)+1}', 'name': name, 'script': script,\n"
            "                     'no_agent': True, 'enabled': True, 'state': 'scheduled',\n"
            "                     'schedule': {'kind': 'interval', 'minutes': 15},\n"
            "                     'next_run_at': '2030-01-01T00:00:00+00:00'})\n"
            "store.parent.mkdir(parents=True, exist_ok=True)\n"
            "store.write_text(json.dumps(data))\n"
            "sys.exit(0)\n"
            "sys.exit(2)\n")
        self.bin.chmod(0o755)

    @property
    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    @property
    def jobs(self):
        store = self.home / "cron" / "jobs.json"
        if not store.exists():
            return []
        return json.loads(store.read_text())["jobs"]


class SharedJobTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "hermes"
        self.hermes = FakeHermes(self.home)
        env = patch.dict(os.environ, {
            "REVIEW_LOOP_CONFIG_DIR": str(self.root / "configs"),
            "HERMES_HOME": str(self.home),
            "REVIEW_LOOP_HERMES": str(self.hermes.bin)})
        env.start()
        self.addCleanup(env.stop)
        config.config_dir().mkdir()
        for loop_id in ("widgets", "gadgets", "gizmos"):
            (config.config_dir() / f"{loop_id}.json").write_text(json.dumps(raw_loop(loop_id)))

    def _shim_jobs(self):
        return [job for job in self.hermes.jobs
                if Path(job.get("script", "")).name == cli.SHIM_NAME]

    # --- three loops → one job, one sweep per loop per tick -------------------------------
    def test_three_loops_init_schedule_creates_exactly_one_shared_job(self):
        """Each loop's ``init --schedule`` must not add a second job: one job sweeps all."""
        created = []
        for loop_id in ("widgets", "gadgets", "gizmos"):
            lines, ok = cli._install_schedule({"id": loop_id}, "15m", "local")
            self.assertTrue(ok, lines)
            created.append(lines[0])
        # One shared job, not three: N loops must not mean N jobs.
        self.assertEqual(len(self._shim_jobs()), 1, self.hermes.jobs)
        job = self._shim_jobs()[0]
        self.assertEqual(job["name"], cli.SHARED_JOB_NAME)
        # The second and third inits are idempotent: they report it, they do not create it.
        self.assertIn("already scheduled", created[1])
        self.assertIn("already scheduled", created[2])
        creates = [call for call in self.hermes.calls if call[:2] == ["cron", "create"]]
        self.assertEqual(len(creates), 1, creates)
        # The single job runs the shim with no --loop: the shim sweeps every loop once.
        self.assertEqual(job["script"], cli.SHIM_NAME)

    def test_one_job_sweeps_each_loop_exactly_once(self):
        """The shared job's shim runs the watchdog with no --loop, so every loop is swept once.

        N jobs each running the same unscoped shim is the N² bug (#60): with one job there is
        exactly one sweep per loop per tick. Proven by the watchdog's own contract: with no
        ``--loop`` it loads every readable loop and sweeps each once.
        """
        from review_loop import config as cfg
        loops, refused = cfg.readable_loops()
        self.assertEqual([loop["id"] for loop in loops], ["gadgets", "gizmos", "widgets"])
        self.assertEqual(refused, [])
        # Exactly one sweep source: the one shared job. A second job would double every sweep.
        cli._install_schedule({"id": "widgets"}, "15m", "local")
        cli._install_schedule({"id": "gizmos"}, "15m", "local")
        self.assertEqual(len(self._shim_jobs()), 1)

    # --- uninstall: the job outlives any one loop -------------------------------------------
    def test_uninstall_one_loop_keeps_the_job_for_the_others(self):
        cli._install_schedule({"id": "widgets"}, "15m", "local")
        self.assertEqual(len(self._shim_jobs()), 1)
        removed_jobs, failures = cli._remove_cron({"id": "widgets"})
        self.assertEqual(failures, [], failures)
        # The shared job must stay: two loops still sweep through it.
        self.assertEqual(len(self._shim_jobs()), 1, self.hermes.jobs)
        self.assertTrue(any(line.startswith("cron job kept") for line in removed_jobs),
                        removed_jobs)
        removes = [call for call in self.hermes.calls if call[:2] == ["cron", "remove"]]
        self.assertEqual(removes, [], removes)

    def test_uninstall_the_last_loop_removes_the_job(self):
        for loop_id in ("gadgets", "gizmos"):
            (config.config_dir() / f"{loop_id}.json").unlink()
        cli._install_schedule({"id": "widgets"}, "15m", "local")
        self.assertEqual(len(self._shim_jobs()), 1)
        removed_jobs, failures = cli._remove_cron({"id": "widgets"})
        self.assertEqual(failures, [], failures)
        # Last loop gone: the job goes with it.
        self.assertEqual(self._shim_jobs(), [], self.hermes.jobs)
        self.assertTrue(any(line.startswith("cron job removed") for line in removed_jobs),
                        removed_jobs)

    # --- migration: doctor names the legacy per-loop jobs ------------------------------------
    def test_doctor_names_legacy_per_loop_jobs_with_the_removal_commands(self):
        """A pre-#60 install has one job per loop; doctor must name each and say how to fix it."""
        store = self.home / "cron" / "jobs.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"jobs": [
            {"id": "legacy1", "name": "review loop watchdog (widgets)",
             "script": cli.SHIM_NAME, "no_agent": True, "enabled": True, "state": "scheduled",
             "schedule": {"kind": "interval", "minutes": 15},
             "next_run_at": "2030-01-01T00:00:00+00:00"},
            {"id": "legacy2", "name": "review loop watchdog (gadgets)",
             "script": cli.SHIM_NAME, "no_agent": True, "enabled": True, "state": "scheduled",
             "schedule": {"kind": "interval", "minutes": 15},
             "next_run_at": "2030-01-01T00:00:00+00:00"}]}))
        check = doctor.check_cron_job(config.load_id("widgets"))
        self.assertTrue(check.failed, check.detail)
        self.assertIn("per-loop watchdog job", check.detail)
        self.assertIn("N² sweeps per tick", check.detail)
        # The remedy names every legacy job's exact removal, then the shared-job create.
        self.assertIn("`hermes cron remove legacy1`", check.fix)
        self.assertIn("`hermes cron remove legacy2`", check.fix)
        self.assertIn(f'--name "{doctor.SHARED_JOB_NAME}"', check.fix)

    def test_doctor_verifies_the_shared_job(self):
        cli._install_schedule({"id": "widgets"}, "15m", "local")
        check = doctor.check_cron_job(config.load_id("widgets"))
        self.assertFalse(check.failed, check.detail)
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)


if __name__ == "__main__":
    unittest.main()
