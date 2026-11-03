"""The host's own count of its GitHub calls: which reads spend the rate limit.

Every REST call ``gh`` makes is counted here by login, method, path shape (numbers, SHAs and
branch names replaced, query values dropped) and outcome: ``spent`` (an answer GitHub counted),
``free`` (a 304 from a conditional GET, and ``/rate_limit`` itself, which GitHub does not count),
``limited`` (a 403 or 429 because the limit was already spent), ``unsent`` (no credential, so
nothing left the host) or ``error`` (no HTTP answer: it may or may not have counted). The newest rate-limit headers seen for each login are kept beside the counts,
so GitHub's own figure can be set against the host's: a gap is calls made somewhere else.

``hermes dk calls`` reads it. Counts are kept in hour buckets, in one small file per process
under ``$HERMES_HOME/state/diaktoros-gh-calls/``, rewritten whole every ``FLUSH_EVERY`` calls and
at exit, so no two processes write one file and no lock is taken. Only a writer process keeps a
count, as for the cache (``gh_cache.writes_enabled``): the watchdog, the gates and the workers,
which make nearly every call. A command that promises to write nothing (``doctor``, ``explain``,
``status``, the Desktop) is not counted, and ``calls`` says so. A worker writes only into a
directory that already exists: the host makes it as it starts one (``prepare``). Files older
than ``KEEP_S`` are pruned by a later flush. Nothing here holds a token, a body or an error
text: logins and path shapes only.
"""
from __future__ import annotations

import atexit
import json
import os
import pathlib
import random
import re
import time
from collections import Counter

KEEP_S = 2 * 86400             # a count file older than this is pruned (by a later flush)
FLUSH_EVERY = 25               # a process rewrites its file after this many calls, and at exit
PRUNE_ONE_IN = 20              # a flush prunes the directory one time in this many
FREE_PATHS = ("/rate_limit",)  # GitHub does not count these against the limit
LIMIT_HEADERS = ("limit", "remaining", "used", "reset", "resource")
OUTCOMES = ("spent", "free", "limited", "unsent", "error")

_counts: Counter = Counter()   # (hour, login, method, shape, outcome) -> calls
_limits: dict = {}             # login -> the newest rate-limit headers seen, with when
_pending = 0
_name = ""
_registered = False


def directory(home: pathlib.Path | None = None) -> pathlib.Path:
    from . import config
    return (config.home() if home is None else pathlib.Path(home)) / "state" / "diaktoros-gh-calls"


def prepare(home: pathlib.Path | None = None) -> None:
    """Make the count directory, host-side, before a worker is started (``_spawn``): a worker
    counts only into a directory that exists and never makes one (review of #1758, F1). Does
    nothing in a worker; best effort."""
    from . import hostdirs
    if hostdirs.in_worker():
        return
    try:
        directory(home).mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        pass


def shape(path: str) -> str:
    """A path with what varies call to call taken out: ``/repos/R/pulls/N/reviews?page``."""
    base, _, query = path.partition("?")
    base = re.sub(r"^/repos/[^/]+/[^/]+", "/repos/R", base)
    base = re.sub(r"/git/(refs|ref)/heads/.+$", r"/git/\1/heads/REF", base)
    base = re.sub(r"/compare/.+$", "/compare/X", base)
    base = re.sub(r"/users/[^/]+", "/users/X", base)
    base = re.sub(r"/[0-9a-f]{40}(?=/|$)", "/SHA", base)
    base = re.sub(r"/\d+(?=/|$)", "/N", base)
    keys = sorted({part.split("=", 1)[0] for part in query.split("&") if part})
    return base + ("?" + "&".join(keys) if keys else "")


def outcome(path: str, status: int | None, not_modified: bool = False,
            headers: dict | None = None) -> str:
    """How a call counts: ``free``, ``limited``, ``spent`` or ``error`` (no HTTP answer)."""
    if not_modified or path.split("?", 1)[0] in FREE_PATHS:
        return "free"
    if status in (403, 429) and str((headers or {}).get("x-ratelimit-remaining")) == "0":
        return "limited"
    return "error" if status is None else "spent"


def record(login: str, method: str, path: str, result: str, headers: dict | None = None) -> None:
    """Count one call; never raises, and does nothing in a process that keeps no count."""
    global _pending
    try:
        if not _writable():
            return
        hour = int(time.time() // 3600 * 3600)
        _counts[(hour, login, method, shape(path), result)] += 1
        seen = {k: (headers or {}).get(f"x-ratelimit-{k}") for k in LIMIT_HEADERS}
        if seen["used"] is not None:
            _limits[login] = {**seen, "at": time.time()}
        _register()
        _pending += 1
        if _pending >= FLUSH_EVERY:
            flush()
    except Exception:  # noqa: BLE001 - a count must never fail a call
        return


def _writable() -> bool:
    from . import gh_cache, hostdirs
    if hostdirs.in_worker():
        return directory().is_dir()
    return gh_cache.writes_enabled()


def _register() -> None:
    global _registered
    if not _registered:
        atexit.register(flush)
        _registered = True


def flush() -> None:
    """Rewrite this process's file with everything it has counted; best effort."""
    global _pending, _name
    if not _counts or not _writable():
        return
    from .util import atomic_write
    try:
        _name = _name or f"{int(time.time())}-{os.getpid()}-{random.getrandbits(32):08x}.json"
        rows = [[*key, n] for key, n in _counts.items()]
        directory().mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(directory() / _name, json.dumps({"counts": rows, "limits": _limits}))
        _pending = 0
    except (OSError, TypeError, ValueError):
        return
    if random.randrange(PRUNE_ONE_IN) == 0:
        prune()


def prune(now: float | None = None) -> int:
    """Drop count files older than ``KEEP_S``; how many went."""
    cutoff = (time.time() if now is None else now) - KEEP_S
    gone = 0
    try:
        files = list(directory().iterdir())
    except OSError:
        return 0
    for file in files:
        try:
            if file.stat().st_mtime < cutoff:
                file.unlink()
                gone += 1
        except OSError:
            pass
    return gone


def _empty() -> dict:
    return {**{result: 0 for result in OUTCOMES}, "limit": None}


def summary(since: float) -> dict:
    """Every process's counts from the hour holding ``since`` on, by login: ``{login: {"spent",
    "free", "limited", "unsent", "error", "paths": [[method, shape, outcome, calls], ...] (most first),
    "limit": the newest rate-limit headers seen, or None}}``. Read-only; unreadable files are
    skipped."""
    start = int(since // 3600 * 3600)
    logins: dict = {}
    paths: dict = {}
    for file in sorted(directory().glob("*.json")) if directory().is_dir() else []:
        try:
            data = json.loads(file.read_text())
            rows, limits = data["counts"], data.get("limits") or {}
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for row in rows if isinstance(rows, list) else []:
            try:
                hour, login, method, path, result, calls = row
                if int(hour) < start:
                    continue
                entry = logins.setdefault(str(login), _empty())
                entry[str(result)] = entry.get(str(result), 0) + int(calls)
                key = (str(login), str(method), str(path), str(result))
                paths[key] = paths.get(key, 0) + int(calls)
            except (TypeError, ValueError):
                continue
        for login, seen in limits.items() if isinstance(limits, dict) else []:
            entry = logins.setdefault(str(login), _empty())
            if isinstance(seen, dict) and (entry["limit"] is None
                                           or float(seen.get("at") or 0)
                                           > float(entry["limit"].get("at") or 0)):
                entry["limit"] = seen
    for login, entry in logins.items():
        entry["paths"] = sorted(([m, p, r, n] for (who, m, p, r), n in paths.items()
                                 if who == login), key=lambda x: (-x[3], x[1]))
    return logins
