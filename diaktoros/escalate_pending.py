"""An escalation asked for while a run is in flight (#1246): it takes effect at the PR's next
changes-requested verdict.

``hermes dk escalate`` cannot write a breach while a reviewer or fixer run is out (the run would
race the marker), and with unattended fixer pushes on there is never a gap between one run and the
next. So the operator's request is recorded here instead. When the next verdict requests changes,
the fixer gate writes the breach in place of the fixer turn and the adjudicator rules on that head;
when it approves, the dispute settled itself and the request is dropped with a notice. A ruling is
therefore always on a head the reviewer actually blocked, and no run is ever interrupted.

One record per PR in the loop's state dir. It lapses after ``TTL_S`` with no verdict, and the
watchdog drops it once the PR is no longer open.
"""
from __future__ import annotations

import json
import time

from . import gate

FILE = "escalate-pending.json"
TTL_S = 24 * 3600


def _path(st):
    return st.dir / FILE


def _live(entry, now: float) -> bool:
    at = entry.get("at") if isinstance(entry, dict) else None
    return (isinstance(at, (int, float)) and not isinstance(at, bool) and 0 < at <= now
            and now - at < TTL_S)


def _read(st) -> dict:
    """The records, read without a lock or a write (writes are atomic replaces): ``explain`` stays
    read-only, and a gate on a PR with nothing pending touches nothing. Unreadable is empty, so
    the loop then behaves as it did before #1246."""
    try:
        path = _path(st)
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def get(st, number: int) -> dict | None:
    """The PR's pending escalation, or None (none, unreadable, or lapsed)."""
    entry = _read(st).get(str(number))
    return entry if _live(entry, time.time()) else None


_OMITTED = object()     # ``by`` not passed: told apart from any value, ``None`` and "" included


def record(st, number: int, *, reason: str, head: str, by=_OMITTED) -> dict:
    """Record (or replace) the PR's pending escalation; returns it. ``by`` is the breach
    marker's ``escalated.by`` it will be written with: the operator's when it is not passed. A
    value that is passed is stored as it is, ``None`` and an empty string too, as before the
    constant (the fixer gate then falls back to the operator kind when it reads the record)."""
    entry = {"reason": reason, "head": head,
             "by": gate.OPERATOR_ESCALATION if by is _OMITTED else by, "at": time.time()}
    with st.locked():
        memory = st.kv_get(_path(st), {})
        memory = memory if isinstance(memory, dict) else {}
        memory[str(number)] = entry
        # Strict (#1262): the CLI says "pending" only once it is on disk; a failed write raises.
        st.kv_set_strict(_path(st), memory)
    return entry


def take(st, number: int) -> dict | None:
    """Remove and return the PR's pending escalation if it is live (lapsed ones are dropped)."""
    if str(number) not in _read(st):
        return None                     # nothing recorded for it: no lock, no write
    with st.locked():
        memory = st.kv_get(_path(st), {})
        memory = memory if isinstance(memory, dict) else {}
        entry = memory.pop(str(number), None)
        if entry is not None:
            st.kv_set_strict(_path(st), memory)     # a record that will not go must not act twice
    return entry if _live(entry, time.time()) else None


def prune(st, open_numbers) -> list[int]:
    """Drop records for PRs no longer open, and lapsed ones; the PR numbers dropped.

    Strict like ``record`` and ``take`` (#1283): the watchdog logs each number returned as
    dropped, so a write that did not land raises (the sweep logs the failure) instead of
    returning numbers whose records are still on disk. The next sweep retries it."""
    keep = {str(n) for n in open_numbers}
    now = time.time()
    if not _read(st):
        return []
    with st.locked():
        memory = st.kv_get(_path(st), {})
        if not isinstance(memory, dict) or not memory:
            return []
        dropped = [k for k, v in memory.items() if k not in keep or not _live(v, now)]
        if dropped:
            st.kv_set_strict(_path(st), {k: v for k, v in memory.items() if k not in dropped})
    return [int(k) for k in dropped if k.isdigit()]
