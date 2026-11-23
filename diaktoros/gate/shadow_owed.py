"""The shadow a reviewer turn owes when it is queued, redelivered or re-armed (#1682, #2627,
#2628), told apart from a turn that owes none (#2634 review F1).

``shadows.snapshot_shadow`` answers False both for a turn that owes no shadow and for one whose
findings snapshot could not be kept. A caller that read the second as the first would arm a
reviewer with no shadow row, and ``shadow.escalate`` would then find nothing to wait for.
"""
from __future__ import annotations

from .. import config
from . import shadows

# Why an owed shadow could not be queued: its findings snapshot was not kept (#1013, #1647).
UNKEPT = "the findings snapshot its shadow review is judged on could not be kept"


def owed(loop: dict, seat: str, number: int, head: str, turn_key: str = "", db=None):
    """What this turn owes, its findings snapshot taken now when it owes a shadow.

    * None: nothing. Not a reviewer turn, no shadow profile, not sampled, or its shadow row is
      already in ledger ``db`` with its own snapshot gone, which keeps #1139's treatment (no
      new snapshot; ``enqueue_shadow`` and ``retry`` refuse that row).
    * ``(delivery, budget)``: the shadow row to write, its snapshot kept.
    * A ``str``: owed, but its snapshot could not be kept (a state write failed, or the ledger
      could not be read). Never "nothing": the caller fails closed on it.
    """
    if seat != "reviewer" or not config.shadow_profile(loop) or not shadows.shadow_sampled(
            loop, number, head, turn_key):
        return None
    if shadows.snapshot_shadow(loop, number, head, turn_key, db):
        return (shadows.shadow_delivery(loop, number, head, turn_key),
                config.turn_budget(loop, "shadow"))
    try:
        if shadows.shadow_row_exists(loop["repo"], number, head, turn_key, db):
            return None
    except Exception as exc:  # noqa: BLE001 - unknown is not "none owed"
        return f"{UNKEPT} (the run ledger could not be read: {type(exc).__name__})"
    return UNKEPT
