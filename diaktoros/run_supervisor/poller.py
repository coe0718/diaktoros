"""The one CI poller per ledger (#2609): its lock, and what it polls and for how long.

With about 30 PRs held for CI, every worker that held a row lingered and polled every held row,
and each wake started another worker: 9,200 in an hour, and the read App's GitHub limit ran out.
Only the worker holding ``<ledger>.poller`` polls now (``workers.linger_for_ci``).
"""
from __future__ import annotations

import fcntl
import os
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
