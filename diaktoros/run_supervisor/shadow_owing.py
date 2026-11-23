"""A redelivery that finds its reviewer row not ended, and the shadow that turn owes (#2627; #2634
review F4). Moved out of ``intake`` (#1349: the intake is over its line budget).

A worker can claim a pending row, and launch it, between the gate's findings snapshot and the
redelivery's transaction: the gate's ``recover`` may spawn one, and one already running claims
any pending row. So the owed shadow is decided here, inside that transaction, for every row not
ended (``UNENDED``), not only one still pending.
"""
from __future__ import annotations

from .. import run_supervisor

# The reviewer row states a redelivery may still give the shadow its turn owes.
UNENDED = ('pending', 'waiting', 'claimed', 'launching', 'running')


def beside(sup, con, prior, outcome: str, shadow, unkept: str, now: float) -> str:
    """``submit``'s answer for a redelivery that finds ``prior`` not ended (``UNENDED``), with the
    owed ``shadow`` the gate decided before this transaction: ``(delivery, budget)``, None, or,
    as ``unkept``, why it could not be queued (``intake.SHADOW_OWED``). The row is left as it is.

    The shadow row goes in beside it, on the snapshot the gate took, while the row holds no
    write-ahead record: a reviewer's findings are recorded only after its review is posted, and
    that only after its receipt claim commits to the ledger (``review_receipt``,
    ``write_records``), which this transaction would see. With none now, none was there when
    the snapshot was taken, so it holds no finding of this review. A row that may have written
    began its review before this event could decide its shadow, as a turn queued unsampled does
    with no event at all: no snapshot of it could be the review's, so none is used.

    Unkept, the turn is not left to approve without its shadow: a row still to start (pending,
    waiting, claimed) is failed, and a claimed one loses its owner, so its worker never launches
    it; a launching or running one is asked to stop, and its review receipt is refused from now
    on (``ReceiptLedger.claim``). A later event, or ``retry``, re-arms it with its shadow."""
    if shadow is not None and run_supervisor.write_evidence(con, prior['id']) is not None:
        return outcome
    if unkept and prior['state'] in ('launching', 'running'):
        con.execute("UPDATE runs SET cancel_requested=COALESCE(cancel_requested, ?), error=?, "
                    "updated=? WHERE id=?", (now, unkept, now, prior['id']))
        return f"{outcome}: {unkept}"
    if unkept:
        con.execute("UPDATE runs SET state='failed', error=?, owner=NULL, lease=NULL, "
                    "retry_at=NULL, updated=? WHERE id=?", (unkept, now, prior['id']))
        return f"duplicate failed: {unkept}"
    sup._insert_runs(con, [], shadow, (prior['repo'], prior['pr'], prior['head'],
                                       prior['turn_key']), now)
    if prior['state'] == 'waiting' and prior['retry_at']:
        outcome += f" (retry {prior['retries']} due in {max(0, int(prior['retry_at'] - now))}s)"
    return outcome
