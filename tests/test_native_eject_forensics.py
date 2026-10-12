"""The evidence collector must preserve failures and bound its observations."""
import _home_guard  # noqa: F401
import io
import plistlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import native_eject_forensics as forensics
from diaktoros import native_storage


class ForensicsValidation(unittest.TestCase):
    def test_forced_unmount_rechecks_ownership_and_reports_failed_intervention(self):
        for still_owned, outcome in ((True, {'returncode': 0}), (False, {'returncode': 0}),
                                     (True, {'returncode': 1})):
            with tempfile.TemporaryDirectory() as directory:
                recorder = forensics.Recorder(Path(directory) / 'evidence', pre_unmount=True)
                workspace = SimpleNamespace(image=Path('/private/fixture/turn.dmg'),
                                            _devices=mock.Mock(side_effect=[['/dev/disk99'],
                                                ['/dev/disk99'] if still_owned else []]))
                with mock.patch.object(recorder, 'command', side_effect=[
                        ({'returncode': 0}, b'', b''), (outcome, b'', b'')]) as command, \
                        mock.patch.object(forensics.time, 'sleep'):
                    if outcome['returncode']:
                        with self.assertRaisesRegex(RuntimeError, 'unmount failed'):
                            recorder.before_detach(workspace)
                    else:
                        recorder.before_detach(workspace)
                self.assertEqual(workspace._devices.call_count, 2)
                self.assertEqual(command.call_count, 2 if still_owned else 1)
                if still_owned:
                    self.assertEqual(command.call_args.args[1],
                                     [native_storage.DISKUTIL, 'unmountDisk', 'force', '/dev/disk99'])

    def test_sync_delay_is_opt_in_and_failed_sync_does_not_claim_a_completed_wait(self):
        workspace = SimpleNamespace(image=Path('/private/fixture/turn.dmg'))
        for enabled, outcome in ((False, {'returncode': 0}), (True, {'returncode': 0}),
                                 (True, {'error': 'timeout'})):
            with tempfile.TemporaryDirectory() as directory:
                recorder = forensics.Recorder(Path(directory) / 'evidence', sync_delay=enabled)
                with mock.patch.object(recorder, 'command', return_value=(outcome, b'', b'')) as run, \
                        mock.patch.object(forensics.time, 'sleep') as sleep, \
                        mock.patch.object(recorder, 'emit') as emit:
                    if outcome.get('error'):
                        with self.assertRaisesRegex(RuntimeError, 'sync failed'):
                            recorder.before_detach(workspace)
                        sleep.assert_not_called()
                        emit.assert_not_called()
                    else:
                        recorder.before_detach(workspace)
                        self.assertEqual(run.call_count, int(enabled))
                        if enabled:
                            run.assert_called_once_with('pre-detach-sync', ['/bin/sync'], timeout=15)
                            sleep.assert_called_once_with(2)
                            emit.assert_called_once()
                        else:
                            sleep.assert_not_called()

    def test_registry_preserves_provider_sibling_client_and_filters_owned_media(self):
        tree = [{'IORegistryEntryName': 'root', 'IORegistryEntryChildren': [
            {'IORegistryEntryName': 'image-driver', 'IORegistryEntryID': 42,
             'IORegistryEntryChildren': [
                 {'IORegistryEntryName': 'media', 'BSD Name': 'disk99', 'Open': True},
                 {'IOObjectClass': 'ImageUserClient', 'IOUserClientCreator': 'pid 123, helper'}]},
            {'IORegistryEntryName': 'other', 'BSD Name': 'disk0'}]}]
        result = forensics.registry_connections(plistlib.dumps(tree), ['/dev/disk99'])
        self.assertEqual(result, forensics.registry_connections(plistlib.dumps(tree[0]), ['/dev/disk99']))
        self.assertEqual(len(result['media']), 1)
        self.assertEqual(result['media'][0]['path'][-1]['BSD Name'], 'disk99')
        self.assertTrue(result['media'][0]['path'][-1]['Open'])
        self.assertEqual(result['user_clients'][0]['path'][-2]['IORegistryEntryID'], 42)
        self.assertEqual(result['user_clients'][0]['path'][-1]['IOUserClientCreator'],
                         'pid 123, helper')
        with self.assertRaises(ValueError):
            forensics.registry_connections(plistlib.dumps({'unexpected': 'root'}), [])

    def test_cpu_parser_preserves_fractional_time_and_rejects_malformed_rows(self):
        rows = forensics.process_rows(
            '123 Sun Oct 11 13:00:01 2026 1:02.34 S /path/diskimages-helper\n'
            '456 Sun Oct 11 13:00:01 2026 2-03:04:05.67 S helper\n'
            '789 invalid\n')
        self.assertAlmostEqual(rows[123]['cpu_seconds'], 62.34)
        self.assertAlmostEqual(rows[456]['cpu_seconds'], 183845.67)
        self.assertNotIn(789, rows)

    def test_cpu_comparison_does_not_call_replacement_or_missing_process_zero_progress(self):
        for started, cpu, expected in [('13:00:01', '0:00.19', 0.07),
                                       ('13:00:02', '0:00.19', None),
                                       ('13:00:01', '0:00.01', None),
                                       (None, None, None)]:
            with self.subTest(started=started, cpu=cpu), tempfile.TemporaryDirectory() as directory:
                recorder = forensics.Recorder(Path(directory) / 'evidence')
                workspace = SimpleNamespace(image=Path('/private/fixture/turn.dmg'))
                first = b'123 Sun Oct 11 13:00:01 2026 0:00.12 S /path/diskimages-helper\n'
                last = (f'123 Sun Oct 11 {started} 2026 {cpu} S /path/diskimages-helper\n'.encode()
                        if started else b'')
                holder = b'p123\ncdiskimages-helper\nf4\nn/private/fixture/turn.dmg\n'
                with mock.patch.object(recorder, 'command', side_effect=[
                        ({}, holder, b''), ({}, first, b''),
                        ({}, holder if started else b'', b''), ({}, last, b'')]):
                    recorder.helper_progress(workspace, 'first-failure')
                    recorder.helper_progress(workspace, 'give-up')
                event = json.loads((recorder.output / 'events.jsonl').read_text().splitlines()[-1])
                self.assertEqual(event['observations'][0]['cpu_delta_seconds'], expected)
                self.assertEqual(event['observations'][0]['holds_image'], bool(started))

    def test_stream_keeps_bounded_start_and_end_when_failure_is_late(self):
        raw = b'early messages\n' + b'x' * 80 + b'late eject rejection\n'
        process = SimpleNamespace(stdout=io.BytesIO(raw), returncode=0,
                                  poll=lambda: 0, wait=lambda **kwargs: 0)
        with tempfile.TemporaryDirectory() as directory:
            recorder = forensics.Recorder(Path(directory) / 'evidence')
            with mock.patch.object(subprocess, 'Popen', return_value=process), \
                    mock.patch.object(forensics, 'MAX_STREAM', 32):
                with recorder.log_stream():
                    pass
            self.assertEqual((recorder.output / 'system-stream.log').read_bytes(), raw[:32])
            self.assertEqual((recorder.output / 'system-stream-tail.log').read_bytes(), raw[-32:])
            event = json.loads((recorder.output / 'events.jsonl').read_text())
            self.assertTrue(event['truncated'])
            self.assertEqual(event['tail_bytes'], 32)

    def test_timeout_preserves_partial_evidence_and_does_not_report_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = forensics.Recorder(Path(directory) / 'evidence')
            with mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired(
                    'fixture', 5, output=b'partial holder', stderr=b'partial error')):
                event, stdout, stderr = recorder.command('holders', ['fixture'])
            self.assertEqual(event['error'], 'timeout')
            self.assertNotIn('returncode', event)
            self.assertEqual(stdout, b'partial holder')
            self.assertEqual(stderr, b'partial error')
            self.assertEqual((recorder.output / '0001-holders.stdout').read_bytes(), stdout)

    def test_standard_and_verbose_detach_keep_original_busy_failure_and_full_output(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose), tempfile.TemporaryDirectory() as directory:
                recorder = forensics.Recorder(Path(directory) / 'evidence', verbose=verbose)
                argv = [native_storage.HDIUTIL, 'detach', '/dev/disk99']
                with mock.patch.object(subprocess, 'run', return_value=SimpleNamespace(
                        stdout=b'full detach diagnostics', stderr=b'Resource busy', returncode=16)) as run:
                    with self.assertRaisesRegex(native_storage.StorageError, 'Resource busy'):
                        recorder.run(argv)
                self.assertEqual(run.call_args.args[0],
                                 argv[:-1] + ['-verbose', argv[-1]] if verbose else argv)
                event = json.loads((recorder.output / 'events.jsonl').read_text())
                self.assertEqual(event['returncode'], 16)
                self.assertEqual((recorder.output / '0001-detach.stdout').read_bytes(),
                                 b'full detach diagnostics')

    def test_lsof_machine_fields_reject_non_pid_fields_and_keep_process_names(self):
        self.assertEqual(forensics.holders('p123\ncdiskimages-helper\nf4u\nn/private/image\n'
                                           'p1\ncignored\npbad\ncignored\np456\ncpython\n'),
                         {123: 'diskimages-helper', 456: 'python'})

    def test_disk_state_snapshots_are_read_only_and_owned_nodes_are_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = forensics.Recorder(Path(directory) / 'evidence')
            workspace = SimpleNamespace(root=Path('/private/tmp/fixture'),
                                        image=Path('/private/tmp/fixture/turn.dmg'),
                                        _attachments=lambda: [{'system-entities': [
                                            {'dev-entry': '/dev/disk99s1'},
                                            {'dev-entry': '--bad-argument'}]}])
            with mock.patch.object(recorder, 'command', return_value=({}, b'', b'')) as command, \
                    mock.patch('os.kill') as kill, mock.patch('os.killpg') as killpg:
                recorder.snapshot(workspace, 'fixture')
            calls = [c.args[1] for c in command.call_args_list]
            self.assertEqual(calls[0][0], '/usr/sbin/ioreg')
            self.assertEqual(next(c for c in calls if c[0] == '/usr/sbin/lsof'),
                                     ['/usr/sbin/lsof', '-nP', '+c', '0', '-Fpcfn', '--',
                                       str(workspace.image), '/dev/disk99s1', '/dev/rdisk99s1'])
            self.assertTrue(all('detach' not in argv and '-force' not in argv for argv in calls))
            kill.assert_not_called()
            killpg.assert_not_called()


class NaturalRecoveryValidation(unittest.TestCase):
    def test_fifth_sweep_must_remove_image_and_late_success_fails(self):
        deadline = forensics.DisposalDeadline(0)
        for tick in range(4):
            deadline.before(tick)
            deadline.after(tick, removed=False)
        deadline.before(4)
        with self.assertRaises(RuntimeError):
            deadline.after(4, removed=False)
        self.assertEqual(deadline.after(4, removed=True), 4)
        with self.assertRaises(RuntimeError):
            deadline.before(5)
        with self.assertRaises(RuntimeError):
            forensics.DisposalDeadline(0).after(301, removed=True)

    def test_no_natural_busy_is_not_eligible_for_acceptance(self):
        recorder = mock.Mock()
        workspace = SimpleNamespace(had_busy=False, pending_cleanup={'status': 'pending'})
        with self.assertRaisesRegex(RuntimeError, 'not a captured natural busy'):
            forensics.reconcile_natural(recorder, workspace, mock.Mock(), 0)
        recorder.command.assert_not_called()

    def fixture(self, directory, *, drained):
        import os
        from diaktoros import native_cleanup, native_recovery
        pool = native_cleanup.Manager(Path(directory).resolve()/'pool')
        w = native_storage.Workspace(64, cleanup_manager=pool)
        r = pool.prepare(w)
        w.had_busy = True
        w.pending_cleanup = {'status': 'pending'}
        w.cleanup_events = [{'error':'Resource busy','force':False} for _ in range(5)] + [{'error':'Resource busy','force':True}]
        w.image.write_bytes(b'fixture')
        w.image.chmod(0o600)
        w.mount.mkdir()
        r.update('created', image_identity=native_recovery._identity(w.image.lstat()))
        r.update('attached')
        r.update('armed')
        if drained:
            r.update('drained')
            r.defer_cleanup('Resource busy')
        r.close()
        os.close(w._disposal_lease)
        w._disposal_lease = None
        return w

    def test_uncertain_process_receipt_never_reaches_cleanup_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            w = self.fixture(directory, drained=False)
            recorder = forensics.Recorder(Path(directory)/'evidence')
            with mock.patch.object(recorder, 'command') as command:
                with self.assertRaisesRegex(RuntimeError, 'durable completion'):
                    forensics.reconcile_natural(recorder, w, w.cleanup_manager, 0)
            command.assert_not_called()
            self.assertTrue(w.image.exists())

    def test_transient_busy_without_exhausted_force_does_not_qualify(self):
        with tempfile.TemporaryDirectory() as directory:
            w = self.fixture(directory, drained=True)
            w.cleanup_events = [{'error':'Resource busy','force':False}]
            recorder = forensics.Recorder(Path(directory)/'evidence')
            with mock.patch.object(recorder, 'command') as command:
                with self.assertRaisesRegex(RuntimeError, 'did not exhaust'):
                    forensics.reconcile_natural(recorder, w, w.cleanup_manager, 0)
            command.assert_not_called()
            self.assertTrue(w.image.exists())

    def test_quiet_churn_is_inconclusive_when_natural_capture_is_required(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            recorder = forensics.Recorder(root/'evidence')
            with mock.patch.object(forensics.tempfile, 'mkdtemp', return_value=str(root/'pool')), \
                    mock.patch.object(recorder, 'log_stream', return_value=nullcontext()), \
                    mock.patch.object(recorder, 'command', return_value=({'returncode':0},b'',b'')):
                result = forensics.probe_eventual(recorder, 0, require_natural=True)
            self.assertEqual(result, 2)
            report = json.loads((recorder.output/'eventual-result.json').read_text())
            self.assertTrue(report['inconclusive'])
            self.assertFalse(report['natural_recovery_accepted'])

    def test_verified_natural_queue_success_records_disposal_latency(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            w = self.fixture(directory, drained=True)
            recorder = forensics.Recorder(Path(directory)/'evidence')
            def command(*args, **kwargs):
                shutil.rmtree(w.root)  # Simulate the fresh worker's verified disposal.
                return {'returncode': 0}, json.dumps({'results':[{'root':str(w.root),'status':'removed'}]}).encode(), b''
            with mock.patch.object(recorder, 'command', side_effect=command), \
                    mock.patch.object(recorder, 'helper_progress'), \
                    mock.patch.object(forensics.time, 'monotonic', side_effect=[1,2]):
                result = forensics.reconcile_natural(recorder, w, w.cleanup_manager, 0)
            self.assertEqual(result, {'sweeps':1, 'elapsed_seconds':2})
            self.assertTrue((recorder.output/'natural-pending-receipt.json').exists())

    def test_disappearance_without_verified_worker_result_cannot_pass(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            w = self.fixture(directory, drained=True)
            recorder = forensics.Recorder(Path(directory)/'evidence')
            def command(*args, **kwargs):
                shutil.rmtree(w.root)
                return {'returncode':0}, b'{"results":[{"status":"absent"}]}', b''
            with mock.patch.object(recorder, 'command', side_effect=command), \
                    mock.patch.object(forensics.time, 'monotonic', return_value=1):
                with self.assertRaisesRegex(RuntimeError, 'without matching verified'):
                    forensics.reconcile_natural(recorder, w, w.cleanup_manager, 0)

    def test_failed_fresh_worker_retains_image_and_does_not_claim_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            w = self.fixture(directory, drained=True)
            recorder = forensics.Recorder(Path(directory)/'evidence')
            with mock.patch.object(recorder, 'command', return_value=({'error':'timeout'}, b'', b'')), \
                    mock.patch.object(forensics.time, 'monotonic', return_value=1):
                with self.assertRaisesRegex(RuntimeError, 'worker failed or timed out'):
                    forensics.reconcile_natural(recorder, w, w.cleanup_manager, 0)
            self.assertTrue(w.image.exists())


if __name__ == "__main__":
    unittest.main()
