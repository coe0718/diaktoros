"""Recovery sweep for issue triage whose webhook delivery never arrived (#849).

The Hermes webhook gateway answers HTTP 429 to the deliveries over its ``rate_limit`` per route per
minute (30 unless set) and GitHub does not redeliver. A burst of ``issues.opened`` events (55 issues
filed in a minute) therefore loses triage turns silently. The watchdog calls :func:`sweep`: it lists
the open issues and queues the triage turn of each one the gate would have queued, through the same
``gate.enqueue_isolated`` call ``gate_triage.py`` makes, so the ledger's unique key still stops a
second turn. A
noted issue whose triage run later ``succeeded`` is dropped from the ``late-triage.json`` list
that ``status`` prints; a failed, cancelled or still-active run stays noted. That cleanup runs
first in every sweep, so it also happens when triage is turned off and when the open-issues read
fails (#1136); it still needs the private runtime's run ledger.

An issue is queued when ``run_supervisor.triage_blocker`` has nothing against it (open, not a pull
request, author in ``triage.authors``, no label from ``triage.labels``), it has no triage row in the
run ledger in any state, it is no newer than ``GRACE_S`` (the gate may still be on its way) and no
older than ``HORIZON_S`` (a backlog from before the loop was armed is not triaged in bulk). An
issue a maintainer already sent to the fixer (``triage.fix_label``) is left alone as well. At most
``PER_SWEEP`` are queued per sweep, oldest first; the rest wait for the next one. The listing is newest first and bounded
(``gh.MAX_ISSUE_PAGES`` pages, pull requests included): past the bound the sweep works on the
newest issues it read and returns an alert line saying the older ones were not checked (#1037).
"""
from __future__ import annotations

import time

from . import config, gate, gh, run_supervisor
from .util import epoch, log

GRACE_S = 300.0
HORIZON_S = 3 * 86400.0
PER_SWEEP = 10


def candidates(loop: dict, issues: list, queued: set[int], now: float) -> list[int]:
    """The issue numbers, oldest first, whose triage was missed (pure: no GitHub, no ledger)."""
    fix_label = str((loop.get("triage") or {}).get("fix_label") or "").casefold()
    found = []
    for issue in issues:
        number = issue.get("number") if isinstance(issue, dict) else None
        if type(number) is not int or number < 1 or number in queued:
            continue
        if run_supervisor.triage_blocker(loop, issue):
            continue
        if fix_label and fix_label in {str((label or {}).get("name") or "").casefold()
                                       for label in issue.get("labels") or []
                                       if isinstance(label, dict)}:
            continue                      # a maintainer already handed it to the fixer
        created = epoch(issue.get("created_at"))
        if not created or now - created < GRACE_S or now - created > HORIZON_S:
            continue
        found.append(number)
    return sorted(found)


def resolve_late(loop: dict, st) -> None:
    """Drop the late-triage entries whose triage run has since succeeded (`status` reads them).

    It needs only the ledger and ``late-triage.json``, so :func:`sweep` runs it first: before the
    triage-enabled check (a loop that turned triage off still settles what it noted) and before
    the open-issues read (a GitHub read error does not delay it).
    """
    try:
        noted = st.late_triage().keys()
        if noted:
            st.late_triage_resolve(noted & gate.isolated_supervisor(loop).seat_numbers(
                loop["repo"], "triage", "succeeded"))
    except Exception as exc:
        log(f"triage sweep: late-triage cleanup failed: {type(exc).__name__}: {exc}")


def sweep(loop: dict, st, now: float | None = None) -> list[str]:
    """Queue the triage turn of each issue whose delivery was missed; return the alert lines."""
    resolve_late(loop, st)
    if not config.triage_enabled(loop):
        return []
    now = time.time() if now is None else now
    issues, error = gh.open_issues_read(
        loop, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - HORIZON_S)))
    if issues is None:
        log(f"triage sweep: open issues unreadable: {gh.one_line(error)}")
        return []
    lines: list[str] = []
    if error:                             # past the page bound: the newest were read, older not
        log(f"triage sweep: open issues incomplete: {gh.one_line(error)}")
        lines.append(f"⚠️ Diaktoros [{loop['id']}] triage recovery sweep read only the newest "
                     f"{len(issues)} open issues/PRs ({gh.one_line(error)}); older issues "
                     "inside the sweep window were not checked")
    try:
        queued = gate.isolated_supervisor(loop).seat_numbers(loop["repo"], "triage")
    except Exception as exc:              # no private runtime / ledger: the gate cannot enqueue either
        log(f"triage sweep: run ledger unavailable: {type(exc).__name__}: {exc}")
        return lines
    for number in candidates(loop, issues, queued, now)[:PER_SWEEP]:
        try:
            outcome = gate.enqueue_isolated(loop, "triage", number, config.TRIAGE_HEAD,
                                            turn_key="triage")
        except Exception as exc:
            lines.append(f"⚠️ Diaktoros [{loop['id']}] issue #{number} triage not queued: "
                         f"isolated worker unavailable: {type(exc).__name__}: {exc} "
                         "— retrying next sweep")
            break                         # the worker is the same for every issue
        log(f"issue #{number} triage {outcome} (recovery sweep)")
        if outcome in ("enqueued", "rearmed", "pending"):
            st.late_triage_note(number, now, outcome)
            lines.append(f"Diaktoros [{loop['id']}] issue #{number} triage queued late: the "
                         "webhook delivery was missed")
    return lines
