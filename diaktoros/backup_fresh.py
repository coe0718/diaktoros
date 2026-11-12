"""How recent the newest backup is, for ``doctor``'s ``backup:fresh`` check (#1081), and the
record of archives ``hermes dk backup`` saw dated in the future (#1905). Split out of ``backup``.

Like retention, ``backup:fresh`` has no setting: it warns on a fixed age, ``FRESH_DAYS``, and
reads only ``backups_dir()``: it cannot see an archive written with ``--out`` elsewhere, nor a
backup taken some other way. It does not trust an archive dated more than ``CLOCK_SKEW_S`` in the
future, since that file would stay "fresh" long after the backup. Because
that cutoff moves with the clock, ``hermes dk backup`` records each such archive it sees in
``FUTURE_RECORD`` (``record_future_dated``, #1905) and ``doctor`` keeps a recorded one apart after
the clock reaches its date; one that arrives and is caught up with between two backup runs is not
recorded.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import pathlib
import time

from . import backup, util

FRESH_DAYS = 30         # doctor's backup:fresh warns when the newest archive is older than this
CLOCK_SKEW_S = 86400    # an archive dated further ahead than this is not trusted as recent (#1875)
FUTURE_RECORD = ".future-dated.json"   # in backups_dir(): archives `backup` saw past it (#1905)


def latest_archive(now: float | None = None) -> tuple[tuple[pathlib.Path, float] | None,
                                                      tuple[pathlib.Path, float] | None]:
    """The archives in ``backups_dir()`` as ``(newest, ahead)``, each ``(path, mtime)`` or None.

    ``newest`` is the most recently written archive whose mtime is not past ``now`` plus
    ``CLOCK_SKEW_S`` (``now`` defaults to the current time) and that ``hermes dk backup`` has not
    recorded as future-dated (``record_future_dated``). ``ahead`` is the latest-dated archive
    that is either: written while the host clock was wrong, or copied in from a host whose
    clock is. Its age cannot be told, and trusting it would keep ``doctor`` green for as long as
    the clock was ahead plus ``FRESH_DAYS``, however long ago the backup really was (#1875); a
    recorded one stays apart after the clock reaches its date (#1905). So the
    caller must not count it as a recent backup. It is returned so that an install holding nothing
    else can be told why, rather than reported as having no archive.

    An archive here is a regular, non-empty ``*.tar.gz`` (not a symlink), whatever its name: an
    operator's ``--out`` into this directory counts as much as a default name. The file is not
    opened, so this says a recent archive *exists*, not that it would restore. A directory that
    does not exist is "none"; one that cannot be read raises ``OSError`` — the caller says so
    rather than reporting a backup that may be there as missing. So does a damaged
    ``FUTURE_RECORD`` (``FutureRecordError``, an ``OSError``): it is never read as empty.
    """
    limit = (time.time() if now is None else now) + CLOCK_SKEW_S
    try:
        entries = _archives()
    except FileNotFoundError:
        return None, None
    recorded = _read_future_record()
    newest: tuple[pathlib.Path, float] | None = None
    ahead: tuple[pathlib.Path, float] | None = None
    for path, mtime in entries:
        found = (path, mtime)
        # Past the allowance now, or seen past it by an earlier ``create`` (#1905): the clock
        # catching up with the file's date does not make that date its age.
        if mtime > limit or recorded.get(path.name) == mtime:
            ahead = found if ahead is None or found[1] > ahead[1] else ahead
        elif newest is None or mtime > newest[1]:
            newest = found
    return newest, ahead


def _archives() -> list[tuple[pathlib.Path, float]]:
    """Every archive in ``backups_dir()`` as ``(path, mtime)``: a regular, non-empty, non-symlink
    ``*.tar.gz``. Raises ``FileNotFoundError`` when the directory does not exist and ``OSError``
    when it cannot be read; a file gone between the listing and its ``stat`` is skipped."""
    found = []
    for entry in os.scandir(backup.backups_dir()):
        if not entry.name.endswith(".tar.gz") or not entry.is_file(follow_symlinks=False):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue        # gone between the listing and now
        if info.st_size:
            found.append((pathlib.Path(entry.path), info.st_mtime))
    return found


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
    limit = (time.time() if now is None else now) + CLOCK_SKEW_S
    if not backup.backups_dir().is_dir():
        return []
    with _future_record_locked():
        current = dict((p.name, m) for p, m in _archives())
        old = _read_future_record()
        kept = {name: m for name, m in old.items() if current.get(name) == m}
        added = [name for name, m in sorted(current.items())
                 if m > limit and kept.get(name) != m]
        kept.update({name: current[name] for name in added})
        if kept != old:
            util.atomic_write(_future_record_path(), json.dumps(kept, indent=1, sort_keys=True))
    return [backup.backups_dir() / name for name in added]
