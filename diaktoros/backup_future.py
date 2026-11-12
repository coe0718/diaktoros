"""The record of archives ``hermes dk backup`` saw dated past ``backup.CLOCK_SKEW_S`` (#1905).

Split out of ``backup`` to keep that module under the size ratchet; ``backup`` re-exports every
name here, so callers and tests keep using ``backup.record_future_dated`` and friends. The
backups directory, the archive scan and the atomic write are reached through ``backup`` at call
time, so a test that patches ``backup._archives`` or ``backup.util.atomic_write`` still patches
what this module calls.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import pathlib
import time

FUTURE_RECORD = ".future-dated.json"   # in backups_dir(): archives `backup` saw past it (#1905)


def _future_record_path() -> pathlib.Path:
    """The record of archives ``hermes dk backup`` saw dated past ``CLOCK_SKEW_S`` (#1905). Not a
    ``*.tar.gz``, so it is never taken for an archive, and not default-named, so never pruned."""
    return backup.backups_dir() / FUTURE_RECORD


class FutureRecordError(OSError):
    """``FUTURE_RECORD`` exists but is not the record ``record_future_dated`` writes (not UTF-8,
    not JSON, or not ``{name: mtime}``). An ``OSError``, so every caller that already stops on an
    unreadable backups directory stops on it too. The archives it named can no longer be told
    apart (#1905 review F2), so it is never read as "nothing recorded" and never overwritten."""


def _read_future_record() -> dict[str, float]:
    """``{archive name: mtime}`` from the record; empty only when there is no record. A record
    that cannot be read raises ``OSError``, as an unreadable directory does, and one that does not
    parse raises ``FutureRecordError``: reading it as empty would let every archive it named count
    as recent once the clock reaches its date (#1905 review F2)."""
    path = _future_record_path()
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:       # UnicodeDecodeError and JSONDecodeError are both ValueErrors
        raise FutureRecordError(f"{path} is damaged ({exc})") from exc
    if not isinstance(data, dict):
        raise FutureRecordError(f"{path} is damaged (not an {{archive name: mtime}} object)")
    return {str(name): _recorded_mtime(path, name, value) for name, value in data.items()}


def _recorded_mtime(path: pathlib.Path, name: str, value: object) -> float:
    """One entry of the record at ``path`` as a finite float mtime, or ``FutureRecordError``.

    The JSON decoder accepts ``NaN`` and ``Infinity``, which never equal an archive's mtime (so the
    archive would count as recent once the clock reaches it, and the next backup would drop the
    entry), and integers too large for a float, whose conversion raises ``OverflowError``. Each is
    a damaged record, never skipped and never a crash (#2446 review round 2, F2)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise FutureRecordError(f"{path} is damaged (not an {{archive name: mtime}} object)")
    try:
        mtime = float(value)
    except OverflowError:
        mtime = math.inf
    if not math.isfinite(mtime):
        raise FutureRecordError(f"{path} is damaged (the mtime of {name!r} is not a finite "
                                f"number)")
    return mtime


@contextlib.contextmanager
def _future_record_locked():
    """Hold an exclusive ``flock`` on ``backups_dir()`` itself while the caller scans, reads and
    rewrites ``FUTURE_RECORD``, so two overlapping backups never both start from the old record
    and the later writer drops the other's entries (#1905 review F1). The directory, not a lock
    file beside the record: that would be one more file in the directory every listing sees.
    Raises ``FileNotFoundError`` when the directory does not exist. On a filesystem without
    ``flock`` the lock does not take (as with ``util.hold_temp``) and overlapping runs are not
    serialized."""
    fd = os.open(backup.backups_dir(), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)        # closing the descriptor releases the flock


def record_future_dated(now: float | None = None) -> list[pathlib.Path]:
    """Record every archive in ``backups_dir()`` dated more than ``CLOCK_SKEW_S`` past ``now``
    (#1905), and return the ones not recorded before.

    ``latest_archive`` sets such a file apart only while its date is still ahead of the clock; once
    the real clock comes within the allowance it would count as fresh for ``FRESH_DAYS`` more,
    although no backup was made then. ``doctor`` is read-only and the archive cannot tell (its
    manifest's ``created`` comes from the same wrong clock), so ``hermes dk backup``, which writes
    anyway, records the file's name with its mtime, and ``latest_archive`` never counts a recorded
    archive whose mtime is unchanged. Entries whose file is gone or was rewritten (another mtime)
    are dropped. Only ``hermes dk backup`` calls this, so an archive that arrives and is caught up
    with between two backups (or with no backup run in between) is never seen, and not recorded.

    The scan, the read and the write happen under one lock (``_future_record_locked``), so a run
    that overlaps another starts from the record as the other left it (review F1). A damaged
    record raises ``FutureRecordError`` and is left as it is: rewriting it would forget what it
    named (review F2)."""
    limit = (time.time() if now is None else now) + backup.CLOCK_SKEW_S
    if not backup.backups_dir().is_dir():
        return []
    with _future_record_locked():
        current = dict((p.name, m) for p, m in backup._archives())
        old = _read_future_record()
        kept = {name: m for name, m in old.items() if current.get(name) == m}
        added = [name for name, m in sorted(current.items())
                 if m > limit and kept.get(name) != m]
        kept.update({name: current[name] for name in added})
        if kept != old:
            backup.util.atomic_write(_future_record_path(), json.dumps(kept, indent=1, sort_keys=True))
    return [backup.backups_dir() / name for name in added]


# Last, so importing this module first (before ``backup``) still finds every name above when
# ``backup`` re-exports them from its own last lines.
from . import backup  # noqa: E402
