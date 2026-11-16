"""What the operator's ``hermes dk review`` asks beyond a plain review (split from
``one_shot_action``, #1369): another verdict on a review-only PR at its cap
(``--another-round``, or with ``review_only_rerequest_grants`` a maintainer's GitHub re-request,
#2496), and the answer for a reconsider (#1334) the gate held instead of queuing.
Both read the reviews the reviewer gate itself decides from (``gate_reviews``, #1370)."""
from __future__ import annotations

from . import config, gate, gh, observer, transition


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


def rerequest_grants(loop) -> bool:
    """Whether a maintainer's GitHub re-request at a review-only PR's cap grants another round,
    as ``--another-round`` does (#2496, ``review_only_rerequest_grants``). Off unless set."""
    return loop.get("review_only_rerequest_grants") is True


def unannounced(loop) -> str:
    """Why a re-request's grant could not be announced on this loop, or ``""`` (#2501 review, F3,
    F5): no observer feed, a muted one, a route the gateway can't deliver to
    (``observer.unusable``), or ``observer.events`` without ``granted``. The grant is made away
    from the terminal, and its notice is how the maintainer sees it, so without one the re-request
    grants nothing."""
    reason = observer.unusable(loop)
    if not reason and not observer.subscribes(loop, "granted"):
        reason = "observer.events leaves out granted"
    return reason


def rerequest_can_grant(loop) -> bool:
    """Whether a re-request can grant here, so the cap notice and ``explain`` may offer it: the
    setting is on, GitHub can request the reviewer (not a GitHub App, ``reviewer_is_app``), and
    the grant's notice has a feed that sends it (``unannounced``)."""
    return rerequest_grants(loop) and not config.reviewer_is_app(loop) and not unannounced(loop)


def grant_by_rerequest(loop, st, number, head, rounds, sender) -> str:
    """The reviewer gate's grant at a review-only PR's cap (#2496), for a head with no verdict
    yet. A maintainer's (``config.maintainers``) ``review_requested``, where a re-request can grant
    (``rerequest_can_grant``), sends the urgent ``granted`` notice; the grant itself, one more
    verdict at ``head`` under ``--another-round``'s rules, is written only once the observer
    ledger records that notice delivered (#2501 review, F5). So no review starts on a grant nobody
    was told of: a failed, uncertain or unfinished POST grants nothing yet, and the first gate
    pass that finds it delivered (after the sweep's retry, a settled one, or a gate stopped after
    the receipt) writes the grant. ``sender``: '' when the pass is no re-request. Returns why a
    re-request's grant still waits for its notice, else ''."""
    if not rerequest_grants(loop) or st.review_cap_granted(number, head, rounds):
        return ""
    identity = f"rerequest:{rounds}"
    if sender in config.maintainers(loop) and rerequest_can_grant(loop):
        observer.notify(loop, st, "granted", number, head, identity=identity,
                        outcome=(f"{sender}'s re-request on GitHub (review_only_rerequest_grants): "
                                 f"one more verdict at this head, {rounds} spent"),
                        actor=sender, next_turn="the review of this head")
    key = observer.key_for(loop, number, head, "granted", identity)
    notice = observer.recorded(st, key)
    status = notice.get("status")
    if status == "delivered":
        st.review_cap_grant(number, head, rounds)
        return ""
    if not status:
        return ""
    if status == "uncertain":
        then = (f"check the chat, then `hermes dk observer --loop {loop['id']} --settle {key} "
                "--delivered` (or `--not-delivered` to have the sweep send it once)")
    elif status == "failed":
        then = f"the watchdog sweep resends it ({notice.get('error') or 'delivery failed'})"
    else:
        then = "it is being sent, or its sender stopped and the sweep settles it"
    return (f"a re-request's grant waits for its granted notice, {status}: {then}; "
            "re-request again once it is delivered")


def reconsider_held(emit, st, hold, args, head, loop) -> int:
    """A reconsider (#1334) the gate held in the seat queue instead of queuing a turn (#1369):
    no worker runtime, say. The queue entry keeps no turn key and no webhook carries a
    reconsider, so nothing replays it: a drain or a later delivery at this reviewed head only
    finds "already has a reviewer's verdict". Say it was not queued, and drop the gate's hold so
    no surface shows it as waiting. ``hold`` is what this call's gate noted
    (``util.note_gate_hold``): only the row it wrote itself (``hold["entry"]``) is removed, and
    only while it is still that exact row. A row another event wrote — before the gate's
    compare-and-swap, which then left it alone, or after it — is never this call's to drop."""
    entry = hold.get("entry")
    if isinstance(entry, dict) and st.queue_pop_if("reviewer", hold["key"], entry):
        log_reason = "the hold was dropped"
    elif isinstance(entry, dict):
        log_reason = "a newer queue entry was kept"
    else:
        log_reason = "another event's queue entry was kept"
    reason = hold.get("reason") or "held in the seat queue"
    emit(f"#{args.pr} @ {head[:7]}: reconsider NOT queued — {reason}; a held reconsider is "
         f"never replayed ({log_reason}), so run `hermes dk review --loop {loop['id']} --pr "
         f"{args.pr} --reconsider ...` again once that is resolved")
    return 2


def reconsider_ask(st, number, head, reason, ask, reconsider_runs) -> int:
    """Ask the gate for a reconsider (#1334) with ``ask()``, and return its exit code. The
    reason goes with the turn into the reviewer's prompt, so it is written first: the worker
    the gate arms may claim the turn before the gate returns. Once the gate has answered (#1384),
    it is kept only if the run ledger holds a reconsider turn at this head
    (``reconsider_runs()``, a count), so a reconsider the gate refused, held or declined, or that
    crashed, leaves no reason behind. Only this call's own entry is taken back
    (``reconsider_forget``): a newer reconsider's stays. A ledger that cannot be read keeps the
    reason, since a turn may exist."""
    written = st.reconsider_record(number, head, reason)
    try:
        return ask()
    finally:
        try:
            if not reconsider_runs():
                st.reconsider_forget(number, *written)
        except Exception:  # noqa: BLE001 - the command's answer stands; the prompt never reads it
            pass


def reconsider_cap_refusal(loop, st, number, pr, head, reviews) -> str:
    """Why the verdict cap stops a reconsider (#1453), or ``""``: the reviewer gate's own cap
    rule, read before the gate runs. At a fixer PR's spent cap the gate parks the head for
    adjudication instead of reviewing it, so a reconsider (a request for a review) must never
    reach the gate there; a review-only PR at its cap (``gate.review_cap_hit``) is declined by
    the gate, and only ``--another-round`` (which a reconsider can't be combined with) lifts it."""
    rounds = len(gate.verdicts(reviews, loop))
    author = ((pr.get("user") or {}).get("login") or "").lower()
    if author in config.review_only(loop):
        if gate.review_cap_hit(loop, st, number, head, rounds, author):
            return (f"the review cap is reached ({rounds} verdicts) — a reconsider can't lift "
                    "it; `--another-round` grants one more verdict")
    elif rounds >= loop["cap"]:
        return (f"the verdict cap is reached ({rounds} of {loop['cap']}) — a reconsider never "
                "hands a PR to adjudication; `hermes dk escalate` asks for a ruling")
    return ""
