"""Whether ``backup.create`` may snapshot the install now, given ``migrate``'s pause marker
(#1054, #2322). Split out of ``backup``, which is over the size cap.

While the marker exists (a migrate or restore holds it, or one died holding it) records may be
half moved, and they are only safe under the pause: an archive of them restored later would come
back with no marker, and the loop would run on them unpaused (#1054). ``create`` asks twice:
before the archive's first byte, and again once its last is written. The first check alone left a
window: a backup that passed it and then raced a ``migrate`` that wrote its marker while the
archive was being written read records half moved, and still wrote ``migrating: None`` into its
manifest, which ``restore`` takes for a clean archive (#2322). Asked again at the end, such an
archive is refused and deleted, never published.

A migration that both starts and finishes between the two checks leaves no marker for either to
see. Closing that too would mean holding ``migrate``'s lock for the whole write, so that a
migrate refuses while any backup runs; that is not done here.
"""
from __future__ import annotations

from . import backup, migrate


def refuse(held: bool) -> None:
    """Raise ``backup.BackupError`` with ``migrate.snapshot_refusal``'s reason while the marker
    exists, unless the caller holds it itself (``held``: ``migrate``'s and ``restore``'s own
    backups, which record what an interrupted run left as ``half_moved`` instead). The marker is
    read once: read twice, it could go between the test and the message."""
    why = "" if held else migrate.snapshot_refusal()
    if why:
        raise backup.BackupError(why)
