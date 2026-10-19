"""Desktop action boundary: exact targets, bounded errors and shared host operations."""
import _home_guard  # noqa: F401
import importlib.util
import json
import pathlib
import unittest
from unittest import mock

from diaktoros import desktop_api, one_shot_action as actions

LOOP = {'id': 'one', 'repo': 'acme/widgets', 'tokens': {'reader': '/private/PAT'}}
HEAD = 'a' * 40
TOKEN = 'b' * 64


class Boundary(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(desktop_api, '_loop', return_value=LOOP)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_preview_uses_shared_owner_and_allowlists_response(self):
        proposal = dict(action='escalate', pr=7, seat=None, eligible=True, head=HEAD,
                        reason='parks this head and requests an adjudicator ruling', confirmation=TOKEN,
                        credential='/private/PAT')
        with mock.patch.object(actions, 'plan', return_value=proposal) as plan:
            result = desktop_api.action_view('one', 'escalate', 7, None)
        plan.assert_called_once_with(LOOP, 'escalate', pr=7, seat=None)
        self.assertEqual(result['confirmation'], TOKEN)
        self.assertEqual(result['repo'], LOOP['repo'])
        self.assertNotIn('/private', json.dumps(result))
        self.assertGreater(result['read_at'], 0)

    def test_post_forwards_exact_confirmation_to_shared_operation(self):
        with mock.patch.object(actions, 'confirmed_perform', return_value={
                'ok': True, 'status': 'verified', 'reason': 'head parked', 'head': HEAD}) as perform:
            result = desktop_api.action_perform_view('one', 'escalate', 7, None, TOKEN)
        perform.assert_called_once_with(LOOP, 'escalate', TOKEN, pr=7, seat=None)
        self.assertTrue(result['ok'])

    def test_stale_is_a_conflict_not_retargeted(self):
        with mock.patch.object(actions, 'confirmed_perform', return_value={
                'ok': False, 'status': 'stale', 'reason': 'preview again', 'head': ''}):
            with self.assertRaises(desktop_api.Conflict):
                desktop_api.action_perform_view('one', 'escalate', 7, None, TOKEN)

    def test_partial_and_unknown_are_not_success(self):
        for status in ('partial', 'unknown'):
            with self.subTest(status=status), mock.patch.object(actions, 'confirmed_perform', return_value={
                    'ok': False, 'status': status, 'reason': 'inspect before retrying', 'head': HEAD}):
                result = desktop_api.action_perform_view('one', 'escalate', 7, None, TOKEN)
            self.assertFalse(result['ok'])
            self.assertEqual(result['status'], status)

    def test_exception_is_bounded_and_never_echoes_transport(self):
        for name, args in [('action_view', ('one', 'review', 7, None)),
                           ('action_perform_view', ('one', 'escalate', 7, None, TOKEN))]:
            owner = 'plan' if name == 'action_view' else 'confirmed_perform'
            with self.subTest(name=name), mock.patch.object(actions, owner,
                    side_effect=RuntimeError('/private/PAT ghp_credential\n' * 100)):
                with self.assertRaises(desktop_api.Conflict) as caught:
                    getattr(desktop_api, name)(*args)
            self.assertLess(len(str(caught.exception)), 200)
            self.assertNotIn('/private', str(caught.exception))
            self.assertNotIn('credential', str(caught.exception))

    def test_malformed_selection_never_reaches_owner(self):
        bad = [('merge', 7, None), ('review', True, None), ('review', 0, None),
               ('review', '7', None), ('escalate', 7, 'fixer'), ('drain', 7, 'reviewer'),
               ('drain', None, 'adjudicator'), ('drain', None, None)]
        with mock.patch.object(actions, 'plan') as plan:
            for action, pr, seat in bad:
                with self.subTest(action=action, pr=pr, seat=seat), self.assertRaises(desktop_api.BadRequest):
                    desktop_api.action_view('one', action, pr, seat)
        plan.assert_not_called()

    def test_malformed_confirmation_is_not_normalized(self):
        with mock.patch.object(actions, 'confirmed_perform') as perform:
            for token in (None, '', ' ' + TOKEN, TOKEN.upper(), 'c' * 63, 12):
                # Uppercase B is invalid; valid lowercase hex remains exact.
                with self.subTest(token=token), self.assertRaises(desktop_api.BadRequest):
                    desktop_api.action_perform_view('one', 'escalate', 7, None, token)
        perform.assert_not_called()

    def test_loop_failure_is_safe(self):
        with mock.patch.object(desktop_api, '_loop', side_effect=RuntimeError('/private/PAT')):
            with self.assertRaises(desktop_api.Conflict) as caught:
                desktop_api.action_view('one', 'review', 7, None)
        self.assertNotIn('/private', str(caught.exception))

    def test_all_read_only_refusals_retain_backend_reason(self):
        for action, pr, seat in [('review', 7, None), ('drain', None, 'reviewer'), ('drain', None, 'fixer')]:
            with self.subTest(action=action, seat=seat), mock.patch.object(actions.gh, 'pr', return_value={'number': 7, 'head': {'sha': HEAD}}):
                result = desktop_api.action_view('one', action, pr, seat)
                self.assertFalse(result['eligible'])
                self.assertEqual(result['confirmation'], '')
                self.assertIn('snapshot', result['reason'])


try:
    import fastapi  # noqa: F401
except ImportError:
    fastapi = None


@unittest.skipUnless(fastapi, 'FastAPI is not installed')
class Routes(unittest.TestCase):
    def setUp(self):
        path = pathlib.Path(__file__).resolve().parents[1] / 'dashboard/plugin_api.py'
        spec = importlib.util.spec_from_file_location('desktop_actions_route_test', path)
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)

    def test_post_exact_body_shape(self):
        good = dict(loop='one', action='escalate', pr=7, seat=None, confirmation=TOKEN)
        with mock.patch.object(desktop_api, 'action_perform_view', return_value={'ok': False}) as perform:
            for body in (None, [], {}, {**good, 'token': 'secret'}, {k: v for k, v in good.items() if k != 'seat'}):
                with self.subTest(body=body), self.assertRaises(self.api.HTTPException) as caught:
                    self.api.actions_perform(body)
                self.assertEqual(caught.exception.status_code, 422)
            perform.assert_not_called()
            self.api.actions_perform(good)
            perform.assert_called_once_with('one', 'escalate', 7, None, TOKEN)

    def test_get_parses_only_pr_and_keeps_backend_validation(self):
        with mock.patch.object(desktop_api, 'action_view', return_value={}) as view:
            self.api.actions(loop='one', action='review', pr='7', seat=None)
        view.assert_called_once_with('one', 'review', 7, None)
        with self.assertRaises(self.api.HTTPException):
            self.api.actions(loop='one', action='review', pr='oops', seat=None)
