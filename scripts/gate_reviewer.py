#!/usr/bin/env python3
"""Reviewer gate — decides whether this event starts a review run.

Route this at the reviewer's profile, on the ``pull_request`` event. It wakes the reviewer
when its turn *starts*:

* ``opened`` / ``ready_for_review`` / ``reopened`` — a new PR needs a first look;
* ``review_requested`` — the fixer asked for one, and only when the request names this
  seat and comes from the fixer side;
* **never** on ``synchronize``. A push is not a turn: the fixer pushes, *then* asks, and
  GitHub clears a review request the moment a verdict lands — so the explicit ask is the
  only signal that means "review me now". Intermediate pushes cost nothing.
* ``closed`` — the review is over (merged or abandoned): reclaim the PR's local disk and
  let it go. No agent runs, no tokens.

stdin : a GitHub webhook payload
stdout: ``[SILENT]`` (eligible runs queue; whole-agent isolation not available)
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from diaktoros import (  # noqa: E402
    config,
    gate,
    gh,
    observer,
    transition,
)
from diaktoros.util import log, silence  # noqa: E402

ACTIONS = {"opened", "ready_for_review", "reopened", "review_requested"}


def loop_worked_on(loop: dict, st, number: int, pr: dict) -> bool:
    """Whether the loop has any stake in this PR: a fixer's, or one its state has an entry for."""
    author = ((pr or {}).get("user") or {}).get("login") or ""
    if author.lower() in config.reviewed_authors(loop):
        return True
    try:
        if st.breach_get(number) or st.transition_get(number):
            return True
        key = gate.seat_key(loop, number)
        if any(isinstance(items, dict) and key in items for items in st.queue_all().values()):
            return True
        prefix = f"{loop.get('id') or loop.get('repo') or 'loop'}:{number}:"
        data = json.loads(st.observations.read_text())
        return any(k.startswith(prefix) for k in (data.get("entries") or {}))
    except Exception:  # noqa: BLE001 - unreadable state: unknown, so stay quiet
        return False


def main() -> None:
    payload = json.load(sys.stdin)
    loop, st = gate.context(payload)
    pr = gate.pr_of(payload)
    action = payload.get("action", "")

    if action == "closed":
        number = gate.number_of(payload, pr)
        # A delayed close can arrive after a reopen, or while GitHub is unavailable. Neither
        # the event snapshot nor cleanup's quiet zero exit code proves anything was removed.
        current = gh.pr(loop, number)
        if (not isinstance(current, dict) or current.get("number") != number
                or current.get("state") != "closed"):
            silence("close event is stale or current PR state is unavailable")
        closing = "merged" if (current.get("merged") or current.get("merged_at")) else "closed"
        gate.reclaim(loop, number, closing)
        # Cleanup above is unconditional; the notice is only for PRs the loop worked on.
        if not loop_worked_on(loop, st, number, current):
            silence()
        # Reclaim is best-effort and has no success receipt. Report the close, not freed disk.
        observer.notify(loop, st, "closed", number, (current.get("head") or {}).get("sha") or "",
                        identity=closing, outcome=closing, next_turn="nothing — cleanup attempted")
        silence()

    if action == "edited":
        # A base edit is only a hint to re-read the live PR. A stacked child now on trunk at
        # the same head starts a fresh review situation: the transition's one isolated
        # reviewer turn, deduplicated by its turn key against redeliveries and the sweep.
        number = gate.number_of(payload, pr)
        live = gh.pr(loop, number)
        if (isinstance(live, dict) and live.get("number") == number and
                live.get("state") == "open" and
                (live.get("base") or {}).get("ref") == loop["base"]):
            if transition.record(loop, st, number, (live.get("head") or {}).get("sha"),
                                 loop["base"]):
                status, detail = transition.start_fresh_review(loop, st, number, live=live)
                log(f"#{number} retarget: fresh review {status} ({detail})")
                silence(f"base retarget: fresh review {status} — {detail}")
        silence("base edit observed — no reviewer run")
    if action not in ACTIONS:
        silence(f"action {action!r} is not a review trigger")

    sender = ((payload.get("sender") or {}).get("login") or "").lower()
    if action == "review_requested":
        # Only an explicit request for THIS seat counts, and only from the fixer side or a loop
        # maintainer (#375): a request aimed at another reviewer, or from a stranger, is
        # somebody else's business.
        requested = ((payload.get("requested_reviewer") or {}).get("login") or "").lower()
        if requested != loop["reviewer_seat"]:
            silence(f"review requested from {requested or 'nobody'} — not this seat")
        # A review-only author (#191) may ask again for a review of their own PR.
        own = sender in config.review_only(loop) and sender == (
            ((pr.get("user") or {}).get("login") or "").lower())
        if (sender not in set(loop["fixers"]) and sender not in loop["reviewers"]
                and sender not in config.maintainers(loop) and not own):
            silence(f"sender {sender or 'unknown'} is not a fixer, the reviewer or a maintainer "
                    "(triage.maintainers)")

    if pr.get("draft"):
        silence("draft PR")
    if (pr.get("base") or {}).get("ref") != loop["base"]:
        silence(f"base is not {loop['base']}")
    author = ((pr.get("user") or {}).get("login") or "").lower()
    if author not in config.reviewed_authors(loop):
        silence(f"author {author or 'unknown'} is not a fixer or a review-only author for "
                "this loop")

    seat = "reviewer"
    number = gate.number_of(payload, pr)
    head = gate.head_of(pr)
    # Every trigger can arrive after a push or close. Never claim a seat or escalate
    # using the event snapshot if GitHub now points at another head.
    current = gh.pr(loop, number)
    if (not isinstance(current, dict) or current.get("number") != number
            or current.get("state") != "open"
            or (current.get("head") or {}).get("sha") != head):
        silence("review trigger is stale or current PR is unavailable")
    # Eligibility must be judged against current facts, not only the old snapshot.
    if (current.get("draft") or (current.get("base") or {}).get("ref") != loop["base"]
            or ((current.get("user") or {}).get("login") or "").lower()
            not in config.reviewed_authors(loop)):
        silence("current PR is no longer eligible for this review")
    snapshot_base_sha = (pr.get("base") or {}).get("sha")
    # Only a stacked base's generation decides which diff is under review; trunk moving on
    # does not make a direct-trunk review request stale.
    if (snapshot_base_sha and (pr.get("base") or {}).get("ref") != loop["base"]
            and snapshot_base_sha != (current.get("base") or {}).get("sha")):
        silence("review trigger is from an older base generation")
    boundary = transition.record(loop, st, number, head, loop["base"])
    if boundary and transition.baseline_missing(boundary):
        silence(f"base retarget hold: {transition.MISSING_BASELINE}")
    fresh_key = transition.turn_key(boundary) if boundary else ""
    if boundary and not fresh_key:
        silence("base retarget hold: transition time unreadable — cannot name its fresh turn")

    # After a same-head retarget, only host-receipted post-boundary reviews count: a request
    # is neither a receipt nor a second fresh turn. It can only re-drive the transition's own
    # turn (same turn key, so the ledger dedups it against the sweep and the edited hook).
    reviews = transition.effective_reviews(loop, st, number, head, gate.fetch_reviews(loop, number))
    if not isinstance(reviews, list):
        silence("host review receipts unreadable — not guessing which reviews count")
    if gate.reviewed_at_head(reviews, loop, head):
        silence(f"head {head[:7]} already has a reviewer's verdict")
    held = st.human_hold(number, head)
    if isinstance(held, dict):
        # #478: the loop's review of this head ended with its approval left to a person.
        silence(f"head {head[:7]} waits for a person's approval (human_paths: "
                f"{held.get('why') or 'reserved paths'})")
    dismissed = [int(r['id']) for r in gate.reviews_at_head(reviews, loop, head)
                 if gh.review_state(r) == 'DISMISSED' and type(r.get('id')) is int and r['id'] > 0]
    turn_key = f'dismissed:{max(dismissed)}' if dismissed else fresh_key
    if st.inflight(f"review:{number}:{head}"):
        silence(f"a review for head {head[:7]} is already out")

    if gate.escalated(st.breach_get(number), head):
        silence(f"#{number} @ {head[:7]} is escalated to adjudication — no further review here")
    rounds = len(gate.verdicts(reviews, loop))
    # A review-only author's PR (#191) has no verdict cap: no fixer rounds are spent, the author
    # answers each verdict by pushing and asking again, so there is nothing to adjudicate.
    if author in config.review_only(loop):
        # ...but it does have its own verdict cap (review_only_cap, default `cap`). At the cap
        # the gate declines with nothing spent, whoever asks: the author re-requesting their own
        # PR is no bypass. Only a maintainer's --another-round (one verdict, this head) lifts it.
        ro_cap = config.review_only_cap(loop)
        if rounds >= ro_cap and not st.review_cap_granted(number, head, rounds):
            if st.review_cap_notice_once(number, head):
                observer.notify(
                    loop, st, "held", number, head, identity=f"review-cap:{head}",
                    outcome=(f"review cap reached on #{number} ({rounds} verdicts): no further "
                             "review until a maintainer grants another round — merge, close, or "
                             f"`hermes dk review --loop {loop['id']} --pr {number} "
                             "--another-round`"),
                    next_turn="nothing — waiting for a maintainer")
            silence(f"review cap reached on #{number} ({rounds} verdicts) — no further review "
                    "until a maintainer grants another round")
    elif rounds >= loop["cap"]:
        gate.breach(loop, st, number, head, rounds,
                    f"review cap reached — {loop['cap']} verdicts, no approval; another review "
                    f"would loop forever")
        silence(f"cap reached on #{number} — handed to adjudication")

    # A request *is* the handoff: the fixer pushes, then asks. That is the only signal that means
    # the fixer is finished with this PR, so it is what frees the fixer's slot. A review must never
    # start against a PR the fixer is still working — and everything else (opened, ready_for_review,
    # reopened) is *not* a handoff, so if the fixer still holds this PR the claim below queues us.
    # A maintainer's request (#375) asks for a review; it is not the fixer finishing, so it never
    # frees the fixer's slot — if the fixer still holds the PR, the claim below queues the review.
    handoff = action == "review_requested" and (sender in set(loop["fixers"])
                                                or sender in loop["reviewers"])
    if handoff and st.release_if("fixer", gate.seat_key(loop, number)):
        log(f"released fixer seat for {gate.seat_key(loop, number)}")
    gate.drain_seat(loop, "fixer")

    gate.block_pr_agent(
        loop, st, seat, number, head, turn_key=turn_key,
        on_queued=lambda: observer.notify(
            loop, st, "handoff" if action == "review_requested" else "opened", number, head,
            identity=action, actor=sender if action == "review_requested" else author,
            next_turn="reviewer queued", round_no=rounds + 1))


if __name__ == "__main__":
    # Crash, overrun or a silence after a failed read is recorded for the watchdog (#75).
    from diaktoros import gate_failures  # noqa: E402
    gate_failures.run("gate_reviewer", main)
