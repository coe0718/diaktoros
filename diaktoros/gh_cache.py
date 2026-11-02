"""Conditional GETs for the loop's GitHub reads (#1646).

GitHub answers a GET sent with ``If-None-Match`` with ``304 Not Modified`` when the resource is
unchanged, and a 304 does not count against the rate limit. The reader shares its account's
5,000 calls an hour, and the watchdog re-reads every open PR every sweep, most of it unchanged.
So a GET whose answer carries an ``ETag`` is kept here, per login and path, and the next GET of
it asks GitHub whether it changed. A 304 is GitHub's word that the stored body is current:
nothing stale is ever served.

Disposable: a missing, unreadable or corrupt entry is a plain GET. Entries are 0600 files of
at most ``MAX_BYTES``, dropped once unused for ``KEEP_S``. Bodies are what GitHub returned, never
a token: the login only names the entry.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import random
import time

MAX_BYTES = 1_000_000          # a bigger answer is not kept
KEEP_S = 2 * 86400             # an entry unused this long is dropped
PRUNE_ONE_IN = 50              # a store prunes the directory one time in this many


def directory() -> pathlib.Path:
    from . import config
    return config.home() / "state" / "diaktoros-gh-cache"


def _file(login: str, path: str) -> pathlib.Path:
    return directory() / hashlib.sha256(f"{login}\0{path}".encode()).hexdigest()


def lookup(login: str, path: str) -> dict | None:
    """The stored ``{"etag", "data", "headers"}`` for this login's GET of ``path``, or None."""
    try:
        entry = json.loads(_file(login, path).read_text())
    except (OSError, ValueError):
        return None
    if (not isinstance(entry, dict) or not isinstance(entry.get("etag"), str)
            or not isinstance(entry.get("headers"), dict) or "data" not in entry):
        return None
    return entry


def hit(login: str, path: str) -> None:
    """Mark an entry used (a 304 served it), so ``prune`` keeps it."""
    try:
        os.utime(_file(login, path))
    except OSError:
        pass


def store(login: str, path: str, raw: str, data, headers: dict) -> None:
    """Keep a 200's body when GitHub gave it an ``ETag``; best effort, never raises."""
    etag = headers.get("etag")
    if not isinstance(etag, str) or not etag or len(raw) > MAX_BYTES:
        return
    from .util import atomic_write
    try:
        directory().mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(_file(login, path),
                     json.dumps({"etag": etag, "data": data, "headers": headers}))   # 0600
    except (OSError, TypeError, ValueError):
        return
    if random.randrange(PRUNE_ONE_IN) == 0:
        prune()


def prune(now: float | None = None) -> int:
    """Drop entries (and stray temp files) unused for ``KEEP_S``; how many went."""
    cutoff = (time.time() if now is None else now) - KEEP_S
    gone = 0
    try:
        entries = list(directory().iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                gone += 1
        except OSError:
            pass
    return gone
