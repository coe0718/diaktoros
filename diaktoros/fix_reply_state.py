"""What the reply wake (``fix_reply``) keeps between sweeps, and how its two writers share it.

``STATE`` holds one record per issue, ``{"<n>": {"stamp", "wait", "seen"}}``: ``wait`` the fixer
comment it waits on a maintainer's reply to, for ``status``; ``seen`` a reply seen while the open
PR listing could not be read, ``{"at"}`` when first seen (its retry window runs from it); and
``stamp`` when the reads behind the record's last decision began. ``WAITING``, the file before
``STATE``, is folded into it by the first commit, and removed once that write has landed.

The sweep and a webhook's wake (``fix_reply.on_comment``) both decide about an issue from GitHub
reads that take a while, in different processes, so one can finish its reads after the other
has started and finished newer ones (Vex on #2549, F5 to F7, F10). So the newest reads decide:
every decision is committed with the stamp taken before the ledger read it rests on, under the
loop's state lock, and a commit whose reads began before the record's stamp is dropped. The
record and its stamp are one write, so neither lands without the other (F11): a write that
fails changes nothing, and a later sweep's reads decide again. A decision leaves alone what it
did not read: an unreadable issue keeps its wait and its guard (#2019).
"""
from __future__ import annotations

import time

from .util import log

STATE = "fix-reply-state.json"  # {"<n>": {"stamp": int, "wait": comment id, "seen": {"at"}}}
WAITING = "fix-replies.json"    # before STATE: {"<n>": comment id}, read for issues with no record
KEEP = object()                 # a commit's "leave this one as it is"
_STAMP_DAYS = 60                # a bare stamp older than this decides nothing any more: dropped


def stamp() -> int:
    """When a decision's reads begin: nanoseconds, so two decisions never share one."""
    return time.time_ns()


def _read(st, name: str) -> dict:
    found = st.kv_get(st.dir / name, {})
    return dict(found) if isinstance(found, dict) else {}


def _entry(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("at"), (int, float))


def _records(st) -> dict:
    return {str(n): r for n, r in _read(st, STATE).items() if isinstance(r, dict)}


def guard(st) -> dict:
    """The replies seen while the listing was unreadable: ``{"<n>": {"at": first seen}}``."""
    return {n: r["seen"] for n, r in _records(st).items() if _entry(r.get("seen"))}


def waiting(st) -> dict:
    """``{"<n>": comment id}``: the records' waits, and ``WAITING``'s for issues with none (until
    the first commit folds it in)."""
    records = _records(st)
    found = {n: v for n, v in _read(st, WAITING).items() if n not in records}
    return {**found, **{n: r["wait"] for n, r in records.items() if r.get("wait") is not None}}


def commit(st, key: str, read_at: int, *, wait=KEEP, seen=KEEP, expect_at=None) -> bool:
    """Commit one decision about issue ``key``, made from reads begun at ``read_at``: ``wait`` its
    wait (``None`` drops it), ``seen`` its sighting (``{"at": now}``, an earlier ``at`` kept;
    ``None`` drops it). False, and nothing written, when a decision from later reads was
    committed first, ``expect_at`` is given and the sighting is not that one, or the write
    fails."""
    with st.locked():
        legacy = _read(st, WAITING)
        records = {**{str(n): {"wait": v} for n, v in legacy.items()}, **_records(st)}
        old = records.get(key) or {}
        if int(old.get("stamp") or 0) > read_at:
            return False
        if expect_at is not None and not (_entry(old.get("seen")) and old["seen"]["at"] == expect_at):
            return False
        new = {"stamp": read_at, "wait": old.get("wait") if wait is KEEP else wait,
               "seen": old.get("seen") if seen is KEEP or (seen and _entry(old.get("seen")))
               else seen}
        records[key] = {k: v for k, v in new.items() if v is not None}
        cutoff = stamp() - _STAMP_DAYS * 86400 * 10**9
        records = {n: r for n, r in records.items() if n in legacy    # its tombstone (F12)
                   or set(r) - {"stamp"} or int(r.get("stamp") or 0) > cutoff}
        try:
            st.kv_set_strict(st.dir / STATE, records)
        except Exception as exc:        # the record and its stamp stay as they were
            log(f"state write failed ({STATE}): {exc}")
            return False
        if legacy:                      # now in STATE; a failed removal is retried next commit
            try:
                (st.dir / WAITING).unlink()
            except OSError as exc:
                log(f"state cleanup failed ({WAITING}): {exc}")
        return True
