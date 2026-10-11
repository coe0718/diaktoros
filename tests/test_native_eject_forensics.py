"""The evidence collector must preserve failures and bound its observations."""
import _home_guard  # noqa: F401
import io
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
            self.assertEqual(calls[1], ['/usr/sbin/lsof', '-nP', '+c', '0', '-Fpcfn', '--',
                                       str(workspace.image), '/dev/disk99s1', '/dev/rdisk99s1'])
            self.assertTrue(all('detach' not in argv and '-force' not in argv for argv in calls))
            kill.assert_not_called()
            killpg.assert_not_called()


if __name__ == '__main__':
    unittest.main()
