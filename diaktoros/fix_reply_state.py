"""What the reply wake (``fix_reply``) keeps between sweeps, and how its two writers share it.

``WAITING``: the issues waiting for a maintainer's reply, for ``status``. ``GUARD``: replies seen
while the open PR listing could not be read, ``{n: {at}}``, ``at`` when the reply was first seen
(its retry window runs from it).

The sweep and a webhook's wake (``fix_reply.on_comment``) both decide about an issue from GitHub
reads that take a while, in different processes, so one can finish its reads after the other
has started and finished newer ones (Vex on #2549, F5 to F7). So the newest reads decide: every
decision about an issue is committed with ``stamp()`` taken before the reads it rests on, under
the loop's state lock, and a commit whose reads began before the issue's last committed
decision's is dropped. A decision rests only on its own issue's reads, and leaves alone what it
did not read: an unreadable issue keeps its wait and its guard (#2019).
"""
from __future__ import annotations

import time

WAITING = "fix-replies.json"    # the issues waiting for a maintainer's reply, for ``status``
GUARD = "fix-reply-guard.json"  # replies seen whose open-PR guard could not be read: {n: {at}}
STAMPS = "fix-reply-stamps.json"    # each issue's last committed decision: {n: its reads' stamp}
KEEP = object()                 # a commit's "leave this one as it is"
_STAMP_DAYS = 60                # a stamp older than this decides nothing any more: dropped


def stamp() -> int:
    """When a decision's reads begin: nanoseconds, so two decisions never share one."""
    return time.time_ns()


def _read(st, name: str) -> dict:
    found = st.kv_get(st.dir / name, {})
    return dict(found) if isinstance(found, dict) else {}


def _entry(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("at"), (int, float))


def guard(st) -> dict:
    """``GUARD``'s well-formed entries: ``{"<n>": {"at": first seen}}``."""
    return {str(n): e for n, e in _read(st, GUARD).items() if _entry(e)}


def waiting(st) -> dict:
    return _read(st, WAITING)


def commit(st, key: str, read_at: int, *, wait=KEEP, seen=KEEP, expect_at=None) -> bool:
    """Commit one decision about issue ``key``, made from reads begun at ``read_at``: ``wait`` its
    ``WAITING`` value (``None`` drops it), ``seen`` its ``GUARD`` sighting (``{"at": now}``, an
    earlier ``at`` kept; ``None`` drops it, only while its ``at`` is ``expect_at`` when given).
    False, and nothing written, when a decision from later reads was committed first."""
    with st.locked():
        stamps = _read(st, STAMPS)
        if int(stamps.get(key) or 0) > read_at:
            return False
        cutoff = stamp() - _STAMP_DAYS * 86400 * 10**9
        stamps = {k: v for k, v in stamps.items() if isinstance(v, int) and v > cutoff}
        stamps[key] = read_at
        if wait is not KEEP:
            found = _read(st, WAITING)
            found.pop(key, None) if wait is None else found.update({key: wait})
            st.kv_set(st.dir / WAITING, found)
        if seen is not KEEP:
            found = _read(st, GUARD)
            old = found.get(key)
            if seen is not None:
                found[key] = old if _entry(old) else seen
            elif expect_at is None or (_entry(old) and old["at"] == expect_at):
                found.pop(key, None)
            st.kv_set(st.dir / GUARD, found)
        st.kv_set(st.dir / STAMPS, stamps)
        return True
