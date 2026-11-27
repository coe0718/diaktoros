"""``hermes dk unstick``: do the one next step a stuck PR is owed (#2759).

``explain`` says why a PR is not moving; before this, ending it meant picking among ``retry``
(a failed turn only), ``drain`` (a queued one only), ``review --reconsider`` (which spends a
verdict) and re-delivering a webhook by hand. ``unstick`` reads the PR live and takes the step its
state owes, or says whose move it is:

- a change request at the head: a fix turn at the head that ended is re-armed when ``retry``
  would re-arm it (else the line says why not); with none there, one is queued (``fix_rescue``,
  without the sweep's wait);
- no verdict at the head and no review turn of any key queued or running there: the review turn
  the reviewer gate would queue (its key: a dismissal's, a retarget's, or the plain one, #2760
  F4) is re-armed when it ended and ``retry`` would re-arm it, as the gate's redelivery does not
  past ``MAX_REARMS`` (F6); with none at that key, the review request is delivered to the gate
  (the sweep's own delivery, #532 / #2468), which queues it;
- a turn already queued or running, an approval, a maintainer's hold, a ruling pending, a spent
  cap, a review-only author's move, or a loop without fixer pushes: nothing is done, and the
  line names the command that is the operator's.

Nothing here posts on GitHub or spends a verdict: each step only queues or re-arms a turn, and the
loop's own gates and claims still decide it. ``dry_run`` says the step without taking it.
"""
from __future__ import annotations

import sqlite3

from . import config, fix_rescue, gate, gh, merge_only, pr_hold, review_kick, run_supervisor
from . import state as state_mod
from . import transition
from .run_supervisor import views


def perform(loop: dict, number: int, *, dry_run: bool = False) -> dict:
    """``{"lines", "code"}``: 0 when a step was taken (or, dry, would be), 1 when nothing is the
    loop's to do (the line says whose move it is), 2 when the PR could not be read."""
    tag = f"[{loop['id']}] #{number}"
    pr = gh.pr(loop, number)
    if not isinstance(pr, dict) or pr.get("number") != number:
        return _out(2, f"{tag}: the PR could not be read — nothing done")
    head = gate.head_of(pr)
    at = f"{tag} @ {head[:7]}"
    if pr.get("state") != "open" or (pr.get("base") or {}).get("ref") != loop["base"]:
        return _out(1, f"{tag}: not an open PR on {loop['base']} — nothing to unstick")
    hold, hold_error = pr_hold.read(loop, number)
    if hold_error:
        return _out(2, f"{tag}: whether a maintainer holds it could not be read — nothing done")
    if hold:
        return _out(1, f"{at}: a maintainer holds it — release the hold first")
    st = state_mod.state_for(loop)
    reviews = transition.effective_reviews(loop, st, number, head, gh.reviews(loop, number))
    if not isinstance(reviews, list):
        return _out(2, f"{tag}: its reviews could not be read — nothing done")
    with merge_only.decision():
        latest = gate.latest_effective_review_at_head(reviews, loop, head)
        if latest is None and merge_only.undecided(loop, reviews, head):
            return _out(2, f"{at}: {merge_only.UNDECIDED_WHY} — nothing done; try again")
        step = _owed(loop, st, number, head, pr, reviews, latest)
    if isinstance(step, tuple) and isinstance(step[0], int):
        return _out(*step)
    step, run_id = step if isinstance(step, tuple) else (step, None)
    if dry_run:
        return _out(0, f"{at}: would {step}")
    return _take(loop, number, head, pr, step, run_id)


def _owed(loop: dict, st, number: int, head: str, pr: dict, reviews: list, latest):
    """The step owed at ``head``: ``FIX`` or ``REVIEW``, ``(REFIX | REREVIEW, run id)``, or
    ``(code, line)`` when nothing is the loop's to do."""
    at = f"[{loop['id']}] #{number} @ {head[:7]}"
    state = gh.review_state(latest) if latest is not None else ""
    author = ((pr.get("user") or {}).get("login") or "").lower()
    review_only = author in config.review_only(loop)
    spent = len(gate.verdicts(reviews, loop))
    if state == "APPROVED":
        return 1, f"{at}: approved — nothing is owed; the merge is yours"
    marker = st.breach_get(number)
    if gate.escalated(marker, head) or (not review_only and spent >= loop["cap"]
                                        and gate.breach_delivery_status(marker, head)):
        return 1, (f"{at}: parked for a ruling — the adjudicator's; `hermes dk explain --loop "
                   f"{loop['id']} --pr {number}` follows it")
    if not review_only and spent >= loop["cap"]:
        return 1, (f"{at}: {spent}/{loop['cap']} verdicts spent — `hermes dk escalate --loop "
                   f"{loop['id']} --pr {number}` sends it for a ruling")
    if state == "CHANGES_REQUESTED":
        if review_only:
            return 1, f"{at}: changes requested on a review-only PR — the author's move"
        if not config.unattended_fixer_push_enabled(loop):
            return 1, (f"{at}: changes requested, and fixer pushes are off — run "
                       f"`{config.fixer_push_enable_command(loop)}` (or fix it by hand)")
        return _fix_step(loop, number, head)
    held = st.human_hold(number, head)
    if isinstance(held, dict):
        # #478, as the sweep and the reviewer gate read it: no review is owed here (#2760 F3).
        return 1, (f"{at}: waits for a person's approval (human_paths: "
                   f"{held.get('why') or 'reserved paths'}) — no review is owed")
    if review_only and spent >= config.review_only_cap(loop) and not st.review_cap_granted(
            number, head, spent):
        return 1, (f"{at}: review cap reached — `hermes dk review --loop {loop['id']} --pr "
                   f"{number} --another-round` grants one more")
    return _review_step(loop, st, number, head, reviews)


FIX = "queue the fix turn owed at this head"
REFIX = "re-arm the fix turn at this head"
REVIEW = "deliver the review request to the reviewer gate"
REREVIEW = "re-arm the review turn owed at this head"


def _fix_step(loop: dict, number: int, head: str):
    """``FIX`` with no fix turn at ``head`` under the gate's key, else ``_rearm_step``'s answer
    for it. An unreadable ledger reads as none: the enqueue dedups against any turn there."""
    turn = _turn_at(loop, number, head, "fixer", "")
    return FIX if turn is None else _rearm_step(loop, number, head, "fixer", turn)


def _review_step(loop: dict, st, number: int, head: str, reviews: list):
    """``REVIEW`` when the review turn the reviewer gate would queue here has no row yet, else
    ``_rearm_step``'s answer for it; nothing (said) while a reviewer turn of any key is queued or
    running here (#2760 F7). The key is the gate's own (``gate_reviewer.decide``): a dismissal's,
    else a recorded retarget's, else the plain one (F4). A retarget the gate has still to record
    names a turn not yet written, so the request is delivered for the gate to record it."""
    at = f"[{loop['id']}] #{number} @ {head[:7]}"
    live = _live_at(loop, number, head, "reviewer")
    if live:
        return 1, f"{at}: a reviewer turn is already {live} here — nothing to unstick"
    if transition.unrecorded(loop, st, number, head):
        return REVIEW
    entry = transition.hold(st, number, head)
    fresh = transition.turn_key(entry) if entry is not None else ""
    if entry is not None and (transition.baseline_missing(entry) or not fresh):
        why = (transition.MISSING_BASELINE if transition.baseline_missing(entry)
               else "its transition time is unreadable, so its fresh turn cannot be named")
        return 1, f"{at}: base retarget hold — {why}"
    turn = _turn_at(loop, number, head, "reviewer", gate.dismissed_key(reviews, loop, head) or fresh)
    return REVIEW if turn is None else _rearm_step(loop, number, head, "reviewer", turn)


def _rearm_step(loop: dict, number: int, head: str, seat: str, turn: dict):
    """For ``seat``'s owed turn at ``head``: ``(REFIX | REREVIEW, run id)`` when it ended and
    ``retry`` would re-arm it, and nothing (said) when it is queued or running or ``retry`` refuses
    it: the dry run says the real step (#2760 F5)."""
    at = f"[{loop['id']}] #{number} @ {head[:7]}"
    if turn["state"] in run_supervisor.NOT_ENDED:
        return 1, f"{at}: a {seat} turn is already {turn['state']} here — nothing to unstick"
    why = _not_rearmable(loop, number, seat, turn["id"])
    name = "fix" if seat == "fixer" else "review"
    if why:
        return 1, (f"{at}: the {name} turn here ended {turn['state']}, and retry would not "
                   f"re-arm it ({why}) — `hermes dk explain --loop {loop['id']} --pr {number}` "
                   "says what it answered")
    return (REFIX if seat == "fixer" else REREVIEW), turn["id"]


def _ledger_rows(sql: str, args: tuple) -> list:
    """Rows of a read of the host run ledger; none when it is absent or unreadable."""
    try:
        con = views.read_only_connect(config.host_path("ledger"))
        if con is None:
            return []
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def _turn_at(loop: dict, number: int, head: str, seat: str, turn_key: str) -> dict | None:
    """``seat``'s turn at this PR head under ``turn_key`` as ``{"id", "state"}``, or None."""
    rows = _ledger_rows("SELECT id, state FROM runs WHERE repo=? AND pr=? AND head=? AND seat=? "
                        "AND COALESCE(turn_key, '')=? ORDER BY updated DESC LIMIT 1",
                        (loop["repo"], number, head, seat, turn_key))
    return {"id": rows[0][0], "state": rows[0][1]} if rows else None


def _live_at(loop: dict, number: int, head: str, seat: str) -> str:
    """The state of a ``seat`` turn of any key queued or running at this PR head, or ``""``."""
    marks = ", ".join("?" * len(run_supervisor.NOT_ENDED))
    rows = _ledger_rows(f"SELECT state FROM runs WHERE repo=? AND pr=? AND head=? AND seat=? "
                        f"AND state IN ({marks}) LIMIT 1",
                        (loop["repo"], number, head, seat, *run_supervisor.NOT_ENDED))
    return rows[0][0] if rows else ""


def _not_rearmable(loop: dict, number: int, seat: str, run_id: str) -> str:
    """Why ``retry`` would not re-arm ``run_id`` (``retry_action.plan``, pinned to it), or ``""``
    when it would."""
    from . import retry_action
    try:
        con = views.read_only_connect(config.host_path("ledger"))
        if con is None:
            return "no run ledger"
        con.row_factory = sqlite3.Row                      # plan reads rows by name
        try:
            found = retry_action.plan(con, loop["repo"], number, seat, run_id=run_id)
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        return f"the run ledger cannot be read: {exc}"
    if found["rearmable"]:
        return ""
    return (next(iter(found["blocked"].values()), "") or found["message"]
            or "nothing at this head")


def _take(loop: dict, number: int, head: str, pr: dict, step: str, run_id) -> dict:
    """Take ``step`` and say what came of it. A re-arm is of ``run_id`` alone, the turn
    ``_rearm_step`` found at this head (#2760 F1, F2, F6)."""
    at = f"[{loop['id']}] #{number} @ {head[:7]}"
    if step in (REFIX, REREVIEW):
        from . import retry_action
        result = retry_action.perform(loop, number, "fixer" if step == REFIX else "reviewer",
                                      read_pr=lambda: pr, reveal_errors=True, run_id=run_id)
        lines = [f"{at}: {result['message']}"] if result["message"] else []
        lines += [f"{at}: {e['seat']} re-armed (was {e['state']}: {e.get('error') or 'no reason'})"
                  for e in result["rearmed"]]
        lines += [f"{at}: {e['seat']} {e['state']}: {e['reason']}" for e in result["refused"]]
        return {"lines": lines or [f"{at}: nothing re-armed"], "code": 0 if result["ok"] else 2}
    try:
        if step == FIX:
            outcome = fix_rescue.queue(loop, number, head)
            done = outcome in gate.SCHEDULED_OUTCOMES
            return _out(0 if done else 1, f"{at}: fixer {outcome}")
        author = ((pr.get("user") or {}).get("login") or "").lower()
        sent = review_kick.kick(loop, pr, head, author, f"unstick-review-{number}")
    except Exception as exc:
        return _out(2, f"{at}: {step} failed: {type(exc).__name__}: {exc}")
    return (_out(0, f"{at}: review request delivered — the reviewer gate decides") if sent
            else _out(2, f"{at}: the review request was not delivered (no reviewer route, or "
                         "the gateway did not answer)"))


def _out(code: int, line: str) -> dict:
    return {"lines": [line], "code": code}
