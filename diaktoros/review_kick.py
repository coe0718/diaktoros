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


HANDOFFS = "review-handoffs.json"


def _handoffs_path(loop: dict):
    """The loop's record of the head of each PR whose review the gateway acknowledged."""
    return config.state_dir(loop) / HANDOFFS


HANDOFF_HEADS_KEPT = 16


def _acknowledged_heads(entry: object) -> list[str]:
    """The heads one PR's record entry names, oldest first: a list, or a single head as an
    earlier record wrote it. Anything else names none."""
    if isinstance(entry, str):
        return [entry] if entry else []
    if isinstance(entry, list):
        return [h for h in entry if isinstance(h, str) and h]
    return []


def _record_handoff(loop: dict, number: object, head: str) -> None:
    """Remember that ``head`` of PR ``number`` reached the reviewer gate (#1213), under the
    loop's state lock so two deliveries for different PRs do not lose each other's entry.

    Every acknowledged head is kept (the last ``HANDOFF_HEADS_KEPT`` of them), not only the
    latest one to complete (Vex F3 on #1943): deliveries can complete out of order, and a late
    acknowledgement for an older head must not erase the current head's."""
    from . import state as state_mod      # here: state imports much more than this module needs
    st = state_mod.state_for(loop)
    path = _handoffs_path(loop)
    with st.locked():
        record = st.kv_get(path, {})
        record = record if isinstance(record, dict) else {}
        heads = [h for h in _acknowledged_heads(record.get(str(number))) if h != head]
        record[str(number)] = (heads + [head])[-HANDOFF_HEADS_KEPT:]
        st.kv_set(path, record)


def app_handoff_delivered(loop: dict, number: int, head: str) -> bool:
    """Whether the host's delivery of ``head`` of PR ``number`` reached an App reviewer's gate
    (#1213). GitHub never lists an App reviewer in ``requested_reviewers`` (#1163), so for an
    App seat this is what a pending request is.

    Only a delivery the gateway acknowledged counts: ``kick`` records it after ``routes.fire``
    answers 2xx, whoever called it (the broker's ``request_review``, the watchdog sweep,
    ``hermes dk continue``). A request the broker audited but could not deliver is no pending
    request, so ``explain`` keeps saying none exists until the sweep's retry lands. The caller
    ends it at a verdict at the head, as GitHub ends a request. Always False for a PAT reviewer
    and for an unreadable record (unknown is never a request). ``hermes dk review`` and the
    watchdog's queue drain do not go through ``kick``, so their deliveries are not seen."""
    if not config.reviewer_is_app(loop) or not head:
        return False
    try:
        record = json.loads(_handoffs_path(loop).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and head in _acknowledged_heads(record.get(str(number)))


def kick(loop: dict, pr: dict, head: str, sender: str, tag: str) -> bool:
    """Deliver the reviewer gate the ``review_requested`` GitHub did not send. True when the
    gateway acknowledged it; the gate still decides whether a review starts. An acknowledged
    delivery is recorded for ``app_handoff_delivered`` (#1213); a failed one is not."""
    route = ((loop.get("seats") or {}).get("reviewer") or {}).get("route")
    if not route:
        return False
    sent = routes.fire(route, "pull_request", review_requested(loop, pr, head, sender), tag,
                       loop.get("host"))
    if sent:
        try:
            _record_handoff(loop, pr.get("number"), head)
        except Exception as exc:          # the delivery happened; only explain learns less
            from .util import log
            log(f"#{pr.get('number')} @ {str(head)[:7]}: review handoff not recorded: "
                f"{type(exc).__name__}: {exc}")
    return sent
