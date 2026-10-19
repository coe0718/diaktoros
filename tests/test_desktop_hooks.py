"""#645: real loop/registry fixtures, fake GitHub only; never touch installed hooks."""
import _home_guard  # noqa: F401
import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from diaktoros import arm_action, cli, config, desktop_api, gh
from diaktoros.cli import common
import test_arm_verify as arm_fixture


class HookDisclosures(unittest.TestCase):
    def test_shipped_disclosures_name_hook_pause_resume(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('dashboard/manifest.json', 'catalog/diaktoros.yaml', 'plugin.yaml',
                     'docs/security.md', 'docs/operations.md', 'docs/development.md'):
            with self.subTest(name=name):
                text = (root / name).read_text().lower()
                self.assertIn('pause', text)
                self.assertIn('resume', text)
        security = (root / 'docs/security.md').read_text()
        self.assertIn('POST /hooks', security)
        self.assertIn('sweeps may still recover events', security.lower())
        self.assertIn('not running turns', security)


class HooksFixture(unittest.TestCase):
    root: Path
    setUp = arm_fixture.ArmVerifyTests.setUp
    write_registry = arm_fixture.ArmVerifyTests.write_registry

    def fake(self, active=True, **kwargs):
        fake = arm_fixture.FakeGitHub(arm_fixture.hooks(active), **kwargs)
        patcher = patch.object(gh, 'fetch', fake.fetch)
        patcher.start()
        self.addCleanup(patcher.stop)
        ping = patch.object(common, '_ping_loop_hooks', return_value=True)
        self.ping = ping.start()
        self.addCleanup(ping.stop)
        return fake

    def view(self):
        return desktop_api.hooks_view('widgets')

    def act(self, view, action='pause'):
        return desktop_api.hooks_action_view('widgets', action, view['confirmation'])

    def assert_no_writes(self, fake):
        self.assertFalse([call for call in fake.calls if call[0] != 'GET'], fake.calls)


class HooksTests(HooksFixture):
    def test_armed_read_contract_is_read_only_and_opaque(self):
        fake = self.fake()
        view = self.view()
        self.assertEqual(set(view), {'loop', 'repo', 'state', 'eligible', 'confirmation',
                                     'reason', 'read_at'})
        self.assertEqual((view['state'], view['eligible']), ('armed', True))
        self.assertRegex(view['confirmation'], r'^[0-9a-f]{64}$')
        self.assertIsInstance(view['read_at'], float)
        self.assertEqual(view['confirmation'], self.view()['confirmation'])
        self.assert_no_writes(fake)
        self.ping.assert_not_called()
        text = json.dumps(view)
        for secret in ('fixture-secret', 'https://', str(self.root), 'reader', 'token_file'):
            self.assertNotIn(secret, text)

    def test_pause_resume_reads_back_and_preserves_foreign_hook(self):
        fake = self.fake()
        paused = self.act(self.view())
        self.assertTrue(paused['ok'])
        self.assertEqual(paused['state'], 'paused')
        self.assertTrue(paused['eligible'])
        self.ping.assert_not_called()
        self.assertTrue(fake.hooks[3]['active'])
        resumed = self.act(paused, 'resume')
        self.assertTrue(resumed['ok'])
        self.assertEqual(resumed['state'], 'armed')
        self.ping.assert_called_once()
        self.assertTrue(fake.hooks[3]['active'])
        self.assertEqual({login for _, _, login in fake.calls}, {'reader'})

    def test_unknown_never_offers_a_control(self):
        cases = ('mixed', 'missing', 'empty', 'malformed', 'unreadable', 'bad_listing',
                 'bad_event', 'slashed', 'foreign')
        for case in cases:
            with self.subTest(case=case):
                fake = self.fake()
                if case == 'mixed':
                    fake.hooks[1]['active'] = False
                elif case == 'missing':
                    del fake.hooks[1]
                elif case == 'empty':
                    fake.hooks.clear()
                elif case == 'malformed':
                    fake.hooks[1]['active'] = 'true'
                elif case == 'unreadable':
                    fake.list_error = 'HTTP 403 /home/private/PAT ghp_SECRET'
                elif case == 'bad_listing':
                    fake.hooks[1]['config'] = {}
                elif case == 'bad_event':
                    fake.hooks[1]['events'] = ['push']
                elif case == 'slashed':
                    fake.hooks[1]['config']['url'] += '/'
                elif case == 'foreign':
                    fake.hooks[1]['config']['url'] = 'https://elsewhere/p/reviewer/webhooks/widgets-review'
                view = self.view()
                self.assertEqual(view['state'], 'unknown')
                self.assertFalse(view['eligible'])
                self.assertIsNone(view['confirmation'])
                self.assertNotIn('ghp_SECRET', json.dumps(view))
                self.assert_no_writes(fake)

    def test_paused_but_miswired_is_not_resumable(self):
        fake = self.fake(False)
        fake.hooks[1]['events'] = ['push']
        view = self.view()
        self.assertEqual(view['state'], 'paused')
        self.assertFalse(view['eligible'])
        self.assertIsNone(view['confirmation'])
        self.assert_no_writes(fake)

    def test_confirmation_binds_config_hook_ids_state_events_url_and_registry(self):
        for change in ('repo', 'config', 'id', 'state', 'events', 'url', 'registry'):
            with self.subTest(change=change):
                fake = self.fake()
                original = (config.config_dir() / 'widgets.json').read_text()
                self.write_registry({'widgets-review': 'reviewer', 'widgets-fix': 'fixer'})
                before = self.view()
                if change in ('repo', 'config'):
                    loop = json.loads(original)
                    loop['repo' if change == 'repo' else 'cap'] = 'owner/changed' if change == 'repo' else 42
                    (config.config_dir() / 'widgets.json').write_text(json.dumps(loop))
                elif change == 'id':
                    fake.hooks[10] = fake.hooks.pop(1)
                    fake.hooks[10]['id'] = 10
                elif change == 'state':
                    fake.hooks[1]['active'] = False
                elif change == 'events':
                    fake.hooks[1]['events'].append('push')
                elif change == 'url':
                    fake.hooks[1]['config']['url'] += '?new=1'
                else:
                    self.write_registry({'widgets-review': 'other', 'widgets-fix': 'fixer'})
                with self.assertRaises(desktop_api.Conflict):
                    self.act(before)
                self.assert_no_writes(fake)
                (config.config_dir() / 'widgets.json').write_text(original)

    def test_deliveries_and_foreign_hooks_do_not_invalidate_identity(self):
        fake = self.fake()
        before = self.view()
        fake.hooks[1]['last_response'] = {'code': 200, 'message': 'new delivery'}
        fake.hooks[1]['updated_at'] = 'tomorrow'
        fake.hooks[3]['active'] = False
        self.assertEqual(before['confirmation'], self.view()['confirmation'])
        self.assertTrue(self.act(before)['ok'])

    def test_unknown_reread_refuses_a_previously_valid_confirmation(self):
        fake = self.fake()
        before = self.view()
        fake.list_error = 'HTTP 403 /private/PAT'
        with self.assertRaises(desktop_api.Conflict):
            self.act(before)
        self.assert_no_writes(fake)

    def test_success_from_operation_is_not_enough_without_final_readback(self):
        self.fake()
        before = self.view()
        with patch.object(arm_action, 'perform', return_value=([], True)):
            result = self.act(before)
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'armed')
        self.assertFalse(result['eligible'])

    def test_identity_failure_is_unknown_and_safe(self):
        self.fake()
        with patch.object(arm_action, '_identity', side_effect=RuntimeError('/private/PAT')):
            view = self.view()
        self.assertEqual(view['state'], 'unknown')
        self.assertFalse(view['eligible'])
        self.assertIsNone(view['confirmation'])
        self.assertNotIn('/private/PAT', json.dumps(view))

    def test_read_does_not_create_files_and_timestamp_follows_read(self):
        self.fake()
        before = sorted(str(p) for p in self.root.rglob('*'))
        with patch.object(arm_action.time, 'time', side_effect=[10.0, 20.0]):
            view = self.view()
        self.assertEqual(view['read_at'], 20.0)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob('*')))

    def test_confirmation_for_other_loop_or_action_cannot_write(self):
        fake = self.fake()
        view = self.view()
        with self.assertRaises(desktop_api.Conflict):
            self.act(view, 'resume')
        with self.assertRaises(desktop_api.Conflict):
            desktop_api.hooks_action_view('absent', 'pause', view['confirmation'])
        with self.assertRaises(desktop_api.Conflict):
            self.act(dict(view, confirmation='0' * 64))
        self.assert_no_writes(fake)

    def test_partial_write_is_unknown_and_no_next_control(self):
        fake = self.fake(patch_error={2: 'HTTP 403 private /home/PAT ghp_SECRET'})
        result = self.act(self.view())
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'unknown')
        self.assertFalse(result['eligible'])
        self.assertIsNone(result['confirmation'])
        self.assertFalse(fake.hooks[1]['active'])
        self.assertTrue(fake.hooks[2]['active'])
        self.assertNotIn('ghp_SECRET', json.dumps(result))
        self.ping.assert_not_called()

    def test_refused_or_ignored_patch_reports_observed_armed_not_success(self):
        for opts in ({'patch_error': 'HTTP 403 /private/PAT'}, {'ignore_patch': True}):
            with self.subTest(opts=opts):
                fake = self.fake(**opts)
                result = self.act(self.view())
                self.assertFalse(result['ok'])
                self.assertEqual(result['state'], 'armed')
                self.assertFalse(result['eligible'])
                self.assertIsNone(result['confirmation'])
                self.assertNotIn('/private/PAT', json.dumps(result))

    def test_unreadable_individual_readback_never_claims_success(self):
        self.fake(get_error='HTTP 502 /private/PAT')
        result = self.act(self.view())
        self.assertFalse(result['ok'])
        # A later complete listing can establish state, not success of the whole action.
        self.assertEqual(result['state'], 'paused')
        self.assertFalse(result['eligible'])

    def test_post_write_listing_failure_is_unknown(self):
        fake = self.fake()
        before = self.view()
        original = fake.fetch
        def fetch(loop, path, method='GET', body=None, login=None):
            answer = original(loop, path, method=method, body=body, login=login)
            if method == 'PATCH':
                fake.list_error = 'HTTP 502 /private/PAT'
            return answer
        with patch.object(gh, 'fetch', fetch):
            result = self.act(before)
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'unknown')
        self.assertFalse(result['eligible'])

    def test_lost_patch_response_but_confirmed_readback_is_success(self):
        fake = self.fake()
        original = fake.fetch
        def fetch(loop, path, method='GET', body=None, login=None):
            answer = original(loop, path, method=method, body=body, login=login)
            return (None, 'timeout') if method == 'PATCH' else answer
        with patch.object(gh, 'fetch', fetch):
            result = self.act(self.view())
        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'paused')

    def test_exception_after_partial_write_still_reads_actual_state(self):
        fake = self.fake()
        before = self.view()
        def broken(*args, **kwargs):
            fake.hooks[1]['active'] = False
            raise RuntimeError('/private/PAT ghp_SECRET')
        with patch.object(arm_action, 'perform', side_effect=broken):
            result = self.act(before)
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('ghp_SECRET', json.dumps(result))

    def test_ping_failure_does_not_claim_resumed_success(self):
        self.fake(False)
        self.ping.return_value = False
        result = self.act(self.view(), 'resume')
        self.assertFalse(result['ok'])
        self.assertEqual(result['state'], 'armed')
        self.assertFalse(result['eligible'])

    def test_cli_and_desktop_use_the_same_host_operation(self):
        self.fake()
        owner = arm_action.perform
        with patch.object(arm_action, 'perform', wraps=owner) as perform:
            result = self.act(self.view())
            self.assertTrue(result['ok'])
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cli.cmd_arm(argparse.Namespace(loop='widgets', pause=False, admin_token='owner'))
            self.assertEqual(rc, 0)
        self.assertEqual(perform.call_count, 2)
        self.assertEqual(perform.call_args_list[1].args[2], 'owner')
        self.assertFalse(perform.call_args_list[0].args[1])
        self.assertTrue(perform.call_args_list[1].args[1])

    def test_invalid_inputs_are_bounded_and_never_write(self):
        fake = self.fake()
        for action, confirmation in [('bogus', '0' * 64), ([], '0' * 64),
                                     ('pause', ''), ('pause', {}), ('pause', 'x' * 10000)]:
            with self.subTest(action=action, confirmation=str(confirmation)[:10]):
                with self.assertRaises(desktop_api.HookRequest) as caught:
                    desktop_api.hooks_action_view('widgets', action, confirmation)
                self.assertLess(len(str(caught.exception)), 100)
        self.assert_no_writes(fake)


@unittest.skipUnless(importlib.util.find_spec('fastapi'), 'fastapi is not installed here')
class HttpTests(HooksFixture):
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        path = Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py'
        spec = importlib.util.spec_from_file_location('dk_hooks_plugin_api', path)
        assert spec is not None and spec.loader is not None
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        app = FastAPI()
        app.include_router(api.router, prefix='/api/plugins/diaktoros')
        return TestClient(app)

    def test_http_contract_roundtrip_and_replayed_confirmation(self):
        self.fake()
        client = self.client()
        path = '/api/plugins/diaktoros/hooks'
        before = client.get(path, params={'loop': 'widgets'})
        self.assertEqual(before.status_code, 200, before.text)
        payload = dict(loop='widgets', action='pause', confirmation=before.json()['confirmation'])
        done = client.post(path, json=payload)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertTrue(done.json()['ok'])
        self.assertEqual(done.json()['state'], 'paused')
        self.assertEqual(set(done.json()), set(before.json()) | {'ok', 'answer'})
        stale = client.post(path, json=payload)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertLess(len(stale.text), 200)
        self.assertEqual(client.post('/hooks', json=payload).status_code, 404)

    def test_http_malformed_and_unavailable_are_safe_422_or_409(self):
        fake = self.fake()
        client = self.client()
        path = '/api/plugins/diaktoros/hooks'
        for payload in (None, [], 'x' * 10000, {}, {'loop': []},
                        {'loop': 'widgets', 'action': 'pause', 'confirmation': {}},
                        {'loop': 'widgets', 'action': 'pause', 'confirmation': '0' * 64,
                         'admin_token': '/private/PAT'}):
            response = client.post(path, json=payload)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertLess(len(response.text), 200)
            self.assertNotIn('/private/PAT', response.text)
        with patch.object(config, 'readable_loops', side_effect=RuntimeError('/private/PAT ghp_SECRET')):
            response = client.post(path, json={'loop': 'widgets', 'action': 'pause',
                                               'confirmation': '0' * 64})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('ghp_SECRET', response.text)
        self.assert_no_writes(fake)



