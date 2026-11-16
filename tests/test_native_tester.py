"""The tester runner must preserve failures and keep live credentials out of tests."""
import _home_guard  # noqa: F401
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import native_tester


class TesterRunnerTests(unittest.TestCase):
    def test_credentials_and_live_install_settings_are_not_inherited(self):
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN':'secret', 'OPENAI_API_KEY':'secret',
                                         'HERMES_HOME':'/live', 'DIAKTOROS_CONFIG_DIR':'/live'}):
            env = native_tester.environment()
        for key in ('GITHUB_TOKEN', 'OPENAI_API_KEY', 'HERMES_HOME', 'DIAKTOROS_CONFIG_DIR'):
            self.assertNotIn(key, env)
        self.assertEqual(env['DIAKTOROS_REQUIRE_NATIVE_STORAGE'], '1')

    def test_failed_suite_stops_run_and_is_preserved_in_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()/'evidence'
            def step(name, *args, **kwargs):
                return {'name':name, 'status':'failed' if name == 'test_seatbelt' else 'passed'}
            with mock.patch.object(native_tester, 'preflight', return_value={'passed':True}), \
                    mock.patch.object(native_tester, 'run_step', side_effect=step) as command:
                self.assertEqual(native_tester.run('boundary', output), 1)
            self.assertEqual(command.call_count, 3)
            report = json.loads((output/'result.json').read_text())
            self.assertFalse(report['passed'])
            self.assertEqual(report['steps'][-1]['name'], 'test_seatbelt')
            self.assertFalse(report['hermes_model_turns'])

    def test_preflight_failure_never_launches_native_suites(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()/'evidence'
            with mock.patch.object(native_tester, 'preflight', return_value={'passed':False}), \
                    mock.patch.object(native_tester, 'run_step', return_value={'status':'passed'}) as command:
                self.assertEqual(native_tester.run('boundary', output), 1)
            self.assertEqual(command.call_count, 2)
            with self.assertRaises(FileExistsError):
                native_tester.run('preflight', output)

    def test_real_stalled_test_process_times_out_with_its_log_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            result = native_tester.run_step('stall', [sys.executable, '-u', '-c',
                                           'import time; print("started"); time.sleep(60)'],
                                           output, timeout=0.2)
            self.assertEqual(result['status'], 'timeout')
            self.assertLess(result['seconds'], 3)
            self.assertIn('started', (output/'stall.log').read_text())

    def test_soak_plan_requires_precondition_and_requests_full_200_turns(self):
        steps = native_tester.plan('soak', Path('/evidence'))
        self.assertEqual(steps[0][0], 'cleanup_precondition')
        self.assertEqual(steps[1][1][-4:], ['--turns', '200', '--output', '/evidence/soak'])


if __name__ == '__main__':
    unittest.main()
