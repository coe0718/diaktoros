"""Start a review that GitHub will not announce (#532).

A fixer's review request is what wakes the reviewer gate: GitHub sends ``review_requested``.
But GitHub sends nothing when the reviewer is *already* requested, which is the case whenever the
fixer pushes before the reviewer's first verdict (a CI-fix or conflict turn): that request is
still pending, so asking again changes nothing and no review ever starts.

The host then delivers the event itself: the same signed ``review_requested`` payload, through
the reviewer's own route and gate, so every eligibility rule, cap and dedup is the gate's as for
a real delivery. The watchdog's drain sends the same event for a queued review.
"""
from __future__ import annotations

import json

from . import config, routes


def review_requested(loop: dict, pr: dict, head: str, sender: str) -> dict:
    """The ``pull_request`` / ``review_requested`` payload for ``pr`` at ``head``."""
    number = pr.get("number")
    short = {"number": number, "draft": False,
             "base": {"ref": (pr.get("base") or {}).get("ref") or loop["base"]},
             "user": {"login": ((pr.get("user") or {}).get("login") or "")},
             "head": {"sha": head, "ref": (pr.get("head") or {}).get("ref")},
             "title": pr.get("title", ""), "html_url": pr.get("html_url", "")}
    return {"repository": {"full_name": loop["repo"]}, "action": "review_requested",
            "requested_reviewer": {"login": loop["reviewer_seat"]},
            "sender": {"login": sender}, "number": number, "pull_request": short}


def already_requested(loop: dict, pr: object) -> bool:
    """Whether the reviewer seat is a pending requested reviewer on ``pr`` (so a request again
    makes GitHub send no event)."""
    if not isinstance(pr, dict):
        return False
    seat = str(loop.get("reviewer_seat") or "").lower()
    pending = pr.get("requested_reviewers")
    return bool(seat) and isinstance(pending, list) and any(
        isinstance(user, dict) and str(user.get("login") or "").lower() == seat
        for user in pending)


def app_handoff_delivered(loop: dict, watch: dict, number: int, head: str) -> bool:
    """Whether the host handed ``head`` of PR ``number`` to an App reviewer's gate (#1213).

    GitHub never lists an App reviewer in ``requested_reviewers`` (#1163), so for an App seat this
    is what a pending request is: the fixer's ``request_review`` the broker audited at this head
    (``broker.perform`` writes that line before the host delivers it to the gate), or the
    watchdog sweep's delivery of it (``review_kicks`` in the loop's watch state). It says the
    handoff was made, not that the gate's route accepted it; the caller ends it at a verdict, as
    GitHub ends a request. Always False for a PAT reviewer, and for an unreadable audit.
    An issue fix's PR is not covered: its request is audited with no head (its ``opened`` wakes
    the gate), and ``hermes dk review`` writes no audit line either."""
    if not config.reviewer_is_app(loop) or not head:
        return False
    kicks = watch.get("review_kicks") if isinstance(watch, dict) else None
    if isinstance(kicks, dict) and kicks.get(str(number)) == head:
        return True
    try:
        text = (config.state_dir(loop) / "broker-audit.jsonl").read_text()
    except (OSError, UnicodeDecodeError):
        return False
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue                    # a torn line is no handoff
        if (isinstance(record, dict) and record.get("operation") == "request_review"
                and record.get("repo") == loop.get("repo") and record.get("pr") == number
                and record.get("head") == head):
            return True
    return False


def kick(loop: dict, pr: dict, head: str, sender: str, tag: str) -> bool:
    """Deliver the reviewer gate the ``review_requested`` GitHub did not send. True when the
    gateway acknowledged it; the gate still decides whether a review starts."""
    route = ((loop.get("seats") or {}).get("reviewer") or {}).get("route")
    if not route:
        return False
    return routes.fire(route, "pull_request", review_requested(loop, pr, head, sender), tag,
                       loop.get("host"))
