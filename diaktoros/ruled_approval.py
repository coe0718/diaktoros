"""#905: the adjudicator acts on its ACCEPT ruling, so the PR can merge.

An ACCEPT says the reviewer's remaining change request does not block. Left alone it changes
nothing on GitHub: the change request still blocks a base that requires a review, and ``ready``
waits for a person. With ``adjudicator_approves`` on, the watchdog sweep acts on it, as the
adjudicator's own login (``seats.adjudicator.login``), once all of these hold, re-read live:

* the newest ruling at the PR's current head is ACCEPT, and the reviewer's verdict there is a
  change request (a shadow escalation's ACCEPT leaves an approval standing: nothing to do);
* the required checks are green at that head;
* the diff touches no ``human_paths`` path (those approvals are a person's, #478);
* no maintainer hold (#477), and the PR is open, not a draft, by an authorized fixer.

Then it **approves** the head, and **dismisses** the reviewer's change request there, the one the
ruling overruled. The loop's readers count that approval only while an ACCEPT backs it
(``gate.ruled_approvals``), so the dismissal never makes the head look unreviewed. Each write is
recorded before it is sent (``ruled_approvals``): a lost response is ``uncertain`` and never
replayed, and a refusal is ``denied`` with its reason. Only the reviewer seat's change request at
the ruled head is dismissed: one by anyone else (a maintainer, the adjudicator itself) or at
another head, which GitHub also still counts, is left alone, and the merge check names it for a
person. A wait (red
or running CI, a hold, an unreadable read) is checked again on the next sweep.
"""
from __future__ import annotations

from . import broker, ci, config, gate, gh, pr_hold
from .run_supervisor import Supervisor, production_ledger, views


def sweep(loop: dict, st, prs: list, log=lambda _msg: None) -> list[int]:
    """Act on each open loop PR's ACCEPT, as above; the PRs whose state moved. Never raises for
    one PR: its failure is logged and the next PR is tried."""
    if not config.adjudicator_approves(loop):
        return []
    db = production_ledger()
    moved = []
    for pr in prs:
        if (listed := config.loop_pr(loop, pr)) is None:
            continue
        number, head, _author = listed
        if views.ruling_at(db, loop["repo"], number, head) != "ACCEPT":
            continue
        try:
            if _act(loop, st, Supervisor(db, create=False), pr, number, head, log):
                moved.append(number)
        except Exception as exc:  # noqa: BLE001 - one PR's failure never stops the sweep
            log(f"#{number}: acting on the ACCEPT failed: {type(exc).__name__}: {exc}")
    if moved:
        from . import merge_check
        for number in moved:
            merge_check.refresh_quietly(loop, number)
    return moved


def _act(loop: dict, st, sup, pr: dict, number: int, head: str, log) -> bool:
    """Approve, then dismiss, for one PR whose newest ruling at ``head`` is ACCEPT."""
    row = sup.ruled_approval(loop["repo"], number, head) or {}
    if row.get("dismiss", "pending") != "pending" or row.get("approve") in ("denied", "uncertain"):
        return False                                   # done, or left for a person
    reviews = gh.reviews(loop, number)
    if not isinstance(reviews, list):
        return False
    branch = str((pr.get("head") or {}).get("ref") or "")
    latest = gate.latest_effective_review_at_head(reviews, loop, head)
    if row.get("approve") == "posted" or gate.is_ruled_approval(latest, loop):
        return _dismiss(loop, sup, reviews, number, head, branch, log)
    if latest is None or gh.review_state(latest) != "CHANGES_REQUESTED":
        return False                                   # no change request to overrule
    outcome = _approve(loop, st, sup, number, head, branch, log)
    if outcome == "posted" and isinstance(reviews := gh.reviews(loop, number), list):
        _dismiss(loop, sup, reviews, number, head, branch, log)
    return bool(outcome)                               # else dismissed on the next sweep


def _wait_or_deny(loop: dict, st, number: int, head: str) -> tuple[str, str]:
    """``(wait, deny)``: why the approval waits for a later sweep, or why it is a person's."""
    gating = ci.gating(ci.read(loop, head), config.required_checks(loop))
    if gating is None or not gating.green:
        return "the required checks are not green yet", ""
    held = st.human_hold(number, head)
    if isinstance(held, dict):
        return "", f"waiting for a person: {held.get('why') or 'touches human_paths'}"
    refusal, reserved = broker.human_paths_check(loop, number)
    if refusal or reserved:
        return refusal, (f"its approval is a person's (human_paths): {reserved}" if reserved else "")
    hold, error = pr_hold.read(loop, number)
    if error:
        return f"the maintainer hold could not be read: {error[:120]}", ""
    if hold:
        return "a maintainer holds the PR", ""
    return "", ""


def _approve(loop: dict, st, sup, number: int, head: str, branch: str, log) -> str:
    """The approval step: ``posted``, ``stopped`` (denied or uncertain, for a person), or ``""``
    when it waits for a later sweep."""
    repo = loop["repo"]
    wait, deny = _wait_or_deny(loop, st, number, head)
    if wait:
        return ""
    try:
        if deny:
            raise broker.BrokerDenied(deny)
        login = broker.authorize_adjudicator(loop, repo=repo, number=number, head=head,
                                             branch=branch)
    except broker.BrokerDenied as exc:
        sup.ruled_approval_step(repo, number, head, "approve", "denied", error=str(exc)[:200])
        log(f"#{number} @ {head[:7]}: not approved on the ACCEPT: {exc}")
        return "stopped"
    if not sup.ruled_approval_step(repo, number, head, "approve", "posting"):
        return ""
    text = (f"Approved on the adjudicator's ACCEPT ruling at `{head[:7]}`: the ruling found the "
            "remaining change request does not block. The reviewer did not re-review this head; "
            "this is the adjudicator's decision.")
    try:
        review_id = broker.approve_on_ruling(loop, repo=repo, number=number, head=head,
                                             branch=branch, login=login, text=text)
    except Exception as exc:  # noqa: BLE001 - the POST may have landed: never replayed
        sup.ruled_approval_step(repo, number, head, "approve", "uncertain",
                                error=f"POST outcome unknown: {type(exc).__name__}: {exc}"[:200])
        log(f"#{number} @ {head[:7]}: the approval on the ACCEPT is uncertain: {exc}")
        return "stopped"
    sup.ruled_approval_step(repo, number, head, "approve", "posted", review_id=review_id)
    log(f"#{number} @ {head[:7]}: approved on the adjudicator's ACCEPT (review {review_id})")
    return "posted"


def _dismiss(loop: dict, sup, reviews: list, number: int, head: str, branch: str, log) -> bool:
    """The dismissal step: the reviewer seat's change request at the ruled head, never another
    (a person's, the adjudicator's own, or one at another head is left for a person)."""
    repo = loop["repo"]
    blocking = gate.blocking_reviews(reviews)
    targets = [r for r in blocking if gate.is_reviewer(r, loop) and r.get("commit_id") == head
               and type(r.get("id")) is int]
    others = [r for r in blocking if r not in targets]
    if not targets:
        state, error = ("denied", f"{_named(others[0])} still blocks, for a person") if others \
            else ("none", None)
        return sup.ruled_approval_step(repo, number, head, "dismiss", state, error=error)
    try:
        login = broker.authorize_adjudicator(loop, repo=repo, number=number, head=head,
                                             branch=branch)
    except broker.BrokerDenied as exc:
        log(f"#{number} @ {head[:7]}: dismissal waits: {exc}")
        return False
    if not sup.ruled_approval_step(repo, number, head, "dismiss", "posting"):
        return False
    message = (f"Dismissed on the adjudicator's ACCEPT ruling at {head[:7]}, which overruled "
               "this change request.")
    try:
        for review in targets:
            broker.dismiss_on_ruling(loop, repo=repo, number=number, head=head, branch=branch,
                                     login=login, review_id=review["id"], message=message)
    except Exception as exc:  # noqa: BLE001 - the PUT may have landed: never replayed
        sup.ruled_approval_step(repo, number, head, "dismiss", "uncertain",
                                error=f"dismissal outcome unknown: {type(exc).__name__}: {exc}"[:200])
        log(f"#{number} @ {head[:7]}: the dismissal on the ACCEPT is uncertain: {exc}")
        return True
    sup.ruled_approval_step(repo, number, head, "dismiss", "posted", review_id=targets[-1]["id"])
    log(f"#{number} @ {head[:7]}: dismissed review {targets[-1]['id']} on the ACCEPT; "
        + (f"{_named(others[0])} still blocks, for a person" if others else
           "nothing blocks the merge now"))
    return True


def _named(review: dict) -> str:
    """A blocking review as the log and the ledger name it."""
    return (f"{gate.reviewer_login(review) or '?'}'s change request {review.get('id')} at "
            f"{str(review.get('commit_id') or '?')[:7]}")
