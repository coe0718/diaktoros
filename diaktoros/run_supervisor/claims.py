"""Which pending run goes next, and a running run's seat claim (#1349, split from the core).

The claim order (#481): ``claim_class`` and ``claim_rank`` sort pending runs (finish work before
starting more, aged up so nothing starves); the worker's claim and the queue view share them.
``claim_seat`` / ``release_seat`` write a run's seat into the loop's ``locks.json`` and its head's
in-flight mark for the life of the run (#98), best effort: the run ledger enforces capacity.
The core's names are reached through the package (``run_supervisor.X``); callers use
``claims.<name>``.
"""
from __future__ import annotations

import time

from .. import run_supervisor


# Claim order (#481): finish work before starting more. Lower class goes first; a pending row
# climbs one class per CLAIM_AGE_STEP_S waited, so the lowest class runs within
# (classes - 1) * CLAIM_AGE_STEP_S of arrival even while higher classes keep arriving.
CLAIM_CLASSES = ('CI-fix/conflict turn', 'fix round', 'review', 'new issue fix')


CLAIM_AGE_STEP_S = 900.0


def claim_class(row) -> int:
    """The priority class (index into ``CLAIM_CLASSES``) of a run row."""
    from .. import ci_fix
    seat, key = row['seat'], str(row['turn_key'] or '')
    if seat == 'fixer' and (key.startswith(run_supervisor.CONFLICT_KEY) or ci_fix.is_ci_fix(key)):
        return 0
    if seat in ('fixer', 'adjudicator'):
        return 1
    return 3 if seat == 'issue_fixer' else 2


def claim_rank(row, now: float) -> tuple:
    """Sort key for a pending row: aged class, then arrival. The one definition the worker's
    claim and the status/explain queue view share."""
    aged = int(max(0.0, now - float(row['created'])) // CLAIM_AGE_STEP_S)
    return (max(0, claim_class(row) - aged), row['created'], row['id'])


def claim_order(rows, now: float | None = None) -> list:
    now = time.time() if now is None else now
    return sorted(rows, key=lambda r: claim_rank(r, now))


def claim_why(row, now: float) -> str:
    cls, eff = claim_class(row), claim_rank(row, now)[0]
    text = CLAIM_CLASSES[cls]
    if eff < cls:
        text += f", aged up to {CLAIM_CLASSES[eff]} priority after {int(now - float(row['created']))}s"
    return text


# Seats whose turns a held PR (#477) waits out; an issue fix's number is an issue, not a PR.
PR_HOLD_SEATS = ('reviewer', 'fixer', 'adjudicator')


INFLIGHT_LABEL = {'reviewer': 'review', 'fixer': 'fix'}


def claim_seat(loop: dict, row, budget: float):
    """Claim ``row``'s seat in the loop's ``locks.json`` and mark its head in flight, for the
    life of the run (#98). Best effort: the run ledger is what enforces capacity; these are
    what ``explain``, ``status``, the queue drain and the watchdog's clocks read. Returns what
    ``release_seat`` needs, or None when nothing was written."""
    try:
        from .. import gate, state as state_mod
        st = state_mod.state_for(loop)
        key = gate.seat_key(loop, row['pr'])
        # Exclusive (#727): it stamps the gate's run-less claim, but never evicts another live
        # run's. Then that run keeps the entry, this one has nothing of its own to release, and
        # the run ledger is what refuses the duplicate.
        if not st.acquire(row['seat'], key, row['head'], f"isolated run {row['id']}",
                          budget=budget, run=row['id'], exclusive=True):
            return None
    except Exception:
        return None
    # The claim is written: from here on its release must stay reachable, whatever the mark
    # does (a failed mark write must not orphan the claim).
    mark = (f"{INFLIGHT_LABEL[row['seat']]}:{row['pr']}:{row['head']}"
            if row['seat'] in INFLIGHT_LABEL else None)
    if mark:
        try:
            st.inflight(mark, record=True)
        except Exception:
            mark = None
    return st, row['seat'], key, row['head'], mark, row['id']


def release_seat(claim, state: str | None) -> None:
    """Free a run's claim and in-flight mark once it has ended — unless it ended ``uncertain``
    (or its end could not be recorded): then the claim stays as the seat's visible occupancy
    until an operator reconciles it, with its TTL and the "that run died" report as backstop."""
    if claim is None or state in (None, 'uncertain'):
        return
    st, seat, key, head, mark, run = claim
    try:
        # Only this run's own claim; its mark goes with it. A newer run that has claimed the
        # same seat/PR/head (a retry, a re-armed turn) keeps both (#98).
        if st.release_if(seat, key, head, run=run) and mark:
            st.inflight_clear(mark)
    except Exception:
        pass
