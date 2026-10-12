"""Pinned Hermes preparation fails visibly and exports only explicit fixture inputs."""
import _home_guard  # noqa: F401
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import native_tester
import native_tester_hermes as preparation


class PreparedHermesTests(unittest.TestCase):
    def fixture(self, root):
        paths = {key:str(root/key) for key in preparation.ENV_PATHS}
        for key in preparation.ENV_PATHS:
            Path(paths[key]).mkdir()
        adapter = Path(paths['DIAKTOROS_NATIVE_SPAWN_ADAPTER'])
        adapter.rmdir()
        adapter.write_bytes(b'fixture')
        for key, suffix in (('HERMES_AGENT_SOURCE','venv/bin/hermes'),
                            ('DIAKTOROS_NATIVE_RUST_ROOT','bin/rustc'),
                            ('DIAKTOROS_NATIVE_RUST_ROOT','bin/cargo'),
                            ('DIAKTOROS_NATIVE_BUILD_FIXTURE','Cargo.lock'),
                            ('DIAKTOROS_NATIVE_BUILD_FIXTURE','vendor/memchr/Cargo.toml'),
                            ('DIAKTOROS_NATIVE_DEVELOPER_TOOLS','bin/clang')):
            path = Path(paths[key])/suffix
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture')
        manifest = root/'result.json'
        manifest.write_text(json.dumps({'passed':True, 'hermes_commit':preparation.HERMES_COMMIT,
                                       'paths':paths, 'OPENAI_API_KEY':'must-not-propagate'}))
        return manifest, paths

    def test_only_validated_fixture_paths_reach_the_test_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, paths = self.fixture(Path(directory).resolve())
            with mock.patch.object(preparation.subprocess, 'run', return_value=SimpleNamespace(stdout=preparation.HERMES_COMMIT)):
                env = preparation.prepared_environment(manifest)
            self.assertEqual(set(env), set(preparation.ENV_PATHS)|{'DIAKTOROS_REQUIRE_NATIVE_HERMES'})
            self.assertEqual(env['HERMES_AGENT_SOURCE'], paths['HERMES_AGENT_SOURCE'])

    def test_wrong_commit_or_missing_vendor_input_refuses_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, paths = self.fixture(Path(directory).resolve())
            with mock.patch.object(preparation.subprocess, 'run', return_value=SimpleNamespace(stdout='wrong')):
                with self.assertRaises(ValueError):
                    preparation.prepared_environment(manifest)

            (Path(paths['DIAKTOROS_NATIVE_BUILD_FIXTURE'])/'vendor/memchr/Cargo.toml').unlink()
            with mock.patch.object(preparation.subprocess, 'run', return_value=SimpleNamespace(stdout=preparation.HERMES_COMMIT)):
                with self.assertRaisesRegex(ValueError, 'vendor input'):
                    preparation.prepared_environment(manifest)

    def test_live_hermes_checkout_is_rejected_before_any_git_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest, paths = self.fixture(root)
            live = root/'home/.hermes/hermes-agent'
            live.mkdir(parents=True)
            value = json.loads(manifest.read_text())
            value['paths']['HERMES_AGENT_SOURCE'] = str(live)
            manifest.write_text(json.dumps(value))
            with mock.patch.object(preparation.Path, 'home', return_value=root/'home'), \
                    mock.patch.object(preparation.subprocess, 'run') as git:
                with self.assertRaisesRegex(ValueError, 'disposable'):
                    preparation.prepared_environment(manifest)
            git.assert_not_called()

    def test_preparation_failure_preserves_failed_step_without_running_later_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()/'preparation'
            def step(name, argv, output, **kwargs):
                (output/(name+'.log')).write_text('uv 0.12.19\n' if name == 'uv_version' else 'failed\n')
                return {'name':name, 'log':name+'.log', 'status':'passed' if name == 'uv_version' else 'failed'}
            with mock.patch.object(preparation, 'preflight', return_value={'passed':True}), \
                    mock.patch.object(preparation.shutil, 'which', side_effect=lambda name:'/fixture/'+name), \
                    mock.patch.object(preparation, 'run_step', side_effect=step) as command:
                self.assertEqual(preparation.prepare(output), 1)
            self.assertEqual(command.call_count, 2)
            report = json.loads((output/'result.json').read_text())
            self.assertFalse(report['passed'])
            self.assertEqual(report['steps'][-1]['name'], 'hermes_clone')

    def test_invalid_preparation_is_a_failed_tester_manifest_not_a_skipped_hermes_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest = root/'bad.json'
            manifest.write_text('{"passed":false}')
            with mock.patch.object(native_tester, 'preflight', return_value={'passed':True}), \
                    mock.patch.object(native_tester, 'run_step') as command:
                self.assertEqual(native_tester.run('hermes', root/'evidence', prepared=manifest), 1)
            command.assert_not_called()
            self.assertFalse(json.loads((root/'evidence/result.json').read_text())['passed'])


if __name__ == '__main__':
    unittest.main()
