"""``hermes dk explain``: why a PR is not moving, read-only (#1349 split of ``gate.py``).

It asks the same predicates the gates run (``gate.verdicts``, ``gate.breach_holds``, …) and
re-derives none of them; see the section comment below.
"""
from __future__ import annotations

import time

from . import config, gate, gate_decisions, gate_failures, gh, pr_hold, situation, transition, state as state_mod
from .util import iso_at


# -- explain --------------------------------------------------------------------
#
# "Why is this PR not moving?" is the question an operator actually has at 2am, and no single file
# answers it: half the answer is in GitHub (the head, the verdicts *at that head*, whether an
# approval exists) and half is on disk (who holds the PR, what is queued, what is marked in
# flight, what the watchdog last saw).
#
# So ``explain`` gathers both halves read-only and runs them through the *same* predicates the
# gates run — ``verdicts``, verdict-only ``reviewed_at_head``, ``changes_at_head``, ``approved_at_head``,
# ``seat_key``, the seat ledgers, the queue, the breach marker, ``hooks_read`` — in the gates' own
# guard order. It re-derives no rule, so it cannot drift from what the loop would do, and it writes
# nothing: no claim, no queue entry, no drain, no webhook POST, no token. Two runs leave GitHub and
# the state directory byte-for-byte as they were.
#
# The difference from a gate is what happens at a guard. A gate stops there — *silence* — and the
# operator only sees that nothing happened. This walks every guard, reports all of them, and then
# names the one event that would move the PR.

# The vocabulary of ``next.kind``. The suite asserts every conclusion is one of these, so a new
# branch cannot quietly invent a kind nobody is checking for.
# Ledger states of an isolated turn that is still coming (queued, held, running, or in doubt).
LIVE_TURN = ("pending", "claimed", "launching", "running", "waiting", "uncertain")

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


def _adjudication_next(loop: dict, head: str) -> str:
    """The one thing left when the budget is spent: a ruling, or a human if nobody can rule."""
    short = head[:7] if head else "?"
    if not (loop.get("adjudicator") or {}).get("route"):
        return ("human adjudication: this loop has no adjudicator route, so the breach marker is "
                "the only record — rule by hand, then merge or close")
    return (f"human adjudication: the adjudicator rules at head {short} and reports; nothing else "
            f"fires for this PR (both gates stop at the cap)")

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
    held: dict[str, dict] = {}
    capacity: dict[str, tuple[int, int]] = {}
    for seat in ("reviewer", "fixer"):
        live = st.live_locks(seat)
        capacity[seat] = gate.seat_capacity(loop, st, seat)
        entry = live.get(key)
        if isinstance(entry, dict):
            held[seat] = entry
    seat_line = " · ".join(
        f"{seat} holds it ({_minutes_since(_mark_time(entry, 0.0), now)}m of ttl "
        f"{-(-config.seat_ttl_s(loop, seat=seat, recorded=config.claim_budget(entry)) // 60)}m, since {iso_at(_mark_time(entry, 0.0)) or 'an unrecorded time'})"
        for seat, entry in sorted(held.items())) or "nobody holds it"

    queue_bits: list[str] = []
    queued_seat = ""
    queued_reason = ""
    stale_queues: list[str] = []
    for seat in ("reviewer", "fixer"):
        items = st.queue_items(seat)
        entry = items.get(key)
        if not isinstance(entry, dict):
            continue
        if head and entry.get("head") != head:
            stale_queues.append(f"{seat} queue targets head {str(entry.get('head') or '?')[:7]}, "
                                f"not current head {head[:7]} — the watchdog drops it, not retargets it")
            queue_bits.append(stale_queues[-1])
            continue
        order = sorted(items, key=lambda name: _mark_time(items.get(name), 0.0))
        position = order.index(key) + 1 if key in order else 0
        queued_reason = str(entry.get("reason") or "no reason recorded")
        queue_bits.append(f"{seat} {position} of {len(order)} "
                          f"(waiting {_minutes_since(_mark_time(entry, 0.0), now)}m) — "
                          f"{queued_reason}")
        queued_seat = seat
    queue_line = " · ".join(queue_bits) or "not queued"

    marks: list[str] = []
    inflight_review = inflight_fix = False
    if head:
        for label in ("review", "fix"):
            mark = f"{label}:{number}:{head}"
            if not st.inflight(mark):
                continue
            marks.append(f"{label} for head {head[:7]} armed "
                         f"{_minutes_since(st.inflight_at(mark), now)}m ago "
                         f"(ttl {st._minutes('inflight_ttl_min'):g}m)")   # the TTL inflight() used
            if label == "review":
                inflight_review = True
            else:
                inflight_fix = True
    inflight_line = " · ".join(marks) or "none"

    raw_marker = st.breach_get(number)
    marker = raw_marker if isinstance(raw_marker, dict) else {}
    delivery_status = gate.breach_delivery_status(marker, head)
    parked = delivery_status in {"awaiting-adjudication", "adjudicating"}
    escalation_line = (
        f"{marker.get('status') or 'recorded'} at head {str(marker.get('head') or '?')[:7]} since "
        f"{marker.get('at') or 'an unrecorded time'} — {marker.get('reason') or 'no reason recorded'}"
        if marker else "none")
    standing = bool(marker) and marker.get("head") == head and marker.get("status") in gate.STANDING
    if not standing:
        from . import escalate_pending      # #1246: asked for while a run was in flight
        pending = escalate_pending.get(st, number)
        if pending:
            at = pending.get("at")
            when = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
                    if isinstance(at, (int, float)) else "an unrecorded time")
            # #1264: shown even when an older marker exists (a ruled or superseded head).
            escalation_line = (f"pending since {when} ({pending.get('reason')}) — the next "
                               "changes-requested verdict goes to the adjudicator instead of the "
                               "fixer; an approval drops it"
                               + (f" (earlier: {escalation_line})" if escalation_line != "none"
                                  else ""))

    watch = st.watch()
    if watch.get("last_run"):
        armed_since = iso_at(float(watch.get("armed_since") or 0)) or "unrecorded"
        sweep = f"watchdog last swept {watch['last_run']} (armed since {armed_since})"
    else:
        sweep = "no watchdog sweep recorded — nothing has read this loop's PRs yet"

    github_bits = []
    blind = watch.get("github_read")
    if isinstance(blind, dict) and blind.get("error"):
        github_bits.append(f"watchdog could not read GitHub as {blind.get('login') or '?'} for "
                           f"{blind.get('sweeps', '?')} sweep(s): {gh.one_line(blind.get('error'), 300)}")
    failure = st.github_failure()
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
                           + _owner_note(loop, failure))
    github_line = " · ".join(github_bits) or "no failed GitHub call recorded"

    return {"seat": seat_line, "queue": queue_line, "inflight": inflight_line,
            "escalation": escalation_line, "github": github_line, "held": held, "queued_seat": queued_seat,
            "queued_reason": queued_reason, "stale_queues": stale_queues,
            "inflight_review": inflight_review,
            "inflight_fix": inflight_fix, "parked": parked, "delivery_status": delivery_status,
            "capacity": capacity, "marker": marker, "sweep": sweep}


def _explain_app_ingress(loop: dict, armed) -> str:
    """The armed/paused line for a loop whose events come through its App's webhook (#1239)."""
    via = f"events arrive through {loop.get('ingress_app')}'s webhook"
    if armed is False:
        return (f"PAUSED — {via}, and the loop's pause flag drops them "
                f"(`hermes dk arm --loop {loop['id']}` resumes)")
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
            "shadow_hold": shadow,
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
    shadow_held, shadow_note = facts.get("shadow_hold") or ("", "")      # #1004
    now = float(facts.get("read_at") or time.time())
    pr = facts.get("pr") if isinstance(facts.get("pr"), dict) else None
    reviews = facts.get("reviews") if isinstance(facts.get("reviews"), list) else None
    armed = facts.get("armed")
    hold = facts.get("hold") if isinstance(facts.get("hold"), dict) else None
    hold_error = str(facts.get("hold_error") or "")
    pr_error = str(facts.get("pr_error") or "")
    reviews_error = str(facts.get("reviews_error") or "")
    armed_error = str(facts.get("armed_error") or "")

    key = gate.seat_key(loop, number)
    cap = loop["cap"]
    repo = loop["repo"]
    head_data = (pr or {}).get("head")
    raw_head = head_data.get("sha") if isinstance(head_data, dict) else None
    head = raw_head if isinstance(raw_head, str) else ""
    short = head[:7] if head else "?"
    base = str(((pr or {}).get("base") or {}).get("ref") or "")
    chain = facts.get("chain")
    resolved = isinstance(chain, situation.Resolution)
    stacked = bool(base and base != loop["base"])
    chain_reason = chain.reason if resolved else "parent chain not verified"
    if stacked and isinstance(facts.get("parent_readiness"), tuple):
        chain_reason += f"; parent approval: {facts['parent_readiness'][1]}"
    chain_parents = chain.parents if resolved else ()
    author = str(((pr or {}).get("user") or {}).get("login") or "").lower()
    stranger = bool(author) and author not in config.reviewed_authors(loop)
    served = f"this loop's fixers ({', '.join(loop['fixers'])}) or review-only authors"
    state = ("unknown" if pr is None
             else "merged" if (pr.get("merged") or pr.get("merged_at"))
             else str(pr.get("state") or "unknown"))
    requested = {str((entry or {}).get("login") or "").lower()
                 for entry in (pr or {}).get("requested_reviewers") or []
                 if isinstance(entry, dict)}
    request_pending = bool(loop["reviewer_seat"]) and loop["reviewer_seat"] in requested
    blockers: list[str] = []
    # A gate that crashed, overran or silenced after a failed read (#75) is not a decision.
    gate_failed = [gate_failure_line(entry) for entry in gate_failures.open_for(loop, number)]
    blockers.extend(gate_failed)

    # -- what the gates conclude about this head (their predicates, not new ones) -------------
    spent: int | None = None
    at_head: int | None = None
    head_states: list[str] = []
    approved = False
    reviewed = False
    changes: list[dict] = []
    boundary = transition.hold(st, number, head) if head and not stacked else None
    if reviews is not None and boundary:
        # A post-retarget review list is diagnostic, not an authorization: its commit_id
        # cannot prove the base generation or a dispatched run. Only host-receipted
        # post-boundary reviews count (the same predicate the gates use). Facts without a
        # receipts read attest nothing; an unreadable ledger leaves the verdicts unknown.
        receipts = facts.get("receipts", {})
        reviews = transition.effective_reviews(loop, st, number, head, reviews,
                                               receipts=receipts if isinstance(receipts, dict) else None)
        if reviews is None:
            reviews_error = ("host review-receipt ledger unreadable: "
                             f"{facts.get('receipts_error') or 'no reason given'}")
    fresh = (boundary.get("fresh_review") if isinstance(boundary, dict) else None) or {}
    if reviews is not None:
        spent = len(gate.verdicts(reviews, loop))
        if head:
            latest = gate.latest_effective_review_at_head(reviews, loop, head)
            changes = (gate.changes_at_head(reviews, loop, head)
                       if latest is not None and gh.review_state(latest) == "CHANGES_REQUESTED"
                       else [])
            at_head = len(changes)
            approved = latest is not None and gh.review_state(latest) == "APPROVED"
            reviewed = gate.reviewed_at_head(reviews, loop, head)
            head_states = sorted({gh.review_state(r) for r in gate.reviews_at_head(reviews, loop, head)})

    stale_line, watch = "", st.watch()
    if approved and head:
        from . import stale_approval
        found = stale_approval.record(watch, number)
        if found and found.get("head") == head:
            stale_line = stale_approval.line(found)
            blockers.append(stale_line)

    # -- the budget line ---------------------------------------------------------------------
    unread = f"the review list could not be read ({reviews_error or 'no reason given'})"
    if reviews is None:
        budget = f"unknown — {unread}; never guessed from a failed read"
    elif at_head:
        last = [r for r in changes if isinstance(r, dict)][-1]
        budget = (f"{spent}/{cap} verdicts spent · {at_head} at head {short} — changes requested "
                  f"{last.get('submitted_at') or 'at an unrecorded time'} by "
                  f"{gate.reviewer_login(last) or 'an unrecorded reviewer'}")
    elif approved:
        budget = f"{spent}/{cap} verdicts spent · approved at head {short}"
    elif head_states:
        budget = (f"{spent}/{cap} verdicts spent · a non-verdict review at head {short} "
                  f"({'/'.join(head_states)}) — reviewer gate still accepts a fresh request")
    elif head:
        budget = f"{spent}/{cap} verdicts spent · nothing at head {short}"
    else:
        budget = (f"{spent if spent is not None else 'unknown'}/{cap} verdicts spent · "
                  f"head unknown")
    # #539: CI fixes have their own budget per PR, outside the verdict cap.
    if config.fix_ci(loop):
        from . import ci_fix
        used = len(ci_fix.rows(loop["repo"], number))
        budget += f" · CI fixes {used}/{config.ci_fix_cap(loop)}"

    # -- local state: who holds it, what waits, what is marked -------------------------------
    local = _explain_state(loop, st, key, number, head, now)
    pending_stack = (watch.get("stacked_wait") or {}).get(str(number))
    if stacked and isinstance(pending_stack, dict):
        if (pending_stack.get("head") == head and pending_stack.get("base") == base and
                pending_stack.get("base_sha") == ((pr or {}).get("base") or {}).get("sha") and
                (not resolved or
                 (pending_stack.get("status") == chain.status and
                  pending_stack.get("reason") == chain.reason and
                  (not chain.identity or pending_stack.get("identity") == chain.identity.key)))):
            stacked_line = (f"stacked visibility queue: {pending_stack.get('status')} — "
                            f"{pending_stack.get('reason')} (no seat run authorized)")
            local["queue"] = (stacked_line if local["queue"] == "not queued" else
                              local["queue"] + " · " + stacked_line)
    held = local["held"]
    queued_seat = local["queued_seat"]
    queued_reason = local["queued_reason"]
    inflight_review = local["inflight_review"]
    inflight_fix = local["inflight_fix"]
    marker = local["marker"]
    cap_spent = spent is not None and spent >= cap
    adjudicated = bool((loop.get("adjudicator") or {}).get("route"))
    # A marker records a past escalation, not a permanent veto. A dismissed
    # verdict can lower the live count, and the reviewer gate then starts a new
    # review even when the marker still says awaiting-adjudication/adjudicating.
    parked = local["parked"] and (cap_spent or gate.escalated(marker, head))
    # A ruling at this head ends adjudication: the marker stays, and the decision is a person's.
    ruled = _ruled(loop, number, head) if parked else None
    if parked and ruled == "ACCEPT" and ((marker or {}).get("escalated") or {}).get(
            "by") == gate.SHADOW_ESCALATION:
        # #1004: the adjudicator overruled the shadow review; the reviewer's approval stands.
        parked, ruled = False, None
    # A dismissed verdict can lower the live count after a failed POST. The
    # watchdog only retries a pending marker while the cap remains spent.
    pending_delivery = (local["delivery_status"] == "delivery-pending"
                        and gate.breach_holds(marker, head, spent, cap))
    stale_held = {seat for seat, entry in held.items() if entry.get("head") != head}
    # An isolated turn already in the ledger at this head (queued, held for CI, running) is
    # the gate's answer, not a missing one (#509): the run itself is reported as the blocker.
    db = config.host_path("ledger")
    from .run_supervisor import views
    turns = {seat: views.turn_state(db, repo, number, head, seat)
             for seat in ("reviewer", "fixer")} if head else {}
    ledgered = {seat for seat, turn in turns.items() if turn in LIVE_TURN}
    # A fix for this head: a lock on an old head cannot finish it (#1481 review).
    fix_live = ("fixer" in held and "fixer" not in stale_held) or inflight_fix or "fixer" in ledgered
    # The fixer's answer to the verdict (#894), as the sweep reads it: there an uncertain turn is
    # not a live one, so it hides no dispute (#1481 review). A verdict at head means a head.
    disputed = (bool(at_head) and ("fixer" not in ledgered or turns["fixer"] == "uncertain")
                and views.disputed(db, repo, number, head, latest) is not None)
    # Parked for a ruling (or its delivery pending), both gates stop on purpose: no "gate did not
    # start one" line, which would read as something stuck while the ruling is the next event.
    ruling = parked or pending_delivery
    # A review-only author's PR (#191) never needs the fixer and spends no fixer rounds: a verdict
    # at the head is the author's to answer. Its verdicts still have a cap (review_only_cap,
    # default `cap`); past it the loop waits for a maintainer's --another-round (below).
    review_only = author in config.review_only(loop)
    needed_seat = ("" if at_head and review_only else "fixer" if at_head
                   else "reviewer" if request_pending else "")
    used, limit = local["capacity"].get(needed_seat, (0, 0))
    full_seat = bool(needed_seat and used >= limit and needed_seat not in held
                     and not (inflight_fix if needed_seat == "fixer" else inflight_review))
    hooks_line = (_explain_app_ingress(loop, armed) if config.app_ingress(loop)
                  else _explain_hooks(armed, armed_error))
    # The fixer gate holds a changes-requested verdict while the loop has not opted in to
    # unattended pushes: no fixer turn can start, so the next event is the operator's.
    push_off = bool(at_head) and not review_only and not config.unattended_fixer_push_enabled(loop)
    push_off_held = push_off and "fixer" not in held and not inflight_fix
    fixer_queue_hold = queued_seat == "fixer" and queued_reason.startswith(config.FIXER_PUSH_HOLD)

    # -- every guard, in the gates' order, reported instead of silencing ----------------------
    if pr is None:
        if pr_error:
            blockers.append(f"stale/failing GitHub read: {pr_error} — the PR's state is unknown, "
                            f"not closed, and its head cannot be read")
        else:
            blockers.append(f"GitHub returned no PR #{number} in {repo} — the number is wrong, or "
                            f"the read token cannot see the repository")
    else:
        if state in ("closed", "merged"):
            blockers.append(f"the PR is {state} — the loop is over here; the closed path reclaims "
                            f"its disk and drops any queue entry for it")
        else:
            if armed is False:
                blockers.append(f"paused loop: seat route(s) without an active repo hook: "
                                f"{armed_error or 'unknown'}")
            elif armed is None:
                blockers.append(f"hook state unreadable ({armed_error or 'no reason given'}) — "
                                f"'paused' is not claimed: not seeing the hooks is not evidence they "
                                f"are off")
            if reviews is None:
                blockers.append(f"{unread} — the verdict count is unknown, never guessed")
            if not head:
                blockers.append("PR head missing or malformed — no gate can authorize a run")
            if hold:
                blockers.append(pr_hold.line(hold, loop))
            elif hold_error:
                blockers.append(f"hold state unreadable ({hold_error}) — no turn starts until it "
                                "is read")
            if pr.get("draft"):
                blockers.append("draft PR: the reviewer gate stays silent until ready_for_review")
            if stacked:
                blockers.append(f"stacked PR: {chain_reason} — visible only; no reviewer or fixer "
                                "run is authorized by parent readiness")
            # The retarget hold vetoes only while the fresh review is still owed: a host-receipted
            # post-boundary approval at this head ends it, as in `ready` and the gates (#791, #934).
            if boundary and not approved and transition.baseline_missing(boundary):
                blockers.append(f"same-head base retarget held: {transition.MISSING_BASELINE}")
            elif boundary and not approved:
                blockers.append("same-head base retarget: pre-retarget reviews, rounds and approvals "
                                "quarantined — fresh review situation; only a host-receipted "
                                "post-boundary review counts (fresh reviewer turn: "
                                f"{fresh.get('state') or 'not queued yet'})")
            if stranger:
                blockers.append(f"the author {author} is not one of {served}")
            if shadow_held:
                blockers.append(f"shadow review: {shadow_held}")
            if ruled:
                blockers.append(f"ruled: the adjudicator ruled {ruled} at head {short}; the "
                                "decision is yours")
            elif parked:
                blockers.append(f"escalated: the PR is parked awaiting adjudication at head "
                                f"{str(marker.get('head') or '?')[:7]} since "
                                f"{marker.get('at') or 'an unrecorded time'}")
            elif pending_delivery:
                blockers.append(f"adjudicator delivery pending at head {short} — no ruling is "
                                "promised until the route acknowledges delivery")
            elif cap_spent and not review_only:
                blockers.append(f"{spent}/{cap} verdicts spent with no approval and no escalation "
                                f"marker — the cap may not have fired (the watchdog reports this "
                                f"shape too)")
            if push_off_held and not parked and not pending_delivery and not cap_spent:
                blockers.append(f"{config.FIXER_PUSH_HOLD}: the changes-requested verdict at head "
                                f"{short} starts no fixer turn until the loop opts in — "
                                f"`{config.fixer_push_enable_command(loop)}`")
            if fixer_queue_hold:
                pass  # the push-off blocker above is this queue entry's whole story
            elif queued_seat:
                blockers.append(f"no capacity: queued with the {queued_seat} seat — {queued_reason}")
            elif full_seat:
                blockers.append(f"no capacity: {needed_seat} seat at capacity {used}/{limit} "
                                "on other PRs — this PR has no queue entry yet")
            blockers.extend(local["stale_queues"])
            for seat in sorted(stale_held):
                blockers.append(f"{seat} lock targets an older head, not current head {short} "
                                "— it cannot authorize a verdict or push at this head")
            if head_states and not reviewed:
                blockers.append(f"non-verdict review at head {short} ({'/'.join(head_states)}) "
                                "does not suppress a fresh reviewer request")
            if (at_head and not fix_live and not queued_seat and not push_off and not review_only
                    and not ruling and not disputed):
                blockers.append(f"the changes-requested verdict at head {short} has no fix run out "
                                f"— the fixer gate did not start one for that delivery")
            if (request_pending and not held and not inflight_review and not at_head
                    and not approved and not ruling and "reviewer" not in ledgered):
                blockers.append(f"a review request for {loop['reviewer_seat']} is pending at head "
                                f"{short}, but no review run is out — the reviewer gate did not "
                                f"start one")
            if (spent and head and not reviewed and not approved and not request_pending and not held
                    and not inflight_review and not queued_seat and not ruling
                    and "reviewer" not in ledgered):
                blockers.append(f"no review request exists for head {short} — GitHub clears a request "
                                f"when a verdict lands, so the reviewer gate stays silent until the "
                                f"fixer asks again")

    # -- the single next event ---------------------------------------------------------------
    to_fixer = f"re-deliver the changes-requested review event for head {short} to the fixer gate "
    fix_push = f"the fixer pushes a fix and re-requests review of head {short} — "
    verdict_at = f"the reviewer's verdict at head {short} (round {(spent or 0) + 1} of {cap}) — "
    if pr is None:
        if pr_error:
            kind = "retry"
            action = (f"retry the GitHub read for {repo}#{number} ({pr_error}) — nothing local can "
                      f"stand in for the head and the verdicts")
        else:
            kind = "none"
            action = (f"nothing to drive — check that #{number} exists, and that the loop's read "
                      f"token can see {repo}")
    elif state in ("closed", "merged"):
        kind = "none"
        action = (f"nothing — the PR is {state}; the loop is over for it, and its next event is "
                  f"gate_reviewer's closed path reclaiming the disk")
    elif armed is False:
        kind = "rearm"
        action = (f"re-arm the loop — hermes dk arm --loop {loop['id']} — no event can "
                  f"reach both seats while a repo hook is missing or inactive")
    elif not head:
        kind = "retry"
        action = (f"retry the PR read for {repo}#{number} — its head is missing or malformed; "
                  "no review can be requested without a known head")
    elif hold or hold_error:
        kind = "operator" if hold else "retry"
        action = (f"{pr_hold.line(hold, loop)}" if hold else
                  f"retry the hold read for {repo}#{number} ({hold_error}) — no turn starts until "
                  "it is known")
    elif pr.get("draft"):
        kind = "ready"
        action = ("mark the PR ready for review (the ready_for_review event) — the reviewer gate "
                  "ignores drafts")
    elif stacked:
        kind = "wait" if resolved and chain.status == "waiting" else "retry"
        action = (f"{chain_reason} — wait for a verified parent/base transition; "
                  "no automatic reviewer wake or gate authorization")
    elif boundary and transition.baseline_missing(boundary):
        kind = "wait"
        action = f"same-head retarget held permanently: {transition.MISSING_BASELINE}"
    elif stranger:
        kind = "none"
        action = f"nothing — the reviewer gate only serves PRs opened by {served}"
    elif reviews is None:
        kind = "retry"
        action = (f"retry the review list for {repo}#{number} "
                  f"({reviews_error or 'unreadable'}) — whether head {short} already has a verdict "
                  f"is unknown, and the loop never guesses that")
    elif boundary and not approved and not at_head and not reviewed:
        if fresh.get("state") == "enqueued":
            kind = "review-verdict"
            action = (f"fresh review after retarget: reviewer queued ({fresh.get('turn_key')}) — "
                      f"awaiting its receipted verdict at head {short}; old approvals/verdicts "
                      "never count, and an unreceipted review stays diagnostic")
        elif fresh.get("state") == "retry":
            kind = "retry"
            action = (f"fresh review after retarget: reviewer enqueue failed "
                      f"({fresh.get('error') or 'no reason recorded'}) — the next armed watchdog "
                      "sweep retries it")
        else:
            kind = "wait"
            action = ("fresh review after retarget: the reviewer is not queued yet (still a "
                      "draft, or not swept since the retarget) — the next armed watchdog sweep "
                      "enqueues it; then awaiting its receipted verdict")
    elif approved and stale_line:
        # The sweep's notice and the ready-to-merge queue (`stale_approval.is_stale`) both say
        # this approval no longer vouches for the head; do not recommend merging it.
        kind = "operator"
        action = (f"your decision: {stale_line} — do not merge head {short}; re-run the check "
                  "or push a fix")
    elif approved and shadow_held:
        # #1004: the shadow review holds this approved head (shadow review of #1097, F2): never
        # recommend merging it while the hold stands.
        if shadow_held.startswith("waiting on"):
            kind = "wait"
            action = f"{shadow_held} — head {short} is approved but held until it lands"
        else:
            kind = "operator"
            action = (f"your decision: {shadow_held} — do not merge head {short} until you have "
                      "read its findings")
    elif approved:
        kind = "none"
        action = f"nothing — head {short} is approved; a human merges it"
    elif head and isinstance(st.human_hold(number, head), dict):
        kind = "operator"
        held = st.human_hold(number, head)
        action = (f"a person must review and approve head {short}: the loop would approve, but "
                  f"{held.get('why') or 'it touches human_paths'}")
    elif (review_only and spent is not None and spent >= config.review_only_cap(loop)
          and not st.review_cap_granted(number, head, spent)):
        kind = "operator"
        push_first = ("the author pushes a new head first (this head already has a verdict, so a "
                      "grant now would be wasted), then " if reviewed else "")
        action = (f"review cap reached on #{number} ({spent} verdicts): no further review until "
                  f"{push_first}a maintainer grants another round — merge, close, or "
                  f"`hermes dk review --loop {loop['id']} --pr {number} --another-round`")
    elif review_only and at_head:
        kind = "author-push"
        ask = (f"runs `hermes dk review --loop {loop['id']} --pr {number}` (a GitHub App reviewer "
               "cannot be requested on GitHub)" if config.reviewer_is_app(loop) else
               "re-requests review of the new head")
        action = (f"the author {author} pushes a fix and {ask} — a "
                  f"review-only PR has no fixer, but its verdicts are capped at "
                  f"{config.review_only_cap(loop)} (`review_only_cap`, default the loop's "
                  f"`cap`); a maintainer's `--another-round` lifts it once reached")
    elif pending_delivery and not adjudicated:
        kind = "adjudication"
        action = _adjudication_next(loop, head)
    elif pending_delivery:
        kind = "retry"
        action = (f"retry adjudicator delivery for head {short} on the next armed watchdog sweep "
                  "after checking the current head and review cap — no ruling is underway yet")
    elif ruled:
        kind = "operator"
        action = (f"your decision: the adjudicator ruled {ruled} at head {short} (its comment is "
                  "on the PR) — merge, close, or push a new head, which starts a fresh review")
    elif parked:
        kind = "adjudication"
        action = _adjudication_next(loop, head)
    elif cap_spent and not review_only:
        # No marker (#575). A live fix run ends first: its push meets the cap at the reviewer gate
        # (escalating now waits for a next verdict). Else escalate: the sweep raises the marker
        # only at a head it saw arrive, never one open since arming (#1481 review).
        if not adjudicated:
            kind = "adjudication"
            action = _adjudication_next(loop, head)
        elif fix_live:
            kind = "fixer-push"
            action = fix_push + "with the cap spent, the reviewer gate then sends it for a ruling"
        else:
            # Escalate's own rule: a run on any head, or one of unknown outcome, holds it. A
            # ledger it cannot read makes that unknown, never a crash (#1481 review).
            import sqlite3
            from . import one_shot_action
            try:
                busy, unknown = one_shot_action.escalation_blockers(loop, number)
                wait = (f"{busy} run(s) for this PR are in flight; once they end, " if busy else
                        "a run's outcome is unknown; once it is reconciled, " if unknown else "")
            except (sqlite3.Error, OSError) as exc:
                wait = (f"the run ledger cannot be read ({exc}), so whether escalate acts now is "
                        "unknown; once it can be, ")
            kind = "release" if wait else "operator"
            action = (wait + f"`hermes dk escalate --loop {loop['id']} --pr {number}` sends head {short} "
                      "for a ruling (the watchdog raises a missing marker itself only at a head it "
                      "saw arrive, never one open since the loop was armed) — no ruling is underway yet")
    elif push_off_held:
        kind = "operator"
        action = (f"operator decision: unattended fixer pushes are off for this loop, so the "
                  f"changes-requested verdict at head {short} starts no fixer turn. To let the "
                  f"fixer answer it, run `{config.fixer_push_enable_command(loop)}` — the next "
                  f"watchdog sweep (or `hermes dk drain --loop {loop['id']} --seat fixer`) "
                  "then starts the fix run for this head; or fix it by hand, push, and re-request "
                  "review")
    elif disputed:
        kind = "adjudication" if adjudicated else "operator"
        action = (f"the fixer disputed the review at head {short}, every finding as no defect — "
                  + ("the next armed watchdog sweep sends it for a ruling" if kind == "adjudication"
                     else "your decision: " + views.dispute_choices(loop["id"], number)))
    elif stale_held == {"reviewer"} and at_head:
        kind = "fixer-retry"
        action = to_fixer + "— that verdict hands off the PR and releases the stale reviewer lock"
    elif stale_held == {"fixer"} and request_pending:
        kind = "retry"
        action = (f"re-deliver the review_requested event for head {short} to the reviewer gate "
                  "— that request hands off the PR and releases the stale fixer lock")
    elif stale_held:
        kind = "release"
        action = (f"release or wait for the stale {', '.join(sorted(stale_held))} lock to expire "
                  f"before re-driving the current head {short}; an old-head run cannot finish it")
    elif "reviewer" in held and not at_head:
        kind = "review-verdict"
        action = verdict_at + "the reviewer holds the PR right now"
    elif "fixer" in held:
        kind = "fixer-push"
        action = fix_push + "asking is what hands the PR back, and it is what frees the fixer's slot"
    elif queued_seat:
        kind = "release"
        action = (f"a {queued_seat} slot frees — the queued run starts then (a verdict or a handoff "
                  f"ends the run holding it; the lock expiry at "
                  f"{-(-config.seat_ttl_s(loop, seat=queued_seat) // 60)}m is "
                  "the backstop)")
    elif full_seat:
        kind = "retry"
        action = (f"re-deliver the {'changes-requested review' if at_head else 'review_requested'} "
                  f"event for head {short} now — the {needed_seat} gate will queue it at "
                  f"capacity {used}/{limit}; a {needed_seat} slot on another PR freeing then "
                  "starts the queued run")
    elif inflight_review:
        kind = "review-verdict"
        action = verdict_at + "a review for this head is already marked in flight"
    elif inflight_fix:
        kind = "fixer-push"
        action = fix_push + "a fix run for this head is already marked in flight"
    elif at_head:
        kind = "fixer-retry"
        action = to_fixer + "after checking why its run did not start — no fixer is running to push a fix"
    elif request_pending:
        kind = "retry"
        action = (f"a review run for head {short}: the pending request for "
                  f"{loop['reviewer_seat']} started none. A request counts only from a fixer, the "
                  "reviewer or a maintainer (triage.maintainers); anyone else's is ignored. Run "
                  f"`hermes dk review --loop {loop['id']} --pr {number}` (or ask again as "
                  "one of them); if it came from one, "
                  f"`trace --loop {loop['id']}` its delivery to see why the gate declined")
    else:
        kind = "review-request"
        detail = ("a fresh PR also wakes the reviewer on opened / ready_for_review, so re-driving "
                  "that event is the other way in" if not spent else "a push alone wakes nobody")
        action = ((f"ask for review of head {short}: `hermes dk review --loop {loop['id']} --pr "
                   f"{number}` — the reviewer is a GitHub App, which GitHub cannot request")
                  if config.reviewer_is_app(loop) else
                  (f"the fixer asks for review of head {short}: gh api -X POST "
                   f"repos/{repo}/pulls/{number}/requested_reviewers -f "
                   f"'reviewers[]={loop['reviewer_seat']}' — {detail}"))

    if pr is None:
        state_line = "unknown — the PR itself could not be read"
    else:
        bits = [state, f"base {base or 'unknown'}", f"author {author or 'unknown'}"]
        bits.append(f"head {short}" if head else "head unread")
        if pr.get("draft"):
            bits.append("draft")
        if request_pending:
            bits.append(f"review requested from {loop['reviewer_seat']}")
        if pr.get("mergeable_state") == "dirty":
            # #303 stage 1: GitHub cannot merge it as it is; nothing in the loop resolves that yet.
            bits.append(f"CONFLICTS with {base or 'its base'} — merge {base or 'the base'} into "
                        "the branch")
            facts_ro = facts.get("review_only_conflict")
            if author in config.review_only(loop) and isinstance(facts_ro, dict):
                # The same facts the watchdog's notice carries (review-only, #412).
                from . import review_only_conflict
                bits.append(review_only_conflict.cause(loop, facts_ro) + "; "
                            + review_only_conflict.files(facts_ro) + "; run: "
                            + review_only_conflict.commands(loop, facts_ro))
        state_line = " · ".join(bits)

    return {
        "pr": number, "repo": repo, "url": gate.pr_url(loop, number), "state": state,
        "head": head, "cap": cap, "spent": spent, "at_head": at_head,
        "chain": {"status": chain.status if resolved else
                  ("unverified" if stacked else "direct"), "reason": chain_reason if stacked else "direct trunk base",
                  "parents": list(chain_parents),
                  "identity": chain.identity.key if resolved and chain.identity else ""},
        "approved": approved, "stale_approval": stale_line, "reviewed": reviewed, "request_pending": request_pending,
        "read_at": iso_at(now),
        "state_line": state_line, "budget": budget, "seat": local["seat"], "queue": local["queue"],
        "inflight": local["inflight"], "escalation": local["escalation"], "hooks": hooks_line,
        "sweep": local["sweep"],
        "github": local.get("github", "no failed GitHub call recorded"),
        "gate_decisions": [gate_decisions.line(e) for e in gate_decisions.for_pr(loop, number)],
        "gate_failures": gate_failed, "blockers": blockers, "next": {"kind": kind, "action": action},
        "shadow": {"hold": shadow_held, "note": shadow_note},
        "authorship": authorship_lines(facts),
        "operator_agents": sorted(config.operator_agents(loop)),
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
