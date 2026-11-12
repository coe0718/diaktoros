"""#905: the adjudicator acts on its ACCEPT ruling, so the PR can merge.

An ACCEPT says the reviewer's remaining change request does not block. Left alone it changes
nothing on GitHub: the change request still blocks a base that requires a review, and ``ready``
waits for a person. With ``adjudicator_approves`` on, the watchdog sweep acts on it, as the
adjudicator's own login (``seats.adjudicator.login``), once all of these hold, re-read live
before **each** write:

* the newest ruling on the PR at its current head is ACCEPT, and it overruled one review: the
  reviewer seat's latest verdict at that head, a change request submitted before the ruling (a
  shadow escalation's ACCEPT leaves an approval standing, so there is nothing to do);
* the required checks are green at that head;
* the diff touches no ``human_paths`` path (those approvals are a person's, #478);
* no maintainer hold (#477), and the PR is open, not a draft, by an authorized fixer.

Then it **approves** the head and **dismisses** that one overruled review, both citing the ruling
(a link to its PR comment, or where to read it when it has none). The loop's readers count the
approval only while an ACCEPT backs it and it is the head's newest verdict
(``gate.ruled_approvals``), so the dismissal never makes the head look unreviewed. A newer
verdict from the reviewer after the approval stops the dismissal for a person. Any other blocking
review (a maintainer's, one at another head) is never dismissed: the merge check names it.

Each write is recorded before it is sent (``ruled_approvals``, a record per ruling) and never
sent again; a later ACCEPT at the same head is acted on afresh, once every earlier ruling's write
there is settled. A lost
response is ``uncertain`` until a later sweep reads GitHub: the approval or dismissal is there
(``posted``) or it is not (``denied``). A refusal is ``denied`` with its reason. ``ready`` and the
merge check show an uncertain or denied step with its reason. A wait (red or running CI, a hold,
an unreadable read) writes nothing and is checked again on the next sweep.
"""
from __future__ import annotations

import time

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
        ruling = views.ruling_row(db, loop["repo"], number)
        if not ruling or (ruling["head"], ruling["verdict"]) != (head, "ACCEPT"):
            continue                           # the PR's newest ruling: an ACCEPT at this head
        try:
            if _act(loop, st, Supervisor(db, create=False), pr, ruling, log):
                moved.append(number)
        except Exception as exc:  # noqa: BLE001 - one PR's failure never stops the sweep
            log(f"#{number}: acting on the ACCEPT failed: {type(exc).__name__}: {exc}")
    if moved:
        from . import merge_check
        for number in moved:
            merge_check.refresh_quietly(loop, number)
    return moved


def overruled(reviews: list, loop: dict, head: str, ruled_at: float) -> dict | None:
    """The one review an ACCEPT at ``head`` overruled: the reviewer seat's latest effective
    verdict there, a change request submitted before the ruling. GitHub dates a review to the
    second, so one dated within the ruling's own second may have come after it. None when there
    is none, or when it can't be told (an unproved order): then nothing is dismissed."""
    mine = [r for r in reviews if isinstance(r, dict) and gate.is_reviewer(r, loop)]
    latest = gate.latest_effective_review_at_head(mine, loop, head)
    if latest is None or gh.review_state(latest) != "CHANGES_REQUESTED":
        return None
    when = gate._review_when(latest)
    return latest if when is not None and when[0].timestamp() + 1 <= float(ruled_at) else None


def _act(loop: dict, st, sup, pr: dict, ruling: dict, log) -> bool:
    """Approve, then dismiss, for one PR whose newest ruling at its head is ACCEPT, as this
    ruling's record says; an earlier ruling's record there is only settled (its write in flight
    stops this ruling's claim, ``ruled_approval_step(claim=True)``)."""
    repo, number, head, run = loop["repo"], int(pr["number"]), ruling["head"], ruling["run_id"]
    reviews = gh.reviews(loop, number)
    if not isinstance(reviews, list):
        return False
    rows = sup.ruled_approvals(repo, number, head)
    settled = [_reconcile(loop, sup, r, reviews, number, log) for r in rows]   # every one
    if any(settled):
        return True                                    # an uncertain step, settled from GitHub
    row = next((r for r in rows if r["ruling"] == run), {})
    if row.get("dismiss", "pending") != "pending" or row.get("approve") in (
            "denied", "uncertain", "posting"):
        return False                                   # done, left for a person, or being written
    wait, deny = _wait_or_deny(loop, st, number, head)
    if wait or not _still_ruled(loop, number, ruling):
        return False                                   # a newer ruling since: the next sweep sees it
    step = "approve" if row.get("approve", "pending") == "pending" else "dismiss"
    if deny:
        return sup.ruled_approval_step(repo, number, head, run, step, "denied", error=deny[:200])
    branch = str((pr.get("head") or {}).get("ref") or "")
    latest = gate.latest_effective_review_at_head(reviews, loop, head)
    if step == "approve":
        found = overruled(reviews, loop, head, ruling["created"])
        if found is None:
            return False                               # no change request it overruled
        if gate.is_ruled_approval(latest, loop):       # approved already, by hand
            return sup.ruled_approval_step(repo, number, head, run, "approve", "none",
                                           overruled=found["id"])
        return _approve(loop, sup, ruling, number, branch, found["id"], log)
    if not gate.is_ruled_approval(latest, loop):
        if latest is None:
            return False                               # the approval not readable yet: wait
        if gh.review_state(latest) == "APPROVED":      # the reviewer approved: its own block
            return sup.ruled_approval_step(repo, number, head, run, "dismiss", "none")   # is lifted
        return sup.ruled_approval_step(
            repo, number, head, run, "dismiss", "denied",
            error=f"a newer verdict at the head ({_named(latest)}) supersedes the approval")
    return _dismiss(loop, sup, ruling, reviews, number, branch, row.get("overruled"), log)


# The steps of acting on an ACCEPT that are finished, either way: nothing more is written for them.
SETTLED = ("posted", "none", "denied")


def unsettled(loop: dict, number: int, head: str, reviews: list) -> str:
    """Why acting on the ACCEPT at ``head`` is not finished, or '' (#2364). ``base_merge`` waits on
    it: a merge moves the head, and the sweep acts only on a ruling at the current head.

    The newest ruling's record decides, as ``_act`` does: no record yet is not settled while a
    change request is still there to dismiss (``overruled``: the sweep has not acted; an approval
    posted by hand still waits for its dismissal), and with none the sweep writes no record, so
    nothing is owed. A step not yet posted, none or denied is not settled. An older ruling's record holds only while one of its writes may
    be on the wire (``posting``, ``uncertain``): its pending steps are never acted on again, so
    they wait on nothing. A record or ruling that can't be read is not settled either."""
    db = production_ledger()
    rows, error = views.ruled_approvals(db, loop["repo"], number, head)
    if error:
        return f"the record of acting on the adjudicator's ACCEPT can't be read ({error[:120]})"
    ruling = views.ruling_row(db, loop["repo"], number) or {}
    newest = ruling.get("run_id")
    if not newest:
        return "the adjudicator's ruling can't be read"
    if not any(row.get("ruling") == newest for row in rows):
        if overruled(reviews, loop, head, ruling.get("created") or 0) is None:
            return ""                          # no change request to dismiss: nothing to act on
        return "the adjudicator's ACCEPT has not been acted on yet"
    for row in rows:
        for step in ("approve", "dismiss"):
            state = row.get(step) or "pending"
            if state in ("posting", "uncertain") or (row.get("ruling") == newest
                                                     and state not in SETTLED):
                return f"the adjudicator's ACCEPT is still being acted on ({step}: {state})"
    return ""


POSTING_STALE_S = 300   # a write is answered in well under this: a posting step older is cut off


def _reconcile(loop: dict, sup, row: dict, reviews: list, number: int, log) -> bool:
    """Settle an ``uncertain`` step of one ruling's record from what GitHub now shows, never by
    writing again: the adjudicator's approval at the head naming that ruling is there, or the
    overruled review is dismissed. A step
    left ``posting`` past ``POSTING_STALE_S`` (the process stopped after claiming the write) is
    uncertain too: its write may or may not have landed."""
    repo, head, run = loop["repo"], row["head"], row["ruling"]
    for step in ("approve", "dismiss"):
        if row.get(step) == "posting" and time.time() - float(row.get("updated") or 0) > \
                POSTING_STALE_S and sup.ruled_approval_step(
                    repo, number, head, run, step, "uncertain",
                    error="interrupted after the write was claimed; outcome unknown"):
            row = {**row, step: "uncertain"}
    if row.get("approve") == "uncertain":
        found = _landed(loop, reviews, head, run)
        state = "posted" if found else "denied"
        sup.ruled_approval_step(repo, number, head, run, "approve", state,
                                review_id=found[0]["id"] if found else None)
    elif row.get("dismiss") == "uncertain":
        target = next((r for r in reviews if isinstance(r, dict)
                       and r.get("id") == row.get("overruled")), None)
        state = "posted" if target and gh.review_state(target) == "DISMISSED" else "denied"
        sup.ruled_approval_step(repo, number, head, run, "dismiss", state)
    else:
        return False
    log(f"#{number} @ {head[:7]}: an uncertain write on the ACCEPT read back as {state}")
    return True


def _landed(loop: dict, reviews: list, head: str, run: str) -> list:
    """The adjudicator's approval at ``head`` that names ruling ``run`` in its text: only that
    write's own review, never one an earlier ruling at the head posted."""
    login = config.adjudicator_login(loop).lower()
    return [r for r in reviews if isinstance(r, dict) and gate.reviewer_login(r) == login
            and r.get("commit_id") == head and gh.review_state(r) == "APPROVED"
            and ruling_tag(run) in str(r.get("body") or "")]


def ruling_tag(run: str) -> str:
    """How an approval on a ruling names it, so its own write can be told from another's."""
    return f"ruling `{run}`"


def _wait_or_deny(loop: dict, st, number: int, head: str) -> tuple[str, str]:
    """``(wait, deny)``: why the next write waits for a later sweep, or why it is a person's."""
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


def _still_ruled(loop: dict, number: int, ruling: dict) -> bool:
    """Whether ``ruling`` is still the PR's newest, read again right before a write: an
    adjudication that lands while this sweep reads GitHub supersedes it, and nothing is written."""
    now = views.ruling_row(production_ledger(), loop["repo"], number) or {}
    return now.get("run_id") == ruling.get("run_id") and now.get("verdict") == "ACCEPT"


def _still_current(loop: dict, number: int, ruling: dict, step: str, target) -> bool:
    """Right before a write, read the reviews again: the write goes ahead only if they still call
    for this exact write. A verdict the reviewer posts while the sweep reads CI, paths or holds
    supersedes the snapshot it began with, and nothing is written. The ruling is checked after
    this, in the ledger transaction that claims the write (``ruled_approval_step(claim=True)``)."""
    reviews, head = gh.reviews(loop, number), ruling["head"]
    if not isinstance(reviews, list):
        return False
    if step == "approve":
        found = overruled(reviews, loop, head, ruling["created"])
        return found is not None and found.get("id") == target
    latest = gate.latest_effective_review_at_head(reviews, loop, head)
    review = next((r for r in reviews if isinstance(r, dict) and r.get("id") == target), None)
    return (gate.is_ruled_approval(latest, loop) and review is not None
            and gh.review_state(review) == "CHANGES_REQUESTED")


def cite(loop: dict, number: int, ruling: dict) -> str:
    """Where the ruling can be read: its PR comment's link, or, with none, the host command that
    prints its full text from the run ledger."""
    if type(ruling.get("comment_id")) is int and ruling.get("comment") == "posted":
        return (f"https://github.com/{loop['repo']}/pull/{number}"
                f"#issuecomment-{ruling['comment_id']}")
    return (f"not posted as a comment (comment {ruling.get('comment') or 'unknown'}); its full text: "
            f"`python -m diaktoros.run_supervisor rulings {production_ledger()} "
            f"{ruling.get('run_id') or '?'}` on the host")


def _approve(loop: dict, sup, ruling: dict, number: int, branch: str, target: int, log) -> bool:
    """The approval step, with the review it overrules recorded first."""
    repo, head, run = loop["repo"], ruling["head"], ruling["run_id"]
    try:
        login = broker.authorize_adjudicator(loop, repo=repo, number=number, head=head,
                                             branch=branch)
    except broker.BrokerDenied as exc:
        sup.ruled_approval_step(repo, number, head, run, "approve", "denied", error=str(exc)[:200])
        log(f"#{number} @ {head[:7]}: not approved on the ACCEPT: {exc}")
        return True
    if not _still_current(loop, number, ruling, "approve", target) or not sup.ruled_approval_step(
            repo, number, head, run, "approve", "posting", overruled=target, claim=True):
        return False
    text = (f"Approved on the adjudicator's ACCEPT ruling at `{head[:7]}` ({ruling_tag(run)}, "
            f"{cite(loop, number, ruling)}): the ruling found the remaining change request "
            "does not block. The reviewer did not re-review this head; this is the "
            "adjudicator's decision.")
    try:
        review_id = broker.approve_on_ruling(loop, repo=repo, number=number, head=head,
                                             branch=branch, login=login, text=text)
    except Exception as exc:  # noqa: BLE001 - the POST may have landed: never replayed
        sup.ruled_approval_step(repo, number, head, run, "approve", "uncertain",
                                error=f"POST outcome unknown: {type(exc).__name__}: {exc}"[:200])
        log(f"#{number} @ {head[:7]}: the approval on the ACCEPT is uncertain: {exc}")
        return True
    sup.ruled_approval_step(repo, number, head, run, "approve", "posted", review_id=review_id)
    log(f"#{number} @ {head[:7]}: approved on the adjudicator's ACCEPT (review {review_id})")
    return True


def _dismiss(loop: dict, sup, ruling: dict, reviews: list, number: int, branch: str,
             target, log) -> bool:
    """The dismissal step: the one review the ruling overruled, never another."""
    repo, head, run = loop["repo"], ruling["head"], ruling["run_id"]
    review = next((r for r in reviews if isinstance(r, dict) and r.get("id") == target), None)
    if review is None or gh.review_state(review) != "CHANGES_REQUESTED":
        return sup.ruled_approval_step(repo, number, head, run, "dismiss", "none")
    try:
        login = broker.authorize_adjudicator(loop, repo=repo, number=number, head=head,
                                             branch=branch)
    except broker.BrokerDenied as exc:
        return sup.ruled_approval_step(repo, number, head, run, "dismiss", "denied",
                                       error=str(exc)[:200])
    if not _still_current(loop, number, ruling, "dismiss", target) or not sup.ruled_approval_step(
            repo, number, head, run, "dismiss", "posting", claim=True):
        return False
    message = (f"Dismissed on the adjudicator's ACCEPT ruling at {head[:7]}, which overruled "
               f"this change request: {cite(loop, number, ruling)}")
    try:
        broker.dismiss_on_ruling(loop, repo=repo, number=number, head=head, branch=branch,
                                 login=login, review_id=target, message=message)
    except Exception as exc:  # noqa: BLE001 - the PUT may have landed: never replayed
        sup.ruled_approval_step(repo, number, head, run, "dismiss", "uncertain",
                                error=f"dismissal outcome unknown: {type(exc).__name__}: {exc}"[:200])
        log(f"#{number} @ {head[:7]}: the dismissal on the ACCEPT is uncertain: {exc}")
        return True
    sup.ruled_approval_step(repo, number, head, run, "dismiss", "posted")
    others = [r for r in gate.blocking_reviews(reviews) if r.get("id") != target]
    log(f"#{number} @ {head[:7]}: dismissed review {target} on the ACCEPT; "
        + (f"{_named(others[0])} still blocks, for a person" if others else
           "nothing else blocks the merge"))
    return True


def _named(review: dict) -> str:
    """A review as the log and the ledger name it."""
    return (f"{gate.reviewer_login(review) or '?'}'s {gh.review_state(review).lower()} "
            f"{review.get('id')} at {str(review.get('commit_id') or '?')[:7]}")
