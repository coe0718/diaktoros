#!/usr/bin/env python3
"""Fixer gate — decides whether this event starts a fix run.

Route this at the fixer's profile, on the ``pull_request_review`` event. It wakes the fixer
when a verdict it must answer lands:

* ``submitted`` with state ``changes_requested``, authored by one of the loop's reviewers,
  **at the current head** (a verdict on an older commit is already superseded) — current as
  GitHub says *now*, with this review still the latest effective verdict there;
* ``commented`` and ``approved`` end here — approval is where the loop stops being useful;
* the verdict that reaches the cap is **not** a work order. Handing the fixer a fourth fix
  no reviewer will read is how a loop burns a night; the PR goes to adjudication instead;
* a loop without unattended fixer pushes (the default) starts no fixer turn: the verdict is
  held for the operator with the enable command, and the watchdog re-evaluates it after opt-in.

stdin : a GitHub webhook payload
stdout: ``[SILENT]`` (eligible runs queue; whole-agent isolation not available)
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import NoReturn

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from diaktoros import (  # noqa: E402
    config,
    escalate_pending,
    findings,
    gate,
    gh,
    issue_facts,
    observer,
    transition,
)
from diaktoros.util import log, silence  # noqa: E402

# The breach outcomes that leave the head parked for a ruling (``stale`` parks nothing).
PARKED = gate.BREACH_PARKED


def _take_pending_escalation(loop: dict, st, number: int, pr_head: str, prior: int,
                             pending: dict) -> NoReturn:
    """Act on a pending escalation (#1246) at this changes-requested verdict: the operator asked
    for a ruling while a run was in flight, so the adjudicator rules on this head instead of the
    fixer. Always ends the gate with ``silence`` (no fix turn), whatever the breach returned."""
    import secrets
    from diaktoros.util import now_iso
    why = str(pending.get("reason") or "the operator asked for a ruling")
    outcome = gate.breach(loop, st, number, pr_head, prior + 1, f"escalated: {why}",
                          escalated={"by": str(pending.get("by") or gate.OPERATOR_ESCALATION),
                                     "at": now_iso(), "reason": why,
                                     "operation_id": secrets.token_hex(16),
                                     "pending_since": pending.get("at")})
    if outcome not in PARKED:
        # #1264: nothing was parked (the head moved under the breach lock), so the request
        # stands for the next changes-requested verdict, and the log says what happened.
        silence(f"#{number} @ {pr_head[:7]} pending escalation not taken: the breach was "
                f"{outcome}; it stays pending for the next verdict")
    escalate_pending.take(st, number)
    silence(f"#{number} @ {pr_head[:7]} pending escalation taken at verdict {prior + 1} — "
            "handed to adjudication instead of a fix", "accepted")


def _escalate_instead(loop: dict, st, number: int, pr_head: str, prior: int, pr: dict,
                      review: dict) -> None:
    """A ruling instead of a fix round at this changes-requested verdict, when one is owed: the
    operator's pending escalation (#1246), else a blocking finding that names the PR itself or
    an issue it closes (#754: the fixer cannot deliver its own PR's work, and a round there only
    burns the cap). Returns only when neither applies, and the fixer gets the finding."""
    pending = escalate_pending.get(st, number)
    if pending:
        _take_pending_escalation(loop, st, number, pr_head, prior, pending)
    # #2548: the ids recorded before this review, so a state line reads as the broker read it.
    known = findings.recorded_before(st.findings_get(number), review.get("id"))
    cited = issue_facts.cited_own_work(loop, number, pr.get("body"),
                                       (pr.get("head") or {}).get("ref"), review.get("body"), known)
    if not cited:
        return
    import secrets
    from diaktoros.util import now_iso
    why = (f"a blocking finding names {', '.join(f'#{n}' for n in cited)}, this PR or an issue "
           "it closes: a fix round cannot deliver the PR's own work")
    outcome = gate.breach(loop, st, number, pr_head, prior + 1, f"escalated: {why}",
                          escalated={"by": gate.CYCLE_ESCALATION, "at": now_iso(), "reason": why,
                                     "operation_id": secrets.token_hex(16)})
    if outcome not in PARKED:
        silence(f"#{number} @ {pr_head[:7]} finding cites the PR's own work, but the breach was "
                f"{outcome}: nothing parked, no fix queued")
    silence(f"#{number} @ {pr_head[:7]} {why} — handed to adjudication instead of a fix",
            "accepted")


def _author(loop: dict, pr: dict) -> str:
    """The PR's author, lowercased: one whose PRs the loop reviews, or the gate silences."""
    user = pr.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    author = author.lower() if isinstance(author, str) else ""
    if author not in config.reviewed_authors(loop):
        silence(f"PR author {author or 'unknown'} is not an authorized fixer or review-only author")
    return author


def _verdict_of(loop: dict, payload: dict) -> dict:
    """The submitted review in ``payload``, by one of the loop's reviewers, or the gate silences."""
    review = payload.get("review")
    if not isinstance(review, dict):
        silence("no review in payload")

    if payload.get("action", "") != "submitted":
        silence(f"review action {payload.get('action')!r} is not a verdict landing")

    # GitHub's webhook payloads spell review states lowercase ("changes_requested"); the REST
    # API shouts them ("CHANGES_REQUESTED"). Compare case-insensitively on both sides of that
    # boundary — assuming one spelling is how the fixer leg of this loop died on arrival once.
    if gate.reviewer_login(review) not in set(loop["reviewers"]):
        silence(f"verdict author {gate.reviewer_login(review) or 'unknown'} is not a reviewer")
    return review


def _quarantine_hold(loop: dict, number: int) -> None:
    """Silence while a post-write push on this PR is quarantined for the operator."""
    # A ref may have advanced while PR metadata became unverifiable. Its
    # supervisor hold is authoritative even if a later webhook says approved:
    # neither a merge handoff nor another fixer turn may bypass inspection.
    from diaktoros.run_supervisor import Supervisor
    ledger = config.host_path("ledger")
    if ledger.exists() and Supervisor(ledger).post_write_hold(loop['repo'], number):
        silence('post-write push quarantined — operator inspection required; no merge handoff')


def _release_approved_claim(loop: dict, st, key: str, review: dict) -> bool:
    """Free the reviewer claim on an approved head; True when one was freed (and drained)."""
    # An approval ends the turn that was claimed for the approved head. Free that claim before
    # the checks that gate the merge handoff and fixer work: a failed read or a retarget must
    # not keep the reviewer's seat until its TTL. Only that head's claim ends, so a late
    # webhook cannot end a newer run on the same PR.
    if (str(review.get("state", "")).upper() == "APPROVED" and review.get("commit_id")
            and st.release_if("reviewer", key, review["commit_id"])):
        log(f"released reviewer seat for {key}")
        gate.drain_seat(loop, "reviewer")
        return True
    return False


def _verify_rejection_snapshot(loop: dict, st, number: int, pr: dict) -> None:
    """A verdict other than an approval is a work order: silence unless the webhook's PR
    snapshot still matches the live PR on the loop's base, with no unresolved retarget hold."""
    # A webhook is a delivered snapshot, not permission to release a seat, announce
    # approval, escalate, or start a fixer after the PR was retargeted. Stacked reviews
    # have no trustworthy review-ID/base association in this direct-gh workflow yet.
    if (pr.get("base") or {}).get("ref") != loop["base"]:
        silence("stacked/retargeted verdict has no verified review situation")
    live = gh.pr(loop, number)
    if (not isinstance(live, dict) or live.get("number") != number
            or live.get("state") != "open" or live.get("draft")
            or (live.get("base") or {}).get("ref") != loop["base"]
            or (live.get("head") or {}).get("sha") != (pr.get("head") or {}).get("sha")):
        silence("verdict snapshot is stale or live PR unavailable")
    snapshot_base_sha = (pr.get("base") or {}).get("sha")
    live_base_sha = (live.get("base") or {}).get("sha")
    # Trunk moving on does not supersede a verdict on a direct-trunk PR; a stacked base's
    # generation does. The merge handoff still rechecks the base.
    if (snapshot_base_sha and (pr.get("base") or {}).get("ref") != loop["base"]
            and live_base_sha != snapshot_base_sha):
        silence("verdict base generation changed")
    boundary = transition.record(loop, st, number, (live.get("head") or {}).get("sha"), loop["base"])
    if boundary and transition.baseline_missing(boundary):
        silence(f"base retarget hold: {transition.MISSING_BASELINE}")
    # Otherwise a held head continues: review commit_id binds only the head, so later
    # only a post-boundary review with a host receipt from the fresh isolated reviewer
    # run (transition.effective_reviews) can be this work order.


def _approval_facts(loop: dict, st, number: int, pr: dict, review: dict) -> dict:
    """What the live PR says about this approval: every read behind the merge handoff."""
    # The webhook PR is a snapshot: an approval can arrive after a push, close or failed
    # lookup. Only a matching, currently open live head authorizes a merge handoff.
    approved_head = review.get("commit_id") or ""
    current = gh.pr(loop, number)
    live_open = (isinstance(current, dict) and current.get("number") == number
                 and current.get("state") == "open")
    current_head = ""
    if live_open and isinstance(current, dict):
        current_head = ((current.get("head") or {}).get("sha") or "")
    snapshot_matches = approved_head == ((pr.get("head") or {}).get("sha") or "")
    # The review commit pins only the child. A retarget or base advance can
    # change the reviewed diff without changing that child SHA.
    base_sha = observer.verified_base_sha(loop, current) if live_open else ""
    base_matches = bool(base_sha and observer.base_identity(loop, pr) == base_sha)
    # The webhook is not evidence of the review's *current* verdict: GitHub can dismiss
    # the same review id after submitting it. A failed/partial live read is not approval.
    reviews = gh.reviews(loop, number) if live_open and current_head == approved_head else None
    # After a same-head retarget only a host-receipted post-boundary approval is one; an
    # old approval, or one GitHub merely lists after the boundary, is not.
    reviews = (transition.effective_reviews(loop, st, number, current_head, reviews)
               if current_head else None)
    latest = (gate.latest_effective_review_at_head(reviews, loop, approved_head)
              if isinstance(reviews, list) else None)
    live_approval = (latest is not None and latest.get("id") == review.get("id")
                     and gh.review_state(latest) == "APPROVED"
                     and gate.reviewer_login(latest) == gate.reviewer_login(review))
    facts = {"approved_head": approved_head, "current_head": current_head,
             "live_open": live_open, "snapshot_matches": snapshot_matches,
             "base_sha": base_sha, "base_matches": base_matches, "live_approval": live_approval}
    facts.update(_approval_base_facts(loop, st, number, pr, current, facts))
    return facts


def _approval_base_facts(loop: dict, st, number: int, pr: dict, current, facts: dict) -> dict:
    """The base half of the approval facts: ``same_base`` (the live PR still describes the
    webhook's direct-trunk generation) and ``base_moved`` with its ``pr_base`` and ``tip``."""
    live_open, current_head = facts["live_open"], facts["current_head"]
    # A review's commit_id binds the head only. The live PR must still describe the
    # same direct-trunk generation as the webhook snapshot; a same-head base retarget or
    # advancement between reads makes the approval an unknown-diff verdict. A retarget
    # hold no longer vetoes by itself: live_approval already had to be a receipted
    # post-boundary review, whose pinned generation is this head on the root base.
    first_base = pr.get("base") or {}
    final_base = (current.get("base") or {}) if isinstance(current, dict) and live_open else {}
    boundary = transition.hold(st, number, current_head) if current_head else None
    same_base = (final_base.get("ref") == loop["base"]
                 and first_base.get("ref") == final_base.get("ref")
                 and first_base.get("sha") == final_base.get("sha")
                 and not transition.baseline_missing(boundary))
    # #595: the base branch moved on since the PR's recorded base. The review still holds
    # (the diff against the merge base is unchanged), but the PR's CI ran against the older
    # base, so this is not a merge hand-off: say what is known instead of "unverified".
    pr_base = observer.base_identity(loop, pr)
    tip = observer.base_tip(loop) if live_open and not facts["base_matches"] else ""
    base_moved = bool(pr_base and tip and tip != pr_base
                      and observer.base_identity(loop, current) == pr_base)
    return {"same_base": same_base, "base_moved": base_moved, "pr_base": pr_base, "tip": tip}


def _approval_handoff(loop: dict, facts: dict) -> tuple[str, str]:
    """``(outcome, next_turn)`` for the approved notice: a merge handoff only on a verified
    live approval of the current head on the same base."""
    approved_head, current_head = facts["approved_head"], facts["current_head"]
    verified = bool(facts["live_open"] and current_head and approved_head == current_head
                    and facts["snapshot_matches"] and facts["live_approval"]
                    and facts["same_base"])
    if facts["live_open"] and current_head and approved_head and approved_head != current_head:
        return "on an older head — the PR moved since", "the reviewer, on this head"
    if verified and facts["base_matches"]:
        return "", "you merge"
    if verified and facts["base_moved"]:
        pr_base, tip = facts["pr_base"], facts["tip"]
        outcome = (f"{loop['base']} moved since this PR's base ({pr_base[:7]} → {tip[:7]}): "
                   f"its CI ran against the older {loop['base']}")
        return outcome, f"you — update the branch for CI on current {loop['base']}, then merge"
    return "current approval/head/base unverified — no merge handoff", "check current PR state"


def _approval(loop: dict, st, number: int, key: str, pr: dict, review: dict,
              approved_early: bool) -> NoReturn:
    """An approval: announce it, with a merge handoff only when verified live; end the
    reviewer's turn; drop a pending escalation it settles. Always silences (no fixer)."""
    # An approval ends the reviewer's turn exactly as a rejection does, and nothing else would
    # free that slot before it expired. A slot that leaks for `ttl_min` is a queue that stops
    # moving — on a busy repo, that is the difference between ten review slots and nine.
    facts = _approval_facts(loop, st, number, pr, review)
    outcome, next_turn = _approval_handoff(loop, facts)
    approved_head, current_head = facts["approved_head"], facts["current_head"]
    # The claim for the approved head was freed already; a verified handoff also ends any other.
    released = st.release_if("reviewer", key) if next_turn == "you merge" else False
    if released:
        log(f"released reviewer seat for {key}")
    if (released or next_turn == "you merge") and not approved_early:
        gate.drain_seat(loop, "reviewer")
    observer.notify(loop, st, "approved", number, approved_head,
                    identity=review.get("id"), actor=gate.reviewer_login(review),
                    outcome=outcome, next_turn=next_turn,
                    base_sha=facts["base_sha"] if facts["base_matches"] else "")
    # Only a live approval of the current head settles it; a stale or unverified one does not.
    dropped = (escalate_pending.take(st, number)
               if facts["live_approval"] and current_head and approved_head == current_head
               else None)
    if dropped:
        # #1246: approved before a ruling was needed; nothing is left to rule on.
        observer.notify(loop, st, "escalation", number, approved_head,
                        identity=f"pending-dropped:{approved_head}",
                        outcome="pending escalation dropped: approved before a ruling was "
                                f"needed ({dropped.get('reason')})",
                        next_turn=next_turn)
    silence(f"#{number} approval event — no fixer dispatch")


def _live_rejection(loop: dict, st, number: int, pr: dict, review: dict) -> tuple[str, int]:
    """``(pr_head, prior)`` for a changes-requested verdict that is the live latest effective
    verdict at the PR's current head, with no fix run already out for it; else silence."""
    pr_head = gate.head_of(pr)
    if review.get("commit_id") != pr_head:
        silence("verdict is on an older head — superseded")

    # The payload's head is a snapshot from when the review was submitted; the PR may have moved,
    # closed, or had this verdict superseded (a later approval, a dismissal) since. A fix run is a
    # work order, so it needs the live PR at this head *and* this review as the latest effective
    # verdict there — the same chronology rule the approval path uses for a merge handoff.
    current = gh.pr(loop, number)
    if (not isinstance(current, dict) or current.get("number") != number
            or current.get("state") != "open" or current.get("draft")
            or (current.get("base") or {}).get("ref") != loop["base"]
            or ((current.get("user") or {}).get("login") or "").lower()
            not in config.reviewed_authors(loop)
            or (current.get("head") or {}).get("sha") != pr_head):
        silence("verdict PR is stale or current state is unverified")
    reviews = transition.effective_reviews(loop, st, number, pr_head,
                                           gate.fetch_reviews(loop, number))
    if not isinstance(reviews, list):
        silence("host review receipts unreadable — not guessing which verdicts count")
    latest = gate.latest_effective_review_at_head(reviews, loop, pr_head)
    if (latest is None or latest.get("id") != review.get("id")
            or gh.review_state(latest) != "CHANGES_REQUESTED"
            or gate.reviewer_login(latest) != gate.reviewer_login(review)):
        silence("changes-requested webhook is not the live latest effective verdict"
                + (" (after a retarget only a host-receipted post-boundary verdict counts)"
                   if transition.hold(st, number, pr_head) else ""))
    prior = len(gate.verdicts(reviews, loop, exclude_id=review.get("id")))
    if st.inflight(f"fix:{number}:{pr_head}"):
        silence(f"a fix run for head {pr_head[:7]} is already out")
    return pr_head, prior


def _return_to_author(loop: dict, st, number: int, pr_head: str, prior: int, review: dict,
                      author: str) -> NoReturn:
    """Return a changes-requested verdict on a review-only author's PR to its author."""
    # #191: the fixer never touches a review-only author's PR. The verdict goes back to the
    # author (their push and request start the next review). The PR spends no fixer rounds and
    # is never adjudicated; its verdicts still have a cap (review_only_cap).
    observer.notify(loop, st, "verdict", number, pr_head, identity=review.get("id"),
                    outcome="changes requested", actor=gate.reviewer_login(review),
                    next_turn=f"{author}: returned to the author (review-only — no fixer)",
                    round_no=prior + 1)
    silence(f"#{number} changes requested on a review-only author's PR — returned to {author}")


def _adjudicated_instead(loop: dict, st, number: int, pr_head: str, prior: int) -> None:
    """Silence when this head is already escalated, or when this verdict reaches the cap (the
    PR is handed to adjudication instead of a fix). Returns only when neither applies."""
    if gate.escalated(st.breach_get(number), pr_head):
        silence(f"#{number} @ {pr_head[:7]} is escalated to adjudication — no fix turn")
    if prior + 1 >= loop["cap"]:
        outcome = gate.breach(loop, st, number, pr_head, prior + 1,
                              f"verdict {prior + 1} returned changes requested — loop exhausted "
                              f"({loop['cap']} reviews / {loop['cap'] - 1} fixes)")
        if outcome not in PARKED:
            # Shadow on #1265: nothing was parked, so the log must not say it was handed over.
            silence(f"cap reached on #{number} — but the breach was {outcome}: nothing parked, "
                    "no fix queued; the next verdict tries again")
        escalate_pending.take(st, number)          # the cap's breach is the ruling (#1246)
        silence(f"cap reached on #{number} — handed to adjudication instead of a fix")


def _queue_fixer(loop: dict, st, number: int, pr_head: str, prior: int, review: dict) -> None:
    """Hand the verdict to the fixer seat, or hold it for the operator when fixer pushes are off."""
    # A loop that has not opted in to unattended fixer pushes gets no fixer turn: it could not
    # publish, so it would spend a model conversation and fail. block_pr_agent holds the verdict
    # for the operator instead (no ledger row), and the same notice says what to run. Its key is
    # the review id, so a redelivered verdict — or the drain after an opt-in — is not a second one.
    def verdict_notice(next_turn: str):
        return lambda: observer.notify(
            loop, st, "verdict", number, pr_head, identity=review.get("id"),
            outcome="changes requested", actor=gate.reviewer_login(review),
            next_turn=next_turn, round_no=prior + 1)

    gate.block_pr_agent(
        loop, st, "fixer", number, pr_head,
        on_queued=verdict_notice("fixer queued"),
        on_push_off=verdict_notice(gate.fixer_push_off_notice(loop)))


def main() -> None:
    """Read a review webhook from stdin and decide: an approval's notice and merge handoff, or
    a changes-requested verdict's fixer turn, ruling or hand-back. Every other path silences."""
    payload = json.load(sys.stdin)
    loop, st = gate.context(payload)
    if isinstance(payload.get("check_suite"), dict):   # #681: a finished CI run, on its own path
        from diaktoros import ci_event
        ci_event.gate(loop, st, payload)
    pr = gate.pr_of(payload)
    review = _verdict_of(loop, payload)
    author = _author(loop, pr)
    review_only = author in config.review_only(loop)
    number = gate.number_of(payload, pr)
    key = gate.seat_key(loop, number)
    _quarantine_hold(loop, number)
    approved_early = _release_approved_claim(loop, st, key, review)

    state = str(review.get("state", "")).upper()
    # An approval reaches its own verification, which rechecks the live head, base and
    # retarget hold before any merge cue and otherwise reports it unverified. A rejection is a
    # work order, so it must pass the snapshot checks before anything else happens.
    if state == "APPROVED":
        _approval(loop, st, number, key, pr, review, approved_early)
    _verify_rejection_snapshot(loop, st, number, pr)
    if state != "CHANGES_REQUESTED":
        silence(f"verdict state {review.get('state')!r} needs no fix")
    pr_head, prior = _live_rejection(loop, st, number, pr, review)

    # A verdict landed: the reviewer's turn is over, and anything queued behind it can start.
    if st.release_if("reviewer", key):
        log(f"released reviewer seat for {key}")
    gate.drain_seat(loop, "reviewer")

    if review_only:
        _return_to_author(loop, st, number, pr_head, prior, review, author)
    _adjudicated_instead(loop, st, number, pr_head, prior)
    _escalate_instead(loop, st, number, pr_head, prior, pr, review)
    _queue_fixer(loop, st, number, pr_head, prior, review)



if __name__ == "__main__":
    # Crash, overrun or a silence after a failed read is recorded for the watchdog (#75).
    from diaktoros.gate import failures as gate_failures  # noqa: E402
    gate_failures.run("gate_fixer", main)
