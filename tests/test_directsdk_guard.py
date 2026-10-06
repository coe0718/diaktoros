"""Recorded ef73726 launch grammar; no network/native login needed."""
import _home_guard  # noqa: F401
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from review_loop import directsdk_guard as guard


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # install_guard patches os/subprocess process-wide; undo it so other tests are unaffected.
        for n in guard.REFUSED_OS_LAUNCHERS:
            if hasattr(os, n):
                self.addCleanup(setattr, os, n, getattr(os, n))
        self.addCleanup(setattr, subprocess, '_USE_POSIX_SPAWN', subprocess._USE_POSIX_SPAWN)
        self.root = Path(self.tmp.name)
        self.plugin = self.root / 'plugin'
        self.plugin.mkdir(mode=0o700)
        shutil.copyfile(Path(__file__).parent / 'fixtures/directsdk_inert_mcp.py', self.plugin / 'inert_mcp.py')
        self.marker = self.root / 'ran'
        self.command = self.root / 'claude'
        self.command.write_text('#!/bin/sh\ntouch "' + str(self.marker) + '"\n')
        self.command.chmod(0o700)
        for name, text in [('settings.json', json.dumps({'env': {'CLAUDE_CODE_EXTRA_BODY': '{}'}})), ('tools.json', '[]'), ('system.md', '')]:
            (self.root / name).write_text(text)
        self.argv = [str(self.command), '-p', '--model', 'sonnet', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '--tools', '', '--system-prompt-file', str(self.root / 'system.md'), '--settings', str(self.root / 'settings.json'), '--setting-sources', '', '--strict-mcp-config', '--disable-slash-commands', '--max-turns', '1', '--permission-mode', 'dontAsk', '--no-session-persistence', '--mcp-config', json.dumps({'mcpServers': {'hermes': {'command': sys.executable, 'args': [str(self.plugin / 'inert_mcp.py'), str(self.root / 'tools.json')]}}})]

    def launch(self, argv):
        with mock.patch.object(subprocess, 'Popen', subprocess.Popen):
            guard.install_guard(str(self.command), str(self.plugin))
            with subprocess.Popen(argv, cwd=str(self.root)) as child:
                self.assertEqual(child.wait(), 0)

    def test_other_launch_routes_refused(self):
        saved = {n: getattr(os, n) for n in guard.REFUSED_OS_LAUNCHERS if hasattr(os, n)}
        for n, f in saved.items():
            self.addCleanup(setattr, os, n, f)
        with mock.patch.object(subprocess, 'Popen', subprocess.Popen):
            guard.install_guard(str(self.command), str(self.plugin))
        cmd = str(self.command)
        calls = {
            'posix_spawn': (cmd, [cmd], {}), 'fork': (), 'execv': (cmd, [cmd]),
            'execl': (cmd, cmd), 'execvp': (cmd, [cmd]), 'execve': (cmd, [cmd], {}),
            'spawnv': (os.P_WAIT, cmd, [cmd]), 'spawnl': (os.P_WAIT, cmd, cmd),
            'spawnvp': (os.P_WAIT, cmd, [cmd]),
        }
        for name, args in calls.items():
            with self.subTest(name=name):
                self.assertIn(name, saved)
                with self.assertRaises(guard.NativeLaunchRefused):
                    getattr(os, name)(*args)
        self.assertFalse(self.marker.exists())

    def test_recorded_pinned_provider_launch_passes(self):
        self.launch(self.argv)
        self.assertTrue(self.marker.exists())

    def test_every_lockdown_omission_refuses_before_process_start(self):
        pairs = {'--tools', '--setting-sources', '--max-turns', '--permission-mode', '--mcp-config'}
        for flag in pairs | {'--strict-mcp-config', '--disable-slash-commands', '--no-session-persistence'}:
            with self.subTest(flag=flag):
                argv = self.argv.copy()
                i = argv.index(flag)
                del argv[i:i + (2 if flag in pairs else 1)]
                with self.assertRaises(guard.NativeLaunchRefused):
                    self.launch(argv)
                self.assertFalse(self.marker.exists())

    def test_unsafe_additions_duplicates_equals_and_values_refused(self):
        changes = [['--dangerously-skip-permissions'], ['--allowedTools', 'Read'], ['--allowed-tools', 'Read'], ['--add-dir', '/'], ['--settings', '/evil'], ['--tools=Read'], ['--tools', 'Read'], ['--permission-mode', 'bypassPermissions']]
        for extra in changes:
            with self.subTest(extra=extra), self.assertRaises(guard.NativeLaunchRefused):
                self.launch(self.argv + extra)
            self.assertFalse(self.marker.exists())
        for flag, value in [('--tools', 'Read'), ('--setting-sources', 'user'), ('--max-turns', '2'), ('--permission-mode', 'default')]:
            argv = self.argv.copy(); argv[argv.index(flag) + 1] = value
            with self.subTest(flag=flag), self.assertRaises(guard.NativeLaunchRefused):
                self.launch(argv)
            self.assertFalse(self.marker.exists())

    def test_mcp_settings_and_code_tampering_refused(self):
        argv = self.argv.copy(); argv[argv.index('--mcp-config') + 1] = '{"mcpServers":{"evil":{"command":"sh"}}}'
        with self.assertRaises(guard.NativeLaunchRefused): self.launch(argv)
        (self.root / 'settings.json').write_text('{"hooks": {}}')
        with self.assertRaises(guard.NativeLaunchRefused): self.launch(self.argv)
        (self.root / 'settings.json').write_text(json.dumps({'env': {'CLAUDE_CODE_EXTRA_BODY': '{}'}}))
        (self.plugin / 'inert_mcp.py').write_text('import os; os.system("evil")')
        with self.assertRaises(guard.NativeLaunchRefused): self.launch(self.argv)
        self.assertFalse(self.marker.exists())

    def test_real_helper_refuses_unsafe_provider_spawns(self):
        source = self.root / 'source'
        package = source / 'hermes_cli'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        (package / 'env_loader.py').write_text('def load_hermes_dotenv(**kw): pass\n')
        helper = Path(guard.__file__).with_name('directsdk_child.py')
        env = {'HOME': str(self.root), 'HERMES_HOME': str(self.root), 'PATH': '/usr/bin:/bin',
               'TMPDIR': str(self.root), 'CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND': str(self.command)}
        pairs = {'--tools', '--setting-sources', '--max-turns', '--permission-mode', '--mcp-config'}
        for flag in pairs | {'--strict-mcp-config', '--disable-slash-commands', '--no-session-persistence', '--dangerously-skip-permissions'}:
            argv = self.argv.copy()
            if flag == '--dangerously-skip-permissions':
                argv.append(flag)
            else:
                i = argv.index(flag); del argv[i:i + (2 if flag in pairs else 1)]
            (self.plugin / 'directsdk.py').write_text('import subprocess\nclass Client:\n    def __init__(self, **kw):\n        subprocess.Popen(' + repr(argv) + ', cwd=' + repr(str(self.root)) + ')\n')
            result = subprocess.run([sys.executable, str(helper), str(source), str(self.plugin)],
                                    env=env, input='{}', text=True, capture_output=True, timeout=10)
            with self.subTest(flag=flag):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('NativeLaunchRefused', result.stderr)
                self.assertFalse(self.marker.exists())

    def test_mutation_without_guard_starts_unsafe_fixture(self):
        # Sensitivity control: the same bad argv runs if the guard is removed.
        argv = self.argv.copy(); argv.remove('--strict-mcp-config')
        with subprocess.Popen(argv, cwd=str(self.root)) as child:
            self.assertEqual(child.wait(), 0)
        self.assertTrue(self.marker.exists())
