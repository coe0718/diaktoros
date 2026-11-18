"""What the reply wake (``fix_reply``) keeps between sweeps, and how its two writers share it.

``WAITING``: the issues waiting for a maintainer's reply at the last sweep, for ``status``.
``GUARD``: replies seen while the open PR listing could not be read, ``{n: {at, n}}``: ``at`` is
when the reply was first seen (its retry window runs from it), ``n`` counts the sightings.

The sweep and a webhook's wake (``fix_reply.on_comment``) both write these, from different
processes, each after slow GitHub reads, so neither may write back a view read before those
reads (Vex on #2549, F5, F6). Every write is made under the loop's state lock. A ``GUARD`` entry
is removed only while it is still the entry its writer decided on: a sighting recorded meanwhile
counts up ``n``, so it no longer matches and stands. ``WAITING`` is published against a ledger
read made inside the lock: a wake that queued a fix has written that turn first, so a sweep's
older "waits for a reply" is never written back over it.
"""
from __future__ import annotations

WAITING = "fix-replies.json"    # the issues waiting for a reply at the last sweep, for ``status``
GUARD = "fix-reply-guard.json"  # replies seen whose open-PR guard could not be read: {n: {at, n}}


def _read(st, name: str) -> dict:
    found = st.kv_get(st.dir / name, {})
    return dict(found) if isinstance(found, dict) else {}


def _entry(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("at"), (int, float))


def guard(st) -> dict:
    """``GUARD``'s well-formed entries: ``{"<n>": {"at": first seen, "n": sightings}}``."""
    return {str(n): e for n, e in _read(st, GUARD).items() if _entry(e)}


def waiting(st) -> dict:
    return _read(st, WAITING)


def sighted(st, key: str, now: float) -> None:
    """Record one more sighting of the reply on issue ``key`` while the listing is unreadable."""
    with st.locked():
        found = _read(st, GUARD)
        old = found.get(key) if _entry(found.get(key)) else {"at": now, "n": 0}
        found[key] = {"at": old["at"], "n": int(old.get("n") or 0) + 1}
        st.kv_set(st.dir / GUARD, found)


def unguard(st, decided: dict) -> None:
    """Remove each ``GUARD`` entry in ``decided`` (``{key: the entry decided on}``) only while it
    is still that entry: a sighting recorded since then stands."""
    with st.locked():
        found = _read(st, GUARD)
        gone = [key for key, entry in decided.items() if entry and found.get(key) == entry]
        for key in gone:
            del found[key]
        if gone:
            st.kv_set(st.dir / GUARD, found)


def settled(st, key: str) -> None:
    """Issue ``key`` no longer waits for a reply: its fix was queued, or it can't be fixed."""
    with st.locked():
        found = _read(st, WAITING)
        if found.pop(key, None) is not None:
            st.kv_set(st.dir / WAITING, found)


def publish_waiting(st, found: dict, still) -> None:
    """Publish a sweep's ``found`` waits, keeping only the issues ``still()`` (a ledger read, made
    here inside the lock) says still wait: none with a turn queued since the sweep read them."""
    with st.locked():
        open_now = still()
        st.kv_set(st.dir / WAITING, {k: v for k, v in found.items() if k in open_now})
