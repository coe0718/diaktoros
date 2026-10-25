"""A PR head whose conflict only a person can resolve (#1154's notices): recorded once, so the
loop stops trying.

The watchdog queues one conflict turn per (head, base tip). When the host's merge refuses it for a
reason no later base fixes for this head (the base changed workflow files the fixer's token cannot
push, or a whole-file conflict), every merge to the base made a new key, so a new turn, the same
failure and another failure notice. The refusal is recorded here for the head instead; the
watchdog queues no conflict turn for a recorded head, and a new push (a new head) clears it.
"""
from __future__ import annotations

import json
import time

FILE = "conflict-person.json"


def _path(st):
    return st.dir / FILE


def _read(st) -> dict:
    """Lock-free (writes are atomic replaces); unreadable is empty, i.e. the old behaviour."""
    try:
        path = _path(st)
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def get(st, number: int, head: str) -> str:
    """Why this head's conflict needs a person, or "" (none recorded for this head)."""
    entry = _read(st).get(str(number))
    if isinstance(entry, dict) and entry.get("head") == head:
        return str(entry.get("reason") or "needs a person")
    return ""


def record(st, number: int, head: str, reason: str) -> None:
    """Remember that ``head``'s conflict needs a person (replacing an older head's record)."""
    with st.locked():
        memory = st._load(_path(st), {})
        memory = memory if isinstance(memory, dict) else {}
        memory[str(number)] = {"head": head, "reason": reason, "at": time.time()}
        st._save(_path(st), memory)


def prune(st, open_numbers) -> None:
    """Drop records for PRs no longer open."""
    keep = {str(n) for n in open_numbers}
    current = _read(st)
    if not any(k not in keep for k in current):
        return
    with st.locked():
        memory = st._load(_path(st), {})
        if isinstance(memory, dict):
            st._save(_path(st), {k: v for k, v in memory.items() if k in keep})
