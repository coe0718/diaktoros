"""One way to open the SQLite ledger: a connection that is always closed.

``with sqlite3.connect(...) as con:`` only commits or rolls back; it never closes. The handle
then lives until garbage collection, which a long-lived broker, proxy or supervisor turns into a
growing pile of open files (and Python 3.13 into a ResourceWarning per call). ``connect`` keeps
the ``with con:`` transaction semantics exactly and closes the connection on the way out:

    with ledger.connect(db, timeout=10, isolation_level=None) as con:
        con.execute('BEGIN IMMEDIATE')
        ...

Any ``sqlite3.connect`` keyword passes through unchanged (``timeout``, ``isolation_level``,
``uri``, ...). ``row_factory`` and ``pragmas`` are applied before the body runs; if one of them
fails, the connection is still closed.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Iterator


@contextlib.contextmanager
def connect(path, *, row_factory=None, pragmas: tuple[str, ...] = (),
            **kwargs) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(path, **kwargs)
    try:
        if row_factory is not None:
            con.row_factory = row_factory
        for pragma in pragmas:
            con.execute(f"PRAGMA {pragma}")
        with con:  # commit on success, roll back on an exception: sqlite3's own semantics
            yield con
    finally:
        con.close()
