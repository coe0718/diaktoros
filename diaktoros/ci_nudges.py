"""The notes ``ci_event`` leaves for whoever holds the sweep lock (#681, #1996): each finished
suite's commit and PR numbers, taken and cleared by ``ci_event.drain``. Each note has a token, and
a take lists the tokens it cleared (``taken``), so a handler whose note is gone can tell a holder
that took it from the cap (``NUDGES_MAX``) that dropped it (review on #2577, F4).
"""
from __future__ import annotations

import os
import re

NUDGES = "ci-nudges.json"     # suites noted for the lock's holder, newest last
NUDGES_MAX = 50
# The tokens of notes a take cleared, newest last (review on #2577, F4): a handler whose note is
# gone records "another holder took it" only when one did; a note the cap dropped is read here.
TAKEN = "ci-nudges-taken.json"
TAKEN_MAX = 500
_SHA = re.compile(r"[0-9a-f]{40}")


def note(st, head: str, numbers: list[int]) -> str:
    """Note a finished suite for whoever holds the sweep lock next (``drain``); its token, which
    ``taken`` lists once a take clears it."""
    path, token = st.dir / NUDGES, os.urandom(8).hex()
    with st.locked():
        noted = st.kv_get(path, [])
        noted = noted if isinstance(noted, list) else []
        entry = {"head": head, "numbers": numbers, "token": token}
        st.kv_set(path, (noted + [entry])[-NUDGES_MAX:])
    return token


def taken(st) -> set[str]:
    """The tokens of the notes a take has cleared (the newest ``TAKEN_MAX``)."""
    with st.locked():
        found = st.kv_get(st.dir / TAKEN, [])
    return {t for t in found if isinstance(t, str)} if isinstance(found, list) else set()


def _take(st) -> dict[str, list[int]]:
    """The noted suites, cleared: the PR numbers named for each commit."""
    path = st.dir / NUDGES
    with st.locked():
        noted = st.kv_get(path, [])
        st.kv_set(path, [])
        noted = noted if isinstance(noted, list) else []
        tokens = [e.get("token") for e in noted if isinstance(e, dict)]
        if any(isinstance(t, str) for t in tokens):
            was = st.kv_get(st.dir / TAKEN, [])
            was = was if isinstance(was, list) else []
            st.kv_set(st.dir / TAKEN, (was + [t for t in tokens if isinstance(t, str)])[-TAKEN_MAX:])
    heads: dict[str, list[int]] = {}
    for entry in noted:
        head = entry.get("head") if isinstance(entry, dict) else None
        if isinstance(head, str) and _SHA.fullmatch(head):
            heads.setdefault(head, []).extend(n for n in entry.get("numbers") or []
                                              if type(n) is int and n > 0)
    return heads
