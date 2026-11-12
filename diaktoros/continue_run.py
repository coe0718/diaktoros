"""Finish a half-written issue fix, or leave it quarantined with what was found (#485).

An issue-fix run writes in stages, each recorded in ``issue_fixes`` before it happens:
branch push -> PR create -> review request. A run that stopped between two of them (or whose
last write's outcome is unknown, so it is ``uncertain``) used to need a person to read the
ledger and GitHub by hand. ``inspect`` compares the two and names the one safe next step;
``run`` performs only the stages still missing. It never pushes: the branch push is not a
stage ``continue`` can do, so no push can happen twice. Anything the records and GitHub
disagree on stays quarantined, with the facts listed. Before any stage writes, the fix is
re-authorized against live policy (``broker.authorize_issue_fix``, #1051): the operator's
switch, the identity checks, the issue's author and fix label and its dependency holds bind
``continue`` as they bind the seat's own ``open_pr``, and so does the plan-first rule
(``fix_hold.plan_required``, checked on that same authorizing read, #2334, #2336): a PR stage
waits for a maintainer-approved plan, which ``post_plan`` asks for.

``from_chat`` is the reply path (``continue N``): a sender who is not one of the loop's
maintainers is ignored without a trace.
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote

from . import broker, config, gate, gh, review_kick
from .util import log

# What the ledger may say when a continue is meaningful (the row's state, not the run's).
RESUMABLE = ("pushed", "opened", "uncertain")
CLAIM = "continuing"
# The review stage for an App reviewer (#1209): GitHub cannot request one (#1163), so the host
# delivers the review to the reviewer gate instead of asking for it.
DELIVER_REVIEW = "deliver_review"
REPLY = re.compile(r"\s*continue\s+([A-Za-z0-9_.:-]{1,64})\s*", re.IGNORECASE)


def inspect(loop: dict, sup, run_id: str) -> dict:
    """Compare the run's records with GitHub: ``{"stages": [...], "conflicts": [...],
    "facts": [...], "fix": row}``. ``stages`` is what ``run`` would do, in order; any
    ``conflicts`` mean it does nothing."""
    facts: list[str] = []
    conflicts: list[str] = []
    plan = {"stages": [], "conflicts": conflicts, "facts": facts, "fix": None, "run": None,
            "plan_needed": None}
    with sup._connect() as con:
        row = con.execute("SELECT id,repo,pr,head,seat,state FROM runs WHERE id=?",
                          (run_id,)).fetchone()
    if row is None or row["seat"] != "issue_fixer" or row["repo"] != loop["repo"]:
        conflicts.append(f"run {run_id} is not an issue-fix run of {loop['repo']}")
        return plan
    plan["run"] = dict(row)
    fix = sup.issue_fix_result(run_id)
    plan["fix"] = fix
    if fix is None:
        facts.append("no issue-fix write is on record: nothing was written")
        return plan
    if row["state"] in ("launching", "running", "pending", "waiting"):
        conflicts.append(f"the run is still {row['state']}: a worker may be writing")
        return plan
    if fix["kind"] != "pr":
        facts.append(f"the recorded write is a {fix['kind']}, not a branch and PR: nothing to finish")
        return plan
    if fix["state"] == "requested":
        facts.append("every stage is recorded as done")
    elif fix["state"] == CLAIM:
        conflicts.append("a previous continue stopped mid-write: its outcome is unknown")
    elif fix["state"] not in RESUMABLE:
        facts.append(f"the fix is recorded as {fix['state']}: nothing to finish")
        return plan
    repo, number, branch = fix["repo"], fix["number"], fix["branch"]
    issue, err = gh.fetch(loop, f"/repos/{repo}/issues/{number}")
    if not isinstance(issue, dict) or err:
        conflicts.append(f"could not read issue #{number}: {err or 'unreadable'}")
    elif "pull_request" in issue or issue.get("state") != "open":
        conflicts.append(f"issue #{number} is no longer an open issue")
    plan["plan_needed"] = _plan_needed(loop, number, issue, err)
    ref, err = gh.fetch(loop, f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}")
    sha = None
    if gh.status_of(err) == 404:
        facts.append(f"branch {branch} is not on GitHub")
        if fix["new_head"] or fix["pr_number"]:
            conflicts.append(f"the ledger says {branch} was pushed, GitHub has no such branch")
    elif err or not isinstance(ref, dict):
        conflicts.append(f"could not read branch {branch}: {err or 'unreadable'}")
    else:
        sha = (ref.get("object") or {}).get("sha")
        facts.append(f"branch {branch} is at {str(sha)[:7]}")
        if not fix["new_head"]:
            conflicts.append(f"{branch} exists but the ledger never recorded a pushed head, so "
                             "it cannot be shown to be this run's")
        elif sha != fix["new_head"]:
            conflicts.append(f"{branch} is at {str(sha)[:7]}, the ledger recorded "
                             f"{fix['new_head'][:7]}")
    owner = repo.split("/")[0]
    prs, err = gh.fetch(loop, f"/repos/{repo}/pulls?state=all&per_page=100&head="
                              f"{quote(owner + ':' + branch, safe='')}")
    pr = None
    if not isinstance(prs, list) or err:
        conflicts.append(f"could not list PRs for {branch}: {err or 'unreadable'}")
    elif len(prs) > 1:
        conflicts.append("more than one PR has this branch: "
                         + ", ".join(f"#{p.get('number')}" for p in prs if isinstance(p, dict)))
    elif prs and isinstance(prs[0], dict):
        pr = prs[0]
        _check_pr(loop, plan, pr, sha)
    elif fix["pr_number"]:
        conflicts.append(f"the ledger recorded PR #{fix['pr_number']}, GitHub has no PR "
                         "for the branch")
    if conflicts:
        return plan
    if sha is None:
        facts.append("the push never landed; continue never pushes")
        return plan
    if pr is None:
        plan["stages"] = ["open_pr", review_stage(loop)]
    else:
        _review_stage(loop, plan, pr)
    _authorize(loop, plan)
    return plan


def _plan_needed(loop: dict, number: int, issue, err: str) -> bool | None:
    """Whether the issue, as ``inspect`` just read it, still needs an approved plan before a PR
    (``fix_hold.plan_required``, the rule the broker's ``open_pr`` applies); ``None`` when it
    could not be read. A closed issue, or one that is now a PR, needs none: no plan is possible.
    ``post_plan`` reads it to settle the run's plan ask even when another conflict stands
    (#2382 review F2): a conflict alone does not mean a plan is still needed."""
    from . import fix_hold, state as state_mod
    if not isinstance(issue, dict) or err:
        return None
    if "pull_request" in issue or issue.get("state") != "open":
        return False
    return fix_hold.plan_required(state_mod.state_for(loop), number, issue)


def _check_pr(loop: dict, plan: dict, pr: dict, sha: str | None) -> None:
    """Say in ``plan`` what GitHub's one PR for the fix branch is, and every way it disagrees
    with the records: not open, another head or base, not the fixer seat's, another number."""
    fix, facts, conflicts = plan["fix"], plan["facts"], plan["conflicts"]
    facts.append(f"PR #{pr.get('number')} is {pr.get('state')} at "
                 f"{str((pr.get('head') or {}).get('sha'))[:7]}")
    seat = config.seat_login(loop, "fixer").casefold()
    if pr.get("state") != "open":
        conflicts.append(f"PR #{pr.get('number')} is {pr.get('state')}")
    if sha and (pr.get("head") or {}).get("sha") != sha:
        conflicts.append(f"PR #{pr.get('number')} head differs from the branch head")
    if (pr.get("base") or {}).get("ref") != loop.get("base"):
        conflicts.append(f"PR #{pr.get('number')} targets another base")
    if str((pr.get("user") or {}).get("login") or "").casefold() != seat:
        conflicts.append(f"PR #{pr.get('number')} was not opened by the fixer seat")
    if fix["pr_number"] and fix["pr_number"] != pr.get("number"):
        conflicts.append(f"the ledger recorded PR #{fix['pr_number']}, GitHub has "
                         f"#{pr.get('number')}")


def _authorize(loop: dict, plan: dict) -> None:
    """Re-authorize the planned writes against live policy (#1051), as the seat's own
    ``open_pr`` is: ``broker.authorize_issue_fix`` (issue fixes on, distinct verified
    identities, the issue still open by an allowlisted author with the fix label, no
    dependency hold), and for a PR stage the plan-first rule the broker's ``open_pr`` applies
    (``plan_first=True``: ``fix_hold.plan_required`` on the very issue read that authorized it,
    #2334, #2336). A denial is a conflict, so the dry run says it and ``run`` writes nothing;
    an issue GitHub could not be read for is worded as not checked, not as refused (#2335); a
    plan-first denial also marks ``plan["plan_first"]``, which ``post_plan`` recovers from.
    With no stage planned nothing is written, so recording a finished fix needs no check."""
    if not plan["stages"]:
        return
    fix = plan["fix"]
    try:
        broker.authorize_issue_fix(loop, repo=fix["repo"], number=fix["number"],
                                   plan_first="open_pr" in plan["stages"])
    except broker.PlanRequired as exc:
        plan["conflicts"].append(f"{exc}; post the pushed fix as its plan with `hermes dk "
                                 f"continue --loop {loop['id']} --run {plan['run']['id']} "
                                 "--post-plan`, then a maintainer replies `/loop approve`")
        plan["plan_first"] = True
        plan["stages"] = []
    except broker.BrokerUnchecked as exc:
        plan["conflicts"].append(f"the issue fix could not be checked now: {exc}")
        plan["stages"] = []
    except broker.BrokerDenied as exc:
        plan["conflicts"].append(f"the issue fix is not authorized now: {exc}")
        plan["stages"] = []


def post_plan(loop: dict, sup, run_id: str) -> tuple[bool, list[str]]:
    """Post a pushed plan-first fix as its plan (#2334): the recovery for a run ``continue``
    refuses only because no maintainer approved a plan. One fixer comment on the issue, worded
    like the seat's ``--ask plan`` (``fix_hold.posted_ask``), opens a plan ask tied to this run;
    a maintainer's ``/loop approve`` after it is read by the usual sweep or webhook
    (``fix_hold.sweep_asks``), which records the approval, and ``continue`` then opens the PR.
    Refused when anything else stands in the way, another ask is open, or the asks are spent.

    The ask is reserved (``State.fix_ask_claim``) before the comment is posted, and the reserve,
    the post and the record all run under ``config.push_policy_lock``, the lock the seat's own
    ``issue_comment --ask`` checks, posts and records under: two ``--post-plan`` calls, or one
    and a seat, never both post (review F3). A reservation whose comment was not confirmed (a
    lost response, a failed state write, a crash) stays open as ``pending``, which no reply
    resolves; calling this again for the same run reconciles it: it adopts the fixer's comment
    carrying this run's ``plan_marker`` if GitHub has one, and posts only if not (review F4).
    That reconciling happens whatever the inspection says now (#2381): when the run is no longer
    refused only for want of a plan, its own reservation is still settled by
    ``_release_plan_ask`` before the refusal, so a changed label, body or branch cannot strand it.
    """
    from . import fix_hold, state as state_mod
    plan = inspect(loop, sup, run_id)
    lines = list(plan["facts"])
    if not plan.get("plan_first") or len(plan["conflicts"]) != 1:
        why = "; ".join(plan["conflicts"]) or "no plan is needed"
        settled = []
        if plan["fix"] is not None:
            # The plan is no longer required (no plan-first refusal and no conflict, or the
            # issue read now says no plan is needed or possible, #2382 review F2): the run's ask
            # is closed outright. While a conflict hides whether one is still needed (the issue
            # still asks for a plan, or could not be read), only an unconfirmed one is settled.
            keep = bool(plan["conflicts"]) and plan.get("plan_needed") is not False
            settled = [_release_plan_ask(loop, state_mod.state_for(loop), plan["fix"]["number"],
                                         run_id, keep_adopted=keep)]
        return False, lines + [f"no plan posted: only a run refused for want of a plan has one "
                               f"to post ({why})"] + [s for s in settled if s]
    fix, st = plan["fix"], state_mod.state_for(loop)
    number = fix["number"]
    text = (f"Run `{run_id}` pushed a fix for this issue to `{fix['branch']}` at "
            f"`{str(fix['new_head'])[:12]}`, but the issue needs an approved plan before a PR. "
            f"The plan is that branch as it stands: https://github.com/{fix['repo']}/compare/"
            f"{loop['base']}...{quote(fix['branch'], safe='/')}")
    with config.push_policy_lock():
        try:
            login = broker.authorize_issue_fix(loop, repo=fix["repo"], number=number)
            ask = st.fix_ask_claim(number, "plan", text, run_id, fix_hold.check_ask)
        except (ValueError, broker.BrokerDenied) as exc:
            return False, lines + [f"no plan posted: {exc}"]
        failed = _post_claimed_plan(loop, st, fix, login, run_id, ask)
    if failed:
        return False, lines + [failed]
    return True, lines + [f"posted the plan on issue #{number}; once a maintainer replies "
                          f"`{fix_hold.APPROVE}`, `hermes dk continue --loop {loop['id']} --run "
                          f"{run_id}` opens the PR"]


def plan_marker(run_id: str) -> str:
    """The hidden first line that ties a posted plan comment to its run, so a retry finds it."""
    return f"<!-- diaktoros plan of run {run_id} -->"


def _adopt_plan_comment(loop: dict, number: int, login: str, run_id: str) -> tuple[int | None, str]:
    """``(id, "")`` of the oldest comment by ``login`` carrying ``run_id``'s ``plan_marker``,
    ``(None, "")`` when there is none, ``(None, why)`` when the comments are unreadable."""
    comments, error = gh.issue_comments_read(loop, number)
    if comments is None:
        return None, error or "unreadable"
    for comment in sorted((c for c in comments if isinstance(c, dict) and type(c.get("id")) is int),
                          key=lambda c: c["id"]):
        user = comment.get("user")
        author = str(user.get("login") or "") if isinstance(user, dict) else ""
        if author.casefold() == login.casefold() and plan_marker(run_id) in str(comment.get("body") or ""):
            return comment["id"], ""
    return None, ""


def _release_plan_ask(loop: dict, st, number: int, run_id: str, keep_adopted: bool) -> str:
    """Settle ``run_id``'s own plan ask on issue ``number`` once it is no longer the way to the
    run's PR (#2381); the line to report, or ``""`` when the run has no ask open. Another run's
    or the seat's ask is never touched.

    A ``pending`` reservation is first matched against the issue's comments
    (``_adopt_plan_comment``): with no marked comment on the issue it is dropped and its count
    given back, since nothing was asked. With ``keep_adopted`` (a conflict hides whether a plan
    is still needed) a found comment is adopted, so a maintainer's ``/loop approve`` resolves it
    as usual, and a confirmed ask stays; without it (the plan is no longer required, or the PR is
    open) the ask is closed, still counted when its comment is on the issue or may be. Runs under
    ``config.push_policy_lock``, as a ``--post-plan`` post does."""
    if not isinstance(st.fix_ask_open().get(number), dict):
        return ""
    with config.push_policy_lock():
        ask = st.fix_ask_open().get(number)
        if not isinstance(ask, dict) or ask.get("run") != run_id:
            return ""
        if not ask.get("pending"):
            if keep_adopted:
                return ""
            st.fix_ask_drop(number, run=run_id)
            return f"closed run {run_id}'s plan ask on issue #{number}: the plan is not needed now"
        comment, error = _adopt_plan_comment(loop, number, config.seat_login(loop, "fixer"), run_id)
        if error and keep_adopted:
            return (f"run {run_id}'s plan ask on issue #{number} stays pending: the issue's "
                    f"comments are unreadable ({error}); run `--post-plan` again")
        if comment is None and not error:
            st.fix_ask_drop(number, run=run_id, uncount=True)
            return (f"dropped run {run_id}'s unposted plan reservation on issue #{number} "
                    "(no plan comment of the run is on the issue)")
        if keep_adopted:
            try:
                st.fix_ask_confirm(number, run_id, comment)
            except Exception as exc:
                return f"plan comment {comment} found but not recorded ({exc}); run `--post-plan` again"
            return (f"adopted plan comment {comment} of run {run_id}; a maintainer's "
                    "`/loop approve` resolves it")
        st.fix_ask_drop(number, run=run_id)
        return f"closed run {run_id}'s plan ask on issue #{number}: the plan is not needed now"


def _post_claimed_plan(loop: dict, st, fix: dict, login: str, run_id: str, ask: dict) -> str:
    """Post (or adopt) the comment of the plan ask ``post_plan`` reserved and record its id;
    the refusal line, or ``""``. A reservation left ``pending`` by an earlier call is first
    matched against the issue's comments (``_adopt_plan_comment``), so a comment that was posted
    but never recorded is adopted, not posted twice. Any failure leaves it ``pending``."""
    from . import fix_hold
    number, again = fix["number"], "; the plan ask stays reserved: run `--post-plan` again"
    comment = None
    if ask.get("reconcile"):
        comment, error = _adopt_plan_comment(loop, number, login, run_id)
        if error:
            return f"no plan posted: the issue's comments are unreadable ({error}){again}"
    if comment is None:
        body = f"{plan_marker(run_id)}\n{fix_hold.posted_ask('plan', ask['text'])}"
        try:
            comment = broker.post_issue_comment(loop, repo=fix["repo"], number=number,
                                                login=login, body=body)
        except Exception as exc:
            return (f"plan comment failed or unconfirmed ({exc}){again}, which adopts the "
                    "comment if GitHub has it")
    try:
        st.fix_ask_confirm(number, run_id, comment)
    except Exception as exc:
        return f"plan comment {comment} posted but not recorded ({exc}){again}, which adopts it"
    return ""


def review_stage(loop: dict) -> str:
    """The stage that hands a PR to the reviewer: ``deliver_review`` for an App reviewer, which
    GitHub cannot be asked to review (#1163) so the host delivers it to the reviewer gate;
    ``request_review`` for a PAT reviewer."""
    return DELIVER_REVIEW if config.reviewer_is_app(loop) else "request_review"


def _review_stage(loop: dict, plan: dict, pr: dict) -> None:
    """Say in ``plan`` what the open ``pr`` still needs of the reviewer's handoff.

    A verdict counts only at the PR's current head, as the gate counts it
    (``gate.reviewed_at_head``): one at an older head says nothing about this one. A PAT
    reviewer's pending request is read from ``requested_reviewers``. An App reviewer never shows
    there (GitHub records no request for it, #1163), so only a verdict proves its handoff.
    """
    number = pr["number"]
    reviews, err = gh.reviews_read(loop, number)
    if reviews is None:
        plan["conflicts"].append(f"could not read reviews of PR #{number}: {err}")
        return
    plan["pr"] = number
    head = (pr.get("head") or {}).get("sha")
    if gate.reviewed_at_head(reviews, loop, head):
        plan["facts"].append(f"review of PR #{number} is already given at {str(head)[:7]}")
    elif not config.reviewer_is_app(loop) and review_kick.already_requested(loop, pr):
        plan["facts"].append(f"review of PR #{number} is already requested")
    else:
        plan["stages"] = [review_stage(loop)]


def offer(plan: dict, loop_id: str, run_id: str) -> str:
    """The one safe next step, in words."""
    if plan["conflicts"]:
        return "stays quarantined: " + "; ".join(plan["conflicts"])
    if plan["stages"]:
        app = ("; the reviewer is a GitHub App, which GitHub cannot request, so the host "
               "delivers the review to the reviewer gate" if DELIVER_REVIEW in plan["stages"]
               else "")
        return (f"safe next step: complete {' and '.join(plan['stages'])} "
                f"(hermes dk continue --loop {loop_id} --run {run_id}; chat: continue {run_id})"
                + app)
    return (f"nothing is missing: continue only records it and releases the run "
            f"(hermes dk continue --loop {loop_id} --run {run_id})")


def _claim(sup, run_id: str) -> bool:
    with sup._connect() as con:
        return con.execute("UPDATE issue_fixes SET state=?,updated=? WHERE run_id=? AND state IN "
                           "('pushed','opened','uncertain','requested')",
                           (CLAIM, time.time(), run_id)).rowcount == 1


def _deliver_review(loop: dict, pr: int, sender: str) -> str:
    """Hand the reviewer gate the review GitHub cannot be asked for (an App reviewer, #1163);
    the line to report. Best effort: the watchdog's sweep delivers a quiet head's review too."""
    live = gh.api(loop, gh.pr_path(loop, pr), login=loop["read_token"])
    head = ((live or {}).get("head") or {}).get("sha") if isinstance(live, dict) else None
    if not head:
        return (f"PR #{pr}: could not read it to start its review (the reviewer is a GitHub App, "
                "which GitHub cannot request); the watchdog sweep will")
    try:
        sent = review_kick.kick(loop, live, head, sender, f"continue-review-{pr}")
    except Exception as exc:
        sent = False
        log(f"#{pr} @ {head[:7]}: review start not delivered: {type(exc).__name__}: {exc}")
    return (f"delivered the review of PR #{pr} to the reviewer gate (the reviewer is a GitHub "
            "App, which GitHub cannot request)" if sent else
            f"PR #{pr}: its review start was not delivered (the reviewer is a GitHub App); the "
            "watchdog sweep will")


def _release_after_pr(loop: dict, number: int, run_id: str, lines: list[str]) -> bool:
    """The run's PR is open (``continue`` just created it, or found it open): a plan ask
    ``--post-plan`` opened for this run asks for nothing any more, so close it (#2381) before
    any later stage, so a failed review request cannot leave it stranded (#2382 review F3).

    Best effort; whether it was settled. A failure (the state lock or the ask file cannot be
    written) is reported, never raised: the run is claimed ``continuing`` here, and an escaping
    error would leave that claim as an unknown mid-write that no later ``continue`` resumes
    (#2382 review F5). The ask stays as it was; the run's remaining stages still complete, and
    the next ``continue`` (which finds the PR open) or ``--post-plan`` settles the ask."""
    from . import state as state_mod
    try:
        released = _release_plan_ask(loop, state_mod.state_for(loop), number, run_id,
                                     keep_adopted=False)
    except Exception as exc:
        lines.append(f"run {run_id}'s plan ask on issue #{number} not closed "
                     f"({type(exc).__name__}: {exc}); run `continue` again to close it")
        return False
    if released:
        lines.append(released)
    return True


def run(loop: dict, sup, run_id: str) -> tuple[bool, list[str]]:
    """Inspect, then complete only the missing stages. ``(ok, lines)``; ok is False when the
    run stays quarantined or a write failed."""
    plan = inspect(loop, sup, run_id)
    lines = list(plan["facts"])
    if plan["conflicts"]:
        return False, lines + [offer(plan, loop["id"], run_id)]
    fix = plan["fix"]
    if fix is None or fix["kind"] != "pr" or fix["state"] not in RESUMABLE + ("requested",):
        return False, lines + ["nothing to continue"]
    if not _claim(sup, run_id):
        return False, lines + ["another continue holds this run"]
    repo, number, branch = fix["repo"], fix["number"], fix["branch"]
    login = config.seat_login(loop, "fixer")
    pr = plan.get("pr")
    new_head = fix["new_head"]
    settled = True
    if pr is not None:
        settled = _release_after_pr(loop, number, run_id, lines)
    for stage in plan["stages"]:
        if stage == "open_pr":
            issue = gh.fetch(loop, f"/repos/{repo}/issues/{number}")[0] or {}
            title = str(issue.get("title") or f"Fix #{number}").strip()[:broker.ISSUE_PR_TITLE_MAX]
            body = (f"Opened by `hermes dk continue` for run {run_id}: the fix branch was pushed "
                    "and the run stopped before this PR was created.")
            try:
                pr = broker.open_issue_pr(loop, repo=repo, number=number, branch=branch,
                                          title=title, body=body, login=login)
            except Exception as exc:
                sup.issue_fix_status(run_id, "uncertain", error=f"continue: PR create unknown: {exc}"[:300])
                return False, lines + [f"PR create failed or unconfirmed ({exc}); quarantined again"]
            sup.issue_fix_status(run_id, "opened", pr_number=pr)
            lines.append(f"opened PR #{pr}")
            settled = _release_after_pr(loop, number, run_id, lines)
        else:
            try:
                broker.request_issue_pr_review(loop, repo=repo, pr=pr, login=login)
            except Exception as exc:
                sup.issue_fix_status(run_id, "opened", pr_number=pr,
                                     error=f"continue: review request not confirmed: {exc}"[:300])
                return False, lines + [f"review request not confirmed ({exc}); PR #{pr} is open"]
            if stage == DELIVER_REVIEW:
                # GitHub cannot request an App reviewer (#1163), and this PR is already open, so
                # its `opened` will not come again: deliver the review to the gate, as the broker
                # does after a push (review_kick, #532).
                lines.append(_deliver_review(loop, pr, login))
            else:
                lines.append(f"requested review of PR #{pr}")
    sup.issue_fix_status(run_id, "requested", pr_number=pr, new_head=new_head)
    if plan["run"]["state"] == "uncertain":
        try:
            sup.reconcile_uncertain(run_id, reason=f"continue: issue fix finished (PR #{pr})",
                                    acknowledge_no_live_worker=True)
            lines.append("run released")
        except ValueError as exc:
            lines.append(f"run left uncertain: {exc}")
    return settled, lines


def from_chat(loop: dict, sup, sender: str, text: str) -> tuple[bool, list[str]] | None:
    """A ``continue N`` chat reply. None (ignored, nothing read or written) unless the sender
    is one of the loop's maintainers and the text is exactly ``continue <run id>``."""
    match = REPLY.fullmatch(text or "")
    if match is None or str(sender or "").strip().lower() not in config.maintainers(loop):
        return None
    return run(loop, sup, match.group(1))
