"""Hold an issue fix until the PR its finding came from is merged (#324).

An issue the reviewer filed (``filed_issues``, #247) is about code that may exist only in the PR
it was filed from. A fix turn starts from the base branch, so while that PR is open it would find
nothing. ``gate_triage`` holds such a hand-off here; the watchdog sweep releases it once the PR
merges, or drops it (with one comment) when the PR closes unmerged.

An issue a person opened has no ``filed_issues`` row. It is held only on an open PR it *declares*
it depends on ("depends on #N", "blocked by #N", "follow-up to #N", ...), never on one it merely
mentions (#686): a mention is an example or a link, and holding on it deadlocked an issue whose
fix the mentioned PR was waiting for.
"""

from __future__ import annotations

from . import config, gate, gh, issue_deps
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


def queue_fix(loop: dict, number: int, turn_key: str = "issue-fix") -> tuple[str, str]:
    """Queue the fix turn from the base branch's current commit: ``(base, outcome)``.

    Raises ``RuntimeError`` when the base is unreadable, or whatever the enqueue raises.
    """
    ref = gh.api(loop, f"/repos/{loop['repo']}/git/ref/heads/{loop['base']}",
                 login=loop["read_token"])
    base = ((ref or {}).get("object") or {}).get("sha") if isinstance(ref, dict) else None
    if not isinstance(base, str) or len(base) != 40:
        raise RuntimeError(f"base branch {loop['base']} unreadable (GitHub read failed)")
    try:
        return base, gate.enqueue_isolated(loop, "issue_fixer", number, base, turn_key=turn_key)
    except Exception as exc:
        exc.fix_base = base  # the caller's notice needs the base the enqueue failed at
        raise


# -- the fixer's one question, and plan-first issues (#483) -------------------------------------

PLAN_LABEL = "plan-first"
PLAN_BODY_MAX = 4000        # an issue body longer than this counts as large: plan before fix
ASK_MAX = 3                 # asks (questions and plans) per issue; each costs a fixer turn
ASK_REPLY_MAX = 4000
APPROVE = "/loop approve"
ASK_KINDS = ("question", "plan")


def plan_required(st, number: int, issue: dict | None) -> bool:
    """Whether the issue needs an approved plan before a fix may be opened: it carries the
    ``plan-first`` label or its body is large, and no maintainer has approved a plan yet. The one
    rule the prompt, the broker's ``open_pr`` and ``explain`` all use."""
    if st.fix_plan_approved(number) or not isinstance(issue, dict):
        return False
    labels = {str((x or {}).get("name") or "").casefold()
              for x in issue.get("labels") or [] if isinstance(x, dict)}
    return PLAN_LABEL in labels or len(str(issue.get("body") or "")) > PLAN_BODY_MAX


def ask_line(number: int, ask: dict) -> str:
    """The one sentence ``status`` and ``explain`` use for an issue waiting on a maintainer."""
    what = ("approval of its plan (`/loop approve`)" if ask.get("kind") == "plan"
            else "an answer to its question")
    return f"issue #{number} fix waits for a maintainer: {what}; no fix turn starts until then"


def check_ask(st, number: int) -> None:
    """Raise ``ValueError`` when the issue already has an open ask or its asks are spent."""
    if number in st.fix_ask_open():
        raise ValueError("this issue already has an open question or plan: wait for the reply")
    if st.fix_ask_count(number) >= ASK_MAX:
        raise ValueError(f"this issue has used its {ASK_MAX} questions/plans; fix it, or say "
                         "it cannot be fixed with a plain issue_comment")


def reply_to(loop: dict, ask: dict, comments: list) -> tuple[str, str] | None:
    """``(login, body)`` of the first maintainer comment after the ask that resumes it, else
    ``None``. Anyone else's comment is ignored; a plan is resumed only by ``/loop approve``."""
    allowed = config.maintainers(loop)
    after = ask.get("comment_id") if type(ask.get("comment_id")) is int else 0
    for comment in sorted((c for c in comments if isinstance(c, dict) and type(c.get("id")) is int),
                          key=lambda c: c["id"]):
        user = comment.get("user")
        login = str(user.get("login") or "").lower() if isinstance(user, dict) else ""
        body = str(comment.get("body") or "")
        if comment["id"] <= after or login not in allowed:
            continue
        if ask.get("kind") == "plan" and body.strip().lower() != APPROVE:
            continue
        return login, body
    return None


def sweep_asks(loop: dict, st, lines: list[str]) -> None:
    """Resume each open ask a maintainer answered: save the handoff, queue a fresh turn."""
    from . import run_supervisor
    for number, ask in sorted(st.fix_ask_open().items()):
        comments, error = gh.issue_comments_read(loop, number)
        if comments is None:
            log(f"issue #{number}: replies unreadable, ask stays open: {error}")
            continue
        found = reply_to(loop, ask, comments)
        if found is None:
            continue
        login, body = found
        st.fix_ask_resolve(number, body[:ASK_REPLY_MAX], login, ask.get("kind") == "plan")
        try:
            run_supervisor.issue_fix_issue(loop, number)
            base, outcome = queue_fix(loop, number, f"issue-fix:ask{st.fix_ask_count(number)}")
        except Exception as exc:
            lines.append(f"⚠️ issue #{number}: reply seen, fix not queued: "
                         f"{type(exc).__name__}: {exc}")
            continue                                # the ask stays open: retried next sweep
        st.fix_ask_drop(number)
        log(f"issue #{number}: {login} replied; fix turn resumed at {base[:7]}: {outcome}")


def hold(loop: dict, st, number: int, pr: int, why: str = "") -> None:
    """Record the held hand-off and say so once; ``why`` is the person's own words that declared
    the dependency, quoted in the comment (empty for a reviewer's finding)."""
    st.fix_hold_set(number, pr)
    if not st.fix_hold_said(number):
        if not _wait_comment(loop, number, pr, why):
            return                                # POST failed: not "said"; a later sweep retries
        st.fix_hold_say(number)
        _notice(loop, number, "held", pr,
                f"#{number} waits for PR #{pr} to merge: its finding is about code only there")


def _wait_comment(loop: dict, number: int, pr: int, why: str = "") -> bool:
    """Post the wait comment; ``False`` when the POST failed (``gh.api`` returned ``None``)."""
    login = config.triage_login(loop)
    if not login:
        return True                               # nobody to say it as: retrying cannot help
    text = (f"waiting for #{pr} to merge — this issue says “{why}”. Re-apply the fix label once "
            "that no longer holds." if why else
            f"waiting for #{pr} to merge — this finding is about code only on that branch")
    return gh.api(loop, f"/repos/{loop['repo']}/issues/{number}/comments", method="POST",
                  body={"body": text}, login=login) is not None


def _drop_comment(loop: dict, number: int, pr: int, declared: bool = False) -> None:
    from . import config as cfg
    login = cfg.triage_login(loop)
    if login:
        text = (f"PR #{pr}, which this issue says it depends on, closed without merging; not "
                "handing it to the fixer. Re-apply the fix label if it should go ahead without it."
                if declared else
                f"PR #{pr} closed without merging, so the finding in this issue is moot; not "
                "handing it to the fixer. The label stays for a person to decide.")
        gh.api(loop, f"/repos/{loop['repo']}/issues/{number}/comments", method="POST",
               body={"body": text}, login=login)


def sweep(loop: dict, st) -> list[str]:
    """Re-check held hand-offs; queue the fix of those whose PR merged. Best effort."""
    lines: list[str] = []
    if not config.issue_fixes_enabled(loop):
        return lines
    sweep_asks(loop, st, lines)
    for number, pr in sorted(st.fix_holds().items()):
        state = pr_state(loop, pr)
        if state is None:
            continue                              # unreadable: retry on the next sweep
        if state == "open":
            hold(loop, st, number, pr)            # says so once, if the gate could not
            continue
        if state == "closed":
            try:
                declared = origin_pr(loop, number) is None    # a person's declared dependency
            except Exception:
                continue                                       # lineage unreadable: retry
            _drop_comment(loop, number, pr, declared)
            st.fix_hold_drop(number)
            log(f"issue #{number}: PR #{pr} closed unmerged; hand-off dropped")
            continue
        blockers = issue_deps.blocking(loop, st, number)
        if blockers:                              # the PR merged; a prerequisite still waits
            st.fix_hold_drop(number)
            issue_deps.wait(loop, st, number, blockers)
            continue
        _release(loop, st, number, lines, st.fix_hold_drop, pr,
                 f"PR #{pr} merged")
    for number in st.dep_waits():                 # #671: waits on declared prerequisites
        if number in st.fix_holds():
            continue                              # the PR wait above owns it
        blockers = issue_deps.blocking(loop, st, number)
        if blockers:
            issue_deps.wait(loop, st, number, blockers)   # says so once, if not yet said
            continue
        _release(loop, st, number, lines, st.dep_wait_drop, 0, "its prerequisites merged")
    return lines


def _release(loop: dict, st, number: int, lines: list[str], drop, pr: int, why: str) -> None:
    """Queue a waiting issue's fix from the base's current commit, after re-checking that it is
    still eligible; ``drop`` clears the wait it came from. Best effort, like the sweep."""
    from . import run_supervisor
    try:
        live = run_supervisor.issue_fix_issue(loop, number)
    except run_supervisor.RetryableError:
        return
    except Exception as exc:                      # closed, unlabelled, ...: nothing to fix now
        drop(number)
        log(f"issue #{number}: held hand-off dropped: {exc}")
        return
    prs, error = gh.open_prs_read(loop)           # the guard hand_off applies, at every release
    if prs is None:
        log(f"issue #{number}: open PR listing unreadable, release waits: {error}")
        return                                    # retry on the next sweep
    existing = _closing(loop, prs, number)
    if existing is not None:                      # a PR opened during the wait already fixes it
        _skip_comment(loop, number, existing)
        drop(number)
        log(f"issue #{number}: open PR #{existing} already fixes it; held hand-off dropped")
        return
    present = {str((x or {}).get("name") or "").casefold()
               for x in live.get("labels") or [] if isinstance(x, dict)}
    auto = loop["triage"]["fix_label"].casefold() not in present   # no maintainer's label
    if auto and _capped(loop):
        return                                    # the day's cap is spent: held till tomorrow
    try:
        base, outcome = queue_fix(loop, number)
    except Exception as exc:
        lines.append(f"⚠️ issue #{number}: held fix not queued: {type(exc).__name__}: {exc}")
        return
    drop(number)
    if auto and outcome in ("enqueued", "rearmed", "pending"):
        from . import pacing
        pacing.count_turn(loop["id"], AUTO_FIX_SEAT)
    log(f"issue #{number} released at {base[:7]}: {outcome}")
    if outcome in ("enqueued", "rearmed", "pending"):
        _notice(loop, number, "fixing", pr,
                f"{why}; fix turn queued from {loop['base']} at {base[:7]}")


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


# Only phrases that say "this waits for that". "after #N" or "needs #N" read as prose as often
# as not ("it broke after #12 merged"), and a false hold is the bug this replaced.
DEPENDS = (r"depends\s+on|depend\s+on|blocked\s+by|builds?\s+on|follow[-\s]?up\s+(?:to|of|on)|"
           r"requires")


def declared_open_pr(loop: dict, prs: list[dict], body: str) -> tuple[int, str] | None:
    """The first open PR of this repo an issue body says it depends on, with those words, else
    ``None`` (#686). A declaration is one of ``DEPENDS`` right before the PR (``#N``, ``PR #N``,
    ``owner/repo#N`` or its URL), as in "Depends on #12" or "Follow-up to PR #12"; a PR the body
    only mentions ("as #12 showed", a bare link) is never one. A merged or closed PR, or an
    issue, is not in the open listing: never held."""
    import re
    repo = re.escape(str(loop["repo"]))
    open_numbers = {pr["number"] for pr in prs if type(pr.get("number")) is int}
    pattern = re.compile(rf"\b(?:{DEPENDS})\s*:?\s+(?:(?:PR|pull\s+request)\s+)?"
                         rf"(?:(?:{repo})?#(\d+)(?!\d)|https://github\.com/{repo}/pull/(\d+)(?!\d))",
                         re.IGNORECASE)
    for match in pattern.finditer(str(body or "")):
        number = int(match.group(1) or match.group(2))
        if number in open_numbers:
            return number, " ".join(match.group(0).split())
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
    why = ""
    if origin is None:                               # ... or a person's declared dependency
        declared = declared_open_pr(loop, prs, str((issue or {}).get("body") or ""))
        origin, why = declared if declared else (None, "")
    st = state_mod.state_for(loop)
    if number in st.fix_ask_open():               # #483: waiting for a maintainer's reply
        return "held", "waits for a maintainer's reply to the fixer's question or plan", "", ""
    if origin is None:
        st.fix_hold_drop(number)                     # an earlier hold no longer applies (#686)
    else:
        origin_state = pr_state(loop, origin)
        if origin_state is None:
            st.fix_hold_set(number, origin)          # the sweep retries
            return "unreadable", f"PR #{origin} unreadable; held", "", ""
        if origin_state == "open":
            hold(loop, st, number, origin, why)
            return "held", f"held until PR #{origin} merges", "", ""
        if origin_state == "closed":
            _drop_comment(loop, number, origin, bool(why))
            st.fix_hold_drop(number)
            return "dropped", f"PR #{origin} closed unmerged; finding is moot", "", ""
        st.fix_hold_drop(number)
    blockers = issue_deps.blocking(loop, st, number)   # #671: declared prerequisites
    if blockers:
        issue_deps.wait(loop, st, number, blockers)
        names = ", ".join(f"#{a}" for a, _ in blockers)
        return "held", f"waits for prerequisite {names}", "", ""
    st.dep_wait_drop(number)
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
