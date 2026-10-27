"""Start the runs that queued while a seat was at capacity — the watchdog's drain (#1047).

One implementation for the watchdog's sweep, ``watchdog.py --drain`` and the host's confirmed
drain (``one_shot_action.drain_confirmed``). A queued entry is a request for exactly the head it
was observed at: each one is re-checked against live GitHub before its route is woken, and the
gate the route reaches decides the turn. Nothing here merges, pushes or reviews.

``drain`` can bind a confirmed queue snapshot (``expected``, from ``state.queue_snapshot``) and
report per entry what it did (``report``): ``started`` (the sweep's: the gate took the entry),
``sent`` (a confirmed drain's: the gate answered the POST; whether a run started is the run
ledger's), ``left`` (still queued), ``dropped`` (no longer a valid request), ``uncertain`` (a
POST that may have been sent) or ``stale`` (the queue was not the one confirmed, so nothing
happened).
"""
from __future__ import annotations

import time
import uuid

from . import config, gate, gh, review_kick, routes, transition
from . import state as state_mod
from .util import log


def drain(loop: dict, st: state_mod.LoopState, seat: str, quiet: bool = False, *,
          expected: list | None = None, report: list | None = None) -> int:
    """Start whatever queued while a seat was at capacity. Up to the free slots; the claim holds it.

    ``expected`` (#1047), when given, is the confirmed queue snapshot: if the live queue is not
    exactly that, nothing is started, dropped or pruned, and ``report`` gets one ``stale``
    entry. ``report``, when given, receives one ``{"key", "number", "outcome", "why"}`` per
    queued entry looked at. Returns how many runs were started."""
    def note(key, outcome, why):
        if report is not None:
            report.append({"key": key, "number": _queue_number(key), "outcome": outcome,
                           "why": why})
    if expected is not None and state_mod.queue_snapshot(st, seat) != expected:
        note("", "stale", "the queue changed since it was confirmed — nothing started")
        return 0
    # Prune *before* the empty-queue return: an expired claim is dead whether or not anything
    # is queued behind it, and this is the sweep's one chance to drop it from locks.json. Left
    # pruning to the queue path, a dead mark would be reported as stuck by every later sweep
    # and never leave the file.
    live = st.active(seat)
    # The same prune for the queue: an entry whose ``at`` cannot be read is dropped, not sorted,
    # so one junk entry never stops the well-formed ones behind it (#80).
    for key in st.queue_drop_unreadable(seat):
        log(f"drain: {seat} queue entry {key} is unreadable (no numeric 'at') — dropped")
    queued = st.queue_items(seat)
    items = {k: v for k, v in (queued if isinstance(queued, dict) else {}).items()
             if state_mod.mark_at(v) is not None}
    if expected is not None:
        # Only what was confirmed (#1407 review, F1): an entry queued after the check is never
        # woken by this drain, whatever the queue holds by now. One that changed or left the
        # queue since the check is reported, never read as an empty queue (F5).
        items = {k: v for k, v in items.items() if _confirmed(expected, k, v)}
        _note_rest(note, sorted(row[0] for row in expected if row[0] not in items),
                   "the entry changed or left the queue since it was confirmed")
    if not items:
        return 0
    capacity = config.seat_concurrency(loop, seat)
    free = capacity - len(live)
    if free <= 0:
        if not quiet:
            held = ", ".join(f"{k} ({int((time.time() - state_mod.mark_at(v)) / 60)}m)"
                             for k, v in sorted(live.items()))
            print(f"{seat} is at capacity ({len(live)}/{capacity}: {held}) — "
                  f"{len(items)} request(s) queued")
        for key in sorted(items):
            note(key, "left", f"the {seat} seat is at capacity ({len(live)}/{capacity})")
        return 0
    started = 0
    order = sorted(items, key=lambda k: state_mod.mark_at(items[k]))
    for index, key in enumerate(order):
        if key in live:
            # A free *other* slot is not permission to wake this PR twice.
            note(key, "left", "this PR's run already holds the seat")
            continue
        outcome, why, fire = _drain_check(loop, st, seat, key, items[key], bound=expected is not None)
        if fire is None:
            note(key, outcome, why)
            continue
        entry = items[key]
        # Every drain claims its entry before the POST (#1407): one compare-and-swap, so an
        # entry that changed since the checks, or that another drain has claimed or sent, is
        # never sent again. No lock is held across the POST (the gate takes it to answer).
        claimed = dict(entry, drain={"at": time.time(), "id": uuid.uuid4().hex})
        if not st.queue_swap_if(seat, key, entry, claimed):
            note(key, "left", "the entry changed since it was confirmed" if expected is not None
                 else "the entry changed during the checks")
            continue
        if _wake(loop, st, seat, key, claimed, fire, quiet, note, give_back=entry,
                 bound=expected is not None):
            started += 1
            if started < free:
                continue
            _note_rest(note, order[index + 1:], "the free slots are used")
        else:
            # Known pre-POST failure remains retryable; a potentially sent POST
            # without a gate acknowledgement requires operator inspection.
            _note_rest(note, order[index + 1:], "the drain stopped at an unanswered POST")
        break
    return started


def _wake(loop: dict, st: state_mod.LoopState, seat: str, key: str, entry: dict, fire: tuple,
          quiet: bool, note, give_back: dict | None = None, bound: bool = False) -> bool:
    """POST the queued request to the seat's route; True when the gate took it (the run is
    queued). False stops the drain: the POST was not sent, or may have been sent unanswered.
    ``entry`` is the drain's claim; ``give_back``, the entry it replaced, is restored if
    nothing was sent (#1407). A ``bound`` (confirmed) drain reports ``sent``, never
    ``started``: the gate answered this POST, but nothing ties a run in the ledger to it (the
    ledger keys a turn by repo, PR, head and seat, not by delivery), so whether a run started
    is the run ledger's to say (``status``, ``explain``)."""
    number, head, event, payload, tag = fire
    attempted = False

    def mark_attempt() -> None:
        nonlocal attempted
        attempted = True

    def clear_attempt() -> None:
        nonlocal attempted
        attempted = False

    posted = routes.fire(loop["seats"][seat]["route"], event, payload, tag,
                         loop.get("host"), on_attempt=mark_attempt,
                         on_unsent=clear_attempt)
    # HTTP 2xx only acknowledges webhook receipt: the gate may have emitted [SILENT]
    # because no private runtime exists. Only the gate can remove its observed queue
    # entry, after Supervisor.enqueue succeeds. Never pop a replacement here.
    current = st.queue_items(seat).get(key)
    if posted and current is None and bound:
        st.note(f"drained {seat} for {key}")
        note(key, "sent", f"the gate answered the request for head {head[:7]}; whether a run "
                          "started is the run ledger's (status)")
        return True
    if posted and current is None:
        st.note(f"drained {seat} for {key}")
        if not quiet:
            print(f"{seat}: started the queued run for PR #{number} (head {head[:7]})")
        note(key, "started", f"the gate queued the run for head {head[:7]}")
        return True
    if posted and current is not None and current != entry:
        # A new request replaced the claimed entry during the POST: whether the gate took
        # this one is unknown, and the new one is the gate's to decide in its own turn.
        note(key, "uncertain", "a new request replaced it during the POST — the gate decides it")
        return False
    if current == entry and (attempted or posted):
        st.queue_replace_if(seat, key, entry, entry["head"], entry.get("url", ""),
                            "route delivery uncertain — manual reconciliation required")
        note(key, "uncertain", "the route POST may have been sent without the gate's answer")
    else:
        if give_back is not None:
            st.queue_swap_if(seat, key, entry, give_back)     # never sent: no longer claimed
        note(key, "left", "the route POST was not sent")
    return False


# A confirmed drain's claim older than this was left by a drain that stopped between the claim
# and its POST's answer: it may have been sent, so it is an unverified POST from then on.
DRAIN_CLAIM_S = 600.0


def _in_flight(st: state_mod.LoopState, seat: str, key: str, entry: dict):
    """An entry another (confirmed) drain has claimed: left alone while it is sending; once the
    claim is stale, marked an unverified POST, as the drain itself marks an unanswered one."""
    at = entry["drain"].get("at")
    if isinstance(at, (int, float)) and time.time() - at < DRAIN_CLAIM_S:
        return "left", "a confirmed drain is sending it", None
    st.queue_replace_if(seat, key, entry, entry.get("head", ""), entry.get("url", ""),
                        "route delivery uncertain — manual reconciliation required")
    log(f"drain: {key} was claimed by a drain that never answered — manual reconciliation required")
    return "left", "an unverified POST — manual reconciliation required", None


def _confirmed(expected: list, key: str, entry: dict) -> bool:
    """Whether this queue entry is one the operator confirmed: same key, head and time."""
    return [str(key), str((entry or {}).get("head") or ""), state_mod.mark_at(entry)] in expected


def _changed():
    """A conditional removal lost its compare (#1407 review, F6): the entry was replaced while
    it was being checked, so the newer request stays queued and nothing is reported dropped."""
    return "left", "the entry changed during the checks", None


def _queue_number(key: str):
    try:
        return int(str(key).split("#")[-1])
    except (TypeError, ValueError):
        return None


def _note_rest(note, keys, why: str) -> None:
    for key in keys:
        note(key, "left", why)


def _drain_check(loop: dict, st: state_mod.LoopState, seat: str, key: str, entry: dict,
                 bound: bool = False):
    """Whether one queued entry is still a request to wake ``seat``: ``(outcome, why, fire)``.

    ``fire`` is ``(number, head, event, payload, tag)`` when the route should be woken;
    otherwise None, and ``outcome`` is ``left`` or ``dropped`` with the reason. The checks,
    their order and their log lines are the drain's own."""
    if str(entry.get("reason") or "").startswith("route delivery uncertain —"):
        log(f"drain: {key} has an unverified POST — manual reconciliation required")
        return "left", "an unverified POST — manual reconciliation required", None
    if isinstance(entry.get("drain"), dict):
        return _in_flight(st, seat, key, entry)
    if config.is_fixer_push_hold(entry) and not config.unattended_fixer_push_enabled(loop):
        # Held for the operator, not for capacity: nothing to wake until the loop opts in.
        # Once it has, this entry drains like any other — the fixer gate re-checks the live
        # verdict and admits a *new* run under the new policy (no older run is upgraded).
        return "left", "held until unattended fixer pushes are on", None
    number = _queue_number(key)
    if number is None:
        if not st.queue_pop_if(seat, key, entry):
            return _changed()
        return "dropped", "the queue key names no PR", None
    held = st.held_by_other(seat, key)
    if held:
        # The other seat owns this PR. Its handoff (or its slot expiring) is what frees it —
        # the poller must not start a run on top of it, or it would just re-queue.
        log(f"drain: the {held} seat holds {key} — left queued")
        return "left", f"the {held} seat holds this PR", None
    if number in st.pr_holds():
        log(f"drain: #{number} is held by a maintainer — left queued")   # #477
        return "left", "a maintainer holds this PR", None
    pr = gh.pr(loop, number)
    if not isinstance(pr, dict) or not pr:
        log(f"drain: PR #{number} unreadable — left queued")
        return "left", "the PR could not be read", None
    if pr.get("state") != "open":
        if not st.queue_pop_if(seat, key, entry):
            return _changed()
        log(f"drain: PR #{number} is {pr.get('state')} — dropped from queue")
        return "dropped", f"the PR is {pr.get('state')}", None
    head = (pr.get("head") or {}).get("sha") or ""
    if not head:
        log(f"drain: PR #{number} head unreadable — left queued")
        return "left", "the PR's head could not be read", None
    if entry.get("head") != head:
        # A queued event is authorization for exactly its observed head. Drop it;
        # a new webhook for the new SHA must be evaluated through the normal gate.
        if not st.queue_pop_if(seat, key, entry):
            return _changed()
        log(f"drain: PR #{number} moved since queued — stale head dropped")
        return "dropped", "the PR moved since it was queued", None
    if gate.is_declined_hold(entry):
        # The gate answered and declined (a duplicate of a terminal turn): nothing to
        # replay, and not an unverified POST. A new head or a closed PR clears it above.
        log(f"drain: {key} was declined by the gate ({entry.get('reason')}) — left as is")
        return "left", "the gate declined it", None
    return _pr_check(loop, st, seat, key, entry, number, pr, head, bound)


def _pr_check(loop: dict, st: state_mod.LoopState, seat: str, key: str, entry: dict,
              number: int, pr: dict, head: str, bound: bool = False):
    """The checks on an open PR at the queued head: retargets, eligibility, then the verdicts.
    A ``bound`` (confirmed) drain never reconciles a retarget: that starts a fresh review and
    rewrites both seats' queues, which no confirmation of one seat's queue covers (#1407)."""
    base = (pr.get("base") or {}).get("ref") or ""
    stacked = (st.watch().get("stacked_wait") or {}).get(str(number))
    if isinstance(stacked, dict) and stacked.get("base") != base and bound:
        return "left", "retargeted since it was queued — the watchdog's sweep reconciles it", None
    if isinstance(stacked, dict) and stacked.get("base") != base:
        if base == loop["base"] and transition.record(loop, st, number, head, base):
            status, detail = transition.start_fresh_review(loop, st, number, live=pr)
            log(f"drain: PR #{number} retarget: fresh review {status} ({detail})")
        # The exact entry, not any at that head (#1407 review, F6): a newer same-head request
        # queued meanwhile is kept for its own turn.
        if entry.get("head") != stacked.get("head") or not st.queue_pop_if(seat, key, entry):
            return _changed()
        log(f"drain: PR #{number} retargeted since stacked observation — stale request dropped")
        return "dropped", "the PR was retargeted since it was queued", None
    boundary = transition.hold(st, number, head)
    if boundary and transition.baseline_missing(boundary):
        if not st.queue_pop_if(seat, key, entry):
            return _changed()
        log(f"drain: PR #{number} same-head retarget held — {transition.MISSING_BASELINE}")
        return "dropped", "a same-head retarget hold", None
    author = ((pr.get("user") or {}).get("login") or "").lower()
    # The reviewer serves review-only authors too; the fixer seat only the loop's fixers (#191).
    served = config.reviewed_authors(loop) if seat == "reviewer" else set(loop["fixers"])
    if pr.get("draft") or base != loop["base"] or author not in served:
        if not st.queue_pop_if(seat, key, entry):
            return _changed()
        log(f"drain: PR #{number} is not a {seat}'s PR on {loop['base']} — dropped")
        return "dropped", f"not a {seat}'s PR on {loop['base']}", None
    # A held head's queue entry is post-boundary (record() dropped the old ones); its
    # checks still see only host-receipted post-boundary reviews.
    reviews = transition.effective_reviews(loop, st, number, head, gh.reviews(loop, number))
    if not isinstance(reviews, list):
        log(f"drain: cannot read a valid review list or receipts for #{number} — left queued")
        return "left", "the reviews could not be read", None
    short = {"number": number, "draft": False, "base": {"ref": base},
             "user": {"login": author}, "head": {"sha": head, "ref": (pr.get("head") or {}).get("ref")},
             "title": pr.get("title", ""), "html_url": pr.get("html_url", "")}
    return _verdict_check(loop, st, seat, key, entry, number, head, reviews, short)


def _verdict_check(loop: dict, st: state_mod.LoopState, seat: str, key: str, entry: dict,
                   number: int, head: str, reviews: list, short: dict):
    """The fixer wakes only for a live changes-requested verdict; the reviewer only for an
    unreviewed head. Returns the route request to fire, or why not."""
    if seat == "fixer":
        # Unknown chronology is not proof that the queued verdict was superseded.
        # Any changes-requested at this head is not enough: a later approval (or an
        # undatable verdict) at the same head means there is no fix to order.
        latest = gate.latest_effective_review_at_head(reviews, loop, head)
        if latest is None:
            log(f"drain: latest verdict at {head[:7]} of #{number} unknown — left queued")
            return "left", "the latest verdict at this head is unknown", None
        if gh.review_state(latest) != "CHANGES_REQUESTED":
            if not st.queue_pop_if(seat, key, entry):
                return _changed()
            log(f"drain: latest verdict at {head[:7]} of #{number} is not changes-requested — dropped")
            return "dropped", "the latest verdict is not changes-requested", None
        payload = {"repository": {"full_name": loop["repo"]}, "action": "submitted",
                   "review": latest, "pull_request": short,
                   "sender": latest.get("user") or {}}
        return "", "", (number, head, "pull_request_review", payload, f"drain-fix-{number}")
    if gate.reviewed_at_head(reviews, loop, head):
        if not st.queue_pop_if(seat, key, entry):
            return _changed()
        log(f"drain: #{number} head {head[:7]} already reviewed — dropped")
        return "dropped", "this head is already reviewed", None
    payload = review_kick.review_requested(loop, short, head, loop["fixers"][0])
    return "", "", (number, head, "pull_request", payload, f"drain-review-{number}")
