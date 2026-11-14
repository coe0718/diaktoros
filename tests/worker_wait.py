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
import tempfile
import time
from unittest import mock

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


def lingering_marks(root: Path) -> dict[int, Path]:
    """The live processes naming ``root`` that carry a linger mark (#1811), each with the ledger
    beside which it keeps it: the PIDs in the ``<ledger>.linger.<pid>`` files
    ``Supervisor.linger_for_ci`` keeps beside a ledger while it waits. A mark whose process is
    gone, or is not one naming ``root`` (a reused PID), counts for nothing. Which process lingers
    is what the process says of itself, not what a ledger row says of the runs."""
    from diaktoros.run_supervisor import workers    # after the caller's _home_guard import
    named = set(processes_naming(str(root)))
    return {pid: db for db in root.rglob("*.sqlite") for pid in workers.lingering_pids(db)
            if pid in named}


def lingering_workers(root: Path) -> set[int]:
    """The PIDs of ``lingering_marks``."""
    return set(lingering_marks(root))


def end_lingering_workers(root: Path, settle: float = 5) -> None:
    """End the workers that mark themselves as lingering; call it before ``wait_for_workers``.

    A worker whose claim-time read failed (or whose review is held for CI) stays for up to
    ``max(CI_POLL_S, RETRY_BASE) + 30`` seconds to schedule the retry itself (#1302,
    ``Supervisor.linger_for_ci``), far past ``WORKER_EXIT_TIMEOUT``. A test that only checks what
    the ledger says after the first claim has nothing left for it to do. Within ``settle`` seconds
    every process naming ``root`` either exits or carries the linger mark; then the marked ones,
    and only those, are sent SIGTERM, each through ``workers.signal_lingerer``, which sends it
    only if the worker is still asleep at that moment (#1830): one that woke after it was picked
    is at work, and is left alone. Any other process (a hung or still working worker) is left
    alone, so ``wait_for_workers`` fails on it, whatever the ledger or its neighbours say.
    """
    from diaktoros.run_supervisor import workers    # after the caller's _home_guard import
    marker = str(root)
    until = time.monotonic() + settle
    while time.monotonic() < until:
        live = set(processes_naming(marker))
        if live <= lingering_workers(root):         # none left, or each of them lingers
            break
        time.sleep(0.05)
    for pid, db in lingering_marks(root).items():
        workers.signal_lingerer(db, pid, signal.SIGTERM)


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


def removal_watch(root: Path) -> tuple[list, object]:
    """``(seen, patch)``: while ``patch`` is active, each time a ``TemporaryDirectory`` removes
    ``root``, the processes still naming it (``processes_naming``) are appended to ``seen``. A
    test's cleanups that wait for its workers first leave ``[{}]`` (#1511, #2479); one that races
    a live worker records it, every time, whether or not that ``rmtree`` happened to fail."""
    seen: list = []
    original = tempfile.TemporaryDirectory._rmtree

    def rmtree(name, *args, **kwargs):
        if Path(name) == Path(root):
            seen.append(processes_naming(str(root)))
        return original(name, *args, **kwargs)
    return seen, mock.patch.object(tempfile.TemporaryDirectory, "_rmtree", staticmethod(rmtree))
