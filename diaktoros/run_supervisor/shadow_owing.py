"""A redelivery that finds its reviewer row not ended, and the shadow that turn owes (#2627; #2634
review F4). Moved out of ``intake`` (#1349: the intake is over its line budget).

A worker can claim a pending row, and launch it, between the gate's findings snapshot and the
redelivery's transaction: the gate's ``recover`` may spawn one, and one already running claims
any pending row. So the owed shadow is decided here, inside that transaction, for every row not
ended (``UNENDED``), not only one still pending.
"""
from __future__ import annotations

import contextlib
import time

from .. import run_supervisor
from ..state import ledger

# The reviewer row states a redelivery may still give the shadow its turn owes.
UNENDED = ('pending', 'waiting', 'claimed', 'launching', 'running')
# The waits between tries of the barrier lift that ends a redelivery (``lift``; #2644 review F2).
LIFT_WAITS = (0.05, 0.2, 0.75)


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

    A row already asked to stop posts no review (``ReceiptLedger.claim``), and its stop keeps the
    cause it has (#2634 review F5): an operator's is never turned into one an event re-arms.

    Unkept, or a shadow row that could not be written (review F4), the turn is not left to
    approve without its shadow: ``stop``."""
    if prior['cancel_requested'] is not None:
        held = run_supervisor.intake.shadow_owed_stop(prior['error'])
        return f"{outcome}: {prior['error']}" if held else outcome
    if shadow is not None and run_supervisor.write_evidence(con, prior['id']) is not None:
        return outcome
    if not unkept:
        # A savepoint, so a shadow row that cannot be written stops the turn in this same
        # transaction: no worker can claim the review's receipt in between.
        con.execute("SAVEPOINT owed_shadow")
        try:
            sup._insert_runs(con, [], shadow, (prior['repo'], prior['pr'], prior['head'],
                                               prior['turn_key']), now)
        except Exception as exc:
            con.execute("ROLLBACK TO owed_shadow")
            unkept = unwritten(exc)
        con.execute("RELEASE owed_shadow")
    if unkept:
        return stop(con, prior, outcome, unkept, now)
    if prior['state'] == 'waiting' and prior['retry_at']:
        outcome += f" (retry {prior['retries']} due in {max(0, int(prior['retry_at'] - now))}s)"
    return outcome


def unwritten(exc: BaseException) -> str:
    return (f"{run_supervisor.intake.SHADOW_OWED}: its shadow row could not be written "
            f"({type(exc).__name__}: {exc})")


def stop(con, prior, outcome: str, unkept: str, now: float) -> str:
    """Keep a reviewer turn that owes a shadow it cannot have from approving, in ``con``'s
    transaction: a row still to start (pending, waiting, claimed) is failed, and a claimed one
    loses its owner, so its worker never launches it; a launching or running one is asked to
    stop, and its review receipt is refused from now on (``ReceiptLedger.claim``). It ends
    ``cancelled`` with this reason, which a later event, or ``retry``, re-arms with its shadow.
    The outcome names ``SHADOW_OWED``, so the gate holds the event."""
    if prior['state'] in run_supervisor.CANCEL_BY_WORKER:
        con.execute("UPDATE runs SET cancel_requested=?, error=?, updated=? WHERE id=? AND "
                    "cancel_requested IS NULL", (now, unkept, now, prior['id']))
        return f"{outcome}: {unkept}"
    con.execute("UPDATE runs SET state='failed', error=?, owner=NULL, lease=NULL, "
                "retry_at=NULL, updated=? WHERE id=?", (unkept, now, prior['id']))
    return f"duplicate failed: {unkept}"


def claim_lock(db, seat: str):
    """Around ``submit``'s transaction for a reviewer's event: the ledger's claim lock
    (``ledger.claim_lock``), so no receipt claim lands between a failed transaction and the stop
    ``fails_closed`` then writes (#2634 review F4, round 5). Taken before ``BEGIN IMMEDIATE``,
    as the claim takes it, so the two never wait on each other's locks."""
    return ledger.claim_lock(db) if seat == "reviewer" else contextlib.nullcontext()


@contextlib.contextmanager
def fails_closed(sup, con, prior, shadow, held=None):
    """Around ``submit``'s redelivery and its COMMIT: a failure that ends the transaction itself
    (its COMMIT, or one SQLite rolls back whole) loses the shadow row with it, but not a worker
    already running the turn. So it is stopped in a fresh transaction, then the error is raised
    and the gate holds the event (#2634 review F4). ``submit`` holds the claim lock (``claim_lock``)
    throughout, so the turn's receipt cannot be claimed in between (round 5). Taken before
    anything is written: a row not ended, not asked to stop, with no write-ahead record, owing
    a shadow (unkept, a ``str``, too: round 7).

    The receipt barrier beside the lock (``held``, ``ledger.owe``) is written ahead, before the
    redelivery writes anything, and cleared only once its transaction or the stop commits, so
    the receipt stays refused until the turn's shadow row is in (round 6) if neither commits,
    or if the process dies before either does (round 8). A barrier that cannot be written then
    is tried again after a failure; if the barrier, the shadow row and the stop all refuse every
    write, nothing durable is left to hold, and the error says so. A barrier add cut short
    holds every id it begins (``ledger._owing``) until this writer lifts it by the offset it
    cut at (``mine``, ``lift``; #2643, #2645): never another writer's cut."""
    owed = (prior is not None and shadow is not None and prior['state'] in UNENDED
            and prior['cancel_requested'] is None
            and run_supervisor.write_evidence(con, prior['id']) is None)
    barred, mine = None, []
    if owed:
        try:
            ledger.owe(held, prior['id'])
        except Exception as exc:
            barred = exc
            mine += cuts(exc)
    try:
        yield
    except Exception as exc:
        if not owed:
            raise
        with contextlib.suppress(Exception):
            con.execute("ROLLBACK")
        if barred is not None:
            try:
                ledger.owe(held, prior['id'], mine=mine)
                barred, mine[:] = None, []
            except Exception as again:
                mine += cuts(again)
                exc.add_note(f"its receipt barrier could not be written either: {barred!r}, "
                             f"then {again!r}")
        with contextlib.suppress(Exception), sup._connect() as fresh:
            fresh.execute("BEGIN IMMEDIATE")
            row = fresh.execute("SELECT * FROM runs WHERE id=?", (prior['id'],)).fetchone()
            if (row is not None and row['state'] in UNENDED and row['cancel_requested'] is None
                    and not fresh.execute("SELECT 1 FROM runs WHERE repo=? AND pr=? AND head=? "
                                          "AND seat=? AND turn_key=?", (row['repo'], row['pr'],
                                          row['head'], run_supervisor.SHADOW,
                                          row['turn_key'])).fetchone()):
                stop(fresh, row, '', shadow if isinstance(shadow, str) else unwritten(exc),
                     time.time())
            fresh.execute("COMMIT")
            lift(held, prior['id'], mine)           # the stop stands now and refuses the receipt
        raise
    if owed:
        with contextlib.suppress(Exception):    # its shadow row, or its stop, is in now
            lift(held, prior['id'], mine)


def cuts(exc: BaseException) -> list[int]:
    """Where a barrier add that raised ``exc`` cut its own line, if it wrote any of it."""
    return [exc.at] if isinstance(exc, ledger.BarrierCut) else []


def lift(held, run_id: str, mine) -> None:
    """Clear ``run_id`` from the barrier once its shadow row or its stop is in, lifting the cut
    adds this writer made (``mine``, their offsets) first (#2643, #2645). Only this writer can
    lift them, so a write that fails is tried again (``LIFT_WAITS``) while it still holds the
    lock and ``mine`` (#2644 review F2). If it never lands, the last error is raised and they
    stay, failing closed as a dead writer's cut does: nothing records them by offset, as the log
    is emptied once it holds nothing and a later cut may start at the same byte (review F4)."""
    for wait in (*LIFT_WAITS, None):
        try:
            return ledger.owe(held, run_id, False, mine=mine)
        except OSError:
            if wait is None:
                raise
            time.sleep(wait)
