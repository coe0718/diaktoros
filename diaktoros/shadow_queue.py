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
* a confirmed change request, or a turn that ended without a verdict: it is skipped (cancelled),
  nothing spent;
* a newer reviewer turn at the same head: it is skipped, since that turn's shadow is the one
  that counts (an older approval never runs a shadow on a head since sent back);
* the reviewer's turn still running, its review being posted or confirmed, or an uncertain or
  unreadable record: it waits and is polled again, for as long as that lasts. No time bound: a
  bound would end it with the head clear, and a late APPROVE would then be green with no shadow.
"""
from __future__ import annotations

APPROVED = ("APPROVE", "APPROVED")          # a receipt names the event or GitHub's state
OPEN = ("pending", "waiting", "claimed", "launching", "running", "uncertain")


def after_reviewer(db, row) -> tuple[str, str]:
    """``(what, why)`` for a shadow row: ``run``, ``wait`` or ``skip``, by its reviewer turn."""
    from .run_supervisor import views
    short = str(row["head"])[:7]
    try:
        con = views.read_only_connect(db)
        if con is None:
            return "wait", "the run ledger is unreadable, so the reviewer's verdict is unknown"
        try:
            reviewer = _reviewer_run(con, row)
            receipt = reviewer and con.execute(
                "SELECT state, verdict FROM review_receipts WHERE run_id=?",
                (reviewer[0],)).fetchone()
            newer = reviewer and con.execute(
                "SELECT 1 FROM runs WHERE repo=? AND pr=? AND head=? AND seat='reviewer' AND "
                "created>? LIMIT 1", (row["repo"], row["pr"], row["head"], reviewer[2])).fetchone()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - unknown is never "approved"
        return "wait", f"the reviewer's verdict is unreadable ({type(exc).__name__})"
    if reviewer is None:
        return "run", ""                   # a row from before #1647: run as it was queued
    if newer:
        return "skip", f"a newer review at {short} supersedes the one this shadow was queued with"
    if receipt and receipt[0] == "confirmed":
        if str(receipt[1]) in APPROVED:
            return "run", ""
        return "skip", f"the reviewer requested changes at {short}, so no shadow review"
    if receipt or reviewer[1] in OPEN:
        return "wait", f"waiting for the reviewer's verdict at {short}"
    return "skip", f"the reviewer's turn at {short} ended {reviewer[1]} without a verdict"


def _reviewer_run(con, row):
    """The reviewer run a shadow row was queued with: same PR, head and turn key, the newest."""
    return con.execute(
        "SELECT id, state, created FROM runs WHERE repo=? AND pr=? AND head=? AND "
        "seat='reviewer' AND turn_key=? ORDER BY created DESC LIMIT 1",
        (row["repo"], row["pr"], row["head"], row["turn_key"] or "")).fetchone()


def approved_at(db, row) -> float | None:
    """When the shadow's own reviewer turn had its APPROVE confirmed, or None (none yet, a row
    from before #1647, or an unreadable ledger). The shadow's CI wait starts here, not at its
    queueing: the verdict wait before it has no bound (review of #1656, F11)."""
    from .run_supervisor import views
    try:
        con = views.read_only_connect(db)
        if con is None:
            return None
        try:
            reviewer = _reviewer_run(con, row)
            receipt = reviewer and con.execute(
                "SELECT confirmed FROM review_receipts WHERE run_id=? AND state='confirmed'",
                (reviewer[0],)).fetchone()
        finally:
            con.close()
        return float(receipt[0]) if receipt and receipt[0] is not None else None
    except Exception:  # noqa: BLE001 - unknown: the clock falls back to the queueing
        return None
