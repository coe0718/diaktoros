"""Eventual disposal never substitutes for verified process completion."""
import _home_guard  # noqa: F401
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from diaktoros import native_cleanup, native_lifecycle, native_recovery, native_storage, run_supervisor


class CleanupPoolTests(unittest.TestCase):
    def pool(self, directory, **limits):
        return native_cleanup.Manager(Path(directory).resolve() / 'native-cleanup',
                                      policy=native_cleanup.Policy(**limits))

    def workspace(self, pool, *, drained=True):
        w = native_storage.Workspace(64, cleanup_manager=pool)
        r = pool.prepare(w)
        w.image.write_bytes(b'fixture image')
        w.image.chmod(0o600)
        w.mount.mkdir(mode=0o700)
        r.update('created', image_identity=native_recovery._identity(w.image.lstat()))
        r.update('attached')
        r.update('armed')
        r.update('running')
        if drained:
            r.update('drained')
        r.close()
        return w

    def fail_eject(self):
        return mock.patch.object(native_storage.Workspace, '_detach_owned',
                                 side_effect=native_storage.StorageError('Resource busy'))

    def successful_eject(self):
        return mock.patch.object(native_storage.Workspace, '_devices', return_value=[])

    def test_busy_disposal_is_durable_before_context_completion_and_sweep_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = self.pool(directory)
            w = self.workspace(pool)
            with self.fail_eject():
                w._cleanup()
            self.assertEqual(w.pending_cleanup['status'], 'pending')
            with native_recovery.Record(w.root) as r:
                record = r.read()
                self.assertEqual(record['state'], 'drained')
                self.assertTrue(record['cleanup_pending'])
                self.assertEqual(record['cleanup_attempts'], 1)
            fresh = native_cleanup.Manager(pool.root, create=False)
            with mock.patch.object(native_storage.Workspace, '_detach_owned'), \
                    mock.patch.object(native_storage.Workspace, '_verify_detached'):
                self.assertEqual(fresh.sweep()['results'][0]['status'], 'removed')
            self.assertFalse(w.root.exists())

    def test_queue_write_failure_prevents_acknowledging_deferred_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            w = self.workspace(self.pool(directory))
            with self.fail_eject(), mock.patch.object(native_recovery.Record, 'write', side_effect=OSError('disk full')):
                with self.assertRaises(native_storage.StorageError):
                    w._cleanup()
            self.assertTrue(w.image.exists())

    def test_unverified_completion_cannot_enter_pending_disposal_or_admit_another_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = self.pool(directory)
            w = self.workspace(pool, drained=False)
            with self.fail_eject() as detach:
                with self.assertRaises(native_storage.StorageError):
                    w._cleanup()
            detach.assert_not_called()
            with self.assertRaises(native_cleanup.AdmissionRefused):
                pool.prepare(native_storage.Workspace(64, cleanup_manager=pool))
            self.assertEqual(len(list(pool.volumes.iterdir())), 1)

    def test_full_disk_refuses_before_workspace_or_image_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = self.pool(directory)
            w = native_storage.Workspace(64, cleanup_manager=pool)
            with mock.patch.object(native_cleanup.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
                with self.assertRaises(native_cleanup.AdmissionRefused):
                    pool.prepare(w)
                self.assertTrue(pool.status()['paused'])
            self.assertIsNone(w.root)
            self.assertEqual(list(pool.volumes.iterdir()), [])

    def test_pending_count_limit_and_notices_survive_restart_without_repetition(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = self.pool(directory, max_pending=1)
            w = self.workspace(pool)
            with self.fail_eject():
                w._cleanup()
                messages = []
                pool.sweep(notify=messages.append)
                pool.sweep(notify=messages.append)
            self.assertEqual(len(messages), 1)
            self.assertIn('native turns paused', messages[0])
            self.assertIn('Resource busy', messages[0])
            with self.assertRaises(native_cleanup.AdmissionRefused):
                pool.prepare(native_storage.Workspace(64, cleanup_manager=pool))
            fresh = native_cleanup.Manager(pool.root, create=False)
            with mock.patch.object(native_storage.Workspace, '_detach_owned'), \
                    mock.patch.object(native_storage.Workspace, '_verify_detached'):
                fresh.sweep(notify=messages.append)
                fresh.sweep(notify=messages.append)
            self.assertEqual(len(messages), 2)
            self.assertIn('native turns resumed', messages[1])

    def test_sweep_rotates_bounded_retries_past_a_busy_image(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = self.pool(directory, sweep_limit=1)
            a = self.workspace(pool)
            with self.fail_eject():
                a._cleanup()
                b = self.workspace(pool)
                b._cleanup()
                first = pool.sweep()['results']
                second = native_cleanup.Manager(pool.root, create=False).sweep()['results']
            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 1)
            self.assertNotEqual(first[0]['root'], second[0]['root'])

    def test_unknown_orphan_blocks_admission_and_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = self.pool(directory)
            pool.sweep()  # Persist control ownership before introducing the orphan.
            orphan = pool.volumes / 'dk-volume-unregistered'
            orphan.mkdir(mode=0o700)
            (orphan / 'turn.dmg').write_bytes(b'unowned')
            with self.assertRaises(native_cleanup.AdmissionRefused):
                pool.prepare(native_storage.Workspace(64, cleanup_manager=pool))
            self.assertEqual(pool.sweep()['results'], [])
            self.assertTrue(orphan.exists())

    def test_status_is_read_only_and_active_preparation_blocks_concurrent_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = self.pool(directory)
            w = native_storage.Workspace(64, cleanup_manager=pool)
            r = pool.prepare(w)
            try:
                before = (pool.root / native_recovery.JOURNAL).read_bytes()
                self.assertEqual(pool.status()['active'], 1)
                self.assertEqual((pool.root / native_recovery.JOURNAL).read_bytes(), before)
                with self.assertRaises(native_cleanup.AdmissionRefused):
                    pool.prepare(native_storage.Workspace(64, cleanup_manager=pool))
                self.assertEqual(len(list(pool.volumes.iterdir())), 1)
            finally:
                r.close()
                import os
                os.close(w._disposal_lease)
                w._disposal_lease = None

    def test_uncertain_notification_delivery_is_retained_and_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = self.pool(directory, max_pending=1)
            w = self.workspace(pool)
            with self.fail_eject():
                w._cleanup()
                pool.sweep(notify=mock.Mock(side_effect=OSError('delivery failed')))
                again = mock.Mock()
                pool.sweep(notify=again)
            again.assert_not_called()
            self.assertEqual(pool.status()['notices'][0]['state'], 'uncertain')

    def test_supervisor_sweeps_pool_before_ledger_recovery_host_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            pool = self.pool(directory)
            w = self.workspace(pool)
            with self.fail_eject():
                w._cleanup()
            sup = run_supervisor.Supervisor(root / 'ledger.sqlite')
            with mock.patch.object(run_supervisor.sys, 'platform', 'darwin'), \
                    mock.patch.object(native_storage.Workspace, '_detach_owned'), \
                    mock.patch.object(native_storage.Workspace, '_verify_detached'), \
                    mock.patch.object(native_recovery, 'startup', return_value=[]), redirect_stderr(io.StringIO()):
                sup.recover()
            self.assertFalse(w.root.exists())
            self.assertEqual(sup.native_cleanup_results['results'][0]['status'], 'removed')

    def test_watchdog_pending_status_keeps_process_proof_distinct_from_disk_disposal(self):
        import json, os
        read, write = os.pipe()
        try:
            os.write(write, json.dumps({'returncode': 0, 'timeout': False, 'parent_lost': False,
                                       'group_drained': True, 'watchdog_lost': True,
                                       'storage_cleaned': False, 'storage_pending': True}).encode())
            os.close(write)
            write = None
            with self.assertRaises(native_lifecycle.StoragePending):
                native_lifecycle._completion(SimpleNamespace(returncode=0), read)
        finally:
            os.close(read)
            if write is not None:
                os.close(write)

    def test_live_host_retains_volume_after_group_drain_until_writes_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = self.pool(directory)
            w = self.workspace(pool)
            with mock.patch.object(native_storage.Workspace, '_detach_owned') as detach:
                self.assertEqual(pool.sweep()['results'], [])
                self.assertEqual(native_recovery.recover(w.root)['status'], 'active')
            detach.assert_not_called()
            with self.fail_eject():
                w._cleanup()
            self.assertEqual(pool.status()['pending_images'], 1)

    def test_pending_byte_and_age_limits_refuse_new_creation(self):
        for limits in ({'max_pending_bytes': 64 * 1024 ** 2}, {'max_pending_age': 1}):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as directory:
                pool = self.pool(directory, **limits)
                w = self.workspace(pool)
                with self.fail_eject():
                    w._cleanup()
                with native_recovery.Record(w.root) as r:
                    record = r.read()
                    record['pending_since'] -= 10
                    r.write(record)
                with self.assertRaises(native_cleanup.AdmissionRefused):
                    pool.prepare(native_storage.Workspace(64, cleanup_manager=pool))
                self.assertEqual(len(list(pool.volumes.iterdir())), 1)

    def test_status_resolves_parent_alias_but_rejects_a_linked_pool_root(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as target:
            root = Path(directory).resolve()
            pool = self.pool(directory)
            pool.sweep()
            alias = Path(target).resolve() / 'parent-alias'
            alias.symlink_to(root, target_is_directory=True)
            self.assertFalse(native_cleanup.ledger_status(alias / 'ledger.sqlite')['paused'])
            linked = Path(target).resolve() / 'native-cleanup'
            linked.symlink_to(pool.root, target_is_directory=True)
            self.assertTrue(native_cleanup.ledger_status(linked.parent / 'ledger.sqlite')['paused'])

    def test_missing_pool_state_refuses_existing_images_instead_of_resetting_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = self.pool(directory, max_pending=1)
            w = self.workspace(pool)
            with self.fail_eject():
                w._cleanup()
            (pool.root / native_recovery.JOURNAL).unlink()
            with self.assertRaises(native_recovery.RecoveryError):
                native_cleanup.Manager(pool.root, create=False)
            self.assertTrue(w.image.exists())

    def test_doctor_reads_pending_pool_and_reports_pause_without_disposal(self):
        from diaktoros import doctor
        with tempfile.TemporaryDirectory() as directory:
            pool = self.pool(directory, max_pending=1)
            w = self.workspace(pool)
            with self.fail_eject():
                w._cleanup()
            before = (pool.root / native_recovery.JOURNAL).read_bytes()
            with mock.patch.object(run_supervisor, 'production_ledger', return_value=Path(directory) / 'ledger.sqlite'), \
                    mock.patch.object(native_storage.Workspace, '_detach_owned') as detach:
                check = doctor.check_native_cleanup()
            self.assertEqual(check.status, doctor.MISMATCH)
            self.assertIn('images awaiting cleanup', check.detail)
            self.assertEqual((pool.root / native_recovery.JOURNAL).read_bytes(), before)
            detach.assert_not_called()


class NativeCleanupBoundary(unittest.TestCase):
    def setUp(self):
        import os
        from diaktoros import seatbelt
        reason = seatbelt.unavailable()
        if reason:
            if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_STORAGE') == '1':
                self.fail(reason)
            self.skipTest(reason)

    def dispose(self, pool, root):
        for _ in range(5):
            if not root.exists():
                return
            pool.sweep()
        self.assertFalse(root.exists(), 'image remained after five verified cleanup sweeps')

    def test_real_turn_result_survives_busy_eject_and_fresh_pool_disposes(self):
        from test_native_recovery import NativeRecoveryBoundary
        with tempfile.TemporaryDirectory(prefix='dk-pool-', dir='/tmp') as directory:
            outer = Path(directory).resolve()
            pool = native_cleanup.Manager(outer / 'native-cleanup')
            w = native_storage.Workspace(64, cleanup_manager=pool).__enter__()
            root = w.root
            try:
                native_recovery.arm(w)
                argv = NativeRecoveryBoundary().command(w, outer)
                result = native_lifecycle.capture(argv, env={'PATH': '/usr/bin:/bin'}, timeout=10,
                                                  cwd=w.work, recovery_root=root)
                self.assertEqual(result.returncode, 7)
                # Seat completion does not permit racing the host's result recording.
                self.assertEqual(pool.sweep()['results'], [])
                (w.work / 'host-result').write_text(str(result.returncode))
                with mock.patch.object(native_storage.Workspace, '_detach_owned',
                                       side_effect=native_storage.StorageError('injected Resource busy')):
                    w._cleanup()
                self.assertEqual(w.pending_cleanup['status'], 'pending')
                self.assertEqual((w.work / 'host-result').read_text(), '7')
                fresh = native_cleanup.Manager(pool.root, create=False)
                self.dispose(fresh, root)
                self.assertEqual(fresh.status()['pending_images'], 0)
            finally:
                if w._disposal_lease is not None:
                    import os
                    os.close(w._disposal_lease)
                    w._disposal_lease = None
                if root.exists():
                    native_recovery.recover(root, defer=True)

    def test_real_watchdog_death_drains_group_then_queues_disposal_for_host_sweep(self):
        import os, select
        from contextlib import closing
        from test_native_recovery import NativeRecoveryBoundary
        from diaktoros import contained
        with tempfile.TemporaryDirectory(prefix='dk-pool-watchdog-', dir='/tmp') as directory, \
                closing(select.kqueue()) as events:
            outer = Path(directory).resolve()
            pool = native_cleanup.Manager(outer / 'native-cleanup')
            w = native_storage.Workspace(64, cleanup_manager=pool).__enter__()
            root = w.root
            boundary = NativeRecoveryBoundary()
            native_recovery.arm(w)
            argv = boundary.command(w, outer, bounded=True, descendants=True)
            trigger, helper = outer / 'kill-monitor', outer / 'helper.py'
            helper.write_text(
                'import os,sys,time\nfrom pathlib import Path\n'
                f'sys.path.insert(0,{str(Path(native_lifecycle.__file__).resolve().parent.parent)!r})\n'
                'from diaktoros import native_lifecycle as life\nlife.__file__=__file__\n'
                'if sys.argv[1] == "--sentinel":\n'
                '    deadline=time.monotonic()+20\n'
                f'    while not Path({str(trigger)!r}).exists() and time.monotonic()<deadline: time.sleep(.02)\n'
                '    os.kill(os.getpid(),9)\n'
                'else:\n    life._watch(*(int(value) for value in sys.argv[1:]))\n')
            original = contained.capture_process
            child = group = None
            def fail_monitor(guardian, **kwargs):
                nonlocal child, group
                child = boundary.ready(w)
                group = os.getpgid(child)
                boundary.observe(events, child)
                trigger.touch()
                return original(guardian, **kwargs)
            try:
                with mock.patch.object(native_lifecycle, '__file__', str(helper)), \
                        mock.patch.object(contained, 'capture_process', side_effect=fail_monitor):
                    with self.assertRaises(native_lifecycle.StoragePending):
                        native_lifecycle.capture(argv, env={'PATH': '/usr/bin:/bin'}, timeout=10,
                                                 cwd=w.work, recovery_root=root)
                self.assertTrue(any(e.ident == child for e in events.control(None, 1, 5)))
                self.assertFalse(native_lifecycle._group_members(group))
                self.assertTrue(root.exists())
                w._cleanup()  # Release the host result-recording lease; queue is durable.
                self.assertEqual(w.pending_cleanup['status'], 'pending')
                self.dispose(native_cleanup.Manager(pool.root, create=False), root)
            finally:
                if w._disposal_lease is not None:
                    os.close(w._disposal_lease)
                    w._disposal_lease = None
                if root.exists():
                    native_recovery.recover(root, defer=True)
