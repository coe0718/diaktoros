"""The shadow review's rules in the gate (#218, #1004): which reviewer turns get a shadow,
its findings snapshot and queueing, the hold a shadow review puts on a head, and the
escalation and upheld fix that follow a shadow's dissent.

Moved here from ``gate/__init__.py`` (#1144: the gate was at its size ceiling). The gate's own
rules are read through ``gate``, so a test that patches one there patches it here too.
"""
from __future__ import annotations

import pathlib

from .. import config, gate, gh, stale_approval, state as state_mod
from ..util import log, now_iso


def _shadow_snapshot_lock(loop: dict, seat: str, number: int, head: str, turn_key: str = ""):
    """The loop's state lock for a reviewer turn that gets a shadow, held from its snapshot to
    the shadow's submit: every eviction runs under it and reads the open shadow runs first, so
    none can drop a snapshot whose row is not in the ledger yet. Otherwise no lock."""
    from contextlib import nullcontext
    if seat != "reviewer" or not config.shadow_profile(loop) or not shadow_sampled(
            loop, number, head, turn_key):
        return nullcontext()
    try:
        return state_mod.state_for(loop).locked()
    except Exception:  # noqa: BLE001 - then the snapshot cannot be kept either: no shadow
        return nullcontext()


def shadow_sampled(loop: dict, number: int, head: str, turn_key: str = "") -> bool:
    """Whether this reviewer turn also gets a shadow review (#218): by what the PR changes when
    the loop names paths (#1788), else one in ``shadow.every``; chosen once per PR, head and
    reviewer turn, so a redelivery or a re-queued turn chooses the same, and a new turn at the
    same head (a retarget's fresh review) chooses again from its own diff (``shadow_choice``)."""
    from .. import shadow_choice
    return shadow_choice.choose(loop, number, head, turn_key)["shadow"]


# A shadow run in one of these states may still record its review at the head (#1004):
# ``record_shadow`` writes only for a live (launching/running) run. ``uncertain`` is terminal
# (an expired lease, a post-write quarantine) and never records one, so it is not open: holding
# on it would park the PR for good (shadow review of #1097, F1).
SHADOW_OPEN = ("pending", "waiting", "claimed", "launching", "running")


def _rulings_at(con, repo: str, number: int, head: str, turn_key: str | None = None) -> list:
    """The rulings at a head, newest first, as ``(verdict, created, shadow_run)``; ``turn_key``
    narrows to one round. A ledger from before ``rulings.shadow_run`` (#1112) reads NULL for it."""
    import sqlite3
    where = "repo=? AND pr=? AND head=?" + (" AND turn_key=?" if turn_key is not None else "")
    args = (repo, number, head, *(() if turn_key is None else (turn_key,)))
    try:
        return con.execute(f"SELECT verdict, created, shadow_run FROM rulings WHERE {where} "
                           "ORDER BY created DESC", args).fetchall()
    except sqlite3.OperationalError:
        return con.execute(f"SELECT verdict, created, NULL FROM rulings WHERE {where} "
                           "ORDER BY created DESC", args).fetchall()


_ACTIVE = object()      # "the active reviewer turn", read from the ledger


def _active_turn(con, repo: str, number: int, head: str) -> str | None:
    """The turn key of the active reviewer turn at a head: the newest reviewer run there (#1683).
    None when there is no reviewer run (or the ledger predates ``runs.turn_key``): the head's
    shadows then count as before, whichever turn queued them."""
    import sqlite3
    try:
        row = con.execute("SELECT turn_key FROM runs WHERE repo=? AND pr=? AND head=? AND "
                          "seat='reviewer' ORDER BY created DESC, updated DESC LIMIT 1",
                          (repo, number, head)).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None else str(row[0] or "")


def _latest_shadow_review(con, repo: str, number: int, head: str, turn=_ACTIVE):
    """The newest shadow review of a head's active reviewer turn (#1683), as ``(verdict, created,
    run_id)``, or None. A shadow counts for the turn it was queued with (its run carries the
    reviewer's turn key): an older turn's review at the same head never answers a newer turn.
    ``turn`` is that key, read from the ledger when left out; None counts every shadow there."""
    if turn is _ACTIVE:
        turn = _active_turn(con, repo, number, head)
    if turn is None:
        return con.execute("SELECT verdict, created, run_id FROM shadow_reviews WHERE repo=? AND "
                           "pr=? AND head=? ORDER BY created DESC LIMIT 1",
                           (repo, number, head)).fetchone()
    return con.execute("SELECT s.verdict, s.created, s.run_id FROM shadow_reviews s JOIN runs r "
                       "ON r.id=s.run_id WHERE s.repo=? AND s.pr=? AND s.head=? AND r.turn_key=? "
                       "ORDER BY s.created DESC LIMIT 1", (repo, number, head, turn)).fetchone()


def _shadow_view(con, repo: str, number: int, head: str) -> tuple:
    """What the ledger holds for a head's shadow, for ``shadow_state``: ``(review, run, ruling,
    pending, lost)``. The newest review, the newest shadow run, the ruling that answers the review
    and the open run newer than it, all of the active reviewer turn (#1683). ``lost`` is true when
    that turn has no review while an earlier turn's shadow run exists at the head: the head is
    sampled, so this turn's shadow is owed, and its enqueue was delayed or lost or its run ended
    (failed, cancelled, skipped) without one. With no earlier turn's shadow, a run that ended
    without a review is nothing to wait for, as before."""
    import sqlite3
    turn = _active_turn(con, repo, number, head)
    scope, targs = ("", ()) if turn is None else (" AND turn_key=?", (turn,))
    try:
        review = _latest_shadow_review(con, repo, number, head, turn)
    except sqlite3.OperationalError:
        review = None              # a ledger from before shadow reviews: none recorded
    base = "FROM runs WHERE repo=? AND pr=? AND head=? AND seat='shadow'"
    run = con.execute(f"SELECT state, created {base}{scope} ORDER BY created DESC, updated DESC "
                      "LIMIT 1", (repo, number, head, *targs)).fetchone()
    try:
        # A ruling answers the shadow review it was shown (#1112: its run id, not the time it
        # landed): a later dissent at the same head, even one that landed during the
        # adjudicator's turn, is not overruled by it (reviews on #1103).
        ruling = None
        if review is not None:
            ruling = next((r for r in _rulings_at(con, repo, number, head)
                           if ruling_answers(r, review[2], review[1])), None)
    except sqlite3.OperationalError:
        ruling = None
    pending = con.execute(
        f"SELECT state {base}{scope} AND state IN ({','.join('?' * len(SHADOW_OPEN))}) AND "
        "created>? ORDER BY created DESC LIMIT 1",
        (repo, number, head, *targs, *SHADOW_OPEN,
         float(review[1]) if review is not None else -1.0)).fetchone()
    lost = bool(turn is not None and review is None and con.execute(
        f"SELECT 1 {base} AND COALESCE(turn_key,'')<>? LIMIT 1", (repo, number, head, turn)).fetchone())
    return review, run, ruling, pending, lost


def _lost_hold(short: str, run) -> str:
    """The hold text when the active reviewer turn has no shadow review of its own (#1683):
    ``run`` is that turn's newest shadow run row, or None when none was queued."""
    ended = f"its shadow run ended {run[0]} without a review" if run else "it has none queued yet"
    return (f"waiting on the shadow review at {short}: the newest reviewer turn has no review "
            f"({ended}), and an earlier turn's does not count for it")


def ruling_answers(ruling, review_run: str, review_created: float) -> bool:
    """Whether a ruling (a ``_rulings_at`` row) answers this shadow review (#1112): the one its
    prompt showed, by run id. A ruling from before that was recorded (NULL) falls back to the
    time rule: made after the review. One rule for the hold and for the escalation check."""
    shown = ruling[2]
    if shown is None:
        return float(ruling[1]) > float(review_created)
    return str(shown) == str(review_run)


def shadow_open_heads(repo: str, db=None) -> set[tuple[int, str]]:
    """``(pr, head)`` of every shadow run of ``repo`` still open (``SHADOW_OPEN``), whose
    findings snapshot must outlive it (#1013). Scoped to the loop's repo (#1133): the snapshot
    file is per loop and keyed by ``pr:head`` alone, so another repo's open run at the same PR
    number and head would otherwise keep this loop's snapshot past the cap. Empty when the
    ledger does not exist yet; raises when it exists but cannot be read — a corrupt file, or a
    path that is not a regular file (a directory) — so the caller never evicts on a guess, and
    ``shadow_state`` holds on the same ledger."""
    from .. import run_supervisor
    path = pathlib.Path(db or run_supervisor.production_ledger())
    if not path.exists():
        return set()
    con = run_supervisor.views.read_only_connect(path)
    if con is None:
        # ``read_only_connect`` answers None for anything that is not a file, and this path
        # exists: that is not an absent ledger, so not "no open runs" (an empty set would let
        # the eviction run).
        raise OSError(f"the run ledger {path} exists but is not a readable file")
    try:
        rows = con.execute(f"SELECT pr, head FROM runs WHERE repo=? AND seat='shadow' AND "
                           f"state IN ({','.join('?' * len(SHADOW_OPEN))})",
                           (repo, *SHADOW_OPEN)).fetchall()
    finally:
        con.close()
    return {(int(r[0]), str(r[1])) for r in rows}


def shadow_row_exists(repo: str, number: int, head: str, turn_key: str = "", db=None) -> bool:
    """Whether the run ledger already holds the shadow row of this PR, head and reviewer turn
    key (the identity ``Supervisor.submit`` deduplicates on). False when the ledger does not
    exist yet; raises when it exists but cannot be read, so the caller never decides on a
    guess."""
    from .. import run_supervisor
    path = pathlib.Path(db or run_supervisor.production_ledger())
    if not path.exists():
        return False
    con = run_supervisor.views.read_only_connect(path)
    if con is None:
        return False
    try:
        return con.execute("SELECT 1 FROM runs WHERE repo=? AND pr=? AND head=? AND "
                           "seat='shadow' AND turn_key=? LIMIT 1",
                           (repo, number, head, turn_key)).fetchone() is not None
    finally:
        con.close()


def shadow_hold(loop: dict, number: int, head: str, db=None,
                marker: dict | None = None) -> tuple[str, str]:
    """``(hold, note)``: why the shadow review holds this head (``""`` when it does not), and a
    note. ``shadow_state`` adds the kind of hold for the merge check (#1163 M2b)."""
    return shadow_state(loop, number, head, db=db, marker=marker)[1:]


def shadow_state(loop: dict, number: int, head: str, db=None,
                marker: dict | None = None) -> tuple[str, str, str]:
    """``(kind, hold, note)`` for a PR head — kind ``pending`` or ``failure`` when it holds under ``shadow.escalate`` (#1004), read-only.

    ``hold`` (it is not ready to merge) once the newest shadow review of this head requested
    changes, or while a shadow run started after the newest review (or with none yet) is still
    open; a ledger that exists but cannot be read holds too (unknown is not ready). ``note``
    only, never a hold, when there is nothing to wait for: no ledger yet, or a head with no
    earlier reviewer turn's shadow and no shadow run queued (sampled out, or queued before
    escalate was on) or only runs that ended without a review. Once an earlier turn's shadow
    exists at the head (after ``review --reconsider``), a newer turn whose shadow was never
    queued, or ended failed, cancelled or skipped without a review, does hold (``_lost_hold``,
    #1683) until the active reviewer turn has a shadow review: its own, landing late, or a newer
    turn's at the head (a dispute re-review, a dismissed verdict, a retarget) when that turn gets
    a shadow. Failing that, ``shadow.escalate`` off or a new head clears it.
    ``("", "", "")`` with escalate off; ``shadow_hold`` is the ``(hold, note)`` view of it."""
    if not config.shadow_escalate(loop):
        return "", "", ""
    from pathlib import Path
    from .. import run_supervisor
    short = str(head)[:7]
    path = Path(db or run_supervisor.production_ledger())
    if not path.exists():
        # A loop whose ledger was never created holds no shadow review (shadow review of #1097).
        return "", "", f"no run ledger yet, so no shadow review of {short} exists: nothing to wait for"
    try:
        con = run_supervisor.views.read_only_connect(path)
        if con is None:
            return "pending", f"the run ledger is unreadable, so the shadow review at {short} is unknown", ""
        try:
            review, run, ruling, pending, lost = _shadow_view(con, loop["repo"], number, head)
        finally:
            con.close()
    except Exception as exc:
        return "pending", (f"the run ledger is unreadable ({type(exc).__name__}), so the shadow review at "
                f"{short} is unknown"), ""
    if review is not None and str(review[0]) == "REQUEST_CHANGES":
        return _dissent_state(loop, number, head, short, ruling, marker, db)
    if pending is not None:
        # A shadow run queued after the newest review (a re-review at the same head, #1097's
        # shadow review) can still change it: wait for it, even past an earlier approval.
        return "pending", f"waiting on the shadow review at {short} ({pending[0]})", ""
    if lost:
        # #1683: an earlier turn's shadow at this head never answers the newer reviewer turn; the
        # head holds until that turn's own shadow has a review.
        return "pending", _lost_hold(short, run), ""
    if review is not None:
        return "", "", ""
    if run is None:
        return "", "", f"no shadow review was queued for {short}: nothing to wait for"
    return "", "", f"the shadow run at {short} ended {run[0]} without a review: nothing to wait for"


def _dissent_state(loop: dict, number: int, head: str, short: str, ruling, marker,
                   db=None) -> tuple[str, str, str]:
    """``(kind, hold, note)`` once the newest shadow review of the head requested changes: the
    adjudicator's ruling on it (#1004 parts 2 and 3), or the operator's decision."""
    mine = isinstance(marker, dict) and str(marker.get("head") or "") == head and (
        (marker.get("escalated") or {}).get("by") == gate.SHADOW_ESCALATION)
    verdict = str(ruling[0]) if ruling else ""
    if mine and verdict == "ACCEPT":
        # Part 2 (#1004): the adjudicator overruled it; the reviewer's approval stands.
        return "", "", f"the adjudicator overruled the shadow review at {short} (ACCEPT)"
    if mine and verdict == "REJECT":
        # Part 3 (#1004): upheld. The fixer answers it where queue_shadow_fix can hand it on;
        # a loop without unattended pushes, or a review-only author's PR, is the operator's.
        if fixer_answers_upheld(loop, number):
            # #2263 review F1: not once its fixer turn ended unanswered and no sweep re-arms it.
            state, how, _key = shadow_fix_turn(loop, number, head, db=db)
            return "failure", upheld_fix_line(short, state, how), ""
        return "failure", (f"the adjudicator upheld the shadow review at {short} (REJECT) — no "
                "fixer takes it here, so the decision is yours (`hermes dk explain --pr N` "
                "shows the findings)"), ""
    if mine and verdict:
        return "failure", (f"the adjudicator ruled {verdict} on the shadow review at {short} — the "
                "decision is yours (`hermes dk explain --pr N` shows the findings)"), ""
    if mine:
        return "pending", (f"the shadow review requested changes at {short}; escalated to the "
                "adjudicator, waiting for its ruling"), ""
    return "failure", (f"the shadow review requested changes at {short} — it holds the PR until "
            "you decide (`hermes dk explain --pr N` shows its findings)"), ""


def _review_only_pr(loop: dict, number: int) -> bool:
    """Whether a PR's author is not one of the loop's fixers (its author answers it), read live;
    an unreadable PR counts as not the fixer's, so no fix is promised on a guess."""
    pr = gh.pr(loop, number)
    author = str(((pr or {}).get("user") or {}).get("login") or "").casefold()
    return not author or author not in {str(x).casefold() for x in loop.get("fixers") or []}


def fixer_answers_upheld(loop: dict, number: int) -> bool:
    """Whether the fixer answers a shadow review the adjudicator upheld on this PR (#1004 part 3):
    the loop pushes unattended and the PR is a fixer's; otherwise the decision is a person's. One
    rule for the hold's text, ``ready`` and ``explain`` (#1353), and the two conditions
    ``queue_shadow_fix`` checks, each with its own reason."""
    return config.unattended_fixer_push_enabled(loop) and not _review_only_pr(loop, number)


def upheld_fix_line(short: str, state: str, how: str) -> str:
    """Who answers an upheld shadow review the fixer takes: the fixer, until its shadow-fix turn
    (``shadow_fix_turn``'s ``state`` and ``how``) ended with no push and no sweep re-arms it
    (#2263 review F1). One text for the shadow hold, and for ``explain`` with the hold off."""
    if state and not how:
        return (f"the adjudicator upheld the shadow review at {short} (REJECT), but its shadow-fix "
                f"fixer turn is {state} with no fix pushed and no watchdog sweep re-arms it: the "
                "decision is yours, not the fixer's")
    return (f"the adjudicator upheld the shadow review at {short} (REJECT): the fixer answers its "
            "findings, and the fixed head is reviewed fresh")


def shadow_fix_owed(loop: dict, marker, head: str, db=None) -> bool:
    """Whether this head's breach marker is a shadow escalation (#1004) the adjudicator upheld
    (REJECT, ``upheld_shadow_ruling``) and the fixer answers it (``fixer_answers_upheld``): its
    findings go to the fixer, so no person is waited for (#1353). The shadow hold still stands,
    so this is False with ``shadow.escalate`` off: ``shadow_state`` gives no hold then, and the
    escalation's own line keeps the ruled head from reading ready (``fixer_takes_upheld``)."""
    return config.shadow_escalate(loop) and fixer_takes_upheld(loop, marker, head, db=db)


def fixer_takes_upheld(loop: dict, marker, head: str, db=None) -> bool:
    """``shadow_fix_owed`` whatever ``shadow.escalate`` says: ``queue_shadow_fix`` and the push
    never read it, so with it off the fixer still answers the upheld findings (#2269)."""
    m = marker if isinstance(marker, dict) else {}
    if str(m.get("head") or "") != head or (m.get("escalated") or {}).get("by") != gate.SHADOW_ESCALATION:
        return False
    number = int(m.get("pr") or 0)
    return (upheld_shadow_ruling(loop, number, head, db=db) is not None
            and fixer_answers_upheld(loop, number))


def shadow_fix_turn(loop: dict, number: int, head: str, db=None,
                    strict: bool = False) -> tuple[str, str, str]:
    """The fixer turn answering this head's upheld shadow review (#2262): ``(state, how, key)``,
    its ledger state ("" with none, or an unreadable ledger, which ``strict`` raises instead:
    #2599), ``how`` it still answers the findings: ``live`` (queued or running), ``rearmed``
    (the sweep's ``queue_shadow_fix`` resubmits its turn key and ``submit`` re-arms it, by
    ``intake.redelivery_refusal``), or "" when it ended and nothing restarts it, and its turn
    ``key`` (``shadow:<current ruling>``). Read only when the fix is owed (``shadow_fix_owed``):
    the upheld hold's text (#2263 review F1), ``explain``, ``ready`` and the ruling notice."""
    import sqlite3
    from ..run_supervisor import (NOT_ENDED, REARMABLE, SHADOW_FIX_KEY, intake,
                                  production_ledger, views)
    ruled = upheld_shadow_ruling(loop, number, head, db=db)
    try:
        con = views.read_only_connect(db or production_ledger()) if ruled is not None else None
        if con is None:
            raise sqlite3.OperationalError("no upheld ruling, or no ledger, to read the turn from")
        con.row_factory = sqlite3.Row
        try:
            key = f"{SHADOW_FIX_KEY}{int(ruled)}"
            row = con.execute("SELECT * FROM runs WHERE repo=? AND pr=? AND head=? AND "
                              "seat='fixer' AND turn_key=?", (loop["repo"], number, head, key)).fetchone()
            if row is None:
                return "", "", ""
            rearms = row["state"] in REARMABLE and intake.redelivery_refusal(
                con, row, admitted=True, production=True, seat="fixer") is None
            return row["state"], ("live" if row["state"] in NOT_ENDED else
                                  "rearmed" if rearms else ""), key
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        if strict:
            raise
        return "", "", ""


def upheld_shadow_ruling(loop: dict, number: int, head: str, db=None) -> float | None:
    """When the adjudicator upheld this head's newest shadow review (#1004 part 3): the REJECT
    ruling that answers that review (``ruling_answers``), as its time; None otherwise, or when the
    ledger cannot be read. One rule for queueing the fix, launching it, and its push."""
    dissent, ruling = shadow_dissent_ruling(loop, number, head, db=db)
    return float(ruling[1]) if dissent and ruling is not None and str(ruling[0]) == "REJECT" else None


def shadow_dissent_ruling(loop: dict, number: int, head: str, db=None) -> tuple[bool, tuple | None]:
    """``(dissent, ruling)``: whether this head's newest shadow review requested changes, and the
    ruling that answers that review (``ruling_answers``: the one its prompt showed, #1112) as a
    ``_rulings_at`` row, or None while none does. ``(False, None)`` when the newest review is
    not a dissent, or the ledger is absent or cannot be read. One rule for the upheld fix
    (``upheld_shadow_ruling``) and for the ruling ``ready`` and ``explain`` report on a shadow
    escalation (#1144), so an ACCEPT of an earlier review never reads as this one's."""
    import sqlite3
    from pathlib import Path
    from .. import run_supervisor
    path = Path(db or run_supervisor.production_ledger())
    if not path.exists():
        return False, None
    con = run_supervisor.views.read_only_connect(path)
    if con is None:
        return False, None
    try:
        review = _latest_shadow_review(con, loop["repo"], number, head)
        if review is None or str(review[0]) != "REQUEST_CHANGES":
            return False, None
        ruling = next((r for r in _rulings_at(con, loop["repo"], number, head)
                       if ruling_answers(r, review[2], review[1])), None)
    except sqlite3.Error:
        return False, None
    finally:
        con.close()
    return True, ruling


def queue_shadow_fix(loop: dict, number: int, head: str, db=None) -> str:
    """Hand an upheld shadow review's findings to the fixer (#1004 part 3), once per ruling: the
    turn key is ``shadow:<ruling time>``, so a redelivery or a second sweep dedups. What happened:
    ``""`` (nothing upheld), ``push off``, ``not a fixer PR``, or the ledger's enqueue outcome."""
    from .. import run_supervisor
    ruled = upheld_shadow_ruling(loop, number, head, db=db)
    if ruled is None:
        return ""
    if not config.unattended_fixer_push_enabled(loop):
        return "push off"        # the hold stays the operator's: no turn that could not publish
    if _review_only_pr(loop, number):
        return "not a fixer PR"  # a review-only PR's author answers it, not the fixer
    return gate.enqueue_isolated(loop, "fixer", number, head,
                                 turn_key=f"{run_supervisor.SHADOW_FIX_KEY}{int(ruled)}")


def shadow_overruled(loop: dict, marker, head: str, db=None) -> bool:
    """Whether this head's breach marker is a shadow escalation (#1004) the adjudicator
    overruled (ACCEPT): the reviewer's approval stands, so it parks nothing any more."""
    m = marker if isinstance(marker, dict) else {}
    if str(m.get("head") or "") != head or (m.get("escalated") or {}).get("by") != gate.SHADOW_ESCALATION:
        return False
    held, _note = shadow_hold(loop, int(m.get("pr") or 0), head, db=db, marker=m)
    return not held and "overruled the shadow review" in _note


def _shadow_review_and_ruling(loop: dict, number: int, head: str, marker, db=None):
    """``(newest shadow review row, ruling on the marker's round)`` at a head, from the run
    ledger; the ruling is None unless the marker is this head's shadow escalation, or none is in
    yet. None when the ledger cannot be read."""
    import sqlite3
    from .. import run_supervisor
    mine = (isinstance(marker, dict) and marker.get("head") == head
            and (marker.get("escalated") or {}).get("by") == gate.SHADOW_ESCALATION)
    try:
        con = run_supervisor.views.read_only_connect(db or run_supervisor.production_ledger())
        if con is None:
            return None
        try:
            row = _latest_shadow_review(con, loop["repo"], number, head)
            # The ruling on this marker's round, if it is in.
            ruled = next(iter(_rulings_at(con, loop["repo"], number, head,
                                          f"breach:{marker.get('rounds')}")), None) if mine else None
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return None
    return row, ruled


def shadow_escalation(loop: dict, st, number: int, head: str, reviews, db=None) -> str:
    """Send a head for a ruling when the shadow review disagrees with the reviewer's approval
    (#1004 part 2): ``shadow.escalate`` on, the reviewer's effective verdict at the head
    APPROVED (its own approval, not the adjudicator's ruled one: ``gate.reviewer_approved``,
    #1706) and that approval not stale (#473: a required check at the head has since gone
    red, cancelled or missing, the finding ``ready`` and ``explain`` deny the approval by), and
    the newest shadow review there REQUEST_CHANGES. A stale approval is "not approved": the
    ruling prompt says the reviewer approved the head, which it no longer vouches for. Once per
    shadow review: the breach marker dedups, and a dissent newer than the head's ruling goes as
    the next round (#1111). ``gate.breach``'s outcome, or why nothing was sent."""
    if not config.shadow_escalate(loop):
        return "off"
    if not loop.get("adjudicator", {}).get("route"):
        return "no adjudicator"           # the hold stays; the decision is the operator's
    if not isinstance(reviews, list):
        return "reviews unreadable"
    # #1706: the reviewer's own approval. The adjudicator's ruled one (#905) stands over the
    # reviewer's change request, and the ruling prompt would say the reviewer approved.
    if not gate.reviewer_approved(gate.latest_effective_review_at_head(reviews, loop, head), loop):
        return "not approved"
    if stale_approval.is_stale(st.watch(), number, head):
        return "not approved"             # the one test ``ready`` and ``explain`` apply
    marker = st.breach_get(number)
    read = _shadow_review_and_ruling(loop, number, head, marker, db)
    if read is None:
        return "ledger unreadable"
    row, ruled = read
    if row is None or str(row[0]) != "REQUEST_CHANGES":
        return "shadow agrees"
    if ruled is not None and not ruling_answers(ruled, row[2], row[1]):
        # #1111: the shadow dissented again after its ruling (a re-review at the head). That
        # ruling answered an older review (#1112: by run id, so one that landed during the
        # adjudicator's turn counts), so this one goes for its own, as the next round.
        reason = (f"the shadow review requested changes again at {head[:7]} after the "
                  "adjudicator's ruling: rule on its new findings")
        rounds = int(marker.get("rounds") or 0)
        if not st.breach_reopen(number, head, rounds, reason):
            return "already"
        outcome = gate.breach(
            loop, st, number, head, rounds + 1, reason,
            escalated={"by": gate.SHADOW_ESCALATION, "at": now_iso(), "reason": reason})
        return "new" if outcome == "retry" else outcome     # breach() delivers the re-armed one
    # A wake that failed leaves the marker delivery-pending: retry it here, since the sweep's
    # own delivery retry skips approved heads and every shadow escalation is at one.
    retry = (isinstance(marker, dict) and marker.get("status") == "delivery-pending"
             and (marker.get("escalated") or {}).get("by") == gate.SHADOW_ESCALATION)
    if gate.escalated(marker, head) and not retry:
        return "already"
    reason = (f"the shadow review requested changes at {head[:7]} while the reviewer approved "
              "it: rule on the shadow review's findings")
    # Rounds count the verdicts at the head, the approval included: a breach turn needs one at
    # least (its turn key, ``breach:<rounds>``), and an approved head has no blocking verdict.
    rounds = max(1, len(gate.verdicts(reviews, loop)) + 1)
    return gate.breach(loop, st, number, head, rounds, reason,
                       escalated={"by": gate.SHADOW_ESCALATION, "at": now_iso(), "reason": reason})


def shadow_snapshot_gone(st, repo: str, number: int, head: str, turn_key: str, db=None) -> bool:
    """Whether this reviewer turn's shadow row is in the ledger ``db`` but its own findings
    snapshot is not (evicted, or from before turns were keyed): one taken now would be the live
    ledger's (#1139). Call it under the state lock; raises when the ledger cannot be read."""
    return (st.shadow_findings_get(number, head, turn_key) is None
            and shadow_row_exists(repo, number, head, turn_key, db))


def snapshot_shadow(loop: dict, number: int, head: str, turn_key: str = "", db=None) -> bool:
    """Keep the findings ledger for a sampled head's shadow review (#218): the broker judges the
    shadow against it, never against what the real review records. Taken before the reviewer's
    row exists (#1647); the first snapshot wins. Whether one is kept; never raises. Never for a
    shadow row already in the ledger ``db`` whose own snapshot is gone (#1139): a re-queued
    reviewer turn would otherwise give it one from the live ledger, which ``retry`` accepts."""
    if not config.shadow_profile(loop) or not shadow_sampled(loop, number, head, turn_key):
        return False
    try:
        st = state_mod.state_for(loop)
        with st.locked():
            if shadow_snapshot_gone(st, loop["repo"], number, head, turn_key, db):
                from ..run_supervisor import intake
                log(f"#{number}: no shadow findings snapshot taken: {intake.SHADOW_SNAPSHOT_GONE}")
                return False
            try:
                keep = shadow_open_heads(loop["repo"])
            except Exception as exc:
                keep = None                  # unknown open runs: evict nothing
                log(f"#{number}: shadow runs unreadable, no snapshot evicted: "
                    f"{type(exc).__name__}: {exc}")
            st.shadow_findings_put(number, head, st.findings_get(number), keep, turn_key)
        return True
    except Exception as exc:
        log(f"#{number}: shadow findings snapshot not kept: {type(exc).__name__}: {exc}")
        return False


def shadow_delivery(loop: dict, number: int, head: str, turn_key: str = "") -> str:
    """A shadow row's delivery key: its reviewer turn's, on the shadow seat."""
    return f"{loop['repo']}:{number}:{head}:shadow" + (f":{turn_key}" if turn_key else "")


def enqueue_shadow(loop: dict, supervisor, number: int, head: str, *, turn_key: str = "") -> str:
    """Queue a shadow review beside a newly queued reviewer turn (#218), when the loop has a
    shadow profile and this head is sampled. It runs only once that reviewer turn's APPROVE is
    confirmed (#1647, ``shadow_queue.after_reviewer``). Best effort and silent: a shadow is never
    part of the loop, so nothing here can change what the reviewer's enqueue answers. Returns
    what happened, for logs and tests. A shadow row already in the ledger whose findings
    snapshot is gone is neither re-armed nor given a new snapshot (``snapshot gone``, #1139):
    ``retry`` refuses the same, and a snapshot taken now would be the live ledger's."""
    try:
        if not config.shadow_profile(loop):
            return "off"
        if not shadow_sampled(loop, number, head, turn_key):
            return "not sampled"
        delivery = shadow_delivery(loop, number, head, turn_key)
        # No durable snapshot, no shadow: the broker would judge it on the live ledger, which
        # the real review of this head may change meanwhile (#218, #1013).
        try:
            st = state_mod.state_for(loop)
        except Exception as exc:
            log(f"#{number}: shadow findings snapshot not kept, shadow not queued: "
                f"{type(exc).__name__}: {exc}")
            return "no snapshot"
        # One state lock over the open-runs read, the snapshot write and the submit: a second
        # enqueue cannot sample the ledger before this run is visible in it and then evict this
        # snapshot as unused (#1013). The lock is reentrant.
        with st.locked():
            # A row that is already queued keeps the snapshot it was queued with (#1139): one
            # evicted since would be taken again from the live ledger, and ``submit`` would
            # re-arm the row on it. ``retry`` refuses the same (the same lock, then the ledger).
            # Snapshots are kept per reviewer turn (``shadow_findings_put``), so another turn's,
            # or one from before turns were keyed, never stands in for this turn's own. The row
            # is looked up in the ledger this supervisor submits to, not only the production one.
            ledger_db = gate._ledger_of(supervisor)
            if shadow_snapshot_gone(st, loop["repo"], number, head, turn_key, ledger_db):
                from ..run_supervisor import intake
                log(f"#{number}: shadow not queued again: {intake.SHADOW_SNAPSHOT_GONE}")
                return "snapshot gone"
            if not snapshot_shadow(loop, number, head, turn_key, ledger_db):
                log(f"#{number}: no shadow findings snapshot, shadow not queued")
                return "no snapshot"
            return supervisor.submit(delivery, loop["repo"], number, head, "shadow",
                                     turn_key=turn_key, budget=config.turn_budget(loop, "shadow"))
    except Exception as exc:
        log(f"#{number}: shadow review not queued: {type(exc).__name__}: {exc}")
        return "error"
