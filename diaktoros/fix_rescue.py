"""An owed fix whose turn a head move ended (#2759).

A change request at a PR's head is the fixer's to answer: ``gate_fixer`` queues the turn when the
verdict's webhook arrives. When a base merge moves the head before that turn is done (the host's
``update_behind``, the review-only update, or GitHub's Update branch), the turn ends without
answering it: its claim cancels it (``FIX_VERDICT_SUPERSEDED``), or its staging finds the head gone
(``FetchDenied: stale PR head``). ``merge_only`` carries the change request to the merged head and
makes a turn still queued follow it (#2757), but a turn that already ended is asked for again by
nobody: ``retry`` re-arms no cancelled turn, a redelivered verdict names the old head (the gate
drops it), and the sweep only said the fixer never pushed. A verdict whose webhook queued no turn
at all is in the same place.

``owed`` says when the sweep may queue that turn at the current head itself; ``queue`` queues it
under the gate's own ledger key, so a late webhook and a later sweep are the same turn. A turn
that ran and failed for its own reasons, or ended without pushing, is never re-run here: that
stays the operator's (``retry``, ``unstick``).
"""
from __future__ import annotations

import sqlite3

from . import gate, merge_only, run_supervisor, util
from .run_supervisor import views

# How a fix turn's staging ends when the PR's head moved before it could fetch it: nothing ran.
STALE_STAGING = "FetchDenied: stale PR head"
# A turn created this long before the verdict's GitHub time still answers it: the clocks differ.
CLOCK_SLACK_S = 120


def moved(state: str, error: str | None) -> bool:
    """Whether a fix turn ended only because the PR's head moved: cancelled at its claim, or
    failed at staging before anything ran."""
    return ((state == "cancelled" and error == run_supervisor.FIX_VERDICT_SUPERSEDED)
            or (state == "failed" and str(error or "").endswith(STALE_STAGING)))


def _rows(db, repo: str, number: int, head: str, since: float):
    """The PR's fix turns under the gate's key (no ``turn_key``) at ``head`` or created since
    ``since``, oldest first, as (head, state, error); None when the ledger is absent or
    unreadable."""
    try:
        con = views.read_only_connect(db)
        if con is None:
            return None
        try:
            return con.execute(
                "SELECT head, state, error FROM runs WHERE repo=? AND pr=? AND seat='fixer' "
                "AND COALESCE(turn_key, '')='' AND (head=? OR created>=?) ORDER BY created",
                (repo, number, head, since)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None


def turn_at(db, repo: str, number: int, head: str, seat: str) -> dict | None:
    """``seat``'s newest turn at this PR head as ``{"id", "state"}`` (for the fixer, under the
    gate's key only: a CI-fix or conflict turn answers no verdict); None when there is none, or
    the ledger is absent or unreadable."""
    plain = " AND COALESCE(turn_key, '')=''" if seat == "fixer" else ""
    try:
        con = views.read_only_connect(db)
        if con is None:
            return None
        try:
            row = con.execute(f"SELECT id, state FROM runs WHERE repo=? AND pr=? AND head=? AND "
                              f"seat=?{plain} ORDER BY updated DESC, created DESC LIMIT 1",
                              (repo, number, head, seat)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return {"id": row[0], "state": row[1]} if row else None


def owed(loop: dict, db, number: int, head: str, latest: dict) -> bool | None:
    """Whether the change request ``latest`` (the latest effective verdict at ``head``, its own
    or carried there) is owed a fix turn nobody will queue: there is none at this head, and every
    one since the verdict ended only because the head moved on, by merges of the base alone, to
    this one. None when the ledger or a merge-only read cannot be read: a later sweep decides."""
    at = util.iso_epoch(latest.get("submitted_at"))
    if at is None:
        return False
    rows = _rows(db, loop["repo"], number, head, at - CLOCK_SLACK_S)
    if rows is None:
        return None
    for row_head, state, error in rows:
        if row_head == head or not moved(state, error):
            return False          # a turn at this head, or one that ran: not this rescue's
        follows = merge_only.leads_to(loop, head, row_head)
        if not follows:
            return follows        # None: unreadable, decided later; False: a real push since
    return True


def queue(loop: dict, number: int, head: str) -> str:
    """Queue the owed fix turn at ``head`` under the gate's own key: the ledger's outcome
    (``gate.SCHEDULED_OUTCOMES`` when it scheduled one). Raises as ``enqueue_isolated`` does."""
    return gate.enqueue_isolated(loop, "fixer", number, head)
