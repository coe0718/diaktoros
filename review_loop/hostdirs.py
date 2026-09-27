"""Host state directories: host-side commands create them; a detached worker never does.

A worker (``run_supervisor``'s ``_fixture-worker`` / ``_production-worker``, and anything it
runs in-process, such as the run broker) is marked by ``REVIEW_LOOP_WORKER=1`` in the
environment its supervisor builds for it. Host state it writes to must already exist: the
host created it before the run was enqueued, so a missing directory means something removed
it, and recreating it would leave stray state behind in a place the operator emptied.
"""
from __future__ import annotations

import os
import pathlib

WORKER_ENV = "REVIEW_LOOP_WORKER"


class HostStateGone(FileNotFoundError):
    """A worker found a host state directory or ledger missing; it creates nothing."""


def in_worker() -> bool:
    return os.environ.get(WORKER_ENV) == "1"


def ensure(path: str | os.PathLike) -> pathlib.Path:
    """Create ``path`` on the host; in a worker, require that it already exists."""
    path = pathlib.Path(path)
    if in_worker():
        if not path.is_dir():
            raise HostStateGone(f"{path} is gone; a worker never recreates host state")
        return path
    path.mkdir(parents=True, exist_ok=True)
    return path
