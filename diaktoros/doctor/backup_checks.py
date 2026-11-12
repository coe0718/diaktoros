"""The backup-freshness check (#1081): is there a recent ``hermes dk backup`` archive?

``check_watchdog_last_run`` proves the watchdog *runs*; nothing else proves a backup *happens*,
so an install that was set up a year ago and never backed up passed every other line. Read-only,
like every doctor check; reaches the core through ``doctor`` (``from .. import doctor``), and
callers use ``backup_checks.<name>``.

What it can and cannot see: it looks in one place, ``backup.backups_dir()`` (where ``backup``
writes without ``--out``), at the file's modification time, against a fixed ``backup_fresh.FRESH_DAYS``
— ``backup`` has no output-directory or retention setting to read either from. An archive
written with ``--out`` somewhere else, or a backup of the host taken some other way, is invisible
here: the check then warns although a backup exists. It says an archive *exists*; it does not
open it, so it does not prove the archive would restore. An archive whose modification time is
more than ``backup_fresh.CLOCK_SKEW_S`` in the future (the clock was wrong when it was written, or it was
copied from a host whose clock is) is not counted as recent: its age is unknown, and counting it
would keep the line green for as long as the clock was ahead plus ``FRESH_DAYS`` (#1875). Once the
real clock comes within ``CLOCK_SKEW_S`` of such a file's date it is still not counted if ``hermes dk
backup`` saw it while it was ahead: that command records it (``backup_fresh.record_future_dated``, #1905).
One that no backup run saw while it was ahead cannot be told from a recent archive here. A backup
is of the whole install, not of a loop, so every loop's report carries the same line.
"""
from __future__ import annotations

import time

from .. import doctor

_DAY_S = 86400


def check_backup_fresh() -> doctor.Check:
    """Is the newest archive in the backups directory younger than ``backup_fresh.FRESH_DAYS``?

    A warning (``unknown``), never a failure: a loop with no backup yet still runs, so ``doctor``
    exits 0 and only ``--strict`` turns it into a failure — the same as ``fixer-push`` being off.
    ``report`` prints a ``fix:`` line for failures only, so the command is in the detail.
    """
    from .. import backup, backup_fresh   # here, not module level: importing them loads every writer module
    name = "backup:fresh"
    command = "hermes dk backup"
    where = backup.backups_dir()
    now = time.time()
    try:
        newest, ahead = backup_fresh.latest_archive(now)
    except backup_fresh.FutureRecordError as exc:     # #1905 review F2: never read as "none recorded"
        return doctor.Check(name, doctor.UNKNOWN,
                            f"{exc}, so which archives in {where} were dated in the future cannot "
                            f"be told and a recent backup cannot be confirmed",
                            f"repair {backup_fresh.FUTURE_RECORD} as a JSON {{archive name: mtime}} "
                            f"object, or remove it (the archives it named then count by their "
                            f"date), and run `{command}`")
    except OSError as exc:
        return doctor.Check(name, doctor.UNKNOWN,
                            f"cannot read {where} ({exc}), so a recent backup cannot be confirmed",
                            f"make {where} readable by the user running doctor")
    if newest is None and ahead is not None:
        path, written = ahead
        if written > now + backup_fresh.CLOCK_SKEW_S:
            when = f"is dated {(written - now) / _DAY_S:.0f} days in the future"
        else:   # recorded by ``hermes dk backup`` while it was still ahead (#1905)
            when = "was dated in the future when `hermes dk backup` last saw it"
        return doctor.Check(name, doctor.UNKNOWN,
                            f"{path.name} in {where} {when} (a wrong clock when it was written?), "
                            f"so how old the newest backup is cannot be told; run `{command}`",
                            f"`{command}`")
    if newest is None:
        return doctor.Check(name, doctor.UNKNOWN,
                            f"no backup archive in {where} — nothing would restore this install; "
                            f"run `{command}`", f"`{command}`")
    path, written = newest
    age_days = max(0.0, (now - written) / _DAY_S)
    if age_days > backup_fresh.FRESH_DAYS:
        return doctor.Check(name, doctor.UNKNOWN,
                            f"the newest backup in {where} is {path.name}, {age_days:.0f} days old "
                            f"(older than {backup_fresh.FRESH_DAYS}); run `{command}`", f"`{command}`")
    return doctor.Check(name, doctor.VERIFIED,
                        f"{path.name} in {where} is {age_days:.0f} day(s) old "
                        f"(within {backup_fresh.FRESH_DAYS} days)")
