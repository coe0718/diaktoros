"""Actual disk-full boundary and failure-safe cleanup for native workspaces."""
import _home_guard  # noqa: F401
from pathlib import Path
from contextlib import closing
from dataclasses import replace
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

from diaktoros import contained, native_lifecycle, native_macos, native_storage, seatbelt, turn_layout


class StorageValidation(unittest.TestCase):
    def test_eject_diagnostics_cannot_interrupt_cleanup_after_reader_dies(self):
        reader, writer = os.pipe()
        os.close(reader)
        try:
            with mock.patch.object(native_storage.sys, 'stderr',
                                   SimpleNamespace(fileno=lambda: writer)):
                native_storage._diagnostic('NATIVE_EJECT', {'device': '/dev/disk99'})
        finally:
            os.close(writer)

    def test_busy_creation_retries_only_after_partial_image_is_detached_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace(64)
            workspace.root = Path(directory)
            workspace.mount = workspace.root / 'mount'
            workspace.mount.mkdir()
            workspace.image = workspace.root / 'turn.dmg'
            attached = True

            def create(argv):
                self.assertFalse(workspace.image.exists(), 'overwrote partial backing image')
                workspace.image.write_bytes(b'partial' if run.call_count == 1 else b'complete')
                if run.call_count == 1:
                    raise native_storage.StorageError('hdiutil: create failed - Resource busy')

            def detach():
                nonlocal attached
                self.assertEqual(workspace.image.read_bytes(), b'partial')
                attached = False

            with mock.patch.object(native_storage, '_run', side_effect=create) as run, \
                    mock.patch.object(workspace, '_attachments', return_value=[]), \
                    mock.patch.object(workspace, '_detach_owned', side_effect=detach) as drain, \
                    mock.patch.object(workspace, '_devices', side_effect=lambda:
                                      ['/dev/disk99'] if attached else []), \
                    mock.patch.object(native_storage.time, 'sleep') as sleep:
                workspace._create_image()
            self.assertEqual(run.call_count, 2)
            drain.assert_called_once()
            sleep.assert_called_once_with(2)
            self.assertEqual(workspace.image.read_bytes(), b'complete')
            self.assertIn('Resource busy', workspace.creation_events[0]['error'])

    def test_busy_creation_never_overwrites_a_still_attached_partial_image(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace(64)
            workspace.root = Path(directory)
            workspace.mount = workspace.root / 'mount'
            workspace.mount.mkdir()
            workspace.image = workspace.root / 'turn.dmg'
            workspace.image.write_bytes(b'partial')
            with mock.patch.object(native_storage, '_run', side_effect=
                                   native_storage.StorageError('Resource busy')) as run, \
                    mock.patch.object(workspace, '_attachments', return_value=[]), \
                    mock.patch.object(workspace, '_detach_owned'), \
                    mock.patch.object(workspace, '_devices', return_value=['/dev/disk99']), \
                    mock.patch.object(native_storage.time, 'sleep') as sleep:
                with self.assertRaisesRegex(native_storage.StorageError, 'remains attached'):
                    workspace._create_image()
            run.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(workspace.image.read_bytes(), b'partial')

    def test_busy_creation_has_a_finite_retry_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace(64)
            workspace.root = Path(directory)
            workspace.mount = workspace.root / 'mount'
            workspace.mount.mkdir()
            workspace.image = workspace.root / 'turn.dmg'
            with mock.patch.object(native_storage, '_run', side_effect=
                                   native_storage.StorageError('Resource busy')) as run, \
                    mock.patch.object(workspace, '_attachments', return_value=[]), \
                    mock.patch.object(workspace, '_detach_owned') as drain, \
                    mock.patch.object(workspace, '_devices', return_value=[]), \
                    mock.patch.object(native_storage.time, 'sleep') as sleep:
                with self.assertRaisesRegex(native_storage.StorageError, 'Resource busy'):
                    workspace._create_image()
            self.assertEqual(run.call_count, 3)
            self.assertEqual(drain.call_count, 2)
            self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(4)])

    def test_invalid_sizes_fail_before_creating_storage(self):
        for value in (True, 0, 63, 16385, '512', 1.5):
            with self.assertRaises(ValueError):
                native_storage.Workspace(value)

    def test_non_mac_has_no_unbounded_fallback(self):
        if sys.platform == 'darwin':
            self.skipTest('non-mac fail-closed probe')
        workspace = native_storage.Workspace()
        with self.assertRaisesRegex(native_storage.StorageError, 'requires macOS'):
            workspace.__enter__()
        self.assertIsNone(workspace.root)

    def test_failed_detach_retains_backing_image(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace()
            workspace.root = Path(directory)
            workspace.mount = workspace.root / 'mount'
            workspace.mount.mkdir()
            workspace.image = workspace.root / 'turn.dmg'
            workspace.image.write_bytes(b'fixture')
            with mock.patch.object(workspace, '_devices', return_value=['/dev/disk99']), \
                    mock.patch.object(native_storage, '_run', side_effect=native_storage.StorageError('busy')):
                with self.assertRaisesRegex(native_storage.StorageError, 'retained at'):
                    workspace._cleanup()
            self.assertTrue(workspace.image.is_file())
            self.assertFalse(workspace.active)

    def test_unverified_execution_cleanup_preserves_mount_and_image(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace()
            workspace.root = Path(directory)
            workspace.image = workspace.root / 'turn.dmg'
            workspace.image.touch()
            workspace.retain('watchdog completion unknown')
            with mock.patch.object(workspace, '_devices') as devices:
                with self.assertRaisesRegex(native_storage.StorageError, 'watchdog completion unknown'):
                    workspace._cleanup()
                devices.assert_not_called()
            self.assertTrue(workspace.image.exists())

    def test_busy_eject_rediscovers_owned_disk_before_retry(self):
        workspace = native_storage.Workspace()
        busy = native_storage.StorageError('hdiutil: Resource busy')
        with mock.patch.object(workspace, '_devices', side_effect=[
                ['/dev/disk99'], ['/dev/disk99'], ['/dev/disk99'],
                ['/dev/disk100'], ['/dev/disk100'], []]), \
                mock.patch.object(native_storage, '_run', side_effect=[busy, b'']) as run, \
                mock.patch.object(native_storage.time, 'sleep') as sleep:
            workspace._detach_owned()
        self.assertEqual(run.call_args_list, [
            mock.call([native_storage.HDIUTIL, 'detach', '/dev/disk99']),
            mock.call([native_storage.HDIUTIL, 'detach', '/dev/disk100'])])
        sleep.assert_called_once_with(2)

    def test_failed_eject_that_detached_does_not_force_stale_disk(self):
        workspace = native_storage.Workspace()
        with mock.patch.object(workspace, '_devices', side_effect=[
                ['/dev/disk99'], ['/dev/disk99'], [], []]), \
                mock.patch.object(native_storage, '_run', side_effect=
                                  native_storage.StorageError('hdiutil: Resource busy')) as run:
            workspace._detach_owned()
        run.assert_called_once_with([native_storage.HDIUTIL, 'detach', '/dev/disk99'])

    def test_settling_image_succeeds_on_last_normal_attempt_without_force(self):
        workspace = native_storage.Workspace()
        busy = native_storage.StorageError('Resource busy')
        attached = True

        def eject(argv):
            nonlocal attached
            if run.call_count < 5:
                raise busy
            attached = False
            return b''

        with mock.patch.object(workspace, '_devices', side_effect=lambda:
                               ['/dev/disk99'] if attached else []), \
                mock.patch.object(native_storage, '_run', side_effect=eject) as run, \
                mock.patch.object(native_storage.time, 'sleep') as sleep:
            workspace._detach_owned()
        self.assertEqual(run.call_args_list, [
            mock.call([native_storage.HDIUTIL, 'detach', '/dev/disk99'])] * 5)
        self.assertEqual(sleep.call_args_list, [mock.call(n) for n in (2, 4, 6, 8)])

    def test_nonbusy_failure_is_not_retried_or_forced(self):
        workspace = native_storage.Workspace()
        with mock.patch.object(workspace, '_devices', return_value=['/dev/disk99']), \
                mock.patch.object(native_storage, '_run', side_effect=
                                  native_storage.StorageError('permission denied')) as run, \
                mock.patch.object(native_storage.time, 'sleep') as sleep:
            with self.assertRaisesRegex(native_storage.StorageError, 'permission denied'):
                workspace._detach_owned()
        run.assert_called_once_with([native_storage.HDIUTIL, 'detach', '/dev/disk99'])
        sleep.assert_not_called()

    def test_final_busy_force_that_detached_is_verified_before_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace()
            workspace.root = Path(directory) / 'owned'
            workspace.root.mkdir()
            workspace.mount = workspace.root / 'mount'
            workspace.mount.mkdir()
            workspace.image = workspace.root / 'turn.dmg'
            workspace.image.write_bytes(b'fixture')
            attached = True

            def eject(device, *, force):
                nonlocal attached
                self.assertEqual(device, '/dev/disk99')
                if force:
                    attached = False
                raise native_storage.StorageError('Resource busy')

            with mock.patch.object(workspace, '_devices', side_effect=lambda:
                                   ['/dev/disk99'] if attached else []), \
                    mock.patch.object(workspace, '_eject', side_effect=eject) as run, \
                    mock.patch.object(native_storage.time, 'sleep'):
                workspace._cleanup()
            self.assertEqual(run.call_count, 6)
            self.assertFalse(workspace.root.exists())

    def test_persistent_busy_eject_is_bounded_and_retains_image(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace()
            workspace.root = Path(directory)
            workspace.mount = workspace.root / 'mount'
            workspace.mount.mkdir()
            workspace.image = workspace.root / 'turn.dmg'
            workspace.image.write_bytes(b'fixture')
            with mock.patch.object(workspace, '_devices', return_value=['/dev/disk99']), \
                    mock.patch.object(workspace, '_attachments', return_value=[]), \
                    mock.patch.object(native_storage, '_run', side_effect=
                                      native_storage.StorageError('hdiutil: Resource busy')) as run, \
                    mock.patch.object(native_storage.time, 'sleep') as sleep:
                with self.assertRaisesRegex(native_storage.StorageError, 'retained at'):
                    workspace._cleanup()
            self.assertEqual(run.call_count, 6)
            self.assertEqual(run.call_args_list, [
                mock.call([native_storage.HDIUTIL, 'detach', '/dev/disk99'])] * 5 + [
                mock.call([native_storage.HDIUTIL, 'detach', '-force', '/dev/disk99'])])
            self.assertEqual(sleep.call_args_list, [mock.call(n) for n in (2, 4, 6, 8)])
            self.assertEqual(workspace.image.read_bytes(), b'fixture')

    def test_eject_records_partial_unmount_without_hiding_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace()
            workspace.root = Path(directory)
            workspace.mount = workspace.root / 'mount'
            workspace.mount.mkdir()
            workspace.image = workspace.root / 'turn.dmg'
            attachment = {'system-entities': [{'dev-entry': '/dev/disk99'}]}
            with mock.patch.object(workspace, '_attachments', return_value=[attachment]), \
                    mock.patch.object(native_storage, '_run', side_effect=
                                      native_storage.StorageError('Resource busy')):
                with self.assertRaisesRegex(native_storage.StorageError, 'Resource busy'):
                    workspace._eject('/dev/disk99', force=False)
            event, = workspace.cleanup_events
            self.assertFalse(event['mounted'])
            self.assertEqual(event['attachments'][0]['entities'], attachment['system-entities'])
            self.assertEqual(event['error'], 'Resource busy')
            self.assertGreaterEqual(event['elapsed_seconds'], 0)

    def test_false_successful_detach_still_retains_image(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = native_storage.Workspace()
            workspace.root = Path(directory)
            workspace.mount = workspace.root / 'mount'
            workspace.mount.mkdir()
            workspace.image = workspace.root / 'turn.dmg'
            workspace.image.touch()
            with mock.patch.object(workspace, '_devices', return_value=['/dev/disk99']), \
                    mock.patch.object(native_storage, '_run', return_value=b''):
                with self.assertRaisesRegex(native_storage.StorageError, 'retained at'):
                    workspace._cleanup()
            self.assertTrue(workspace.image.exists())

    def test_busy_mounted_eject_captures_volume_users_before_force(self):
        workspace = native_storage.Workspace()
        workspace.root = Path('/private/tmp/dk-eject-fixture')
        workspace.mount = workspace.root / 'mount'
        workspace.image = workspace.root / 'turn.dmg'
        attachment = {'system-entities': [{'dev-entry': '/dev/disk99s1',
                                          'mount-point': str(workspace.mount)}]}
        with mock.patch.object(workspace, '_attachments', return_value=[attachment]), \
                mock.patch.object(Path, 'stat', autospec=True, side_effect=lambda path:
                                  SimpleNamespace(st_dev=2 if path == workspace.mount else 1)), \
                mock.patch.object(native_storage, '_run', side_effect=
                                  native_storage.StorageError('Resource busy')), \
                mock.patch.object(subprocess, 'run', side_effect=[SimpleNamespace(
                                  stdout=b'bash fixture cwd on volume', stderr=b'', returncode=0),
                                  subprocess.TimeoutExpired('lsof fixture', 5)]) as users:
            with self.assertRaisesRegex(native_storage.StorageError, 'Resource busy'):
                workspace._eject('/dev/disk99', force=False)
            with self.assertRaisesRegex(native_storage.StorageError, 'Resource busy'):
                workspace._eject('/dev/disk99', force=True)
        self.assertIn('bash fixture cwd', workspace.cleanup_events[0]['volume_users'])
        self.assertEqual(workspace.cleanup_events[0]['volume_users_returncode'], 0)
        self.assertIn('timed out', workspace.cleanup_events[1]['diagnostic_error'])
        self.assertEqual(workspace.cleanup_events[1]['error'], 'Resource busy')
        self.assertEqual(users.call_args.args[0],
                         ['/usr/sbin/lsof', '-nP', '+f', '--', str(workspace.mount)])
        self.assertEqual(users.call_args.kwargs['timeout'], 5)


class NativeStorageBoundary(unittest.TestCase):
    def setUp(self):
        reason = seatbelt.unavailable()
        if reason:
            if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_STORAGE') == '1':
                self.fail(reason)
            self.skipTest(reason)

    def test_watchdog_sigkill_retains_active_turn_volume_without_cleanup_proof(self):
        # Exercise the real launcher's retention path, substituting only the
        # Hermes entry with a bounded fixture. No discovered PID is signalled.
        with tempfile.TemporaryDirectory(prefix='dk-watch-death-', dir='/tmp') as directory, \
                closing(select.kqueue()) as exits, \
                closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as inference, \
                closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as broker:
            outer = Path(directory).resolve()
            paths = {name: outer / name for name in
                     ('code', 'venv', 'rust', 'export', 'client')}
            for path in paths.values():
                path.mkdir()
            query = paths['code'] / 'query'
            query.touch()
            sockets = (outer / 'inference', outer / 'broker')
            for endpoint, path in zip((inference, broker), sockets):
                endpoint.bind(str(path))
                endpoint.listen(1)
            interpreter = Path(sys.executable).resolve()
            framework = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
            if framework.is_file():
                interpreter = framework.resolve()
            workspace = native_storage.Workspace(64).__enter__()
            ready, stop = workspace.work / 'ready', workspace.work / 'stop'
            child = None
            launched = False
            observed = False
            original_capture = contained.capture_process
            original_profile = seatbelt.profile
            script = '''
import json, os, time
from pathlib import Path
work = Path.cwd()
with (work / 'held-open').open('wb') as held:
    held.write(b'owned turn file')
    held.flush()
    for fd in (0, 1, 2):
        os.close(fd)
    (work / 'ready.tmp').write_text(json.dumps({'pid': os.getpid(), 'group': os.getpgrp()}))
    (work / 'ready.tmp').rename(work / 'ready')
    deadline = time.monotonic() + 20
    while not (work / 'stop').exists() and time.monotonic() < deadline:
        time.sleep(0.02)
'''

            def strict_profile(**kwargs):
                profile = original_profile(**kwargs)
                return replace(profile, text=profile.text + '(allow syscall-unix)\n'
                               '(deny syscall-unix (syscall-number 82 147 244))\n')

            def kill_watchdog(helper, **kwargs):
                nonlocal child, launched
                launched = True
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), 'bounded child did not start')
                record = json.loads(ready.read_text())
                child = record['pid']
                self.assertEqual(record['group'], child)
                exits.control([select.kevent(child, filter=select.KQ_FILTER_PROC,
                                             flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                             fflags=select.KQ_NOTE_EXIT)], 0, 0)
                # Popen still owns this unreaped helper, reserving its PID.
                helper.kill()
                result = original_capture(helper, **kwargs)
                self.assertEqual(result.returncode, -9)
                return result

            try:
                with mock.patch.object(turn_layout.TurnLayout, 'hermes_entry',
                                       return_value=[str(interpreter), '-I', '-B', '-c', script]), \
                        mock.patch.object(seatbelt, 'profile', side_effect=strict_profile), \
                        mock.patch.object(contained, 'capture_process', side_effect=kill_watchdog):
                    with self.assertRaisesRegex(native_lifecycle.CleanupIncomplete, 'completion'):
                        native_macos.run(**paths, runtime=Path(sys.base_prefix),
                                         home=workspace.home, work=workspace.work,
                                         scratch=workspace.scratch, query=query,
                                         inference_socket=sockets[0], broker_socket=sockets[1],
                                         model='fixture', role='reviewer', workspace=workspace,
                                         timeout=10)
                self.assertTrue(workspace.retained_reason)
                self.assertFalse(workspace.active)
                self.assertEqual(exits.control(None, 1, 0), [], 'fixture already exited')
                self.assertIn(child, native_lifecycle._group_members(child))
                with mock.patch.object(workspace, '_detach_owned') as detach:
                    with self.assertRaisesRegex(native_storage.StorageError, 'retained at'):
                        workspace.__exit__(None, None, None)
                    detach.assert_not_called()
                self.assertTrue(workspace.image.is_file())
                self.assertTrue(workspace._devices())
                self.assertNotEqual(workspace.mount.stat().st_dev, workspace.root.stat().st_dev)
                print('NATIVE_WATCHDOG_DEATH ' + json.dumps({
                    'child': child, 'image': str(workspace.image), 'child_survived': True,
                    'storage_retained': True, 'eject_attempts': len(workspace.cleanup_events)}),
                      flush=True)
            finally:
                # Only the cooperative fixture can release itself. Wait on the
                # registered kernel identity before touching retained storage.
                if not launched:
                    observed = True
                elif child is not None:
                    stop.touch()
                    observed = any(event.ident == child for event in exits.control(None, 1, 25))
                    if observed:
                        deadline = time.monotonic() + 5
                        while native_lifecycle._group_members(child) and time.monotonic() < deadline:
                            time.sleep(0.02)
                        observed = not native_lifecycle._group_members(child)
                if not observed:
                    workspace.retain('watchdog-death fixture exit could not be verified')
                    self.fail(f'fixture exit unverified; image retained at {workspace.root}')
                # Test-only release after independent exit proof. This is not a
                # restart recovery protocol or a production cleanup guarantee.
                workspace.retained_reason = ''
                workspace._cleanup()
            self.assertFalse(workspace.root.exists())

    def test_all_writable_roots_share_a_hard_allocation_bound(self):
        with tempfile.TemporaryDirectory(prefix='dk-storage-probe-', dir='/tmp') as directory:
            outer = Path(directory).resolve()
            outside = outer / 'outside'
            outside.write_text('HOST_FILE')
            policy_file = outer / 'policy.sb'
            with native_storage.Workspace(128) as workspace:
                self.assertTrue((workspace.mount / '.metadata_never_index').is_file())
                self.assertTrue((workspace.mount / '.fseventsd/no_log').is_file())
                image, image_size, root = workspace.image, workspace.image.stat().st_size, workspace.root
                workspace.validate(home=workspace.home, work=workspace.work, scratch=workspace.scratch)
                with self.assertRaises(native_storage.StorageError):
                    workspace.validate(home=workspace.home, work=outer, scratch=workspace.scratch)
                profile = seatbelt.profile(read_roots=(Path(sys.base_prefix),),
                                           write_roots=(workspace.home, workspace.work, workspace.scratch))
                script = f'''
import errno, os
from pathlib import Path
for forbidden in ({str(image)!r}, {str(outside)!r}):
    try:
        with open(forbidden, 'r+b'):
            pass
    except PermissionError:
        pass
    else:
        raise AssertionError('host/image write allowed')
escape = Path({str(workspace.work / 'escape')!r})
escape.symlink_to({str(outside)!r})
try:
    escape.write_text('changed')
except PermissionError:
    pass
else:
    raise AssertionError('symlink escaped volume')
for root in ({str(workspace.work)!r}, {str(workspace.home)!r}, {str(workspace.scratch)!r}):
    written = 0
    try:
        with open(Path(root) / 'fill', 'wb', buffering=0) as out:
            while written <= {workspace.size_bytes}:
                written += out.write(os.urandom(1024 * 1024))
    except OSError as error:
        assert error.errno == errno.ENOSPC, repr(error)
    else:
        raise AssertionError('allocated beyond fixed capacity')
print('ENOSPC_ALL_ROOTS')
'''
                policy_file.write_text(profile.text)
                argv = profile.command(policy_file, [str(Path(sys.executable).resolve()), '-I', '-B', '-c', script])
                result = subprocess.run(argv, env={'PATH': '/usr/bin:/bin', 'HOME': str(workspace.home),
                                                   'TMPDIR': str(workspace.scratch)},
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('ENOSPC_ALL_ROOTS', result.stdout)
                self.assertEqual(image.stat().st_size, image_size)
                self.assertEqual(outside.read_text(), 'HOST_FILE')
            self.assertFalse(root.exists())

    def test_exception_unmounts_and_removes_private_image(self):
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            with native_storage.Workspace(128) as workspace:
                root = workspace.root
                raise RuntimeError('fixture failure')
        self.assertFalse(root.exists())

    def test_unmounted_but_attached_image_is_ejected_by_exact_device(self):
        # Reproduce the state left by a partial eject, not the intermittent
        # kernel/subsystem race itself. The mount path is no longer a filesystem.
        with native_storage.Workspace(64) as workspace:
            root = workspace.root
            before = workspace._devices()
            self.assertTrue(before)
            native_storage._run([native_storage.DISKUTIL, 'unmount', str(workspace.mount)])
            self.assertEqual(workspace.mount.stat().st_dev, root.stat().st_dev)
            self.assertTrue(workspace._devices(), 'volume-only unmount detached the image')
            with mock.patch.object(workspace, '_eject', wraps=workspace._eject) as eject:
                workspace._detach_owned()
            self.assertTrue(eject.call_args_list)
            self.assertTrue(all(call.args[0] in before for call in eject.call_args_list))
            self.assertFalse(workspace._devices())
            self.assertTrue(workspace.image.exists(), 'eject deleted backing files before verification')
        self.assertFalse(root.exists())

    def test_repeated_eject_after_guarded_turn_with_open_workspace_file(self):
        # A short repeated fixture gives first-error diagnostics several chances
        # to catch the intermittent busy eject without reinstalling Hermes.
        with tempfile.TemporaryDirectory(prefix='dk-eject-probe-', dir='/tmp') as directory:
            policy = Path(directory).resolve() / 'policy.sb'
            interpreter = Path(sys.executable).resolve()
            framework = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
            if framework.is_file():
                # Bypass framework bin/python's native-spawn launcher, keeping
                # this kernel-only probe independent of the compatibility shim.
                interpreter = framework.resolve()
            for cycle in range(6):
                with self.subTest(cycle=cycle):
                    with native_storage.Workspace(64) as workspace:
                        root = workspace.root
                        profile = seatbelt.profile(read_roots=(Path(sys.base_prefix),),
                                                   write_roots=(workspace.home, workspace.work,
                                                                workspace.scratch))
                        policy.write_text(profile.text + '(allow syscall-unix)\n'
                                          '(deny syscall-unix (syscall-number 82 147 244))\n')
                        script = """
import os, time
# Leave cwd and a real open file on APFS until process exit. The host watchdog
# must finish before eject; neither the file nor cwd is held by the test host.
fd = os.open('held-open', os.O_CREAT | os.O_WRONLY, 0o600)
os.write(fd, b'x' * (1024 * 1024))
os.fsync(fd)
# Each reparented grandchild retains this open file and cwd. Bound the fixture
# even if the watchdog fails; the kernel profile prevents session/group escape.
for _ in range(8):
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        if os.fork():
            os._exit(0)
        null = os.open('/dev/null', os.O_RDWR)
        for target in (0, 1, 2):
            os.dup2(null, target)
        os.write(writer, b'r')
        os.close(writer)
        time.sleep(15)
        os._exit(0)
    os.close(writer)
    assert os.read(reader, 1) == b'r'
    os.close(reader)
    os.waitpid(child, 0)
print('TURN_COMPLETE')
"""
                        result = native_lifecycle.capture(
                            profile.command(policy, [str(interpreter),
                                                     '-I', '-B', '-c', script]),
                            env={'PATH': '/usr/bin:/bin'}, cwd=workspace.work, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn('TURN_COMPLETE', result.stdout)
                        print(f'NATIVE_EJECT_CYCLE cycle={cycle}', flush=True)
                    self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
