"""What ``Supervisor.retry`` reads before it re-arms a run (moved out of ``intake``, #2628): the
state lock it holds from its reads to the re-arm, the push-policy or snapshot refusal and the
fixer's fresh admission, and the shadow row a re-armed reviewer turn owes.

Reads the package's names through the package (``run_supervisor.X``), so a test patching
``run_supervisor.X`` still reaches this code, as in ``intake``.
"""
from __future__ import annotations

from contextlib import nullcontext

from .. import run_supervisor


def locks(first) -> tuple:
    """``(st, lock, loop)`` for the run ``retry`` re-arms (``first``: its ledger row, or None).

    A shadow run is judged on the findings snapshot taken when it was queued (#1013), and one
    that has ended may have had it evicted: ``st`` is the loop's state, read for that check, and
    ``lock`` its state lock (state lock, then ledger: the order ``enqueue_shadow`` uses), so a
    put cannot evict the snapshot in between. A reviewer run whose head is sampled owes a shadow
    (#2628): ``loop`` is its loop, and ``lock`` the state lock ``gate.enqueue_isolated`` holds
    from the shadow's snapshot to its row. Otherwise no lock."""
    from .. import config, state as state_mod
    seat = first['seat'] if first is not None else None
    if seat not in (run_supervisor.SHADOW, 'reviewer'):
        return None, nullcontext(), None
    try:
        loop = config.by_repo(first['repo'])
        st = state_mod.state_for(loop) if loop is not None and seat != 'reviewer' else None
    except config.ConfigError:
        loop = st = None     # no loop to read: the snapshot counts as gone, and no shadow owed
    if loop is None:
        return None, nullcontext(), None
    if seat == 'reviewer':
        from ..gate import shadows
        return None, shadows._shadow_snapshot_lock(loop, seat, first['pr'], first['head'],
                                                   first['turn_key'] or ''), loop
    return st, st.locked(), loop


def owed_shadow(loop, first, db) -> tuple[str, float] | None:
    """The ``(delivery, budget)`` of the shadow row a re-armed reviewer run owes, or None (#2628):
    the rule ``gate.enqueue_isolated`` applies to a queued one. The loop has a shadow profile,
    this reviewer turn is sampled and its findings snapshot is kept now (``snapshot_shadow``,
    which never takes one for a shadow row in ledger ``db`` whose own snapshot is gone, #1139).
    Called under ``locks``' state lock, before the re-arm's transaction."""
    if loop is None or first is None or first['seat'] != 'reviewer':
        return None
    from .. import config
    from ..gate import shadows
    number, head, turn_key = first['pr'], first['head'], first['turn_key'] or ''
    if not shadows.snapshot_shadow(loop, number, head, turn_key, db):
        return None
    return (shadows.shadow_delivery(loop, number, head, turn_key),
            config.turn_budget(loop, run_supervisor.SHADOW))


def policy(first, st) -> tuple[int | None, str]:
    """``(admitted, refusal)``: the push admission ``retry`` writes on a fixer run (None for any
    other seat), and why the retry is refused by policy, ``''`` when it is not. Read under the
    push-policy lock for a fixer or issue-fixer run. The caller reports the refusal after its
    write and state checks, which say more about the run."""
    from .. import config
    from .intake import shadow_snapshot_refusal
    seat = first['seat'] if first is not None else None
    if seat == run_supervisor.SHADOW:
        return None, shadow_snapshot_refusal(st, first)
    if seat == 'issue_fixer':
        # An issue fix publishes under the same push policy (and triage and a fix label, #980).
        try:
            loop = config.by_repo(first['repo'])
            reason = (run_supervisor.issue_fix_off_reason(loop) if loop is not None
                      else f"no loop is configured for {first['repo']}")
        except config.ConfigError as exc:
            reason = f"the loop configuration for {first['repo']} is unusable ({exc})"
        return None, (f"refused: {reason}, so an issue fix could not publish; turn it on "
                      "(triage with a fix label, and unattended fixer pushes), then retry"
                      if reason else '')
    if seat != 'fixer':
        return None, ''
    try:
        loop = config.by_repo(first['repo'])
    except config.ConfigError as exc:
        # Not a ValueError: named here so `retry` reports it per run, not a crash.
        return 1, f"refused: the loop configuration for {first['repo']} is unusable ({exc})"
    if loop is None or not config.unattended_fixer_push_enabled(loop):
        command = config.fixer_push_enable_command(loop or "LOOP")
        return 1, (f"refused: unattended fixer pushes are off for {first['repo']}, so a fixer "
                   f"turn could not publish; run `{command}` first, then retry")
    return 1, ''
