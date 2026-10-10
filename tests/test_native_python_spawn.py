import _home_guard  # noqa: F401
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from diaktoros import native_python_spawn


class OwnedPythonSpawn(unittest.TestCase):
    def test_bootstrap_ignores_checkout_module_shadows_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / 'work'
            work.mkdir()
            (work / 'diaktoros').mkdir()
            (work / 'diaktoros/__init__.py').write_text("raise AssertionError('checkout package loaded')")
            (work / 'subprocess.py').write_text("raise AssertionError('checkout subprocess loaded')")
            script = root / 'trusted.py'
            script.write_text('import os,subprocess,sys\n'
                              'assert sys.argv[1:] == ["requested argument"]\n'
                              'result=subprocess.check_output([sys.executable,"-c",'
                              '"import os; print(os.getpgrp())"],start_new_session=True,text=True)\n'
                              'assert result.strip() == str(os.getpgrp())\n'
                              'print("TRUSTED_BOOTSTRAP_OK")\n')
            result = subprocess.run(native_python_spawn.entry([sys.executable, str(script),
                                                               'requested argument']),
                                    cwd=work, env={**os.environ, 'PYTHONPATH': str(
                                        Path(native_python_spawn.__file__).resolve().parents[1])},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('TRUSTED_BOOTSTRAP_OK', result.stdout)

    def test_requested_session_stays_in_owned_group_and_preserves_output(self):
        with mock.patch.object(subprocess, 'Popen', subprocess.Popen), \
                mock.patch.object(subprocess, '_USE_POSIX_SPAWN', subprocess._USE_POSIX_SPAWN), \
                mock.patch.object(subprocess, '_USE_VFORK', subprocess._USE_VFORK):
            native_python_spawn.install()
            result = subprocess.run([sys.executable, '-c',
                                     'import os; print(os.getpgrp()); raise SystemExit(7)'],
                                    start_new_session=True, capture_output=True, text=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.strip(), str(os.getpgrp()))

    def test_explicit_group_change_is_rejected_before_launch(self):
        with mock.patch.object(subprocess, 'Popen', subprocess.Popen), \
                mock.patch.object(subprocess, '_USE_POSIX_SPAWN', subprocess._USE_POSIX_SPAWN), \
                mock.patch.object(subprocess, '_USE_VFORK', subprocess._USE_VFORK):
            native_python_spawn.install()
            with self.assertRaisesRegex(ValueError, 'owned process group'):
                subprocess.Popen([sys.executable, '-c', 'raise SystemExit(99)'], process_group=0)
