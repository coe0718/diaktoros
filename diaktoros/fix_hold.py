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
    st.fix_hold_set(number, pr)
    if not st.fix_hold_said(number):
        if not _wait_comment(loop, number, pr):
            return                                # POST failed: not "said"; a later sweep retries
        st.fix_hold_say(number)
        _notice(loop, number, "held", pr,
                f"#{number} waits for PR #{pr} to merge: its finding is about code only there")


def _wait_comment(loop: dict, number: int, pr: int) -> bool:
    """Post the wait comment; ``False`` when the POST failed (``gh.api`` returned ``None``)."""
    login = config.triage_login(loop)
    if not login:
        return True                               # nobody to say it as: retrying cannot help
    return gh.api(loop, f"/repos/{loop['repo']}/issues/{number}/comments", method="POST",
                  body={"body": f"waiting for #{pr} to merge — this finding is about code only on "
                                "that branch"}, login=login) is not None


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
        if state is None:
            continue                              # unreadable: retry on the next sweep
        if state == "open":
            hold(loop, st, number, pr)            # says so once, if the gate could not
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


AUTO_FIX_SEAT = "auto_fix"      # pacing counter key for automatic hand-offs (#232)


def closing_pr(loop: dict, number: int) -> int | None:
    """The number of an open PR whose body says it fixes issue ``number``, else ``None`` (#308).

    Raises ``RuntimeError`` when the open PR listing is unknown.
    """
    import re
    prs, error = gh.open_prs_read(loop)
    if prs is None:
        raise RuntimeError(f"open PR listing unreadable: {error}")
    repo = re.escape(str(loop["repo"]))
    pattern = re.compile(
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
        rf"(?:(?:{repo})?#|https://github\.com/{repo}/issues/){number}(?!\d)", re.IGNORECASE)
    for pr in prs:
        if type(pr.get("number")) is int and pattern.search(str(pr.get("body") or "")):
            return pr["number"]
    return None


def auto_offer(loop: dict, number: int) -> str:
    """Host-side hand-off of a triaged issue to the fixer (#232); returns what happened.

    Called after the triage write is recorded; the triage seat never applies ``fix_label``. The
    same guards as a maintainer's label: the live issue must carry an ``auto_fix_labels`` label
    and no P0/P1/P2, no open PR may already fix it, a finding from an unmerged PR is held, and
    the lineage depth and the daily cap bound it. Best effort: never raises.
    """
    from . import pacing, run_supervisor, state as state_mod
    try:
        if not config.auto_fix_labels(loop):
            return ""
        try:
            run_supervisor.issue_fix_issue(loop, number)   # open, allowlisted, eligible labels
        except Exception as exc:
            return f"not auto-offered: {exc}"
        depth = gate.isolated_supervisor(loop).filed_depth(loop["repo"], number)
        if depth is not None and depth > config.AUTO_FIX_MAX_DEPTH:
            return f"not auto-offered: lineage depth {depth} needs a person"
        existing = closing_pr(loop, number)
        if existing is not None:
            return f"not auto-offered: open PR #{existing} already fixes it"
        origin = origin_pr(loop, number)
        st = state_mod.state_for(loop)
        if origin is not None:
            origin_state = pr_state(loop, origin)
            if origin_state is None:
                st.fix_hold_set(number, origin)
                return f"not auto-offered: PR #{origin} unreadable; held"
            if origin_state == "open":
                hold(loop, st, number, origin)
                return f"held until PR #{origin} merges"
            if origin_state == "closed":
                return f"not auto-offered: PR #{origin} closed unmerged"
            st.fix_hold_drop(number)
        cap = config.auto_fix_daily(loop)
        if pacing.turns_today(loop["id"], AUTO_FIX_SEAT) >= cap:
            return f"not auto-offered: daily cap ({cap}) reached"
        base, outcome = queue_fix(loop, number)
        if outcome in ("enqueued", "rearmed", "pending"):
            pacing.count_turn(loop["id"], AUTO_FIX_SEAT)
            _notice(loop, number, "fixing", 0,
                    f"auto-offered; fix turn queued from {loop['base']} at {base[:7]}")
        return f"auto-offered at {base[:7]}: {outcome}"
    except Exception as exc:
        return f"not auto-offered: {type(exc).__name__}: {exc}"
