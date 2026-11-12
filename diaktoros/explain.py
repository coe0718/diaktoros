"""``hermes dk explain``: why a PR is not moving, read-only (#1349 split of ``gate.py``).

It asks the same predicates the gates run (``gate.verdicts``, ``gate.breach_holds``, …) and
re-derives none of them; see the section comment below.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from . import base_merge, config, gate, gate_decisions, gate_failures, gh, pr_hold, review_kick
from . import run_supervisor
from . import drain, explain_next, situation, transition
from . import state as state_mod
from .util import iso_at


# -- explain --------------------------------------------------------------------
#
# "Why is this PR not moving?" is the question an operator actually has at 2am, and no single file
# answers it: half the answer is in GitHub (the head, the verdicts *at that head*, whether an
# approval exists) and half is on disk (who holds the PR, what is queued, what is marked in
# flight, what the watchdog last saw).
#
# So ``explain`` gathers both halves read-only and runs them through the *same* predicates the
# gates run — ``verdicts``, verdict-only ``reviewed_at_head``, ``changes_at_head``,
# ``latest_effective_review_at_head``, ``seat_key``, the seat ledgers, the queue, the breach marker,
# ``hooks_read`` — in the gates' own guard order. It re-derives no rule, so it cannot drift from
# what the loop would do, and it writes nothing: no claim, no queue entry, no drain, no webhook
# POST, no token. Two runs leave GitHub and the state directory byte-for-byte as they were.
#
# The difference from a gate is what happens at a guard. A gate stops there — *silence* — and the
# operator only sees that nothing happened. This walks every guard, reports all of them, and then
# names the one event that would move the PR.

# The vocabulary of ``next.kind``. The suite asserts every conclusion is one of these, so a new
# branch cannot quietly invent a kind nobody is checking for.
# Ledger states of an isolated turn that was started at this head and has not ended cleanly:
# still coming (``run_supervisor.NOT_ENDED``: queued, held, running) or in doubt (``uncertain``).
# Only the first part is a live turn — the one answer the watchdog's ``live_turn`` also gives;
# an uncertain turn will not push by itself, so ``fix_live`` and the sweep both read it as a
# stall, while it still counts as started (no "the gate did not start one" blocker) (#1503).
LIVE_TURN = (*run_supervisor.NOT_ENDED, "uncertain")

EXPLAIN_KINDS = ("review-verdict", "review-request", "fixer-retry", "fixer-push", "author-push", "release",
                 "adjudication", "rearm", "ready", "retry", "wait", "operator", "none")


def _mark_time(entry: dict | None, fallback: float) -> float:
    """A state mark's ``at`` epoch, or the fallback when the mark is malformed or has none.

    Read by ``state.mark_at``, the same rule every other reader of a mark uses (#80).
    """
    at = state_mod.mark_at(entry)
    return at if at else fallback


def _minutes_since(when: float, now: float) -> int:
    return max(0, int((now - when) / 60)) if when else 0


def _adjudication_next(loop: dict, head: str, escalated: bool = False) -> str:
    """The one thing left when the budget is spent: a ruling, or a human if nobody can rule.

    ``escalated``: an operator escalation holds this head below the verdict cap (#1887), so the
    gates stop because of that escalation, not the cap; the action then names the escalation.
    """
    short = head[:7] if head else "?"
    if not (loop.get("adjudicator") or {}).get("route"):
        return ("human adjudication: this loop has no adjudicator route, so the breach marker is "
                "the only record — rule by hand, then merge or close")
    why = ("the operator's escalation stops both gates below the cap" if escalated
           else "both gates stop at the cap")
    return (f"human adjudication: the adjudicator rules at head {short} and reports; nothing else "
            f"fires for this PR ({why})")

def _ruled(loop: dict, number: int, head: str) -> str | None:
    """The adjudicator's verdict at this head, if it has ruled (the watchdog reads the same)."""
    from .run_supervisor import views
    return views.ruling_at(config.host_path("ledger"), loop["repo"], number, head)


def _explain_state(loop: dict, st: state_mod.LoopState, key: str, number: int, head: str,
                   now: float) -> dict:
    """What the loop's own files say about this PR, read without persisting anything.

    Everything here is either a claim on the PR (``held``), a place in line (``queued_seat``), a
    head-level mark, or a loop-level fact; the report shows all of them, because "the seat is free
    and nothing is queued" is as much an answer as a queue entry is.
    """
    c = SimpleNamespace(loop=loop, st=st, key=key, number=number, head=head, now=now)
    _held_seats(c)
    _queue_place(c)
    _inflight_marks(c)
    _escalation(c)
    _watch_lines(c)
    return _state_report(c)


def _held_seats(c) -> None:
    """Who holds the PR (``c.held``), each seat's capacity, and the seat line."""
    c.held: dict[str, dict] = {}
    c.capacity: dict[str, tuple[int, int]] = {}
    for seat in ("reviewer", "fixer"):
        live = c.st.live_locks(seat)
        c.capacity[seat] = gate.seat_capacity(c.loop, c.st, seat)
        entry = live.get(c.key)
        if isinstance(entry, dict):
            c.held[seat] = entry
    c.seat_line = " · ".join(
        f"{seat} holds it ({_minutes_since(_mark_time(entry, 0.0), c.now)}m of ttl "
        f"{-(-config.seat_ttl_s(c.loop, seat=seat, recorded=config.claim_budget(entry)) // 60)}m, since {iso_at(_mark_time(entry, 0.0)) or 'an unrecorded time'})"
        for seat, entry in sorted(c.held.items())) or "nobody holds it"


def _queue_place(c) -> None:
    """The PR's place in each seat's queue, and any queue entry for an older head."""
    queue_bits: list[str] = []
    c.queued_seat = ""
    c.queued_reason = ""
    c.stale_queues: list[str] = []
    for seat in ("reviewer", "fixer"):
        items = c.st.queue_items(seat)
        entry = items.get(c.key)
        if not isinstance(entry, dict):
            continue
        if c.head and entry.get("head") != c.head:
            c.stale_queues.append(f"{seat} queue targets head {str(entry.get('head') or '?')[:7]}, "
                                  f"not current head {c.head[:7]} — the watchdog drops it, not retargets it")
            queue_bits.append(c.stale_queues[-1])
            continue
        order = sorted(items, key=lambda name: _mark_time(items.get(name), 0.0))
        position = order.index(c.key) + 1 if c.key in order else 0
        c.queued_reason = str(entry.get("reason") or "no reason recorded")
        queue_bits.append(f"{seat} {position} of {len(order)} "
                          f"(waiting {_minutes_since(_mark_time(entry, 0.0), c.now)}m) — "
                          f"{c.queued_reason}")
        c.queued_seat = seat
    c.queue_line = " · ".join(queue_bits) or "not queued"


def _inflight_marks(c) -> None:
    """The review/fix marks armed for this head, and the inflight line."""
    marks: list[str] = []
    c.inflight_review = c.inflight_fix = False
    if c.head:
        for label in ("review", "fix"):
            mark = f"{label}:{c.number}:{c.head}"
            if not c.st.inflight(mark):
                continue
            marks.append(f"{label} for head {c.head[:7]} armed "
                         f"{_minutes_since(c.st.inflight_at(mark), c.now)}m ago "
                         f"(ttl {c.st.minutes('inflight_ttl_min'):g}m)")   # the TTL inflight() used
            if label == "review":
                c.inflight_review = True
            else:
                c.inflight_fix = True
    c.inflight_line = " · ".join(marks) or "none"


def _escalation(c) -> None:
    """The breach marker, its delivery status, and the escalation line (or a pending one)."""
    raw_marker = c.st.breach_get(c.number)
    c.marker = raw_marker if isinstance(raw_marker, dict) else {}
    c.delivery_status = gate.breach_delivery_status(c.marker, c.head)
    c.parked = c.delivery_status in {"awaiting-adjudication", "adjudicating"}
    c.escalation_line = (
        f"{c.marker.get('status') or 'recorded'} at head {str(c.marker.get('head') or '?')[:7]} since "
        f"{c.marker.get('at') or 'an unrecorded time'} — {c.marker.get('reason') or 'no reason recorded'}"
        if c.marker else "none")
    standing = bool(c.marker) and c.marker.get("head") == c.head and c.marker.get("status") in gate.STANDING
    if not standing:
        from . import escalate_pending      # #1246: asked for while a run was in flight
        pending = escalate_pending.get(c.st, c.number)
        if pending:
            at = pending.get("at")
            when = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
                    if isinstance(at, (int, float)) else "an unrecorded time")
            # #1264: shown even when an older marker exists (a ruled or superseded head).
            c.escalation_line = (f"pending since {when} ({pending.get('reason')}) — the next "
                                 "changes-requested verdict goes to the adjudicator instead of the "
                                 "fixer; an approval drops it"
                                 + (f" (earlier: {c.escalation_line})" if c.escalation_line != "none"
                                    else ""))


def _watch_lines(c) -> None:
    """The watchdog's last sweep, and the last failed GitHub read or write."""
    watch = c.st.watch()
    if watch.get("last_run"):
        armed_since = iso_at(float(watch.get("armed_since") or 0)) or "unrecorded"
        c.sweep = f"watchdog last swept {watch['last_run']} (armed since {armed_since})"
    else:
        c.sweep = "no watchdog sweep recorded — nothing has read this loop's PRs yet"

    github_bits = []
    blind = watch.get("github_read")
    if isinstance(blind, dict) and blind.get("error"):
        github_bits.append(f"watchdog could not read GitHub as {blind.get('login') or '?'} for "
                           f"{blind.get('sweeps', '?')} sweep(s): {gh.one_line(blind.get('error'), 300)}")
    failure = c.st.github_failure()
    if failure.get("error"):
        at = failure.get("at")
        when = iso_at(float(at)) if isinstance(at, (int, float)) and not isinstance(at, bool) else "?"
        github_bits.append(f"last failed call: {failure.get('where') or '?'} "
                           f"{failure.get('method') or 'GET'} {failure.get('path') or '?'} at {when}: "
                           f"{gh.one_line(failure.get('error'), 300)} — "
                           + ("a gate that hit it treated the PR as unavailable and started nothing"
                              if (failure.get("method") or "GET") == "GET"
                              else gh.write_outcome(str(failure.get("method")),
                                                    str(failure.get("path") or ""),
                                                    failure.get("status")
                                                    if type(failure.get("status")) is int
                                                    else None))
                           + _owner_note(c.loop, failure))
    c.github_line = " · ".join(github_bits) or "no failed GitHub call recorded"


def _state_report(c) -> dict:
    """The dict ``_explain_state`` returns, from the pieces above."""
    return {"seat": c.seat_line, "queue": c.queue_line, "inflight": c.inflight_line,
            "escalation": c.escalation_line, "github": c.github_line, "held": c.held, "queued_seat": c.queued_seat,
            "queued_reason": c.queued_reason, "stale_queues": c.stale_queues,
            "inflight_review": c.inflight_review,
            "inflight_fix": c.inflight_fix, "parked": c.parked, "delivery_status": c.delivery_status,
            "capacity": c.capacity, "marker": c.marker, "sweep": c.sweep}


def _explain_app_ingress(loop: dict, armed) -> str:
    """The armed/paused line for a loop whose events come through its App's webhook (#1239)."""
    via = f"events arrive through {loop.get('ingress_app')}'s webhook"
    if armed is False:
        return (f"PAUSED — {via}, and the loop's pause flag drops them "
                f"(`hermes dk arm --loop {loop['id']}` resumes)")
    if armed is None:   # #1295: the dispatcher drops on an unreadable flag; "paused" is not claimed
        return (f"unknown — {via}, but the loop's pause flag cannot be read or is malformed, so "
                f"the dispatcher drops them and the watchdog starts nothing "
                f"(`hermes dk arm --loop {loop['id']}` rewrites it)")
    return f"armed — {via} (`hermes dk doctor` reads that webhook)"


def _explain_hooks(armed, armed_error: str) -> str:
    """The armed/paused line. Unreadable is its own answer: "paused" is a claim, not a default."""
    if armed is True:
        return "armed — both seat routes are active repo hooks"
    if armed is False:
        return ("PAUSED — seat route(s) without an active repo hook: "
                f"{armed_error or 'unknown'}; those seats cannot receive events")
    return (f"unknown — the repo's hooks could not be read "
            f"({armed_error or 'no reason given'}); 'paused' is not claimed")


def explain_facts(loop: dict, number: int) -> dict:
    """Everything ``explain`` reasons about, read once, with the reason any read failed.

    Read-only by construction: PR/review/hook GETs, and a state directory that is only ever opened for
    reading (``live_locks``, ``inflight_at`` and ``queue_items`` never persist their pruning).
    """
    pr, pr_error = gh.fetch(loop, gh.pr_path(loop, number))
    if pr is None and (pr_error == "HTTP 404" or pr_error.startswith("HTTP 404 ")):
        pr_error = ""  # GitHub hides inaccessible resources behind 404 as well.
    reviews, reviews_error = gh.reviews_read(loop, number)
    armed, armed_error = gate.hooks_read(loop)
    base = (pr.get("base") or {}).get("ref") if isinstance(pr, dict) else None
    chain = situation.resolve(loop, number) if base and base != loop["base"] else None
    if isinstance(chain, situation.Resolution) and chain.identity and isinstance(pr, dict):
        if (chain.identity.head_sha != (pr.get("head") or {}).get("sha") or
                chain.identity.base_ref != base or
                chain.identity.base_sha != (pr.get("base") or {}).get("sha")):
            chain = situation.Resolution("blocked", "PR changed during chain read; retry")
    readiness = (situation.parent_readiness(loop, state_mod.state_for(loop), number, chain)
                 if isinstance(chain, situation.Resolution) and chain.status == "waiting"
                 else (False, "parent chain unverified"))
    # A same-head retarget hold is judged by host receipts only; read them here, so that
    # explain itself still reads nothing. A failed read is reported, never taken as "none".
    receipts, receipts_error = None, ""
    head = ((pr.get("head") or {}).get("sha") if isinstance(pr, dict) else None)
    try:
        if isinstance(head, str) and head and transition.hold(state_mod.state_for(loop), number, head):
            receipts = transition.read_receipts(loop, number, head)
    except Exception as exc:
        receipts_error = f"{type(exc).__name__}: {exc}"
    ro_conflict = None
    if (isinstance(pr, dict) and pr.get("mergeable_state") == "dirty" and isinstance(head, str)
            and head and str(((pr.get("user") or {}).get("login")) or "").lower()
            in config.review_only(loop)):
        try:    # a dry merge in a private repository; nothing is pushed
            from . import review_only_conflict
            ro_conflict = review_only_conflict.assess(loop, number, head, pr)
        except Exception:
            ro_conflict = None
    hold, hold_error = pr_hold.read(loop, number)      # #477
    try:
        breach_marker = state_mod.state_for(loop).breach_get(number)
    except Exception:
        breach_marker = None               # no marker read: an unruled hold, never an error
    shadow = (gate.shadow_hold(loop, number, head, marker=breach_marker)
              if isinstance(head, str) and head else ("", ""))
    from . import shadow_choice
    choice = shadow_choice.explain_line(loop, number, head) if isinstance(head, str) else ""
    # #757: commits that show as a seat's but no seat turn pushed. Report-only, never a blocker.
    stray, stray_error = [], ""
    if isinstance(pr, dict):
        try:
            from . import authorship
            stray, stray_error = authorship.scan(loop, number)
        except Exception as exc:
            stray_error = f"{type(exc).__name__}: {exc}"
    return {"review_only_conflict": ro_conflict, "hold": hold, "hold_error": hold_error,
            "misattributed": stray, "misattributed_error": stray_error,
            "shadow_hold": shadow, "shadow_choice": choice,
            "pr": pr, "pr_error": pr_error, "reviews": reviews, "reviews_error": reviews_error,
            "armed": armed, "armed_error": armed_error, "read_at": time.time(),
            "chain": chain, "parent_readiness": readiness,
            "receipts": receipts, "receipts_error": receipts_error}


def _owner_note(loop: dict, failure: dict) -> str:
    """How ``explain`` names who handles a failed read — only what a ledger still backs."""
    if failure.get("resolved_by"):
        return f" (settled: {failure['resolved_by']})"
    owner = failure.get("owned_by")
    if not owner:
        return ""
    where = f" in {failure['owned_in']}" if failure.get("owned_in") else ""
    state = gate_failures.owner_state(loop, failure)
    if state == "open":
        return f" (tracked as {owner}{where}: its gate-failure line says what happens next)"
    if state == "resolved":
        return f" (tracked as {owner}{where}, which has resolved)"
    if state == "unreadable":
        return (f" (owned by {owner}, but a gate-failure ledger that may hold it cannot be read, "
                f"so no line about it can come from there; the watchdog reports this read "
                f"itself)")
    return (f" (was owned by {owner}, which is no longer in any gate-failure ledger — the ledger "
            f"was moved aside or the entry dropped; the watchdog reports this read itself)")


def gate_failure_line(entry: dict) -> str:
    if entry.get("kind") == gate_failures.CORRUPT:
        if entry.get("corrupt_copy"):
            return (f"gate-failure ledger {entry.get('path')} was unreadable ({entry.get('error')}) "
                    f"and was moved aside to {entry.get('corrupt_copy')}; failures recorded before "
                    f"then (this PR's too) are only in that copy — salvage what you need, then "
                    f"delete it")
        if entry.get("movable", True):
            return (f"gate-failure ledger {entry.get('path')} is unreadable "
                    f"({entry.get('error')}); the next gate failure or watchdog sweep moves it "
                    f"aside, unchanged, for you to salvage")
        return (f"gate-failure ledger {entry.get('path')} is unreadable ({entry.get('error')}) "
                f"and cannot be moved aside automatically ({entry.get('why_not')}): nothing is "
                f"recorded there until you repair or move it yourself, and the failures in it are "
                f"unknown — the watchdog reports failed reads it would have owned itself")
    status = gate_failures.explain_status(entry)
    if entry.get("pr") is None:
        status += " (its payload names no PR, so every PR's explain shows it)"
    if entry.get("held_in"):
        status += (f" (recorded in {entry['held_in']}, the no-loop ledger: this loop's ledger was "
                   f"busy or unwritable when the gate recorded it)")
    return (f"gate failure {entry.get('id')}: {entry.get('gate')} {entry.get('kind')} at head "
            f"{str(entry.get('head') or '?')[:7]} ({entry.get('action') or '?'}), last "
            f"{iso_at(float(entry.get('last_at') or 0))}, {entry.get('attempts')} attempt(s): "
            f"{entry.get('error_type')}: {str(entry.get('error') or '')[:160]} — {status}")


def explain(loop: dict, st: state_mod.LoopState, number: int, facts: dict) -> dict:
    """Why one PR is not moving, and the single event that would move it. Reads nothing itself.

    ``facts`` comes from ``explain_facts`` (or a test). Every guard the live gates run is evaluated
    here in the same order; the first one that names an action decides ``next``, and all of them
    are reported in ``blockers``. Unknowns stay unknown: an unreadable review list is never
    rendered as "0 verdicts".
    """
    c = SimpleNamespace(loop=loop, st=st, number=number, facts=facts)
    _read(c)
    _head_verdicts(c)
    _budget(c)
    _local_state(c)
    _seat_needs(c)
    _blockers(c)
    kind, action = explain_next.next_step(c)
    _state_line(c)
    return _report(c, kind, action)


def _read(c) -> None:
    """Read ``facts`` into ``c``: the PR, its head, base and chain, its author, the review request."""
    c.shadow_held, c.shadow_note = c.facts.get("shadow_hold") or ("", "")      # #1004
    c.shadow_choice = str(c.facts.get("shadow_choice") or "")                  # #1788
    c.now = float(c.facts.get("read_at") or time.time())
    c.pr = c.facts.get("pr") if isinstance(c.facts.get("pr"), dict) else None
    c.reviews = c.facts.get("reviews") if isinstance(c.facts.get("reviews"), list) else None
    c.armed = c.facts.get("armed")
    c.hold = c.facts.get("hold") if isinstance(c.facts.get("hold"), dict) else None
    c.hold_error = str(c.facts.get("hold_error") or "")
    c.pr_error = str(c.facts.get("pr_error") or "")
    c.reviews_error = str(c.facts.get("reviews_error") or "")
    c.armed_error = str(c.facts.get("armed_error") or "")

    c.key = gate.seat_key(c.loop, c.number)
    c.cap = c.loop["cap"]
    c.repo = c.loop["repo"]
    head_data = (c.pr or {}).get("head")
    raw_head = head_data.get("sha") if isinstance(head_data, dict) else None
    c.head = raw_head if isinstance(raw_head, str) else ""
    c.short = c.head[:7] if c.head else "?"
    c.base = str(((c.pr or {}).get("base") or {}).get("ref") or "")
    c.chain = c.facts.get("chain")
    c.resolved = isinstance(c.chain, situation.Resolution)
    c.stacked = bool(c.base and c.base != c.loop["base"])
    c.chain_reason = c.chain.reason if c.resolved else "parent chain not verified"
    if c.stacked and isinstance(c.facts.get("parent_readiness"), tuple):
        c.chain_reason += f"; parent approval: {c.facts['parent_readiness'][1]}"
    c.chain_parents = c.chain.parents if c.resolved else ()
    c.author = str(((c.pr or {}).get("user") or {}).get("login") or "").lower()
    c.stranger = bool(c.author) and c.author not in config.reviewed_authors(c.loop)
    c.served = f"this loop's fixers ({', '.join(c.loop['fixers'])}) or review-only authors"
    c.state = ("unknown" if c.pr is None
               else "merged" if (c.pr.get("merged") or c.pr.get("merged_at"))
               else str(c.pr.get("state") or "unknown"))
    # A PAT reviewer's pending request is GitHub's; an App reviewer's is the host's handoff
    # (``_head_verdicts``, #1213), as GitHub never lists an App there.
    c.request_pending = review_kick.already_requested(c.loop, c.pr)
    c.blockers: list[str] = []
    # A gate that crashed, overran or silenced after a failed read (#75) is not a decision.
    c.gate_failed = [gate_failure_line(entry) for entry in gate_failures.open_for(c.loop, c.number)]
    c.blockers.extend(c.gate_failed)


def _head_verdicts(c) -> None:
    """What the gates conclude about this head: verdicts spent and at head, approval, staleness."""
    # -- what the gates conclude about this head (their predicates, not new ones) -------------
    c.spent: int | None = None
    c.at_head: int | None = None
    c.head_states: list[str] = []
    c.approved = False
    c.reviewed = False
    c.changes: list[dict] = []
    c.boundary = transition.hold(c.st, c.number, c.head) if c.head and not c.stacked else None
    if c.reviews is not None and c.boundary:
        # A post-retarget review list is diagnostic, not an authorization: its commit_id
        # cannot prove the base generation or a dispatched run. Only host-receipted
        # post-boundary reviews count (the same predicate the gates use). Facts without a
        # receipts read attest nothing; an unreadable ledger leaves the verdicts unknown.
        receipts = c.facts.get("receipts", {})
        c.reviews = transition.effective_reviews(c.loop, c.st, c.number, c.head, c.reviews,
                                                 receipts=receipts if isinstance(receipts, dict) else None)
        if c.reviews is None:
            c.reviews_error = ("host review-receipt ledger unreadable: "
                               f"{c.facts.get('receipts_error') or 'no reason given'}")
    c.fresh = (c.boundary.get("fresh_review") if isinstance(c.boundary, dict) else None) or {}
    if c.reviews is not None:
        c.spent = len(gate.verdicts(c.reviews, c.loop))
        if c.head:
            c.latest = gate.latest_effective_review_at_head(c.reviews, c.loop, c.head)
            c.changes = (gate.changes_at_head(c.reviews, c.loop, c.head)
                         if c.latest is not None and gh.review_state(c.latest) == "CHANGES_REQUESTED"
                         else [])
            c.at_head = len(c.changes)
            c.approved = c.latest is not None and gh.review_state(c.latest) == "APPROVED"
            c.reviewed = gate.reviewed_at_head(c.reviews, c.loop, c.head)
            c.head_states = sorted({gh.review_state(r) for r in gate.reviews_at_head(c.reviews, c.loop, c.head)})

    c.stale_line, c.watch = "", c.st.watch()
    # #1213: an App reviewer's request is the handoff the gateway acknowledged at this head
    # (not merely the audited request), pending until a verdict lands there, as GitHub clears
    # a request then. Unread reviews leave it as read: an unknown verdict list proves no
    # request still open.
    if not c.request_pending and c.reviews is not None and not c.reviewed:
        c.request_pending = review_kick.app_handoff_delivered(c.loop, c.number, c.head)
    # #905: approved on the adjudicator's ACCEPT, but GitHub still counts a change request.
    from . import ready                   # here: ready reads explain at import
    c.unlifted = ([text for _, text in ready.acting(c.loop, c.number, c.head, c.reviews,
                                                     getattr(c, "latest", None))]
                  if c.approved and c.reviews is not None and c.head else [])
    c.blockers.extend(c.unlifted)
    if c.approved and c.head:
        from . import stale_approval
        found = stale_approval.record(c.watch, c.number)
        if found and found.get("head") == c.head:
            c.stale_line = stale_approval.line(found)
            c.blockers.append(c.stale_line)


def _budget(c) -> None:
    """The budget line: verdicts spent against the cap, the head's verdict, CI fixes."""
    # -- the budget line ---------------------------------------------------------------------
    c.unread = f"the review list could not be read ({c.reviews_error or 'no reason given'})"
    if c.reviews is None:
        c.budget = f"unknown — {c.unread}; never guessed from a failed read"
    elif c.at_head:
        last = [r for r in c.changes if isinstance(r, dict)][-1]
        c.budget = (f"{c.spent}/{c.cap} verdicts spent · {c.at_head} at head {c.short} — changes requested "
                    f"{last.get('submitted_at') or 'at an unrecorded time'} by "
                    f"{gate.reviewer_login(last) or 'an unrecorded reviewer'}")
    elif c.approved:
        c.budget = (f"{c.spent}/{c.cap} verdicts spent · approved at head {c.short}"
                    + base_merge.said(c.loop, c.latest))
    elif c.head_states:
        c.budget = (f"{c.spent}/{c.cap} verdicts spent · a non-verdict review at head {c.short} "
                    f"({'/'.join(c.head_states)}) — reviewer gate still accepts a fresh request")
    elif c.head:
        c.budget = f"{c.spent}/{c.cap} verdicts spent · nothing at head {c.short}"
    else:
        c.budget = (f"{c.spent if c.spent is not None else 'unknown'}/{c.cap} verdicts spent · "
                    f"head unknown")
    # #539: CI fixes have their own budget per PR, outside the verdict cap.
    if config.fix_ci(c.loop):
        from . import ci_fix
        c.used = len(ci_fix.rows(c.loop["repo"], c.number))
        c.budget += f" · CI fixes {c.used}/{config.ci_fix_cap(c.loop)}"


def _local_state(c) -> None:
    """The loop's own files: holders, queue (and stacked wait), escalation and ruling."""
    # -- local state: who holds it, what waits, what is marked -------------------------------
    c.local = _explain_state(c.loop, c.st, c.key, c.number, c.head, c.now)
    pending_stack = (c.watch.get("stacked_wait") or {}).get(str(c.number))
    if c.stacked and isinstance(pending_stack, dict):
        if (pending_stack.get("head") == c.head and pending_stack.get("base") == c.base and
                pending_stack.get("base_sha") == ((c.pr or {}).get("base") or {}).get("sha") and
                (not c.resolved or
                 (pending_stack.get("status") == c.chain.status and
                  pending_stack.get("reason") == c.chain.reason and
                  (not c.chain.identity or pending_stack.get("identity") == c.chain.identity.key)))):
            stacked_line = (f"stacked visibility queue: {pending_stack.get('status')} — "
                            f"{pending_stack.get('reason')} (no seat run authorized)")
            c.local["queue"] = (stacked_line if c.local["queue"] == "not queued" else
                                c.local["queue"] + " · " + stacked_line)
    c.held = c.local["held"]
    c.queued_seat = c.local["queued_seat"]
    c.queued_reason = c.local["queued_reason"]
    c.inflight_review = c.local["inflight_review"]
    c.inflight_fix = c.local["inflight_fix"]
    c.marker = c.local["marker"]
    c.cap_spent = c.spent is not None and c.spent >= c.cap
    c.adjudicated = bool((c.loop.get("adjudicator") or {}).get("route"))
    # A marker records a past escalation, not a permanent veto. A dismissed
    # verdict can lower the live count, and the reviewer gate then starts a new
    # review even when the marker still says awaiting-adjudication/adjudicating.
    c.parked = c.local["parked"] and (c.cap_spent or gate.escalated(c.marker, c.head))
    # A ruling at this head ends adjudication: the marker stays, and the decision is a person's.
    c.ruled = _ruled(c.loop, c.number, c.head) if c.parked else None
    if c.parked and c.ruled == "ACCEPT" and (((c.marker or {}).get("escalated") or {}).get(
            "by") == gate.SHADOW_ESCALATION or gate.is_ruled_approval(getattr(c, "latest", None),
                                                                     c.loop)):
        # #1004: the adjudicator overruled the shadow review; the reviewer's approval stands.
        # #905: the adjudicator approved on its ACCEPT, so no person is waited for either.
        c.parked, c.ruled = False, None
    # #1004 part 3 (#1353): an upheld shadow review the fixer answers waits on no person; the
    # shadow hold says the fixer answers it, and the next event is the fixer's.
    c.shadow_fix = gate.shadow_fix_owed(c.loop, c.marker, c.head)
    if c.shadow_fix:
        c.parked, c.ruled = False, None


def _seat_needs(c) -> None:
    """Live runs at this head, a dispute, the seat the PR needs next, and the push gate."""
    # A dismissed verdict can lower the live count after a failed POST. The
    # watchdog only retries a pending marker while the cap remains spent.
    c.pending_delivery = (c.local["delivery_status"] == "delivery-pending"
                          and gate.breach_holds(c.marker, c.head, c.spent, c.cap))
    c.stale_held = {seat for seat, entry in c.held.items() if entry.get("head") != c.head}
    # An isolated turn already in the ledger at this head (queued, held for CI, running) is
    # the gate's answer, not a missing one (#509): the run itself is reported as the blocker.
    db = config.host_path("ledger")
    from .run_supervisor import views
    turns = {seat: views.turn_state(db, c.repo, c.number, c.head, seat)
             for seat in ("reviewer", "fixer")} if c.head else {}
    c.ledgered = {seat for seat, turn in turns.items() if turn in LIVE_TURN}
    c.fixer_turn = turns.get("fixer")     # any ledger state: a lock or mark with no run has none
    # Uncertain or ended with no push: not live, whatever lock or mark still stands over it.
    c.fixer_ended = bool(c.fixer_turn) and c.fixer_turn not in run_supervisor.NOT_ENDED
    c.fixer_rearm = explain_next.fixer_retry(c) if c.fixer_ended else ("", "", False)   # the retry plan (#1514)
    c.redeliver = c.fixer_ended and not c.cap_spent and explain_next.redelivery_rearms(c)  # intake's own rule
    # A fix for this head: a lock on an old head cannot finish it (#1481 review).
    # An uncertain fixer turn is no live fix: nothing will push, and the sweep reads it so (#1495).
    c.fix_live = (("fixer" in c.held and "fixer" not in c.stale_held) or c.inflight_fix
                  or c.fixer_turn in run_supervisor.NOT_ENDED)   # the sweep's live_turn (#1503)
    # The fixer's answer to the verdict (#894), as the sweep reads it: there an uncertain turn is
    # not a live one, so it hides no dispute (#1481 review). A verdict at head means a head.
    c.disputed = (bool(c.at_head) and c.fixer_turn not in run_supervisor.NOT_ENDED
                  and views.disputed(db, c.repo, c.number, c.head, c.latest) is not None)
    # Parked for a ruling (or its delivery pending), both gates stop on purpose: no "gate did not
    # start one" line, which would read as something stuck while the ruling is the next event.
    c.ruling = c.parked or c.pending_delivery
    # A review-only author's PR (#191) never needs the fixer and spends no fixer rounds: a verdict
    # at the head is the author's to answer. Its verdicts still have a cap (review_only_cap,
    # default `cap`); past it the loop waits for a maintainer's --another-round (below).
    c.review_only = c.author in config.review_only(c.loop)
    c.needed_seat = ("" if c.at_head and c.review_only else "fixer" if c.at_head
                     else "reviewer" if c.request_pending else "")
    c.used, c.limit = c.local["capacity"].get(c.needed_seat, (0, 0))
    c.full_seat = bool(c.needed_seat and c.used >= c.limit and c.needed_seat not in c.held
                       and not (c.inflight_fix if c.needed_seat == "fixer" else c.inflight_review))
    c.hooks_line = (_explain_app_ingress(c.loop, c.armed) if config.app_ingress(c.loop)
                    else _explain_hooks(c.armed, c.armed_error))
    _push_and_queue(c)


def _push_and_queue(c) -> None:
    """What holds a fixer turn or a queue entry: the push-off policy and the queue entry's kind."""
    # The fixer gate holds a changes-requested verdict while the loop has not opted in to
    # unattended pushes: no fixer turn can start, so the next event is the operator's.
    c.push_off = bool(c.at_head) and not c.review_only and not config.unattended_fixer_push_enabled(c.loop)
    c.push_off_held = c.push_off and "fixer" not in c.held and not c.inflight_fix
    c.fixer_queue_hold = c.queued_seat == "fixer" and c.queued_reason.startswith(config.FIXER_PUSH_HOLD)
    # A queue entry the gate declined (a duplicate of a terminal turn) is a confirmed answer the
    # drain leaves as is (drain.py, gate.is_declined_hold): it waits for no slot, any seat (#1610).
    c.queued_declined = bool(c.queued_seat) and gate.is_declined_hold({"reason": c.queued_reason})
    # An unverified route POST: the drain never re-sends it (drain.is_route_uncertain, #1512).
    c.queued_uncertain = bool(c.queued_seat) and drain.is_route_uncertain({"reason": c.queued_reason})


def _blockers(c) -> None:
    """Every guard, in the gates' order, appended to ``c.blockers``."""
    # -- every guard, in the gates' order, reported instead of silencing ----------------------
    if c.pr is None:
        if c.pr_error:
            c.blockers.append(f"stale/failing GitHub read: {c.pr_error} — the PR's state is unknown, "
                              f"not closed, and its head cannot be read")
        else:
            c.blockers.append(f"GitHub returned no PR #{c.number} in {c.repo} — the number is wrong, or "
                              f"the read token cannot see the repository")
    else:
        if c.state in ("closed", "merged"):
            c.blockers.append(f"the PR is {c.state} — the loop is over here; the closed path reclaims "
                              f"its disk and drops any queue entry for it")
        else:
            _read_blockers(c)
            _gate_blockers(c)
            _run_blockers(c)
            from . import ci_decline        # a CI fix the fixer declined at this head (#1526)
            declined = c.head and ci_decline.line(config.host_path("ledger"), c.repo, c.number,
                                                  c.head, c.short)
            if declined:
                c.blockers.append(declined)


def _read_blockers(c) -> None:
    """Guards on an open PR's reads and shape: hooks, reviews, head, hold, draft, stack."""
    if c.armed is False:
        c.blockers.append(f"paused loop: seat route(s) without an active repo hook: "
                          f"{c.armed_error or 'unknown'}")
    elif c.armed is None:
        c.blockers.append(f"hook state unreadable ({c.armed_error or 'no reason given'}) — "
                          f"'paused' is not claimed: not seeing the hooks is not evidence they "
                          f"are off")
    if c.reviews is None:
        c.blockers.append(f"{c.unread} — the verdict count is unknown, never guessed")
    if not c.head:
        c.blockers.append("PR head missing or malformed — no gate can authorize a run")
    if c.hold:
        c.blockers.append(pr_hold.line(c.hold, c.loop))
    elif c.hold_error:
        c.blockers.append(f"hold state unreadable ({c.hold_error}) — no turn starts until it "
                          "is read")
    if c.pr.get("draft"):
        c.blockers.append("draft PR: the reviewer gate stays silent until ready_for_review")
    if c.stacked:
        c.blockers.append(f"stacked PR: {c.chain_reason} — visible only; no reviewer or fixer "
                          "run is authorized by parent readiness")


def _gate_blockers(c) -> None:
    """Guards where a gate stops on purpose: retarget, stranger, shadow, ruling, cap."""
    # The retarget hold vetoes only while the fresh review is still owed: a host-receipted
    # post-boundary approval at this head ends it, as in `ready` and the gates (#791, #934).
    if c.boundary and not c.approved and transition.baseline_missing(c.boundary):
        c.blockers.append(f"same-head base retarget held: {transition.MISSING_BASELINE}")
    elif c.boundary and not c.approved:
        c.blockers.append("same-head base retarget: pre-retarget reviews, rounds and approvals "
                          "quarantined — fresh review situation; only a host-receipted "
                          "post-boundary review counts (fresh reviewer turn: "
                          f"{c.fresh.get('state') or 'not queued yet'})")
    if c.stranger:
        c.blockers.append(f"the author {c.author} is not one of {c.served}")
    if c.shadow_held:
        c.blockers.append(f"shadow review: {c.shadow_held}")
    if c.ruled:
        c.blockers.append(f"ruled: the adjudicator ruled {c.ruled} at head {c.short}; the "
                          "decision is yours")
    elif c.parked:
        c.blockers.append(f"escalated: the PR is parked awaiting adjudication at head "
                          f"{str(c.marker.get('head') or '?')[:7]} since "
                          f"{c.marker.get('at') or 'an unrecorded time'}")
    elif c.pending_delivery:
        c.blockers.append(f"adjudicator delivery pending at head {c.short} — no ruling is "
                          "promised until the route acknowledges delivery")
    elif c.cap_spent and not c.review_only:
        c.blockers.append(f"{c.spent}/{c.cap} verdicts spent with no approval and no escalation "
                          f"marker — the cap may not have fired (the watchdog reports this "
                          f"shape too)")


def _ended_rearm(c) -> str:
    """What re-arms an ended fixer turn once pushes are on, as its step says: a redelivery waits
    out a live in-flight mark (#1514 F3), a retry does not."""
    marked = " the in-flight fix mark expires" if c.inflight_fix else ""
    if c.redeliver and not marked:
        return "a redelivered verdict re-arms it once the loop opts in"
    if c.fixer_rearm[0]:
        return ("retry re-arms it once the loop opts in"
                + (f" (a redelivered verdict waits until{marked})" if c.redeliver else ""))
    if c.redeliver:
        return f"a redelivered verdict re-arms it once the loop opts in and{marked}"
    return "a redelivered verdict is a duplicate the ledger refuses"


def _run_blockers(c) -> None:
    """Guards on the runs: push-off, capacity, stale queues and locks, a missing run."""
    declined = c.queued_seat == "fixer" and c.queued_declined
    if c.push_off and c.fixer_ended and not c.ruling:
        c.blockers.append(f"{config.FIXER_PUSH_HOLD}: the fixer turn at head {c.short} is "
                          f"{c.fixer_turn} with no fix pushed — "
                          + _ended_rearm(c))
    elif c.push_off and c.fix_live and not c.ruling:
        c.blockers.append(f"{config.FIXER_PUSH_HOLD}: the fixer turn at head {c.short} cannot "
                          f"publish — the worker cancels it before launch, or the broker refuses "
                          f"its push")
    elif c.push_off_held and declined and not c.ruling:
        c.blockers.append(f"{config.FIXER_PUSH_HOLD}: the gate declined the fixer turn for head "
                          f"{c.short} ({c.queued_reason}) — nothing restarts it by itself")
    elif declined and not c.ruling:
        # Pushes on, or a lock/mark over it (#1613): the drain leaves it, a freed slot too.
        c.blockers.append(f"the gate declined the fixer turn for head {c.short} ({c.queued_reason}) "
                          "— a freed fixer slot or a drain leaves that entry as is, so nothing "
                          "restarts it by itself")
    elif c.push_off_held and not c.parked and not c.pending_delivery and not c.cap_spent:
        c.blockers.append(f"{config.FIXER_PUSH_HOLD}: the changes-requested verdict at head "
                          f"{c.short} starts no fixer turn until the loop opts in — "
                          f"`{config.fixer_push_enable_command(c.loop)}`")
    _queue_blockers(c, declined)
    c.blockers.extend(c.local["stale_queues"])
    for seat in sorted(c.stale_held):
        c.blockers.append(f"{seat} lock targets an older head, not current head {c.short} "
                          "— it cannot authorize a verdict or push at this head")
    if c.head_states and not c.reviewed:
        c.blockers.append(f"non-verdict review at head {c.short} ({'/'.join(c.head_states)}) "
                          "does not suppress a fresh reviewer request")
    # At the cap the fixer gate starts no fix by design (gate_fixer: not a work order, #1494). A
    # fixer turn in the ledger, uncertain included, was started: not "did not start one" (#1505).
    if (c.at_head and not c.fix_live and "fixer" not in c.ledgered and not c.queued_seat
            and not c.push_off and not c.review_only and not c.ruling and not c.disputed
            and not c.cap_spent):
        c.blockers.append(f"the changes-requested verdict at head {c.short} has no fix run out "
                          f"— the fixer gate did not start one for that delivery")
    if (c.request_pending and not c.held and not c.inflight_review and not c.at_head
            and not c.approved and not c.ruling and "reviewer" not in c.ledgered):
        c.blockers.append(f"a review request for {c.loop['reviewer_seat']} is pending at head "
                          f"{c.short}, but no review run is out — the reviewer gate did not "
                          f"start one")
    if (c.spent and c.head and not c.reviewed and not c.approved and not c.request_pending and not c.held
            and not c.inflight_review and not c.queued_seat and not c.ruling
            and "reviewer" not in c.ledgered):
        c.blockers.append(f"no review request exists for head {c.short} — GitHub clears a request "
                          f"when a verdict lands, so the reviewer gate stays silent until the "
                          f"fixer asks again")


def _queue_blockers(c, declined: bool) -> None:
    """The queue entry's blocker: a declined entry, a capacity wait, or a full seat. A hold or a
    declined fixer entry already named above adds nothing; one a started turn's push-off blocker
    displaced is still named declined, never capacity (#1613, #1614)."""
    # A push-off blocker above for a started turn (a lock, mark or ledger row) names that turn,
    # not the declined entry, so the entry gets its own line below (#1614).
    started = c.push_off and (c.fixer_ended or c.fix_live) and not c.ruling
    # Parked for a ruling (or a delivery pending), no fixer blocker above names the declined
    # entry, so it gets its own line below too (#2075 review F1).
    if c.fixer_queue_hold or (declined and not started and not c.ruling):
        pass  # a hold or a declined entry is no capacity wait: no freed slot starts it (#1613)
    elif c.queued_seat and gate.is_declined_hold({"reason": c.queued_reason}):
        # A declined entry is never capacity, whatever lock or mark stands beside it (#1614):
        # the gate answered, the drain leaves it, and only a retry or a new head moves it.
        c.blockers.append(f"declined: the gate declined the {c.queued_seat} turn for head "
                          f"{c.short} ({c.queued_reason}) — not capacity; nothing restarts it "
                          "by itself")
    elif c.queued_uncertain:
        c.blockers.append(f"unverified route POST: the {c.queued_seat} entry for head {c.short} "
                          f"({c.queued_reason}) is never re-sent by a sweep or drain")
    elif c.queued_seat:
        c.blockers.append(f"no capacity: queued with the {c.queued_seat} seat — {c.queued_reason}")
    elif c.full_seat:
        c.blockers.append(f"no capacity: {c.needed_seat} seat at capacity {c.used}/{c.limit} "
                          "on other PRs — this PR has no queue entry yet")


def _state_line(c) -> None:
    """The PR's one-line state: state, base, author, head, draft, request, conflicts."""
    if c.pr is None:
        c.state_line = "unknown — the PR itself could not be read"
    else:
        bits = [c.state, f"base {c.base or 'unknown'}", f"author {c.author or 'unknown'}"]
        bits.append(f"head {c.short}" if c.head else "head unread")
        if c.pr.get("draft"):
            bits.append("draft")
        if c.request_pending:
            bits.append(f"review requested from {c.loop['reviewer_seat']}"
                        + (" (handed to its gate by the host: GitHub cannot request an App)"
                           if config.reviewer_is_app(c.loop) else ""))
        if c.pr.get("mergeable_state") == "dirty":
            # #303 stage 1: GitHub cannot merge it as it is; nothing in the loop resolves that yet.
            bits.append(f"CONFLICTS with {c.base or 'its base'} — merge {c.base or 'the base'} into "
                        "the branch")
            facts_ro = c.facts.get("review_only_conflict")
            if c.author in config.review_only(c.loop) and isinstance(facts_ro, dict):
                # The same facts the watchdog's notice carries (review-only, #412).
                from . import review_only_conflict
                bits.append(review_only_conflict.cause(c.loop, facts_ro) + "; "
                            + review_only_conflict.files(facts_ro) + "; run: "
                            + review_only_conflict.commands(c.loop, facts_ro))
        c.state_line = " · ".join(bits)


def _report(c, kind: str, action: str) -> dict:
    """The dict ``explain`` returns, from ``c`` and the next event."""
    return {
        "pr": c.number, "repo": c.repo, "url": gate.pr_url(c.loop, c.number), "state": c.state,
        "head": c.head, "cap": c.cap, "spent": c.spent, "at_head": c.at_head,
        "chain": {"status": c.chain.status if c.resolved else
                  ("unverified" if c.stacked else "direct"), "reason": c.chain_reason if c.stacked else "direct trunk base",
                  "parents": list(c.chain_parents),
                  "identity": c.chain.identity.key if c.resolved and c.chain.identity else ""},
        "approved": c.approved, "stale_approval": c.stale_line, "reviewed": c.reviewed, "request_pending": c.request_pending,
        "read_at": iso_at(c.now),
        "state_line": c.state_line, "budget": c.budget, "seat": c.local["seat"], "queue": c.local["queue"],
        "inflight": c.local["inflight"], "escalation": c.local["escalation"], "hooks": c.hooks_line,
        "sweep": c.local["sweep"],
        "github": c.local.get("github", "no failed GitHub call recorded"),
        "gate_decisions": [gate_decisions.line(e) for e in gate_decisions.for_pr(c.loop, c.number)],
        "gate_failures": c.gate_failed, "blockers": c.blockers, "next": {"kind": kind, "action": action},
        "shadow": {"hold": c.shadow_held, "note": c.shadow_note, "choice": c.shadow_choice},
        "authorship": authorship_lines(c.facts),
        "operator_agents": sorted(config.operator_agents(c.loop)),
    }


def authorship_lines(facts: dict) -> list[str]:
    """What ``explain`` says about commit authorship (#757): one line per commit that shows as a
    seat's but no seat turn pushed, or why that could not be checked. Empty when all is well.
    Never a blocker: nothing here stops a turn."""
    from . import authorship
    lines = [f"misattributed: {authorship.line(entry)}"
             for entry in facts.get("misattributed") or [] if isinstance(entry, dict)]
    if facts.get("misattributed_error"):
        lines.append(f"not checked: {facts['misattributed_error']}")
    return lines
