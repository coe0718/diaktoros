"""The one CI poller per ledger (#2609): its lock, and what it polls and for how long.

With about 30 PRs held for CI, every worker that held a row lingered and polled every held row,
and each wake started another worker: 9,200 in an hour, and the read App's GitHub limit ran out.
Only the worker holding ``<ledger>.poller`` polls now (``workers.linger_for_ci``).
"""
from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

from .. import run_supervisor

SUFFIX = '.poller'


def lock(db: Path):
    """The ledger's one CI poller lock (``<ledger>.poller``): an open file holding an exclusive
    ``flock`` when this worker is the poller, False when another worker holds it, None when the
    lock cannot be taken at all (the worker then polls unlocked, as before: a missing lock must
    not stop the polling). The kernel drops the lock when the file closes or the process ends,
    so a killed poller never blocks the next one."""
    db = Path(db)
    try:
        # Read-only, made if missing: the file is never written, only locked.
        handle = os.fdopen(os.open(db.with_name(db.name + SUFFIX),
                                   os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o600), 'rb')
    except OSError:
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    except OSError:
        handle.close()
        return None
    return handle


def polled() -> tuple[str, ...]:
    """The waits a lingerer polls, by the prefix of their recorded reason."""
    return (run_supervisor.CI_HOLD, run_supervisor.VERDICT_HOLD,
            run_supervisor.CLAIM_READ_FAILED, run_supervisor.SETTLE_HOLD)


def window() -> float:
    """How far ahead a lingerer waits for a polled row, from its start or its last recovery."""
    return max(run_supervisor.CI_POLL_S, run_supervisor.RETRY_BASE) + 30


def due(sup) -> float | None:
    """The earliest due time of a waiting row of ``sup``'s ledger that a lingerer polls, or None."""
    with sup._connect() as con:
        return con.execute("SELECT MIN(retry_at) FROM runs WHERE state='waiting' AND ("
                           + " OR ".join("error LIKE ?" for _ in polled()) + ")",
                           tuple(hold + '%' for hold in polled())).fetchone()[0]


def wait(sup, sleep, end: float) -> float | None:
    """The wait of ``workers.linger_for_ci`` for the worker ``sup``: poll the polled holds,
    sleep to the earliest due row and ``recover()`` it, until the window closes or nothing falls
    due in it. The window restarts at each recovery, since this one poller polls every worker's
    rows, up to ``end``. It sleeps at most ``POLL_SLICE_S`` at a time, so a row another worker
    holds meanwhile, due sooner than the one it waits for, is not left late (#2609). Returns the
    due time of a row ``recover()`` did not release, or None."""
    span = window()
    deadline = min(time.time() + span, end)
    recovered = None
    while time.time() < deadline:
        at = sup._polled_due()
        if at is None or at > deadline:
            return None     # nothing due in this window: the watchdog sweep is the backstop
        if at <= time.time():
            if at == recovered:
                return at   # recover() did not release it: do not spin on it
            # Not a return: the earliest due row may be another worker's, and the row this
            # worker backed off may fall due later in the window (#1302): keep waiting.
            sup.recover()
            recovered = at
            deadline = min(time.time() + span, end)
            continue
        sleep(min(at - time.time(), deadline - time.time(), run_supervisor.POLL_SLICE_S) + 1)
    return None
