"""Native turn paths and transport, without live model services."""
import _home_guard  # noqa: F401
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from diaktoros import broker_client, trusted_turn, turn_layout, native_turn


class NativeConfiguration(unittest.TestCase):
    def test_native_home_selects_each_wire_without_real_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            for mode, identity, expected in (
                ('chat_completions', '', 'diaktoros-seatbelt-wire'),
                ('codex_responses', '', 'diaktoros-seatbelt-responses'),
                ('anthropic_messages', '', 'review-loop-seat'),
                ('anthropic_messages', 'claude_code', 'anthropic')):
                home = Path(tmp) / (mode + identity)
                provider = native_turn.write_home(home, 'fixture', mode, identity, 'high')
                self.assertEqual(provider, expected)
                config = (home / 'config.yaml').read_text()
                self.assertIn('provider: ' + expected, config)
                self.assertIn('reasoning_effort: high', config)
                if mode != 'anthropic_messages':
                    self.assertIn('http://localhost/v1', config)
                if identity == 'claude_code':
                    self.assertIn('cc-dummy', (home / '.env').read_text())

    def test_directsdk_fails_before_home_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            with self.assertRaisesRegex(trusted_turn.TurnDenied, 'DirectSDK'):
                native_turn.write_home(home, 'fixture', 'chat_completions', 'directsdk', '')
            self.assertFalse(home.exists())


class NativePaths(unittest.TestCase):
    def test_native_instructions_change_only_host_examples(self):
        layout = turn_layout.TurnLayout(**{name: Path('/private/turn space') / name
            for name in ('code', 'venv', 'home', 'work', 'export', 'client', 'scratch', 'query')})
        prompt = 'PR text mentions /work and /tmp and must remain unchanged'
        text = trusted_turn.query_text(prompt, 'fixer', [], [], ('', ''), layout=layout)
        self.assertIn(prompt, text)
        self.assertIn("'/private/turn space/scratch/commit.txt'", text)
        self.assertIn('python -P -m diaktoros.broker_client', text)

    def test_client_reads_native_environment_without_changing_defaults(self):
        env = dict(os.environ, DIAKTOROS_WORK='/private/work',
                   DIAKTOROS_EXPORT='/private/export', DIAKTOROS_TURN_FILE='/private/turn',
                   DIAKTOROS_BROKER_SOCKET='/private/broker')
        result = subprocess.run([sys.executable, '-c',
            'from diaktoros import broker_client as b; print(b.WORK,b.EXPORT,b.TURN_FILE,b.SOCKET)'],
            env=env, text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), '/private/work /private/export /private/turn /private/broker')
        self.assertEqual(broker_client.WORK, '/work')

    def test_descriptor_snapshot_survives_source_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            subprocess.run(['/usr/bin/git', 'init', '-q', str(source)], check=True)
            (source / 'run_agent.py').write_text('PINNED = True\n')
            subprocess.run(['/usr/bin/git', '-C', str(source), 'add', '.'], check=True)
            subprocess.run(['/usr/bin/git', '-C', str(source), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
            descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
            try:
                source.rename(root / 'moved')
                source.mkdir()
                (source / 'run_agent.py').write_text('WRONG = True\n')
                trusted_turn._export_committed_source(descriptor, root / 'snapshot')
            finally:
                os.close(descriptor)
            self.assertEqual((root / 'snapshot/run_agent.py').read_text(), 'PINNED = True\n')


class NativeOrchestration(unittest.TestCase):
    def launch(self, role='reviewer', *, failure=None, stop=None, api_mode='chat_completions', real=False, prefetched=None):
        from contextlib import ExitStack, contextmanager
        from types import SimpleNamespace
        from diaktoros import native_backend
        seen = {}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp).resolve()
            root.chmod(0o700)
            volume = SimpleNamespace(**{name: root / name for name in ('home', 'work', 'scratch')})
            for path in vars(volume).values():
                path.mkdir()

            @contextmanager
            def workspace():
                seen['workspace_entered'] = True
                yield volume
                seen['workspace_exited'] = True

            def snapshot(source, code):
                code.mkdir()
                if real:
                    providers = code / 'providers'
                    providers.mkdir()
                    (providers / '__init__.py').write_text('def register_provider(profile): pass\n')
                    (providers / 'base.py').write_text(
                        'class ProviderProfile:\n def __init__(self, **kwargs): self.__dict__.update(kwargs)\n')
                return []

            def stage(loop, **kwargs):
                checkout = kwargs['sandbox_root']
                checkout.mkdir()
                (checkout / 'file.txt').write_text('original')
                return checkout

            class Capability:
                def __init__(self, directory, *args, **kwargs):
                    is_broker = isinstance(directory, dict)
                    if is_broker:
                        directory = args[1]
                    self.directory = directory
                    self.recorded = []
                    self.completed = True
                    self.requests = 1
                    self.socket_path = directory / ('broker.sock' if is_broker else 'model.sock')
                    self.connection = None
                    self.last_activity = 'fixture'
                    seen.setdefault('capabilities', []).append(kwargs)
                def __enter__(self):
                    self.directory.mkdir(exist_ok=True)
                    if real:
                        import socket
                        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        self.connection.bind(str(self.socket_path))
                    return self
                def __exit__(self, *args):
                    if self.connection is not None:
                        self.connection.close()
                    return False
                def close(self):
                    pass

            def run(**kwargs):
                seen['kwargs'] = kwargs
                seen['query'] = kwargs['query'].read_text()
                seen['home_config'] = (kwargs['home'] / 'config.yaml').read_text()
                self.assertIs(kwargs['workspace'], volume)
                self.assertEqual(kwargs['home'], volume.home)
                self.assertTrue((kwargs['client_code'] / 'diaktoros/seatbelt_wire.py').is_file())
                if failure:
                    raise failure
                return subprocess.CompletedProcess(kwargs['entry'], 0, 'fixture stdout', '')

            stack.enter_context(mock.patch.object(sys, 'platform', 'darwin'))
            stack.enter_context(mock.patch.dict(os.environ, {'DIAKTOROS_NATIVE_MACOS': '1'}))
            if real:
                create_workspace = native_backend.workspace
                stack.enter_context(mock.patch.object(native_backend, 'workspace',
                    side_effect=lambda: create_workspace(pool=root / 'cleanup', size_mib=64)))
                (root / 'venv/bin').mkdir(parents=True)
                interpreter = Path(sys.executable).resolve()
                framework = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
                if framework.is_file():
                    interpreter = framework.resolve()
                (root / 'venv/bin/python').symlink_to(interpreter)
                (root / 'venv/bin/hermes').write_text(
                    'import os,sys\nfrom pathlib import Path\n'
                    'assert sys.flags.safe_path\n'
                    'assert Path(sys.argv[sys.argv.index("--query-file")+1]).is_file()\n'
                    'assert Path(os.environ["HERMES_HOME"]).joinpath("config.yaml").is_file()\n'
                    'assert Path.cwd() == Path(os.environ["DIAKTOROS_WORK"])\n'
                    'import subprocess\n'
                    'assert subprocess.Popen.__name__ == "OwnedPopen"\n'
                    f'subprocess.run([{str(interpreter)!r}, "-c", "print(123)"], check=True, start_new_session=True)\n'
                    'print("REAL_NATIVE_BOOTSTRAP")\n')
                (root / 'rust').mkdir()
                (root / 'node/bin').mkdir(parents=True)
            else:
                stack.enter_context(mock.patch.object(native_backend, 'unavailable', return_value=''))
                stack.enter_context(mock.patch.object(native_backend, 'workspace', side_effect=workspace))
                stack.enter_context(mock.patch.object(native_backend, 'run', side_effect=run))
            stack.enter_context(mock.patch.object(trusted_turn, '_safe_code_snapshot', side_effect=snapshot))
            stack.enter_context(mock.patch.object(trusted_turn.trusted_fetch, 'stage', side_effect=stage))
            stack.enter_context(mock.patch.object(trusted_turn.deps, 'prepare', return_value=prefetched or []))
            release = stack.enter_context(mock.patch.object(trusted_turn.deps, 'release'))
            stack.enter_context(mock.patch.object(trusted_turn, 'change_proof_parts', return_value=('', '')))
            stack.enter_context(mock.patch.object(trusted_turn.inference_proxy, 'InferenceCapability', Capability))
            stack.enter_context(mock.patch.object(trusted_turn.broker_ipc, 'RunBroker', Capability))
            thread = mock.Mock()
            thread.is_alive.return_value = False
            stack.enter_context(mock.patch.object(trusted_turn.broker_ipc, 'serve_in_thread', return_value=thread))
            scope = SimpleNamespace(repo='owner/repo', role=role, head='a' * 40, number=1,
                                    branch='fixture', merge=None)
            observed = {}
            args = dict(source=root, venv=root / 'venv', runtime=Path(sys.base_prefix) if real else root / 'runtime', rust=root / 'rust',
                        upstream='https://fixture.invalid', key='host-only-secret', model='fixture',
                        prompt='PR text /work is untrusted', work_root=root, timeout=5,
                        observed=observed, on_launch=lambda: seen.update(launched=True), stop=stop,
                        node=root / 'node', steps=7, api_mode=api_mode, review_diff='diff fixture')
            try:
                trusted_turn.run_turn({'repo': scope.repo, 'state_dir': str(root)}, scope, **args)
            except BaseException as error:
                seen['error'] = error
            seen['observed'] = observed
            self.assertEqual(release.call_count, 1)
            thread.join.assert_called_once_with(timeout=trusted_turn.BROKER_DRAIN_S)
        return seen

    def test_production_paths_roles_callbacks_and_observation(self):
        for role in trusted_turn.TOOLS:
            with self.subTest(role=role):
                seen = self.launch(role)
                self.assertNotIn('error', seen)
                self.assertTrue(seen['launched'])
                self.assertTrue(seen['workspace_exited'])
                kw = seen['kwargs']
                self.assertEqual(kw['checkout_writable'], role not in ('adjudicator', 'triage'))
                self.assertEqual(kw['timeout'], 35)
                self.assertEqual(kw['extra_env'], {'OPENAI_API_KEY': 'sandbox-dummy'})
                self.assertEqual(kw['node'].name, 'node')
                self.assertEqual(kw['entry'][1:4], ['-P', '-m', 'diaktoros.seatbelt_wire'])
                self.assertNotIn('/opt/venv/bin/python', kw['entry'])
                self.assertNotIn('/opt/query', kw['entry'])
                self.assertIn('PR text /work is untrusted', seen['query'])
                self.assertIn(str(kw['review_dir'] / 'pr.diff'), seen['query'])
                self.assertEqual(seen['observed']['stdout'], 'fixture stdout')
                self.assertEqual(seen['capabilities'][0]['nudge_steps'],
                                 7 if role in trusted_turn.NUDGED_ROLES else None)
                self.assertEqual(seen['capabilities'][1]['require_receipt'], role == 'reviewer')

    def test_prefetched_cache_is_held_and_passed_to_native_launch(self):
        item = trusted_turn.deps.Prefetch('rust', trusted_turn.deps.READY, 'fixture',
                                          Path('/dedicated/cache'))
        seen = self.launch(prefetched=[item])
        self.assertNotIn('error', seen)
        self.assertEqual(seen['kwargs']['dependency_caches'], {'rust': item.cache})
        self.assertEqual(seen['observed']['dependencies'], [('rust', item.status, 'fixture')])
        self.assertIn('available offline', seen['query'])

    def test_proof_runner_uses_an_actual_interpreter_argument(self):
        from diaktoros import native_backend
        paths = {name: Path('/fixture') / name for name in
                 ('code', 'venv', 'runtime', 'home', 'rust', 'query')}
        with mock.patch.object(sys, 'platform', 'darwin'), \
                mock.patch.object(native_backend, 'unavailable', return_value=''), \
                mock.patch.object(native_backend, 'workspace') as workspace, \
                mock.patch.object(native_backend, 'run', return_value=
                subprocess.CompletedProcess([], 0, 'proof', '')) as run:
            output = trusted_turn.unittest_runner(**paths)(Path('/fixture/tree'), ['tests.test_one'])
        self.assertEqual(output, 'proof')
        workspace.assert_called_once()
        self.assertEqual(workspace.call_args.kwargs['pool'].name, 'native-proof-cleanup')
        self.assertIs(run.call_args.kwargs['workspace'], workspace.return_value.__enter__.return_value)
        workspace.return_value.__exit__.assert_called_once()
        entry = run.call_args.kwargs['entry']
        self.assertIn('/fixture/venv/bin/python', entry)
        self.assertNotIn('/opt/venv', ' '.join(entry))

    def test_timeout_keeps_existing_budget_and_output_contract(self):
        seen = self.launch(failure=subprocess.TimeoutExpired(['fixture'], 35, output=b'partial', stderr=b'error'))
        self.assertIsInstance(seen['error'], trusted_turn.TurnBudgetExceeded)
        self.assertIn('partial', seen['observed']['stdout'])
        self.assertEqual(seen['observed']['stderr'], 'error')

    def test_stop_before_launch_does_not_count_a_turn(self):
        import threading
        stop = threading.Event()
        stop.set()
        seen = self.launch(stop=stop)
        self.assertIsInstance(seen['error'], trusted_turn.contained.Stopped)
        self.assertNotIn('launched', seen)
        self.assertNotIn('kwargs', seen)

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS required')
    def test_actual_native_process_uses_production_bootstrap(self):
        seen = self.launch(real=True)
        self.assertNotIn('error', seen)
        self.assertIn('REAL_NATIVE_BOOTSTRAP', seen['observed']['stdout'], seen['observed'])

    def test_messages_launches_transport_bootstrap(self):
        seen = self.launch(api_mode='anthropic_messages')
        self.assertNotIn('error', seen)
        self.assertIn('--messages', seen['kwargs']['entry'])


if __name__ == '__main__':
    unittest.main()
