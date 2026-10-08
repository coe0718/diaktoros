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
            live = run_supervisor.issue_fix_issue(loop, number)
        except run_supervisor.RetryableError:
            continue
        except Exception as exc:                  # closed, unlabelled, ...: nothing to fix now
            st.fix_hold_drop(number)
            log(f"issue #{number}: held hand-off dropped: {exc}")
            continue
        present = {str((x or {}).get("name") or "").casefold()
                   for x in live.get("labels") or [] if isinstance(x, dict)}
        auto = loop["triage"]["fix_label"].casefold() not in present   # no maintainer's label
        if auto and _capped(loop):
            continue                              # the day's cap is spent: held till tomorrow
        try:
            base, outcome = queue_fix(loop, number)
        except Exception as exc:
            lines.append(f"⚠️ issue #{number}: held fix not queued: {type(exc).__name__}: {exc}")
            continue
        st.fix_hold_drop(number)
        if auto and outcome in ("enqueued", "rearmed", "pending"):
            from . import pacing
            pacing.count_turn(loop["id"], AUTO_FIX_SEAT)
        log(f"issue #{number} released at {base[:7]}: {outcome}")
        if outcome in ("enqueued", "rearmed", "pending"):
            _notice(loop, number, "fixing", pr,
                    f"PR #{pr} merged; fix turn queued from {loop['base']} at {base[:7]}")
    return lines


AUTO_FIX_SEAT = "auto_fix"      # pacing counter key for automatic hand-offs (#232)


def _capped(loop: dict) -> bool:
    from . import pacing
    return pacing.turns_today(loop["id"], AUTO_FIX_SEAT) >= config.auto_fix_daily(loop)


def _closing(loop: dict, prs: list[dict], number: int) -> int | None:
    """The open PR whose body says it fixes issue ``number`` (#308), else ``None``."""
    import re
    repo = re.escape(str(loop["repo"]))
    pattern = re.compile(
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+"
        rf"(?:(?:{repo})?#|https://github\.com/{repo}/issues/){number}(?!\d)", re.IGNORECASE)
    for pr in prs:
        if type(pr.get("number")) is int and pattern.search(str(pr.get("body") or "")):
            return pr["number"]
    return None


def named_open_pr(loop: dict, prs: list[dict], body: str) -> int | None:
    """The first open PR of this repo that an issue body names (``#N`` or a PR URL), else ``None``.

    A merged or closed PR, or an issue, is not in the open listing: never held.
    """
    import re
    repo = re.escape(str(loop["repo"]))
    open_numbers = {pr["number"] for pr in prs if type(pr.get("number")) is int}
    pattern = re.compile(rf"(?<![\w/])(?:{repo})?#(\d+)(?!\d)|"
                         rf"https://github\.com/{repo}/pull/(\d+)(?!\d)", re.IGNORECASE)
    for match in pattern.finditer(str(body or "")):
        number = int(match.group(1) or match.group(2))
        if number in open_numbers:
            return number
    return None


def _skip_comment(loop: dict, number: int, pr: int) -> None:
    """Say once, as the triage login, why the issue is not handed to the fixer (best effort)."""
    login = config.triage_login(loop)
    if login:
        gh.api(loop, f"/repos/{loop['repo']}/issues/{number}/comments", method="POST",
               body={"body": f"PR #{pr} already fixes this; not handing it to the fixer."},
               login=login)


def hand_off(loop: dict, number: int, issue: dict, auto: bool) -> tuple[str, str, str, str]:
    """The steps both triggers share: the maintainer's label (#214) and the auto-offer (#232).

    The caller has checked who asked and that the live issue is eligible. Returns
    ``(kind, text, base, outcome)``; ``kind`` is ``skipped`` (an open PR already fixes it),
    ``dropped`` (the PR it waits on closed unmerged), ``held`` (waits for an open PR),
    ``unreadable`` (that PR is unreadable; the sweep retries), ``capped`` (``auto`` and the
    daily cap is spent) or ``queued``. Raises when GitHub state is unreadable or the enqueue
    fails (the exception then carries ``fix_base``).
    """
    from . import pacing, state as state_mod
    prs, error = gh.open_prs_read(loop)
    if prs is None:
        raise RuntimeError(f"open PR listing unreadable: {error}")
    existing = _closing(loop, prs, number)
    if existing is not None:
        _skip_comment(loop, number, existing)
        return "skipped", f"open PR #{existing} already fixes it", "", ""
    try:
        origin = origin_pr(loop, number)             # a seat's finding (#324) ...
    except Exception as exc:
        raise RuntimeError(f"lineage unreadable: {exc}") from exc
    if origin is None:                               # ... or a person's issue naming an open PR
        origin = named_open_pr(loop, prs, str((issue or {}).get("body") or ""))
    st = state_mod.state_for(loop)
    if origin is not None:
        origin_state = pr_state(loop, origin)
        if origin_state is None:
            st.fix_hold_set(number, origin)          # the sweep retries
            return "unreadable", f"PR #{origin} unreadable; held", "", ""
        if origin_state == "open":
            hold(loop, st, number, origin)
            return "held", f"held until PR #{origin} merges", "", ""
        if origin_state == "closed":
            _drop_comment(loop, number, origin)
            st.fix_hold_drop(number)
            return "dropped", f"PR #{origin} closed unmerged; finding is moot", "", ""
        st.fix_hold_drop(number)
    if auto and _capped(loop):
        return "capped", f"daily cap ({config.auto_fix_daily(loop)}) reached", "", ""
    base, outcome = queue_fix(loop, number)
    if auto and outcome in ("enqueued", "rearmed", "pending"):
        pacing.count_turn(loop["id"], AUTO_FIX_SEAT)
    return "queued", "", base, outcome


def auto_offer(loop: dict, number: int) -> str:
    """Host-side hand-off of a triaged issue to the fixer (#232); returns what happened.

    Called after the triage write is recorded; the triage seat never applies ``fix_label``. The
    same guards as a maintainer's label: the live issue must carry an ``auto_fix_labels`` label
    and no P0/P1/P2, no open PR may already fix it, a finding from an unmerged PR is held, and
    the lineage depth and the daily cap bound it. Best effort: never raises.
    """
    from . import run_supervisor
    try:
        if not config.auto_fix_labels(loop):
            return ""
        try:
            live = run_supervisor.issue_fix_issue(loop, number)   # open, allowlisted, eligible
        except Exception as exc:
            return f"not auto-offered: {exc}"
        depth = gate.isolated_supervisor(loop).filed_depth(loop["repo"], number)
        if depth is not None and depth > config.AUTO_FIX_MAX_DEPTH:
            return f"not auto-offered: lineage depth {depth} needs a person"
        kind, text, base, outcome = hand_off(loop, number, live, auto=True)
        if kind == "held":
            return text
        if kind != "queued":
            return f"not auto-offered: {text}"
        if outcome in ("enqueued", "rearmed", "pending"):
            _notice(loop, number, "fixing", 0,
                    f"auto-offered; fix turn queued from {loop['base']} at {base[:7]}")
        return f"auto-offered at {base[:7]}: {outcome}"
    except Exception as exc:
        return f"not auto-offered: {type(exc).__name__}: {exc}"
