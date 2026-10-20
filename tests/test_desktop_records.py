"""#651: bounded local records, pinned to real artifact writers (no GitHub calls)."""
import _home_guard  # noqa: F401
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from diaktoros import broker, config, desktop_api, gate_failures, gh, main_check, observer, safe_push, state


def watchdog_writer():
    path = Path(__file__).resolve().parents[1] / 'scripts/watchdog.py'
    spec = importlib.util.spec_from_file_location('records_watchdog_writer', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def notice(self, event='stall', number=12, receipt=(False, 'private secret', True), **kwargs):
        self.loop['observer'] = {'route': 'private', 'deliver': 'telegram'}
        with patch.object(observer, '_post', return_value=receipt):
            observer.notify(self.loop, state.state_for(self.loop), event, number, 'a' * 40,
                            outcome='/home/private github_pat_secret', **kwargs)

    def watch(self):
        writer = watchdog_writer()
        state.state_for(self.loop).watch_save({
            'last_run': '2027-01-15T08:00:00Z',
            'heads': {'12': {'sha': 'a' * 40, 'base': '/home/private',
                             'base_sha': 'b' * 40, 'observed_at': None,
                             'last_seen_at': 1800000000}},
            'head_history': {'12:' + 'c' * 40 + ':private:secret': {
                'sha': 'c' * 40, 'base': 'private', 'base_sha': 'd' * 40,
                'observed_at': 1799999900, 'last_seen_at': 1799999990}},
            'alerts': {writer.stall_key(12, 'a' * 40, 'reviewer never posted a verdict'): 1800000000,
                       'queue:fixer:private#12': 1800000000},
            'github_read': {'error': '/home/private secret', 'who': 'private-login'}})

    def test_missing_is_not_verified_empty(self):
        view = self.view()
        self.assertEqual(set(view), {'loop', 'repo', 'read_at', 'failures', 'broker', 'main_ci',
                                    'observer', 'watchdog'})
        for key in ('failures', 'broker', 'main_ci', 'observer', 'watchdog'):
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
        self.notice()
        self.watch()
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        with patch.object(gate_failures.Ledger, 'payload',
                          side_effect=AssertionError('payload forbidden')), \
                patch.object(observer, '_read', side_effect=AssertionError('unbounded reader forbidden')), \
                patch.object(state.LoopState, 'watch', side_effect=AssertionError('unbounded reader forbidden')):
            result = self.view()
            self.assertEqual(result['observer']['status'], 'available')
            self.assertEqual(result['watchdog']['status'], 'available')
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

    def test_observer_real_notice_receipts_and_digest_preserve_recorded_states(self):
        cases = ((False, 'private secret', True, 'uncertain'),
                 (False, 'private secret', False, 'failed'),
                 (True, '', False, 'delivered'))
        for number, (delivered, error, uncertain, status) in enumerate(cases, 1):
            self.notice(number=number, receipt=(delivered, error, uncertain))
        rows = self.view()['observer']['items']
        self.assertEqual({row['number']: row['status'] for row in rows},
                         {1: 'uncertain', 2: 'failed', 3: 'delivered'})
        self.assertTrue(all(row['attempts'] == 1 for row in rows))
        self.assertEqual(set(rows[0]), {'event', 'number', 'head', 'status', 'at', 'attempts', 'issue'})
        self.loop['observer']['digest_min'] = 10
        st = state.state_for(self.loop)
        with patch.object(observer, '_post', return_value=(False, 'private secret', True)):
            observer.notify(self.loop, st, 'triaged', 99, '', identity='private secret')
            queued = next(row for row in self.view()['observer']['items'] if row['number'] == 99)
            self.assertEqual((queued['status'], queued['attempts'], queued['issue']), ('queued', 0, True))
            observer.flush(self.loop, st)
        rows = self.view()['observer']['items']
        digest = next(row for row in rows if row['event'] == 'digest')
        self.assertEqual((digest['number'], digest['status'], digest['issue']), (None, 'uncertain', None))
        member = next(row for row in rows if row['number'] == 99)
        self.assertEqual((member['status'], member['attempts']), ('uncertain', 0))
        observer.settle(st, observer.key_for(self.loop, 1, 'a' * 40, 'stall'), delivered=True)
        self.assertEqual(next(row for row in self.view()['observer']['items']
                              if row['number'] == 1)['status'], 'delivered')
        for value in ('private', 'secret', 'message', 'summary', 'url', 'delivery', str(self.root)):
            self.assertNotIn(value, json.dumps(self.view()))

    def test_pending_notice_is_not_a_delivery_and_report_has_no_pr(self):
        self.loop['observer'] = {'route': 'private'}
        st = state.state_for(self.loop)
        with patch.object(observer, '_deliver', return_value=False):
            observer.notify(self.loop, st, 'held', 12, 'a' * 40)
        row = self.view()['observer']['items'][0]
        self.assertEqual((row['status'], row['attempts']), ('pending', 0))
        with patch.object(observer, '_post', return_value=(False, 'private secret', True)):
            observer.send_report(self.loop, st, 'private identity', 'private message')
        report = next(row for row in self.view()['observer']['items'] if row['event'] == 'report')
        self.assertEqual((report['number'], report['head'], report['status']), (0, '', 'uncertain'))

    def test_selected_loop_runtime_records_do_not_mix_repositories(self):
        self.notice()
        self.watch()
        other = {**self.loop, 'id': 'other', 'repo': 'other/repo',
                 'state_dir': str(self.root / 'other')}
        state.state_for(other).watch_save({'alerts': {'23:bbbbbbb:fixer never pushed': 1}})
        with patch.object(config, 'readable_loops', return_value=([self.loop, other], [])):
            first = desktop_api.records_view('widgets')
            second = desktop_api.records_view('other')
        self.assertEqual({row.get('pr') for row in first['watchdog']['items'] if 'pr' in row}, {12})
        self.assertEqual(second['watchdog']['items'][0]['pr'], 23)
        self.assertEqual(second['observer']['status'], 'missing')

    def test_watchdog_head_history_and_cooldown_memory_are_not_live_health(self):
        self.watch()
        rows = self.view()['watchdog']['items']
        by_kind = {row['kind']: row for row in rows}
        self.assertEqual(set(by_kind), {'sweep', 'head', 'head_history', 'stall'})
        self.assertEqual(by_kind['head'], {'kind': 'head', 'pr': 12, 'head': 'a' * 40,
                                          'observed_at': None, 'at': 1800000000})
        self.assertEqual(by_kind['head_history']['head'], 'c' * 40)
        self.assertEqual(by_kind['stall'], {'kind': 'stall', 'pr': 12, 'head7': 'a' * 7,
                                           'category': 'reviewer_missing', 'at': 1800000000})
        self.assertEqual(by_kind['sweep']['at'], 1800000000)
        for value in ('private', 'secret', 'github_read', 'base', 'healthy', str(self.root)):
            self.assertNotIn(value, json.dumps(self.view()))

    def test_watchdog_real_stall_key_categories_and_unknown_prose_are_sanitized(self):
        writer = watchdog_writer()
        kinds = {'reviewer never posted a verdict': 'reviewer_missing',
                 'fixer never pushed — private prose': 'fixer_missing',
                 'parked awaiting adjudication for 3h': 'awaiting_adjudication',
                 'adjudicating for 3h': 'adjudicating',
                 writer.PUSH_OFF_KIND: 'fixer_push_disabled',
                 '3 verdicts, no approval and NO escalation marker': 'missing_escalation',
                 'disputed at head aaaaaaa': 'disputed',
                 '/home/private github_pat_secret': 'unknown'}
        st = state.state_for(self.loop)
        for kind, category in kinds.items():
            with self.subTest(kind=kind):
                st.watch_save({'alerts': {writer.stall_key(12, 'a' * 40, kind): 10}})
                self.assertEqual(self.view()['watchdog']['items'], [
                    {'kind': 'stall', 'pr': 12, 'head7': 'a' * 7, 'category': category, 'at': 10}])

    def test_observer_watchdog_malformed_fields_fail_without_hiding_other_sections(self):
        self.audit()
        cases = {'observations.json': [[], {}, {'entries': []}, {'entries': {'private': None}},
                    {'entries': {'private': {'event': 'stall', 'number': True, 'at': 1}}}],
                 'watchdog.json': [[], {'last_run': 'private secret'}, {'last_run': True},
                    {'last_run': '2026-99-99T00:00:00Z'}, {'heads': []},
                    {'heads': {'12': None}}, {'head_history': {'secret': {}}},
                    {'heads': {'12': {'sha': 'a' * 40, 'observed_at': True, 'last_seen_at': 1}}},
                    {'head_history': {'12:secret': {'sha': 'private', 'observed_at': None,
                                                  'last_seen_at': 1}}},
                    {'alerts': []}, {'alerts': {'private secret': 1}},
                    {'alerts': {'12:aaaaaaa:secret': float('nan')}}]}
        for name, values in cases.items():
            section = 'observer' if name == 'observations.json' else 'watchdog'
            for value in values:
                with self.subTest(name=name, value=value):
                    (self.directory / name).write_text(json.dumps(value))
                    result = self.view()
                    self.assertEqual(result[section],
                                     {'status': 'unreadable', 'reason': 'malformed', 'items': []})
                    self.assertEqual(result['broker']['status'], 'available')

    def test_runtime_records_unknown_enums_are_categories_not_raw_text(self):
        self.notice()
        path = self.directory / 'observations.json'
        data = json.loads(path.read_text())
        entry = next(iter(data['entries'].values()))
        entry.update(event='/home/private secret', status='github_pat_secret')
        observer._save(path, data)
        row = self.view()['observer']['items'][0]
        self.assertEqual((row['event'], row['status']), ('unknown', 'unknown'))
        for field, value in (('at', True), ('attempts', -1), ('number', []),
                             ('head', 'private secret'), ('issue', 'false')):
            with self.subTest(field=field):
                observer._save(path, {'entries': {'private': {**entry, field: value}}})
                self.assertEqual(self.view()['observer']['reason'], 'malformed')

    def test_runtime_records_bound_counts_and_bytes(self):
        self.notice()
        path = self.directory / 'observations.json'
        entry = next(iter(json.loads(path.read_text())['entries'].values()))
        observer._save(path, {'entries': {str(n): {**entry, 'at': n} for n in range(70)}})
        self.assertEqual([row['at'] for row in self.view()['observer']['items']], list(range(69, 19, -1)))
        st = state.state_for(self.loop)
        st.watch_save({'heads': {str(n): {'sha': 'a' * 40, 'observed_at': None,
                                         'last_seen_at': n} for n in range(1, 71)}})
        self.assertEqual([row['pr'] for row in self.view()['watchdog']['items']], list(range(70, 20, -1)))
        st.watch_save({'alerts': {str(n): 1 for n in range(1001)}})
        self.assertEqual(self.view()['watchdog']['reason'], 'too_large')
        observer._save(path, {'entries': {str(n): entry for n in range(1001)}})
        self.assertEqual(self.view()['observer']['reason'], 'too_large')

    def test_runtime_records_refuse_unsafe_files_and_parent_symlinks(self):
        for name, section in (('observations.json', 'observer'), ('watchdog.json', 'watchdog')):
            path = self.directory / name
            for kind in ('fifo', 'symlink', 'directory', 'large'):
                with self.subTest(section=section, kind=kind):
                    if kind == 'fifo':
                        os.mkfifo(path)
                    elif kind == 'symlink':
                        path.symlink_to(self.root / 'not-read-secret')
                    elif kind == 'directory':
                        path.mkdir()
                    else:
                        path.write_bytes(b' ' * (desktop_api.RECORDS_BYTES + 1))
                    result = self.view()[section]
                    self.assertEqual((result['status'], result['reason']),
                                     ('unreadable', 'too_large' if kind == 'large' else 'unsafe_file'))
                    path.rmdir() if path.is_dir() else path.unlink()
        real = self.root / 'real'
        self.directory.rename(real)
        self.directory.symlink_to(real, target_is_directory=True)
        for section in ('observer', 'watchdog'):
            self.assertEqual(self.view()[section]['reason'], 'unsafe_file')

    def test_shared_js_fixture_is_exact_real_writer_projection(self):
        self.loop.update(id='fixture-loop', repo='fixture/repo', required_checks=['tests'])
        with patch('time.time', return_value=1800000000):
            self.failure(pr=651)
            safe_push._audit(self.loop, {'repo': self.loop['repo'], 'pr': 651, 'old_head': 'a' * 40,
                                        'role': 'fixer', 'operation': 'push', 'phase': 'attempt'})
            self.main()
            self.notice(number=651, receipt=(True, '', False))
            self.watch()
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
        self.notice()
        self.watch()
        with TestClient(app) as client:
            reply = client.get('/api/plugins/diaktoros/records', params={'loop': 'widgets'})
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json()['failures']['items'][0]['kind'], 'timeout')
            self.assertEqual(reply.json()['main_ci']['items'][0]['required_checks'], self.loop['required_checks'])
            self.assertEqual(reply.json()['observer']['items'][0]['status'], 'uncertain')
            self.assertEqual({row['kind'] for row in reply.json()['watchdog']['items']},
                             {'sweep', 'head', 'head_history', 'stall'})
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
