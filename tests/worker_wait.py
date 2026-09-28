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
import time

WORKER_EXIT_TIMEOUT = 30


def processes_naming(marker: str) -> dict[int, str]:
    """Live processes (other than this one) whose command line names ``marker``."""
    found = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmd = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
        except OSError:
            continue
        if marker in cmd:  # a zombie's command line is empty: it is not writing
            found[int(entry.name)] = cmd.strip()
    return found


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
