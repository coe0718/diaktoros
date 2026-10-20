"""ASGI read contracts with real initialized loop/config/ledger artifacts.

No host configuration, network or credential fixture is needed. Optional web prerequisites
follow the same required-lane policy as the other Desktop integration tests.
"""
from __future__ import annotations

import _home_guard  # noqa: F401
import importlib.util
import json
import os
import pathlib
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hermes_prereqs import skip_or_fail
from diaktoros import config, desktop_api, run_supervisor


class ReadRoutes(unittest.TestCase):
    def setUp(self):
        for module in ('fastapi', 'httpx'):
            if importlib.util.find_spec(module) is None:
                skip_or_fail(self, module + ' is not installed')
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        env = mock.patch.dict(os.environ, {'DIAKTOROS_CONFIG_DIR': str(self.root / 'loops')})
        env.start()
        self.addCleanup(env.stop)
        config.config_dir().mkdir()
        raw = {'id': 'widgets', 'repo': 'acme/widgets', 'reviewers': ['reviewer'],
               'fixers': ['fixer'], 'read_token': 'reader',
               'tokens': {'reader': str(self.root / 'absent.pat')},
               'seats': {'reviewer': {'profile': 'reviewer', 'route': 'r-review'},
                         'fixer': {'profile': 'fixer', 'route': 'r-fix'}},
               'state_dir': str(self.root / 'state')}
        (config.config_dir() / 'widgets.json').write_text(json.dumps(raw))
        self.assertEqual(config.load_id('widgets')['repo'], 'acme/widgets')
        self.db = self.root / 'runs.sqlite'
        run_supervisor.Supervisor(self.db)
        patch = mock.patch.object(run_supervisor, 'production_ledger', return_value=self.db)
        patch.start()
        self.addCleanup(patch.stop)
        spec = importlib.util.spec_from_file_location('desktop_read_api', ROOT / 'dashboard/plugin_api.py')
        assert spec is not None and spec.loader is not None
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        app = FastAPI()
        app.include_router(self.api.router, prefix='/api/plugins/diaktoros')
        self.client = self.enterContext(TestClient(app, raise_server_exceptions=False))

    def get(self, path):
        return self.client.get('/api/plugins/diaktoros' + path)

    def test_initialized_read_routes_serve_real_artifacts(self):
        # None of the production projection functions is replaced. Missing runtime prerequisites
        # are honest unavailable/check results, not transport failures or invented green state.
        for path in ('/loops', '/stats?loop=widgets', '/health?loop=widgets', '/settings',
                     '/records?loop=widgets', '/hooks?loop=widgets', '/overview'):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status_code, 200, response.text[:200])
                self.assertIsInstance(response.json(), dict)
        self.assertEqual(self.get('/stats?loop=widgets').json()['repo'], 'acme/widgets')
        self.assertEqual(self.get('/overview').json()['loops'][0]['id'], 'widgets')

    def test_corrupt_real_ledger_is_safe_unavailable_not_500_or_empty_stats(self):
        self.db.write_bytes(b'not a SQLite database')
        response = self.get('/stats?loop=widgets')
        self.assertEqual(response.status_code, 503, response.text[:200])
        self.assertEqual(response.json(), {'detail': 'Desktop read unavailable; retry the read.'})
        # Overview keeps its established per-loop error isolation contract.
        entry = self.get('/overview').json()['loops'][0]
        self.assertFalse(entry['available'])
        self.assertTrue(entry['error'])

    def test_read_exceptions_are_bounded_json_for_every_route(self):
        # Exercise the actual ASGI exception boundary, not direct route function calls.
        for path, name in (('/health?loop=widgets', 'health_view'),
                           ('/stats?loop=widgets', 'stats_view'),
                           ('/settings', 'settings_view'),
                           ('/records?loop=widgets', 'records_view'),
                           ('/hooks?loop=widgets', 'hooks_view'),
                           ('/overview', 'overview_view'), ('/loops', 'loops'),
                           ('/actions?loop=widgets&action=escalate&pr=1', 'action_view'),
                           ('/now?loop=widgets', 'now_view'), ('/pr?loop=widgets&pr=1', 'pr_view')):
            with self.subTest(path=path), mock.patch.object(
                    desktop_api, name, side_effect=OSError('/private/secret.pat bearer secret')):
                response = self.get(path)
                self.assertEqual(response.status_code, 503, response.text[:200])
                self.assertEqual(response.json(), {'detail': 'Desktop read unavailable; retry the read.'})

    def test_real_host_mount_and_authenticated_reads(self):
        if importlib.util.find_spec('hermes_cli') is None:
            skip_or_fail(self, 'Hermes is not installed')
        # A fresh process resolves the real host's home globals from this fixture, never the
        # operator's. Import-time mounting, auth and plugin runtime gates are not mocked.
        home = self.root / 'host'
        (home / 'plugins').mkdir(parents=True)
        (home / 'plugins/diaktoros').symlink_to(ROOT, target_is_directory=True)
        (home / 'config.yaml').write_text('plugins:\n  enabled: [diaktoros]\n')
        script = '''
import json
from fastapi.testclient import TestClient
from hermes_cli import web_server as server
from diaktoros import run_supervisor
run_supervisor.production_ledger = lambda: __import__('pathlib').Path(DB)
paths = ['/loops', '/stats?loop=widgets', '/health?loop=widgets', '/settings',
         '/records?loop=widgets', '/hooks?loop=widgets', '/overview']
with TestClient(server.app, base_url='http://127.0.0.1', raise_server_exceptions=False) as client:
    results = []
    for path in paths:
        response = client.get('/api/plugins/diaktoros' + path,
            headers={server._SESSION_HEADER_NAME: server._SESSION_TOKEN})
        results.append([path, response.status_code, response.headers.get('content-type', '')])
print('HOST_READS=' + json.dumps(results))
'''
        env = dict(os.environ, HERMES_HOME=str(home), HOME=str(home),
                   HERMES_REAL_HOME=str(home), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-B', '-c', 'DB=' + repr(str(self.db)) + '\n' + script],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=45)
        # Do not expose arbitrary imported-host logs, response bodies, tokens or paths.
        self.assertEqual(result.returncode, 0, 'isolated host initialization failed')
        lines = [line for line in (result.stdout + '\n' + result.stderr).splitlines()
                 if line.startswith('HOST_READS=')]
        self.assertEqual(len(lines), 1, 'host did not report its read results')
        results = json.loads(lines[0].split('=', 1)[1])
        self.assertEqual(len(results), 7)
        for path, status, content_type in results:
            with self.subTest(path=path):
                self.assertEqual(status, 200)
                self.assertIn('application/json', content_type)

    def test_expected_selection_and_probe_refusals_keep_their_status(self):
        self.assertEqual(self.get('/stats?loop=absent').status_code, 404)
        self.assertEqual(self.get('/stats?loop=widgets&since=never').status_code, 400)
        self.assertEqual(self.get('/health?loop=widgets&probe=1').status_code, 403)


if __name__ == '__main__':
    unittest.main()
