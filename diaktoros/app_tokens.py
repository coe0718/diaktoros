"""Installation tokens shared by the host's processes (#1780).

GitHub's ETags follow the credential that asked (``Vary: Authorization``): an unchanged resource
read with a new installation token answers 200 with a new tag, and that 200 is counted. Each gate,
worker and watchdog sweep is its own process, so a token kept only in process memory
(``github_app._tokens``) was minted afresh by each of them, and the conditional reads of #1653
almost never matched for an App identity. Measured: 18 of 29 re-stored entries came back with a
byte-identical body and a new tag.

So a minted token is kept here, one 0600 file per App identity, repository and App id, holding the
token and its expiry only, and every host process uses it until ``github_app.REUSE_MARGIN_S``
before it expires. One process mints at a time (``held``), so two never replace each other's.

What may touch it, as for the read cache (``gh_cache``): a writer process (the watchdog and the
gates) stores, and a worker process stores only into a directory the host made as it started it
(``prepare``). Any other process, such as ``doctor`` or the Desktop, which promise to write
nothing, reads a stored token but never stores, locks or drops one. The sandboxed turn never sees
it: the sandbox binds its own home, never the host's state. The App's private key, which can mint
these tokens anyway, is on the same host; a token is for one repository and lives an hour.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import pathlib


def directory(home: pathlib.Path | None = None) -> pathlib.Path:
    from . import config
    return (config.home() if home is None else pathlib.Path(home)) / "state" / "diaktoros-app-tokens"


def prepare(home: pathlib.Path | None = None) -> None:
    """Make the directory, host-side, before a worker is started: a worker shares tokens only
    through a directory that exists and never makes one. Does nothing in a worker; best effort."""
    from . import hostdirs
    if hostdirs.in_worker():
        return
    try:
        directory(home).mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        pass


def _writable() -> bool:
    from . import gh_cache, hostdirs
    if hostdirs.in_worker():
        return directory().is_dir()
    return gh_cache.writes_enabled()


def _file(key: tuple) -> pathlib.Path:
    return directory() / hashlib.sha256("\0".join(map(str, key)).encode()).hexdigest()


@contextlib.contextmanager
def held():
    """One minter at a time across the host's processes; a process that may not write takes no
    lock (and so writes nothing). Best effort: an unusable directory is an unshared mint."""
    if not _writable():
        yield
        return
    try:
        directory().mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (directory() / ".lock").open("a+b")
    except OSError:
        yield
        return
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load(key: tuple) -> tuple[str, float] | None:
    """The stored ``(token, expires_at)`` for ``key``, or None (missing, unreadable, malformed)."""
    try:
        entry = json.loads(_file(key).read_text())
        token, expires = entry["token"], float(entry["expires"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (token, expires) if isinstance(token, str) and token else None


def save(key: tuple, token: str, expires: float) -> None:
    """Share a freshly minted token; best effort, never raises."""
    if not _writable():
        return
    from .util import atomic_write
    try:
        atomic_write(_file(key), json.dumps({"token": token, "expires": expires}))     # 0600
    except (OSError, TypeError, ValueError):
        pass


def drop(key: tuple | None = None) -> None:
    """Forget a stored token (GitHub refused it), or every one with no ``key``; best effort."""
    if not _writable():
        return
    try:
        files = [_file(key)] if key is not None else [
            f for f in directory().iterdir() if not f.name.startswith(".")]
    except OSError:
        return
    for file in files:
        with contextlib.suppress(OSError):
            file.unlink()
