"""Whether ``backup.create`` may snapshot the install now, given ``migrate``'s pause (#1054,
#2322). Split out of ``backup``, which is over the size cap.

While the marker exists (a migrate or restore holds it, or one died holding it) records may be
half moved, and they are only safe under the pause: an archive of them restored later would come
back with no marker, and the loop would run on them unpaused (#1054). Checking the marker only
before the archive's first byte left a window: a migrate that wrote its marker during the write,
or that started and even finished during it, had moved records under a backup that still wrote
``migrating: None`` into its manifest, which ``restore`` takes for a clean archive (#2322).

So a backup holds ``migrate``'s lock shared for its whole write (``held_off``). ``migrate.hold``
takes the same lock exclusive and without waiting, so a migrate or restore that starts while a
backup is being written refuses (``MigrationBusy``) and moves nothing, and a backup that starts
while one holds it refuses here. ``migrate``'s and ``restore``'s own backups (``held``) are taken
under their exclusive hold and take nothing. A lock that cannot be taken for any other reason
(no ``flock`` on the filesystem, ``ENOLCK``) refuses the backup: ``migrate.hold`` refuses the same.
"""
from __future__ import annotations

import contextlib
import fcntl
import os

from . import backup, migrate


def refuse(held: bool) -> None:
    """Raise ``backup.BackupError`` with ``migrate.snapshot_refusal``'s reason while the marker
    exists, unless the caller holds it itself (``held``: ``migrate``'s and ``restore``'s own
    backups, which record what an interrupted run left as ``half_moved`` instead). The marker is
    read once: read twice, it could go between the test and the message."""
    why = "" if held else migrate.snapshot_refusal()
    if why:
        raise backup.BackupError(why)


@contextlib.contextmanager
def held_off(held: bool):
    """Keep any migrate or restore from starting until the block ends, and refuse (``refuse``)
    if one holds the install now; ``held`` takes nothing (see the module docstring)."""
    if held:
        yield
        return
    path = migrate.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:        # a migrate or restore holds it, its marker maybe not yet
            raise backup.BackupError(migrate.snapshot_refusal() or (
                "a migration or restore is starting; back up once it has finished")) from None
        except OSError as exc:         # fail closed: unlocked, a migrate could run mid-write
            raise backup.BackupError(f"could not lock {path} to keep a migrate or restore out "
                                     f"while backing up ({exc}); nothing was written") from exc
        refuse(False)
        yield
    finally:
        os.close(fd)                   # closing the descriptor releases the lock
