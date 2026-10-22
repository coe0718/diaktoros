"""Finish a half-written issue fix, or leave it quarantined with what was found (#485).

An issue-fix run writes in stages, each recorded in ``issue_fixes`` before it happens:
branch push -> PR create -> review request. A run that stopped between two of them (or whose
last write's outcome is unknown, so it is ``uncertain``) used to need a person to read the
ledger and GitHub by hand. ``inspect`` compares the two and names the one safe next step;
``run`` performs only the stages still missing. It never pushes: the branch push is not a
stage ``continue`` can do, so no push can happen twice. Anything the records and GitHub
disagree on stays quarantined, with the facts listed.

``from_chat`` is the reply path (``continue N``): a sender who is not one of the loop's
maintainers is ignored without a trace.
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote

from . import broker, config, gh
from .util import log

# What the ledger may say when a continue is meaningful (the row's state, not the run's).
RESUMABLE = ("pushed", "opened", "uncertain")
CLAIM = "continuing"
REPLY = re.compile(r"\s*continue\s+([A-Za-z0-9_.:-]{1,64})\s*", re.IGNORECASE)


def inspect(loop: dict, sup, run_id: str) -> dict:
    """Compare the run's records with GitHub: ``{"stages": [...], "conflicts": [...],
    "facts": [...], "fix": row}``. ``stages`` is what ``run`` would do, in order; any
    ``conflicts`` mean it does nothing."""
    facts: list[str] = []
    conflicts: list[str] = []
    plan = {"stages": [], "conflicts": conflicts, "facts": facts, "fix": None, "run": None}
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
    elif fix["pr_number"]:
        conflicts.append(f"the ledger recorded PR #{fix['pr_number']}, GitHub has no PR "
                         "for the branch")
    if conflicts:
        return plan
    if sha is None:
        facts.append("the push never landed; continue never pushes")
        return plan
    if pr is None:
        plan["stages"] = ["open_pr", "request_review"]
        return plan
    reviewer = config.seat_login(loop, "reviewer").casefold()
    asked = {str(r.get("login") or "").casefold() for r in pr.get("requested_reviewers") or []
             if isinstance(r, dict)}
    reviews, err = gh.reviews_read(loop, pr["number"])
    if reviews is None:
        conflicts.append(f"could not read reviews of PR #{pr['number']}: {err}")
        return plan
    reviewed = any(str((r.get("user") or {}).get("login") or "").casefold() == reviewer
                   for r in reviews if isinstance(r, dict))
    if reviewer in asked or reviewed:
        facts.append(f"review of PR #{pr['number']} is already {'given' if reviewed else 'requested'}")
    else:
        plan["stages"] = ["request_review"]
    plan["pr"] = pr["number"]
    return plan


def offer(plan: dict, loop_id: str, run_id: str) -> str:
    """The one safe next step, in words."""
    if plan["conflicts"]:
        return "stays quarantined: " + "; ".join(plan["conflicts"])
    if plan["stages"]:
        return (f"safe next step: complete {' and '.join(plan['stages'])} "
                f"(hermes dk continue --loop {loop_id} --run {run_id}; chat: continue {run_id})")
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
    from . import review_kick
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
        else:
            try:
                broker.request_issue_pr_review(loop, repo=repo, pr=pr, login=login)
            except Exception as exc:
                sup.issue_fix_status(run_id, "opened", pr_number=pr,
                                     error=f"continue: review request not confirmed: {exc}"[:300])
                return False, lines + [f"review request not confirmed ({exc}); PR #{pr} is open"]
            if config.reviewer_is_app(loop):
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
    return True, lines


def from_chat(loop: dict, sup, sender: str, text: str) -> tuple[bool, list[str]] | None:
    """A ``continue N`` chat reply. None (ignored, nothing read or written) unless the sender
    is one of the loop's maintainers and the text is exactly ``continue <run id>``."""
    match = REPLY.fullmatch(text or "")
    if match is None or str(sender or "").strip().lower() not in config.maintainers(loop):
        return None
    return run(loop, sup, match.group(1))
