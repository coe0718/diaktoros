#!/usr/bin/env python3
"""Triage gate (#213) — decides whether a new issue is queued for the isolated triage turn.

Route this at the triage profile, on the ``issues`` event. Only ``opened`` counts, and only for
an issue whose author is in the loop's ``triage.authors``: anyone else's issue is dropped here,
before any model sees its text (spam and prompt injection on a public repo stop at the gate).
The issue is re-read from GitHub first; one that is closed, is a pull request, changed author,
or already carries a label from the triage list (a person got there first) is left alone.

It also hands an issue to the fixer (#214): when a maintainer (``triage.maintainers``) applies
``triage.fix_label`` to an open issue by an allowlisted author, and the repository's unattended
fixer pushes are on, one isolated issue-fix turn is queued from the base branch's current commit.
Only a person's ``labeled`` event counts, never the triage seat's own labels: ``fix_label`` is
not one of the labels triage may apply.

stdin : a GitHub webhook payload
stdout: ``[SILENT]`` (an eligible issue queues an isolated turn)
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from review_loop import config, gate, gh  # noqa: E402
from review_loop.util import log, silence  # noqa: E402


def closing_pr(loop: dict, number: int) -> int | None:
    """The number of an open PR whose body says it fixes issue ``number``, else ``None``.

    One listing read with the reader token. Raises ``RuntimeError`` when the listing is unknown.
    """
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


def _skip_comment(loop: dict, number: int, pr: int) -> None:
    """Say once, as the triage login, why the issue is not handed to the fixer (best effort)."""
    login = config.triage_login(loop)
    if not login:
        return
    gh.api(loop, f"/repos/{loop['repo']}/issues/{number}/comments", method="POST",
           body={"body": f"PR #{pr} already fixes this; not handing it to the fixer."},
           login=login)


def fix(loop: dict, payload: dict) -> None:
    """``labeled`` with the fix label, by a maintainer: queue one issue-fix turn (#214)."""
    triage = loop["triage"]
    label = str((payload.get("label") or {}).get("name") or "")
    if not triage.get("fix_label") or label.casefold() != triage["fix_label"].casefold():
        silence(f"issues labeled {label or '(no label)'}: not the fix label")
    issue = payload.get("issue")
    if not isinstance(issue, dict) or type(issue.get("number")) is not int or issue["number"] < 1:
        silence("payload has no issue number")
    number = issue["number"]
    sender = str((payload.get("sender") or {}).get("login") or "").lower()
    if sender not in triage.get("maintainers", []):
        silence(f"issue #{number}: {sender or 'an unknown account'} applied the fix label, and "
                "is not in triage.maintainers")
    if not config.issue_fixes_enabled(loop):
        silence(f"issue #{number}: issue fixes need unattended fixer pushes on — "
                f"{config.fixer_push_enable_command(loop)}")
    from review_loop import run_supervisor
    try:
        run_supervisor.issue_fix_issue(loop, number)
    except Exception as exc:
        silence(f"issue #{number} not handed to the fixer: {exc}")
    try:
        existing = closing_pr(loop, number)
    except RuntimeError as exc:
        silence(f"issue #{number} not handed to the fixer: {exc}")
    if existing is not None:
        _skip_comment(loop, number, existing)
        silence(f"issue #{number}: open PR #{existing} already fixes it; not handed to the fixer")
    # #324: a finding a seat filed from a PR that is still open is about code only there.
    from review_loop import fix_hold, state as state_mod
    try:
        origin = fix_hold.origin_pr(loop, number)
    except Exception as exc:
        silence(f"issue #{number} not handed to the fixer: lineage unreadable: {exc}")
    if origin is not None:
        origin_state = fix_hold.pr_state(loop, origin)
        if origin_state is None:
            silence(f"issue #{number} not handed to the fixer: PR #{origin} unreadable")
        if origin_state == "open":
            fix_hold.hold(loop, state_mod.state_for(loop), number, origin)
            silence(f"issue #{number} held until PR #{origin} merges")
        if origin_state == "closed":
            fix_hold._drop_comment(loop, number, origin)
            silence(f"issue #{number}: PR #{origin} closed unmerged; finding is moot")
        state_mod.state_for(loop).fix_hold_drop(number)
    try:
        base, outcome = fix_hold.queue_fix(loop, number)
    except RuntimeError as exc:
        silence(f"issue #{number}: {exc}")
    except Exception as exc:
        reason = f"isolated worker unavailable: {type(exc).__name__}: {exc}"
        _fix_notice(loop, number, base, f"held — {reason}")
        silence(f"issue #{number} fix held: {reason}")
    log(f"issue #{number} handed to the fixer at {base[:7]}: {outcome}")
    if outcome in ("enqueued", "rearmed", "pending"):
        _fix_notice(loop, number, base, f"fix turn queued from {loop['base']} at {base[:7]}")
    silence()


def _fix_notice(loop: dict, number: int, base: str, outcome: str) -> None:
    """The observer's "handed to the fixer" notice (#231): once per issue and base."""
    from review_loop import observer, state as state_mod
    observer.notify(loop, state_mod.state_for(loop), "fixing", number, base, identity=base,
                    outcome=outcome, issue=True)


def main() -> None:
    payload = json.load(sys.stdin)
    if "zen" in payload and "action" not in payload:
        silence("hook ping")
    loop, _st = gate.context(payload)
    triage = loop.get("triage") or {}
    if not triage.get("route"):
        silence("issue triage is off for this loop")
    if payload.get("action") == "labeled":
        fix(loop, payload)
    if payload.get("action") != "opened":
        silence(f"issues {payload.get('action') or '(no action)'}: only a new issue is triaged")
    issue = payload.get("issue")
    if not isinstance(issue, dict) or type(issue.get("number")) is not int or issue["number"] < 1:
        silence("payload has no issue number")
    number = issue["number"]
    author = str((issue.get("user") or {}).get("login") or "").lower()
    if author not in triage["authors"]:
        silence(f"issue #{number} by {author or 'an unknown author'}: not in triage.authors")
    live = gh.api(loop, f"/repos/{loop['repo']}/issues/{number}", login=loop["read_token"])
    if not isinstance(live, dict) or live.get("number") != number:
        silence(f"issue #{number} unreadable (GitHub read failed)")
    if "pull_request" in live:
        silence(f"#{number} is a pull request, not an issue")
    if live.get("state") != "open":
        silence(f"issue #{number} is not open")
    if str((live.get("user") or {}).get("login") or "").lower() != author:
        silence(f"issue #{number} author changed")
    present = {str((label or {}).get("name") or "").casefold() for label in live.get("labels") or []
               if isinstance(label, dict)}
    if present & {name.casefold() for name in triage["labels"]}:
        silence(f"issue #{number} already carries a triage label: a person labelled it")
    try:
        outcome = gate.enqueue_isolated(loop, "triage", number, config.TRIAGE_HEAD,
                                        turn_key="triage")
    except Exception as exc:
        silence(f"issue #{number} triage held: isolated worker unavailable: "
                f"{type(exc).__name__}: {exc}")
    log(f"issue #{number} triage {outcome}")
    silence()


if __name__ == "__main__":
    from review_loop import gate_failures  # noqa: E402
    gate_failures.run("gate_triage", main)
