"""Real native production turn with pinned Hermes, Rust and fake remote services."""
import _home_guard  # noqa: F401
import _ci_green
from contextlib import ExitStack
import http.server
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import unittest
from unittest import mock

from diaktoros import gh, native_backend, review_receipt, runtime_detect, trusted_fetch, trusted_turn
from diaktoros.state import ledger
from diaktoros.broker.ipc import RunScope
from diaktoros.inference_proxy import PATH
from diaktoros.run_supervisor import Supervisor

SOURCE = _home_guard.HERMES_AGENT_SOURCE
HEAD = 'a' * 40


class NativeProduction(unittest.TestCase):
    def test_real_hermes_rust_and_confirmed_review_through_production_entrypoint(self):
        if sys.platform != 'darwin' or SOURCE is None or not os.environ.get('DIAKTOROS_NATIVE_RUST_ROOT'):
            if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_HERMES') == '1':
                self.fail('prepared native Hermes/Rust fixture required')
            self.skipTest('prepared native Hermes/Rust fixture required')
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {'DIAKTOROS_NATIVE_MACOS': '1'}))
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(dir='/tmp'))).resolve()
            root.chmod(0o700)
            key = 'HOST_ONLY_MODEL_SENTINEL'
            secret = root / 'host-secret'
            secret.write_text('HOST_ONLY_PAT_SENTINEL')
            for login in ('reader', 'reviewer', 'fixer'):
                (root / (login + '.pat')).write_text('dummy-' + login)
            loop = {'repo': 'acme/widgets', 'base': 'main', 'state_dir': str(root),
                    'read_token': 'reader', 'tokens': {name: str(root / (name + '.pat'))
                        for name in ('reader', 'reviewer', 'fixer')},
                    'seats': {'reviewer': {'login': 'reviewer'}, 'fixer': {'login': 'fixer'}}}
            pr = {'number': 7, 'state': 'open', 'draft': False,
                  'base': {'ref': 'main', 'sha': 'b' * 40, 'repo': {'full_name': loop['repo']}},
                  'head': {'sha': HEAD, 'ref': 'fix-7', 'repo': {'full_name': loop['repo']}}}
            sup = Supervisor(root / 'runs.sqlite')
            sup.enqueue('turn', loop['repo'], 7, HEAD, 'reviewer')
            generation = review_receipt.generation_for(pr, loop, 7, HEAD)
            with ledger.connect(sup.db) as con:
                con.execute("UPDATE runs SET state='running',generation=?", (generation,))
                run_id = con.execute('SELECT id FROM runs').fetchone()[0]
            scope = RunScope(loop['repo'], 7, HEAD, 'reviewer', 'fix-7', run_id, str(sup.db), generation)
            writes, requests, launched = [], [], []

            def api(_loop, path, method='GET', body=None, login=None):
                if path == '/user':
                    return {'login': login, 'id': {'reader': 1, 'reviewer': 2, 'fixer': 3}[login]}
                if method == 'POST':
                    writes.append((path, body, login))
                    return {'id': 42}
                if path.endswith('/reviews/42'):
                    return {'id': 42, 'state': 'APPROVED', 'commit_id': HEAD,
                            'user': {'id': 2, 'login': 'reviewer'}}
                return pr

            command = (f'cat {shlex.quote(str(secret))}; cargo test --offline && '
                       'python -P -m diaktoros.broker_client review --verdict APPROVE --body-file review.txt')

            class Model(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append((self.path, self.headers.get('Authorization'), request))
                    done = any(m.get('role') == 'tool' for m in request.get('messages', []))
                    delta = ({'role': 'assistant', 'content': 'PRODUCTION_NATIVE_DONE'} if done else
                             {'role': 'assistant', 'content': None, 'tool_calls': [{
                                 'index': 0, 'id': 'native-production', 'type': 'function',
                                 'function': {'name': 'terminal', 'arguments': json.dumps({'command': command})}}]})
                    chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1,
                             'model': 'fixture-model', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}
                    end = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop' if done else 'tool_calls'}]}
                    data = (''.join('data: ' + json.dumps(c) + '\n\n' for c in (chunk, end)) + 'data: [DONE]\n\n').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Model)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            def stop_server():
                server.shutdown()
                server.server_close()
                thread.join()
            stack.callback(stop_server)

            def stage(_loop, **kwargs):
                checkout = kwargs['sandbox_root']
                (checkout / 'src').mkdir(parents=True)
                (checkout / 'Cargo.toml').write_text('[package]\nname="native_production_probe"\nversion="0.1.0"\nedition="2021"\n')
                (checkout / 'src/lib.rs').write_text('#[test] fn works() { assert_eq!(2 + 2, 4); }\n')
                (checkout / 'review.txt').write_text('offline verified\nNot verified: live services and Intel')
                return checkout

            create_workspace = native_backend.workspace
            stack.enter_context(mock.patch.object(native_backend, 'workspace',
                side_effect=lambda **kwargs: create_workspace(
                    pool=kwargs.get('pool', root / 'native-cleanup'), size_mib=512)))
            stack.enter_context(mock.patch.object(trusted_fetch, 'stage', side_effect=stage))
            stack.enter_context(mock.patch.object(gh, 'api', side_effect=_ci_green.green(api)))
            observed = {}
            try:
                rc = trusted_turn.run_turn(loop, scope, source=SOURCE, venv=SOURCE / 'venv',
                    runtime=runtime_detect.runtime_for(SOURCE / 'venv'),
                    rust=Path(os.environ['DIAKTOROS_NATIVE_RUST_ROOT']),
                    upstream=f'http://127.0.0.1:{server.server_port}{PATH}', key=key,
                    model='fixture-model', prompt='Run the offline Rust test and submit the scoped review.',
                    timeout=180, work_root=root, observed=observed, on_launch=lambda: launched.append(True))
            except trusted_turn.TurnDenied as error:
                tools = [m for _, _, r in requests for m in r.get('messages', []) if m.get('role') == 'tool']
                self.fail(f'{error}: {observed}; tool results: {tools}')
            outputs = '\n'.join(str(m.get('content')) for _, _, request in requests
                                for m in request.get('messages', []) if m.get('role') == 'tool')
            self.assertEqual(rc, 0, observed)
            self.assertEqual(launched, [True])
            self.assertGreaterEqual(len(requests), 2)
            self.assertTrue(all(path == PATH and auth == 'Bearer ' + key for path, auth, _ in requests))
            self.assertIn('test result: ok', outputs)
            self.assertIn('Operation not permitted', outputs)
            self.assertNotIn(secret.read_text(), outputs)
            self.assertNotIn(key, outputs)
            self.assertEqual(len(writes), 1, outputs)
            self.assertEqual(writes[0][1]['commit_id'], HEAD)
            self.assertEqual(writes[0][2], 'reviewer')
            self.assertIn('PRODUCTION_NATIVE_DONE', observed['stdout'])
            self.assertFalse(any((root / 'native-cleanup/volumes').iterdir()))


if __name__ == '__main__':
    unittest.main()
