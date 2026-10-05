"""Offline adversarial tests for the host-only DirectSDK subscription transport.

No live native client, subscription login or remote model is used. Proxy tests use
an in-memory endpoint; child tests below use only disposable plugin fixtures.
"""
import _home_guard  # noqa: F401  first import: isolated HOME/HERMES_HOME
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from review_loop import directsdk_backend, inference_proxy, seat_model
from review_loop.inference_proxy import InferenceCapability, _UnixHTTP

UPSTREAM = 'process://claude-subscription-directsdk-experimental'
PROVIDER = 'claude-subscription-directsdk-experimental'


def seat(profile='rev', model='claude-sonnet-4-6'):
    return seat_model.SeatInference(
        seat='reviewer', profile=profile, origin='profile', provider=PROVIDER,
        model=model, upstream=UPSTREAM, key='host-process',
        auth='external_process', settings={})


class FakeEndpoint:
    def __init__(self, profile, settings):
        self.profile = profile
        self.settings = settings
        self.requests = []
        self.closed = False
        self.reply = lambda: iter([b'{"ok":true}'])
        self.content_type = 'application/json'

    def post(self, body, headers):
        self.requests.append((json.loads(body), dict(headers)))
        return 200, self.content_type, self.reply()

    def close(self):
        self.closed = True


def post(cap, body, headers=None, path='/v1/chat/completions'):
    conn = _UnixHTTP(str(cap.socket_path), timeout=5)
    try:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        conn.request('POST', path, body=raw, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


class HostCapabilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.endpoints = []

        def factory(profile, settings):
            endpoint = FakeEndpoint(profile, settings)
            self.endpoints.append(endpoint)
            return endpoint

        patcher = mock.patch.object(directsdk_backend, 'DirectSDKBackend', side_effect=factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def capability(self, name='cap', profile='rev', quota=2, model='claude-sonnet-4-6'):
        return InferenceCapability(self.root / name, UPSTREAM, model=model, quota=quota,
                                   credential=seat(profile, model).credential_provider())

    def test_provider_marker_and_external_auth_description(self):
        inference = seat()
        credential = inference.credential_provider()
        self.addCleanup(credential.close)
        self.assertEqual(credential.backend, 'directsdk')
        self.assertFalse(credential.refreshable)
        self.assertIn('process', inference.auth_label.lower())
        self.assertNotIn('API key', inference.describe())
        self.assertNotIn('host-process', inference.describe())

    def test_process_url_without_backend_marker_is_not_an_http_capability(self):
        with self.assertRaises(ValueError):
            InferenceCapability(self.root / 'bad', UPSTREAM, 'host-process', model='m')
        self.assertEqual(self.endpoints, [])

    def test_wrong_process_destination_or_wire_mode_is_refused(self):
        for destination, mode in ((UPSTREAM + '/evil', 'chat_completions'),
                                  ('process://other-plugin', 'chat_completions'),
                                  ('https://attacker.test/v1/chat/completions', 'chat_completions'),
                                  (UPSTREAM, 'anthropic_messages'),
                                  (UPSTREAM, 'codex_responses')):
            with self.subTest(destination=destination, mode=mode), self.assertRaises(ValueError):
                InferenceCapability(self.root / 'bad', destination, model='m', api_mode=mode,
                                    credential=seat().credential_provider())
        self.assertEqual(self.endpoints, [])

    def test_model_cap_quota_and_auth_headers_stay_host_controlled(self):
        with self.capability(quota=1) as cap:
            endpoint = self.endpoints[-1]
            for payload in ([], None, b'{broken', {'n': 2}, {'n': True},
                            {'max_tokens': True}, {'max_tokens': 0},
                            {'max_tokens': 1, 'max_completion_tokens': 1}):
                with self.subTest(payload=payload):
                    self.assertEqual(post(cap, payload)[0], 400)
            self.assertEqual(post(cap, {}, path='/v1/responses')[0], 400)
            self.assertEqual(cap.used, 0)
            self.assertEqual(endpoint.requests, [])
            status, _ = post(cap, {'model': 'attacker-model', 'messages': []}, {
                'Authorization': 'Bearer hostile', 'x-api-key': 'hostile',
                'Cookie': 'hostile', 'anthropic-beta': 'hostile',
                'CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND': '/evil'})
            self.assertEqual(status, 200)
            body, headers = endpoint.requests[0]
            self.assertEqual(body['model'], 'claude-sonnet-4-6')
            self.assertEqual(body['max_tokens'], inference_proxy.MAX_OUTPUT_TOKENS)
            self.assertNotIn('hostile', str(headers))
            self.assertEqual(post(cap, {})[0], 429)
            self.assertEqual(len(endpoint.requests), 1)
        self.assertTrue(endpoint.closed)
        self.assertFalse(cap.socket_path.exists())

    def test_two_seats_do_not_share_endpoint_model_quota_or_close(self):
        with self.capability('a', 'rev', quota=1, model='model-a') as a:
            endpoint_a = self.endpoints[-1]
            with self.capability('b', 'fix', quota=2, model='model-b') as b:
                endpoint_b = self.endpoints[-1]
                self.assertIsNot(endpoint_a, endpoint_b)
                self.assertEqual((endpoint_a.profile, endpoint_b.profile), ('rev', 'fix'))
                self.assertEqual(post(a, {})[0], 200)
                self.assertEqual(post(a, {})[0], 429)
                self.assertEqual(post(b, {})[0], 200)
            self.assertTrue(endpoint_b.closed)
            self.assertFalse(endpoint_a.closed)
            self.assertEqual(endpoint_a.requests[0][0]['model'], 'model-a')
            self.assertEqual(endpoint_b.requests[0][0]['model'], 'model-b')
        self.assertTrue(endpoint_a.closed)

    def test_response_bound_closes_backend_iterator(self):
        closed = []
        with self.capability(quota=1) as cap:
            endpoint = self.endpoints[-1]

            def oversized():
                try:
                    yield b'x' * 65
                finally:
                    closed.append(True)

            endpoint.reply = oversized
            with mock.patch.object(inference_proxy, 'MAX_RESPONSE', 64):
                self.assertEqual(post(cap, {})[0], 502)
            self.assertEqual(closed, [True])
            self.assertEqual(cap.used, 1)

    def test_backend_failure_spends_quota_without_disclosing_exception(self):
        with self.capability(quota=1) as cap:
            endpoint = self.endpoints[-1]

            def failure():
                raise inference_proxy.ProxyError('HOST_SECRET_SENTINEL /private/profile/.env')

            endpoint.reply = failure
            status, body = post(cap, {})
            self.assertEqual(status, 502)
            self.assertNotIn(b'HOST_SECRET_SENTINEL', body)
            self.assertNotIn(b'/private/profile', body)
            self.assertEqual(post(cap, {})[0], 429)
            self.assertEqual(len(endpoint.requests), 1)

    def test_sse_first_event_arrives_before_backend_finishes(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with self.capability(quota=1) as cap:
            endpoint = self.endpoints[-1]
            endpoint.content_type = 'text/event-stream'

            def stream():
                yield b'data: {"id":"first"}\n\n'
                if not release.wait(3):
                    raise AssertionError('proxy buffered the first SSE event')
                yield b'data: [DONE]\n\n'

            endpoint.reply = stream
            conn = _UnixHTTP(str(cap.socket_path), timeout=4)
            try:
                conn.request('POST', '/v1/chat/completions', body=b'{"stream":true}')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                first = response.fp.readline() + response.fp.readline()
                self.assertIn(b'first', first)
                self.assertFalse(release.is_set())
                release.set()
                self.assertIn(b'[DONE]', response.read())
            finally:
                release.set()
                conn.close()


if __name__ == '__main__':
    unittest.main()
