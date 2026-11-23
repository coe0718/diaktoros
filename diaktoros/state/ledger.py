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


def _owing(lock: int) -> set[str]:
    os.lseek(lock, 0, os.SEEK_SET)
    data = b""
    while chunk := os.read(lock, 65536):
        data += chunk
    return {line.decode(errors="replace") for line in data.split(b"\n")[:-1] if line}


def owe(lock: int, run_id: str, owed: bool = True) -> None:
    """Under ``claim_lock``, record in its file (``owed``) or clear the reviewer run ``run_id``
    whose owed shadow and stop could not be written (#2634 review F4, round 6): the barrier
    ``owes`` refuses its receipt by, held outside SQLite so it stands when the ledger refuses the
    stop. Written before it is cut to length, so a crash between leaves ids, never loses one."""
    ids = _owing(lock) | {run_id} if owed else _owing(lock) - {run_id}
    body = "".join(f"{i}\n" for i in sorted(ids)).encode()
    os.pwrite(lock, body, 0)
    os.ftruncate(lock, len(body))
    os.fsync(lock)


def owes(lock: int, con, run_id: str) -> bool:
    """Whether the barrier (``owe``) still holds ``run_id``'s receipt: it does until the shadow
    row for its turn is in the ledger, and is cleared once it is."""
    if run_id not in _owing(lock):
        return False
    if con.execute("SELECT 1 FROM runs AS r JOIN runs AS s ON s.repo=r.repo AND s.pr=r.pr AND "
                   "s.head=r.head AND s.turn_key=r.turn_key AND s.seat='shadow' WHERE r.id=?",
                   (run_id,)).fetchone() is None:
        return True
    owe(lock, run_id, False)
    return False
