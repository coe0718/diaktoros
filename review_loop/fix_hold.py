"""Hold an issue fix until the PR its finding came from is merged (#324).

An issue the reviewer filed (``filed_issues``, #247) is about code that may exist only in the PR
it was filed from. A fix turn starts from the base branch, so while that PR is open it would find
nothing. ``gate_triage`` holds such a hand-off here; the watchdog sweep releases it once the PR
merges, or drops it (with one comment) when the PR closes unmerged. An issue a person opened has
no ``filed_issues`` row and is never held.
"""

from __future__ import annotations

from . import config, gate, gh
from .util import log


def origin_pr(loop: dict, number: int) -> int | None:
    """The PR a reviewer-filed issue came from, else ``None`` (a person's issue)."""
    supervisor = gate.isolated_supervisor(loop)
    return supervisor.filed_origin_pr(loop["repo"], number)


def pr_state(loop: dict, pr: int) -> str | None:
    """``open``, ``merged`` or ``closed``; ``None`` when GitHub could not be read."""
    data = gh.api(loop, f"/repos/{loop['repo']}/pulls/{pr}", login=loop["read_token"])
    if not isinstance(data, dict) or data.get("number") != pr:
        return None
    if data.get("state") == "open":
        return "open"
    return "merged" if data.get("merged") or data.get("merged_at") else "closed"


def _notice(loop: dict, number: int, event: str, pr: int, outcome: str) -> None:
    from . import observer, state as state_mod
    observer.notify(loop, state_mod.state_for(loop), event, number, "", identity=f"pr{pr}",
                    outcome=outcome, issue=True)


def queue_fix(loop: dict, number: int) -> tuple[str, str]:
    """Queue the fix turn from the base branch's current commit: ``(base, outcome)``.

    Raises ``RuntimeError`` when the base is unreadable, or whatever the enqueue raises.
    """
    ref = gh.api(loop, f"/repos/{loop['repo']}/git/ref/heads/{loop['base']}",
                 login=loop["read_token"])
    base = ((ref or {}).get("object") or {}).get("sha") if isinstance(ref, dict) else None
    if not isinstance(base, str) or len(base) != 40:
        raise RuntimeError(f"base branch {loop['base']} unreadable (GitHub read failed)")
    try:
        return base, gate.enqueue_isolated(loop, "issue_fixer", number, base, turn_key="issue-fix")
    except Exception as exc:
        exc.fix_base = base  # the caller's notice needs the base the enqueue failed at
        raise


def hold(loop: dict, st, number: int, pr: int) -> None:
    """Record the held hand-off and say so once."""
    fresh = not st.fix_hold_get(number)
    st.fix_hold_set(number, pr)
    if fresh:
        _notice(loop, number, "held", pr,
                f"#{number} waits for PR #{pr} to merge: its finding is about code only there")


def _drop_comment(loop: dict, number: int, pr: int) -> None:
    from . import config as cfg
    login = cfg.triage_login(loop)
    if login:
        gh.api(loop, f"/repos/{loop['repo']}/issues/{number}/comments", method="POST",
               body={"body": f"PR #{pr} closed without merging, so the finding in this issue is "
                             "moot; not handing it to the fixer. The label stays for a person "
                             "to decide."}, login=login)


def sweep(loop: dict, st) -> list[str]:
    """Re-check held hand-offs; queue the fix of those whose PR merged. Best effort."""
    lines: list[str] = []
    if not config.issue_fixes_enabled(loop):
        return lines
    from . import run_supervisor
    for number, pr in sorted(st.fix_holds().items()):
        state = pr_state(loop, pr)
        if state is None or state == "open":
            continue
        if state == "closed":
            _drop_comment(loop, number, pr)
            st.fix_hold_drop(number)
            log(f"issue #{number}: PR #{pr} closed unmerged; hand-off dropped")
            continue
        try:
            run_supervisor.issue_fix_issue(loop, number)
        except run_supervisor.RetryableError:
            continue
        except Exception as exc:                  # closed, unlabelled, ...: nothing to fix now
            st.fix_hold_drop(number)
            log(f"issue #{number}: held hand-off dropped: {exc}")
            continue
        try:
            base, outcome = queue_fix(loop, number)
        except Exception as exc:
            lines.append(f"⚠️ issue #{number}: held fix not queued: {type(exc).__name__}: {exc}")
            continue
        st.fix_hold_drop(number)
        log(f"issue #{number} released at {base[:7]}: {outcome}")
        if outcome in ("enqueued", "rearmed", "pending"):
            _notice(loop, number, "fixing", pr,
                    f"PR #{pr} merged; fix turn queued from {loop['base']} at {base[:7]}")
    return lines
