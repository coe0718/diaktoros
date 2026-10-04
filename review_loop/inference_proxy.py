"""Per-run model capability: host-held credential, one Unix socket, fixed upstream per wire format.

The caller (the sandbox) supplies only request bodies. The upstream URL, the wire format
(``api_mode``), the credential and every authenticating header are constructor arguments on the
trusted side, never fields in the RPC request. This is a transport primitive, not a production
supervisor or billing policy.

Wire contracts (``CONTRACTS``), one per Hermes ``api_mode``:

=====================  ===========================  ====================  ====================
api_mode               sandbox-side local path      host upstream suffix  output-token field
=====================  ===========================  ====================  ====================
``chat_completions``   ``/v1/chat/completions``     ``/chat/completions`` ``max_tokens`` /
                                                                          ``max_completion_tokens``
``codex_responses``    ``/v1/responses``            ``/responses``        ``max_output_tokens``
``anthropic_messages`` ``/anthropic/v1/messages``   ``/v1/messages``      ``max_tokens``
=====================  ===========================  ====================  ====================

For every mode: the upstream is one fixed HTTP(S) URL with no userinfo, query or fragment whose
path ends in the mode's suffix; the sandbox may only POST the mode's fixed local path; the body's
``model`` is forced to the seat's model; the output-token cap is enforced in the mode's own field;
every call spends quota; and a streamed (``text/event-stream``) answer is relayed as it arrives.
Request headers from the sandbox are dropped, except a short per-mode allowlist of non-credential
headers (today only Codex's ``session_id`` and ``x-client-request-id``). ``Authorization``,
``x-api-key``, ``anthropic-beta``/``-version``, account and user-agent headers are always the host's
and can never be supplied by the sandbox. Any other ``api_mode`` (``bedrock_converse``, the
``codex_app_server`` runtime, …) is refused at construction.

Credentials: the capability takes a *credential provider* (``StaticCredential`` for an API key,
``RefreshingCredential`` for an OAuth access token the host re-resolves near expiry or once after
an upstream 401). Only the access token and host-chosen headers ever reach this module; refresh
tokens stay in the host's Hermes auth store (see ``seat_model``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

# One model request as the agent sends it: the whole conversation so far, as JSON. 1 MB stopped
# reviews of large files mid-turn (live, #281/#312: refused as "malformed", with no reason
# recorded) and is a fraction of what a 1M-token context needs. Host memory, not policy, is what
# this bounds. The in-sandbox bridge allows twice this, so an oversized request still reaches the
# host, which refuses it *with its reason recorded* (``last_error``).
MAX_REQUEST = 8_000_000
BRIDGE_MAX_REQUEST = 2 * MAX_REQUEST
MAX_RESPONSE = 4_000_000
MAX_OUTPUT_TOKENS = 4096
# The most model calls one turn's capability may grant: the largest step setting a seat may take
# (config.MAX_STEPS_RANGE, through config.model_calls), never more (#271). Each call stays bounded
# by MAX_REQUEST, MAX_OUTPUT_TOKENS and the turn's wall-clock budget.
MAX_CALLS = 250
MAX_CONNECTIONS = 8
CLIENT_TIMEOUT = 3
UPSTREAM_TIMEOUT = 120
BRIDGE_TIMEOUT = UPSTREAM_TIMEOUT + 15
BRIDGE_PORT = 18761
REFRESH_SKEW = 60
PATH = '/v1/chat/completions'          # the chat-completions local path (historical name)
UPSTREAM_SUFFIX = '/chat/completions'  # the chat-completions upstream suffix (historical name)

_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
_SAFE_VALUE = re.compile(r'^[A-Za-z0-9._:/=+-]{1,128}$')
# Never taken from the sandbox, and never accepted as a host "extra" header either: the proxy
# sets authentication itself, and the transport owns framing.
_RESERVED = frozenset({'authorization', 'x-api-key', 'proxy-authorization', 'cookie', 'host',
                       'content-length', 'transfer-encoding', 'connection', 'content-type',
                       'accept', 'te', 'upgrade', 'keep-alive', 'trailer'})


@dataclass(frozen=True)
class Contract:
    mode: str
    local_path: str
    upstream_suffix: str
    token_fields: tuple
    cap: int
    default_field: str | None     # set to the cap when the body names no output limit
    clamp: bool                   # lower an over-cap request instead of refusing it
    forward: frozenset = frozenset()   # sandbox headers allowed through (non-credential)
    fixed: tuple = ()             # host-fixed headers every request of this mode carries


CONTRACTS = {
    'chat_completions': Contract(
        'chat_completions', '/v1/chat/completions', '/chat/completions',
        ('max_tokens', 'max_completion_tokens'), MAX_OUTPUT_TOKENS, 'max_tokens', False),
    # Hermes sends no output cap on Responses unless configured; reasoning tokens count against
    # it, so this mode's cap is larger. Session-affinity headers are the only sandbox headers.
    'codex_responses': Contract(
        'codex_responses', '/v1/responses', '/responses', ('max_output_tokens',), 16384,
        'max_output_tokens', False, frozenset({'session_id', 'x-client-request-id'})),
    # Hermes always sends the model's native output ceiling (e.g. 64000) as max_tokens, so this
    # mode clamps to its cap (and keeps an extended-thinking budget below it) instead of refusing.
    'anthropic_messages': Contract(
        'anthropic_messages', '/anthropic/v1/messages', '/v1/messages', ('max_tokens',), 16384,
        'max_tokens', True, fixed=(('anthropic-version', '2023-06-01'),)),
}
SUPPORTED_MODES = frozenset(CONTRACTS)
LOCAL_PATHS = frozenset(c.local_path for c in CONTRACTS.values())
FORWARDABLE = frozenset().union(*(c.forward for c in CONTRACTS.values()))


def contract_for(mode: str) -> Contract:
    try:
        return CONTRACTS[mode]
    except (KeyError, TypeError):
        raise ValueError(f'unsupported api_mode {mode!r}') from None


def is_codex_backend(upstream: str) -> bool:
    """The ChatGPT Codex backend (subscription Responses), which rejects output-cap fields."""
    url = urlsplit(upstream)
    return (url.scheme == 'https' and url.hostname == 'chatgpt.com' and
            url.path.startswith('/backend-api/codex/'))


# -- credentials ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Credential:
    """One upstream credential: the token, how it authenticates, host-chosen extra headers."""
    token: str = field(repr=False)
    scheme: str = 'bearer'                # 'bearer' | 'x-api-key'
    headers: tuple = ()                   # ((name, value), ...) — non-credential, host-chosen
    expires_at: float | None = None       # epoch seconds, when the provider says

    def __post_init__(self):
        if (not isinstance(self.token, str) or not self.token or '\r' in self.token or
                '\n' in self.token or self.scheme not in ('bearer', 'x-api-key')):
            raise ValueError('invalid credential')
        for name, value in self.headers:
            if (not isinstance(name, str) or not _TOKEN.match(name) or
                    name.lower() in _RESERVED or not isinstance(value, str) or
                    len(value) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError('invalid host header')

    def auth_header(self) -> tuple[str, str]:
        return (('Authorization', 'Bearer ' + self.token) if self.scheme == 'bearer'
                else ('x-api-key', self.token))


class StaticCredential:
    """An API key: never refreshed. A 401 is final."""
    refreshable = False

    def __init__(self, credential: Credential):
        self._credential = credential

    def current(self) -> Credential:
        return self._credential

    def refresh(self, stale: str) -> Credential | None:
        return None

    def close(self) -> None:
        self._credential = None


class RefreshingCredential:
    """An OAuth access token the host re-resolves; the sandbox never sees any of it.

    ``refresh(stale_token)`` is the host callback (``seat_model`` re-runs Hermes's resolver for
    the seat's profile, serialized per profile); it returns a new ``Credential`` or raises.
    Near expiry (``expires_at - skew``) the next request refreshes first; an upstream 401 forces
    one refresh and one retry. Concurrent requests of this capability share one refresh.
    """
    refreshable = True

    def __init__(self, initial: Credential, refresh, *, skew: int = REFRESH_SKEW, clock=time.time):
        self._credential = initial
        self._refresh = refresh
        self._skew = skew
        self._clock = clock
        self._lock = threading.Lock()
        self._quiet_until = 0.0
        self.refreshes = 0

    def _renew(self, stale: str) -> Credential | None:
        """Under ``self._lock``: ask the host for a token that is not ``stale``."""
        if self._credential is None:
            return None
        if self._credential.token != stale:        # someone else already renewed it
            return self._credential
        self.refreshes += 1
        try:
            fresh = self._refresh(stale)
        except Exception:
            fresh = None
        if not isinstance(fresh, Credential) or fresh.token == stale:
            # Nothing newer yet: keep the token, and do not hammer the resolver.
            self._quiet_until = self._clock() + 30
            return None
        self._credential = fresh
        return fresh

    def current(self) -> Credential:
        with self._lock:
            credential = self._credential
            if credential is None:
                raise ProxyError('credential closed')
            expires = credential.expires_at
            now = self._clock()
            if expires is not None and now >= expires - self._skew and now >= self._quiet_until:
                credential = self._renew(credential.token) or credential
            return credential

    def refresh(self, stale: str) -> Credential | None:
        with self._lock:
            return self._renew(stale)

    def close(self) -> None:
        with self._lock:
            self._credential = None


# -- request policy ---------------------------------------------------------------------------

class ProxyError(Exception):
    pass


def _cap_field(payload: dict, name: str, contract: Contract) -> None:
    value = payload[name]
    if type(value) is not int or value < 1:
        raise ProxyError('invalid output token limit')
    if value > contract.cap:
        if not contract.clamp:
            raise ProxyError('invalid output token limit')
        payload[name] = contract.cap


ACTIVITY_LIMIT = 1200
REPLY_COPY = 64 * 1024


_SECRETISH = re.compile(r'(?i)\b(?:sk|pk|rk|key|token|bearer)[-_a-z0-9]*[\s:=]*[A-Za-z0-9_\-\.]{16,}'
                        r'|[A-Za-z0-9_\-]{40,}')


def provider_error(status: int, raw: bytes) -> str:
    """A failed upstream call, as an operator needs it: the status and the provider's message
    (the JSON ``error.message`` when there is one), bounded and with anything key-shaped
    redacted. Never raises."""
    try:
        text = raw.decode('utf-8', 'replace')
        try:
            payload = json.loads(text)
            error = payload.get('error') if isinstance(payload, dict) else None
            if isinstance(error, dict):
                text = str(error.get('message') or error.get('type') or text)
            elif isinstance(error, str):
                text = error
        except ValueError:
            pass
        return f'HTTP {status}: ' + _SECRETISH.sub('[REDACTED]', _text(text, 400))
    except Exception:
        return f'HTTP {status}'


def reply_activity(raw: bytes, stream: bool) -> str:
    """The tool call(s) a model reply asked for — the step the agent is now running — from a
    bounded copy of the reply (buffered JSON or chat-completions SSE). Never raises."""
    try:
        calls: dict[int, dict] = {}
        if stream:
            for line in raw.decode(errors="replace").splitlines():
                if not line.startswith("data:") or line.strip() == "data: [DONE]":
                    continue
                try:
                    event = json.loads(line[5:])
                except ValueError:
                    continue
                for choice in event.get("choices") or []:
                    for delta in ((choice.get("delta") or {}).get("tool_calls") or []):
                        slot = calls.setdefault(int(delta.get("index") or 0),
                                                {"name": "", "arguments": ""})
                        function = delta.get("function") or {}
                        slot["name"] += function.get("name") or ""
                        slot["arguments"] += function.get("arguments") or ""
        else:
            message = ((json.loads(raw).get("choices") or [{}])[0].get("message") or {})
            for index, call in enumerate(message.get("tool_calls") or []):
                function = (call or {}).get("function") or {}
                calls[index] = {"name": function.get("name") or "",
                                "arguments": function.get("arguments") or ""}
        return "; ".join(f"call {slot['name'] or '?'}: {_text(slot['arguments'], 300)}"
                         for _, slot in sorted(calls.items())[:3])
    except Exception:
        return ""


def _text(value: object, limit: int) -> str:
    """Model text or JSON, flattened to one bounded string (a part list becomes its text)."""
    if isinstance(value, list):
        value = " ".join(str(part.get("text") or part.get("content") or "")
                         if isinstance(part, dict) else str(part) for part in value)
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def last_activity(body: bytes) -> str:
    """The agent's latest step, from one validated request: its last tool call(s) and the
    result it is about to read. Bounded; never raises (it is diagnostics, not policy).

    Chat-completions shape (assistant ``tool_calls`` and ``tool`` messages); other wire shapes
    fall back to the last message's text. The sandbox's own words: an operator diagnostic, not
    a fact the loop acts on.
    """
    try:
        payload = json.loads(body)
        messages = payload.get("messages") or payload.get("input") or []
        if not isinstance(messages, list) or not messages:
            return ""
        lines = []
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant" and message.get("tool_calls"):
                for call in message["tool_calls"][:3]:
                    function = (call or {}).get("function") or {}
                    lines.append(f"call {function.get('name') or '?'}: "
                                 f"{_text(function.get('arguments') or '', 300)}")
                break
        last = messages[-1] if isinstance(messages[-1], dict) else {}
        if last.get("role") == "tool":
            lines.append(f"result: {_text(last.get('content') or '', 500)}")
        elif not lines:
            lines.append(f"{last.get('role') or 'message'}: {_text(last.get('content') or '', 500)}")
        return _text("\n".join(lines), ACTIVITY_LIMIT) if lines else ""
    except Exception:
        return ""


def bounded_request(body: bytes, model: str, contract: Contract,
                    codex_backend: bool = False) -> bytes:
    """Validate one sandbox body for ``contract``; force the model and the output cap."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProxyError('invalid JSON request') from exc
    if not isinstance(payload, dict):
        raise ProxyError('request must be a JSON object')
    present = [name for name in contract.token_fields if name in payload]
    if len(present) > 1:
        raise ProxyError('ambiguous output token limits')
    for name in present:
        _cap_field(payload, name, contract)
    if contract.mode == 'chat_completions':
        for name in ('n', 'best_of'):
            if name in payload and (type(payload[name]) is not int or payload[name] != 1):
                raise ProxyError('multiple completions are not permitted')
    elif contract.mode == 'codex_responses':
        if payload.get('background') not in (None, False):
            raise ProxyError('background responses are not permitted')
    elif contract.mode == 'anthropic_messages':
        thinking = payload.get('thinking')
        if isinstance(thinking, dict) and thinking.get('type') == 'enabled':
            budget = thinking.get('budget_tokens')
            limit = payload.get('max_tokens', contract.cap)
            if type(budget) is not int or budget < 1:
                raise ProxyError('invalid thinking budget')
            if budget >= limit:
                if limit <= 1024:
                    raise ProxyError('thinking budget does not fit the output cap')
                thinking['budget_tokens'] = limit - 1
    payload['model'] = model
    if not present and contract.default_field:
        payload[contract.default_field] = contract.cap
    if codex_backend:
        # The ChatGPT Codex backend answers 400 to an output cap (and to these fields), exactly
        # as Hermes's own codex client omits them; its cap is validated above, then dropped.
        for name in ('max_output_tokens', 'temperature', 'top_p', 'prompt_cache_retention'):
            payload.pop(name, None)
        payload['store'] = False
    return json.dumps(payload).encode('utf-8')


def _bounded_request(body: bytes, model: str) -> bytes:
    """Chat-completions policy (kept for callers of the pre-``api_mode`` API)."""
    return bounded_request(body, model, CONTRACTS['chat_completions'])


def forwarded_headers(headers, contract: Contract) -> dict:
    """The sandbox headers ``contract`` lets through: allowlisted names, plain values only."""
    kept = {}
    for name in contract.forward:
        value = headers.get(name)
        if isinstance(value, str) and _SAFE_VALUE.match(value):
            kept[name] = value
    return kept


# -- upstream ---------------------------------------------------------------------------------

class _NoRedirectConnection:
    """Open a fresh connection to the one trusted endpoint per request."""

    def __init__(self, upstream: str, suffix: str = UPSTREAM_SUFFIX):
        url = urlsplit(upstream)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or
                url.password or url.query or url.fragment or
                not url.path.endswith(suffix) or '//' in url.path or
                any(part in ('.', '..') for part in url.path.split('/')) or
                any(ord(char) < 33 or ord(char) == 127 for char in url.path)):
            raise ValueError(f'upstream must be a fixed …{suffix} URL')
        self.url = url

    def post(self, body: bytes, headers: dict):
        """POST once; return ``(status, content_type, chunks)``.

        ``chunks`` yields the body as it arrives (bounded by ``MAX_RESPONSE``) and closes the
        connection when exhausted or closed. Redirects are never followed.
        """
        from .config import guard_network
        guard_network(self.url.geturl())
        cls = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
        conn = cls(self.url.hostname or '', self.url.port, timeout=UPSTREAM_TIMEOUT)
        try:
            conn.request('POST', self.url.path, body=body, headers=headers)
            response = conn.getresponse()
        except BaseException:
            conn.close()
            raise
        # Kept for the caller that has to read a 429's reset time (#219); never a credential:
        # these are the upstream's *response* headers.
        self.last_headers = {k.lower(): v for k, v in response.getheaders()}
        content_type = response.getheader('Content-Type')
        first = b''
        if content_type is None or content_type.startswith('application/json'):
            # The body decides, not the label: an event stream sent unlabelled or labelled JSON
            # (as chatgpt.com's Codex backend was seen to) is still a stream, and buffering it as
            # one JSON body would hand the client something no JSON parser accepts.
            try:
                first = _leading(response)
            except BaseException:
                conn.close()
                raise
            if first.lstrip().startswith(_SSE_STARTS):
                content_type = 'text/event-stream'
            elif content_type is None:
                content_type = 'application/json'

        def chunks():
            total = len(first)
            try:
                if total > MAX_RESPONSE:
                    raise ProxyError('upstream response too large')
                if first:
                    yield first
                while True:
                    data = response.read1(65536)
                    if not data:
                        return
                    total += len(data)
                    if total > MAX_RESPONSE:
                        raise ProxyError('upstream response too large')
                    yield data
            finally:
                conn.close()
        return response.status, content_type, chunks()


# How an event stream's first line starts (a field, or a ``:`` comment). No JSON text starts so.
_SSE_STARTS = (b'event:', b'data:', b'id:', b'retry:', b':')


def _leading(response, limit: int = 4096) -> bytes:
    """The body's first bytes, read until something other than whitespace (or EOF, or ``limit``)
    arrived, so the shape can be judged from them; the caller relays them first."""
    head = b''
    while len(head) < limit and not head.strip():
        data = response.read1(limit - len(head))
        if not data:
            break
        head += data
    return head


def _chunks(data):
    """A buffered body (bytes) or a chunk iterator, as an iterator."""
    return iter([bytes(data)]) if isinstance(data, (bytes, bytearray)) else data


def _close(data) -> None:
    close = getattr(data, 'close', None)
    if close is not None:
        close()


def _is_stream(content_type: str) -> bool:
    return content_type.startswith('text/event-stream')


class InferenceCapability:
    """One run's model capability on a Unix socket.

    ``InferenceCapability(dir, upstream, key, model=…)`` keeps the pre-``api_mode`` call shape (a
    static bearer key, chat-completions). Pass ``credential=`` (a ``StaticCredential`` or
    ``RefreshingCredential``) and ``api_mode=`` for the other contracts.
    """

    def __init__(self, socket_dir: Path, upstream: str, key: str | None = None, *, model: str,
                 quota: int = 8, api_mode: str = 'chat_completions', credential=None):
        contract = contract_for(api_mode)   # unsupported modes: refused before any credential use
        if credential is None:
            if not isinstance(key, str) or not key or '\r' in key or '\n' in key:
                raise ValueError('invalid key, model or quota')
            credential = StaticCredential(Credential(key))
        elif key is not None or not hasattr(credential, 'current'):
            raise ValueError('pass either a key or a credential provider')
        if (not isinstance(model, str) or not model or type(quota) is not int or
                not 1 <= quota <= MAX_CALLS):
            raise ValueError('invalid key, model or quota')
        self.contract = contract
        self.process_backend = getattr(credential, 'backend', '') == 'directsdk'
        if self.process_backend:
            from .directsdk_backend import DirectSDKBackend, UPSTREAM
            if upstream != UPSTREAM or api_mode != 'chat_completions':
                raise ValueError('invalid DirectSDK destination or contract')
            self.endpoint = DirectSDKBackend(credential.profile, credential.settings)
        else:
            self.endpoint = _NoRedirectConnection(upstream, contract.upstream_suffix)
        self.codex_backend = contract.mode == 'codex_responses' and is_codex_backend(upstream)
        self.credential = credential
        # When the upstream last answered 429, the time its usage window reopens (#219), so the
        # host can hold the seat instead of retrying into a closed window.
        self.rate_limited_until: float | None = None
        # What the agent was last doing, read from its latest request (``last_activity``): a turn
        # killed at its budget leaves no output of its own, and this is how the operator learns
        # whether it hung on a test, a build or the model.
        self.last_activity = ''
        # The provider's own words the last time a call failed (HTTP status and a bounded,
        # redacted body), host-side: the agent sees only a generic error, and an operator needs
        # to know whether it was a context-length limit, a bad parameter or an outage.
        self.last_error = ''
        self.model = model
        self.quota = quota
        self.used = 0
        self.lock = threading.Lock()
        self.directory = Path(socket_dir)
        self.socket_path = self.directory / 'model.sock'
        self.server = None
        self.thread = None

    @property
    def key(self) -> str:
        """The current token (host-side only; kept for callers and tests of the old API)."""
        try:
            return self.credential.current().token
        except Exception:
            return ''

    def _headers(self, credential: Credential, sandbox_headers) -> dict:
        headers = forwarded_headers(sandbox_headers, self.contract)
        headers.update(self.contract.fixed)
        headers.update(credential.headers)
        headers['Content-Type'] = 'application/json'
        headers['Accept'] = 'text/event-stream, application/json'
        name, value = credential.auth_header()
        headers[name] = value
        return headers

    def forward(self, body: bytes, sandbox_headers):
        """Send one policy-checked body upstream; refresh and retry once on a 401."""
        if self.process_backend:
            return self.endpoint.post(body, {})
        credential = self.credential.current()
        status, content_type, data = self.endpoint.post(body, self._headers(credential, sandbox_headers))
        if status == 401 and getattr(self.credential, 'refreshable', False):
            _close(data)
            fresh = self.credential.refresh(credential.token)
            if fresh is None:
                return 401, 'application/json', iter([b'{"error": "upstream rejected the credential"}'])
            status, content_type, data = self.endpoint.post(body, self._headers(fresh, sandbox_headers))
        return status, content_type, data

    def note_rate_limit(self, headers: dict, body: bytes) -> None:
        """Record when a 429'd window reopens — only when the provider *says* when. A bare 429
        (often a per-minute limit) stays an ordinary failure with the ordinary backoff: the
        host never guesses a usage window."""
        from . import pacing
        until = pacing.parse_reset(headers or {}, body, time.time())
        if until is None:
            return
        with self.lock:
            self.rate_limited_until = max(self.rate_limited_until or 0.0, until)

    def __enter__(self):
        self.directory.mkdir(mode=0o700)
        os.chmod(self.directory, 0o700)
        if self.socket_path.exists():
            raise FileExistsError(self.socket_path)
        capability = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def refuse(self, reason: str) -> None:
                # Every refusal names itself host-side: the agent only ever sees a bare 400,
                # which Hermes reports as "the provider rejected a malformed request".
                capability.last_error = f'proxy refused: {reason}'
                self.send_error(400)

            def do_POST(self):
                length = self.headers.get('Content-Length', '')
                if self.headers.get('Transfer-Encoding') or not length.isdecimal():
                    self.refuse('request without a plain Content-Length')
                    return
                if not 0 < int(length) <= MAX_REQUEST:
                    self.refuse(f'request of {int(length)} bytes is over the {MAX_REQUEST}-byte '
                           'limit (the conversation has outgrown one request)')
                    return
                body = self.rfile.read(int(length))
                if self.path != capability.contract.local_path:
                    # Refused after reading the (bounded) body, so the client gets its 400
                    # instead of a reset while it is still sending.
                    self.refuse(f'unexpected path {self.path[:80]!r}')
                    return
                if len(body) != int(length):
                    self.refuse('request body shorter than its Content-Length')
                    return
                try:
                    body = bounded_request(body, capability.model, capability.contract,
                                           capability.codex_backend)
                except ProxyError as exc:
                    self.refuse(str(exc))
                    return
                capability.last_activity = last_activity(body)
                # Reserve quota before contacting provider, including failed requests. A
                # refresh-and-retry after a 401 is part of the same call.
                with capability.lock:
                    if capability.used >= capability.quota:
                        self.send_error(429)
                        return
                    capability.used += 1
                try:
                    status, content_type, data = capability.forward(body, self.headers)
                except (OSError, ProxyError, http.client.HTTPException) as exc:
                    capability.last_error = f'upstream unreachable: {type(exc).__name__}'
                    self.send_error(502)
                    return
                if not (content_type.startswith('application/json') or _is_stream(content_type)):
                    _close(data)
                    self.send_error(502)
                    return
                if status == 429:
                    # The provider's own 429 (the quota refusal above never reaches here): note
                    # when its window reopens, then relay the answer unchanged.
                    try:
                        data = b''.join(_chunks(data))[:MAX_RESPONSE]
                    except (OSError, ProxyError, http.client.HTTPException):
                        data = b''
                    capability.note_rate_limit(getattr(capability.endpoint, 'last_headers', {}), data)
                if hasattr(data, "set_peer"):
                    data.set_peer(self.connection)
                seen = bytearray()
                try:
                    _relay(self, status, content_type, data, seen)
                finally:
                    reply = reply_activity(bytes(seen), _is_stream(content_type))
                    capability.last_activity = (f"{capability.last_activity} → then {reply}"
                                                if reply else capability.last_activity)
                    if status >= 400:
                        capability.last_error = provider_error(status, bytes(seen))

            def do_GET(self):
                self.send_error(405)

        class Server(_BoundedThreads, socketserver.UnixStreamServer):
            pass

        self.server = Server(str(self.socket_path), Handler)
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        if self.process_backend:
            self.endpoint.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.socket_path.unlink(missing_ok=True)
        # Do not retain the credential beyond the capability lifetime.
        self.credential.close()


def _relay(handler, status: int, content_type: str, data, seen: bytearray | None = None) -> None:
    """Answer ``handler`` with an upstream reply: buffered JSON, or SSE streamed as it arrives.

    A stream is sent without ``Content-Length`` and ends when the connection closes (HTTP/1.0
    framing), so the client sees every event when the upstream sends it. ``seen``, when given,
    receives a bounded copy of what was relayed (diagnostics: ``reply_activity``).
    """
    chunks = _chunks(data)
    try:
        if not _is_stream(content_type):
            try:
                body = b''.join(chunks)
            except (OSError, ProxyError, http.client.HTTPException):
                handler.send_error(502)
                return
            if len(body) > MAX_RESPONSE:
                handler.send_error(502)
                return
            if seen is not None:
                seen.extend(body[:REPLY_COPY])
            handler.send_response(status)
            handler.send_header('Content-Type', content_type)
            handler.send_header('Content-Length', str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        handler.send_response(status)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        handler.close_connection = True
        try:
            for chunk in chunks:
                handler.wfile.write(chunk)
                handler.wfile.flush()
                if seen is not None and len(seen) < REPLY_COPY:
                    seen.extend(chunk[:REPLY_COPY - len(seen)])
        except (OSError, ProxyError, http.client.HTTPException):
            return  # truncated stream: the client sees an incomplete event stream
    finally:
        _close(chunks)


class _BoundedThreads(socketserver.ThreadingMixIn):
    daemon_threads = True
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, *args, **kwargs):
        self._connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(CLIENT_TIMEOUT)
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            # The client timeout bounds a slow *request*; once it is read, the answer may take
            # as long as the upstream does.
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        socketserver.BaseServer.handle_error(self, request, client_address)


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = BRIDGE_TIMEOUT):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def bridge(entry: list[str], socket_path: str = '/opt/inference/model.sock') -> int:
    """Run localhost HTTP and the real Hermes CLI in the same sandbox process tree.

    Runs inside the sandbox, so it is not a trust boundary: it forwards only the known local
    paths with a JSON content type (plus the non-credential allowlisted headers) and relays the
    host's answer, streaming it when it is an event stream. The host capability enforces policy.
    """
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            length = self.headers.get('Content-Length', '')
            if (self.headers.get('Transfer-Encoding') or
                    not length.isdecimal() or not 0 < int(length) <= BRIDGE_MAX_REQUEST):
                self.send_error(400)
                return
            body = self.rfile.read(int(length))
            if self.path not in LOCAL_PATHS:
                self.send_error(400)
                return
            conn = _UnixHTTP(socket_path)
            try:
                # Never forward Hermes's dummy Authorization or caller-selected headers.
                headers = {'Content-Type': 'application/json'}
                headers.update({name: self.headers[name] for name in FORWARDABLE
                                if self.headers.get(name) is not None})
                conn.request('POST', self.path, body=body, headers=headers)
                response = conn.getresponse()
                content_type = response.getheader('Content-Type', 'application/json')

                def chunks():
                    total = 0
                    while True:
                        data = response.read1(65536)
                        if not data:
                            return
                        total += len(data)
                        if total > MAX_RESPONSE:
                            raise ProxyError('response too large')
                        yield data
                _relay(self, response.status, content_type, chunks())
            except (OSError, ProxyError, http.client.HTTPException):
                self.send_error(502)
            finally:
                conn.close()

    class Server(_BoundedThreads, http.server.HTTPServer):
        pass

    server = Server(('127.0.0.1', BRIDGE_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return subprocess.call(entry)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    if len(sys.argv) < 3 or sys.argv[1] != 'bridge' or sys.argv[2] != '--':
        raise SystemExit('usage: python -m review_loop.inference_proxy bridge -- COMMAND...')
    raise SystemExit(bridge(sys.argv[3:]))
