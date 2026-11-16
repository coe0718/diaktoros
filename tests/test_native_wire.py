"""Real SDK calls over a local Unix HTTP fixture; never a model service."""
import _home_guard  # noqa: F401
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import socketserver
import http.server
import tempfile
import threading
import types
import unittest
from unittest import mock

try:
    import httpx
    import openai
    import anthropic
except ImportError:
    httpx = openai = anthropic = None


@unittest.skipUnless(httpx and openai and anthropic, 'installed HTTP/SDK test dependencies required')
class NativeWire(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        registered = {}
        providers = types.ModuleType('providers')
        base = types.ModuleType('providers.base')
        class Profile:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
        base.ProviderProfile = Profile
        providers.register_provider = lambda profile: registered.update({profile.name: profile})
        self.stack.enter_context(mock.patch.dict('sys.modules', {'providers': providers, 'providers.base': base}))
        spec = importlib.util.spec_from_file_location('native_wire_fixture',
            Path(__file__).resolve().parents[1] / 'diaktoros/seatbelt_wire.py')
        self.wire = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wire)
        self.profiles = registered
        # Unix socket names must stay below Darwin's pathname bound.
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='nw-', dir='/tmp'))
        self.socket = str(Path(directory).resolve() / 'model.sock')
        self.received = []
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.received.append((self.path, data, dict(self.headers)))
                if self.path == '/v1/chat/completions':
                    body = {'id': 'chat-fixture', 'object': 'chat.completion', 'created': 1,
                            'model': 'fixture', 'choices': [{'index': 0, 'finish_reason': 'stop',
                            'message': {'role': 'assistant', 'content': 'chat fixture'}}]}
                elif self.path == '/v1/responses':
                    body = {'id': 'resp-fixture', 'object': 'response', 'created_at': 1,
                            'status': 'completed', 'model': 'fixture', 'output': []}
                else:
                    body = {'id': 'msg-fixture', 'type': 'message', 'role': 'assistant',
                            'model': 'fixture', 'content': [{'type': 'text', 'text': 'message fixture'}],
                            'stop_reason': 'end_turn', 'stop_sequence': None,
                            'usage': {'input_tokens': 1, 'output_tokens': 1}}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = socketserver.UnixStreamServer(self.socket, Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.stack.callback(close)
        self.stack.enter_context(mock.patch.dict(os.environ,
            {'DIAKTOROS_INFERENCE_SOCKET': self.socket, 'HTTP_PROXY': 'http://127.0.0.1:1'}))

    def test_chat_profile_uses_unix_socket_and_ignores_caller_auth_and_url(self):
        profile = self.profiles[self.wire.NAME]
        with profile.create_client(base_url='https://do-not-contact.invalid', api_key='not-forwarded') as client:
            reply = client.chat.completions.create(model='fixture', messages=[{'role': 'user', 'content': 'local'}])
        self.assertEqual(reply.choices[0].message.content, 'chat fixture')
        self.assertEqual(self.received[0][0], '/v1/chat/completions')
        self.assertEqual(self.received[0][2]['Authorization'], 'Bearer sandbox-dummy')

    def test_responses_profile_uses_responses_wire(self):
        profile = self.profiles[self.wire.RESPONSES_NAME]
        self.assertEqual(profile.api_mode, 'codex_responses')
        with profile.create_client() as client:
            reply = client.responses.create(model='fixture', input='local')
        self.assertEqual(reply.id, 'resp-fixture')
        self.assertEqual(self.received[0][0], '/v1/responses')
        self.assertEqual(self.received[0][1]['input'], 'local')

    def test_messages_rebuild_preserves_headers_and_uses_unix_socket(self):
        original = anthropic.Anthropic
        self.stack.callback(setattr, anthropic, 'Anthropic', original)
        self.wire.install_messages_transport()
        with anthropic.Anthropic(auth_token='cc-dummy', default_headers={'x-app': 'cli'},
                                 base_url='https://do-not-contact.invalid') as client:
            reply = client.messages.create(model='fixture', max_tokens=8,
                                           messages=[{'role': 'user', 'content': 'local'}])
        self.assertEqual(reply.content[0].text, 'message fixture')
        self.assertEqual(self.received[0][0], '/anthropic/v1/messages')
        self.assertEqual(self.received[0][2]['x-app'], 'cli')
        self.assertEqual(self.received[0][2]['Authorization'], 'Bearer cc-dummy')

    def test_missing_socket_fails_closed(self):
        with mock.patch.dict(os.environ, {'DIAKTOROS_INFERENCE_SOCKET': '/no/such/socket'}):
            with self.assertRaisesRegex(RuntimeError, 'live inference socket'):
                self.profiles[self.wire.NAME].create_client()


if __name__ == '__main__':
    unittest.main()
