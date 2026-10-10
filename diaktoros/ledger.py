"""One way to open the SQLite ledger: a connection that is always closed.

``with sqlite3.connect(...) as con:`` only commits or rolls back; it never closes. The handle
then lives until garbage collection, which a long-lived broker, proxy or supervisor turns into a
growing pile of open files (and Python 3.13 into a ResourceWarning per call). ``connect`` keeps
the ``with con:`` transaction semantics exactly and closes the connection on the way out:

    with ledger.connect(db, timeout=LOCK_WAIT_S, isolation_level=None) as con:
        con.execute('BEGIN IMMEDIATE')
        ...

Any ``sqlite3.connect`` keyword passes through unchanged (``timeout``, ``isolation_level``,
``uri``, ...). ``row_factory`` and ``pragmas`` are applied before the body runs; if one of them
fails, the connection is still closed. A close() that fails while an exception is already on
its way out is attached to that exception as a note instead of replacing it.

A caller that must open the connection itself (a worker vetting the host's ledger before any
pragma touches it) passes ``opener=``: the connection it returns gets the same transaction and
the same close. What the opener opens and then refuses, it closes itself before raising.
"""

from __future__ import annotations

import contextlib
import sqlite3
import traceback
from pathlib import Path
from typing import Callable, Iterator

# How long any connection to the run ledger waits for another's lock before giving up (#601).
# Writes are short, but on a host also running heavy test suites each commit's fsync can stall,
# and writers queue behind it: 10 s was not enough. Waiting costs nothing; a failed turn costs a
# retry. ``timeout=LOCK_WAIT_S`` on connect, and ``BUSY_TIMEOUT`` where pragmas are set.
LOCK_WAIT_S = 60
BUSY_TIMEOUT = f"busy_timeout={LOCK_WAIT_S * 1000}"

# Frames that only open or wrap a connection; ``where`` looks past them to the operation.
_PLUMBING = {"connect", "_connect", "_worker_connect", "_read_only", "__enter__", "__exit__"}


def locked(exc: BaseException) -> bool:
    """Whether ``exc`` is SQLite giving up on another connection's lock (transient)."""
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def where(exc: BaseException) -> str:
    """For a lock timeout, `` (during <operation>)``: the innermost plugin function that was
    using the ledger, so a recurrence is diagnosable from the log alone (#601). '' otherwise."""
    if not locked(exc):
        return ""
    package = Path(__file__).resolve().parent
    name = ""
    for frame, _ in traceback.walk_tb(exc.__traceback__):
        code = frame.f_code
        if (Path(code.co_filename).resolve().parent == package
                and Path(code.co_filename).name != "ledger.py"
                and code.co_name not in _PLUMBING):
            name = code.co_name
    return f" (during {name})" if name else ""


@contextlib.contextmanager
def connect(path, *, row_factory=None, pragmas: tuple[str, ...] = (),
            opener: Callable[[], sqlite3.Connection] | None = None,
            **kwargs) -> Iterator[sqlite3.Connection]:
    # ``opener`` opens (and vets) the connection in place of ``sqlite3.connect(path, **kwargs)``.
    # From the moment it returns, the connection is this function's to close; until then it is
    # the opener's, which must close anything it opened before raising.
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
