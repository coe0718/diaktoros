"""Offline authorization regressions: never touch a live Hermes home or GitHub."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from diaktoros import broker_ipc, cli, config, ledger
from diaktoros.run_supervisor import Supervisor
from tests.test_fixer_push_policy import raw_loop

REPO = 'owner/one'
HEAD = 'a' * 40


class PolicyBoundaryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {'DIAKTOROS_CONFIG_DIR': str(self.root / 'cfg'),
                                   'HERMES_HOME': str(self.root / 'home')})
        env.start()
        self.addCleanup(env.stop)
        config.config_dir().mkdir()
        self.path = config.config_dir() / 'one.json'
        self.path.write_text(json.dumps(raw_loop('one')))

    def change(self, enable, loop='one'):
        from argparse import Namespace
        return cli.cmd_fixer_push(Namespace(loop=loop, enable=enable, disable=not enable,
                                             acknowledge_pr_race=True, dry_run=False))

    def production_supervisor(self):
        settings = self.root / 'production.json'
        settings.write_text('{}')
        settings.chmod(0o600)
        return Supervisor(config.home() / 'state' / 'diaktoros-runs.sqlite',
                          production_config=settings, hermes_home=self.root)

    def admitted_scope(self):
        sup = self.production_supervisor()
        with patch.object(sup, '_spawn'):
            sup.enqueue('run', 'owner/one', 3, 'a' * 40, 'fixer')
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running',launch_intent=1 WHERE delivery='run'")
        return broker_ipc.RunScope('owner/one', 3, 'a' * 40, 'fixer', 'branch',
                                   sup.get('run')['id'], str(sup.db))

    def test_disabled_enqueue_cannot_borrow_later_enable(self):
        sup = self.production_supervisor()
        with patch.object(sup, '_spawn'):
            sup.enqueue('old', 'owner/one', 3, 'a' * 40, 'fixer')
        self.assertEqual(sup.get('old')['push_admitted'], 0)
        with patch.object(cli, '_busy_seats', return_value=[]):
            self.assertEqual(self.change(True), 0)
        with patch.object(sup, '_spawn'):
            sup.enqueue('old', 'owner/one', 3, 'a' * 40, 'fixer')
        self.assertEqual(sup.get('old')['push_admitted'], 0)
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running',launch_intent=1 WHERE delivery='old'")
        scope = broker_ipc.RunScope('owner/one', 3, 'a' * 40, 'fixer', 'branch',
                                   sup.get('old')['id'], str(sup.db))
        server = broker_ipc.RunBroker(config.load_id('one'), scope, self.root)
        with patch('diaktoros.safe_push._manifest'), patch('diaktoros.safe_push.push') as push:
            with self.assertRaises(broker_ipc.ProtocolError) as caught:
                server._dispatch(json.dumps({'operation': 'push', 'manifest': {}}).encode())
        push.assert_not_called()
        # (#81) The refusal names the policy hold (the wording differs: a run admitted under
        # an earlier policy vs. one never admitted).
        self.assertIn('every write from this turn is refused', str(caught.exception))

    def test_enabled_enqueue_is_pinned_and_disable_revokes(self):
        with patch.object(cli, '_busy_seats', return_value=[]):
            self.assertEqual(self.change(True), 0)
        scope = self.admitted_scope()
        sup = Supervisor(scope.ledger_db)
        self.assertEqual(sup.get('run')['push_admitted'], 1)
        self.assertEqual(self.change(False), 0)
        server = broker_ipc.RunBroker(config.load_id('one'), scope, self.root)
        with patch('diaktoros.safe_push.push') as push:
            with self.assertRaises(broker_ipc.ProtocolError):
                server._dispatch(json.dumps({'operation': 'push', 'manifest': {}}).encode())
        push.assert_not_called()

    def test_stale_set_and_apply_snapshot_cannot_restore_disabled_policy(self):
        with patch.object(cli, '_busy_seats', return_value=[]):
            self.assertEqual(self.change(True), 0)
        stale = config.load_id('one')
        self.assertEqual(self.change(False), 0)
        cli._write_config({**stale, 'cap': 10})  # set/apply publish a stale snapshot
        self.assertFalse(config.load_id('one')['unattended_fixer_push'])
        cli._restore_config(self.path, json.dumps(stale).encode())  # apply rollback
        self.assertFalse(config.load_id('one')['unattended_fixer_push'])

    def test_symlink_id_cannot_write_another_loop(self):
        other = config.config_dir() / 'two.json'
        other.write_text(json.dumps(raw_loop('two')))
        (config.config_dir() / 'one.json').unlink()
        self.path.symlink_to(other)
        before = other.read_bytes()
        self.assertEqual(self.change(True), 2)
        self.assertEqual(self.change(False), 2)
        self.assertEqual(other.read_bytes(), before)

    def test_duplicate_repo_configs_refuse_disable_and_broker_lookup(self):
        other = raw_loop('two')
        other['repo'] = 'owner/one'
        (config.config_dir() / 'two.json').write_text(json.dumps(other))
        with self.assertRaisesRegex(config.ConfigError, 'duplicate'):
            config.by_repo('owner/one')
        self.assertEqual(self.change(True), 2)
        self.assertEqual(self.change(False), 2)
        self.assertFalse(config.load_id('one')['unattended_fixer_push'])

    def test_supervisor_in_flight_blocks_enable_even_if_gate_state_empty(self):
        db = config.home() / 'state' / 'diaktoros-runs.sqlite'
        sup = Supervisor(db)
        sup.enqueue('run', 'owner/one', 3, 'a' * 40, 'fixer')
        with ledger.connect(db) as con:
            con.execute("UPDATE runs SET state='running',launch_intent=1 WHERE delivery='run'")
        with patch.object(cli, '_busy_seats', return_value=[]):
            self.assertEqual(self.change(True), 2)
        self.assertFalse(config.load_id('one')['unattended_fixer_push'])

    def test_disable_between_initial_reload_and_push_denies_without_git(self):
        armed = {**raw_loop('one'), 'unattended_fixer_push': True}
        self.path.write_text(json.dumps(armed))
        loop = config.load_id('one')
        scope = broker_ipc.RunScope('owner/one', 3, 'a' * 40, 'fixer', 'branch')
        server = broker_ipc.RunBroker(loop, scope, self.root)
        real = config.by_repo
        calls = []
        def reload(repo):
            calls.append(repo)
            if len(calls) == 2:
                self.path.write_text(json.dumps({**armed, 'unattended_fixer_push': False}))
            return real(repo)
        with patch.object(config, 'by_repo', side_effect=reload), \
             patch('diaktoros.safe_push._manifest'), \
             patch('diaktoros.safe_push.push') as push:
            with self.assertRaises(broker_ipc.ProtocolError):
                server._dispatch(json.dumps({'operation': 'push', 'manifest': {}}).encode())
        push.assert_not_called()

    def test_broker_cannot_borrow_policy_from_other_loop_same_repo(self):
        armed = {**raw_loop('one'), 'unattended_fixer_push': True}
        self.path.write_text(json.dumps(armed))
        scope = broker_ipc.RunScope('owner/one', 3, 'a' * 40, 'fixer', 'branch')
        server = broker_ipc.RunBroker({**config.load_id('one'), 'id': 'two'}, scope, self.root)
        with patch('diaktoros.safe_push.push') as push:
            with self.assertRaises(broker_ipc.ProtocolError):
                server._dispatch(json.dumps({'operation': 'push', 'manifest': {}}).encode())
        push.assert_not_called()

    def test_disable_waits_for_in_progress_push_before_returning(self):
        armed = {**raw_loop('one'), 'unattended_fixer_push': True}
        self.path.write_text(json.dumps(armed))
        scope = self.admitted_scope()
        server = broker_ipc.RunBroker(config.load_id('one'), scope, self.root)
        writing, release, disabled = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def push(*args, **kwargs):
            writing.set()
            if not release.wait(5):
                raise AssertionError('push test timed out')
            return {'new_head': 'b' * 40}
        def dispatch():
            try:
                server._dispatch(json.dumps({'operation': 'push', 'manifest': {}}).encode())
            except Exception as exc:
                errors.append(exc)
        def disable():
            try:
                self.assertEqual(self.change(False), 0)
            except Exception as exc:
                errors.append(exc)
            finally:
                disabled.set()
        with patch('diaktoros.safe_push._manifest'), patch('diaktoros.safe_push.push', side_effect=push):
            worker = threading.Thread(target=dispatch)
            worker.start()
            operator = None
            try:
                self.assertTrue(writing.wait(5))
                operator = threading.Thread(target=disable)
                operator.start()
                self.assertFalse(disabled.wait(.1), 'disable returned during an active push')
            finally:
                release.set()
                worker.join(5)
                if operator is not None:
                    operator.join(5)
        self.assertFalse(errors, errors)
        self.assertTrue(disabled.is_set())
        self.assertFalse(config.load_id('one')['unattended_fixer_push'])

    # -- #81: every write refused ⇒ no turn launches, and the denial is never silent --------

    def test_a_push_refusal_names_the_policy_denial_not_a_spent_run(self):
        # The broker says *why* every write is refused — the same wording the run ledger and
        # the operator notice use — instead of an unrelated "must publish a confirmed push
        # first" or "run capability already used".
        self.path.write_text(json.dumps({**raw_loop('one'), 'unattended_fixer_push': True}))
        scope = self.admitted_scope()
        server = broker_ipc.RunBroker(config.load_id('one'), scope, self.root)
        self.assertEqual(self.change(False), 0)
        with patch('diaktoros.safe_push.push') as push:
            with self.assertRaisesRegex(broker_ipc.ProtocolError,
                                        'every write from this turn is refused') as caught:
                server._dispatch(json.dumps({'operation': 'push', 'manifest': {}}).encode())
        push.assert_not_called()
        self.assertIn('unattended fixer pushes were disabled', str(caught.exception))
        self.assertIn('not published', str(caught.exception))

    def test_a_fixer_review_request_when_no_write_is_left_names_the_denial(self):
        # The last write surface: a request_review whose answers comment would also be
        # refused names the policy hold instead of spending the capability first.
        self.path.write_text(json.dumps({**raw_loop('one'), 'unattended_fixer_push': True}))
        scope = self.admitted_scope()
        server = broker_ipc.RunBroker(config.load_id('one'), scope, self.root)
        self.assertEqual(self.change(False), 0)
        with patch.object(config, 'by_repo',
                          side_effect=lambda repo: config.load_id('one')), \
             patch('diaktoros.safe_push.push') as push:
            with self.assertRaisesRegex(broker_ipc.ProtocolError,
                                        'every write from this turn is refused') as caught:
                server._dispatch(json.dumps({'operation': 'request_review', 'verdict': '',
                                             'body': 'answers'}).encode())
        push.assert_not_called()
        self.assertIn('unattended fixer pushes were disabled', str(caught.exception))

    def test_policy_hold_reason_is_the_one_denial_wording(self):
        # A single source of truth: the reason the run ledger records, the broker's refusal
        # and the seat queue hold all name the same facts (#81).
        from diaktoros.run_supervisor import FIXER_NOT_ADMITTED, FIXER_PUSH_OFF, FIXER_PUSH_REVOKED
        self.path.write_text(json.dumps(raw_loop('one')))
        self.assertEqual(broker_ipc.policy_hold_reason(config.load_id('one')),
                         FIXER_PUSH_OFF)
        # A run the host cannot resolve is named ``FIXER_NOT_ADMITTED`` (#81): a redelivered
        # verdict never upgrades an unadmitted one. The ``ledger_db`` is the run-vs-gate
        # discriminator: only a run names ``FIXER_NOT_ADMITTED``, never the gate's hold.
        self.path.write_text(json.dumps({**raw_loop('one'), 'unattended_fixer_push': True}))
        with patch.object(config, 'by_repo', return_value=None):
            self.assertEqual(broker_ipc.policy_hold_reason(config.load_id('one'),
                                                           ledger_db='runs.sqlite'),
                             FIXER_NOT_ADMITTED)
        # Admitted run, current policy on: nothing is held — the broker judges the push.
        self.assertEqual(broker_ipc.policy_hold_reason(config.load_id('one')), '')


if __name__ == '__main__':
    unittest.main()
