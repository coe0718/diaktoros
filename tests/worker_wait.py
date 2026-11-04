"""Wait for the detached supervisor workers a test started, before its temp dir goes (#108, #110).

``Supervisor.enqueue`` returns as soon as it has spawned a detached worker (``start_new_session``,
not this process's child), and a finished worker's ``recover`` can spawn another. A test that
returns on what the ledger says can therefore tear its temp dir down while a worker is still
writing there: ``rmtree`` then fails with "Directory not empty: 'state'", or the worker loses its
ledger mid-run. Every worker names a path under the test's root on its command line, and only a
live worker spawns another, so an empty scan means none is left.

A minimal copy of the pattern #113 adds to tests/test_run_supervisor.py; either can replace the
other once both have landed.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import sqlite3
import time

WORKER_EXIT_TIMEOUT = 30


def processes_naming(marker: str) -> dict[int, str]:
    """Live processes (other than this one) whose command line names ``marker``.

    Off-Linux (where ``/proc`` does not exist) this degrades to "no live workers" rather than
    raising: the deleted test_run_supervisor copy carried exactly this guard, and the dedupe
    (#145) must not lose it even though CI is ubuntu-only.
    """
    found = {}
    proc = Path('/proc')
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmd = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
        except OSError:
            continue
        if marker in cmd:  # a zombie's command line is empty: it is not writing
            found[int(entry.name)] = cmd.strip()
    return found


WORKING_STATES = ("pending", "claimed", "launching", "running")


def ledgers_idle(root: Path) -> bool:
    """True when every run ledger under ``root`` has a ledger of its own to read and no run in
    ``WORKING_STATES``: nothing is queued, claimed or running, so a worker still alive is only
    lingering to re-arm a waiting row (``Supervisor.linger_for_ci``). A ledger that cannot be
    read, or no ledger at all, is not idle: nothing then licenses ending a worker."""
    marks = ",".join("?" for _ in WORKING_STATES)
    seen = False
    for path in root.rglob("*.sqlite"):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            try:
                if not con.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone():
                    continue                    # another database, not the run ledger
                seen = True
                if con.execute(f"SELECT 1 FROM runs WHERE state IN ({marks}) LIMIT 1",
                               WORKING_STATES).fetchone():
                    return False
            finally:
                con.close()
        except sqlite3.Error:
            return False
    return seen


def end_lingering_workers(root: Path, settle: float = 5) -> None:
    """End workers that are only waiting out a backoff; call it before ``wait_for_workers``.

    A worker whose claim-time read failed (or whose review is held for CI) stays for up to
    ``max(CI_POLL_S, RETRY_BASE) + 30`` seconds to schedule the retry itself (#1302,
    ``Supervisor.linger_for_ci``), far past ``WORKER_EXIT_TIMEOUT``. A test that only checks what
    the ledger says after the first claim has nothing left for it to do. Within ``settle`` seconds
    the workers either exit or the ledger shows no run pending, claimed or running; only then is
    what is left (holding no seat and no turn) sent SIGTERM. While a run is still working the
    workers are left alone, so ``wait_for_workers`` fails on a stuck one as it always did.
    """
    marker = str(root)
    until = time.monotonic() + settle
    while processes_naming(marker) and not ledgers_idle(root) and time.monotonic() < until:
        time.sleep(0.05)
    if not ledgers_idle(root):
        return
    for pid in processes_naming(marker):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def wait_for_workers(root: Path, timeout: float = WORKER_EXIT_TIMEOUT) -> None:
    """Block until no process naming ``root`` is left; kill and fail past ``timeout``."""
    marker = str(root)
    until = time.monotonic() + timeout
    while True:
        live = processes_naming(marker)
        if not live:
            return
        if time.monotonic() >= until:
            for pid in live:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            raise AssertionError(f"detached workers still running {timeout}s after the test "
                                 f"under {marker} (killed): {live}")
        time.sleep(0.05)
