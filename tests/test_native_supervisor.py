"""Native lifecycle integration: dead workers never prove dead descendants."""
import _home_guard  # noqa: F401
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from diaktoros import native_lifecycle, run_supervisor
from diaktoros.run_supervisor import workers


class NativeSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sup = run_supervisor.Supervisor(Path(self.tmp.name) / 'runs.sqlite')
        self.sup.enqueue('delivery', 'owner/repo', 1, 'abc', 'fixer')
        self.run_id = self.sup.get('delivery')['id']
        self.update(state='running', owner='owner', pid=12345,
                    launch_intent=time.time() - 30, lease=time.time() - 10)
        self.platform = mock.patch.object(workers.sys, 'platform', 'darwin')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.gone = mock.patch.object(workers, 'process_state', return_value='gone')
        self.gone.start()
        self.addCleanup(self.gone.stop)

    def update(self, **values):
        with self.sup._connect() as con:
            con.execute('UPDATE runs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                        (*values.values(), self.run_id))

    def row(self):
        return self.sup.get('delivery')

    def test_lost_cancel_keeps_quarantine_and_capacity(self):
        self.update(cancel_requested=time.time())
        self.sup.recover()
        row = self.row()
        self.assertEqual(row['state'], 'uncertain')
        self.assertIsNotNone(row['cancel_requested'])
        self.assertFalse(self.sup.push_worker_gone(self.run_id))
        self.assertIn(row['state'], run_supervisor.ACTIVE)
        with self.assertRaisesRegex(ValueError, 'native process completion unverified'):
            self.sup.reconcile_uncertain(self.run_id, reason='worker gone',
                                         acknowledge_no_live_worker=True)
        self.assertEqual(self.row()['state'], 'uncertain')

    def test_push_cannot_settle_even_if_worker_is_gone(self):
        self.update(state='uncertain', push_intent=time.time(),
                    error=run_supervisor.PUSH_QUARANTINE + 'unknown')
        self.assertFalse(self.sup.push_worker_gone(self.run_id))
        self.assertFalse(self.sup.settle_push_quarantine(self.run_id, evidence='no push'))
        self.assertIsNotNone(self.row()['push_intent'])
        self.assertEqual(self.row()['state'], 'uncertain')

    def test_linux_lost_cancel_still_settles(self):
        self.update(cancel_requested=time.time())
        with mock.patch.object(workers.sys, 'platform', 'linux'):
            self.sup.recover()
        self.assertEqual(self.row()['state'], 'cancelled')

    def test_fixture_lost_cancel_still_settles(self):
        self.sup.fixture_mode = True
        self.update(cancel_requested=time.time())
        with mock.patch.object(self.sup, '_spawn'):
            self.sup.recover()
        self.assertEqual(self.row()['state'], 'cancelled')

    def test_native_incomplete_completion_overrides_cancel(self):
        self.update(cancel_requested=time.time())
        def fail(turn):
            raise native_lifecycle.CleanupIncomplete('all observers lost')
        with mock.patch.object(self.sup, '_production_turn', side_effect=fail), \
                mock.patch.object(self.sup, '_production_finish') as finish:
            self.sup._run_production(self.run_id, 'owner')
        turn = finish.call_args.args[0]
        self.assertFalse(turn.stopped)
        self.assertFalse(turn.retry)
        self.sup.complete_uncertain(self.run_id, 'owner', turn.rc, turn.error,
                                    stopped=turn.stopped, retry=turn.retry)
        self.assertEqual(self.row()['state'], 'uncertain')

    def test_verified_completion_can_release_native_seat(self):
        self.assertEqual(self.sup.complete_uncertain(self.run_id, 'owner', 0), 'succeeded')
        self.assertEqual(self.row()['outcome'], 0)

    def test_native_storage_admission_waits_without_spending_a_retry(self):
        from diaktoros import native_cleanup
        with mock.patch.object(self.sup, '_production_turn',
                               side_effect=native_cleanup.AdmissionRefused('another native turn is active')), \
                mock.patch.object(self.sup, '_production_finish') as finish:
            self.sup._run_production(self.run_id, 'owner')
        turn = finish.call_args.args[0]
        self.assertFalse(turn.retry)
        self.assertGreater(turn.paced_until or 0, time.time())
        self.sup.complete_uncertain(self.run_id, 'owner', turn.rc, turn.error,
                                    paced_until=turn.paced_until, retry=turn.retry)
        self.assertEqual(self.row()['state'], 'waiting')

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS process enumeration')
    def test_worker_cleanup_discovers_a_live_native_child(self):
        import subprocess
        import worker_wait
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', self.tmp.name])
        try:
            self.assertIn(child.pid, worker_wait.processes_naming(self.tmp.name))
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_cleanup_wrapper_cannot_hide_unverified_process_completion(self):
        def fail(turn):
            try:
                raise native_lifecycle.CleanupIncomplete('all observers lost')
            except native_lifecycle.CleanupIncomplete as error:
                raise RuntimeError('workspace retained') from error
        with mock.patch.object(self.sup, '_production_turn', side_effect=fail), \
                mock.patch.object(self.sup, '_production_finish') as finish:
            self.sup._run_production(self.run_id, 'owner')
        self.assertFalse(finish.call_args.args[0].stopped)
        self.assertFalse(finish.call_args.args[0].retry)

    def test_verified_observer_loss_is_not_incomplete(self):
        for exception in (native_lifecycle.GuardianLost, native_lifecycle.WatchdogLost,
                          native_lifecycle.StoragePending):
            with self.subTest(exception=exception):
                with mock.patch.object(self.sup, '_production_turn',
                                       side_effect=exception('verified drain')), \
                        mock.patch.object(self.sup, '_production_finish') as finish:
                    self.sup._run_production(self.run_id, 'owner')
                self.assertTrue(finish.call_args.args[0].stopped)

    def test_host_sweeps_only_existing_ledger_adjacent_pool(self):
        from diaktoros import native_cleanup
        pool = self.sup.db.parent / 'native-cleanup'
        with mock.patch.object(native_cleanup, 'bounded_sweep') as sweep, \
                mock.patch.object(workers, 'in_worker', return_value=False):
            self.sup.recover()
            sweep.assert_not_called()
            pool.mkdir()
            sweep.return_value = {'status': 'timeout', 'results': []}
            self.sup.recover()
            sweep.assert_called_once_with(pool.resolve())
        self.assertEqual(self.row()['state'], 'uncertain')
        with mock.patch.object(native_cleanup, 'bounded_sweep') as sweep, \
                mock.patch.object(workers, 'in_worker', return_value=True):
            self.sup.recover()
            sweep.assert_not_called()

    def test_host_sweeps_both_exact_pools_even_after_one_fails(self):
        from diaktoros import native_cleanup
        pools = [self.sup.db.parent / name for name in
                 ('native-cleanup', 'native-proof-cleanup')]
        for pool in pools:
            pool.mkdir()
        (self.sup.db.parent / 'unrelated-cleanup').mkdir()
        completed = {'status': 'completed', 'results': []}
        with mock.patch.object(native_cleanup, 'bounded_sweep',
                               side_effect=[OSError('main held'), completed]) as sweep, \
                mock.patch.object(workers, 'in_worker', return_value=False), \
                mock.patch.object(workers.util, 'log'):
            self.sup.recover()
        self.assertEqual(sweep.call_args_list, [mock.call(pool.resolve()) for pool in pools])
        self.assertEqual(self.sup.native_cleanup_results['status'], 'unverified')
        self.assertEqual(self.sup.native_proof_cleanup_results, completed)
        self.assertEqual(self.row()['state'], 'uncertain')

    def test_sweep_failure_never_releases_quarantine(self):
        from diaktoros import native_cleanup
        (self.sup.db.parent / 'native-cleanup').mkdir()
        with mock.patch.object(native_cleanup, 'bounded_sweep', side_effect=OSError('unreadable')), \
                mock.patch.object(workers, 'in_worker', return_value=False), \
                mock.patch.object(workers.util, 'log'):
            self.sup.recover()
        self.assertEqual(self.row()['state'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
