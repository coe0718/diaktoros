"""A conflict check soon after a merge, not at the next sweep.

A merge into the base is what makes other loop PRs conflict, and the 15-minute watchdog was the
only thing that looked: GitHub computes mergeability lazily, so a conflict was often first seen
two sweeps later. The reviewer gate, on a merged ``closed`` event, now asks for one scan a short
while later (``request``) and starts a detached ``watchdog.py --conflicts``, which waits for it
(``run``) and does only the sweep's conflict step.

A burst of merges is one scan: each merge only pushes the due time later, one scanner holds the
lock, and a merge during a scan is picked up by another pass after its own delay. A request the
scanner misses is still found by the next sweep.
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import time

from . import state as state_mod

# Long enough for a run of merges to settle and for GitHub to start computing mergeability.
DELAY_S = 45.0
# A scanner never outlives this, whatever keeps pushing the due time. The watchdog passes a
# shorter one: its GitHub reads share one budget for the whole process (half of it, so the
# last scan still has time to read).
MAX_LIFETIME_S = 1800.0


def path(st) -> pathlib.Path:
    return pathlib.Path(st.dir) / "conflict-scan.json"


def due(st) -> float | None:
    """When the requested scan is due, or None (no request, or an unreadable one)."""
    try:
        value = json.loads(path(st).read_text()).get("due")
    except (OSError, ValueError, AttributeError):
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else None


def request(st, now: float | None = None) -> float:
    """Ask for a scan ``DELAY_S`` from now; a later merge moves it later. Returns the due time."""
    at = (time.time() if now is None else now) + DELAY_S
    path(st).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_mod._atomic_write(path(st), {"due": at})
    return at


def run(st, scan, *, clock=time.time, sleep=time.sleep, lifetime: float = MAX_LIFETIME_S) -> int:
    """Wait for the requested scan and run ``scan()``, again while new requests arrive. Returns
    how many scans ran; 0 when another scanner holds the lock (it will see the request)."""
    lock_path = path(st).with_suffix(".lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    started, handled, ran = clock(), 0.0, 0
    while True:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return ran
            while clock() - started < lifetime:
                at = due(st)
                if at is None or at <= handled:
                    break
                if clock() < at:
                    sleep(min(at - clock(), 30.0))
                    continue
                handled = at
                scan()
                ran += 1
        finally:
            os.close(fd)                      # closing releases the lock
        # A request written just as the lock was released: take it rather than leave it to
        # a scanner that may already have given up on the lock.
        at = due(st)
        if at is None or at <= handled or clock() - started >= lifetime:
            return ran
