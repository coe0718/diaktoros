"""What the operator's ``hermes dk review`` asks beyond a plain review (split from
``one_shot_action``, #1369): another verdict on a review-only PR at its cap
(``--another-round``), and the answer for a reconsider (#1334) the gate held instead of queuing.
Both read the reviews the reviewer gate itself decides from (``gate_reviews``, #1370)."""
from __future__ import annotations

from . import config, gate, gh, transition


def gate_reviews(loop, st, number, head):
    """The reviews the reviewer gate decides from at ``head`` (#1370): ``gh.reviews`` through
    ``transition.effective_reviews``, so under a retarget hold only receipt-confirmed reviews
    count. ``None`` when they (or the receipts) could not be read."""
    return transition.effective_reviews(loop, st, number, head, gh.reviews(loop, number))


def grant_another_round(loop, args, pr, head, st, emit):
    """``review --another-round``: one more verdict on a review-only PR that hit its cap. The
    exit code when nothing is granted (the review is not asked for), else None."""
    # The operator running this on the host is the maintainer; the grant is recorded against
    # this head and verdict count, so it buys exactly one verdict and a repeat of the command
    # changes nothing.
    author = ((pr.get("user") or {}).get("login") or "").lower()
    if author not in config.review_only(loop):
        emit(f"#{args.pr}: {author or 'its author'} is not review-only — only a review-only "
              "PR has a review cap to lift; nothing granted")
        return 1
    reviews = gate_reviews(loop, st, args.pr, head)
    if not isinstance(reviews, list):
        emit(f"#{args.pr}: the reviews could not be read — nothing granted")
        return 2
    rounds = len(gate.verdicts(reviews, loop))
    if rounds < config.review_only_cap(loop):
        emit(f"#{args.pr}: {rounds} of {config.review_only_cap(loop)} verdicts spent — the "
              "cap is not reached; nothing to grant")
        return 1
    if gate.reviewed_at_head(reviews, loop, head):
        # The gate declines a head that already has a verdict before it looks at the cap, so a
        # grant here would be spent on nothing and not match the author's next push.
        emit(f"#{args.pr} @ {head[:7]}: head already has a verdict — push the change, then "
              "grant another round; nothing granted")
        return 1
    if not st.review_cap_grant(args.pr, head, rounds):
        emit(f"#{args.pr} @ {head[:7]}: another round is already granted for this head")
    else:
        emit(f"#{args.pr} @ {head[:7]}: another round granted — one more verdict")
    return None


def reconsider_held(emit, st, key, waiting, args, head, loop) -> int:
    """A reconsider (#1334) the gate held in the seat queue instead of queuing a turn (#1369):
    no worker runtime, say. The queue entry keeps no turn key and no webhook carries a
    reconsider, so nothing replays it: a drain or a later delivery at this reviewed head only
    finds "already has a reviewer's verdict". Say it was not queued, drop the gate's hold so no
    surface shows it as waiting (the exact entry only; a newer one stays), and ask again."""
    if st.queue_pop_if("reviewer", key, waiting):
        log_reason = "the hold was dropped"
    else:
        log_reason = "a newer queue entry was kept"
    reason = waiting.get("reason") or "held in the seat queue"
    emit(f"#{args.pr} @ {head[:7]}: reconsider NOT queued — {reason}; a held reconsider is "
         f"never replayed ({log_reason}), so run `hermes dk review --loop {loop['id']} --pr "
         f"{args.pr} --reconsider ...` again once that is resolved")
    return 2
