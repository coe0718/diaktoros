"""#651: bounded local records, pinned to real artifact writers (no GitHub calls)."""
import _home_guard  # noqa: F401
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from diaktoros import broker, config, desktop_api, gate_failures, gh, main_check, safe_push, state


def require_records_prereqs(test, dependencies=('fastapi', 'httpx')):
    from hermes_prereqs import skip_or_fail
    for dependency in dependencies:
        if importlib.util.find_spec(dependency) is None:
            skip_or_fail(test, 'Desktop records integration needs ' + dependency)


class RecordsPrerequisites(unittest.TestCase):
    def test_missing_dependency_skips_standalone_but_fails_required_lane(self):
        for required in (False, True):
            for dependency in ('fastapi', 'httpx'):
                with self.subTest(required=required, dependency=dependency), \
                        patch('hermes_prereqs.REQUIRED', required), \
                        patch.object(importlib.util, 'find_spec',
                                     side_effect=lambda name: None if name == dependency else object()):
                    with self.assertRaisesRegex(AssertionError if required else unittest.SkipTest, dependency):
                        require_records_prereqs(self)


class Records(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.loop = {'id': 'widgets', 'repo': 'acme/widgets', 'state_dir': str(self.root / 'state'),
                     'required_checks': ['unit tests', 'CI / lint']}
        self.directory = config.state_dir(self.loop)
        self.directory.mkdir()
        for patcher in (patch.object(config, 'readable_loops', return_value=([self.loop], [])),
                        patch.object(gate_failures, 'fallback_ledger',
                                     return_value=gate_failures.Ledger(self.root / 'fallback')),
                        patch.object(gh, 'api', side_effect=AssertionError('network forbidden')),
                        patch.object(gh, 'token', side_effect=AssertionError('credentials forbidden'),
                                     create=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ledger = gate_failures.loop_ledger(self.loop)

    def view(self):
        return desktop_api.records_view('widgets')

    def failure(self, raw='{}', **extra):
        key = gate_failures.fingerprint('gate_reviewer', raw)
        self.ledger.record(key, {'repo': self.loop['repo'], 'pr': 12, 'head': 'a' * 40,
                                'gate': 'gate_reviewer', 'kind': 'timeout',
                                'error': '/home/private token=secret', 'traceback': 'secret',
                                **extra}, raw)
        return key

    def audit(self, number=12):
        broker._audit(self.loop, self.loop['repo'], number, 'a' * 40, 'private/branch',
                      'reviewer', 'review', 'private-login')

    def main(self):
        st = state.state_for(self.loop)
        main_check._remember(st, self.directory / main_check.FILE, 'green', 'b' * 40)
        main_check._remember(st, self.directory / main_check.FILE, 'notified', 'c' * 40)

    def test_missing_is_not_verified_empty(self):
        view = self.view()
        self.assertEqual(set(view), {'loop', 'repo', 'read_at', 'failures', 'broker', 'main_ci'})
        for key in ('failures', 'broker', 'main_ci'):
            self.assertEqual(view[key], {'status': 'missing', 'reason': 'not_recorded', 'items': []})

    def test_real_writers_contract_and_secret_exclusion(self):
        key = self.failure()
        resolved = self.failure('{"other":1}')
        self.ledger.resolve(resolved, 'completed', settle=False)
        self.audit()
        self.main()
        view = self.view()
        self.assertEqual(view['failures']['status'], 'available')
        self.assertEqual([item['id'] for item in view['failures']['items']], [key])
        self.assertEqual(set(view['failures']['items'][0]),
                         {'id', 'gate', 'kind', 'pr', 'head', 'first_at', 'last_at', 'attempts', 'redrives'})
        self.assertEqual(view['broker']['items'], [{'at': view['broker']['items'][0]['at'],
                         'pr': 12, 'head': 'a' * 40, 'role': 'reviewer', 'operation': 'review',
                         'phase': 'recorded', 'outcome': 'recorded'}])
        self.assertEqual(view['main_ci']['items'], [{'green': 'b' * 40, 'notified': 'c' * 40,
                          'required_checks': ['unit tests', 'CI / lint']}])
        for forbidden in ('private', 'secret', 'traceback', 'login', 'branch', str(self.root)):
            self.assertNotIn(forbidden, json.dumps(view))

    def test_resolved_failures_are_verified_empty_not_quiet_declines(self):
        key = self.failure()
        self.ledger.resolve(key, 'completed', settle=False)
        self.assertEqual(self.view()['failures'],
                         {'status': 'available', 'reason': 'recorded', 'items': []})

    def test_broker_latest_bounded_and_push_attempt_not_success(self):
        for number in range(65):
            self.audit(number)
        safe_push._audit(self.loop, {'repo': self.loop['repo'], 'pr': 123, 'old_head': 'd' * 40,
                                    'role': 'fixer', 'operation': 'push', 'phase': 'attempt',
                                    'paths': ['/home/private'], 'login': 'secret'})
        rows = self.view()['broker']['items']
        self.assertEqual(len(rows), desktop_api.RECORDS_MAX)
        self.assertEqual((rows[0]['pr'], rows[0]['phase'], rows[0]['outcome']),
                         (123, 'attempt', 'unknown'))
        self.assertEqual(rows[-1]['pr'], 16)

    def test_reconciled_push_writer_outcomes_are_preserved(self):
        for operation in ('push', 'update_branch'):
            for outcome in ('unchanged', 'published', 'unknown', 'published_pr_unverified'):
                with self.subTest(operation=operation, outcome=outcome):
                    safe_push._audit(self.loop, {'repo': self.loop['repo'], 'pr': 123,
                        'old_head': 'd' * 40, 'role': 'fixer', 'operation': operation,
                        'phase': 'reconciled', 'outcome': outcome})
                    row = self.view()['broker']['items'][0]
                    self.assertEqual((row['operation'], row['outcome']), (operation, outcome))

    def test_jsonl_tail_discards_only_incomplete_prefix_and_bounds_io(self):
        self.audit()
        path = self.directory / 'broker-audit.jsonl'
        line = path.read_bytes()
        path.write_bytes(line * (desktop_api.RECORDS_BYTES // len(line) + 10))
        real_pread = os.pread
        calls = []
        def bounded(fd, count, offset):
            calls.append(count)
            return real_pread(fd, count, offset)
        with patch.object(os, 'pread', side_effect=bounded):
            self.assertEqual(self.view()['broker']['status'], 'available')
        self.assertLessEqual(max(calls), desktop_api.RECORDS_BYTES + 1)
        path.write_bytes(b'x' * (desktop_api.RECORDS_BYTES + 10))
        self.assertEqual(self.view()['broker']['status'], 'unreadable')

    def test_malformed_nested_artifacts_never_verified_empty(self):
        cases = {'gate-failures.json': [[], {'abc': None}, {'abc': {'resolved': 'false'}}],
                 'broker-audit.jsonl': [[None], [{'repo': self.loop['repo'], 'at': []}]],
                 'main-ci.json': [[], {}, {'green': []}, {'green': 'secret'}]}
        section = {'gate-failures.json': 'failures', 'broker-audit.jsonl': 'broker',
                   'main-ci.json': 'main_ci'}
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    (self.directory / name).write_text(json.dumps(value))
                    result = self.view()[section[name]]
                    self.assertEqual((result['status'], result['reason'], result['items']),
                                     ('unreadable', 'malformed', []))

    def test_fifo_symlinks_directories_and_oversize_refused(self):
        path = self.directory / 'main-ci.json'
        for kind in ('fifo', 'symlink', 'directory', 'large'):
            with self.subTest(kind=kind):
                if kind == 'fifo':
                    os.mkfifo(path)
                elif kind == 'symlink':
                    path.symlink_to(self.root / 'not-read-secret')
                elif kind == 'directory':
                    path.mkdir()
                else:
                    path.write_bytes(b' ' * (desktop_api.RECORDS_BYTES + 1))
                result = self.view()['main_ci']
                self.assertEqual(result['status'], 'unreadable')
                self.assertEqual(result['reason'], 'too_large' if kind == 'large' else 'unsafe_file')
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()

    def test_parent_directory_symlink_refused(self):
        real = self.root / 'real'
        self.directory.rename(real)
        self.directory.symlink_to(real, target_is_directory=True)
        self.assertEqual(self.view()['main_ci']['reason'], 'unsafe_file')

    def test_fallback_filters_repository_and_deduplicates(self):
        key = self.failure()
        spare = gate_failures.fallback_ledger()
        for repo, raw in ((self.loop['repo'], '{}'), ('other/repo', '{"x":1}')):
            spare.record(gate_failures.fingerprint('gate_reviewer', raw),
                         {**self.ledger.entries()[key], 'repo': repo}, raw)
        self.assertEqual([row['id'] for row in self.view()['failures']['items']], [key])
        self.ledger.path.unlink()
        self.assertEqual([row['id'] for row in self.view()['failures']['items']], [key])

    def test_read_preserves_artifacts_and_reads_no_payloads(self):
        self.failure()
        self.audit()
        self.main()
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        with patch.object(gate_failures.Ledger, 'payload',
                          side_effect=AssertionError('payload forbidden')):
            self.view()
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_malformed_unresolved_fields_are_not_hidden(self):
        key = self.failure()
        original = self.ledger.entries()
        for field, value in (('attempts', True), ('last_at', float('nan')),
                             ('pr', []), ('head', 'a' * 41), ('gate', [])):
            with self.subTest(field=field):
                data = {key: {**original[key], field: value}}
                self.ledger.path.write_text(json.dumps(data))
                self.assertEqual(self.view()['failures']['status'], 'unreadable')

    def test_one_unreadable_section_does_not_hide_other_records(self):
        self.audit()
        self.main()
        self.ledger.path.write_text('{')
        view = self.view()
        self.assertEqual(view['failures']['status'], 'unreadable')
        self.assertEqual(view['broker']['status'], 'available')
        self.assertEqual(view['main_ci']['status'], 'available')

    def test_sanitized_error_categories_and_selection(self):
        with patch.object(desktop_api, '_record_broker', side_effect=OSError('/home/private secret')):
            self.assertEqual(self.view()['broker'],
                             {'status': 'unreadable', 'reason': 'read_failed', 'items': []})
        with patch.object(desktop_api, '_loop', side_effect=ValueError('/home/private secret')):
            with self.assertRaisesRegex(desktop_api.NotFound, '^Loop configuration unavailable.$'):
                self.view()
        for value in ('', 123, 'x' * 201):
            with self.assertRaises(desktop_api.BadRequest):
                desktop_api.records_view(value)

    def test_required_checks_reject_nested_and_secret_values(self):
        self.main()
        for checks in ([None], [['nested']], ['/home/private'], ['github_pat_secret'], ['https://secret']):
            with self.subTest(checks=checks):
                self.loop['required_checks'] = checks
                self.assertEqual(self.view()['main_ci']['status'], 'unreadable')

    def test_shared_js_fixture_is_exact_real_writer_projection(self):
        self.loop.update(id='fixture-loop', repo='fixture/repo', required_checks=['tests'])
        with patch('time.time', return_value=1800000000):
            self.failure(pr=651)
            safe_push._audit(self.loop, {'repo': self.loop['repo'], 'pr': 651, 'old_head': 'a' * 40,
                                        'role': 'fixer', 'operation': 'push', 'phase': 'attempt'})
            self.main()
            actual = desktop_api.records_view('fixture-loop')
        expected = json.loads((Path(__file__).parent / 'fixtures/desktop_records.json').read_text())
        self.assertEqual(actual, expected)

    def test_real_asgi_route_reads_writer_artifacts_and_safe_refusal(self):
        require_records_prereqs(self)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        path = Path(__file__).resolve().parents[1] / 'dashboard/plugin_api.py'
        spec = importlib.util.spec_from_file_location('records_asgi_api', path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = FastAPI()
        app.include_router(module.router, prefix='/api/plugins/diaktoros')
        self.failure()
        self.audit()
        self.main()
        with TestClient(app) as client:
            reply = client.get('/api/plugins/diaktoros/records', params={'loop': 'widgets'})
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json()['failures']['items'][0]['kind'], 'timeout')
            self.assertEqual(reply.json()['main_ci']['items'][0]['required_checks'], self.loop['required_checks'])
            refused = client.get('/api/plugins/diaktoros/records', params={'loop': 'missing'})
            self.assertEqual(refused.status_code, 404)
            self.assertNotIn(str(self.root), refused.text)
            self.assertNotIn('secret', reply.text)

    def test_route_calls_records_view(self):
        require_records_prereqs(self, ('fastapi',))
        path = Path(__file__).resolve().parents[1] / 'dashboard/plugin_api.py'
        spec = importlib.util.spec_from_file_location('records_plugin_api', path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(desktop_api, 'records_view', return_value={'fixture': True}) as view:
            self.assertEqual(module.records('widgets'), {'fixture': True})
            view.assert_called_once_with('widgets')
        self.assertTrue(any(route.path == '/records' and route.methods == {'GET'}
                            for route in module.router.routes))


if __name__ == '__main__':
    unittest.main()
