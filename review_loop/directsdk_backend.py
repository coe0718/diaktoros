"""Host-only native subscription transport behind the existing inference capability.

The installed provider owns native history, reasoning, tool projection and CLI
admission. This adapter only owns isolation, policy and bounded wire framing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import select
import signal
import socket
import subprocess
import threading
import tempfile
import time

PROVIDER = 'claude-subscription-directsdk-experimental'
UPSTREAM = 'process://' + PROVIDER
TIMEOUT = 120


class ProcessCredential:
    backend = 'directsdk'
    refreshable = False

    def __init__(self, profile, settings):
        self.profile = profile
        self.settings = settings

    def current(self):
        from .inference_proxy import Credential
        return Credential('host-process')

    def close(self):
        pass


class DirectSDKBackend:
    def __init__(self, profile, settings):
        from . import seat_model, config
        self.profile = profile
        self.home = config.profile_dir(profile)
        self.python, self.source = seat_model.hermes_interpreter(settings)
        # Host-chosen installed plugin, never any path supplied by an inference request.
        self.plugin = config.home() / 'plugins' / PROVIDER
        if not (self.plugin / 'directsdk.py').is_file():
            raise ValueError('installed DirectSDK provider is unavailable')
        self.lock = threading.Lock()
        self.active = set()
        self.closed = False

    def post(self, body, headers):
        from .inference_proxy import ProxyError, MAX_REQUEST
        try:
            payload = json.loads(body)
            validate_payload(payload)
        except (ValueError, TypeError, KeyError):
            raise ProxyError('invalid DirectSDK request') from None
        if len(body) > MAX_REQUEST:
            raise ProxyError('DirectSDK request too large')
        from . import config, util
        import pwd
        user_home = os.environ.get('HOME', '/') if config.test_guard_active() else pwd.getpwuid(os.getuid()).pw_dir
        env = {'HOME': user_home, 'HERMES_HOME': str(self.home), 'PATH': '/usr/local/bin:/usr/bin:/bin',
               'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_DISABLE_LAZY_INSTALLS': '1',
               'TMPDIR': os.environ.get('TMPDIR', str(Path(__file__).parent))}
        env = util.leak_guard_env(env, pythonpath=False)
        request = tempfile.TemporaryFile(dir=env['TMPDIR'])
        request.write(body)
        request.seek(0)
        try:
            with self.lock:
                if self.closed:
                    raise ProxyError('DirectSDK capability closed')
                process = subprocess.Popen([self.python, '-E', '-s', '-B',
                str(Path(__file__).with_name('directsdk_child.py')), self.source, str(self.plugin)],
                    stdin=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    cwd=str(self.home), env=env, start_new_session=True)
                reply = ProcessReply(process, self)
                self.active.add(reply)
        finally:
            request.close()
        return 200, 'text/event-stream' if payload.get('stream') else 'application/json', reply

    def close(self):
        with self.lock:
            self.closed = True
            active = list(self.active)
        for reply in active:
            reply.close()


def validate_payload(payload):
    """No process configuration or unbounded nested token override crosses the socket."""
    allowed = {'model', 'messages', 'tools', 'stream', 'stream_options', 'max_tokens',
               'max_completion_tokens', 'temperature', 'top_p', 'stop', 'tool_choice',
               'parallel_tool_calls', 'n', 'response_format', 'reasoning', 'extra_body'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError('unknown request field')
    if not isinstance(payload.get('messages'), list):
        raise ValueError('messages must be a list')
    if 'stream' in payload and type(payload['stream']) is not bool:
        raise ValueError('stream must be boolean')
    extra = payload.get('extra_body', {})
    if not isinstance(extra, dict) or set(extra) - {'reasoning', 'response_format'}:
        raise ValueError('unknown extra_body field')
    if 'reasoning' in payload:
        if 'reasoning' in extra:
            raise ValueError('ambiguous reasoning')
        extra = dict(extra, reasoning=payload.pop('reasoning'))
        payload['extra_body'] = extra
    reasoning = extra.get('reasoning')
    if reasoning is not None and (not isinstance(reasoning, dict) or
            set(reasoning) - {'enabled', 'effort'}):
        raise ValueError('invalid reasoning')


class ProcessReply:
    def __init__(self, process, backend):
        self.process = process
        self.backend = backend
        self.deadline = time.monotonic() + TIMEOUT
        self.total = 0
        self.closed = False
        self.peer = None
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)
        self.lock = threading.Lock()

    def set_peer(self, peer):
        self.peer = peer

    def __iter__(self):
        return self

    def __next__(self):
        from .inference_proxy import MAX_RESPONSE, ProxyError
        while not self.closed:
            # Python's positive socket timeout polls before recv even with MSG_DONTWAIT.
            # An idle, connected client must not consume CLIENT_TIMEOUT then fail inference.
            if self.peer is not None and select.select([self.peer], [], [], 0)[0]:
                try:
                    if self.peer.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b'':
                        self.close()
                        raise ProxyError('inference client disconnected')
                except BlockingIOError:
                    pass
            if time.monotonic() >= self.deadline:
                self.close()
                raise ProxyError('DirectSDK inference timeout')
            if not self.selector.select(0.1):
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                rc = self.process.wait(timeout=1)
                self.close()
                if rc:
                    raise ProxyError('DirectSDK host process failed')
                raise StopIteration
            self.total += len(chunk)
            if self.total > MAX_RESPONSE:
                self.close()
                raise ProxyError('DirectSDK response too large')
            return chunk
        raise StopIteration

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.process.poll() is None:
                # The helper's TERM handler invokes Client.close(), which kills the native
                # client's separately-owned process group before exiting the helper.
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
            self.selector.close()
            self.process.stdout.close()
        with self.backend.lock:
            self.backend.active.discard(self)
