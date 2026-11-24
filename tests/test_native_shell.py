"""A private native shell must use a supported, non-platform ABI."""
import _home_guard  # noqa: F401
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from diaktoros import contained, native_backend, native_shell, seatbelt


class ShellSelection(unittest.TestCase):
    def test_arm64e_only_candidate_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / 'bash'
            candidate.touch()
            with mock.patch.object(native_shell.platform, 'machine', return_value='arm64'), \
                    mock.patch.object(native_shell.shutil, 'which', return_value=str(candidate)), \
                    mock.patch.object(native_shell.os, 'access', side_effect=lambda p, mode: Path(p) == candidate), \
                    mock.patch.object(native_shell.subprocess, 'run',
                                      return_value=subprocess.CompletedProcess([], 0, 'x86_64 arm64e\n')):
                with self.assertRaisesRegex(contained.ContainmentUnavailable, 'arm64 Bash'):
                    native_shell.source()

    def test_dependency_alias_and_exact_canonical_file_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            binary, library, alias = (root / name for name in ('bash', 'version.dylib', 'alias.dylib'))
            binary.touch()
            library.touch()
            alias.symlink_to(library)
            def inspect(argv, **kwargs):
                dependency = f'{alias} (compatibility version 1.0.0, current version 1.0.0)'
                return subprocess.CompletedProcess(argv, 0, f'{argv[-1]}:\n\t{dependency}\n')
            with mock.patch.object(native_shell.subprocess, 'run', side_effect=inspect) as command:
                self.assertEqual(set(native_shell.read_files(binary)), {alias, library})
                self.assertEqual(command.call_count, 2)

    def test_relative_dependency_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'bash'
            binary.touch()
            result = subprocess.CompletedProcess([], 0, 'bash:\n\t@rpath/lib.dylib (compatibility version 1.0.0)\n')
            with mock.patch.object(native_shell.subprocess, 'run', return_value=result):
                with self.assertRaisesRegex(contained.ContainmentUnavailable, 'absolute library'):
                    native_shell.read_files(binary)


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS shell')
class NativeShell(unittest.TestCase):
    def test_exact_file_alias_grants_do_not_read_sibling_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            library_dir, policy_dir = root / 'libraries', root / 'policies'
            library_dir.mkdir()
            policy_dir.mkdir()
            library, secret, alias = (library_dir / name for name in ('version.dylib', 'secret', 'current.dylib'))
            library.write_text('library')
            secret.write_text('not readable')
            alias.symlink_to(library)
            profile = seatbelt.profile(read_roots=(policy_dir,), write_roots=(), read_files=(alias, library))
            policy = policy_dir / 'test.sb'
            policy.write_text(profile.text)
            for path in (alias, library, secret):
                result = subprocess.run(profile.command(policy, ['/bin/cat', str(path)]),
                                        capture_output=True, text=True, timeout=10)
                if path == secret:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('not readable', result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, 'library')

    def test_staged_shell_has_plain_native_slice_and_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            shell = native_backend.stage_shell(Path(directory) / 'shell')
            arches = subprocess.check_output(['/usr/bin/lipo', '-archs', str(shell)], text=True).split()
            required = 'arm64' if platform.machine() == 'arm64' else 'x86_64'
            self.assertIn(required, arches, 'arm64e platform binaries cannot substitute for arm64')
            self.assertEqual(shell.stat().st_mode & 0o777, 0o500)
            subprocess.run(['/usr/bin/codesign', '--verify', str(shell)], check=True, capture_output=True)
            result = subprocess.run([str(shell), '-c', 'printf native-shell-ok'],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'native-shell-ok')


if __name__ == '__main__':
    unittest.main()
