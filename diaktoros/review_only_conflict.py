"""A review-only author's PR that no longer merges into its base (#191, #412).

The loop never resolves it with a fixer. It says, once per head, what happened and what to run:
the PR(s) merged since this head last merged (read from the base's new commits), the conflicting
files (a dry merge on the host, nothing pushed) and the commands. With ``review_only_update`` on,
the host first tries a clean merge of the base into the head and pushes it with a lease.
"""
from __future__ import annotations

import re

from . import broker, config, safe_push

PR_REF = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+)\b")
SHOWN = 5


def merged_prs(commits) -> list[int]:
    """The PR numbers the base's new commits name (squash ``(#N)`` or a merge commit), oldest
    first. As far as can be told: a commit that names none is not counted."""
    found: list[int] = []
    for _sha, subject in reversed(list(commits or [])):
        match = PR_REF.search(subject or "")
        number = int(next(g for g in match.groups() if g)) if match else 0
        if number and number not in found:
            found.append(number)
    return found


def assess(loop: dict, number: int, head: str, live: dict, remote: str | None = None) -> dict:
    """The facts of one conflicted review-only PR: ``fork``, ``branch``, and (when the host could
    run its dry merge) ``merged``, ``conflicted``, ``workflows``. ``error`` says why it could not."""
    pr_head = live.get("head") or {}
    branch = str(pr_head.get("ref") or "")
    fork = (pr_head.get("repo") or {}).get("full_name") != loop["repo"]
    facts: dict = {"number": number, "head": head, "branch": branch, "fork": fork,
                   "merged": [], "conflicted": [], "workflows": False, "error": ""}
    if fork or not branch:
        facts["error"] = "fork branch: the host cannot read or push it" if fork else "no branch"
        return facts
    try:
        dry = safe_push.dry_merge(loop, branch=branch, head=head, base_ref=loop["base"],
                                  login=loop["read_token"], remote=remote)
    except broker.BrokerDenied as exc:
        facts["error"] = f"dry merge unavailable: {exc}"
        return facts
    facts.update(merged=merged_prs(dry["commits"]), conflicted=dry["conflicted"],
                 workflows=dry["workflows"], clean=dry["clean"])
    return facts


def commands(loop: dict, facts: dict) -> str:
    base, branch = loop["base"], facts.get("branch") or "<branch>"
    return (f"git fetch origin && git switch {branch} && git merge origin/{base}"
            " (resolve, commit) && git push")


def cause(loop: dict, facts: dict) -> str:
    merged = facts.get("merged") or []
    if not merged:
        return f"{loop['base']} moved since this head (no merged PR could be named)"
    names = ", ".join(f"#{n}" for n in merged[:SHOWN])
    more = f" and {len(merged) - SHOWN} more" if len(merged) > SHOWN else ""
    return f"merged into {loop['base']} since: {names}{more}"


def files(facts: dict) -> str:
    conflicted = facts.get("conflicted") or []
    if not conflicted:
        return "conflicting files: unknown" if facts.get("error") else "conflicting files: none"
    if len(conflicted) == 1:
        return f"the conflict is in one file: {conflicted[0]}"
    shown = ", ".join(conflicted[:SHOWN])
    more = f" and {len(conflicted) - SHOWN} more" if len(conflicted) > SHOWN else ""
    return f"conflicting files: {shown}{more}"


def message(loop: dict, author: str, facts: dict, tried: str = "") -> str:
    """The next-turn clause of the conflict notice: cause, files, commands."""
    parts = [f"{author}: merge {loop['base']} into the branch (review-only — no fixer)",
             cause(loop, facts), files(facts)]
    if facts.get("workflows"):
        parts.append(f"{loop['base']} also changed workflow files")
    if facts.get("error"):
        parts.append(facts["error"])
    if tried:
        parts.append(f"the host tried a clean merge and stopped: {tried}")
    parts.append(f"run: {commands(loop, facts)}")
    return "; ".join(parts)


def update(loop: dict, number: int, head: str, facts: dict, remote: str | None = None) -> tuple:
    """With ``review_only_update`` on, try the host's clean merge push. Returns
    ``(new_head, "")`` when pushed, else ``("", why it stopped)`` — the notice then goes out."""
    if not config.review_only_update(loop):
        return "", ""
    if facts["fork"]:
        return "", "a fork branch is notice-only"
    if not config.unattended_fixer_push_enabled(loop):
        return "", "unattended fixer pushes are off"
    if facts.get("workflows"):
        return "", "the base changed workflow files"
    if facts.get("conflicted"):
        return "", "a real conflict: a person resolves it"
    try:
        done = safe_push.update_branch(loop, repo=loop["repo"], number=number, head=head,
                                       branch=facts["branch"], remote=remote)
    except safe_push.NotClean as exc:
        return "", str(exc)
    except broker.BrokerDenied as exc:
        return "", str(exc)
    return done["new_head"], ""
