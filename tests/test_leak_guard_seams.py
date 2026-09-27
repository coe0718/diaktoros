"""Children that ignore PYTHONPATH still report leaks under tests/leakguard.py, and only there.

The seat-model resolver runs ``python -E -s -c <program>`` and Git's askpass is a script run
with a scrubbed environment; neither reads PYTHONPATH, so the guard loads its recorder into
them by absolute path. With the guard off, each argv, script and environment is pinned to be
exactly what it was before the guard existed.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from review_loop import broker, safe_push, seat_model

LEAK = 'import os\nf = open(os.devnull)\ndel f\n'
RESOLVER_ENV = {'PATH', 'HOME', 'HERMES_HOME', 'LANG', 'PYTHONDONTWRITEBYTECODE'}


class Seams(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=os.environ.get('TMPDIR')))
        self.addCleanup(__import__('shutil').rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / 'leaks.jsonl'
        (self.tmp / 'hermes').mkdir()
        env = mock.patch.dict(os.environ, {'HERMES_HOME': str(self.tmp / 'hermes')})
        env.start()
        self.addCleanup(env.stop)

    def guard(self, on: bool):
        """A private log, so these probes' deliberate leaks never reach the suite's own guard."""
        if on:
            os.environ['REVIEW_LOOP_LEAK_LOG'] = str(self.log)
        else:
            os.environ.pop('REVIEW_LOOP_LEAK_LOG', None)

    def recorded(self) -> list[str]:
        if not self.log.exists():
            return []
        return [leak for line in self.log.read_text().splitlines()
                for leak in json.loads(line)['leaks']]

    # -- the seat-model resolver: python -E -s -c ------------------------------------------

    def resolver_call(self) -> tuple[list, dict]:
        seen = {}

        def fake_run(argv, **kwargs):
            seen['argv'], seen['env'] = argv, kwargs['env']
            return subprocess.CompletedProcess(argv, 0, b'{}', b'')

        settings = {'venv': str(self.tmp / 'venv'), 'source': str(self.tmp / 'src')}
        with mock.patch.object(seat_model.subprocess, 'run', side_effect=fake_run):
            try:
                seat_model.run_resolver('default', 'inspect', settings)
            except seat_model.SeatModelError:
                pass
        return seen['argv'], seen['env']

    def test_resolver_unchanged_without_the_guard(self):
        self.guard(False)
        argv, env = self.resolver_call()
        self.assertEqual(argv[:5], [str(self.tmp / 'venv/bin/python'), '-E', '-s', '-c',
                                    seat_model._RESOLVER])
        self.assertEqual(set(env), RESOLVER_ENV)

    def test_resolver_leak_is_recorded_under_the_guard(self):
        self.guard(True)
        argv, env = self.resolver_call()
        self.assertTrue(argv[4].endswith(seat_model._RESOLVER))
        self.assertEqual(set(env), RESOLVER_ENV | {'REVIEW_LOOP_LEAK_LOG'})
        # The same flags and environment, with a leaking program after the recorder.
        loader = argv[4][:-len(seat_model._RESOLVER)]
        subprocess.run([sys.executable, '-E', '-s', '-c', loader + LEAK], env=env, check=True,
                       capture_output=True, timeout=60)
        self.assertTrue(any('unclosed file' in leak for leak in self.recorded()), self.recorded())

    # -- Git's askpass: a script with a scrubbed environment ---------------------------------

    def askpass_call(self) -> tuple[str, dict]:
        seen = {}

        def fake_run(cmd, **kwargs):
            seen['script'] = Path(kwargs['env']['GIT_ASKPASS']).read_text()
            seen['env'] = dict(kwargs['env'])
            raise OSError('stop before any Git runs')

        loop = {'tokens': {'fixer': str(self.tmp / 'fixer.pat')}}
        with mock.patch.object(safe_push.subprocess, 'run', side_effect=fake_run):
            with self.assertRaises(broker.BrokerDenied):
                safe_push._git_cas(loop, 'acme/widgets', 'fix', 'a' * 40, [], 'm', 'fixer',
                                   {'name': 'n', 'email': 'e@x'}, remote=str(self.tmp))
        return seen['script'], seen['env']

    def test_askpass_unchanged_without_the_guard(self):
        self.guard(False)
        script, env = self.askpass_call()
        self.assertEqual(script, "#!/usr/bin/python3\nimport os,sys\nfrom pathlib import Path\n"
                         "print('x-access-token' if 'Username' in sys.argv[1] "
                         "else Path(os.environ['REVIEW_LOOP_TOKEN_FILE']).read_text().strip())\n")
        self.assertNotIn('REVIEW_LOOP_LEAK_LOG', env)
        self.assertNotIn('PYTHONPATH', env)

    def test_askpass_leak_is_recorded_under_the_guard(self):
        self.guard(True)
        script, env = self.askpass_call()
        self.assertEqual(env['REVIEW_LOOP_LEAK_LOG'], str(self.log))
        self.assertNotIn('PYTHONPATH', env)
        probe = self.tmp / 'askpass.py'
        probe.write_text(script + LEAK)
        (self.tmp / 'fixer.pat').write_text('dummy')
        answer = subprocess.run([sys.executable, str(probe), 'Username for x'], env=env,
                                capture_output=True, text=True, check=True, timeout=60)
        self.assertEqual(answer.stdout, 'x-access-token\n')   # the prompt still answers
        self.assertTrue(any('unclosed file' in leak for leak in self.recorded()), self.recorded())


if __name__ == '__main__':
    unittest.main()
