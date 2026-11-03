"""Installation tokens shared by the host's processes (#1780).

GitHub's ETags follow the credential that asked (``Vary: Authorization``): an unchanged resource
read with a new installation token answers 200 with a new tag, and that 200 is counted. Each gate,
worker and watchdog sweep is its own process, so a token kept only in process memory
(``github_app._tokens``) was minted afresh by each of them, and the conditional reads of #1653
almost never matched for an App identity. Measured: 18 of 29 re-stored entries came back with a
byte-identical body and a new tag.

So a minted token is kept here, one 0600 file per App identity, repository and App id, holding the
token and its expiry only, and every host process uses it until ``github_app.REUSE_MARGIN_S``
before it expires. One process mints at a time for each token (``held``), so two never replace
each other's; the wait for that lock is bounded (``LOCK_WAIT_S``, and a gate's own budget), and
past it a process mints for itself rather than stall behind a holder that hung.

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
import time

LOCK_WAIT_S = 15.0             # past this, a process mints for itself (a mint is two calls)
LOCK_POLL_S = 0.05


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
def held(key: tuple):
    """One minter at a time for ``key`` across the host's processes; a process that may not
    write takes no lock (and so writes nothing). The wait is bounded by ``LOCK_WAIT_S`` and by
    what is left of a gate's budget (review of #1781, F1): past it the caller goes on unlocked
    and mints for itself, and a gate whose budget ran out while waiting stops as any overrun
    does. Best effort: an unusable directory is an unshared mint."""
    if not _writable():
        yield
        return
    try:
        directory().mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (directory() / f".{_file(key).name}.lock").open("a+b")
    except OSError:
        yield
        return
    with lock:
        got = _wait(lock)
        try:
            yield
        finally:
            if got:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _wait(lock) -> bool:
    """Take ``lock`` within the bound; whether it was taken. Raises ``GateBudgetExceeded`` once
    a gate's budget is spent."""
    from . import gh
    left = gh.remaining()
    until = time.monotonic() + (LOCK_WAIT_S if left is None else min(LOCK_WAIT_S, left))
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            pass
        left = gh.remaining()
        if left is not None and left <= 0:
            raise gh.GateBudgetExceeded("time budget spent waiting for an App token being minted")
        if time.monotonic() >= until:
            return False
        time.sleep(LOCK_POLL_S)


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
