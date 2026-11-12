"""``hermes dk explain``'s single next step: the one event that would move the PR. Split from
``explain.py`` when it reached the size ceiling (#1349).

``explain`` walks every guard and gathers its facts on ``c``; ``next_step`` then names the first
event below that applies, as ``(kind, action)`` with ``kind`` one of ``explain.EXPLAIN_KINDS``.
It reads only ``c``, so it decides nothing ``explain`` did not already read.
"""
from __future__ import annotations

from . import config, explain, gate, pr_hold, transition


def next_step(c) -> tuple[str, str]:
    """The single next event as ``(kind, action)``: the first branch below that applies."""
    # -- the single next event ---------------------------------------------------------------
    c.to_fixer = f"re-deliver the changes-requested review event for head {c.short} to the fixer gate "
    c.fix_push = f"the fixer pushes a fix and re-requests review of head {c.short} — "
    c.verdict_at = f"the reviewer's verdict at head {c.short} (round {(c.spent or 0) + 1} of {c.cap}) — "
    return (_next_unread(c) or _next_approval(c) or _next_person(c) or _next_ruling(c)
            or _next_review_only(c) or _next_push_off(c) or _next_escalation(c)
            or _next_lock(c) or _next_run(c))


def _next_unread(c) -> tuple[str, str] | None:
    """Next event while the PR cannot be driven: unread, closed, paused, held, draft, stacked."""
    if c.pr is None:
        if c.pr_error:
            kind = "retry"
            action = (f"retry the GitHub read for {c.repo}#{c.number} ({c.pr_error}) — nothing local can "
                      f"stand in for the head and the verdicts")
        else:
            kind = "none"
            action = (f"nothing to drive — check that #{c.number} exists, and that the loop's read "
                      f"token can see {c.repo}")
    elif c.state in ("closed", "merged"):
        kind = "none"
        action = (f"nothing — the PR is {c.state}; the loop is over for it, and its next event is "
                  f"gate_reviewer's closed path reclaiming the disk")
    elif c.armed is False:
        kind = "rearm"
        action = (f"re-arm the loop — hermes dk arm --loop {c.loop['id']} — no event can "
                  f"reach both seats while a repo hook is missing or inactive")
    elif not c.head:
        kind = "retry"
        action = (f"retry the PR read for {c.repo}#{c.number} — its head is missing or malformed; "
                  "no review can be requested without a known head")
    elif c.hold or c.hold_error:
        kind = "operator" if c.hold else "retry"
        action = (f"{pr_hold.line(c.hold, c.loop)}" if c.hold else
                  f"retry the hold read for {c.repo}#{c.number} ({c.hold_error}) — no turn starts until "
                  "it is known")
    elif c.pr.get("draft"):
        kind = "ready"
        action = ("mark the PR ready for review (the ready_for_review event) — the reviewer gate "
                  "ignores drafts")
    elif c.stacked:
        kind = "wait" if c.resolved and c.chain.status == "waiting" else "retry"
        action = (f"{c.chain_reason} — wait for a verified parent/base transition; "
                  "no automatic reviewer wake or gate authorization")
    elif c.boundary and transition.baseline_missing(c.boundary):
        kind = "wait"
        action = f"same-head retarget held permanently: {transition.MISSING_BASELINE}"
    elif c.stranger:
        kind = "none"
        action = f"nothing — the reviewer gate only serves PRs opened by {c.served}"
    elif c.reviews is None:
        kind = "retry"
        action = (f"retry the review list for {c.repo}#{c.number} "
                  f"({c.reviews_error or 'unreadable'}) — whether head {c.short} already has a verdict "
                  f"is unknown, and the loop never guesses that")
    else:
        return None
    return kind, action


def _next_approval(c) -> tuple[str, str] | None:
    """Next event at a retargeted head owed a fresh review, or an approved head."""
    if c.boundary and not c.approved and not c.at_head and not c.reviewed:
        if c.fresh.get("state") == "enqueued":
            kind = "review-verdict"
            action = (f"fresh review after retarget: reviewer queued ({c.fresh.get('turn_key')}) — "
                      f"awaiting its receipted verdict at head {c.short}; old approvals/verdicts "
                      "never count, and an unreceipted review stays diagnostic")
        elif c.fresh.get("state") == "retry":
            kind = "retry"
            action = (f"fresh review after retarget: reviewer enqueue failed "
                      f"({c.fresh.get('error') or 'no reason recorded'}) — the next armed watchdog "
                      "sweep retries it")
        else:
            kind = "wait"
            action = ("fresh review after retarget: the reviewer is not queued yet (still a "
                      "draft, or not swept since the retarget) — the next armed watchdog sweep "
                      "enqueues it; then awaiting its receipted verdict")
    elif c.approved and c.stale_line:
        # The sweep's notice and the ready-to-merge queue (`stale_approval.is_stale`) both say
        # this approval no longer vouches for the head; do not recommend merging it.
        kind = "operator"
        action = (f"your decision: {c.stale_line} — do not merge head {c.short}; re-run the check "
                  "or push a fix")
    elif c.approved and c.shadow_held:
        # #1004: the shadow review holds this approved head (shadow review of #1097, F2): never
        # recommend merging it while the hold stands.
        if c.shadow_held.startswith("waiting on"):
            kind = "wait"
            action = f"{c.shadow_held} — head {c.short} is approved but held until it lands"
        elif c.shadow_fix:
            kind = "fixer-push"
            action = (f"{c.shadow_held} — do not merge head {c.short}; the fixer's turn on the "
                      "upheld findings answers it")
        else:
            kind = "operator"
            action = (f"your decision: {c.shadow_held} — do not merge head {c.short} until you have "
                      "read its findings")
    elif c.approved and c.unlifted:
        kind = "operator"
        action = (f"{c.unlifted[0]} — once nothing holds it (a dismissal by the review's author, "
                  "a maintainer, or the sweep under `adjudicator_approves` for the reviewer's), a "
                  f"human merges head {c.short}")
    elif c.approved:
        kind = "none"
        action = f"nothing — head {c.short} is approved; a human merges it"
    else:
        return None
    return kind, action


def _next_person(c) -> tuple[str, str] | None:
    """Next event for a human hold: a person must approve the head."""
    if c.head and isinstance(c.st.human_hold(c.number, c.head), dict):
        c.held = c.st.human_hold(c.number, c.head)
        return "operator", (f"a person must review and approve head {c.short}: the loop would "
                            f"approve, but {c.held.get('why') or 'it touches human_paths'}")
    return None


def _next_review_only(c) -> tuple[str, str] | None:
    """Next event for a review-only PR (#191) that is not waiting on a ruling: its verdict cap, or
    the author's answer to a verdict. Read after ``_next_ruling``: a head parked for adjudication
    (by the cap, or by a person's ``escalate`` below it, #1156), pending delivery, or ruled waits
    on that, as ``blockers`` says, and the author's push is not the next event."""
    if (c.review_only and c.spent is not None
            and gate.review_cap_hit(c.loop, c.st, c.number, c.head, c.spent, c.author)):
        kind = "operator"
        push_first = ("the author pushes a new head first (this head already has a verdict, so a "
                      "grant now would be wasted), then " if c.reviewed else "")
        action = (f"review cap reached on #{c.number} ({c.spent} verdicts): no further review until "
                  f"{push_first}a maintainer grants another round — merge, close, or "
                  f"`hermes dk review --loop {c.loop['id']} --pr {c.number} --another-round`")
    elif c.review_only and c.at_head:
        kind = "author-push"
        ask = (f"runs `hermes dk review --loop {c.loop['id']} --pr {c.number}` (a GitHub App reviewer "
               "cannot be requested on GitHub)" if config.reviewer_is_app(c.loop) else
               "re-requests review of the new head")
        action = (f"the author {c.author} pushes a fix and {ask} — a "
                  f"review-only PR has no fixer, but its verdicts are capped at "
                  f"{config.review_only_cap(c.loop)} (`review_only_cap`, default the loop's "
                  f"`cap`); a maintainer's `--another-round` lifts it once reached")
    else:
        return None
    return kind, action


def _below_cap_escalation(c) -> bool:
    """Whether an operator escalation, not a spent cap, holds this head for a ruling (#1887):
    the same ``gate.escalated`` that ``gate.breach_holds`` and ``c.parked`` read."""
    return not c.cap_spent and gate.escalated(c.marker, c.head)


def _next_ruling(c) -> tuple[str, str] | None:
    """Next event while adjudication delivery is pending, ruled, or parked."""
    if c.pending_delivery and not c.adjudicated:
        kind = "adjudication"
        action = explain._adjudication_next(c.loop, c.head, escalated=_below_cap_escalation(c))
    elif c.pending_delivery:
        kind = "retry"
        action = (f"retry adjudicator delivery for head {c.short} on the next armed watchdog sweep "
                  "after checking the current head and review cap — no ruling is underway yet")
    elif c.ruled:
        kind = "operator"
        action = (f"your decision: the adjudicator ruled {c.ruled} at head {c.short} (its comment is "
                  "on the PR) — merge, close, or push a new head, which starts a fresh review")
    elif c.parked:
        kind = "adjudication"
        action = explain._adjudication_next(c.loop, c.head, escalated=_below_cap_escalation(c))
    else:
        return None
    return kind, action


def redelivery_rearms(c) -> bool:
    """Whether a redelivered verdict re-arms this head's ended fixer turn once pushes are on: the
    rule ``submit`` itself applies (``intake.redelivery_refusal``), not retry's, which offers
    different rows (#1514 review). An unreadable ledger answers no; the gate's verdict turn
    carries no turn key."""
    import sqlite3
    from .run_supervisor import REARMABLE, intake, views
    try:
        con = views.read_only_connect(config.host_path("ledger"))
        if con is None:
            return False
        con.row_factory = sqlite3.Row
        try:
            prior = con.execute("SELECT * FROM runs WHERE repo=? AND pr=? AND head=? AND seat='fixer' "
                                "AND turn_key=''", (c.repo, c.number, c.head)).fetchone()
            return (prior is not None and prior["state"] in REARMABLE and intake.redelivery_refusal(
                con, prior, admitted=True, production=True, seat="fixer") is None)
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return False


def fixer_retry(c, seat: str = "fixer") -> tuple[str, str, bool]:
    """``(command, "", False)`` when ``retry`` would re-arm this head's ``seat`` turn (the fixer's
    by default), else ``("", why, blocked)``, ``blocked`` when a run is there but may have written
    (reconcile first): the ledger's own answer (``retry_action.plan``), so explain never names a
    retry it refuses."""
    import sqlite3
    from . import retry_action
    from .run_supervisor import views
    try:
        con = views.read_only_connect(config.host_path("ledger"))
        if con is None:
            return "", "no isolated run on record", False
        con.row_factory = sqlite3.Row                      # plan reads rows by name
        try:
            plan = retry_action.plan(con, c.repo, c.number, seat)
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        return "", f"the run ledger cannot be read: {exc}", False
    if plan["head"] == c.head and plan["rearmable"]:
        return f"`hermes dk retry --loop {c.loop['id']} --pr {c.number} --seat {seat}`", "", False
    blocked = next(iter(plan["blocked"].values()), "")
    return "", plan["message"] or blocked or "nothing at this head", bool(blocked)


def _mark_wait(c) -> str:
    """The fixer gate refuses a re-delivery while this head's in-flight fix mark stands
    (gate_fixer: "a fix run ... is already out"), so with a mark and no run the operator waits for
    it to expire first (#1510 review); a bare lock does not hold the gate, so nothing to wait for."""
    if not c.inflight_fix:
        return ""
    ttl = c.st.minutes("inflight_ttl_min")
    armed = explain._minutes_since(c.st.inflight_at(f"fix:{c.number}:{c.head}"), c.now)
    return (f"the fixer gate refuses a re-delivery while the in-flight fix mark for this head stands "
            f"(armed {armed}m ago, ttl {ttl:g}m — `hermes dk set --loop {c.loop['id']} "
            f"--inflight-ttl-min N` is the knob); once it has expired, ")


def _started_turn_step(c, enable: str) -> str:
    """A fixer turn started while pushes are off: its push is refused (broker_ipc) or the worker
    cancels it at launch, and the sweep only drains queued work. A run in the ledger is what an
    operator retry re-arms; a lock or mark with no run behind it gets a fresh delivery instead
    (#1510 review), since retry has nothing to offer there."""
    rearm = (f"`hermes dk retry --loop {c.loop['id']} --pr {c.number} --seat fixer` re-arms it "
             "(retry refuses a run that may have written, and says how to reconcile it)"
             + (" — with the cap spent, its push then goes for a ruling" if c.cap_spent else "")
             if c.fixer_turn else
             "it has no run in the ledger for retry to re-arm, so "
             + (f"`hermes dk escalate --loop {c.loop['id']} --pr {c.number}` sends head {c.short} for "
                "a ruling — at the spent cap the fixer gate starts no new fix" if c.cap_spent
                else _mark_wait(c) + c.to_fixer + "(the gate admits it under the policy then)"))
    return (f"operator decision: unattended fixer pushes are off for this loop, so the fixer turn "
            f"at head {c.short} cannot publish — the worker cancels it before launch, or the broker "
            f"refuses its push. Run `{enable}`, then, once that turn has ended without a push, "
            + rearm + "; or fix it by hand, push, and re-request review")


def _reconcile_first(c, why: str) -> str:
    """A fixer run that may have written (uncertain, or once quarantined): nothing re-arms it
    until an operator reconciles it, so that is the step, never a replay (#1507)."""
    return (f"reconcile it first, its outcome is unknown ({why}): `hermes dk status --loop "
            f"{c.loop['id']}` names the run and its reconcile command, and only then retry")


def _ended_turn_step(c, enable: str) -> str:
    """A ledgered fixer turn that is not live — uncertain, or ended with no push — whether or not
    a lock or an in-flight mark still stands over it (#1510 review). A redelivered verdict is
    named only when intake would re-arm the turn, retry only when its plan offers it (#1514
    review: the two rules differ), and a redelivery waits out a live in-flight mark, which the
    gate drops it under, while a retry does not; a run that may have written is reconciled first."""
    return (f"operator decision: unattended fixer pushes are off for this loop, and the fixer "
            f"turn at head {c.short} is {c.fixer_turn} with no fix pushed"
            + ("" if c.redeliver else " — a redelivered verdict is a duplicate the ledger refuses")
            + f". Run `{enable}`, then {_ended_turn_recovery(c)}, push, and re-request review")


def _ended_turn_recovery(c) -> str:
    """What re-arms an ended fixer turn at this head: retry when its plan offers it, a redelivered
    verdict when intake would re-arm it (waiting out a live in-flight mark), a reconcile first for
    a run that may have written, else a fix by hand. Shared by the push-off step and the step for
    a declined fixer queue entry with pushes on (#1613), so both name the same recovery."""
    rearm, why, reconcile = c.fixer_rearm
    wait = _mark_wait(c) if c.redeliver else ""   # the gate drops it under a live mark (#1514 F3)
    redeliver = wait + c.to_fixer + "— the ledger re-arms the turn for it" if c.redeliver else ""
    if redeliver and rearm:
        return (f"{rearm} re-arms it now — or {redeliver}" if wait
                else f"{redeliver} (or {rearm} re-arms it)") + "; or fix it by hand"
    if redeliver or rearm:
        return (redeliver or f"{rearm} re-arms it" + (" (with the cap spent, its push then goes "
                                                         "for a ruling)" if c.cap_spent else "")
                ) + "; or fix it by hand"
    if reconcile:
        return _reconcile_first(c, why) + "; or fix it by hand"
    return f"`retry` has nothing to re-arm ({why}): fix it by hand"


def _uncertain_hold(c) -> str:
    """The safety hold on an unverified route POST (#1512): the drain leaves it as is, so neither
    a sweep, a drain nor a freed slot starts it; a person reconciles it first."""
    return (f"a safety hold ({c.queued_reason}): its route POST may have been sent unanswered, so "
            "no watchdog sweep, drain or freed slot re-sends it — check `hermes dk status --loop "
            f"{c.loop['id']}` and the run ledger for a run at this head, then remove the entry or "
            "re-deliver the event by hand")


def _declined_step(c) -> tuple[str, str]:
    """Next event for a fixer queue entry the gate declined (a duplicate of a terminal turn) once
    no push-off step applies (#1613): drain leaves such an entry as is (drain.py), so a freed
    fixer slot never starts it. An ended turn in the ledger gets its own recovery (retry or a
    redelivered verdict); with no run in the ledger a fresh delivery schedules one."""
    declined = (f"the gate declined the fixer turn for head {c.short} ({c.queued_reason}), and a "
                "freed fixer slot or a drain leaves that entry as is")
    if c.fix_live:
        return "fixer-push", c.fix_push + declined + "; the fixer turn already out answers the verdict"
    if c.fixer_ended:
        kind = "fixer-retry" if c.fixer_rearm[0] or c.redeliver else "operator"
        return kind, f"{declined} — {_ended_turn_recovery(c)}, push, and re-request review"
    return "fixer-retry", (f"{declined} — no fixer run for this head is in the ledger, so `retry` "
                           "has nothing to re-arm: "
                           + _mark_wait(c) + c.to_fixer + "(the gate schedules a fresh turn for it)")


def _next_push_off(c) -> tuple[str, str] | None:
    """Next event with unattended fixer pushes off (#1506): a dispute, a fixer turn already
    started, a queued hold, or no work order at all. At a spent cap only a started turn is."""
    from .run_supervisor import views
    enable = config.fixer_push_enable_command(c.loop)
    if not c.push_off or (c.cap_spent and not c.adjudicated):
        return None                          # a spent cap with no route: human adjudication
    if c.disputed and not c.cap_spent:
        # The sweep checks pushes-off before a dispute, whatever lock is held, and alerts the
        # operator; once pushes are on it acts on the dispute already there, not a new fix (#1496).
        action = (f"operator decision: unattended fixer pushes are off for this loop, and the fixer "
                  f"already disputed the review at head {c.short} — once you run `{enable}`, "
                  + ("the next armed watchdog sweep sends the dispute for a ruling" if c.adjudicated
                     else "it is your decision: " + views.dispute_choices(c.loop["id"], c.number)))
    elif c.fixer_ended:
        action = _ended_turn_step(c, enable)   # F5: a lock or mark can outlive its run
    elif c.fix_live:
        action = _started_turn_step(c, enable)
    elif c.cap_spent:
        return None
    elif (c.push_off_held and c.queued_seat == "fixer"     # a reviewer entry is no fixer turn
          and gate.is_declined_hold({"reason": c.queued_reason})):
        # The gate answered and declined (a terminal turn): drain leaves it, so only a retry
        # moves it, and only when retry would re-arm that turn (#1510 review).
        rearm, why, _ = fixer_retry(c)
        action = (f"operator decision: unattended fixer pushes are off for this loop, and the gate "
                  f"declined the fixer turn for head {c.short} ({c.queued_reason}) — nothing "
                  "restarts it. " + (f"Run `{enable}`, then {rearm} re-arms it; or fix it by hand"
                                     if rearm else f"`retry` has nothing to re-arm ({why}): fix "
                                     "it by hand") + ", push, and re-request review")
    elif c.push_off_held and c.queued_seat == "fixer" and c.queued_uncertain:
        action = (f"operator decision: unattended fixer pushes are off for this loop, and the queued "
                  f"fixer entry for head {c.short} is {_uncertain_hold(c)}. Run `{enable}` only "
                  "after that; or fix it by hand, push, and re-request review")
    elif c.push_off_held and c.queued_seat == "fixer":
        # Only the push-off hold is started by a sweep or drain alone; a capacity (or other)
        # entry waits for a fixer slot too, and the gate holds it while pushes are off (shadow).
        starts = ("the next watchdog sweep (or `hermes dk drain --loop {id} --seat fixer`) then "
                  "starts the fix run for this head" if c.fixer_queue_hold else
                  "the queued fixer entry ({reason}) is handed to the gate when a fixer slot frees "
                  "or on `hermes dk drain --loop {id} --seat fixer`, and the gate starts the run "
                  "only once pushes are on").format(id=c.loop["id"], reason=c.queued_reason)
        action = (f"operator decision: unattended fixer pushes are off for this loop, so the "
                  f"changes-requested verdict at head {c.short} starts no fixer turn. To let the "
                  f"fixer answer it, run `{enable}` — {starts}; or fix it by hand, push, and "
                  "re-request review")
    elif c.push_off_held:
        action = (f"operator decision: unattended fixer pushes are off for this loop, and the "
                  f"changes-requested verdict at head {c.short} has no fixer work order (no queued "
                  f"hold, no run), so nothing starts one by itself. Run `{enable}`, then "
                  + c.to_fixer + f"(a fixer run the push policy cancelled is re-armed by `hermes dk "
                  f"retry --loop {c.loop['id']} --pr {c.number} --seat fixer` instead); or fix it "
                  "by hand, push, and re-request review")
    else:
        return None
    return "operator", action


def _next_escalation(c) -> tuple[str, str] | None:
    """Next event at a spent cap with no marker, or a dispute."""
    from .run_supervisor import views
    if c.cap_spent and not c.review_only:
        # No marker (#575). A live fix run ends first: its push meets the cap at the reviewer gate
        # (escalating now waits for a next verdict). Else escalate: the sweep raises the marker
        # only at a head it saw arrive, never one open since arming (#1481 review).
        if not c.adjudicated:
            kind = "adjudication"
            action = explain._adjudication_next(c.loop, c.head)
        elif c.fix_live:
            kind = "fixer-push"
            action = c.fix_push + "with the cap spent, the reviewer gate then sends it for a ruling"
        else:
            # Escalate's own rule: a run on any head, or one of unknown outcome, holds it. A
            # ledger it cannot read makes that unknown, never a crash (#1481 review).
            import sqlite3
            from . import escalate_action
            try:
                busy, unknown = escalate_action.escalation_blockers(c.loop, c.number)
                wait = (f"{busy} run(s) for this PR are in flight; once they end, " if busy else
                        "a run's outcome is unknown; once it is reconciled, " if unknown else "")
            except (sqlite3.Error, OSError) as exc:
                wait = (f"the run ledger cannot be read ({exc}), so whether escalate acts now is "
                        "unknown; once it can be, ")
            kind = "release" if wait else "operator"
            action = (wait + f"`hermes dk escalate --loop {c.loop['id']} --pr {c.number}` sends head {c.short} "
                      "for a ruling (the watchdog raises a missing marker itself only at a head it "
                      "saw arrive, never one open since the loop was armed) — no ruling is underway yet")
    elif c.disputed:
        kind = "adjudication" if c.adjudicated else "operator"
        action = (f"the fixer disputed the review at head {c.short}, every finding as no defect — "
                  + ("the next armed watchdog sweep sends it for a ruling" if kind == "adjudication"
                     else "your decision: " + views.dispute_choices(c.loop["id"], c.number)))
    else:
        return None
    return kind, action


def _next_lock(c) -> tuple[str, str] | None:
    """Next event decided by a stale lock, a holder, a queue entry or a full seat."""
    if c.stale_held == {"reviewer"} and c.at_head:
        kind = "fixer-retry"
        action = c.to_fixer + "— that verdict hands off the PR and releases the stale reviewer lock"
    elif c.stale_held == {"fixer"} and c.request_pending:
        kind = "retry"
        action = (f"re-deliver the review_requested event for head {c.short} to the reviewer gate "
                  "— that request hands off the PR and releases the stale fixer lock")
    elif c.stale_held:
        kind = "release"
        action = (f"release or wait for the stale {', '.join(sorted(c.stale_held))} lock to expire "
                  f"before re-driving the current head {c.short}; an old-head run cannot finish it")
    elif "reviewer" in c.held and not c.at_head:
        kind = "review-verdict"
        action = c.verdict_at + "the reviewer holds the PR right now"
    elif "fixer" in c.held:
        kind = "fixer-push"
        action = c.fix_push + "asking is what hands the PR back, and it is what frees the fixer's slot"
    elif c.queued_seat == "fixer" and c.queued_declined:
        return _declined_step(c)                       # drain leaves it: no freed slot starts it
    elif c.queued_declined:
        # The drain leaves a declined entry: no freed slot starts it, as the blocker says (#1610).
        rearm, why, _ = fixer_retry(c, c.queued_seat)
        kind = "retry" if rearm else "operator"
        action = (f"the gate declined the {c.queued_seat} turn for head {c.short} ({c.queued_reason}) "
                  "— no freed slot starts it. " + (f"{rearm} re-arms it" if rearm else
                                                   f"`retry` has nothing to re-arm ({why})")
                  + "; or re-drive this head by hand")
    elif c.queued_uncertain:
        kind = "operator"
        action = f"the queued {c.queued_seat} entry for head {c.short} is {_uncertain_hold(c)}"
    elif c.queued_seat:
        kind = "release"
        action = (f"a {c.queued_seat} slot frees — the queued run starts then (a verdict or a handoff "
                  f"ends the run holding it; the lock expiry at "
                  f"{-(-config.seat_ttl_s(c.loop, seat=c.queued_seat) // 60)}m is "
                  "the backstop)")
    elif c.full_seat:
        kind = "retry"
        action = (f"re-deliver the {'changes-requested review' if c.at_head else 'review_requested'} "
                  f"event for head {c.short} now — the {c.needed_seat} gate will queue it at "
                  f"capacity {c.used}/{c.limit}; a {c.needed_seat} slot on another PR freeing then "
                  "starts the queued run")
    else:
        return None
    return kind, action


def _next_run(c) -> tuple[str, str]:
    """Next event from an inflight mark, a verdict or request at head, else a review request."""
    if c.inflight_review:
        kind = "review-verdict"
        action = c.verdict_at + "a review for this head is already marked in flight"
    elif c.inflight_fix:
        kind = "fixer-push"
        action = c.fix_push + "a fix run for this head is already marked in flight"
    elif c.at_head and c.fixer_turn == "uncertain":
        # The gate did start it; its outcome was never recorded, and a redelivered verdict
        # re-arms no uncertain run (intake), so "its run did not start" would be false (#1507).
        kind = "operator"
        action = (f"the fixer turn at head {c.short} is uncertain, with no fix pushed — "
                  + _reconcile_first(c, "a worker may have written")
                  + " (retry refuses a run that may have written); or fix it by hand, push, and "
                  "re-request review")
    elif c.at_head:
        kind = "fixer-retry"
        action = c.to_fixer + "after checking why its run did not start — no fixer is running to push a fix"
    elif c.request_pending:
        kind = "retry"
        action = (f"a review run for head {c.short}: the pending request for "
                  f"{c.loop['reviewer_seat']} started none. A request counts only from a fixer, the "
                  "reviewer or a maintainer (triage.maintainers); anyone else's is ignored. Run "
                  f"`hermes dk review --loop {c.loop['id']} --pr {c.number}` (or ask again as "
                  "one of them); if it came from one, "
                  f"`trace --loop {c.loop['id']}` its delivery to see why the gate declined")
    else:
        kind = "review-request"
        detail = ("a fresh PR also wakes the reviewer on opened / ready_for_review, so re-driving "
                  "that event is the other way in" if not c.spent else "a push alone wakes nobody")
        action = ((f"ask for review of head {c.short}: `hermes dk review --loop {c.loop['id']} --pr "
                   f"{c.number}` — the reviewer is a GitHub App, which GitHub cannot request")
                  if config.reviewer_is_app(c.loop) else
                  (f"the fixer asks for review of head {c.short}: gh api -X POST "
                   f"repos/{c.repo}/pulls/{c.number}/requested_reviewers -f "
                   f"'reviewers[]={c.loop['reviewer_seat']}' — {detail}"))
    return kind, action
