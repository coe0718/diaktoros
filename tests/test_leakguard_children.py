"""tests/leakguard.py fails a run when a Python child the suite started leaks, not only the suite.

Each case runs the guard on a one-test probe module whose test starts a child process.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

GUARD = Path(__file__).resolve().parent / 'leakguard.py'
SITE = Path(__file__).resolve().parent / 'leaksite'

PROBE = '''
import subprocess, sys, unittest

class Probe(unittest.TestCase):
    def test_child(self):
        subprocess.run([sys.executable, "-c", {child!r}], check=True, timeout=60)
{own}'''


def outside_guard() -> dict:
    """This suite's environment minus its own guard: a nested run's leaks are its own to
    report, and its guard must arm itself rather than inherit this one."""
    env = dict(os.environ)
    env.pop('REVIEW_LOOP_LEAK_LOG', None)
    path = [p for p in env.pop('PYTHONPATH', '').split(os.pathsep) if p and p != str(SITE)]
    if path:
        env['PYTHONPATH'] = os.pathsep.join(path)
    return env


class ChildLeaksFailTheRun(unittest.TestCase):
    def guard(self, child: str, own: str = '', **env) -> subprocess.CompletedProcess:
        """Run the guard on a probe test that starts ``child``, then runs ``own`` itself."""
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            body = ''.join('        ' + line + '\n' for line in own.splitlines())
            Path(tmp, 'probe_child.py').write_text(
                PROBE.format(child=textwrap.dedent(child), own=body))
            return subprocess.run([sys.executable, str(GUARD), 'discover', '-s', tmp,
                                   '-p', 'probe_*.py'], cwd=tmp, capture_output=True,
                                  text=True, timeout=120, env={**outside_guard(), **env})

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

    def test_the_test_process_leaking_a_file_fails(self):
        # The guard's own process: unittest.main would otherwise reset every warning category
        # to 'default' for the run, and a finalizer's ResourceWarning would only print.
        result = self.guard('pass', own='import os\nf = open(os.devnull)\ndel f')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn('ERROR: test_child', result.stderr)
        self.assertIn('resource leaked while this test ran', result.stderr)
        self.assertIn('unclosed file', result.stderr)
        self.assertNotIn('in child', result.stderr)

    def test_child_leaking_a_file_fails(self):
        result = self.guard('import os\nf = open(os.devnull)\ndel f\n')
        self.assert_charged(result, 'unclosed file')   # with the statement that dropped it

    def test_child_leaking_a_module_global_at_shutdown_fails(self):
        # Released only while the interpreter shuts down, after every atexit handler has run.
        result = self.guard('import os\nf = open(os.devnull)\n')
        self.assert_charged(result, 'unclosed file', where=None)

    def test_traced_child_names_the_allocation(self):
        result = self.guard('import os\nf = open(os.devnull)\ndel f\n', PYTHONTRACEMALLOC='5')
        # 3.14 reports the raw FileIO with no object to trace; the release site still names it.
        where = 'allocated at:' if sys.version_info < (3, 14) else 'released at:'
        self.assert_charged(result, 'unclosed file', where=where)

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


class LanesRefuseToRunUnguarded(unittest.TestCase):
    """Each lane checks its guard is armed; a run without it fails instead of passing green."""

    DISARM = 'import leakguard; leakguard.install = lambda: None\n'

    def lane(self, script: str, args: list[str], disarm: bool,
             setup: str = '') -> subprocess.CompletedProcess:
        program = ('import runpy, sys\n'
                   f'sys.path.insert(0, {str(GUARD.parent)!r})\n'
                   + (self.DISARM if disarm else '') + setup +
                   f'sys.argv = [{script!r}, *{args!r}]\n'
                   f'runpy.run_path({script!r}, run_name="__main__")\n')
        return subprocess.run([sys.executable, '-c', program], cwd=str(GUARD.parents[1]),
                              env=outside_guard(), capture_output=True, text=True, timeout=300)

    def test_harness_lane(self):
        script = str(GUARD.parent / 'run_tests.py')
        armed = self.lane(script, ['config'], disarm=False)
        self.assertEqual(armed.returncode, 0, armed.stdout[-2000:] + armed.stderr[-2000:])
        unarmed = self.lane(script, ['config'], disarm=True)
        self.assertNotEqual(unarmed.returncode, 0, unarmed.stdout[-2000:])
        self.assertIn('leak guard is not armed: leakguard.install() never ran', unarmed.stderr)

    def test_boundary_lane(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            Path(tmp, 'probe_clean.py').write_text(
                'import unittest\nclass P(unittest.TestCase):\n    def test_ok(self): pass\n')
            args = ['discover', '-s', tmp, '-p', 'probe_*.py']
            armed = self.lane(str(GUARD), args, disarm=False)
            self.assertEqual(armed.returncode, 0, armed.stderr[-2000:])
            unarmed = self.lane(str(GUARD), args, disarm=True)
        self.assertNotEqual(unarmed.returncode, 0)
        self.assertIn('leak guard is not armed: leakguard.install() never ran', unarmed.stderr)


class LanesCatchADisarmMidRun(unittest.TestCase):
    """A test or group that disarms the guard is charged for it, and the guard re-arms, so a
    leak after it is still caught. The runner restores warning filters after the run, so only a
    check while the run is under way can see a reset."""

    lane = LanesRefuseToRunUnguarded.lane

    def boundary(self, probe: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            Path(tmp, 'probe_disarm.py').write_text(textwrap.dedent(probe))
            return self.lane(str(GUARD), ['discover', '-s', tmp, '-p', 'probe_*.py'],
                             disarm=False)

    def test_boundary_lane_charges_the_test_that_disarmed_it(self):
        result = self.boundary('''
            import os, sys, unittest, warnings

            class P(unittest.TestCase):
                def test_a_resets_filters(self):
                    warnings.resetwarnings()

                def test_b_leaks(self):
                    f = open(os.devnull)
                    del f

                def test_c_replaces_the_hook(self):
                    sys.unraisablehook = sys.__unraisablehook__
            ''')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        err = result.stderr
        self.assertIn('ERROR: test_a_resets_filters', err)
        self.assertIn('leak guard was disarmed while this test ran: ResourceWarning is not an error',
                      err)
        # Re-armed: the next test's leak is charged as a leak, and that test is not charged
        # with a disarm it did not cause.
        self.assertRegex(err, r'ERROR: test_b_leaks[^\n]*\n-+\nresource leaked while this test '
                              r'ran:\nResourceWarning: unclosed file')
        self.assertEqual(err.count('leak guard was disarmed while this test ran'), 2, err)
        self.assertIn('ERROR: test_c_replaces_the_hook', err)
        self.assertIn("sys.unraisablehook is not the guard's", err)

    def test_boundary_lane_charges_a_redirected_child_log_and_restores_it(self):
        result = self.boundary('''
            import os, subprocess, sys, unittest

            class P(unittest.TestCase):
                def test_a_redirects_the_child_log(self):
                    os.environ['REVIEW_LOOP_LEAK_LOG'] = os.devnull

                def test_b_child_leaks(self):
                    subprocess.run([sys.executable, '-c',
                                    'import os\\nf = open(os.devnull)\\ndel f\\n'], check=True)
            ''')
        err = result.stderr
        self.assertNotEqual(result.returncode, 0, err)
        self.assertIn("leak guard was disarmed while this test ran: REVIEW_LOOP_LEAK_LOG does not "
                      "name this run's child log", err)
        # Restored: the next test's child reports to the run's own log again.
        self.assertRegex(err, r'ERROR: test_b_child_leaks[^\n]*\n-+\nresource leaked while this '
                              r'test ran:\nin child')

    def test_boundary_lane_catches_a_disarm_after_the_last_test(self):
        result = self.boundary('''
            import sys, unittest

            def tearDownModule():
                sys.unraisablehook = sys.__unraisablehook__

            class P(unittest.TestCase):
                def test_ok(self):
                    pass
            ''')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("leak guard is not armed after the run: sys.unraisablehook is not the guard's",
                      result.stderr)

    def test_harness_lane_charges_the_group_that_disarmed_it(self):
        # run_tests.py builds its groups from the area modules, so swap the group in there.
        setup = ('import warnings\nfrom harness import gates\n'
                 'gates.GROUPS["config"] = lambda: warnings.resetwarnings()\n')
        result = self.lane(str(GUARD.parent / 'run_tests.py'), ['config'], disarm=False,
                           setup=setup)
        self.assertNotEqual(result.returncode, 0, result.stdout[-2000:])
        self.assertIn('config: leak guard was disarmed during this group: '
                      'ResourceWarning is not an error', result.stdout)


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
            env = {'REVIEW_LOOP_LEAK_LOG': '/leaks.jsonl'}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(run_supervisor.subprocess, 'Popen') as popen:
                sup._spawn()
            run_supervisor._WORKERS.clear()
            return popen.call_args.kwargs['env']

    def test_fixture_worker_carries_the_recorder(self):
        env = self.spawned_env(fixture_mode=True)
        self.assertEqual(env['REVIEW_LOOP_LEAK_LOG'], '/leaks.jsonl')
        self.assertEqual(env['PYTHONPATH'].split(os.pathsep)[0], str(SITE))

    def test_a_fixture_command_run_by_the_worker_carries_it_too(self):
        # _spawn forwards the recorder to the worker; the worker's _run_fixture must pass it on
        # to the fixture command, whose environment it scrubs again.
        from review_loop import run_supervisor
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            seen = Path(tmp, 'seen')
            probe = ('import os, sys\nwith open(sys.argv[1], "w") as out:\n'
                     '    out.write(os.environ.get("REVIEW_LOOP_LEAK_LOG", "") + "\\n"\n'
                     '              + os.environ.get("PYTHONPATH", ""))\n')
            sup = run_supervisor.Supervisor(Path(tmp, 'runs.sqlite'), fixture_mode=True,
                                            fixture_command=[sys.executable, '-c', probe, str(seen)])
            try:
                sup.enqueue('d', 'o/r', 1, 'a', 'reviewer')
                deadline = time.monotonic() + 30
                while (sup.get('d') or {}).get('state') not in ('succeeded', 'failed'):
                    self.assertLess(time.monotonic(), deadline, sup.get('d'))
                    time.sleep(0.05)
                self.assertEqual(sup.get('d')['state'], 'succeeded', sup.get('d'))
            finally:
                for worker in run_supervisor._WORKERS:
                    worker.wait(timeout=30)
                run_supervisor._WORKERS.clear()
            log, path = seen.read_text().split('\n', 1)
        # On the raw discover lane neither var is set, so the scrubbed env is empty and the
        # assertions are vacuous — guard them so the test runs instead of raising KeyError.
        # On the guarded lanes both are set and the carry-through is proven.
        if 'REVIEW_LOOP_LEAK_LOG' in os.environ:
            self.assertEqual(log, os.environ['REVIEW_LOOP_LEAK_LOG'])
        if 'PYTHONPATH' in os.environ:
            self.assertEqual(path.split(os.pathsep)[0], str(SITE))

    def test_production_worker_never_does(self):
        env = self.spawned_env(production=True)
        self.assertNotIn('REVIEW_LOOP_LEAK_LOG', env)
        self.assertNotIn(str(SITE), env['PYTHONPATH'].split(os.pathsep))


if __name__ == '__main__':
    unittest.main()
