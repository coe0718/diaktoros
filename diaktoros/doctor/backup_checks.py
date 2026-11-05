"""The backup-freshness check (#1081): is there a recent ``hermes dk backup`` archive?

``check_watchdog_last_run`` proves the watchdog *runs*; nothing else proves a backup *happens*,
so an install that was set up a year ago and never backed up passed every other line. Read-only,
like every doctor check; reaches the core through ``doctor`` (``from .. import doctor``), and
callers use ``backup_checks.<name>``.

What it can and cannot see: it looks in one place, ``backup.backups_dir()`` (where ``backup``
writes without ``--out``), at the file's modification time, against a fixed ``backup.FRESH_DAYS``
— ``backup`` has no output-directory or retention setting to read either from. An archive
written with ``--out`` somewhere else, or a backup of the host taken some other way, is invisible
here: the check then warns although a backup exists. It says an archive *exists*; it does not
open it, so it does not prove the archive would restore. A backup is of the whole install, not
of a loop, so every loop's report carries the same line.
"""
from __future__ import annotations

import time

from .. import doctor

_DAY_S = 86400


def check_backup_fresh() -> doctor.Check:
    """Is the newest archive in the backups directory younger than ``backup.FRESH_DAYS``?

    A warning (``unknown``), never a failure: a loop with no backup yet still runs, so ``doctor``
    exits 0 and only ``--strict`` turns it into a failure — the same as ``fixer-push`` being off.
    ``report`` prints a ``fix:`` line for failures only, so the command is in the detail.
    """
    from .. import backup   # here, not module level: importing it loads every writer module
    name = "backup:fresh"
    command = "hermes dk backup"
    where = backup.backups_dir()
    try:
        newest = backup.latest_archive()
    except OSError as exc:
        return doctor.Check(name, doctor.UNKNOWN,
                            f"cannot read {where} ({exc}), so a recent backup cannot be confirmed",
                            f"make {where} readable by the user running doctor")
    if newest is None:
        return doctor.Check(name, doctor.UNKNOWN,
                            f"no backup archive in {where} — nothing would restore this install; "
                            f"run `{command}`", f"`{command}`")
    path, written = newest
    age_days = max(0.0, (time.time() - written) / _DAY_S)
    if age_days > backup.FRESH_DAYS:
        return doctor.Check(name, doctor.UNKNOWN,
                            f"the newest backup in {where} is {path.name}, {age_days:.0f} days old "
                            f"(older than {backup.FRESH_DAYS}); run `{command}`", f"`{command}`")
    return doctor.Check(name, doctor.VERIFIED,
                        f"{path.name} in {where} is {age_days:.0f} day(s) old "
                        f"(within {backup.FRESH_DAYS} days)")
