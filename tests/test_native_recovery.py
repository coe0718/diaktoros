"""Durable receipts authorize disposal, never signals to recorded PIDs."""
import _home_guard  # noqa: F401
import json
from contextlib import closing
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from diaktoros import contained, native_anchor, native_lifecycle, native_recovery, native_storage, run_supervisor, seatbelt


class RecoveryValidation(unittest.TestCase):
    def test_registered_preparation_survives_process_death_at_each_boundary(self):
        source = str(Path(native_recovery.__file__).resolve().parent.parent)
        for phase in ('registered', 'partial-image', 'created', 'attached'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve() / 'dk-volume-owned'
                root.mkdir(mode=0o700)
                code = '''
import os,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from diaktoros import native_recovery
root, phase = Path(sys.argv[2]), sys.argv[3]
w = SimpleNamespace(root=root, image=root/'turn.dmg', size_mib=64)
r = native_recovery.register(w)
if phase != 'registered':
    (root/'mount').mkdir(mode=0o700)
    w.image.write_bytes(b'partial or completed fixture image')
    w.image.chmod(0o600)
if phase in ('created','attached'):
    r.update('created', image_identity=native_recovery._identity(w.image.lstat()))
if phase == 'attached':
    r.update('attached')
os._exit(91)
'''
                child = subprocess.run([sys.executable, '-I', '-B', '-c', code, source, str(root), phase],
                                       capture_output=True, text=True, timeout=10)
                self.assertEqual(child.returncode, 91, child.stderr)
                with native_recovery.Record(root) as receipt:
                    record = receipt.read()
                self.assertEqual(record['state'], 'registered' if phase == 'partial-image' else phase)
                self.assertEqual(record['image_path'], str(root / 'turn.dmg'))
                self.assertEqual(record['reserved_bytes'], 64 * 1024 * 1024)
                with mock.patch.object(native_storage.Workspace, '_detach_owned') as detach:
                    result = native_recovery.startup(root.parent)
                self.assertEqual(result[0]['status'], 'retained')
                self.assertIn('creator completion is unverified', result[0]['reason'])
                detach.assert_not_called()

    def test_durable_workspace_registers_before_create_and_holds_lease_until_arm(self):
        workspace = native_storage.Workspace(64, durable_ownership=True)
        turn_ids = []
        original_open, original_unlink = Path.open, Path.unlink
        def fixture_path(path):
            # This test has no mounted image. Simulate its distinct case names
            # without depending on the host temporary filesystem's case mode.
            if path.parent == workspace.mount and path.name in ('CaseProbe', 'caseprobe'):
                return path.with_name('case-upper' if path.name == 'CaseProbe' else 'case-lower')
            return path
        def fixture_open(path, *args, **kwargs):
            return original_open(fixture_path(path), *args, **kwargs)
        def fixture_unlink(path, *args, **kwargs):
            return original_unlink(fixture_path(path), *args, **kwargs)
        def create():
            self.assertFalse(workspace.image.exists())
            record = workspace._preparation.read()
            self.assertEqual(record['state'], 'registered')
            turn_ids.append(record['turn_id'])
            self.assertEqual(native_recovery.recover(workspace.root)['status'], 'active')
            workspace.image.write_bytes(b'fixture')
        with mock.patch.object(native_storage.sys, 'platform', 'darwin'), \
                mock.patch.object(Path, 'is_file', return_value=True), \
                mock.patch.object(workspace, '_create_image', side_effect=create), \
                mock.patch.object(workspace, '_verify_mount'), \
                mock.patch.object(Path, 'open', autospec=True, side_effect=fixture_open), \
                mock.patch.object(Path, 'unlink', autospec=True, side_effect=fixture_unlink), \
                mock.patch.object(native_storage, '_run', return_value=b'<?xml version="1.0"?>'
                                  b'<plist version="1.0"><dict><key>Format</key><string>UDRW</string></dict></plist>'):
            workspace.__enter__()
        try:
            self.assertEqual(workspace._preparation.read()['state'], 'attached')
            self.assertEqual(native_recovery.recover(workspace.root)['status'], 'active')
            native_recovery.arm(workspace)
            with native_recovery.Record(workspace.root) as receipt:
                record = receipt.read()
                self.assertEqual(record['state'], 'armed')
                self.assertEqual(record['turn_id'], turn_ids[0])
                receipt.update('drained')
            with mock.patch.object(native_storage.Workspace, '_detach_owned'), \
                    mock.patch.object(native_storage.Workspace, '_verify_detached'):
                workspace._cleanup()
            self.assertFalse(workspace.root.exists())
        finally:
            if workspace._preparation is not None:
                workspace._preparation.close()
            if workspace.root.exists():
                import shutil
                shutil.rmtree(workspace.root)

    def test_registration_write_failure_never_starts_image_creation(self):
        workspace = native_storage.Workspace(64, durable_ownership=True)
        with mock.patch.object(native_storage.sys, 'platform', 'darwin'), \
                mock.patch.object(Path, 'is_file', return_value=True), \
                mock.patch.object(native_recovery.Record, 'write', side_effect=OSError('disk full')), \
                mock.patch.object(workspace, '_create_image') as create, \
                mock.patch.object(workspace, '_detach_owned'), \
                mock.patch.object(workspace, '_verify_detached'):
            with self.assertRaisesRegex(OSError, 'disk full'):
                workspace.__enter__()
        create.assert_not_called()

    def test_strict_launcher_rejects_late_registered_workspaces_before_staging(self):
        from diaktoros import native_macos
        paths = {name: Path('/unused') for name in
                 ('code', 'venv', 'runtime', 'rust', 'home', 'work', 'export', 'client',
                  'scratch', 'query', 'inference_socket', 'broker_socket')}
        with mock.patch.object(seatbelt, 'unavailable', return_value=None):
            with self.assertRaisesRegex(contained.ContainmentUnavailable, 'before image creation'):
                native_macos.run(**paths, model='fixture', role='reviewer',
                                 workspace=native_storage.Workspace(64), spawn_adapter=Path('/adapter'))

    def fixture(self, root):
        root.chmod(0o700)
        image = root / 'turn.dmg'
        image.write_bytes(b'private image')
        image.chmod(0o600)
        (root / 'mount').mkdir()
        workspace = SimpleNamespace(root=root, image=image, size_mib=64)
        native_recovery.arm(workspace)
        return workspace

    def test_unlocked_running_record_never_authorizes_cleanup_or_pid_signals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = self.fixture(root)
            with native_recovery.Record(root) as receipt:
                receipt.update('running', leader=123, watchdog=456, guardian=789)
            with mock.patch.object(native_storage.Workspace, '_detach_owned') as detach, \
                    mock.patch('os.kill') as kill, mock.patch('os.killpg') as killpg:
                result = native_recovery.recover(root)
            self.assertEqual(result['status'], 'retained')
            self.assertTrue(workspace.image.exists())
            detach.assert_not_called()
            kill.assert_not_called()
            killpg.assert_not_called()

    def test_active_guardian_lease_blocks_restart_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.fixture(root)
            with native_recovery.Record(root), \
                    mock.patch.object(native_storage.Workspace, '_detach_owned') as detach:
                self.assertEqual(native_recovery.recover(root)['status'], 'active')
            detach.assert_not_called()

    def test_restart_removes_drained_image_only_after_verified_detachment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'owned'
            root.mkdir()
            workspace = self.fixture(root)
            with native_recovery.Record(root) as receipt:
                receipt.update('running', leader=123)
                receipt.update('drained', cause='watchdog_lost')
            with mock.patch.object(native_storage.Workspace, '_detach_owned') as detach, \
                    mock.patch.object(native_storage.Workspace, '_verify_detached') as verify:
                self.assertEqual(native_recovery.reconcile([root])[0]['status'], 'removed')
            detach.assert_called_once()
            verify.assert_called_once()
            self.assertFalse(workspace.image.exists())

    def test_failed_detachment_retains_drained_receipt_for_later_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = self.fixture(root)
            with native_recovery.Record(root) as receipt:
                receipt.update('drained')
            with mock.patch.object(native_storage.Workspace, '_detach_owned',
                                   side_effect=native_storage.StorageError('busy')):
                self.assertEqual(native_recovery.recover(root)['status'], 'retained')
            with native_recovery.Record(root) as receipt:
                self.assertEqual(receipt.read()['state'], 'drained')
            self.assertTrue(workspace.image.exists())

    def test_replaced_image_and_malformed_or_symlink_journal_are_retained(self):
        for corruption in ('image', 'invalid', 'symlink'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                workspace = self.fixture(root)
                with native_recovery.Record(root) as receipt:
                    receipt.update('drained')
                journal = root / native_recovery.JOURNAL
                if corruption == 'image':
                    workspace.image.rename(root / 'old-image')
                    workspace.image.write_bytes(b'replacement')
                    workspace.image.chmod(0o600)
                elif corruption == 'invalid':
                    journal.write_text('{}')
                else:
                    journal.rename(root / 'old-journal')
                    journal.symlink_to(root / 'old-journal')
                with mock.patch.object(native_storage.Workspace, '_detach_owned') as detach:
                    self.assertEqual(native_recovery.recover(root)['status'], 'retained')
                detach.assert_not_called()
                self.assertTrue(workspace.image.exists())

    def test_arming_is_durable_before_launch_and_cannot_be_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(native_recovery.os, 'fsync') as sync:
                workspace = self.fixture(root)
            self.assertEqual(sync.call_count, 2)
            self.assertEqual(json.loads((root / native_recovery.JOURNAL).read_text())['state'], 'armed')
            with self.assertRaises(native_recovery.RecoveryError):
                native_recovery.arm(workspace)

    def test_receipt_cannot_regress_to_running_after_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.fixture(root)
            with native_recovery.Record(root) as receipt:
                receipt.update('drained')
                with self.assertRaises(native_recovery.RecoveryError):
                    receipt.update('running', leader=999)

    def test_unknown_private_root_is_retained_without_creating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(native_recovery.recover(root)['status'], 'retained')
            self.assertEqual(list(root.iterdir()), [])

    def test_discovery_rejects_special_ownership_files_before_opening_them(self):
        for filename in (native_recovery.JOURNAL, native_recovery.LEASE):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory).resolve()
                root = parent / 'dk-volume-fifo'
                root.mkdir()
                self.fixture(root)
                (root / filename).unlink()
                os.mkfifo(root / filename, 0o600)
                original_open = native_recovery.os.open

                def safe_open(path, *args, **kwargs):
                    self.assertNotEqual(path, filename, 'opened a special ownership file')
                    return original_open(path, *args, **kwargs)

                with mock.patch.object(native_recovery.os, 'open', side_effect=safe_open):
                    self.assertEqual(native_recovery.startup(parent)[0]['status'], 'retained')

    def test_discovery_recovers_only_verified_roots_and_repeated_restart_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            drained, unknown, active, unrelated = (parent / name for name in
                ('dk-volume-drained', 'dk-volume-unknown', 'dk-volume-active', 'other-project'))
            for root in (drained, unknown, active, unrelated):
                root.mkdir()
            self.fixture(drained)
            self.fixture(active)
            with native_recovery.Record(drained) as receipt:
                receipt.update('drained')
            symlink = parent / 'dk-volume-symlink'
            symlink.symlink_to(drained, target_is_directory=True)
            with native_recovery.Record(active), \
                    mock.patch.object(native_storage.Workspace, '_detach_owned'), \
                    mock.patch.object(native_storage.Workspace, '_verify_detached'):
                results = {Path(r['root']).name: r['status'] for r in native_recovery.startup(parent)}
                self.assertEqual(results, {'dk-volume-drained': 'removed', 'dk-volume-active': 'active',
                                          'dk-volume-unknown': 'retained', 'dk-volume-symlink': 'retained'})
                second = native_recovery.startup(parent)
                self.assertNotIn(str(drained), [r['root'] for r in second])
            self.assertEqual(list(unknown.iterdir()), [])
            self.assertEqual(list(unrelated.iterdir()), [])
            self.assertTrue(symlink.is_symlink())

    def test_disposed_receipt_survives_partial_directory_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'dk-volume-partial'
            root.mkdir()
            workspace = self.fixture(root)
            with native_recovery.Record(root) as receipt:
                receipt.update('drained')
                receipt.update('disposed')
            workspace.image.unlink()
            (root / 'mount').rmdir()
            with mock.patch.object(native_storage.Workspace, '_devices', return_value=[]) as devices:
                self.assertEqual(native_recovery.recover(root)['status'], 'removed')
            devices.assert_called_once()
            self.assertEqual(native_recovery.recover(root)['status'], 'absent')

    def test_discovery_is_bounded_and_does_not_follow_candidate_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            for number in range(4):
                (parent / f'dk-volume-{number}').mkdir()
            with mock.patch.object(native_recovery, 'MAX_WORKSPACES', 2):
                results = native_recovery.startup(parent)
            self.assertEqual(len(results), 3)
            self.assertIn('limit', results[0]['reason'])
            self.assertTrue(all(r['status'] == 'retained' for r in results))

    def test_supervisor_recovery_scans_on_mac_host_only_before_ledger_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            supervisor = run_supervisor.Supervisor(root / 'runs.db', native_workspace_parent=root)
            with mock.patch.object(run_supervisor.sys, 'platform', 'darwin'), \
                    mock.patch.object(native_recovery, 'startup', return_value=[]) as startup:
                self.assertEqual(supervisor.recover(), run_supervisor.SILENT)
            startup.assert_called_once_with(root)
            worker = run_supervisor.Supervisor(root / 'runs.db', create=False,
                                               native_workspace_parent=root)
            with mock.patch.object(run_supervisor.sys, 'platform', 'darwin'), \
                    mock.patch.object(native_recovery, 'startup') as startup:
                worker.recover()
            startup.assert_not_called()
            with mock.patch.object(run_supervisor.sys, 'platform', 'linux'), \
                    mock.patch.object(native_recovery, 'startup') as startup:
                supervisor.recover()
            startup.assert_not_called()

    def anchor_proof(self, events):
        proof = object.__new__(native_lifecycle._AnchorProof)
        proof.pid = 456
        proof.events = mock.Mock()
        proof.events.control.side_effect = events
        return proof

    def test_live_anchor_exit_and_empty_group_authorize_only_matching_turn_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'owned'
            root.mkdir()
            self.fixture(root)
            with native_recovery.Record(root) as receipt:
                turn = receipt.read()['turn_id']
                receipt.update('running', leader=123)
            event = SimpleNamespace(filter=1, ident=456)
            proof = self.anchor_proof([[event], []])
            with mock.patch.object(select, 'KQ_FILTER_PROC', 1, create=True), \
                    mock.patch.object(native_lifecycle, '_group_members', side_effect=[{789}, set()]) as members, \
                    mock.patch.object(native_storage.Workspace, '_detach_owned'), \
                    mock.patch.object(native_storage.Workspace, '_verify_detached'), \
                    mock.patch('os.kill') as kill, mock.patch('os.killpg') as killpg:
                with self.assertRaises(native_lifecycle.GuardianLost):
                    proof.recover(root, turn)
            self.assertFalse(root.exists())
            self.assertEqual(members.call_args_list, [mock.call(456)] * 2)
            kill.assert_not_called()
            killpg.assert_not_called()

    def test_anchor_recovery_retains_missing_exit_live_members_and_changed_turn(self):
        for failure in ('missing_exit', 'live_members', 'changed_turn'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                self.fixture(root)
                with native_recovery.Record(root) as receipt:
                    turn = receipt.read()['turn_id']
                    receipt.update('running', leader=123)
                event = SimpleNamespace(filter=1, ident=456)
                proof = self.anchor_proof([[] if failure == 'missing_exit' else [event]])
                with mock.patch.object(select, 'KQ_FILTER_PROC', 1, create=True), \
                        mock.patch.object(native_lifecycle.time, 'monotonic', side_effect=[0, 6, 6]), \
                        mock.patch.object(native_lifecycle, '_group_members',
                                          return_value={789} if failure == 'live_members' else set()), \
                        mock.patch.object(native_storage.Workspace, '_detach_owned') as detach:
                    with self.assertRaises(native_lifecycle.CleanupIncomplete):
                        proof.recover(root, 'other-turn' if failure == 'changed_turn' else turn)
                detach.assert_not_called()
                self.assertTrue(root.exists())


class NativeRecoveryBoundary(unittest.TestCase):
    def setUp(self):
        reason = seatbelt.unavailable()
        if reason:
            if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_STORAGE') == '1':
                self.fail(reason)
            self.skipTest(reason)

    def command(self, workspace, outer, *, bounded=False, descendants=False):
        interpreter = Path(sys.executable).resolve()
        framework = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
        if framework.is_file():
            interpreter = framework.resolve()
        profile = seatbelt.profile(read_roots=(Path(sys.base_prefix),),
                                   write_roots=(workspace.work,))
        policy = outer / 'profile.sb'
        policy.write_text(profile.text + '(allow syscall-unix)\n'
                          '(deny syscall-unix (syscall-number 82 147 244))\n')
        script = '''
import os, time
from pathlib import Path
with Path('held-open').open('wb') as held:
    held.write(b'owned file'); held.flush()
    for fd in (0, 1, 2): os.close(fd)
    Path('ready.tmp').write_text(str(os.getpid()))
    Path('ready.tmp').rename('ready')
    deadline = time.monotonic() + 15
    while not Path('stop').exists() and time.monotonic() < deadline:
        time.sleep(0.02)
''' if bounded else 'raise SystemExit(7)'
        if descendants:
            script = script.replace("    Path('ready.tmp').write_text(str(os.getpid()))", '''
    factory = os.fork()
    if factory == 0:
        if os.fork() != 0: os._exit(0)
        Path('descendant').write_text(str(os.getpid()))
        deadline = time.monotonic() + 15
        while not Path('stop').exists() and time.monotonic() < deadline: time.sleep(0.02)
        os._exit(0)
    os.waitpid(factory, 0)
    while not Path('descendant').exists(): time.sleep(0.01)
    Path('ready.tmp').write_text(str(os.getpid()))''')
        return profile.command(policy, [str(interpreter), '-I', '-B', '-c', script])

    def ready(self, workspace):
        path = workspace.work / 'ready'
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), 'bounded turn did not become ready')
        return int(path.read_text())

    def observe(self, events, pid):
        events.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                      flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                      fflags=select.KQ_NOTE_EXIT)], 0, 0)

    def test_supervisor_restarts_recover_partial_unmount_and_interrupted_directory_removal(self):
        source = str(Path(native_recovery.__file__).resolve().parent.parent)
        for interruption in ('partial_unmount', 'partial_delete'):
            with self.subTest(interruption=interruption), \
                    tempfile.TemporaryDirectory(prefix='dk-startup-', dir='/tmp') as directory:
                outer = Path(directory).resolve()
                inventory = outer / 'inventory'
                inventory.mkdir(mode=0o700)
                original_mkdtemp = tempfile.mkdtemp
                with mock.patch.object(native_storage.tempfile, 'mkdtemp',
                                       side_effect=lambda **kwargs: original_mkdtemp(
                                           prefix=kwargs['prefix'], dir=inventory)):
                    workspace = native_storage.Workspace(64, durable_ownership=True).__enter__()
                root = workspace.root
                native_recovery.arm(workspace)
                argv = self.command(workspace, outer)
                # A real owner finishes a guarded turn, then crashes before
                # context cleanup. The partial-unmount variant crashes after
                # diskutil removed the mount but left the image attached.
                owner_code = ('import os,sys,subprocess; from pathlib import Path; '
                              f'sys.path.insert(0,{source!r}); '
                              'from diaktoros import native_lifecycle; '
                              f'native_lifecycle.capture({argv!r}, env={{"PATH":"/usr/bin:/bin"}}, '
                              f'timeout=5,cwd=Path({str(workspace.work)!r}),recovery_root=Path({str(root)!r})); ')
                if interruption == 'partial_unmount':
                    owner_code += (f'subprocess.run(["/usr/sbin/diskutil","unmount",'
                                   f'{str(workspace.mount)!r}],check=True,stdout=subprocess.DEVNULL); ')
                owner_code += 'os.kill(os.getpid(),9)'
                try:
                    owner = subprocess.run([sys.executable, '-I', '-B', '-c', owner_code],
                                           capture_output=True, text=True, timeout=30)
                    self.assertEqual(owner.returncode, -9, owner.stderr)
                    self.assertTrue(workspace._devices())
                    if interruption == 'partial_unmount':
                        self.assertEqual(workspace.mount.stat().st_dev, root.stat().st_dev)
                    prefix = ('import os,sys,json; from pathlib import Path; '
                              f'sys.path.insert(0,{source!r}); '
                              'from diaktoros import native_recovery,run_supervisor; ')
                    restart = (f'sup=run_supervisor.Supervisor(Path({str(outer / "runs.db")!r}),'
                               f'native_workspace_parent=Path({str(inventory)!r})); '
                               'sup.recover(); print(json.dumps(sup.native_recovery_results))')
                    if interruption == 'partial_delete':
                        # Kill the real fresh supervisor after its disposed
                        # receipt is durable, while owned directory deletion is
                        # underway. The next restart must not need the image.
                        injection = ('\ndef crash_remove(path):\n'
                                     f'    Path({str(workspace.image)!r}).unlink(missing_ok=True)\n'
                                     '    os.kill(os.getpid(),9)\n'
                                     'native_recovery.shutil.rmtree=crash_remove\n')
                        interrupted = subprocess.run([sys.executable, '-I', '-B', '-c',
                                                      prefix + injection + restart],
                                                     capture_output=True, text=True, timeout=120)
                        self.assertEqual(interrupted.returncode, -9, interrupted.stderr)
                        self.assertFalse(workspace.image.exists())
                        with native_recovery.Record(root) as receipt:
                            self.assertEqual(receipt.read()['state'], 'disposed')
                    for attempt in range(2):
                        restarted = subprocess.run([sys.executable, '-I', '-B', '-c', prefix + restart],
                                                   capture_output=True, text=True, timeout=120)
                        self.assertEqual(restarted.returncode, 0, restarted.stderr)
                        results = json.loads(restarted.stdout)
                        self.assertEqual(results, ([{'root': str(root), 'status': 'removed'}]
                                                   if attempt == 0 else []))
                    self.assertFalse(root.exists())
                    print(f'NATIVE_STARTUP interruption={interruption} restarts=2 removed=true', flush=True)
                finally:
                    if root.exists():
                        native_recovery.recover(root)

    def test_restart_reconciles_drained_receipt_without_old_python_objects(self):
        with tempfile.TemporaryDirectory(prefix='dk-restart-', dir='/tmp') as directory:
            outer = Path(directory).resolve()
            workspace = native_storage.Workspace(64, durable_ownership=True).__enter__()
            root = workspace.root
            native_recovery.arm(workspace)
            try:
                result = native_lifecycle.capture(self.command(workspace, outer),
                                                  env={'PATH': '/usr/bin:/bin'}, timeout=5,
                                                  cwd=workspace.work, recovery_root=root)
                self.assertEqual(result.returncode, 7)
                with native_recovery.Record(root) as receipt:
                    self.assertEqual(receipt.read()['state'], 'drained')
                # A fresh process has only the retained path, not cached PIDs,
                # disk IDs, locks or Workspace state from the original launcher.
                code = ('import sys,json; from pathlib import Path; '
                        'sys.path.insert(0,sys.argv[1]); from diaktoros import native_recovery; '
                        'print(json.dumps(native_recovery.reconcile([Path(sys.argv[2])])))')
                recovered = subprocess.run([sys.executable, '-I', '-B', '-c', code,
                                            str(Path(native_recovery.__file__).resolve().parent.parent),
                                            str(root)], capture_output=True, text=True, timeout=120)
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                self.assertEqual(json.loads(recovered.stdout)[0]['status'], 'removed')
                self.assertFalse(root.exists())
                print('NATIVE_RECOVERY fresh_process=removed drained_receipt=true', flush=True)
            finally:
                if root.exists():
                    native_recovery.recover(root)

    def test_guardian_automatically_cleans_after_watchdog_sigkill(self):
        with tempfile.TemporaryDirectory(prefix='dk-guardian-', dir='/tmp') as directory, \
                closing(select.kqueue()) as events:
            outer = Path(directory).resolve()
            workspace = native_storage.Workspace(64, durable_ownership=True).__enter__()
            root = workspace.root
            native_recovery.arm(workspace)
            argv = self.command(workspace, outer, bounded=True)
            trigger = outer / 'kill-monitor'
            # Inject failure in the actual monitor process: it signals itself,
            # so a journal PID can never be mistaken for a new unrelated PID.
            helper = outer / 'helper.py'
            helper.write_text(
                'import os,sys,time\nfrom pathlib import Path\n'
                f'sys.path.insert(0, {str(Path(native_lifecycle.__file__).resolve().parent.parent)!r})\n'
                'from diaktoros import native_lifecycle as life\n'
                'life.__file__ = __file__\n'
                'if sys.argv[1] == "--sentinel":\n'
                '    deadline = time.monotonic() + 20\n'
                f'    while not Path({str(trigger)!r}).exists() and time.monotonic() < deadline:\n'
                '        time.sleep(0.02)\n'
                '    os.kill(os.getpid(), 9)\n'
                'else:\n    life._watch(*(int(value) for value in sys.argv[1:]))\n')
            original = contained.capture_process
            child = group = None

            def fail_monitor(guardian, **kwargs):
                nonlocal child, group
                child = self.ready(workspace)
                group = os.getpgid(child)
                self.observe(events, child)
                self.assertEqual(native_recovery.recover(root)['status'], 'active')
                trigger.touch()
                return original(guardian, **kwargs)

            try:
                with mock.patch.object(native_lifecycle, '__file__', str(helper)), \
                        mock.patch.object(contained, 'capture_process', side_effect=fail_monitor):
                    with self.assertRaises(native_lifecycle.WatchdogLost):
                        native_lifecycle.capture(argv, env={'PATH': '/usr/bin:/bin'}, timeout=10,
                                                 cwd=workspace.work, recovery_root=root)
                self.assertTrue(any(event.ident == child for event in events.control(None, 1, 5)))
                self.assertFalse(native_lifecycle._group_members(group))
                self.assertFalse(root.exists())
                print('NATIVE_RECOVERY watchdog_sigkill=cleaned group_survivors=0', flush=True)
            finally:
                if root.exists():
                    native_recovery.recover(root)

    def test_supervisor_sigkill_leaves_guardian_to_drain_and_dispose_image(self):
        with tempfile.TemporaryDirectory(prefix='dk-owner-death-', dir='/tmp') as directory, \
                closing(select.kqueue()) as events:
            outer = Path(directory).resolve()
            workspace = native_storage.Workspace(64, durable_ownership=True).__enter__()
            root = workspace.root
            native_recovery.arm(workspace)
            argv = self.command(workspace, outer, bounded=True)
            script = ('import sys; from pathlib import Path; '
                      f'sys.path.insert(0,{str(Path(native_lifecycle.__file__).resolve().parent.parent)!r}); '
                      'from diaktoros import native_lifecycle; '
                      f'native_lifecycle.capture({argv!r}, env={{"PATH":"/usr/bin:/bin"}}, '
                      f'timeout=10, cwd=Path({str(workspace.work)!r}), recovery_root=Path({str(root)!r}))')
            owner = subprocess.Popen([sys.executable, '-I', '-B', '-c', script],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env={'PATH': '/usr/bin:/bin'})
            try:
                child = self.ready(workspace)
                group = os.getpgid(child)
                self.observe(events, child)
                owner.kill()  # Actual unreaped child of this test, not a saved PID.
                owner.wait(timeout=5)
                self.assertTrue(any(event.ident == child for event in events.control(None, 1, 10)))
                deadline = time.monotonic() + 45
                while root.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(root.exists(), 'guardian retained storage after owner death')
                self.assertFalse(native_lifecycle._group_members(group))
                print('NATIVE_RECOVERY supervisor_sigkill=cleaned group_survivors=0', flush=True)
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.wait(timeout=5)
                if root.exists():
                    native_recovery.recover(root)

    def test_guardian_sigkill_anchor_stops_turn_and_host_verifies_disposal(self):
        with tempfile.TemporaryDirectory(prefix='dk-guard-loss-', dir='/tmp') as directory, \
                closing(select.kqueue()) as events:
            outer = Path(directory).resolve()
            workspace = native_storage.Workspace(64, durable_ownership=True).__enter__()
            root = workspace.root
            native_recovery.arm(workspace)
            argv = self.command(workspace, outer, bounded=True, descendants=True)
            original = contained.capture_process
            child = group = descendant = None

            def fail_guardian(guardian, **kwargs):
                nonlocal child, group, descendant
                child = self.ready(workspace)
                group = os.getpgid(child)
                self.assertNotEqual(group, child, 'seat must inherit the trusted anchor group')
                self.observe(events, child)
                descendant = int((workspace.work / 'descendant').read_text())
                self.observe(events, descendant)
                self.assertIn(descendant, native_lifecycle._group_members(group))
                guardian.kill()  # This Popen owns the unreaped guardian PID.
                return original(guardian, **kwargs)

            try:
                with mock.patch.object(contained, 'capture_process', side_effect=fail_guardian):
                    with self.assertRaises(native_lifecycle.GuardianLost):
                        native_lifecycle.capture(argv, env={'PATH': '/usr/bin:/bin'}, timeout=10,
                                                 cwd=workspace.work, recovery_root=root)
                exits = {event.ident for event in events.control(None, 2, 5)}
                self.assertEqual(exits, {child, descendant})
                self.assertFalse(native_lifecycle._group_members(group))
                self.assertFalse(root.exists())
                self.assertEqual(native_recovery.recover(root)['status'], 'absent')
                print('NATIVE_RECOVERY guardian_sigkill=cleaned anchor_group_survivors=0', flush=True)
            finally:
                if root.exists() and child is not None:
                    (workspace.work / 'stop').touch()
                    self.assertTrue(any(event.ident == child
                                        for event in events.control(None, 1, 20)))
                    deadline = time.monotonic() + 5
                    while native_lifecycle._group_members(group) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertFalse(native_lifecycle._group_members(group),
                                     f'fixture still owns retained storage at {root}')
                    with native_recovery.Record(root) as receipt:
                        receipt.update('drained', cause='test_fixture_exit_proof')
                    self.assertEqual(native_recovery.recover(root)['status'], 'removed')

    def test_guardian_and_anchor_death_retains_live_turn_without_saved_pid_signals(self):
        with tempfile.TemporaryDirectory(prefix='dk-double-loss-', dir='/tmp') as directory, \
                closing(select.kqueue()) as events:
            outer = Path(directory).resolve()
            workspace = native_storage.Workspace(64, durable_ownership=True).__enter__()
            root = workspace.root
            native_recovery.arm(workspace)
            trigger = outer / 'kill-anchor'
            anchor = outer / 'anchor.py'
            anchor.write_text(
                'import os,sys,threading,time\n'
                'from pathlib import Path\n'
                f'sys.path.insert(0,{str(Path(native_lifecycle.__file__).resolve().parent.parent)!r})\n'
                'from diaktoros import native_anchor\n'
                'def fail():\n'
                f'    while not Path({str(trigger)!r}).exists(): time.sleep(0.01)\n'
                '    os.kill(os.getpid(),9)\n'
                'threading.Thread(target=fail,daemon=True).start()\n'
                'raise SystemExit(native_anchor.run(*(int(v) for v in sys.argv[1:])))\n')
            helper = outer / 'helper.py'
            helper.write_text(
                'import sys\n'
                f'sys.path.insert(0,{str(Path(native_lifecycle.__file__).resolve().parent.parent)!r})\n'
                'from diaktoros import native_anchor,native_lifecycle as life\n'
                f'native_anchor.__file__={str(anchor)!r}\n'
                'if sys.argv[1]=="--sentinel": life._sentinel(*(int(v) for v in sys.argv[2:]))\n'
                'else: life._watch(*(int(v) for v in sys.argv[1:]))\n')
            original = contained.capture_process
            child = group = None

            def fail_both(guardian, **kwargs):
                nonlocal child, group
                child = self.ready(workspace)
                group = os.getpgid(child)
                self.observe(events, child)
                # Freeze the actual unreaped guardian before the anchor kills
                # itself, preventing its ordinary drain from winning this race.
                with closing(select.kqueue()) as anchor_events:
                    self.observe(anchor_events, group)
                    guardian.send_signal(signal.SIGSTOP)
                    trigger.touch()
                    self.assertTrue(any(e.ident == group for e in anchor_events.control(None, 1, 5)))
                    guardian.kill()
                return original(guardian, **kwargs)

            try:
                with mock.patch.object(native_lifecycle, '__file__', str(helper)), \
                        mock.patch.object(contained, 'capture_process', side_effect=fail_both):
                    with self.assertRaises(native_lifecycle.CleanupIncomplete):
                        native_lifecycle.capture(self.command(workspace, outer, bounded=True),
                                                 env={'PATH': '/usr/bin:/bin'}, timeout=10,
                                                 cwd=workspace.work, recovery_root=root)
                self.assertEqual(native_recovery.recover(root)['status'], 'retained')
                self.assertIn(child, native_lifecycle._group_members(group))
                self.assertTrue(workspace.image.exists())
                print('NATIVE_RECOVERY guardian_and_anchor_sigkill=retained unknown_turn=true', flush=True)
            finally:
                if root.exists() and child is not None:
                    (workspace.work / 'stop').touch()
                    self.assertTrue(any(e.ident == child for e in events.control(None, 1, 20)))
                    deadline = time.monotonic() + 5
                    while native_lifecycle._group_members(group) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertFalse(native_lifecycle._group_members(group))
                    with native_recovery.Record(root) as receipt:
                        receipt.update('drained', cause='test_fixture_exit_proof')
                    self.assertEqual(native_recovery.recover(root)['status'], 'removed')
