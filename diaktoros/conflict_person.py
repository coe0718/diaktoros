"""A PR head whose conflict only a person can resolve (#1154's notices): recorded once, so the
loop stops trying.

The watchdog queues one conflict turn per (head, base tip). When the host's merge refuses it as a
person's (the base changed workflow files the fixer's token cannot push, or a whole-file
conflict), every merge to the base made a new key, so a new turn, the same failure and another
failure notice. The refusal is recorded here for the head instead; the watchdog queues no conflict
turn for a recorded head, and a new push (a new head) clears it.

Only the workflow-scope refusal is one no later base fixes for the head, so only it is kept until
a new head (#1271). A whole-file conflict (modify/delete, rename, binary) can vanish, or become a
content conflict the fixer resolves, once the base moves on (the base deletes the same file, or
re-adds it), so its record lapses after ``WHOLE_FILE_RETRY_S``: then the next base tip gets one
turn again (the turn key still dedups a tip already tried). That bounds #1154's notices to about
one a day per head instead of one per merge.
"""
from __future__ import annotations

import json
import time

from diaktoros.util import log

FILE = "conflict-person.json"
# How long a whole-file conflict's record holds before a later base tip is tried again (#1271).
WHOLE_FILE_RETRY_S = 24 * 3600
# How safe_push's whole-file NeedsPerson reason begins (safe_push builds it from this constant).
# A record written before #1271 has no ``until``; one with this reason is still a whole-file
# refusal, so ``get`` lapses it from its ``at`` like a new one (the workflow reason never matches).
WHOLE_FILE_REASON = "merge conflicts a person must resolve"


def _path(st):
    return st.dir / FILE


def _read(st) -> dict:
    """Lock-free (writes are atomic replaces); unreadable is empty, i.e. the old behaviour, but a
    file that exists and cannot be read is logged: without the record every base merge requeues
    the refused conflict turn again (#1154), and nothing else would say why."""
    try:
        data = json.loads(_path(st).read_text())
    except (FileNotFoundError, NotADirectoryError):
        return {}          # genuinely absent: the normal case, silent
    except Exception as exc:
        # Anything else (permissions on the file or an unsearchable parent, bad JSON, ...) is a
        # store we cannot tell from an absent one: say so rather than treating it as missing.
        log(f"{FILE} exists but is unreadable ({type(exc).__name__}: {exc}); treating it as "
            f"empty, so conflict turns requeue on every base merge until it is fixed")
        return {}
    if not isinstance(data, dict):
        log(f"{FILE} does not hold a JSON object; treating it as empty, so conflict turns "
            f"requeue on every base merge until it is fixed")
        return {}
    return data


def get(st, number: int, head: str) -> str:
    """Why this head's conflict needs a person, or "" (none recorded for this head, or a record
    whose ``until`` has passed, #1271)."""
    entry = _read(st).get(str(number))
    if not isinstance(entry, dict) or entry.get("head") != head:
        return ""
    until = _until(entry)
    if until is not None and time.time() >= until:
        return ""
    return str(entry.get("reason") or "needs a person")


def _number(value):
    """``value`` if it is a real number (not a bool), else None."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _until(entry: dict):
    """When ``entry`` lapses, or None (held until a new head). A record from before #1271 has no
    ``until``: a whole-file one (its reason starts with ``WHOLE_FILE_REASON``) lapses
    ``WHOLE_FILE_RETRY_S`` after its ``at``, so heads stuck before the upgrade are retried too."""
    if "until" in entry:
        return _number(entry.get("until"))
    at = _number(entry.get("at"))
    if at is not None and str(entry.get("reason") or "").startswith(WHOLE_FILE_REASON):
        return at + WHOLE_FILE_RETRY_S
    return None


def record(st, number: int, head: str, reason: str, *, lapse_s: float | None = None) -> None:
    """Remember that ``head``'s conflict needs a person (replacing an older head's record).

    ``lapse_s`` None keeps it until a new head (a refusal no later base fixes); a number lets it
    lapse after that many seconds, so a later base tip is tried again (a whole-file conflict)."""
    now = time.time()
    entry: dict = {"head": head, "reason": reason, "at": now}
    if lapse_s is not None:
        entry["until"] = now + lapse_s
    with st.locked():
        memory = st.kv_get(_path(st), {})
        memory = memory if isinstance(memory, dict) else {}
        memory[str(number)] = entry
        st.kv_set(_path(st), memory)


def prune(st, open_numbers) -> None:
    """Drop records for PRs no longer open."""
    keep = {str(n) for n in open_numbers}
    current = _read(st)
    if not any(k not in keep for k in current):
        return
    with st.locked():
        memory = st.kv_get(_path(st), {})
        if isinstance(memory, dict):
            st.kv_set(_path(st), {k: v for k, v in memory.items() if k in keep})
