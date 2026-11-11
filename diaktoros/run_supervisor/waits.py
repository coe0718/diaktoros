"""Why and when a run waited (#2080): one line in the worker log each time a run enters
``waiting`` or its reason changes, so a wait can be explained after the row has moved on.

The ledger keeps only a row's current ``error``, overwritten by its next state, so the log is the
record. A line reads::

    2026-11-09T17:40:00Z wait repo=o/r pr=12 head=abc1234 seat=reviewer run=55 retries=1 retry_at=2026-11-09T17:41:30Z reason=ci: 2 checks pending

A CI hold re-polled with an unchanged reason (and retry count) adds no line: the log's last line
for the run is compared first. Best effort: a failure here never changes how a run is handled.
``hermes dk history`` reads these lines back (``read``).
"""
from __future__ import annotations

import io
import os
import re
import stat
import sys
import time
from pathlib import Path

from .. import envnames

REASON_MAX = 300
# Set by ``_spawn`` in a worker's environment when its stderr is the worker log: the log's
# ``<device>:<inode>`` as the host opened it, so ``cap_stderr`` caps that file even if a later
# spawn rotated the path away before the worker got there (#2302).
LOG_ENV = envnames.name("WORKER_LOG")
# Read from each of the log and its one rotation (the host rotates past 256 KB): enough to hold
# a run's last wait line however chatty the workers' stderr since.
READ_BYTES = 1 << 20
_LINE = re.compile(r"^(?P<at>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) wait repo=(?P<repo>\S+) "
                   r"pr=(?P<pr>\d+) head=(?P<head>\S*) seat=(?P<seat>\S+) run=(?P<run>\S+) "
                   r"retries=(?P<retries>\d+) retry_at=(?P<retry_at>\S+) reason=(?P<reason>.*)$")


def log_path(db: Path) -> Path:
    return db.with_name(db.name + ".workers.log")


def room(size: int) -> int:
    """Bytes a writer that never rotates may still add to a worker log of ``size`` bytes: what is
    left under ``WORKER_LOG_MAX`` (#2302). The one statement of the cap for every writer of the
    log: the host rotates it once to ``.1`` when it has none left (``_worker_log``), ``note``
    appends a wait line only while there is some, and a worker's stderr is cut to it
    (``cap_stderr``)."""
    from .. import run_supervisor
    return max(0, run_supervisor.WORKER_LOG_MAX - size)


def within_cap(path: Path) -> bool:
    """May a writer that never rotates append to the worker log at ``path``? Only to an existing
    file with ``room`` left (#2302)."""
    return path.is_file() and room(path.stat().st_size) > 0


class _CappedLog(io.RawIOBase):
    """The worker log's descriptor, written only up to its ``room``: a write past it is cut at
    the cap, and once the file is full the rest is dropped until the host rotates it (#2302)."""

    def __init__(self, fd: int):
        super().__init__()
        self.fd = fd

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self.fd

    def write(self, data) -> int:
        data = bytes(data)
        left = room(os.fstat(self.fd).st_size)
        if left:
            os.write(self.fd, data[:left])
        return len(data)


def cap_stderr(db: Path) -> bool:
    """In a worker, bound what its Python code writes to its stderr (prints, tracebacks,
    warnings) when that stderr is the worker log of the ledger ``db`` (#2302). Whether it did.

    ``_spawn`` hands a worker the log's descriptor and the host only checks its size at its next
    spawn, so without this a chatty worker grows it without bound, and after a rotation it would
    keep growing the ``.1`` it still holds (its size, read from the descriptor, is past the cap,
    so nothing more is written to it). Which file the worker holds is told by ``LOG_ENV``, the
    identity ``_spawn`` read from the descriptor it handed over, not by the log's path: another
    spawn may rotate the log between that hand-over and this call, and the ``.1`` the worker then
    holds is capped all the same (#2302 review). Without ``LOG_ENV`` (a stderr not set up by
    ``_spawn``) the log's current inode is compared. Any other stderr is left alone.
    Not covered: bytes written to descriptor 2 natively (a fatal interpreter error as the
    process dies) or by a child that inherits it (the worker's children capture or discard
    their stderr); and two workers appending at once can each pass the check before the other
    writes, so the log can pass the cap by at most one write per concurrent worker."""
    try:
        fd = sys.stderr.fileno()
        held = os.fstat(fd)
        if not stat.S_ISREG(held.st_mode) or not _is_the_log(held, db):
            return False
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return False
    sys.stderr.flush()
    sys.stderr = io.TextIOWrapper(io.BufferedWriter(_CappedLog(fd)), encoding="utf-8",
                                  errors="backslashreplace", line_buffering=True,
                                  write_through=True)
    return True


def identity(fd: int) -> str:
    """The ``LOG_ENV`` value naming the file open at ``fd``: its device and inode."""
    held = os.fstat(fd)
    return f"{held.st_dev}:{held.st_ino}"


def _is_the_log(held: os.stat_result, db: Path) -> bool:
    """Is ``held`` (a stderr's stat) the worker log ``_spawn`` handed over, as ``LOG_ENV`` names
    it, or failing that the log at its path now?"""
    marked = os.environ.get(LOG_ENV)
    if marked:
        return marked == f"{held.st_dev}:{held.st_ino}"
    log = os.stat(log_path(Path(db)))
    return (held.st_dev, held.st_ino) == (log.st_dev, log.st_ino)


def _utc(at) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(at))) if at else "-"


def _reason(text) -> str:
    return " ".join(str(text or "").split())[:REASON_MAX]


def _parse(line: str) -> dict | None:
    found = _LINE.match(line.rstrip("\n"))
    return found.groupdict() if found else None


def _tail(path: Path, size: int = READ_BYTES) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, fh.seek(0, os.SEEK_END) - size))
            return fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _lines(db: Path) -> list[str]:
    """The worker log's lines, oldest first: its one rotation, then the live log."""
    path = log_path(db)
    return _tail(path.with_name(path.name + ".1")) + _tail(path)


def note(sup, run_id) -> None:
    """Log the run's wait when it is ``waiting`` and its reason or retry count differs from its
    last logged wait. Appends to the existing worker log (a worker never creates it: the host
    does); with no log, writes to stderr, which is the log in a spawned worker."""
    try:
        with sup._connect() as con:
            row = con.execute("SELECT id, repo, pr, head, seat, state, retries, retry_at, error "
                              "FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row["state"] != "waiting":
            return
        reason, retries, path = _reason(row["error"]), int(row["retries"] or 0), log_path(sup.db)
        last = next((w for w in map(_parse, reversed(_lines(sup.db)))
                     if w and w["run"] == str(row["id"])), None)
        if last and last["reason"] == reason and int(last["retries"]) == retries:
            return
        line = (f"{_utc(time.time())} wait repo={row['repo']} pr={row['pr']} "
                f"head={str(row['head'] or '')[:7]} seat={row['seat']} run={row['id']} "
                f"retries={retries} retry_at={_utc(row['retry_at'])} reason={reason}\n")
        if path.is_file():
            if not within_cap(path):   # past the cap: only the host's rotation makes room (#2302)
                return
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
        else:
            sys.stderr.write(line)
    except Exception:  # noqa: BLE001  the record is best effort, never the run's outcome
        return


def read(db: Path, repo: str, pr: int) -> list[dict]:
    """The PR's logged waits, oldest first, from the worker log and its one rotation. Lines
    written before #2080 (or by anything else) are not waits and are skipped."""
    out = []
    for line in _lines(db):
        wait = _parse(line)
        if wait and wait["repo"] == repo and int(wait["pr"]) == pr:
            out.append(wait)
    return out
