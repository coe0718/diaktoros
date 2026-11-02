"""Conditional GETs for the loop's GitHub reads (#1646).

GitHub answers a GET sent with ``If-None-Match`` with ``304 Not Modified`` when the resource is
unchanged, and a 304 does not count against the rate limit. The reader shares its account's
5,000 calls an hour, and the watchdog re-reads every open PR every sweep, most of it unchanged.
So a GET whose answer carries an ``ETag`` is kept here, per login and path, and the next GET of
it asks GitHub whether it changed. A 304 is GitHub's word that the stored body is current:
nothing stale is ever served.

A 304 validates the body, not the credential that asked: its answer carries only its own
headers, never a stored one (#1653 review). The reads that exist to inspect the credential itself
(``gh.CREDENTIAL_PROBES``, whose scopes, expiry and limits are read from headers) are never
conditional.

Only a writer process keeps the cache: the watchdog and the gates (through ``gate.context``)
call ``enable_writes``, and a worker always is one; together they make nearly every read. Any
other, such as ``doctor``, ``explain``, ``status`` and the Desktop routes, which promise to write
nothing, uses what is stored but never stores, marks or prunes an entry (#1653 review). A worker
writes only into a directory that already exists: it never creates host state (``hostdirs``).

Disposable: a missing, unreadable or corrupt entry (a malformed ETag among them) is a plain GET.
Entries are 0600 files of at most ``MAX_BYTES`` bytes as stored, and an entry unused for
``KEEP_S`` may be pruned. Pruning is opportunistic: a writer's store runs it about one time in
``PRUNE_ONE_IN``, so an old entry stays until a later store happens to prune, and with no stores
nothing is pruned. Bodies are what GitHub returned, never a token: the login only names the entry.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import random
import re
import time

MAX_BYTES = 1_000_000          # an entry bigger than this, in bytes as stored, is not kept
KEEP_S = 2 * 86400             # an entry unused this long may be pruned (on a later store)
PRUNE_ONE_IN = 50              # a store prunes the directory one time in this many
# An entity tag as HTTP defines it (RFC 9110 8.8.3): anything else is never sent as a header.
ETAG = re.compile(r'(W/)?"[\x21\x23-\x7e]*"')
_WRITES = False


def enable_writes() -> None:
    """This process may store, mark and prune entries: called by the writer processes only."""
    global _WRITES
    _WRITES = True


def _writable() -> bool:
    """Whether this process may write the cache now: a writer that called ``enable_writes``, or
    a worker (always a writer), which writes only into a directory the host already made."""
    from . import hostdirs
    if hostdirs.in_worker():
        return directory().is_dir()
    return _WRITES


def directory() -> pathlib.Path:
    from . import config
    return config.home() / "state" / "diaktoros-gh-cache"


def cacheable(path: str) -> bool:
    """Whether a GET of ``path`` may be conditional: every read but a credential probe."""
    from . import gh
    return path.split("?", 1)[0] not in gh.CREDENTIAL_PROBES


def _file(login: str, path: str) -> pathlib.Path:
    return directory() / hashlib.sha256(f"{login}\0{path}".encode()).hexdigest()


def lookup(login: str, path: str) -> dict | None:
    """The stored ``{"etag", "data"}`` for this login's GET of ``path``, or None."""
    if not cacheable(path):
        return None
    try:
        entry = json.loads(_file(login, path).read_text())
    except (OSError, ValueError):
        return None
    if (not isinstance(entry, dict) or not isinstance(entry.get("etag"), str)
            or not ETAG.fullmatch(entry["etag"]) or "data" not in entry):
        return None
    return entry


def hit(login: str, path: str) -> None:
    """Mark an entry used (a 304 served it), so ``prune`` keeps it."""
    if not _writable():
        return
    try:
        os.utime(_file(login, path))
    except OSError:
        pass


def store(login: str, path: str, data, headers: dict) -> None:
    """Keep a 200's body when GitHub gave it an ``ETag``; best effort, never raises. Only the
    body and its ETag: no header is ever served from here."""
    etag = headers.get("etag")
    if not isinstance(etag, str) or not ETAG.fullmatch(etag) or not cacheable(path) \
            or not _writable():
        return
    from .util import atomic_write
    try:
        entry = json.dumps({"etag": etag, "data": data}, ensure_ascii=False).encode()
        if len(entry) > MAX_BYTES:
            return
        directory().mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(_file(login, path), entry)                                     # 0600
    except (OSError, TypeError, ValueError):
        return
    if random.randrange(PRUNE_ONE_IN) == 0:
        prune()


def prune(now: float | None = None) -> int:
    """Drop entries (and stray temp files) unused for ``KEEP_S``; how many went."""
    if not _writable():
        return 0
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
