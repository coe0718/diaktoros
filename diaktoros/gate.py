"""The gate runtime: the parts both gates share.

A gate is a script between a GitHub event and an agent run. It answers one question —
*should this event start a run, and under what terms?* — and it must answer it the same
way twice, out loud or not at all. Every branch here ends in either ``silence()`` (no run,
no tokens, nothing announced) or a payload annotated with a ``_loop`` block the route
prompt can render.

The order of the guards matters and is the same in both gates:

1. is this event even ours (repo, action, who did it);
2. can we read the facts we need (never guess a round count from a failed API call);
3. has this exact head already been handled (in-flight marks);
4. is the budget spent (cap → hand the PR to adjudication, do not buy another round);
5. does the other seat already own this PR (one PR runs one seat at a time — queue, don't start);
6. is there a free slot for this seat (otherwise queue it and stay quiet);
7. can this run be isolated (above ``concurrency 1`` a shared clone is not an option);
8. only then: record, prepare the workspace, announce, fire.

A gate must free the *other* seat's turn **before** it claims its own (the fixer's request is what
ends the fixer's turn, the reviewer's verdict is what ends the reviewer's) — claim first and the
gates deadlock against each other, each waiting for the other's hold to clear.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
import pathlib
import subprocess
import sys
import time

from . import config, gh, isolation, observer, state as state_mod
from .util import log, now_iso, silence
from . import envnames


def payload_loop(payload: dict) -> dict:
    full = ((payload.get("repository") or {}).get("full_name") or "")
    # Per-file: a sibling loop file the loader refuses is skipped with one log line instead of
    # stopping this repo's gate (it still fails closed when that file might be this repo's).
    try:
        loop = config.loop_for_repo(full, warn=log)
    except config.ConfigError as exc:
        # This repo's ownership cannot be settled (its own file will not load, or two files
        # claim it): fail closed — nothing runs — with the reason in the log, not a traceback.
        log(f"loop config refused for {full or 'an unknown repository'}: {exc}")
        silence(f"loop config refused for {full or 'an unknown repository'}")
    if not loop:
        silence(f"no loop configured for {full or 'an unknown repository'}")
    return loop


def context(payload: dict):
    loop = payload_loop(payload)
    # A gate's failed GitHub read ends in [SILENT]; keep it on disk for the watchdog and explain.
    gh.record_failures()
    _record_decisions(loop, payload)
    from . import app_ingress
    if config.app_ingress(loop) and app_ingress.woken_by_repo_hook():
        silence(f"{loop['id']} takes its events from {loop.get('ingress_app')}'s webhook: this "
                "delivery came from a leftover repo hook and is dropped (delete the repo's hooks "
                "to this loop's routes)")
    return loop, state_mod.state_for(loop)


def _record_decisions(loop: dict, payload: dict) -> None:
    """From here on, every ``silence(reason)`` in this process is appended to the loop's
    gate-decisions.jsonl (#209)."""
    from . import gate_decisions, util
    name = pathlib.Path(sys.argv[0] or "gate").stem or "gate"
    util.set_decision_recorder(lambda reason, decision: gate_decisions.record(
        loop, name, payload, decision, reason))


def pr_of(payload: dict) -> dict:
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        silence("no pull_request in payload")
    return pr


def number_of(payload: dict, pr: dict) -> int:
    number = pr.get("number") or payload.get("number")
    if not number:
        silence("payload has no PR number")
    return int(number)


def head_of(pr: dict) -> str:
    head = (pr.get("head") or {}).get("sha") or ""
    if not head:
        silence("payload has no head sha")
    return head


def seat_key(loop: dict, number: int) -> str:
    return f"{loop['repo']}#{number}"


def pr_url(loop: dict, number: int) -> str:
    """The PR's web URL. ``gh`` owns it, because the observer's notices need it too."""
    return gh.pr_url(loop, number)


def artifacts_for(loop: dict, number: int) -> str:
    return str(config.artifacts_dir(loop, number))


def isolated_supervisor(loop: dict):
    """The production run ledger and worker launcher, as every isolated enqueue builds it.

    Raises when the private runtime file is missing or invalid (a fail-closed hold).
    """
    from . import seat_model
    from .run_supervisor import SEATS, Supervisor, presence_marker

    # presence= defaults to the config-dir marker for this (the production) ledger path.
    supervisor = Supervisor(
        config.host_path("ledger"),
        presence=presence_marker(),
        production_config=config.host_path("runtime"), hermes_home=config.home(),
        # Every seat, always: a worker spawned by one seat's event claims any pending row, and
        # must know every seat's capacity to do so.
        capacity={s: config.seat_concurrency(loop, s) for s in SEATS},
    )
    # Host-side, once the runtime is known to be valid (no state without one): a worker writes
    # into the loop's state dir (broker audit, observer ledger), its private work root, its
    # crate cache root and the seat-lock dir, but never creates a host directory (#108), so the
    # host makes sure each exists before any worker is spawned — for enqueue_isolated and
    # resume_isolated alike, since both build their supervisor here.
    state_dir = config._path(loop["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "isolated-runs").mkdir(mode=0o700, exist_ok=True)
    (state_dir / "deps").mkdir(mode=0o700, exist_ok=True)  # the worker's crate cache root
    seat_model.lock_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    return supervisor


# The ``enqueue_isolated`` outcomes that scheduled (or re-armed) a turn; any other outcome
# (``duplicate <state>…``) left nothing to run and must not be reported as a fresh enqueue.
SCHEDULED_OUTCOMES = ("enqueued", "rearmed", "pending")

# A queue row the gate kept after declining to schedule (``duplicate <state>``) starts with this:
# a confirmed terminal answer, not a delivery the watchdog should replay or call uncertain.
DECLINED_PREFIX = "turn declined — "


def declined_reason(seat: str, outcome: str) -> str:
    return f"{DECLINED_PREFIX}{seat} turn {outcome}; nothing scheduled, not retried"


def is_declined_hold(entry: object) -> bool:
    reason = entry.get("reason") if isinstance(entry, dict) else None
    return isinstance(reason, str) and reason.startswith(DECLINED_PREFIX)


def enqueue_isolated(loop: dict, seat: str, number: int, head: str, *, turn_key: str = '') -> str:
    """Commit one isolated turn to the host run ledger, then arm its detached worker.

    Raises on anything short of a durable, armed enqueue — a missing/invalid private runtime,
    a ledger failure, or a spawn failure after the row committed (the row stays pending and a
    redelivery re-arms it). The unique repo/PR/head/seat/turn index deduplicates redelivery.
    Returns the ledger's outcome (``Supervisor.submit``): ``enqueued``, ``rearmed``,
    ``pending`` or ``duplicate <state>…`` — only the first three scheduled anything.
    """
    supervisor = isolated_supervisor(loop)
    supervisor.recover()
    delivery = f"{loop['repo']}:{number}:{head}:{seat}"
    # The turn's wall clock rides on the row (#49): whichever worker claims it — possibly one
    # spawned by another loop's event — runs it for this loop's seat budget, not a worker default.
    outcome = supervisor.submit(delivery + (f':{turn_key}' if turn_key else ''),
                                loop["repo"], number, head, seat, turn_key=turn_key,
                                require_push_admission=seat == "fixer",
                                budget=config.turn_budget(loop, seat))
    if seat == "reviewer" and outcome == "enqueued":
        enqueue_shadow(loop, supervisor, number, head, turn_key=turn_key)
    return outcome


def shadow_sampled(loop: dict, number: int, head: str) -> bool:
    """Whether this reviewer turn also gets a shadow review (#218): one in ``shadow.every``,
    chosen by the repo, PR and head, so a redelivery or a re-queued turn chooses the same."""
    import hashlib
    every = config.shadow_every(loop)
    digest = hashlib.sha256(f"{loop['repo']}:{number}:{head}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % every == 0


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


def _latest_shadow_review(con, repo: str, number: int, head: str):
    """The newest shadow review of a head, as ``(verdict, created, run_id)``, or None."""
    return con.execute("SELECT verdict, created, run_id FROM shadow_reviews WHERE repo=? AND pr=? "
                       "AND head=? ORDER BY created DESC LIMIT 1", (repo, number, head)).fetchone()


def ruling_answers(ruling, review_run: str, review_created: float) -> bool:
    """Whether a ruling (a ``_rulings_at`` row) answers this shadow review (#1112): the one its
    prompt showed, by run id. A ruling from before that was recorded (NULL) falls back to the
    time rule: made after the review. One rule for the hold and for the escalation check."""
    shown = ruling[2]
    if shown is None:
        return float(ruling[1]) > float(review_created)
    return str(shown) == str(review_run)


def shadow_open_heads(db=None) -> set[tuple[int, str]]:
    """``(pr, head)`` of every shadow run still open (``SHADOW_OPEN``), whose findings snapshot
    must outlive it (#1013). Empty when the ledger does not exist yet; raises when it exists
    but cannot be read, so the caller never evicts on a guess."""
    from . import run_supervisor
    path = pathlib.Path(db or run_supervisor.production_ledger())
    if not path.exists():
        return set()
    con = run_supervisor.views.read_only_connect(path)
    if con is None:
        return set()
    try:
        rows = con.execute(f"SELECT pr, head FROM runs WHERE seat='shadow' AND "
                           f"state IN ({','.join('?' * len(SHADOW_OPEN))})", SHADOW_OPEN).fetchall()
    finally:
        con.close()
    return {(int(r[0]), str(r[1])) for r in rows}


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
    only, never a hold, when there is nothing to wait for: no ledger yet, no shadow run queued
    (sampled out, or queued before escalate was on), or runs that ended without a review, so a
    failed shadow never parks a PR for good. ``("", "", "")`` with escalate off; ``shadow_hold`` is
    the ``(hold, note)`` view of it."""
    if not config.shadow_escalate(loop):
        return "", "", ""
    import sqlite3
    from pathlib import Path
    from . import run_supervisor
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
            try:
                review = _latest_shadow_review(con, loop["repo"], number, head)
            except sqlite3.OperationalError:
                review = None              # a ledger from before shadow reviews: none recorded
            run = con.execute("SELECT state, created FROM runs WHERE repo=? AND pr=? AND head=? "
                              "AND seat='shadow' ORDER BY created DESC, updated DESC LIMIT 1",
                              (loop["repo"], number, head)).fetchone()
            try:
                # A ruling answers the shadow review it was shown (#1112: its run id, not the
                # time it landed): a later dissent at the same head, even one that landed during
                # the adjudicator's turn, is not overruled by it (reviews on #1103).
                ruling = None
                if review is not None:
                    ruling = next((r for r in _rulings_at(con, loop["repo"], number, head)
                                   if ruling_answers(r, review[2], review[1])), None)
            except sqlite3.OperationalError:
                ruling = None
            pending = con.execute(
                f"SELECT state FROM runs WHERE repo=? AND pr=? AND head=? AND seat='shadow' AND "
                f"state IN ({','.join('?' * len(SHADOW_OPEN))}) AND created>? "
                "ORDER BY created DESC LIMIT 1",
                (loop["repo"], number, head, *SHADOW_OPEN,
                 float(review[1]) if review is not None else -1.0)).fetchone()
        finally:
            con.close()
    except Exception as exc:
        return "pending", (f"the run ledger is unreadable ({type(exc).__name__}), so the shadow review at "
                f"{short} is unknown"), ""
    if review is not None and str(review[0]) == "REQUEST_CHANGES":
        mine = isinstance(marker, dict) and str(marker.get("head") or "") == head and (
            (marker.get("escalated") or {}).get("by") == SHADOW_ESCALATION)
        verdict = str(ruling[0]) if ruling else ""
        if mine and verdict == "ACCEPT":
            # Part 2 (#1004): the adjudicator overruled it; the reviewer's approval stands.
            return "", "", f"the adjudicator overruled the shadow review at {short} (ACCEPT)"
        if mine and verdict == "REJECT":
            # Part 3 (#1004): upheld. The fixer answers it where queue_shadow_fix can hand it on;
            # a loop without unattended pushes, or a review-only author's PR, is the operator's.
            if config.unattended_fixer_push_enabled(loop) and not _review_only_pr(loop, number):
                return "failure", (f"the adjudicator upheld the shadow review at {short} (REJECT): "
                        "the fixer answers its findings, and the fixed head is reviewed fresh"), ""
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
    if pending is not None:
        # A shadow run queued after the newest review (a re-review at the same head, #1097's
        # shadow review) can still change it: wait for it, even past an earlier approval.
        return "pending", f"waiting on the shadow review at {short} ({pending[0]})", ""
    if review is not None:
        return "", "", ""
    if run is None:
        return "", "", f"no shadow review was queued for {short}: nothing to wait for"
    return "", "", f"the shadow run at {short} ended {run[0]} without a review: nothing to wait for"


SHADOW_ESCALATION = "shadow"           # the breach marker's ``escalated.by`` for #1004


def _review_only_pr(loop: dict, number: int) -> bool:
    """Whether a PR's author is not one of the loop's fixers (its author answers it), read live;
    an unreadable PR counts as not the fixer's, so no fix is promised on a guess."""
    pr = gh.pr(loop, number)
    author = str(((pr or {}).get("user") or {}).get("login") or "").casefold()
    return not author or author not in {str(x).casefold() for x in loop.get("fixers") or []}


def upheld_shadow_ruling(loop: dict, number: int, head: str, db=None) -> float | None:
    """When the adjudicator upheld this head's newest shadow review (#1004 part 3): the REJECT
    ruling that answers that review (``ruling_answers``), as its time; None otherwise, or when the
    ledger cannot be read. One rule for queueing the fix, launching it, and its push."""
    import sqlite3
    from pathlib import Path
    from . import run_supervisor
    path = Path(db or run_supervisor.production_ledger())
    if not path.exists():
        return None
    con = run_supervisor.views.read_only_connect(path)
    if con is None:
        return None
    try:
        review = _latest_shadow_review(con, loop["repo"], number, head)
        if review is None or str(review[0]) != "REQUEST_CHANGES":
            return None
        ruling = next((r for r in _rulings_at(con, loop["repo"], number, head)
                       if ruling_answers(r, review[2], review[1])), None)
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return float(ruling[1]) if ruling is not None and str(ruling[0]) == "REJECT" else None


def queue_shadow_fix(loop: dict, number: int, head: str, db=None) -> str:
    """Hand an upheld shadow review's findings to the fixer (#1004 part 3), once per ruling: the
    turn key is ``shadow:<ruling time>``, so a redelivery or a second sweep dedups. What happened:
    ``""`` (nothing upheld), ``push off``, ``not a fixer PR``, or the ledger's enqueue outcome."""
    from . import run_supervisor
    ruled = upheld_shadow_ruling(loop, number, head, db=db)
    if ruled is None:
        return ""
    if not config.unattended_fixer_push_enabled(loop):
        return "push off"        # the hold stays the operator's: no turn that could not publish
    if _review_only_pr(loop, number):
        return "not a fixer PR"  # a review-only PR's author answers it, not the fixer
    return enqueue_isolated(loop, "fixer", number, head,
                            turn_key=f"{run_supervisor.SHADOW_FIX_KEY}{int(ruled)}")


def shadow_overruled(loop: dict, marker, head: str, db=None) -> bool:
    """Whether this head's breach marker is a shadow escalation (#1004) the adjudicator
    overruled (ACCEPT): the reviewer's approval stands, so it parks nothing any more."""
    m = marker if isinstance(marker, dict) else {}
    if str(m.get("head") or "") != head or (m.get("escalated") or {}).get("by") != SHADOW_ESCALATION:
        return False
    held, _note = shadow_hold(loop, int(m.get("pr") or 0), head, db=db, marker=m)
    return not held and "overruled the shadow review" in _note


def shadow_escalation(loop: dict, st, number: int, head: str, reviews, db=None) -> str:
    """Send a head for a ruling when the shadow review disagrees with the reviewer's approval
    (#1004 part 2): ``shadow.escalate`` on, the reviewer's effective verdict at the head
    APPROVED, and the newest shadow review there REQUEST_CHANGES. Once per shadow review: the
    breach marker dedups, and a dissent newer than the head's ruling goes as the next round
    (#1111). ``gate.breach``'s outcome, or why nothing was sent."""
    if not config.shadow_escalate(loop):
        return "off"
    if not loop.get("adjudicator", {}).get("route"):
        return "no adjudicator"           # the hold stays; the decision is the operator's
    if not isinstance(reviews, list):
        return "reviews unreadable"
    latest = latest_effective_review_at_head(reviews, loop, head)
    if latest is None or gh.review_state(latest) != "APPROVED":
        return "not approved"
    import sqlite3
    from . import run_supervisor
    marker = st.breach_get(number)
    mine = (isinstance(marker, dict) and marker.get("head") == head
            and (marker.get("escalated") or {}).get("by") == SHADOW_ESCALATION)
    try:
        con = run_supervisor.views.read_only_connect(db or run_supervisor.production_ledger())
        if con is None:
            return "ledger unreadable"
        try:
            row = _latest_shadow_review(con, loop["repo"], number, head)
            # The ruling on this marker's round, if it is in.
            ruled = next(iter(_rulings_at(con, loop["repo"], number, head,
                                          f"breach:{marker.get('rounds')}")), None) if mine else None
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return "ledger unreadable"
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
        outcome = breach(loop, st, number, head, rounds + 1, reason,
                         escalated={"by": SHADOW_ESCALATION, "at": now_iso(), "reason": reason})
        return "new" if outcome == "retry" else outcome     # breach() delivers the re-armed one
    # A wake that failed leaves the marker delivery-pending: retry it here, since the sweep's
    # own delivery retry skips approved heads and every shadow escalation is at one.
    retry = (isinstance(marker, dict) and marker.get("status") == "delivery-pending"
             and (marker.get("escalated") or {}).get("by") == SHADOW_ESCALATION)
    if escalated(marker, head) and not retry:
        return "already"
    reason = (f"the shadow review requested changes at {head[:7]} while the reviewer approved "
              "it: rule on the shadow review's findings")
    # Rounds count the verdicts at the head, the approval included: a breach turn needs one at
    # least (its turn key, ``breach:<rounds>``), and an approved head has no blocking verdict.
    rounds = max(1, len(verdicts(reviews, loop)) + 1)
    return breach(loop, st, number, head, rounds, reason,
                  escalated={"by": SHADOW_ESCALATION, "at": now_iso(), "reason": reason})


def enqueue_shadow(loop: dict, supervisor, number: int, head: str, *, turn_key: str = "") -> str:
    """Queue a shadow review beside a newly queued reviewer turn (#218), when the loop has a
    shadow profile and this head is sampled. Best effort and silent: a shadow is never part of
    the loop, so nothing here can change what the reviewer's enqueue answers. Returns what
    happened, for logs and tests."""
    try:
        if not config.shadow_profile(loop):
            return "off"
        if not shadow_sampled(loop, number, head):
            return "not sampled"
        delivery = f"{loop['repo']}:{number}:{head}:shadow" + (f":{turn_key}" if turn_key else "")
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
            try:
                try:
                    keep = shadow_open_heads()
                except Exception as exc:
                    keep = None                  # unknown open runs: evict nothing
                    log(f"#{number}: shadow runs unreadable, no snapshot evicted: "
                        f"{type(exc).__name__}: {exc}")
                st.shadow_findings_put(number, head, st.findings_get(number), keep)
            except Exception as exc:
                log(f"#{number}: shadow findings snapshot not kept, shadow not queued: "
                    f"{type(exc).__name__}: {exc}")
                return "no snapshot"
            return supervisor.submit(delivery, loop["repo"], number, head, "shadow",
                                     turn_key=turn_key, budget=config.turn_budget(loop, "shadow"))
    except Exception as exc:
        log(f"#{number}: shadow review not queued: {type(exc).__name__}: {exc}")
        return "error"


def resume_isolated(loop: dict) -> bool:
    """Schedule ledger work that is due — a backed-off pre-write retry (#53), or a pending
    row whose claim-time read failed — by running the worker-enabled recovery. False (and
    nothing launched) when no private runtime is configured."""
    if not config.host_path("runtime").exists():
        return False
    isolated_supervisor(loop).recover()
    return True


def hold_fixer_push_off(loop: dict, st: state_mod.LoopState, number: int,
                        head: str, reason: str | None = None) -> bool:
    """Hold a changes-requested verdict for the operator: the fixer's writes are all refused.

    No ledger row, no worker, no model turn. ``reason`` names the denial (the shared
    ``broker_ipc`` wording, #81) and carries the exact enable command, so explain, the
    watchdog and ``status`` show it; the watchdog re-evaluates the held head once the loop
    opts in. Returns ``True`` only when this call recorded the hold
    (an identical hold for this head is left as it was, so its age stays honest).
    """
    key = seat_key(loop, number)
    queued = st.queue_items("fixer").get(key)
    # ``reason`` already carries the shared denial (#81) when the gate named one; when it is
    # not given, the plain hold is the whole reason. Either way an identical hold for this
    # head is left exactly as it was (#81): rewriting it would only move its timestamp, and a
    # redelivered verdict must stay one hold.
    hold = reason or config.fixer_push_hold_reason(loop)
    if isinstance(queued, dict) and queued.get("head") == head and queued.get("reason") == hold:
        return False
    return st.queue_replace_if("fixer", key, queued, head, pr_url(loop, number), hold)


def fixer_push_denial(loop: dict, context: str = "") -> str:
    """Why the host refuses every fixer write on this loop (#81), for the operator, else ``""``.

    One source of truth for the *reason*: the queue hold, the operator's ``next_turn`` notice
    and the broker's refusal all name ``broker_ipc.policy_hold_reason``. Only the seat gets it
    inside ``FIXER_WRITE_DENIED``, whose second half instructs the model ("say plainly in your
    summary…"); the operator reads the reason alone. The plain push-off reason is not repeated:
    the hold and the notice already say pushes are off and give the enable command. An
    unreadable policy read returns ``""`` — it must never drop the enable command or the hold
    itself — and ``context`` prefixes the diagnostic.
    """
    from . import broker_ipc
    from .run_supervisor import FIXER_PUSH_OFF
    try:
        denial = broker_ipc.policy_hold_reason(loop)
    except Exception as exc:
        log(f"{context}fixer policy read failed: {type(exc).__name__}: {exc}")
        return ""
    return "" if denial in ("", FIXER_PUSH_OFF) else denial


def fixer_push_off_notice(loop: dict) -> str:
    """``next_turn`` for a held changes-requested verdict (#81), naming the denial.

    ``scripts/gate_fixer.py`` builds the operator's notice before ``block_pr_agent`` runs, so
    the denial is named here rather than there: the notice and the queue's hold reason then
    say the same words about the same loop.
    """
    notice = (f"you — fixer held: unattended fixer pushes are off; to let the fixer answer, "
              f"run `{config.fixer_push_enable_command(loop)}`")
    denial = fixer_push_denial(loop)
    return f"{notice} — {denial}" if denial else notice


def block_pr_agent(loop: dict, st: state_mod.LoopState, seat: str,
                   number: int, head: str, on_queued=None, *, turn_key: str = '',
                   on_push_off=None) -> None:
    """Durably enqueue an isolated turn; NEVER return a gateway-dispatch payload.

    An absent/invalid private runtime configuration is a visible fail-closed hold.
    The ledger's unique repo/PR/head/seat/turn index deduplicates webhook redelivery
    before launching a detached worker. No checkout or agent runs in this script.
    A fixer turn on a loop without unattended pushes is held instead (``hold_fixer_push_off``),
    and ``on_push_off`` (when given) is the notice sent in place of ``on_queued``.
    """
    from .run_supervisor import FixerPushDisabled

    key = seat_key(loop, number)
    queued = st.queue_items(seat).get(key)
    push_off = seat == "fixer" and not config.unattended_fixer_push_enabled(loop)
    final = (f"{seat} turn: nothing scheduled", "queued")   # what silence() records below
    if not push_off:
        try:
            outcome = enqueue_isolated(loop, seat, number, head, turn_key=turn_key)
            scheduled = outcome in SCHEDULED_OUTCOMES
            if scheduled:
                # Only the gate removes its observed queue entry, and only once a turn is
                # really armed (#729): a ``duplicate <state>`` scheduled nothing.
                st.queue_pop_if(seat, key, queued)
            else:
                on_queued = None   # no "queued" notice for a turn that will never run
                # Keep the observed row, but say why: the watchdog must tell this confirmed
                # decline from an acknowledged POST the gate never answered. Only a row that
                # exists is reworded; a decline never creates one.
                if isinstance(queued, dict) and queued.get("reason") != declined_reason(seat, outcome):
                    st.queue_replace_if(seat, key, queued, queued.get("head") or head,
                                        queued.get("url") or pr_url(loop, number),
                                        declined_reason(seat, outcome))
            final = (f"{seat} turn {outcome}", "accepted" if scheduled else "declined")
            if scheduled and seat == "reviewer":
                from . import merge_check          # #1242: the check shows at once, pending
                merge_check.mark_pending(loop, st, number, head, "review queued")
            if outcome == "enqueued":
                log(f"#{number} @ {head[:7]} {seat} enqueued for isolated worker")
            elif scheduled:
                log(f"#{number} @ {head[:7]} {seat} {outcome}: isolated worker re-armed")
            else:
                # Nothing was scheduled (#73): say so, instead of reporting a fresh enqueue.
                log(f"#{number} @ {head[:7]} {seat} not enqueued: {outcome}")
        except FixerPushDisabled:
            push_off = True  # the policy changed under the admission lock: same hold
        except Exception as exc:
            reason = f"isolated worker unavailable: {type(exc).__name__}: {exc}"
            st.queue_replace_if(seat, key, queued, head, pr_url(loop, number), reason)
            log(f"#{number} @ {head[:7]} {seat} held: {reason}")
            final = (f"{seat} turn held: {reason}", "held")
    if push_off:
        # Checked before the runtime: a turn that could never publish is not worth a worker.
        # The hold reason keeps the exact enable command at its front (#81) — every surface
        # (``is_fixer_push_hold``, explain, the watchdog) matches on those words — with the
        # policy denial named after it.
        reason = config.fixer_push_hold_reason(loop)
        denial = fixer_push_denial(loop, context=f"#{number} @ {head[:7]} ")
        if denial:
            reason = f"{reason} {denial}"
        hold_fixer_push_off(loop, st, number, head, reason)
        final = (f"{seat} turn held: {reason}", "held")
        log(f"#{number} @ {head[:7]} fixer held: {reason}")
    notice = on_push_off if push_off and on_push_off is not None else on_queued
    if notice is not None:
        try:
            notice()
        except Exception as exc:
            log(f"#{number} @ {head[:7]} observer notice failed: {type(exc).__name__}: {exc}")
    silence(*final)


def isolation_block(loop: dict, number: int, workspace: dict | None, seat: str) -> dict:
    """Where this run is allowed to work — always present, so no prompt key renders as text.

    The gate decided the workspace; the prompt only repeats the decision. ``isolated: false``
    means no sandbox could be built, which is only allowed to happen at ``concurrency = 1``.
    """
    p = isolation.paths(loop, number, seat)
    shared = str(config.clone_path(loop)) if loop.get("clone") else ""
    if workspace:
        env = " ".join(f"{k}={v}" for k, v in workspace["env"].items()
                       if k in ("CARGO_TARGET_DIR", "TMPDIR"))
        return {"isolated": True, "root": workspace["root"], "clone": workspace["clone"],
                "target": workspace["target"], "tmp": workspace["tmp"], "env": env,
                "shared": shared, "seat": seat,
                "brief": isolation.describe(workspace, loop, number, seat)}
    return {"isolated": False, "root": str(p["root"]), "clone": shared or str(p["root"]),
            "target": "", "tmp": "", "env": "", "shared": shared, "seat": seat,
            "brief": isolation.describe(None, loop, number, seat)}


def loop_block(loop: dict, number: int, head: str, workspace: dict | None = None,
               seat: str = "reviewer", **extra) -> dict:
    block = {"pr": number, "repo": loop["repo"], "head": head, "cap": loop["cap"],
             "url": pr_url(loop, number), "artifacts": artifacts_for(loop, number),
             "concurrency": config.seat_concurrency(loop, seat),
             "isolation": isolation_block(loop, number, workspace, seat)}
    block.update(extra)
    return block


# -- reading GitHub -----------------------------------------------------------


def fetch_reviews(loop: dict, number: int):
    """The verdict list, or silence. A loop that cannot count its rounds must not guess one."""
    reviews = gh.reviews(loop, number)
    if not isinstance(reviews, list):
        silence("could not read a valid review list — not guessing the round count")
    return reviews


def is_reviewer(review: dict, loop: dict) -> bool:
    login = ((review.get("user") or {}).get("login") or "").lower()
    return login in set(loop["reviewers"])


def reviewer_login(review: dict) -> str:
    return ((review.get("user") or {}).get("login") or "").lower()


def verdicts(reviews: list, loop: dict, exclude_id=None) -> list:
    return [r for r in reviews
            if isinstance(r, dict)
            and is_reviewer(r, loop)
            and gh.review_state(r) == "CHANGES_REQUESTED"
            and (exclude_id is None or r.get("id") != exclude_id)]


def reviewed_at_head(reviews: list, loop: dict, head: str) -> bool:
    """A submitted verdict at this head, not a comment, draft, or dismissed review. The
    adjudicator's approval on its ACCEPT is one (#905): dismissing the change request it
    overruled must not make the head look unreviewed, or a fresh review would start."""
    return any(gh.review_state(r) in {"APPROVED", "CHANGES_REQUESTED"}
               for r in reviews_at_head(reviews, loop, head) + ruled_approvals(reviews, loop, head))


def reviews_at_head(reviews: list, loop: dict, head: str) -> list:
    """Reviewer reviews at this head, including non-verdicts for diagnostics only."""
    return [r for r in reviews
            if isinstance(r, dict) and is_reviewer(r, loop) and r.get("commit_id") == head]


# Review states that are not a verdict: they never supersede one (overrides.py reads this too).
NOT_VERDICTS = frozenset({"DISMISSED", "COMMENTED", "PENDING"})


def ruled_approvals(reviews: list, loop: dict, head: str) -> list:
    """The adjudicator's approval at this head, when its ACCEPT ruling on that PR backs it (#905):
    it counts as a verdict, like the reviewer's. Only that login's newest verdict at the head
    counts, so a later change request from it withdraws the approval. The ruling is the newest
    on the PR the review names (``pull_request_url``), at this head: a ruling on another PR at the
    same commit backs nothing, and cancels nothing. A bare approval, one whose PR can't be told,
    or any when the ledger can't be read counts for nothing."""
    login = config.adjudicator_login(loop).lower()
    if not login or login in set(loop["reviewers"]):
        return []
    mine = [(_review_when(r), r) for r in reviews if isinstance(r, dict)
            and reviewer_login(r) == login and r.get("commit_id") == head
            and gh.review_state(r) not in NOT_VERDICTS]
    if (not mine or any(when is None for when, _ in mine)
            or any(gh.review_state(r) not in {"APPROVED", "CHANGES_REQUESTED"} for _, r in mine)):
        return []                  # none, or an undatable or unfamiliar verdict: order unproved
    when, newest = max(mine, key=lambda item: item[0])
    if sum(other == when for other, _ in mine) != 1:
        return []
    number = _review_pr(newest, loop["repo"])
    if gh.review_state(newest) != "APPROVED" or number is None:
        return []
    from .run_supervisor import views
    ruled = views.ruling_at(config.host_path("ledger"), loop["repo"], number, head)
    return [newest] if ruled == "ACCEPT" else []


def _review_when(review: dict) -> tuple | None:
    """``(submitted, id)``, a review's place in GitHub's order, or None when it can't be told:
    no or a naive timestamp, or no positive id. Unknown order never counts as earlier."""
    try:
        submitted = datetime.fromisoformat(review["submitted_at"].replace("Z", "+00:00"))
        review_id = int(review["id"])
        if submitted.tzinfo is None or review_id <= 0:
            return None
        return submitted.astimezone(timezone.utc), review_id
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        return None


def _review_pr(review: dict, repo: str) -> int | None:
    """The PR number a review names in its ``pull_request_url``, if it is in ``repo``."""
    import re
    url = str(review.get("pull_request_url") or "")
    match = re.search(r"/repos/([^/]+/[^/]+)/pulls/([1-9][0-9]*)$", url)
    if not match or match.group(1).lower() != str(repo).lower():
        return None
    return int(match.group(2))


def is_ruled_approval(review, loop: dict) -> bool:
    """Whether ``review`` (an effective verdict) is the adjudicator's, not the reviewer's (#905)."""
    return isinstance(review, dict) and not is_reviewer(review, loop)


def blocking_reviews(reviews: list, loop: dict) -> list:
    """The reviews GitHub still counts against the merge (#905): each reviewer's, and the
    adjudicator's, newest approval, change request or dismissal, where that is a change request.
    Only a dismissal or the same login's approval lifts one; the adjudicator's does not. When a
    login's order can't be told (an undatable review), every change request of it blocks."""
    counted = (set(loop["reviewers"]) | {config.adjudicator_login(loop).lower()}) - {""}
    history: dict[str, list] = {}
    for review in reviews:
        if (isinstance(review, dict) and reviewer_login(review) in counted
                and gh.review_state(review) in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}):
            history.setdefault(reviewer_login(review), []).append((_review_when(review), review))
    blocking = []
    for dated in history.values():
        if any(when is None for when, _ in dated):
            # Order unproved: any of this login's change requests may be its newest, so each blocks.
            blocking += [r for _, r in dated if gh.review_state(r) == "CHANGES_REQUESTED"]
            continue
        newest = max(dated, key=lambda item: item[0])[1]
        if gh.review_state(newest) == "CHANGES_REQUESTED":
            blocking.append(newest)
    return blocking


def latest_effective_review_at_head(reviews: list, loop: dict, head: str) -> dict | None:
    """Latest live verdict, or unknown when the same-head chronology cannot be proved.

    REST order is not a verdict. Dismissed reviews and comments do not supersede a verdict;
    an unfamiliar state or undatable verdict cannot authorize a merge instruction. The
    adjudicator's approval on its ACCEPT ruling (``ruled_approvals``, #905) counts too.
    """
    candidates = []
    for review in reviews_at_head(reviews, loop, head) + ruled_approvals(reviews, loop, head):
        state = gh.review_state(review)
        if state in NOT_VERDICTS:
            continue
        if state not in {"APPROVED", "CHANGES_REQUESTED"}:
            return None
        when = _review_when(review)
        if when is None:
            return None
        candidates.append((*when, review))
    if not candidates:
        return None
    latest = max(candidates, key=lambda item: (item[0], item[1]))
    if sum((submitted, review_id) == latest[:2] for submitted, review_id, _ in candidates) != 1:
        return None
    return latest[2]


def approved_at_head(reviews: list, loop: dict, head: str) -> bool:
    return any(is_reviewer(r, loop) and gh.review_state(r) == "APPROVED"
               and r.get("commit_id") == head
               for r in reviews if isinstance(r, dict))


def changes_at_head(reviews: list, loop: dict, head: str) -> list:
    return [r for r in reviews
            if is_reviewer(r, loop) and gh.review_state(r) == "CHANGES_REQUESTED"
            and r.get("commit_id") == head]


def hooks_read(loop: dict) -> tuple[bool | None, str]:
    """``(armed, error)``. ``None`` means the hook list could not be read at all.

    Both seat routes must exist as active repo hooks, or the loop is parked. An unreadable list is
    **not** "paused": a reader without hook read access (a PAT's classic ``repo`` or ``read:repo_hook``; a GitHub App's Webhooks: Read)
    cannot see hooks that may well be active.
    The watchdog reads that as silence, and ``explain`` labels it unknown rather than guessing in
    the other direction — "the loop is paused" is a claim, and it needs the hooks to prove it.

    A loop whose events arrive through a GitHub App's webhook (#1239) has no repo hooks: it is
    armed unless its own pause flag is set (``app_ingress``), which is what its dispatcher reads.
    """
    if config.app_ingress(loop):
        from . import app_ingress
        paused = app_ingress.paused(state_mod.state_for(loop))
        return not paused, ("paused (the App's deliveries for this loop are dropped)"
                            if paused else "")
    hooks, error = gh.hooks_read(loop)      # every page: a full first page is not the whole list
    if error or not isinstance(hooks, list):
        return None, error or "GitHub returned no hook list"
    from .doctor import hook_checks   # arm's and apply's matchers too; imported late (it is big)
    missing = []
    for seat in ("reviewer", "fixer"):
        # The seat's URL is the one `arm` credits (doctor.seat_route_target: the registry route,
        # bound to the seat's profile), judged by the rule doctor and apply use
        # (doctor.exact_hook_url: same origin, exact path, query ignored). A hook whose URL
        # merely *contains* the route name — a trailing slash, another profile — is a 404 at the
        # gateway, and a seat woken only through it is not armed.
        try:
            url = hook_checks.seat_hook_url(loop, loop["seats"][seat]["route"])
        except config.ConfigError:
            url = None
        if not url or not any(isinstance(h, dict) and h.get("active") is True
                              and hook_checks.exact_hook_url(str((h.get("config") or {}).get("url")
                                                                 or ""), url)
                              for h in hooks):
            missing.append(seat)
    return not missing, ", ".join(missing)


def hooks_armed(loop: dict) -> bool | None:
    """Both seat routes present and active: True armed, False paused, None unknown (unreadable).

    None is falsy, so "start nothing unless armed" callers stay fail-closed; the ones that talk to
    an operator must tell the two apart — a paused loop is silent on purpose, a blind one is not.
    """
    armed, _ = hooks_read(loop)
    return armed


# -- breach markers: the predicates the gates, the watchdog and explain share ---


def seat_capacity(loop: dict, st: state_mod.LoopState, seat: str) -> tuple[int, int]:
    """The gate's capacity predicate, with a read-only ledger view for explain."""
    return len(st.live_locks(seat)), config.seat_concurrency(loop, seat)

# A breach marker in one of these states still holds its head for a ruling.
STANDING = ("delivery-pending", "awaiting-adjudication", "adjudicating")


def escalated(marker, head: str) -> bool:
    """Whether the operator escalated this head (#459) and the escalation still stands: the PR is
    then parked for a ruling as if its verdict cap were spent. A new head supersedes it."""
    return (isinstance(marker, dict) and marker.get("head") == head
            and isinstance(marker.get("escalated"), dict) and marker.get("status") in STANDING)


def breach_holds(marker, head: str, spent: int | None, cap: int) -> bool:
    """Whether a breach marker at ``head`` still holds it for a ruling: the live verdict count
    is at the cap, or the operator's escalation stands. The one predicate the watchdog's retry,
    its stall scan and ``explain`` share: a raised cap or a dismissed verdict ends it."""
    return (spent is not None and spent >= cap) or escalated(marker, head)


def breach_delivery_status(marker: dict, head: str) -> str:
    """Only a marker for the live head can park or retry this PR."""
    return str(marker.get("status") or "") if marker.get("head") == head else ""


# -- side effects -------------------------------------------------------------


def drain_seat(loop: dict, seat: str) -> None:
    """Start whatever queued for a seat now that its turn is over. Best effort, never fatal.

    Inside a gate this runs on the gate's clock (#75): the drain gets at most half of what is
    left, and its own GitHub reads are budgeted to that, so a slow drain cannot spend the
    gateway's script timeout. The watchdog's sweep drains whatever this one had to leave.
    """
    left = gh.remaining()
    timeout = 180.0 if left is None else left / 2
    if timeout < 1:
        log(f"drain {seat} deferred to the watchdog — the gate's time budget is nearly spent")
        return
    env = {**os.environ, envnames.name("WATCHDOG_BUDGET_S"): f"{max(0.5, timeout - 0.5):.1f}"}
    try:
        subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve().parents[1]
                                            / "scripts" / "watchdog.py"),
                        "--loop", loop["id"], "--drain", "--seat", seat],
                       capture_output=True, text=True, timeout=timeout, env=env)
    except Exception as exc:
        log(f"drain {seat} failed: {exc}")


def schedule_conflict_scan(loop: dict, st) -> None:
    """A merge into the base: ask for a conflict check shortly (``conflict_scan``) and start the
    detached scanner that waits for it. Best effort; the watchdog's sweep is the fallback."""
    from . import conflict_scan
    try:
        conflict_scan.request(st)
        subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve().parents[1]
                                              / "scripts" / "watchdog.py"),
                          "--loop", loop["id"], "--conflicts"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                         env=dict(os.environ))
    except Exception as exc:
        log(f"conflict scan not scheduled ({type(exc).__name__}: {exc}); the sweep checks")


def reclaim(loop: dict, number: int, state: str) -> None:
    """A finished PR gives back its disk: worktrees, build dirs, locks, counters."""
    log(f"PR #{number} {state} — reclaiming local review artifacts")
    try:
        proc = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve().parents[1]
                                                   / "scripts" / "cleanup.py"),
                               "--loop", loop["id"], "--pr", str(number), "--quiet"],
                              capture_output=True, text=True, timeout=1800)
        for line in (proc.stdout or "").strip().splitlines()[-6:]:
            log(f"cleanup: {line}")
        if proc.returncode != 0:
            log(f"cleanup rc={proc.returncode}: {(proc.stderr or '')[:200]}")
    except Exception as exc:
        log(f"cleanup failed for #{number}: {exc}")


def wake_adjudicator(loop: dict, number: int, head: str, rounds: int, reason: str) -> bool:
    """Hand a stalled PR to an isolated adjudicator turn — never the gateway route.

    The legacy breach route dispatched a credential-owning gateway agent and stays silenced
    (``scripts/gate_adjudicator.py``). Instead this commits an adjudicator turn to the host run
    ledger, keyed by ``breach:<rounds>`` at this head: a redelivered breach dedups on the
    ledger's unique turn index, and a re-breach at a new head is a new turn. The worker
    re-reads every fact before it runs, so the reason here is only a label.

    Returns True only once a turn is durably enqueued with its worker armed, or the ledger's
    existing turn for this key is live or has run (a ``duplicate`` of a claimed, running,
    waiting, uncertain or succeeded turn), which moves the marker to ``awaiting-adjudication``.
    Anything else returns False — including a ``duplicate failed`` / ``duplicate cancelled``
    turn the ledger did not re-arm, where no adjudicator is coming: the marker stays
    ``delivery-pending`` and the watchdog's retry comes back through ``breach()`` — whose
    live-head check under the breach lock keeps a stale head from being re-armed — until
    ``hermes dk retry`` re-arms the dead turn.
    """
    try:
        outcome = enqueue_isolated(loop, "adjudicator", number, head,
                                   turn_key=f"breach:{int(rounds)}")
    except Exception as exc:
        log(f"adjudicator turn for #{number} @ {head[:7]} not enqueued: "
            f"{type(exc).__name__}: {exc}")
        return False
    if outcome == "enqueued":
        log(f"#{number} @ {head[:7]} adjudicator enqueued for isolated worker ({reason})")
    else:
        log(f"#{number} @ {head[:7]} adjudicator {outcome} ({reason})")
    # A duplicate of a live or finished turn is durable (the ledger owns it). A duplicate of a
    # dead one (failed/cancelled and not re-armed) is not: nothing is coming, so don't advance.
    if outcome.startswith("duplicate ") and outcome[len("duplicate "):].split(":")[0].strip() \
            in ("failed", "cancelled"):
        log(f"#{number} @ {head[:7]} adjudicator turn is dead and was not re-armed — marker "
            "stays delivery-pending (`hermes dk retry` re-arms it)")
        return False
    return True


def breach(loop: dict, st: state_mod.LoopState, number: int, head: str, rounds: int,
           reason: str, *, escalated: dict | None = None) -> str:
    """Park the PR at ``head`` for a ruling and wake the adjudicator; the marker's outcome.

    ``escalated`` (#459): the operator asked for a ruling before the cap was spent; the marker
    records who and why, and that is what holds the head instead of a spent cap."""
    entry = {
        "pr": number, "head": head, "rounds": rounds, "cap": loop["cap"],
        "reason": reason, "at": now_iso(), "status": "awaiting-adjudication",
        **({"escalated": dict(escalated)} if escalated else {}),
    }

    def current_head() -> bool:
        # Both gate payloads can arrive late. Recheck GitHub inside the breach
        # lock so a delayed A cannot overwrite an already parked B.
        current = gh.pr(loop, number)
        return (isinstance(current, dict) and current.get("number") == number
                and current.get("state") == "open" and not current.get("draft")
                and (current.get("base") or {}).get("ref") == loop["base"]
                and ((current.get("user") or {}).get("login") or "").lower() in authors
                and (current.get("head") or {}).get("sha") == head)

    # Shadow rulings and explicit operator requests may concern any reviewed PR; automatic
    # cap/dispute rulings remain fixer-only. Review-only authors are never fixed here.
    authors = (config.reviewed_authors(loop)
               if (escalated or {}).get("by") in {SHADOW_ESCALATION, "operator"}
               else set(loop["fixers"]))

    route = loop.get("adjudicator", {}).get("route")
    def observe_reserved(marker: dict) -> None:
        by = str((marker.get("escalated") or {}).get("by") or "operator")
        outcome = ((f"the fixer disputed the review at {marker['rounds']}/{loop['cap']} verdicts"
                    if by == "dispute" else
                    "the shadow review requested changes at an approved head"
                    if by == SHADOW_ESCALATION else
                    f"escalated by the operator at {marker['rounds']}/{loop['cap']} verdicts")
                   if marker.get("escalated") else
                   f"{marker['rounds']}/{loop['cap']} verdicts, no approval")
        observer.notify(loop, st, "escalation", number, head, identity=str(marker["rounds"]),
                        outcome=outcome,
                        next_turn="adjudicator delivery pending" if route else "you")

    outcome = st.breach_deliver(number, entry, current_head,
                                lambda marker: wake_adjudicator(loop, number, head,
                                                                marker["rounds"], marker["reason"])
                                if route else True, reserved=observe_reserved)
    if outcome == "new":
        st.note(f"breach {loop['repo']}#{number} at {head[:7]}: {reason}")
        if not route:
            log("no adjudicator configured — marker written, nothing woken")

    if outcome == "stale":
        log(f"#{number} @ {head[:7]} no longer current — not escalating")
    return outcome



def take_seat(loop: dict, st: state_mod.LoopState, seat: str, number: int, head: str,
              why: str, login: str = "") -> dict | None:
    """Claim a slot for this PR and build its isolated workspace.

    Either the run starts — slot available, own clone ready — or this PR is queued and the gate
    stays silent. The claim happens *before* the workspace is built (a clone can take minutes;
    two events arriving in that window must not both decide the seat is free).

    Returns the workspace, or ``None`` when no sandbox could be built. Above ``concurrency = 1``
    an unisolated run is never started: two runs sharing a checkout produce wrong verdicts, and
    the caller would rather have a queued PR than a wrong one.
    """
    key = seat_key(loop, number)
    capacity = config.seat_concurrency(loop, seat)

    # Check and claim are one step under the loop's state lock: two gates reading "one slot
    # free" and both acquiring would put the seat over its capacity. ``silence()`` raises, and
    # the lock is released on the way out.
    with st.locked():
        if st.is_active(seat, key):
            log(f"{seat} is already running {key} — refusing a second run at the same PR")
            silence(f"{seat} is already running {key}", "held")

        other = st.held_by_other(seat, key)
        if other:
            st.queue_add(seat, key, head, pr_url(loop, number),
                         f"the {other} seat is working this PR")
            log(f"{other} holds #{number} — queued {seat} rather than running both on one PR")
            silence(f"the {other} seat is working this PR — queued until it hands off")

        live = st.active(seat)
        used, limit = seat_capacity(loop, st, seat)
        if used >= limit:
            held = ", ".join(f"{k} ({int(time.time() - v.get('at', time.time()))}s)"
                             for k, v in sorted(live.items()))
            st.queue_add(seat, key, head, pr_url(loop, number),
                         f"{seat} at capacity {len(live)}/{capacity}: {held}")
            log(f"{seat} at capacity {len(live)}/{capacity} ({held}) — queued #{number} @ {head[:7]}")
            silence(f"{seat} at capacity {len(live)}/{capacity} — queued", "queued")

        # The checks above hold the same lock, but the claim is itself a compare-and-swap so a
        # live claim can never be evicted, whatever the checks saw (#727).
        try:
            claimed = st.acquire(seat, key, head, why, exclusive=True)
        except Exception as exc:  # noqa: BLE001 - an unrecorded claim must not start a run (#726)
            silence(f"could not record the {seat} claim on {key} "
                    f"({type(exc).__name__}: {exc}) — not running", "held")
        if not claimed:
            log(f"{seat} claimed {key} while this gate decided — refusing a second run")
            silence(f"{seat} is already running {key}", "held")
        st.queue_pop(seat, key)        # a direct event can outrun the drain: the entry is stale now

    workspace = isolation.ensure(loop, number, seat, head, login=login or "")
    if workspace is None and capacity > 1:
        st.release_if(seat, key)
        st.queue_add(seat, key, head, pr_url(loop, number),
                     "no isolated workspace — a parallel run would share a checkout")
        log(f"no isolated workspace for #{number} and concurrency={capacity} — queued")
        silence("no isolated workspace — queued", "queued")
    if workspace is None:
        log(f"running #{number} without isolation (concurrency=1) under {artifacts_for(loop, number)}")
    return workspace
