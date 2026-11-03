"""A shadow review runs only on a head its reviewer turn approved (#1647).

A shadow review (#218) is a second opinion that can hold an approved head (``shadow.escalate``)
and file what the reviewer missed. On a head the reviewer sent back its verdict decides nothing:
the PR is not ready either way, and the fixer pushes a new head. A week of the ledger had 46% of
the shadow's tokens spent on such heads.

The shadow's row is still queued with its reviewer turn (``gate.enqueue_isolated``), on a
findings snapshot taken before that turn's row exists, so the head holds for it from the start
and it stays blind as before. What changes is when it runs: as it is claimed it asks
``after_reviewer`` what **its own** reviewer turn (same PR, head and turn key) decided, from the
ledger alone, with no GitHub read:

* a confirmed APPROVE: it runs;
* a confirmed change request, or a turn that succeeded without a verdict: it is skipped
  (cancelled), nothing spent;
* a turn that failed, or that an operator or the push policy cancelled, without a verdict and
  without writing: it waits, since a redelivered event or ``hermes dk retry`` can re-arm that
  same turn, and an APPROVE it then posts needs this shadow (review of #1656, F14). A moved head
  or a closed PR retires it at its claim, as for any shadow. Any other ended turn (one superseded
  by a review already at its head, or one that may have written) can never be re-armed, so its
  shadow is skipped: a wait would hold the head for ever (F15);
* a newer reviewer turn at the same head: it is skipped, since that turn's shadow is the one
  that counts (an older approval never runs a shadow on a head since sent back);
* the reviewer's turn still running, its review being posted or confirmed, or an uncertain or
  unreadable record: it waits and is polled again, for as long as that lasts. No time bound: a
  bound would end it with the head clear, and a late APPROVE would then be green with no shadow.
"""
from __future__ import annotations

import sqlite3

APPROVED = ("APPROVE", "APPROVED")          # a receipt names the event or GitHub's state
OPEN = ("pending", "waiting", "claimed", "launching", "running", "uncertain")


def after_reviewer(db, row) -> tuple[str, str]:
    """``(what, why)`` for a shadow row: ``run``, ``wait`` or ``skip``, by its reviewer turn."""
    what, why, _approved = decide(db, row)
    return what, why


def decide(db, row) -> tuple[str, str, float | None]:
    """``after_reviewer``'s answer, and with ``run`` when that APPROVE was confirmed (None for a
    row from before #1647). One ledger read for both: the shadow's CI wait starts at that time,
    not at its queueing, since the verdict wait before it has no bound (review of #1656, F11),
    and a second read that could fail on its own would fall back to the queueing."""
    from .run_supervisor import views
    short = str(row["head"])[:7]
    try:
        con = views.read_only_connect(db)
        if con is None:
            return "wait", "the run ledger is unreadable, so the reviewer's verdict is unknown", None
        try:
            con.row_factory = sqlite3.Row              # ``write_evidence`` reads by name
            reviewer = _reviewer_run(con, row)
            receipt = reviewer and con.execute(
                "SELECT state, verdict, COALESCE(confirmed, created) FROM review_receipts "
                "WHERE run_id=?", (reviewer[0],)).fetchone()
            newer = reviewer and con.execute(
                "SELECT 1 FROM runs WHERE repo=? AND pr=? AND head=? AND seat='reviewer' AND "
                "created>? LIMIT 1", (row["repo"], row["pr"], row["head"], reviewer[2])).fetchone()
            rearm = reviewer and _may_be_rearmed(con, reviewer)
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - unknown is never "approved"
        return "wait", f"the reviewer's verdict is unreadable ({type(exc).__name__})", None
    if reviewer is None:
        return "run", "", None             # a row from before #1647: run as it was queued
    if newer:
        return ("skip", f"a newer review at {short} supersedes the one this shadow was queued "
                "with", None)
    if receipt and receipt[0] == "confirmed":
        if str(receipt[1]) in APPROVED:
            return "run", "", float(receipt[2] or 0) or None
        return "skip", f"the reviewer requested changes at {short}, so no shadow review", None
    if receipt or reviewer[1] in OPEN:
        return "wait", f"waiting for the reviewer's verdict at {short}", None
    if rearm:
        return ("wait", f"the reviewer's turn at {short} ended {reviewer[1]} without a verdict; "
                "held, since a re-armed turn may yet approve", None)
    return "skip", f"the reviewer's turn at {short} ended {reviewer[1]} without a verdict", None


def _reviewer_run(con, row):
    """The reviewer run a shadow row was queued with: same PR, head and turn key, the newest."""
    return con.execute(
        "SELECT id, state, created, error FROM runs WHERE repo=? AND pr=? AND head=? AND "
        "seat='reviewer' AND turn_key=? ORDER BY created DESC LIMIT 1",
        (row["repo"], row["pr"], row["head"], row["turn_key"] or "")).fetchone()


def _may_be_rearmed(con, reviewer) -> bool:
    """Whether a reviewer turn that ended can still be re-armed, so its shadow waits for it: the
    line ``retry`` draws. A failed turn, or one an operator or the push policy cancelled, re-arms
    if it never wrote. Any other cancellation was superseded (a review at its head already
    answers it, the head moved, the PR closed): ``retry`` refuses it, and a redelivery re-arms it
    only for the claim to cancel it again (review of #1656, F15)."""
    from . import run_supervisor
    state, error = reviewer[1], reviewer[3]
    if state not in run_supervisor.REARMABLE:
        return False
    if state == "cancelled" and not (run_supervisor.policy_cancelled(error)
                                     or run_supervisor.operator_cancelled(error)):
        return False
    return run_supervisor.write_evidence(con, reviewer[0]) is None
