"""Exercise the real host helper with a disposable, non-network provider fixture."""
import _home_guard  # noqa: F401
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from review_loop import config, directsdk_backend as backend, inference_proxy, seat_model, trusted_turn

FAKE_CLIENT = '''
import json, os, time
from types import SimpleNamespace
class Object:
    def __init__(self, value): self.value = value
    def model_dump(self): return self.value
class Client:
    def __init__(self, **kw):
        assert kw['args'] == []
        assert set(kw['env']) <= {'HOME','PATH','LANG','TMPDIR','CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR'}
        assert not any(k in kw['env'] for k in ('ANTHROPIC_API_KEY', 'NODE_OPTIONS','GH_TOKEN'))
        self.kw = kw
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
    def create(self, **payload):
        if payload['messages'][0]['content'] == 'WAIT':
            time.sleep(30)
        answer = {'choices':[{'message':{'content':'OK','reasoning_details':payload['messages'][1].get('reasoning_details',[]),'tool_calls': [{'id':'t1','type':'function','function':{'name':'read_file','arguments':'{}'}}]}}], 'observed':payload}
        if payload.get('stream'):
            return Stream(answer)
        return Object(answer)
    def close(self): pass
class Stream:
    def __init__(self, value): self.value = value
    def __iter__(self):
        yield Object({'choices':[{'delta':{'reasoning_content':'thinking'}}]})
        yield Object(self.value)
    def close(self): pass
'''


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.source = self.root / 'source'
        package = self.source / 'hermes_cli'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        (package / 'env_loader.py').write_text('def load_hermes_dotenv(**kwargs): pass\n')
        plugin = self.home / 'plugins' / backend.PROVIDER
        plugin.mkdir(parents=True)
        (plugin / 'directsdk.py').write_text(FAKE_CLIENT)
        self.patchers = [mock.patch.object(config, 'home', return_value=self.home),
                         mock.patch.object(config, 'profile_dir', return_value=self.home),
                         mock.patch.object(seat_model, 'hermes_interpreter', return_value=(sys.executable, str(self.source))),
                         mock.patch.dict(os.environ, {'CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND':'/evil',
                             'ANTHROPIC_API_KEY':'HOST_SECRET', 'NODE_OPTIONS':'--require /evil', 'GH_TOKEN':'HOST_SECRET'})]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        # The fixed child PATH contains /bin/true; the request cannot pick a command.
        (package / 'env_loader.py').write_text('import os\ndef load_hermes_dotenv(**kwargs): os.environ["CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND"]="/bin/true"\n')
        self.endpoint = backend.DirectSDKBackend('rev', {})
        self.addCleanup(self.endpoint.close)
        self.payload = {'model':'m', 'max_tokens':16, 'messages':[{'role':'user','content':'OK'},
            {'role':'assistant','content':'prior','reasoning_details':[{'type':backend.PROVIDER+'.native_assistant','data':{'content':[{'type':'thinking','thinking':'prior','signature':'sig'}]}}]}]}

    def test_real_child_history_tools_reasoning_and_stream(self):
        self.payload['reasoning'] = {'enabled':True,'effort':'high'}
        status, content_type, reply = self.endpoint.post(json.dumps(self.payload).encode(), {'Authorization':'HOST_SECRET'})
        answer = json.loads(b''.join(reply))
        self.assertEqual(status, 200)
        self.assertEqual(content_type, 'application/json')
        self.assertEqual(answer['choices'][0]['message']['content'], 'OK')
        self.assertEqual(answer['choices'][0]['message']['tool_calls'][0]['function']['name'], 'read_file')
        self.assertEqual(answer['choices'][0]['message']['reasoning_details'], self.payload['messages'][1]['reasoning_details'])
        self.assertEqual(answer['observed']['extra_body']['reasoning']['effort'], 'high')
        self.payload['stream'] = True
        _, kind, reply = self.endpoint.post(json.dumps(self.payload).encode(), {})
        wire = b''.join(reply)
        self.assertEqual(kind, 'text/event-stream')
        self.assertIn(b'reasoning_content', wire)
        self.assertIn(b'data: [DONE]', wire)
        self.assertEqual(self.endpoint.active, set())

    def test_untrusted_process_and_extra_token_parameters_refused(self):
        for field in ('command','args','env','cwd','timeout','client_kwargs','base_url','api_key'):
            with self.subTest(field=field), self.assertRaises(inference_proxy.ProxyError):
                self.endpoint.post(json.dumps(dict(self.payload, **{field:'/evil'})).encode(), {})
        for field in ('max_tokens','max_completion_tokens','command','env','thinking','output_config'):
            with self.subTest(field=field), self.assertRaises(inference_proxy.ProxyError):
                self.endpoint.post(json.dumps(dict(self.payload, extra_body={field:999999})).encode(), {})
        self.assertEqual(self.endpoint.active, set())

    def test_capability_close_cancels_real_pending_child(self):
        self.payload['messages'][0]['content'] = 'WAIT'
        _, _, reply = self.endpoint.post(json.dumps(self.payload).encode(), {})
        self.assertIsNone(reply.process.poll())
        started = time.monotonic()
        self.endpoint.close()
        self.assertLess(time.monotonic() - started, 4)
        self.assertIsNotNone(reply.process.poll())
        self.assertEqual(self.endpoint.active, set())

    def test_real_socket_capability_does_not_treat_idle_client_as_disconnect(self):
        from review_loop.inference_proxy import InferenceCapability, _UnixHTTP
        credential = backend.ProcessCredential('rev', {})
        with InferenceCapability(self.root / 'cap', backend.UPSTREAM, model='m', quota=1,
                                 credential=credential) as cap:
            connection = _UnixHTTP(str(cap.socket_path), timeout=8)
            try:
                connection.request('POST', '/v1/chat/completions', body=json.dumps(self.payload))
                response = connection.getresponse()
                data = response.read()
                self.assertEqual(response.status, 200, data)
                self.assertEqual(json.loads(data)['choices'][0]['message']['content'], 'OK')
            finally:
                connection.close()

    def test_timeout_disconnect_and_response_bound_cancel_children(self):
        import socket
        for mode in ('timeout', 'disconnect', 'response-bound'):
            with self.subTest(mode=mode):
                payload = json.loads(json.dumps(self.payload))
                if mode != 'response-bound':
                    payload['messages'][0]['content'] = 'WAIT'
                _, _, reply = self.endpoint.post(json.dumps(payload).encode(), {})
                if mode == 'timeout':
                    reply.deadline = 0
                if mode == 'disconnect':
                    a, b = socket.socketpair()
                    self.addCleanup(a.close)
                    b.close()
                    reply.set_peer(a)
                with mock.patch.object(inference_proxy, 'MAX_RESPONSE', 32), self.assertRaises(inference_proxy.ProxyError):
                    b''.join(reply)
                self.assertIsNotNone(reply.process.poll())
                self.assertEqual(self.endpoint.active, set())

    def test_real_resolver_accepts_only_named_external_backend(self):
        package = self.source / 'hermes_cli'
        (package / 'auth.py').write_text(
            'from types import SimpleNamespace\nPROVIDER_REGISTRY={"' + backend.PROVIDER + '":SimpleNamespace(auth_type="external_process")}\n')
        (package / 'runtime_provider.py').write_text(
            'def _get_model_config(): return {"default":"m"}\n'
            'def resolve_requested_provider(): return "' + backend.PROVIDER + '"\n'
            'def resolve_runtime_provider(): return {"provider":"' + backend.PROVIDER + '","api_mode":"chat_completions","base_url":"' + backend.UPSTREAM + '","args":[]}\n')
        answer = seat_model.run_resolver('rev', 'resolve', {})
        self.assertNotIn('error', answer)
        self.assertEqual(answer['auth'], 'external_process')
        self.assertEqual(answer['key'], 'host-process')
        self.assertEqual(answer['base_url'], backend.UPSTREAM)

    def test_model_resolution_and_wire_identity(self):
        answer = dict(model='m', requested=backend.PROVIDER, provider=backend.PROVIDER,
            api_mode='chat_completions', base_url=backend.UPSTREAM, auth='external_process')
        with mock.patch.object(seat_model, 'profile_problem', return_value=''), mock.patch.object(seat_model, 'run_resolver', return_value=answer):
            inference = seat_model.resolve_profile('rev','reviewer',{})
        self.assertEqual(inference.client_identity, 'directsdk')
        self.assertEqual(inference.auth, 'external_process')
        self.assertEqual(inference.key, 'host-process')
        text, env, provider = trusted_turn.sandbox_config('m', client_identity=inference.client_identity)
        self.assertEqual(provider, 'review-loop-directsdk-wire')
        self.assertNotIn('process://', text)
        self.assertNotIn('HOST_SECRET', text + env)
        self.assertNotIn('claude-subscription-directsdk-experimental', text)
        self.assertIn('127.0.0.1', text)
        self.assertNotEqual(inference.identity(), seat_model.SeatInference('fixer','fix','profile',backend.PROVIDER,'m',backend.UPSTREAM,'host-process',auth='external_process').identity())


if __name__ == '__main__':
    unittest.main()
