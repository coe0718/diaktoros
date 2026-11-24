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
import time

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
    """The heads one PR's record entry names, oldest request first (``_requested_at``)."""
    return list(_requested_at(entry))


def _requested_at(entry: object) -> dict[str, float]:
    """One PR's record entry as ``{head: when its delivery was requested}``, oldest request
    first. An earlier record's entry (a list of heads, or a single head) has no times: its heads
    count as requested before every timed one, in their order. Anything else names none."""
    if isinstance(entry, str):
        return {entry: 0.0} if entry else {}
    if isinstance(entry, list):
        return {h: 0.0 for h in entry if isinstance(h, str) and h}
    if isinstance(entry, dict):
        timed = {h: float(t) for h, t in entry.items() if isinstance(h, str) and h
                 and isinstance(t, (int, float)) and not isinstance(t, bool)}
        return dict(sorted(timed.items(), key=lambda item: item[1]))
    return {}


def _record_handoff(loop: dict, number: object, head: str, requested_at: float) -> None:
    """Remember that ``head`` of PR ``number`` reached the reviewer gate (#1213), under the
    loop's state lock so two deliveries for different PRs do not lose each other's entry.

    Every acknowledged head is kept (the ``HANDOFF_HEADS_KEPT`` most recently requested), not
    only the latest one to complete (Vex F3 on #1943). Deliveries complete out of order, so the
    heads are ranked by when their delivery was *requested* (``requested_at``), not by when it
    was acknowledged (#1946): late acknowledgements for older heads, however many, never evict
    the head requested last, which is the PR's live head."""
    from . import state as state_mod      # here: state imports much more than this module needs
    st = state_mod.state_for(loop)
    path = _handoffs_path(loop)
    with st.locked():
        record = st.kv_get(path, {})
        record = record if isinstance(record, dict) else {}
        heads = _requested_at(record.get(str(number)))
        heads[head] = max(requested_at, heads.get(head, requested_at))
        kept = sorted(heads.items(), key=lambda item: item[1])[-HANDOFF_HEADS_KEPT:]
        record[str(number)] = dict(kept)
        st.kv_set(path, record)


PRUNE_CONFIRMS_PER_SWEEP = 20


def _confirmed_closed(loop: dict, number: str) -> bool:
    """Whether a fresh read of PR ``number`` says it is closed (merged or not; #1946, Vex F1 on
    #2654). A failed or malformed read, or another PR's answer, confirms nothing: unknown keeps
    the entry."""
    from . import gh
    try:
        wanted = int(number)
    except (TypeError, ValueError):
        return False
    live = gh.pr(loop, wanted)
    return (isinstance(live, dict) and live.get("number") == wanted
            and live.get("state") == "closed")


def prune(loop: dict, st, open_numbers) -> list[str]:
    """Drop the handoff record of every PR no longer open (#1946), as ``conflict_person.prune``
    does with the open PR numbers the watchdog's sweep already listed; returns those dropped.

    The listing is older than the prune, so a PR opened or reopened since is missing from it
    (Vex F1 on #2654), whether or not a handoff was written for it in between. A PR missing from
    the listing is only a candidate: its entry goes once a fresh read of that PR
    (``_confirmed_closed``) says it is closed, and only if the entry is unchanged since the
    candidates were taken (a handoff written meanwhile keeps it for a later sweep). At most
    ``PRUNE_CONFIRMS_PER_SWEEP`` PRs are read per call; the rest wait for a later sweep."""
    from . import state as state_mod
    keep = {str(n) for n in open_numbers}
    current = state_mod.read_record(st, HANDOFFS)
    candidates = [k for k in current if k not in keep][:PRUNE_CONFIRMS_PER_SWEEP]
    closed = {k: current[k] for k in candidates if _confirmed_closed(loop, k)}
    if not closed:
        return []
    path = st.dir / HANDOFFS
    with st.locked():
        record = st.kv_get(path, {})
        if not isinstance(record, dict):
            return []
        dropped = [k for k, v in closed.items() if k in record and record[k] == v]
        if dropped:
            st.kv_set(path, {k: v for k, v in record.items() if k not in dropped})
    return dropped


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
    requested_at = time.time()            # ranks this head among the PR's handoffs (#1946)
    sent = routes.fire(route, "pull_request", review_requested(loop, pr, head, sender), tag,
                       loop.get("host"))
    if sent:
        try:
            _record_handoff(loop, pr.get("number"), head, requested_at)
        except Exception as exc:          # the delivery happened; only explain learns less
            from .util import log
            log(f"#{pr.get('number')} @ {str(head)[:7]}: review handoff not recorded: "
                f"{type(exc).__name__}: {exc}")
    return sent
