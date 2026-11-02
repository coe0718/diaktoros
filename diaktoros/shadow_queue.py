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
* the reviewer's turn still running, its review being posted or confirmed, or an uncertain or
  unreadable record: it waits and is polled again, until ``WAIT_MAX_S``, then skipped.
"""
from __future__ import annotations

WAIT_MAX_S = 3 * 60 * 60        # a reviewer turn's CI wait, its budget and retries fit in this
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
            reviewer = con.execute(
                "SELECT id, state FROM runs WHERE repo=? AND pr=? AND head=? AND seat='reviewer' "
                "AND turn_key=? ORDER BY created DESC LIMIT 1",
                (row["repo"], row["pr"], row["head"], row["turn_key"] or "")).fetchone()
            receipt = reviewer and con.execute(
                "SELECT state, verdict FROM review_receipts WHERE run_id=?",
                (reviewer[0],)).fetchone()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - unknown is never "approved"
        return "wait", f"the reviewer's verdict is unreadable ({type(exc).__name__})"
    if reviewer is None:
        return "run", ""                   # a row from before #1647: run as it was queued
    if receipt and receipt[0] == "confirmed":
        if str(receipt[1]) in APPROVED:
            return "run", ""
        return "skip", f"the reviewer requested changes at {short}, so no shadow review"
    if receipt or reviewer[1] in OPEN:
        return "wait", f"waiting for the reviewer's verdict at {short}"
    return "skip", f"the reviewer's turn at {short} ended {reviewer[1]} without a verdict"
