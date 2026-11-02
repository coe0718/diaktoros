"""The shadow review follows the reviewer's APPROVE (#1647).

A shadow review (#218) is a second opinion that can hold an approved head (``shadow.escalate``)
and file what the reviewer missed. On a head the reviewer sent back, its verdict decides nothing:
the PR is not ready either way, and the fixer pushes a new head. A week of the ledger had 46% of
the shadow's tokens spent on such heads. So a sampled head's shadow is queued once the reviewer's
APPROVE there is confirmed, not beside the reviewer's turn.

It stays blind to that approval. Its findings snapshot is still taken as the reviewer's turn is
queued (``gate.snapshot_shadow``), and its prompt shows only the reviews posted before then
(``shown_reviews``). Between the APPROVE and the shadow's row, ``gate.shadow_state`` holds the
head (``approved_unqueued``), so the merge check is never green in that gap; the watchdog's
``sweep`` queues any shadow the broker's call after the APPROVE did not.
"""
from __future__ import annotations

from . import config, gate, util
from .util import log

APPROVED = ("APPROVE", "APPROVED")      # a confirmed receipt names the event or GitHub's state


def approved_unqueued(con, repo: str, number: int, head: str) -> bool:
    """Whether the reviewer's APPROVE at this head is confirmed and no shadow run exists for the
    head yet: a sampled head is then waiting for its shadow review to be queued."""
    approved = con.execute(
        "SELECT 1 FROM review_receipts r JOIN runs u ON u.id = r.run_id WHERE r.state='confirmed' "
        f"AND u.seat='reviewer' AND u.repo=? AND u.pr=? AND u.head=? AND r.verdict IN "
        f"({','.join('?' * len(APPROVED))}) LIMIT 1", (repo, number, head, *APPROVED)).fetchone()
    if approved is None:
        return False
    shadow = con.execute("SELECT 1 FROM runs WHERE repo=? AND pr=? AND head=? AND seat='shadow' "
                         "LIMIT 1", (repo, number, head)).fetchone()
    return shadow is None


def shown_reviews(row, reviews: list, db) -> list:
    """The reviews a shadow's prompt shows: those posted before the reviewer's turn at its head
    was queued (#218), so the real review, its APPROVE included, never tells it the answer. With
    no such reviewer row, the shadow's own queueing time."""
    cutoff = float(row["created"])
    try:
        from .run_supervisor import views
        con = views.read_only_connect(db)
        if con is not None:
            try:
                found = con.execute(
                    "SELECT MIN(created) FROM runs WHERE repo=? AND pr=? AND head=? AND "
                    "seat='reviewer' AND turn_key=?",
                    (row["repo"], row["pr"], row["head"], row["turn_key"] or "")).fetchone()
            finally:
                con.close()
            if found and found[0] is not None:
                cutoff = min(cutoff, float(found[0]))
    except Exception as exc:  # noqa: BLE001 - the shadow's own time is the earlier rule
        log(f"#{row['pr']}: the reviewer's queueing time unreadable, the shadow's own is used: "
            f"{type(exc).__name__}: {exc}")
    return [r for r in reviews if not util.epoch(r.get("submitted_at")) > cutoff]


def after_approval(loop: dict, number: int, head: str, run_id: str | None, db) -> str:
    """Queue the shadow review of a head whose reviewer APPROVE just posted (the broker calls
    this). Best effort: the watchdog's ``sweep`` is the backstop. What happened, for logs."""
    try:
        from .run_supervisor import production_ledger
        db = db or production_ledger()
        turn_key = ""
        if run_id:
            from .run_supervisor import views
            con = views.read_only_connect(db)
            if con is not None:
                try:
                    found = con.execute("SELECT turn_key FROM runs WHERE id=?",
                                        (run_id,)).fetchone()
                finally:
                    con.close()
                turn_key = str(found[0] or "") if found else ""
        return gate.enqueue_shadow(loop, gate.isolated_supervisor(loop), number, head,
                                   turn_key=turn_key)
    except Exception as exc:  # noqa: BLE001 - the approval stands; the sweep retries
        log(f"#{number} @ {head[:7]}: shadow review not queued after the APPROVE: "
            f"{type(exc).__name__}: {exc}")
        return "error"


def sweep(loop: dict, prs: list, db=None) -> list[int]:
    """The watchdog's backstop: queue the shadow review of every open loop PR whose sampled head
    the reviewer approved and that has no shadow run yet; the PRs queued."""
    if not config.shadow_profile(loop):
        return []
    from .run_supervisor import production_ledger, views
    path = db or production_ledger()
    con = views.read_only_connect(path)
    if con is None:
        return []
    due = []
    try:
        for pr in prs:
            listed = config.loop_pr(loop, pr)
            if listed is None:
                continue
            number, head, _author = listed
            if gate.shadow_sampled(loop, number, head) and approved_unqueued(
                    con, loop["repo"], number, head):
                due.append((number, head))
    finally:
        con.close()
    queued = []
    for number, head in due:
        if after_approval(loop, number, head, _reviewer_run(path, loop["repo"], number, head),
                          path) in ("enqueued", "rearmed", "pending"):
            queued.append(number)
    return queued


def _reviewer_run(db, repo: str, number: int, head: str) -> str | None:
    """The newest reviewer run at this head with a confirmed APPROVE (its turn key is the
    shadow's)."""
    from .run_supervisor import views
    con = views.read_only_connect(db)
    if con is None:
        return None
    try:
        found = con.execute(
            "SELECT u.id FROM review_receipts r JOIN runs u ON u.id = r.run_id WHERE "
            "r.state='confirmed' AND u.seat='reviewer' AND u.repo=? AND u.pr=? AND u.head=? "
            f"AND r.verdict IN ({','.join('?' * len(APPROVED))}) ORDER BY u.created DESC LIMIT 1",
            (repo, number, head, *APPROVED)).fetchone()
    finally:
        con.close()
    return str(found[0]) if found else None
