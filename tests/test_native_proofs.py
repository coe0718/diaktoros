"""Proofs get their own bounded native volume, never the live turn's volume."""
import _home_guard  # noqa: F401
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from diaktoros import change_proof, native_backend, native_cleanup, trusted_turn


class ProofRouting(unittest.TestCase):
    def test_reviewer_passes_scope_ledger(self):
        scope = SimpleNamespace(role='reviewer', ledger_db=Path('/fixture/ledger.sqlite'))
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(trusted_turn.config, 'change_proof', return_value=True), \
                mock.patch.object(trusted_turn, 'unittest_runner') as runner, \
                mock.patch.object(change_proof, 'for_head', return_value=('', '')):
            trusted_turn.change_proof_parts({}, scope, Path(directory), Path(directory), None)
        self.assertEqual(runner.call_args.kwargs['ledger_db'], scope.ledger_db)

    def test_issue_fixer_passes_scope_ledger_and_stop(self):
        from test_native_turn_orchestration import NativeOrchestration
        from diaktoros import native_turn
        stage, prepare = trusted_turn._stage_source, native_turn.prepare
        ledger = Path('/fixture/ledger.sqlite')
        stop = mock.Mock()
        stop.is_set.return_value = False

        def scoped(root, source, scope):
            scope.ledger_db = ledger
            return stage(root, source, scope)

        with mock.patch.object(trusted_turn, '_stage_source', side_effect=scoped), \
                mock.patch.object(native_turn, 'prepare', side_effect=lambda **kw:
                    prepare(**{**kw, 'ledger_db': None})), \
                mock.patch.object(trusted_turn, 'sandbox_prover') as prover:
            seen = NativeOrchestration().launch('issue_fixer', stop=stop)
        self.assertNotIn('error', seen)
        self.assertEqual(prover.call_args.kwargs['ledger_db'], ledger)
        self.assertIs(prover.call_args.kwargs['stop'], stop)

    def test_issue_prover_forwards_ledger_and_stop(self):
        stop = object()
        with mock.patch.object(trusted_turn, 'unittest_runner') as runner:
            trusted_turn.sandbox_prover(checkout=Path('/tree'), code=Path('/code'),
                venv=Path('/venv'), runtime=Path('/runtime'), home=Path('/home'),
                rust=Path('/rust'), query=Path('/query'), ledger_db=Path('/ledger'), stop=stop)
        self.assertEqual(runner.call_args.kwargs['ledger_db'], Path('/ledger'))
        self.assertIs(runner.call_args.kwargs['stop'], stop)

    def test_legacy_runner_uses_default_ledger_and_forwards_stop(self):
        from contextlib import nullcontext
        paths = {name: Path('/fixture') / name for name in
                 ('code', 'venv', 'runtime', 'home', 'rust', 'query')}
        stop, volume = object(), object()
        with mock.patch.object(sys, 'platform', 'darwin'), \
                mock.patch.object(trusted_turn.config, 'host_path',
                                  return_value=Path('/fallback/runs.db')) as default, \
                mock.patch.object(native_backend, 'workspace',
                                  return_value=nullcontext(volume)) as workspace, \
                mock.patch.object(trusted_turn.contained, 'run',
                    return_value=subprocess.CompletedProcess([], 0, 'out', 'err')) as run:
            output = trusted_turn.unittest_runner(**paths, stop=stop)(Path('/tree'), ['test_id'])
        self.assertEqual(output, 'outerr')
        default.assert_called_once_with('ledger')
        workspace.assert_called_once_with(pool=Path('/fallback/native-proof-cleanup'))
        self.assertIs(run.call_args.kwargs['stop'], stop)
        self.assertIs(run.call_args.kwargs['workspace'], volume)

    def test_linux_keeps_shell_contract_without_native_pool(self):
        paths = {name: Path('/fixture') / name for name in
                 ('code', 'venv', 'runtime', 'home', 'rust', 'query')}
        with mock.patch.object(sys, 'platform', 'linux'), \
                mock.patch.object(native_backend, 'workspace') as workspace, \
                mock.patch.object(trusted_turn.contained, 'run',
                    return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            trusted_turn.unittest_runner(**paths, ledger_db=Path('/explicit/runs.db'))(
                Path('/tree'), ['test_id'], 'tests:.')
        workspace.assert_not_called()
        self.assertEqual(run.call_args.kwargs['entry'][:2], ['/bin/sh', '-c'])
        self.assertEqual(run.call_args.kwargs['entry'][-2:], ['tests:.', 'test_id'])
        self.assertNotIn('workspace', run.call_args.kwargs)


@unittest.skipUnless(sys.platform == 'darwin', 'native macOS proofs')
class NativeProofs(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='native-proofs-')).resolve()
        self.addCleanup(self.cleanup)
        self.ledger = self.root / 'state/runs.sqlite'
        self.ledger.parent.mkdir()
        self.paths = {key: self.root / key for key in ('code', 'venv', 'home', 'rust')}
        for path in self.paths.values():
            path.mkdir()
        venv = self.paths['venv']
        (venv / 'bin').mkdir()
        (venv / 'bin/python').symlink_to(Path(sys.executable).resolve())
        (venv / 'pyvenv.cfg').write_text(
            f'home = {Path(sys.base_prefix) / "bin"}\ninclude-system-site-packages = false\n')
        packages = venv / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
        packages.mkdir(parents=True)
        (packages / 'proof_dependency.py').write_text('VALUE = "VENV_PROOF_OK"\n')
        self.paths['runtime'] = Path(sys.base_prefix).resolve()
        self.paths['query'] = self.root / 'query'
        self.paths['query'].write_text('')
        self.tree = self.root / 'tree'
        (self.tree / 'tests').mkdir(parents=True)
        (self.tree / 'tests/helper.py').write_text('VALUE = "TEST_PATH_OK"\n')
        (self.tree / 'tests/test_proof.py').write_text(
            'import os, sys, unittest, helper, proof_dependency\n'
            'from pathlib import Path\n'
            'class Proof(unittest.TestCase):\n'
            ' def test_pass(self):\n'
            f'  self.assertEqual(sys.prefix, {str(venv)!r})\n'
            f'  self.assertNotIn({str(self.paths["code"])!r}, sys.path)\n'
            '  self.assertEqual(os.environ["PYTHONPATH"], "tests:.")\n'
            '  self.assertEqual(helper.VALUE, "TEST_PATH_OK")\n'
            '  Path("proof-write").write_text("private proof")\n'
            '  print(proof_dependency.VALUE, flush=True)\n'
            ' def test_fail(self):\n'
            '  self.fail("EXPECTED_PROOF_FAILURE")\n')

    def cleanup(self):
        for pool in self.ledger.parent.glob('native*cleanup'):
            result = native_cleanup.bounded_sweep(pool)
            self.assertEqual(result['status'], 'completed', result)
        info = plistlib.loads(subprocess.check_output(['/usr/bin/hdiutil', 'info', '-plist']))
        attached = [item['image-path'] for item in info.get('images', [])
                    if Path(item.get('image-path', '/')).is_relative_to(self.root)]
        self.assertFalse(attached, f'retain attached fixture evidence: {attached}')
        shutil.rmtree(self.root)

    def test_real_proofs_run_while_main_pool_is_occupied(self):
        create, run = native_backend.workspace, native_backend.run
        results, volumes = [], []

        def capture(**kwargs):
            self.assertEqual(kwargs['entry'][0], str(self.paths['venv'] / 'bin/python'))
            volumes.append(kwargs.get('workspace'))
            with self.assertRaises(native_cleanup.AdmissionRefused):
                with create(pool=self.ledger.parent / 'native-proof-cleanup', size_mib=64):
                    self.fail('proof pool admitted a concurrent workspace')
            result = run(**kwargs)
            results.append(result)
            return result

        with create(pool=self.ledger.parent / 'native-cleanup', size_mib=64) as main:
            marker = main.work / 'main-turn'
            marker.write_text('untouched')
            with self.assertRaises(native_cleanup.AdmissionRefused):
                with create(pool=self.ledger.parent / 'native-cleanup', size_mib=64):
                    self.fail('main pool admitted a concurrent workspace')
            with mock.patch.object(trusted_turn.config, 'host_path',
                                   side_effect=AssertionError('explicit ledger lost')), \
                    mock.patch.object(native_backend, 'workspace',
                        side_effect=lambda **kw: create(size_mib=64, **kw)), \
                    mock.patch.object(native_backend, 'run', side_effect=capture):
                proof = trusted_turn.unittest_runner(**self.paths, ledger_db=self.ledger)
                passed = proof(self.tree, ['test_proof.Proof.test_pass'], 'tests:.')
                failed = proof(self.tree, ['test_proof.Proof.test_fail'], 'tests:.')
            self.assertEqual([result.returncode for result in results], [0, 1])
            self.assertIn('VENV_PROOF_OK', results[0].stdout)
            self.assertIn('VENV_PROOF_OK', passed)
            self.assertIn('EXPECTED_PROOF_FAILURE', failed)
            self.assertEqual(marker.read_text(), 'untouched')
            self.assertFalse((main.work / 'proof-write').exists())
            self.assertNotEqual(volumes[0], volumes[1])
            self.assertTrue(all(volume is not main for volume in volumes))
        self.assertFalse((self.tree / 'proof-write').exists())


if __name__ == '__main__':
    unittest.main()
