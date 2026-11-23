"""What ``Supervisor.retry`` reads before it re-arms a run (moved out of ``intake``, #2628): the
state lock it holds from its reads to the re-arm, the push-policy or snapshot refusal and the
fixer's fresh admission, every other refusal, and the shadow row a re-armed reviewer turn owes.

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


def refusal(con, run_id: str, policy_off: str, budget) -> str:
    """Why ``retry`` refuses this run, ``''`` when it re-arms it; read in its transaction, before
    anything is written (a shadow's findings snapshot included, #2634 review F2).

    Anything that may have written — uncertain, quarantined, reconciled, or with any write-ahead
    record — is refused with the reconcile instructions; so is a state that is not failed,
    waiting or cancelled, and a cancellation that was neither an operator's nor the push
    policy's (superseded: a new head gets its own turn). ``policy_off`` comes after those, which
    say more about the run."""
    row = con.execute('SELECT state,error FROM runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        return f'no run {run_id}'
    wrote = run_supervisor.write_evidence(con, run_id)
    if wrote is not None:
        return (f'refused: {wrote}. A run that may have written is never replayed. Inspect the PR '
                'for its external writes, establish no worker remains, then `python -m '
                f'diaktoros.run_supervisor reconcile DB {run_id} --reason \'external writes '
                "inspected' --acknowledge-no-live-worker`; a new head gets a fresh turn.")
    if row['state'] not in run_supervisor.REARMABLE + ('waiting',):
        return f"refused: run is {row['state']}, not failed, waiting or cancelled"
    if row['state'] == 'cancelled' and not (run_supervisor.policy_cancelled(row['error'])
                                            or run_supervisor.operator_cancelled(row['error'])):
        # Re-arming it would only be cancelled again by the claim (the line runs_view draws).
        return (f"refused: cancelled because {row['error'] or 'superseded'} — a new head gets "
                "its own turn; nothing to retry")
    if policy_off:
        return policy_off
    if budget is not None and (isinstance(budget, bool) or not budget > 0):
        return 'positive turn budget required'
    return ''


def owed_shadow(sup, con, loop, first):
    """The shadow row a re-armed reviewer run owes (#2628): ``(delivery, budget)``, its findings
    snapshot kept now; None when it owes none; or why ``retry`` is refused, when it may owe one
    that cannot be queued (#2634 review F1: unknown is never "none owed"). Called in ``retry``'s
    transaction once ``refusal`` passed, so a refused retry keeps no snapshot (F2).

    None: not a reviewer run, no loop configured, no shadow profile, not sampled, or a shadow row
    already at its identity, left as it is (#1139: never a new snapshot for it). Refused: a loop
    configuration that cannot be read, a shadow seat or budget that cannot take the row (checked
    before the snapshot), or a snapshot that cannot be kept (``gate.shadow_owed``)."""
    if first is None or first['seat'] != 'reviewer':
        return None
    from .. import config
    from ..gate import shadow_owed, shadows
    repo, number, head, key = first['repo'], first['pr'], first['head'], first['turn_key'] or ''
    if loop is None:
        try:
            config.by_repo(repo)
        except config.ConfigError as exc:
            return (f"refused: the loop configuration for {repo} is unusable ({exc}), so whether "
                    "this reviewer turn owes a shadow review is unknown; fix it, then retry")
        return None
    if not config.shadow_profile(loop) or not shadows.shadow_sampled(loop, number, head, key):
        return None
    if con.execute("SELECT 1 FROM runs WHERE repo=? AND pr=? AND head=? AND seat=? AND "
                   "turn_key=?", (repo, number, head, run_supervisor.SHADOW, key)).fetchone():
        return None
    if run_supervisor.SHADOW not in sup.capacity or not config.turn_budget(
            loop, run_supervisor.SHADOW) > 0:
        return ("refused: this reviewer turn owes a shadow review, and a shadow row needs the "
                "shadow seat and a positive budget")
    owed = shadow_owed.owed(loop, 'reviewer', number, head, key, sup.db)
    if isinstance(owed, str):
        return (f"refused: this reviewer turn owes a shadow review, but {owed}; nothing was "
                "re-armed, retry once it can be kept")
    return owed


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
