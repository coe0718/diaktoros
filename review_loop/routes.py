"""Webhook routes: read the gateway's subscription file, and fire a signed POST at it.

The loop does not own the gateway, so it does not invent a second way to reach agents. It
writes routes through the same config file the gateway already reads (``new_route``) and
wakes a seat by POSTing a GitHub-shaped payload with a valid signature (``fire``).

A route's URL is derived from its ``profile``: the gateway serves the launch profile at
``/webhooks/<name>`` and every other profile at ``/p/<profile>/webhooks/<name>``.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import pathlib
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager

from . import config, hostdirs
from .util import log


def subs_path() -> pathlib.Path:
    override = os.environ.get("REVIEW_LOOP_SUBS")
    return (config.guard_real_home(pathlib.Path(override).expanduser()) if override
            else config.home() / "webhook_subscriptions.json")


def all_routes() -> dict:
    try:
        return json.loads(subs_path().read_text())
    except Exception:
        return {}


def _parse_for_write(path: pathlib.Path, raw: bytes | None) -> dict:
    """Unlike best-effort reads, writes must not replace an unreadable registry."""
    if raw is None:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"route registry {path} must be a JSON object")
    return data


# -- optimistic concurrency against writers that do not take our lock -----------------------
#
# Hermes's own CLI/dashboard subscription writers rewrite this file without the plugin's
# ``flock``. So every plugin read-modify-write records the file's identity when it reads, and
# re-checks it immediately before ``os.replace``: if a native writer published in between, the
# plugin re-reads and re-applies its edit instead of publishing a registry built from stale
# bytes. What remains is the gap between that last check and the ``rename`` itself (a few
# syscalls), plus a native writer that read *before* our publish and writes *after* it — that
# one overwrites us, and only the intent record + self-heal (``route_intent``) repairs it.

CONFLICT_RETRIES = 5


class RegistryConflictError(OSError):
    """A non-cooperating writer kept changing the registry; nothing was published."""

    published = False


def _identity_of(st: os.stat_result | None, raw: bytes | None):
    if st is None:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size,
            hashlib.sha256(raw or b"").hexdigest())


def _snapshot(path: pathlib.Path):
    """(identity, bytes) read from one open file; (None, None) when the file does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, None
    try:
        st = os.fstat(fd)
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    return _identity_of(st, raw), raw


def _identity(path: pathlib.Path):
    """The registry's identity right now: inode, mtime, size and a content hash."""
    return _snapshot(path)[0]


class _RegistryChanged(Exception):
    pass


def _transact(path: pathlib.Path, edit):
    """Locked, optimistic read-modify-write of the registry.

    ``edit(data)`` mutates the parsed registry in place and returns ``(write, result)``. It must
    be a pure function of ``data``: on a detected concurrent write it is re-run against the
    fresh bytes, so a native writer's edit survives alongside ours.
    """
    with _registry_lock(path):
        for _ in range(CONFLICT_RETRIES):
            identity, raw = _snapshot(path)
            data = _parse_for_write(path, raw)
            write, result = edit(data)
            if not write:
                return result
            try:
                _write_registry(path, data, expected=identity)
            except _RegistryChanged:
                log(f"route registry {path.name} changed under a plugin edit; re-applying")
                continue
            return result
    raise RegistryConflictError(
        f"route registry {path} kept changing under the plugin's edit "
        f"({CONFLICT_RETRIES} attempts); nothing was published")


@contextmanager
def _registry_lock(path: pathlib.Path):
    """Serialize cooperating plugin writers on a persistent sibling inode.

    Hermes CLI/dashboard subscription writers do not take this lock.
    """
    hostdirs.ensure(path.parent)
    lock = path.with_name(path.name + ".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class RegistryDurabilityError(OSError):
    """Replacement is visible, but directory sync failed; durability is unconfirmed."""

    published = True


_UNCHECKED = object()


def _write_registry(path: pathlib.Path, data: dict, expected=_UNCHECKED) -> None:
    """Publish owner-only bytes atomically, then sync the containing directory.

    With ``expected`` (an identity from ``_snapshot``), the live file is re-checked immediately
    before ``os.replace``; a mismatch raises ``_RegistryChanged`` and publishes nothing.

    Before replacement, failures leave the old inode intact. After replacement,
    a directory sync failure raises RegistryDurabilityError: the new bytes are
    visible but their survival across a crash has not been confirmed.
    """
    body = json.dumps(data, indent=2).encode()
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(body)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write to route registry")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if expected is not _UNCHECKED and _identity(path) != expected:
            raise _RegistryChanged()
        os.replace(temp, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise RegistryDurabilityError(
                f"route registry {path} was published but directory sync failed; durability unconfirmed"
            ) from exc
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def route(name: str) -> dict | None:
    entry = all_routes().get(name)
    return entry if isinstance(entry, dict) else None


def route_profile(entry: dict) -> str | None:
    """The profile a registry entry is served under, read exactly the way the gateway reads it.

    Hermes's ``WebhookAdapter._route_allows_profile``: a route with no ``profile`` key is bound to
    ``default``; an explicit null, blank or non-string profile matches no request at all (it
    fails closed), which is ``None`` here. Everything that compares a route's profile — the
    feed's delivery contract, doctor, status, apply's readback — goes through this, so none of
    them can call a route healthy that the gateway refuses, or refuse one it serves.
    """
    profile = entry.get("profile") if "profile" in entry else "default"
    if not isinstance(profile, str) or not profile.strip():
        return None
    return profile.strip()


def contract_mismatch(entry: dict, expected: dict) -> list[str]:
    """The ``expected`` keys a registry entry does not honour (``[]`` when it matches).

    ``profile`` goes through :func:`route_profile`; an absent ``deliver_extra`` is ``{}``;
    ``enabled`` is the gateway's reading (only an explicit ``false`` turns a route off, and it
    then answers 403); every other key must be equal as stored.
    """
    wrong = []
    for key, value in expected.items():
        if key == "profile":
            ok = route_profile(entry) == value
        elif key == "enabled":
            ok = (entry.get("enabled", True) is not False) == value
        elif key == "deliver_extra":
            ok = (entry.get(key) or {}) == value
        else:
            ok = entry.get(key) == value
        if not ok:
            wrong.append(key)
    return wrong


def route_name_of(url: str) -> str:
    """The route a webhook URL posts to: its complete last ``/webhooks/<name>`` segment (``""``
    when it has none). One spelling of the rule for every caller — a substring match would take
    another route whose name merely contains this one.

    The name is percent-decoded and stripped of trailing slashes after the split, so a hook posted
    at ``.../webhooks/<name>%2F`` (a literal ``%2F``, not an encoded ``/``) resolves to ``<name>``
    rather than the never-present ``<name>%2F`` (#133). It is then judged by the raw path —
    ``same_webhook_url``/``exact_hook_url`` compare bytes, so the gateway's 404 for that spelling
    still reports it unserved; only the *name* is recovered so ``arm`` and ``doctor`` name the hook
    instead of saying no hook posts to the route.
    """
    path = urllib.parse.urlsplit(str(url or "")).path.rstrip("/")
    if "/webhooks/" not in path:
        return ""
    return urllib.parse.unquote(path.rsplit("/webhooks/", 1)[-1]).rstrip("/")


def _url_parts(url: str) -> tuple[str, str, str]:
    parts = urllib.parse.urlsplit(str(url or ""))
    scheme = parts.scheme.lower()
    # Userinfo is not part of where the request goes, and the scheme's default port is the
    # same origin as no port: ``https://gw:443/x`` and ``https://user@gw/x`` reach ``https://gw/x``.
    host = parts.netloc.rpartition("@")[2].lower()
    default = {"https": ":443", "http": ":80"}.get(scheme)
    if default and host.endswith(default):
        host = host[:-len(default)]
    return scheme, host, parts.path


def serves_route_url(hook_url: str, route_url: str) -> bool:
    """Does a hook posting to ``hook_url`` reach the route served at ``route_url``?

    The gateway (aiohttp) routes on the exact PATH: scheme and host compare case-insensitively,
    the path exactly, and a query string or fragment is ignored — ``?x=1`` reaches the handler,
    while a trailing slash, a doubled slash or ``%2F`` is a 404. The one rule every surface uses
    to decide whether a hook is *correct* (armed, verified, left alone)."""
    route = _url_parts(route_url)
    return bool(route[2]) and _url_parts(hook_url) == route


def same_webhook_url(a: str, b: str) -> bool:
    """Same origin and the same path up to a trailing slash: how a hook the gateway would 404
    only for that slash is *found* for its route. Whether it is *correct* is ``serves_route_url``."""
    pa, pb = _url_parts(a), _url_parts(b)
    return pa[:2] == pb[:2] and pa[2].rstrip("/") == pb[2].rstrip("/")


def url_for_profile(name: str, profile: str | None, host: str | None = None) -> str | None:
    """The URL a route *has* under a profile — the same shape the gateway serves.

    Split out from ``url_for`` so a preview can show the URL a route is about to get before the
    registry holds it: the profile is part of the URL, which is exactly why changing a seat's
    profile is a route change and not only a config edit.
    """
    base = config.webhook_host(host)
    if not base:
        return None
    if not profile or profile == "default":
        return f"{base}/webhooks/{name}"
    return f"{base}/p/{profile}/webhooks/{name}"


def url_for(name: str, host: str | None = None) -> str | None:
    entry = route(name)
    if not entry:
        return None
    # Never invent a relative webhook URL when neither the caller nor the route
    # names an operator-owned gateway. Reject malformed origins at this boundary.
    # A malformed *stored* origin raises here, which is the caller's to refuse loudly.
    base = config.webhook_host(host or entry.get("host")) or ""
    if not base:
        return None
    profile = route_profile(entry)
    if profile is None:
        return None                   # the gateway serves this route under no URL at all
    return url_for_profile(name, profile, base)


def target(name: str, host: str | None = None, *, expected: dict | None = None):
    """(url, secret_bytes) for a route, or None when it is missing or has no secret."""
    entry = route(name)
    if not entry:
        log(f"route {name!r} not found in {subs_path().name}")
        return None
    if expected is not None and contract_mismatch(entry, expected):
        log(f"route {name!r} no longer matches its delivery contract")
        return None
    secret = entry.get("secret") or ""
    profile = route_profile(entry)
    if profile is None:
        log(f"route {name!r} has a blank or invalid profile: the gateway refuses it")
        return None
    try:
        base = config.webhook_host(host or entry.get("host"))
        url = (f"{base}/webhooks/{name}" if profile == "default"
               else f"{base}/p/{profile}/webhooks/{name}") if base else None
    except config.ConfigError as exc:
        log(f"route {name!r} has invalid webhook host: {exc}")
        return None
    if not secret or not url:
        log(f"route {name!r} has no secret/url")
        return None
    return url, secret.encode()


def delivery_id(tag: str) -> str:
    """A fresh ``X-GitHub-Delivery`` for one notice: the tag, plus entropy.

    The gateway keys its 3600s idempotency window on this header, so it has to identify *the
    notice*. It used to be ``tag + int(time.time())``, and the tags repeat by construction — they
    are per event and per PR ("opened-7", "digest-3", "drain-review-7") — so two distinct notices
    sharing a tag inside one second collapsed onto one delivery id: the gateway dropped the
    second and the loop recorded it as delivered. A uuid4 suffix makes each distinct notice its
    own delivery; the tag stays in front of it so an operator reading the header still knows what
    it was.
    """
    return f"{tag}-{uuid.uuid4().hex}"


def fire(name: str, event: str, payload: dict, tag: str, host: str | None = None,
         *, expected: dict | None = None, on_attempt=None, on_unsent=None, delivery: str | None = None) -> bool:
    """POST a signed payload; on_attempt marks the boundary before transport I/O.

    A false result before that callback is known not delivered; a false result
    after it may have reached the gateway and must not be blindly replayed. A failure that
    provably happens before any byte is sent (DNS failure, connection refused) calls
    ``on_unsent`` so the caller can withdraw the mark and keep the work retryable.

    ``delivery`` pins the ``X-GitHub-Delivery`` header. Leave it out for a notice the gateway has
    not seen — a distinct notice is a distinct delivery — and pass the id the first attempt used
    when re-sending *one* logical delivery: the header is the gateway's idempotency key, so a
    retry of the same notice is then deduplicated if the first attempt did reach the gateway
    after all, instead of the second copy being silently swallowed.
    """
    target_ = target(name, host, expected=expected)
    if not target_:
        return False
    url, secret = target_
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest(),
        "X-GitHub-Delivery": delivery or delivery_id(tag),
        "User-Agent": "diaktoros",
    })
    try:
        config.guard_network(req.full_url)
        if on_attempt is not None:
            on_attempt()
        with urllib.request.urlopen(req, timeout=20) as resp:
            log(f"fired {name} for {tag} (HTTP {resp.status})")
            return 200 <= resp.status < 300
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()   # it holds the response open
        log(f"could not fire {name} for {tag}: {exc}")
        unsent = (ConnectionRefusedError, socket.gaierror)
        if on_unsent is not None and not isinstance(exc, urllib.error.HTTPError) and (
                isinstance(exc, unsent) or isinstance(getattr(exc, "reason", None), unsent)):
            on_unsent()
        return False


def new_route(name: str, *, profile: str, prompt: str, events: list[str], script: str,
               deliver: str, description: str = "", skills: list[str] | None = None,
               host: str | None = None, deliver_only: bool = False,
               deliver_extra: dict | None = None) -> dict:
    """Create (or update) a route entry and write it back to the gateway's file.

    The secret is generated here, not asked for. 0600, same file the gateway reads.

    ``deliver_only`` is the gateway's own "no agent here" mode: the rendered prompt *is* the
    message that reaches ``deliver``, with no model run and nothing to review afterwards. That
    is what makes an observer route a feed rather than a third seat.
    """
    import secrets as _secrets

    path = subs_path()

    def edit(data: dict):
        prior = data.get(name) or {}
        if not isinstance(prior, dict):
            raise ValueError(f"route {name!r} must be a JSON object")
        entry = {
            "description": description or prior.get("description", ""),
            "events": list(events),
            "secret": prior.get("secret") or secret,
            "prompt": prompt,
            "skills": list(skills or prior.get("skills") or []),
            "deliver": deliver,
            "profile": profile,
            "created_at": prior.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "script": script,
        }
        # Written only when asked for, so the seats' routes keep exactly the shape they had:
        # an entry that gains a key the gateway has not seen yet is a change nobody reviewed.
        if deliver_only:
            entry["deliver_only"] = True
        if deliver_extra:
            entry["deliver_extra"] = dict(deliver_extra)
        if host:
            entry["host"] = host
        data[name] = entry
        return True, entry

    # Generated once, outside the retry loop: a re-applied edit must not mint a second secret.
    secret = _secrets.token_hex(32)
    return _transact(path, edit)


def remove_route(name: str) -> bool:
    def edit(data: dict):
        if name not in data:
            return False, False
        data.pop(name)
        return True, True

    return _transact(subs_path(), edit)


def restore_entries(entries: dict[str, dict | None]) -> None:
    """Restore only owned route entries after an incomplete multi-route update."""
    def edit(data: dict):
        for name, entry in entries.items():
            if entry is None:
                data.pop(name, None)
            else:
                data[name] = entry
        return True, None

    _transact(subs_path(), edit)


def heal_entries(expected: dict[str, dict], fields, owned) -> tuple[dict, dict]:
    """Put back the plugin's own routes a non-cooperating writer erased or rewrote.

    ``expected`` is the plugin's intent record (name → full entry); ``fields`` is the watched
    keys, one tuple for every name or a name → tuple map. Under the lock, against the
    live bytes, each name is: left alone when every watched ``field`` already matches; restored
    when missing, or present and still ``owned(entry)`` (one of this plugin's gate scripts);
    reported as a conflict — never overwritten — when something else now holds the name.
    Other names in the registry are not read for anything but preservation. A malformed
    registry raises (fail closed) exactly like every other plugin write.

    Returns ``(restored, conflicts)``: name → list of fields restored ("missing" for an erased
    route), and name → reason.
    """
    def edit(data: dict):
        restored: dict = {}
        conflicts: dict = {}
        for name, want in expected.items():
            live = data.get(name)
            if live is None:
                data[name] = dict(want)
                restored[name] = ["missing"]
                continue
            if not isinstance(live, dict):
                conflicts[name] = "registry entry is not a JSON object"
                continue
            watched = fields.get(name, ()) if isinstance(fields, dict) else fields
            diff = [key for key in watched if live.get(key) != want.get(key)]
            if not diff:
                continue
            if not owned(live):
                conflicts[name] = (f"now runs {live.get('script')!r}, not a Diaktoros gate — "
                                   "something else holds this name")
                continue
            merged = {**live, **want}
            for key in watched:         # a watched key the plugin never wrote is not kept either
                if key not in want:
                    merged.pop(key, None)
            data[name] = merged
            restored[name] = diff
        return bool(restored), (restored, conflicts)

    return _transact(subs_path(), edit)
