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

from . import routes


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


def kick(loop: dict, pr: dict, head: str, sender: str, tag: str) -> bool:
    """Deliver the reviewer gate the ``review_requested`` GitHub did not send. True when the
    gateway acknowledged it; the gate still decides whether a review starts."""
    route = ((loop.get("seats") or {}).get("reviewer") or {}).get("route")
    if not route:
        return False
    return routes.fire(route, "pull_request", review_requested(loop, pr, head, sender), tag,
                       loop.get("host"))
