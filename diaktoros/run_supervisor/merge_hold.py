"""A host merge push's hold on its PR (#2468): the push and a turn's claim, one at a time.

The host merges the base into a PR's branch (``update_behind``, ``review_only_update``) only when
no turn of the PR is queued or in flight. Looking first and pushing after leaves a gap: a turn
queued in it is claimed at the head the push then moves. So the push takes a hold in the same
``BEGIN IMMEDIATE`` that finds nothing in flight, and the claim (``_claim_commit``, under the
same writer lock) does not claim a turn of a held PR: it waits, and its next claim reads the
moved head. Whichever commits first, the other sees it.

A claim reads GitHub before its transaction, so a read made while the push was held (or after it
was taken) may be of the head the push then moved, even when the claim commits after the
release. A released hold is kept, not deleted, and each take bumps the PR's ``gen``: the claim
notes the hold before its reads (``fences``) and waits if a hold was live then, is live now, or
was taken in between (``spanned``).

A hold older than ``HOLD_S`` is a push that never came back (the process died): it holds
nothing, and the next take replaces it.
"""
from __future__ import annotations

import sqlite3
import time
import uuid

from .. import ledger

HOLD_S = 300        # the push it covers is an authorization read and one ``git push``
WAIT_S = 30         # how soon a turn the hold kept from its claim is tried again
WAITING = "waits for the host's merge of the base into the branch to land"
_LIVE = "released IS NULL AND created > ?"


def take(db, repo: str, pr: int, head: str, states: tuple) -> tuple[str, str]:
    """``(token, '')`` once held; ``('', what is in flight)`` when a turn of the PR is in one of
    ``states``, or another push holds it. An unreadable ledger is not idle."""
    now = time.time()
    try:
        with ledger.connect(db, timeout=ledger.LOCK_WAIT_S, isolation_level=None) as con:
            con.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" * len(states))
            row = con.execute(f"SELECT seat, state FROM runs WHERE repo=? AND pr=? AND state IN "
                              f"({marks}) LIMIT 1", (repo, pr, *states)).fetchone()
            why = (f"a {row[0]} turn is {row[1]}" if row is not None else
                   "another merge of the base is being pushed" if held(con, repo, pr, now) else "")
            if why:
                con.execute("ROLLBACK")
                return "", why
            token = uuid.uuid4().hex
            con.execute("INSERT INTO merge_holds (repo, pr, head, token, created, released, gen) "
                        "VALUES (?, ?, ?, ?, ?, NULL, 1) ON CONFLICT (repo, pr) DO UPDATE SET "
                        "head=excluded.head, token=excluded.token, created=excluded.created, "
                        "released=NULL, gen=gen+1", (repo, pr, head, token, now))
            con.execute("COMMIT")
            return token, ""
    except sqlite3.Error as exc:
        return "", f"the run ledger is unreadable ({type(exc).__name__})"


def release(db, repo: str, pr: int, token: str) -> None:
    """Release this push's hold; another's (a newer take after this one went stale) stays."""
    with ledger.connect(db, timeout=ledger.LOCK_WAIT_S, isolation_level=None) as con:
        con.execute("UPDATE merge_holds SET released=? WHERE repo=? AND pr=? AND token=?",
                    (time.time(), repo, pr, token))


def held(con, repo: str, pr: int, now: float) -> bool:
    """Whether a live push holds the PR, read on ``con`` (inside the caller's transaction)."""
    return con.execute(f"SELECT 1 FROM merge_holds WHERE repo=? AND pr=? AND {_LIVE}",
                       (repo, pr, now - HOLD_S)).fetchone() is not None


def fences(con, now: float) -> dict:
    """Every PR's hold as a claim's reads begin: ``{(repo, pr): (gen, live)}``."""
    return {(repo, pr): (gen, bool(live)) for repo, pr, gen, live in con.execute(
        f"SELECT repo, pr, gen, {_LIVE} FROM merge_holds", (now - HOLD_S,))}


def spanned(con, repo: str, pr: int, now: float, fence: tuple | None) -> bool:
    """Whether a push may have moved the PR's head since the claim's reads began, read on ``con``
    under the claim's lock: a hold is live now, or was live at ``fence`` (``fences``' entry, or
    ``(0, False)`` for no hold then), or was taken since. ``None`` checks only a live hold."""
    if held(con, repo, pr, now):
        return True
    if fence is None:
        return False
    row = con.execute("SELECT gen FROM merge_holds WHERE repo=? AND pr=?", (repo, pr)).fetchone()
    return fence[1] or fence[0] != (row[0] if row else 0)
