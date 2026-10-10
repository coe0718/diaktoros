import _home_guard  # noqa: F401
import os
import subprocess
import sys
import unittest
from unittest import mock

from diaktoros import native_python_spawn


class OwnedPythonSpawn(unittest.TestCase):
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
