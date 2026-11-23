"""One way to open the SQLite ledger: a connection that is always closed.

``with sqlite3.connect(...) as con:`` only commits or rolls back; it never closes. The handle
then lives until garbage collection, which a long-lived broker, proxy or supervisor turns into a
growing pile of open files (and Python 3.13 into a ResourceWarning per call). ``connect`` keeps
the ``with con:`` transaction semantics exactly and closes the connection on the way out:

    with ledger.connect(db, timeout=LOCK_WAIT_S, isolation_level=None) as con:
        con.execute('BEGIN IMMEDIATE')
        ...

Without ``opener=``, any ``sqlite3.connect`` keyword passes through unchanged (``timeout``,
``isolation_level``, ``uri``, ...), and a caller that names no ``timeout`` gets ``LOCK_WAIT_S``.
With ``opener=`` (below) ``sqlite3.connect`` is never called: ``path`` and every such keyword,
the ``timeout`` default included, are discarded, and the opener sets its own wait and
isolation. ``row_factory`` and ``pragmas`` are applied before the body runs; if one of them
fails, the connection is still closed. A close() that fails while an exception is already on
its way out is attached to that exception as a note instead of replacing it.

A caller that must open the connection itself (a worker vetting the host's ledger before any
pragma touches it) passes ``opener=``: the connection it returns gets the same transaction and
the same close; ``row_factory`` and ``pragmas`` still apply to it. What the opener opens and
then refuses, it closes itself before raising.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import sqlite3
import time
import traceback
from pathlib import Path
from typing import Callable, Iterator

from .. import util

# How long any connection to the run ledger waits for another's lock before giving up (#601).
# Writes are short, but on a host also running heavy test suites each commit's fsync can stall,
# and writers queue behind it: 10 s was not enough. Waiting costs nothing; a failed turn costs a
# retry. ``timeout=LOCK_WAIT_S`` on connect, and ``BUSY_TIMEOUT`` where pragmas are set.
LOCK_WAIT_S = 60
BUSY_TIMEOUT = f"busy_timeout={LOCK_WAIT_S * 1000}"

# The claim lock beside a run ledger (``claim_lock``), by the ledger's name; a ledger by any other
# name (a test's) gets one named the same way.
CLAIMS_LOCKS = {
    "diaktoros-runs.sqlite": util.register_lock("diaktoros-runs.sqlite.claims.lock"),
    "review-loop-runs.sqlite": util.register_lock("review-loop-runs.sqlite.claims.lock"),
}

# Frames that only open or wrap a connection; ``where`` looks past them to the operation.
_PLUMBING = {"connect", "_connect", "_worker_connect", "read_only_connect", "__enter__",
             "__exit__"}


def locked(exc: BaseException) -> bool:
    """Whether ``exc`` is SQLite giving up on another connection's lock (transient).

    That is SQLITE_BUSY ("database is locked"), including its extended codes. SQLITE_LOCKED
    ("database table is locked") is a conflict inside one connection, a logic error that a
    retry cannot cure, so it is not matched. An error without ``sqlite_errorcode`` (one built
    by hand) is judged by its message."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF == sqlite3.SQLITE_BUSY
    return "database is locked" in str(exc).lower()


def where(exc: BaseException) -> str:
    """For a lock timeout, `` (during <operation>)``: the innermost plugin function that was
    using the ledger, so a recurrence is diagnosable from the log alone (#601). '' otherwise."""
    if not locked(exc):
        return ""
    package = Path(__file__).resolve().parents[1]   # the plugin, not this state/ package
    name = ""
    for frame, _ in traceback.walk_tb(exc.__traceback__):
        code = frame.f_code
        source = Path(code.co_filename).resolve()
        # Anywhere in the plugin, its subpackages included (cli/, config/, run_supervisor/: #717).
        if (source.is_relative_to(package) and source != Path(__file__).resolve()
                and code.co_name not in _PLUMBING):
            name = code.co_name
    return f" (during {name})" if name else ""


def read_only(path, *, immutable: bool = False) -> sqlite3.Connection:
    """A connection that opens the ledger at ``path`` for reading only, never creating a file:
    the path is URI-quoted, so a ``#`` or ``?`` in it never opens (and creates) another one.
    ``mode=ro`` keeps SQLite's locking, so a read sees whole transactions while others write.
    ``immutable`` drops that locking: only for a report read while no WAL is live
    (``views.read_only_connect``). It keeps ``mode=ro``: alone, ``immutable=1`` creates a
    missing file (#1471 review)."""
    from urllib.parse import quote
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro{'&immutable=1' if immutable else ''}",
                           uri=True, timeout=LOCK_WAIT_S)


@contextlib.contextmanager
def connect(path, *, row_factory=None, pragmas: tuple[str, ...] = (),
            opener: Callable[[], sqlite3.Connection] | None = None,
            **kwargs) -> Iterator[sqlite3.Connection]:
    # ``opener`` opens (and vets) the connection in place of ``sqlite3.connect(path, **kwargs)``:
    # with an opener, ``path`` and ``kwargs`` (the timeout default too) are not used at all.
    # From the moment it returns, the connection is this function's to close; until then it is
    # the opener's, which must close anything it opened before raising.
    # A caller that names no wait still gets the ledger's (#601), never SQLite's 5 s default.
    kwargs.setdefault("timeout", LOCK_WAIT_S)
    con = opener() if opener is not None else sqlite3.connect(path, **kwargs)
    try:
        if row_factory is not None:
            con.row_factory = row_factory
        for pragma in pragmas:
            con.execute(f"PRAGMA {pragma}")
        with con:  # commit on success, roll back on an exception: sqlite3's own semantics
            yield con
    except BaseException as exc:
        # The error in flight is the one the caller must see; a failing close() only adds a
        # note to it, never replaces it.
        try:
            con.close()
        except Exception as close_exc:
            exc.add_note(f"closing the ledger connection also failed: {close_exc!r}")
        raise
    con.close()


@contextlib.contextmanager
def claim_lock(db) -> Iterator[int]:
    """One at a time across the host's processes, beside the ledger at ``db``: a reviewer's
    receipt claim (``review_receipt.ReceiptLedger.claim``) and a redelivery deciding the shadow
    that reviewer's turn owes (``run_supervisor.shadow_owing.claim_lock``). That redelivery's
    transaction may fail after its shadow row was lost, and the turn is then stopped in a fresh
    one; between the two, SQLite holds no lock, so a claim there would post a review with no
    shadow (#2634 review F4, round 5). Held across both, this lock keeps the claim out until the
    stop is written. A flock, so a holder that dies frees it; past ``LOCK_WAIT_S`` it raises
    ``TimeoutError``, before anything is written or posted. It yields the lock file, which holds
    the barrier (``owes``) a stop that could not be written leaves (round 6)."""
    path = Path(db)
    name = CLAIMS_LOCKS.get(path.name) or path.name + ".claims.lock"
    lock = os.open(path.with_name(name), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        until = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise TimeoutError(f"{path}: its claim lock was held past {LOCK_WAIT_S}s") \
                        from None
                time.sleep(0.02)
        yield lock
    finally:
        os.close(lock)                    # frees the flock too


class BarrierCut(OSError):
    """``owe`` cut its own add short: the cut add's line starts at byte ``at`` (#2643)."""

    def __init__(self, at: int):
        super().__init__(errno.EIO, "the claim barrier could not be written")
        self.at = at


def _owing(lock: int) -> tuple[set[str], list[str]]:
    """What the barrier holds: its log (``owe``) replayed into ids, each ``+`` line adding one and
    each ``-`` line removing one, and cut adds. An add cut short (a last line with no newline, or
    one ``owe`` ended with ``~``) may have stopped inside its id, so it holds every id it begins
    (#2643; ``+`` alone, all) until a ``*@<offset>`` line lifts the one starting at that byte,
    which only the writer that cut it writes (``owe``'s ``mine``; #2645). A clear or lift cut
    short is not read, so the barrier fails closed (#2634 review F8)."""
    data, at = b"", 0
    while chunk := os.pread(lock, 65536, at):
        data, at = data + chunk, at + len(chunk)
    ids: set[str] = set()
    cuts: dict[int, str] = {}
    start = 0
    for raw in data.split(b"\n"):
        text, at, start = raw.decode(errors="replace"), start, start + len(raw) + 1
        mark, rest = text[:1], text[1:]
        if start > len(data):                   # the last line, with no newline
            rest += "~" if mark == "+" else ""
            mark = mark if mark == "+" else ""
        if mark == "+" and rest.endswith("~"):
            cuts[at] = rest.rstrip("~")
        elif mark == "+":
            ids.add(rest)
        elif mark == "-" and not rest.endswith("~"):
            ids.discard(rest)
        elif mark == "*" and rest[:1] == "@" and rest[1:].isdigit():
            cuts.pop(int(rest[1:]), None)
    return ids, list(cuts.values())


def owe(lock: int, run_id: str, owed: bool = True, mine=()) -> None:
    """Under ``claim_lock``, record in its file (``owed``) or clear the reviewer run ``run_id``
    whose owed shadow and stop could not be written (#2634 review F4, round 6): the barrier
    ``owes`` refuses its receipt by, held outside SQLite so it stands when the ledger refuses the
    stop. Each change is a line appended to a log, written whole (a short write is finished, not
    taken as done) and synced, so an update cut short never loses an id already marked; a cut
    line is ended with ``~`` first, and stays a cut add (``_owing``). An add this call cuts (a
    write in it fails or makes no progress) raises ``BarrierCut`` with its offset. ``mine`` are
    such offsets of the caller's own earlier adds of ``run_id`` under this same lock: each is
    lifted (after the add, before the clear; #2643, #2645). The log is emptied only once it holds
    no id and no cut add."""
    if not run_id or "\n" in run_id or "~" in run_id:
        raise ValueError("invalid run id for the claim barrier")
    end = os.lseek(lock, 0, os.SEEK_END)
    term = b"" if not end or os.pread(lock, 1, end - 1) == b"\n" else b"~\n"
    record = (("+" if owed else "-") + run_id + "\n").encode()
    lifts = "".join(f"*@{at}\n" for at in mine).encode()
    data = term + (record + lifts if owed else lifts + record)
    cut = (end + len(term), end + len(term) + len(record)) if owed else (0, 0)
    done = 0
    while done < len(data):
        try:
            wrote = os.pwrite(lock, data[done:], end + done)
        except OSError as exc:          # inside the add, a cut, as no progress is (#2644 F3)
            if cut[0] < end + done < cut[1]:
                raise BarrierCut(cut[0]) from exc
            raise
        if wrote <= 0:
            raise BarrierCut(cut[0]) if cut[0] < end + done < cut[1] else OSError(
                errno.EIO, "the claim barrier could not be written")
        done += wrote
    os.fsync(lock)
    if not owed and _owing(lock) == (set(), []):
        os.ftruncate(lock, 0)
        os.fsync(lock)


def owes(lock: int, con, run_id: str) -> bool:
    """Whether the barrier (``owe``) still holds ``run_id``'s receipt: it does until the shadow
    row for its turn is in the ledger, and is cleared once it is. A cut add (``_owing``) holds
    every run whose id it begins, until that run's shadow row is in (#2643)."""
    ids, cuts = _owing(lock)
    if run_id not in ids and not any(run_id.startswith(cut) for cut in cuts):
        return False
    if con.execute("SELECT 1 FROM runs AS r JOIN runs AS s ON s.repo=r.repo AND s.pr=r.pr AND "
                   "s.head=r.head AND s.turn_key=r.turn_key AND s.seat='shadow' WHERE r.id=?",
                   (run_id,)).fetchone() is None:
        return True
    if run_id in ids:
        owe(lock, run_id, False)
    return False
