"""tests/leakguard.py fails a run when a Python child the suite started leaks, not only the suite.

Each case runs the guard on a one-test probe module whose test starts a child process.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

GUARD = Path(__file__).resolve().parent / 'leakguard.py'

PROBE = '''
import subprocess, sys, unittest

class Probe(unittest.TestCase):
    def test_child(self):
        subprocess.run([sys.executable, "-c", {child!r}], check=True, timeout=60)
'''


class ChildLeaksFailTheRun(unittest.TestCase):
    def guard(self, child: str, **env) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            Path(tmp, 'probe_child.py').write_text(PROBE.format(child=textwrap.dedent(child)))
            return subprocess.run([sys.executable, str(GUARD), 'discover', '-s', tmp,
                                   '-p', 'probe_*.py'], cwd=tmp, capture_output=True,
                                  text=True, timeout=120, env={**os.environ, **env})

    def assert_charged(self, result, leak: str, where: str | None = 'released at:'):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn('ERROR: test_child', result.stderr)
        self.assertIn('in child', result.stderr)
        self.assertIn(leak, result.stderr)
        if where:
            self.assertIn(where, result.stderr)

    def test_clean_child_passes(self):
        result = self.guard('import sqlite3\ncon = sqlite3.connect(":memory:")\ncon.close()\n')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_child_leaking_a_file_fails(self):
        result = self.guard('import os\nf = open(os.devnull)\ndel f\n')
        self.assert_charged(result, 'unclosed file')   # with the statement that dropped it

    def test_child_leaking_a_module_global_at_shutdown_fails(self):
        # Released only while the interpreter shuts down, after every atexit handler has run.
        result = self.guard('import os\nf = open(os.devnull)\n')
        self.assert_charged(result, 'unclosed file', where=None)

    def test_traced_child_names_the_allocation(self):
        result = self.guard('import os\nf = open(os.devnull)\ndel f\n', PYTHONTRACEMALLOC='5')
        self.assert_charged(result, 'unclosed file', where='allocated at:')

    @unittest.skipIf(sys.version_info < (3, 13), 'sqlite3 warns about an unclosed database from 3.13')
    def test_child_leaking_a_sqlite_connection_fails(self):
        result = self.guard('import sqlite3\ncon = sqlite3.connect(":memory:")\ndel con\n')
        self.assert_charged(result, 'unclosed database')

    def test_child_leaving_a_grandchild_running_fails(self):
        result = self.guard('import subprocess\nP = subprocess.Popen(["sleep", "2"])\n')
        self.assert_charged(result, 'is still running', where=None)   # found at exit, not released

    def test_child_handing_off_a_detached_grandchild_passes(self):
        # A supervisor worker hands the queue to a successor in its own session and exits.
        result = self.guard('import subprocess\n'
                            'P = subprocess.Popen(["sleep", "2"], start_new_session=True)\n')
        self.assertEqual(result.returncode, 0, result.stderr)


class RecorderReachesOnlyFixtureWorkers(unittest.TestCase):
    """The supervisor scrubs a worker's environment; the recorder crosses it in fixture mode only."""

    def spawned_env(self, **kwargs) -> dict:
        from review_loop import run_supervisor
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            config = Path(tmp, 'config.json')
            config.write_text('{}')
            config.chmod(0o600)
            if kwargs.pop('production', False):
                kwargs.update(production_config=config, hermes_home=tmp)
            sup = run_supervisor.Supervisor(Path(tmp, 'runs.sqlite'), **kwargs)
            env = {'REVIEW_LOOP_LEAK_LOG': '/leaks.jsonl', 'REVIEW_LOOP_LEAK_SITE': '/site'}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(run_supervisor.subprocess, 'Popen') as popen:
                sup._spawn()
            run_supervisor._WORKERS.clear()
            return popen.call_args.kwargs['env']

    def test_fixture_worker_carries_the_recorder(self):
        env = self.spawned_env(fixture_mode=True)
        self.assertEqual(env['REVIEW_LOOP_LEAK_LOG'], '/leaks.jsonl')
        self.assertEqual(env['PYTHONPATH'].split(os.pathsep)[0], '/site')

    def test_production_worker_never_does(self):
        env = self.spawned_env(production=True)
        self.assertNotIn('REVIEW_LOOP_LEAK_LOG', env)
        self.assertNotIn('/site', env['PYTHONPATH'].split(os.pathsep))


if __name__ == '__main__':
    unittest.main()
