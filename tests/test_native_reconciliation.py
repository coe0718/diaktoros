"""Manual native reconciliation is an attestation, never automatic exit proof."""
import _home_guard  # noqa: F401
import contextlib
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from diaktoros import run_supervisor
from diaktoros.run_supervisor import workers


class NativeReconciliationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.sup = run_supervisor.Supervisor(Path(temp.name) / 'runs.sqlite')
        self.sup.enqueue('delivery', 'owner/repo', 1, 'abc', 'fixer')
        row = self.row()
        assert row is not None
        self.run_id = row['id']
        self.update(state='uncertain', pid=12345, launch_intent=time.time() - 30,
                    lease=0, push_intent=time.time(),
                    error=run_supervisor.PUSH_QUARANTINE + 'unknown')
        platform = mock.patch.object(workers.sys, 'platform', 'darwin')
        platform.start()
        self.addCleanup(platform.stop)
        process = mock.patch.object(workers, 'process_state', return_value='gone')
        self.process = process.start()
        self.addCleanup(process.stop)

    def update(self, **values):
        with self.sup._connect() as con:
            con.execute('UPDATE runs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                        (*values.values(), self.run_id))

    def row(self):
        row = self.sup.get('delivery')
        assert row is not None
        return row

    def test_explicit_attestations_release_with_distinct_audit_reason(self):
        with mock.patch.object(workers, '_release_claim') as release:
            self.assertTrue(self.sup.reconcile_uncertain(
                self.run_id, reason='external writes inspected; host rebooted',
                acknowledge_no_live_worker=True, acknowledge_native_descendants_stopped=True))
        row = self.row()
        assert row is not None
        self.assertEqual(row['state'], 'failed')
        self.assertIsNone(row['lease'])
        self.assertIsNone(row['push_intent'])
        self.assertEqual(row['error'], 'operator reconciliation: external writes inspected; '
                         'host rebooted; native descendants manually acknowledged stopped')
        self.process.assert_called_once_with(12345, row['launch_intent'])
        self.assertEqual(release.call_args.args[1], self.run_id)
        with self.assertRaisesRegex(ValueError, 'refused'):
            self.sup.retry(self.run_id)

    def test_both_attestations_and_valid_reason_are_required(self):
        before = self.row()
        for options in ({}, {'acknowledge_no_live_worker': True},
                        {'acknowledge_native_descendants_stopped': True},
                        {'acknowledge_no_live_worker': True,
                         'acknowledge_native_descendants_stopped': True, 'reason': ''},
                        {'acknowledge_no_live_worker': True,
                         'acknowledge_native_descendants_stopped': True, 'reason': 'x' * 513}):
            with self.subTest(options=options), mock.patch.object(workers, '_release_claim') as release:
                with self.assertRaises(ValueError):
                    self.sup.reconcile_uncertain(self.run_id, **{'reason': 'inspected', **options})
                self.assertEqual(self.row(), before)
                release.assert_not_called()

    def test_attestations_do_not_override_live_or_unknown_worker(self):
        before = self.row()
        for state in ('alive', 'unknown'):
            with self.subTest(state=state), mock.patch.object(workers, '_release_claim') as release:
                self.process.return_value = state
                with self.assertRaises(ValueError):
                    self.sup.reconcile_uncertain(
                        self.run_id, reason='inspected', acknowledge_no_live_worker=True,
                        acknowledge_native_descendants_stopped=True)
                self.assertEqual(self.row(), before)
                release.assert_not_called()

    def test_attestations_do_not_override_missing_launch_identity(self):
        self.update(launch_intent=None)
        with self.assertRaisesRegex(ValueError, 'missing launch intent'):
            self.sup.reconcile_uncertain(
                self.run_id, reason='inspected', acknowledge_no_live_worker=True,
                acknowledge_native_descendants_stopped=True)
        self.assertEqual(self.row()['state'], 'uncertain')

    def test_unrecorded_pid_still_requires_explicit_attestations(self):
        self.update(pid=None)
        with self.assertRaises(ValueError):
            self.sup.reconcile_uncertain(self.run_id, reason='inspected',
                                         acknowledge_no_live_worker=True)
        self.assertTrue(self.sup.reconcile_uncertain(
            self.run_id, reason='inspected', acknowledge_no_live_worker=True,
            acknowledge_native_descendants_stopped=True))
        self.process.assert_not_called()

    def test_repeated_and_non_uncertain_reconciliation_do_not_release_claims(self):
        self.assertTrue(self.sup.reconcile_uncertain(
            self.run_id, reason='inspected', acknowledge_no_live_worker=True,
            acknowledge_native_descendants_stopped=True))
        before = self.row()
        with mock.patch.object(workers, '_release_claim') as release:
            for run_id in (self.run_id, 'missing'):
                self.assertFalse(self.sup.reconcile_uncertain(
                    run_id, reason='again', acknowledge_no_live_worker=True,
                    acknowledge_native_descendants_stopped=True))
            self.update(state='running')
            self.assertFalse(self.sup.reconcile_uncertain(
                self.run_id, reason='not uncertain', acknowledge_no_live_worker=True,
                acknowledge_native_descendants_stopped=True))
            release.assert_not_called()
        self.assertEqual(self.row()['error'], before['error'])

    def test_linux_and_fixture_keep_existing_acknowledgement_contract(self):
        for platform, fixture in (('linux', False), ('darwin', True)):
            with self.subTest(platform=platform, fixture=fixture):
                self.update(state='uncertain')
                self.sup.fixture_mode = fixture
                with mock.patch.object(workers.sys, 'platform', platform):
                    self.assertTrue(self.sup.reconcile_uncertain(
                        self.run_id, reason='inspected', acknowledge_no_live_worker=True))
                self.assertEqual(self.row()['error'], 'operator reconciliation: inspected')

    def test_automatic_native_release_stays_fail_closed(self):
        self.update(cancel_requested=time.time(), lease=0)
        self.sup.recover()
        self.assertEqual(self.row()['state'], 'uncertain')
        self.assertFalse(self.sup.push_worker_gone(self.run_id))
        self.assertFalse(self.sup.settle_push_quarantine(self.run_id, evidence='no push'))
        self.assertIsNotNone(self.row()['push_intent'])

    def test_cli_omission_does_not_supply_native_attestation(self):
        argv = ['run_supervisor', 'reconcile', str(self.sup.db), self.run_id,
                '--reason', 'inspected', '--acknowledge-no-live-worker']
        with mock.patch.object(sys, 'argv', argv), self.assertRaisesRegex(
                ValueError, 'native process completion unverified'):
            run_supervisor.main()
        self.assertEqual(self.row()['state'], 'uncertain')

    def test_cli_native_attestation_does_not_replace_worker_acknowledgement(self):
        argv = ['run_supervisor', 'reconcile', str(self.sup.db), self.run_id,
                '--reason', 'inspected', '--acknowledge-native-descendants-stopped']
        with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            run_supervisor.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.row()['state'], 'uncertain')

    def test_cli_passes_both_attestations(self):
        argv = ['run_supervisor', 'reconcile', str(self.sup.db), self.run_id,
                '--reason', 'external writes inspected; host rebooted',
                '--acknowledge-no-live-worker', '--acknowledge-native-descendants-stopped']
        with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()) as out:
            run_supervisor.main()
        self.assertEqual(out.getvalue().strip(), 'reconciled')
        row = self.row()
        assert row is not None
        self.assertIn('native descendants manually acknowledged stopped', row['error'])

    def test_cli_rejects_native_attestation_for_other_operations(self):
        for operation in ('status', 'sweep', 'rulings', 'retry',
                          '_fixture-worker', '_production-worker'):
            with self.subTest(operation=operation):
                argv = ['run_supervisor', operation, str(self.sup.db),
                        '--acknowledge-native-descendants-stopped']
                with mock.patch.object(sys, 'argv', argv), \
                        contextlib.redirect_stderr(io.StringIO()) as err, \
                        self.assertRaises(SystemExit) as raised:
                    run_supervisor.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn('only valid for reconcile', err.getvalue())


if __name__ == '__main__':
    unittest.main()
