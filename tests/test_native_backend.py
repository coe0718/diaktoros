"""Production native launch: real Seatbelt/APFS, no live services or credentials."""
import _home_guard  # noqa: F401
from pathlib import Path
import os
import plistlib
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from diaktoros import contained, native_backend, native_cleanup


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS backend')
class NativeBackend(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'DIAKTOROS_NATIVE_MACOS': '1'}))
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(self.cleanup)
        self.paths = {name: self.root / name for name in ('code', 'venv', 'home', 'checkout', 'rust')}
        for path in self.paths.values():
            path.mkdir()
        (self.paths['venv'] / 'bin').mkdir()
        self.python = Path(sys.executable).resolve()
        framework = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
        if framework.is_file():
            self.python = framework.resolve()
        (self.paths['venv'] / 'bin/python').symlink_to(self.python)
        self.paths['runtime'] = Path(sys.base_prefix).resolve()
        self.paths['query'] = self.root / 'query'
        self.paths['query'].write_text('fixture')

    def cleanup(self):
        if (self.root / 'pool').exists():
            swept = native_cleanup.bounded_sweep(self.root / 'pool')
            self.assertEqual(swept['status'], 'completed', swept)
        info = plistlib.loads(subprocess.check_output(['/usr/bin/hdiutil', 'info', '-plist']))
        attached = [item['image-path'] for item in info.get('images', [])
                    if Path(item.get('image-path', '/')).is_relative_to(self.root)]
        self.assertFalse(attached, f'retain attached fixture evidence: {attached}')
        shutil.rmtree(self.root)

    def launch(self, source, workspace, **kwargs):
        return native_backend.run(**self.paths, workspace=workspace,
            entry=['/opt/venv/bin/python', '-I', '-B', '-c', source], timeout=10, **kwargs)

    def test_network_request_is_refused_before_workspace_creation(self):
        with mock.patch.object(native_backend, 'workspace') as create:
            with self.assertRaises(contained.ContainmentUnavailable):
                native_backend.run(**self.paths, entry=['/usr/bin/true'], network=True)
        create.assert_not_called()

    def test_native_dispatch_requires_explicit_host_opt_in(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(native_backend, 'workspace', side_effect=AssertionError('disabled admission')) as create:
            with self.assertRaisesRegex(contained.ContainmentUnavailable, 'opt-in'):
                native_backend.run(**self.paths, entry=['/usr/bin/true'])
        create.assert_not_called()

    def test_disabled_turn_is_refused_before_staging_or_capabilities(self):
        from diaktoros import trusted_turn
        scope = mock.Mock(repo='owner/repo', role='reviewer')
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(trusted_turn, '_stage_source') as stage, \
                mock.patch.object(trusted_turn.inference_proxy, 'InferenceCapability') as inference:
            with self.assertRaisesRegex(trusted_turn.TurnDenied, 'opt-in'):
                trusted_turn.run_turn({'repo': scope.repo}, scope,
                    source=self.paths['code'], venv=self.paths['venv'], runtime=self.paths['runtime'],
                    rust=self.paths['rust'], upstream='http://127.0.0.1:9', key='dummy', model='fixture', prompt='test')
        stage.assert_not_called()
        inference.assert_not_called()

    def test_broad_read_roots_are_rechecked_before_workspace_creation(self):
        alias = self.root / 'broad-alias'
        alias.symlink_to('/tmp', target_is_directory=True)
        for name in ('code', 'venv', 'runtime', 'home', 'checkout', 'rust'):
            with self.subTest(name=name), \
                    mock.patch.object(native_backend, 'workspace', side_effect=AssertionError('unsafe admission')) as create:
                paths = {**self.paths, name: alias}
                with self.assertRaisesRegex(contained.ContainmentUnavailable, 'broad'):
                    native_backend.run(**paths, entry=['/usr/bin/true'])
                create.assert_not_called()

    def test_real_launch_preserves_result_and_denies_secrets_network_and_detach(self):
        secret = self.root / 'secret'
        secret.write_text('host-secret')
        (self.paths['checkout'] / 'input').write_text('source')
        source = f'''
import os, socket
from pathlib import Path
assert Path('input').read_text() == 'source'
Path('output').write_text('bounded')
for action in (lambda: Path({str(secret)!r}).read_text(),
               lambda: socket.socket().connect(('127.0.0.1', 9)), os.setsid):
    try: action()
    except PermissionError: pass
    else: raise AssertionError('forbidden operation succeeded')
assert 'GITHUB_TOKEN' not in os.environ
child = os.posix_spawn('/usr/bin/true', ['true'], os.environ)
assert os.waitpid(child, 0)[1] == 0
try: os.posix_spawn('/usr/bin/true', ['true'], os.environ, setsid=True)
except PermissionError: pass
else: raise AssertionError('spawn escaped its owned group')
print('NATIVE_PRODUCTION_OK', flush=True)
raise SystemExit(7)
'''
        with native_backend.workspace(pool=self.root / 'pool', size_mib=64) as workspace:
            with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'must-not-reach-seat'}):
                result = self.launch(source, workspace)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertIn('NATIVE_PRODUCTION_OK', result.stdout)
            self.assertEqual((workspace.work / 'output').read_text(), 'bounded')
            self.assertFalse((self.paths['checkout'] / 'output').exists())
            image = workspace.image
        self.assertFalse(image.exists())

    def test_readonly_checkout_stays_readonly_and_scratch_is_writable(self):
        with native_backend.workspace(pool=self.root / 'pool', size_mib=64) as workspace:
            result = self.launch('''
import os
from pathlib import Path
try: Path('denied').write_text('no')
except PermissionError: pass
else: raise AssertionError('readonly checkout writable')
Path(os.environ['TMPDIR'], 'allowed').write_text('yes')
''', workspace, checkout_writable=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((workspace.scratch / 'allowed').read_text(), 'yes')

    def test_patch_manifest_reads_only_the_immutable_base_export(self):
        from diaktoros import broker_client
        package = self.paths['code'] / 'diaktoros'
        package.mkdir()
        (package / '__init__.py').touch()
        for name in ('broker_client.py', 'wire.py', 'envnames.py'):
            shutil.copyfile(Path(broker_client.__file__).with_name(name), package / name)
        turn = self.paths['code'] / 'turn.json'
        turn.write_text('{"head": "' + 'a' * 40 + '"}')
        (self.paths['checkout'] / 'deleted').write_text('original\n')
        (self.paths['checkout'] / 'large').write_text('before\n' * 12000)
        secret = self.root / 'not-an-export'
        secret.write_text('must-stay-private')
        source = f'''
import base64, os, sys
from pathlib import Path
sys.path.insert(0, {str(self.paths['code'])!r})
from diaktoros import broker_client
Path('deleted').unlink()
Path('large').write_text('after\\n' * 12000)
manifest = broker_client.build_manifest(['deleted', 'large'], 'change', turn_file={str(turn)!r})
patch = base64.b64decode(manifest['patch_b64'])
assert b'deleted file mode' in patch and b'-original' in patch
assert b'-before' in patch and b'+after' in patch
assert Path(os.environ['DIAKTOROS_EXPORT'], 'deleted').read_text() == 'original\\n'
for action in (lambda: Path(os.environ['DIAKTOROS_EXPORT'], 'deleted').write_text('bad'),
               lambda: Path({str(secret)!r}).read_text()):
    try: action()
    except PermissionError: pass
    else: raise AssertionError('base export widened read/write authority')
print('NATIVE_PATCH_OK')
'''
        with native_backend.workspace(pool=self.root / 'pool', size_mib=64) as workspace:
            result = self.launch(source, workspace)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('NATIVE_PATCH_OK', result.stdout)

    def test_cancel_before_launch_disposes_owned_preparation(self):
        with native_backend.workspace(pool=self.root / 'pool', size_mib=64) as workspace:
            image = workspace.image
            stop = threading.Event()
            stop.set()
            with self.assertRaises(contained.Stopped):
                self.launch("raise AssertionError('must not launch')", workspace, stop=stop)
        self.assertFalse(image.exists())

    def test_cancel_after_child_closes_output_still_stops_owned_group(self):
        stop = threading.Event()
        with native_backend.workspace(pool=self.root / 'pool', size_mib=64) as workspace:
            ready = workspace.work / 'ready'
            def cancel():
                import time
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                stop.set()
            thread = threading.Thread(target=cancel)
            thread.start()
            try:
                with self.assertRaises(contained.Stopped):
                    self.launch("import os,time; from pathlib import Path; "
                                "os.close(1); os.close(2); Path('ready').touch(); time.sleep(30)",
                                workspace, stop=stop)
            finally:
                thread.join(timeout=12)
                self.assertFalse(thread.is_alive())

    def test_legacy_argv_translation_does_not_rewrite_script_contents(self):
        with native_backend.workspace(pool=self.root / 'pool', size_mib=64) as workspace:
            result = self.launch("print('literal /opt/venv/bin/python /work')", workspace)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('literal /opt/venv/bin/python /work', result.stdout)

    def test_shell_preserves_adapter_for_private_tool_subprocesses(self):
        script = "import os; p=os.posix_spawn('/usr/bin/true',['true'],os.environ); assert os.waitpid(p,0)[1]==0; print('SPAWN_OK')"
        with native_backend.workspace(pool=self.root / 'pool', size_mib=64) as workspace:
            result = native_backend.run(**self.paths, workspace=workspace, timeout=10,
                entry=['/bin/sh', '-c', f'{shlex.quote(str(self.python))} -c {shlex.quote(script)}'])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('SPAWN_OK', result.stdout)

    def test_corefoundation_initializes_without_host_user_lookup(self):
        with native_backend.workspace(pool=self.root / 'pool', size_mib=64) as workspace:
            with mock.patch.dict(os.environ, {'__CF_USER_TEXT_ENCODING': 'host-value-must-not-leak'}):
                result = self.launch('''
import ctypes, os
assert os.environ['__CF_USER_TEXT_ENCODING'] == f'0x{os.getuid():X}:0:0'
ctypes.CDLL('/System/Library/Frameworks/Foundation.framework/Foundation')
print('FOUNDATION_READY')
''', workspace)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('FOUNDATION_READY', result.stdout)

    def test_framework_python_keeps_virtualenv_packages(self):
        if not (Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python').is_file():
            self.skipTest('Framework Python fixture')
        venv = self.paths['venv']
        (venv / 'bin/python').unlink()
        (venv / 'bin/python').symlink_to(Path(sys.executable).resolve())
        (venv / 'pyvenv.cfg').write_text(f'home = {Path(sys.base_prefix) / "bin"}\ninclude-system-site-packages = false\n')
        packages = venv / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
        packages.mkdir(parents=True)
        (packages / 'native_fixture.py').write_text('VALUE = "VENV_KEPT"\n')
        with native_backend.workspace(pool=self.root / 'pool', size_mib=64) as workspace:
            result = self.launch('import native_fixture; print(native_fixture.VALUE)', workspace)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('VENV_KEPT', result.stdout)

    def test_offline_rust_compiles_in_native_backend(self):
        rust = os.environ.get('DIAKTOROS_NATIVE_RUST_ROOT')
        if not rust:
            self.skipTest('dedicated Rust fixture required')
        self.paths['rust'] = Path(rust)
        checkout = self.paths['checkout']
        (checkout / 'Cargo.toml').write_text('[package]\nname="native_backend_probe"\nversion="0.1.0"\nedition="2021"\n')
        (checkout / 'src').mkdir()
        (checkout / 'src/lib.rs').write_text('#[test] fn works() { assert_eq!(2 + 2, 4); }\n')
        with native_backend.workspace(pool=self.root / 'pool', size_mib=512) as workspace:
            result = native_backend.run(**self.paths, workspace=workspace, timeout=30,
                                        entry=['/bin/sh', '-c', 'cargo test --offline'])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('test result: ok', result.stdout)


if __name__ == '__main__':
    unittest.main()
