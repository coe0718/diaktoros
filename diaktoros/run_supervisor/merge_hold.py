"""A host merge push's hold on its PR (#2468): the push and a turn's claim, one at a time.

The host merges the base into a PR's branch (``update_behind``, ``review_only_update``) only when
no turn of the PR is queued or in flight. Looking first and pushing after leaves a gap: a turn
queued in it is claimed at the head the push then moves. So the push takes a hold in the same
``BEGIN IMMEDIATE`` that finds nothing in flight, and the claim (``_claim_commit``, under the
same writer lock) does not claim a turn of a held PR: it waits, and its next claim reads the
moved head. Whichever commits first, the other sees it.

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
            con.execute("INSERT OR REPLACE INTO merge_holds (repo, pr, head, token, created) "
                        "VALUES (?, ?, ?, ?, ?)", (repo, pr, head, token, now))
            con.execute("COMMIT")
            return token, ""
    except sqlite3.Error as exc:
        return "", f"the run ledger is unreadable ({type(exc).__name__})"


def release(db, repo: str, pr: int, token: str) -> None:
    """Drop this push's hold; another's (a newer take after this one went stale) stays."""
    with ledger.connect(db, timeout=ledger.LOCK_WAIT_S, isolation_level=None) as con:
        con.execute("DELETE FROM merge_holds WHERE repo=? AND pr=? AND token=?", (repo, pr, token))


def held(con, repo: str, pr: int, now: float) -> bool:
    """Whether a live push holds the PR, read on ``con`` (inside the caller's transaction)."""
    return con.execute("SELECT 1 FROM merge_holds WHERE repo=? AND pr=? AND created>?",
                       (repo, pr, now - HOLD_S)).fetchone() is not None
