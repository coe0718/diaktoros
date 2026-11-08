"""Host-owned one-shot actions shared by CLI and Desktop.

Desktop confirmations never retarget a newer head or queue generation. The reviewer gate
runs as a call bound to a confirmed head (#1046: ``review``), and a drain binds a confirmed
queue snapshot (#1047: ``drain_confirmed``). The page's confirmed controls for both are a
follow-up, so Desktop still refuses them rather than interpreting a confirmation as
open-ended consent.
The CLI retains its explicit operator semantics (including grants and seat selection).
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import hmac
import importlib.util
import io
import json
import pathlib
import secrets
import sys
import threading
import time
from types import SimpleNamespace

from . import cancel_action, escalate_action, gate, gh, review_asks, run_supervisor, state as state_mod
from .run_supervisor import views
from .ledger import read_only
from .util import logged

_LOCK = threading.RLock()
_SECRET = secrets.token_bytes(32)
_ACTIONS = ("review", "escalate", "drain", "cancel")
# The seats a confirmed cancel may name (#1148); the other actions take reviewer or fixer only.
# The cancel command's own list, never a copy: the Desktop route's shape check reads this same
# constant, so the route and the plan cannot disagree about a seat (#1371).
CANCEL_SEATS = cancel_action.SEATS


def _identity(loop, action, pr, seat, snapshot):
    raw = json.dumps([loop, action, pr, seat, snapshot], sort_keys=True,
                     separators=(",", ":"), default=str).encode()
    return hmac.new(_SECRET, raw, hashlib.sha256).hexdigest()


def public_plan(proposal):
    """Only fixed categories and validated identifiers cross the Desktop boundary."""
    return {key: proposal[key] for key in
            ("action", "pr", "seat", "eligible", "head", "reason", "confirmation")}


def plan(loop, action, pr=None, seat=None):
    """Read-only offer; an opaque, process-local confirmation binds the observed facts.

    A host restart invalidates outstanding confirmations. CLI-only grant, reason and
    whole-seat drain semantics are intentionally not expanded into Desktop authority.
    """
    seats = CANCEL_SEATS if action == "cancel" else ("reviewer", "fixer")
    out = {"action": action if action in _ACTIONS else "unknown", "pr": None,
           "seat": seat if seat in seats else None, "eligible": False,
           "head": "", "reason": "invalid action target", "confirmation": ""}
    if action not in _ACTIONS:
        return out
    if action == "cancel":
        return _cancel_plan(loop, pr, seat, out)
    if action == "drain":
        if pr is None and seat in ("reviewer", "fixer"):
            # The core binds a confirmed queue snapshot now (#1047: ``drain_confirmed``); the
            # page's confirmed control is the follow-up, so the offer stays refused here.
            out["reason"] = ("the Desktop drain control is not wired yet (its confirmed "
                             "snapshot is the seat's queue); use the host CLI")
        return out
    if type(pr) is not int or pr <= 0 or seat is not None:
        return out
    out["pr"] = pr
    try:
        live = gh.pr(loop, pr)
        if not isinstance(live, dict) or live.get("number") != pr:
            out["reason"] = "PR facts unavailable; nothing authorized"
            return out
        head = (live.get("head") or {}).get("sha")
        if not isinstance(head, str) or len(head) != 40 or any(c not in "0123456789abcdefABCDEF" for c in head):
            out["reason"] = "PR head unavailable; nothing authorized"
            return out
        out["head"] = head
        if action == "review":
            # The core entry point binds a confirmed head now (#1046: ``review(expected_head=)``);
            # the page's confirmed control is the follow-up, so the offer stays refused here.
            out["reason"] = ("the Desktop review control is not wired yet (its confirmed snapshot "
                             "is the head); use the host CLI")
            return out
        decision = escalate_action._escalation_decision(loop, pr, live=live)
        out["reason"] = decision["public_reason"]
        if decision["code"]:
            return out
        out.update(eligible=True, reason="parks this head and requests an adjudicator ruling",
                   confirmation=decision["confirmation"])
        return out
    except Exception:
        out["reason"] = "action facts unavailable; nothing authorized"
        return out


def _cancel_found(loop, pr, seat):
    """``cancel_action.plan`` for a PR (#1148), at the head GitHub reports now."""
    from . import cancel_action
    db = run_supervisor.production_ledger()
    if not db.exists():
        return None
    con = views.read_only_connect(db)
    if con is None:
        return None
    try:
        def head():
            live = gh.pr(loop, pr)
            return ((live or {}).get("head") or {}).get("sha") if isinstance(live, dict) else None
        return cancel_action.plan(con, loop["repo"], pr, seat, resolve_head=head)
    finally:
        con.close()


def _cancel_plan(loop, pr, seat, out):
    """The confirmed cancel's offer: the exact runs it would stop, bound into the confirmation.
    A run that starts, ends or appears after the preview makes the confirmation stale."""
    from . import cancel_action
    if type(pr) is not int or pr <= 0 or (seat is not None and seat not in CANCEL_SEATS):
        return out
    out["pr"] = pr
    try:
        found = _cancel_found(loop, pr, seat)
    except Exception:
        out["reason"] = "run facts unavailable; nothing authorized"
        return out
    if found is None:
        out["reason"] = "no run ledger; nothing to cancel"
        return out
    out["head"] = found["head"]
    if not found["targets"]:
        out["reason"] = "nothing open to cancel"
        return out
    shown = cancel_action.snapshot(found)
    out.update(eligible=True,
               reason="cancels " + ", ".join(f"{row['seat']} {row['state']}"
                                             for row in found["targets"])
               + "; only a retry re-arms them",
               confirmation=_identity(loop["id"], "cancel", pr, seat, [found["head"], shown]),
               snapshot=shown)
    return out


def _cancel_perform(loop, pr, seat, proposal):
    from . import cancel_action
    head = proposal["head"]
    try:
        result = cancel_action.perform(loop, pr=pr, seat=seat,
                                       ledger=run_supervisor.production_ledger(),
                                       resolve_head=lambda: head, expected=proposal["snapshot"])
    except Exception:
        return _result("unknown", "operation interrupted; inspect state before retrying", head=head)
    if result["message"]:
        return _result("stale", "confirmation is stale or unavailable; preview again", head=head)
    states = [entry["state"] for entry in result["cancelled"]]
    if result["refused"] or not states:
        return _result("unknown", "not every run could be cancelled; inspect before retrying",
                       head=head)
    if all(state == "cancelled" for state in states):
        return _result("verified", "cancelled; only a retry re-arms them", ok=True, head=head)
    if "uncertain" in states:
        # Quarantined, not stopping: it may already have written, so it is never replayed (#1376).
        also = "; stop requested for a running turn" if "stopping" in states else ""
        return _result("unknown", "stopped, but it may have written; inspect and reconcile" + also,
                       head=head)
    return _result("partial", "stop requested; the worker stops the running turn", head=head)


def _result(status, reason, *, ok=False, head=""):
    return {"ok": ok, "status": status, "reason": reason, "head": head}


def confirmed_perform(loop, action, confirmation, pr=None, seat=None):
    """Revalidate the exact offer, invoke the head-bound core, then read back that target.

    No worker launch, CLI exit code or gate return value alone proves success.
    """
    with _LOCK:
        loop = copy.deepcopy(loop)
        proposal = plan(loop, action, pr=pr, seat=seat)
        token = proposal["confirmation"]
        if (not isinstance(confirmation, str) or len(confirmation) != 64 or
                not token or not hmac.compare_digest(confirmation, token)):
            return _result("stale", "confirmation is stale or unavailable; preview again")
        if not proposal["eligible"]:
            return _result("refused", proposal["reason"])
        if action == "cancel":
            return _cancel_perform(loop, pr, seat, proposal)
        head = proposal["head"]
        operation = SimpleNamespace(pr=pr, reason="", expected_head=head, expected_confirmation=token)
        try:
            code = escalate_action._escalate(loop, operation, emit=lambda *args: None)
        except Exception:
            return _result("unknown", "operation interrupted; inspect state before retrying", head=head)
        try:
            st = state_mod.state_for(loop)
            # Read the exact target without LoopState's unreadable-as-empty fallback.
            marker = escalate_action._read_marker(st, loop, pr)
            if not escalate_action._matches_escalation(marker, getattr(operation, "expected_marker", None)):
                return _result("unknown", "escalation could not be verified; inspect before retrying", head=head)
            if marker.get("status") == "delivery-pending":
                return _result("partial", "head parked; adjudicator delivery pending", head=head)
            if code != 0:
                return _result("unknown", "state changed but operation did not complete; inspect before retrying", head=head)
            return _result("verified", "head parked awaiting adjudication; no ruling claimed", ok=True, head=head)
        except Exception:
            return _result("unknown", "read-back unavailable; inspect state before retrying", head=head)


def _script(name):
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"diaktoros_one_shot_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("host action entry point unavailable")
    module = importlib.util.module_from_spec(spec)
    path_before = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = path_before
    return module


def _run_reviewer(payload, reconsider=None, holds=None) -> list[str]:
    """The unchanged webhook gate's decision on one payload, as a call (#1046): it owns every
    eligibility rule. Returns the lines it logged. ``holds``, when given, gets the turns this
    call's gate held instead of queuing (``util.note_gate_hold``, #1369), each with the queue row
    it wrote itself, so the caller never mistakes another event's row for its own.

    Nothing process-wide changes: no stdin or stdout swap, no signal handler or timer, no
    process budget or decision recorder. The GitHub budget, the failed-read flag and the
    decision recorder are this call's (``gh.gate_scope``, ``util.gate_call``), so concurrent
    calls in one host never see each other's. The budget is the webhook gate's own; there is no
    signal backstop: only GitHub calls are deadline-limited, and a hang outside one has no bound.
    Raises ``gh.GateBudgetExceeded`` when the budget runs out, and ``SystemExit`` with a
    non-zero code when the gate refused its input.
    """
    from . import gate_failures, util
    try:
        limit, _rows = gate_failures.effective_timeout()
    except Exception:  # noqa: BLE001 - never let the fit check stop the gate
        limit = None
    budget, _backstop = gate_failures.plan(limit or gate_failures.GATEWAY_DEFAULT_TIMEOUT_S)
    with _LOCK:        # loading the script briefly extends sys.path; never two at once
        decide = _script("gate_reviewer").decide
    with util.gate_call() as call, gh.gate_scope(time.monotonic() + budget):
        try:
            decide(payload, reconsider=reconsider)
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise
        finally:
            if holds is not None:
                holds.extend(call["holds"])
    return list(call["logs"])


def _capture(operation, *args):
    with _LOCK:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = operation(*args)
        return {"ok": code == 0, "code": code, "lines": output.getvalue().splitlines()}


def perform_review(loop, pr, another_round=False, reconsider=None):
    """CLI semantics, without a CLI subprocess; returns printable lines and exit code."""
    return review(loop, pr, another_round=another_round, reconsider=reconsider)


def review(loop, pr, *, expected_head=None, another_round=False, reconsider=None):
    """Ask the reviewer gate for a review of ``pr`` (#1046): the one entry point for the CLI's
    ``hermes dk review`` and the Desktop's confirmed review.

    Everything it needs is an argument, and it returns its outcome: ``{"ok", "code", "lines",
    "head"}``. Nothing process-wide is changed (``_run_reviewer``), so two calls may run at
    once. ``expected_head`` (a Desktop confirmation) is the head the operator was shown: if the
    PR is at another head now, nothing is asked and nothing is written. ``reconsider`` (#1334)
    is a maintainer's reason for one fresh review at a head that already has a verdict.
    """
    lines = []
    args = SimpleNamespace(pr=pr, another_round=another_round, reconsider=reconsider,
                           expected_head=expected_head)
    code = _review(loop, args, emit=lines.append)
    head = args.__dict__.get("head", "")
    return {"ok": code == 0, "code": code, "lines": lines, "head": head}


def perform_escalate(loop, pr, reason="", *, expected_head=None, withdraw=False):
    return _capture(escalate_action._escalate, loop, SimpleNamespace(pr=pr, reason=reason, expected_head=expected_head,
                                                     withdraw=withdraw))


def perform_drain(loop, seat="reviewer"):
    return _capture(_drain, loop, seat)


def drain_snapshot(loop, seat):
    """The queue a confirmed drain binds (#1047): ``state.queue_snapshot`` of the seat's queue."""
    return state_mod.queue_snapshot(state_mod.state_for(loop), seat)


def drain_confirmed(loop, seat, expected):
    """Drain exactly the queue the operator confirmed (#1047), and say what happened per run.

    ``expected`` is the ``drain_snapshot`` that was shown. If the seat's live queue is not that
    queue, nothing is started, dropped or pruned (``stale``). Otherwise the watchdog's own
    per-entry checks run, up to the seat's free slots, and each queued entry is reported:
    ``sent`` (the route accepted the POST; never "started"), ``left`` (still queued),
    ``dropped`` (no longer a valid request) or ``uncertain`` (a POST that may have been sent).
    Nothing process-wide changes: the GitHub budget, log lines and decision recorder are this
    call's (``gh.gate_scope``, ``util.gate_call``), as for the review entry point (#1046).

    Returns ``{"ok", "status", "sent", "outcomes"}``. ``status`` is ``stale``, ``refused``,
    ``nothing queued``, ``nothing to start`` (every entry was dropped as no longer valid),
    ``sent`` (at least one request sent and answered, none left queued), ``partial`` (some
    left queued) or ``unknown`` (an uncertain POST, or the budget ran out). ``sent`` means the
    route accepted the POST, never "started", and is not success (``ok`` is false): nothing
    ties a run in the ledger to one POST, so whether a run started is the run ledger's to say
    (``status``), as for ``hermes dk drain``, until the gate acknowledges the claim (#1417).
    """
    from . import util
    out = {"ok": False, "status": "refused", "sent": 0, "outcomes": []}
    why = _drain_refusal(loop, seat, expected)
    if why:
        out["outcomes"].append({"key": "", "number": None, "outcome": "refused", "why": why})
        return out
    with _LOCK:        # loading the script briefly extends sys.path; never two at once
        watchdog = _script("watchdog")
    report = []
    budget = watchdog.WATCHDOG_BUDGET_S
    try:
        with util.gate_call(), gh.gate_scope(time.monotonic() + budget,
                                             per_call=min(watchdog.WATCHDOG_PER_CALL_S, budget)):
            sent = watchdog.drain(loop, state_mod.state_for(loop), seat, quiet=True,
                                     expected=expected, report=report)
    except gh.GateBudgetExceeded:
        # Starts already made stand; every confirmed entry not reached is left (F3).
        seen = {entry["key"] for entry in report}
        from .drain import _queue_number
        report += [{"key": key, "number": _queue_number(key), "outcome": "left",
                    "why": "the GitHub budget ran out before it was reached"}
                   for key, *_rest in expected if key not in seen]
        out.update(status="unknown", sent=sum(e["outcome"] == "sent" for e in report),
                   outcomes=report)
        return out
    outcomes = {entry["outcome"] for entry in report}
    if "stale" in outcomes:
        status = "stale"
    elif not report:
        status = "nothing queued"
    elif "uncertain" in outcomes:
        status = "unknown"
    elif "left" in outcomes:
        status = "partial"
    elif not sent:
        status = "nothing to start"     # every entry was dropped as no longer a valid request
    else:
        status = "sent"
    # ``sent`` is not success (#1047's amended "Done when"): the route accepted the POST, and
    # whether a run started is unconfirmed until the gate acknowledges the claim (#1417).
    out.update(ok=status in ("nothing queued", "nothing to start"), status=status,
               sent=sent,
               outcomes=report)
    return out


def _drain_refusal(loop, seat, expected) -> str:
    """Why a confirmed drain starts nothing, or "": checked in this order, each only once the one
    before it passed, so a refused request never reads the hooks."""
    from . import migrate
    if not _snapshot_shape(expected):
        # None is the unconfirmed sweep's (#1407 review, F2): never a confirmation.
        return "no confirmed queue snapshot"
    if seat not in ("reviewer", "fixer"):
        return "unknown drain seat"
    if migrate.migrating() is not None:
        return "a migration holds the install"
    armed, _error = gate.hooks_read(loop)
    return "" if armed else "hooks are paused or unreadable — nothing drained"


def _snapshot_shape(expected) -> bool:
    """A confirmed queue snapshot's shape: a list of ``[key, head, at]`` (``drain_snapshot``)."""
    return isinstance(expected, list) and all(
        isinstance(row, list) and len(row) == 3 and isinstance(row[0], str)
        and isinstance(row[1], str) and isinstance(row[2], (int, float))
        and not isinstance(row[2], bool) for row in expected)


def _drain(loop, seat):
    """CLI-only whole-seat request: the watchdog returned, which is not proof a run finished.

    ``docs/commands.md`` makes this command propagate its script's exit code and says it does not
    wait for completion, so a watchdog that returned is 0 with the caveat printed — reporting 2
    for every drain would claim a refusal that did not happen, and 2 is the documented
    refused-input code. 0 does not establish that the sweep covered the queue: the script
    returns normally when a migration holds the install, when a loop's hooks are paused or
    unreadable, and when its own budget runs out (it prints that line, then returns). A refused
    loop file (the script calls ``sys.exit(1)``) and a budget that escapes the script stay
    non-zero. Desktop refuses this operation before launch for the same reason.
    """
    if seat not in ("reviewer", "fixer"):
        print("refused: unknown drain seat")
        return 2
    watchdog = _script("watchdog")
    budget = dict(gh._GATE)
    gh.begin_gate(time.monotonic() + watchdog.WATCHDOG_BUDGET_S,
                  per_call=watchdog.WATCHDOG_PER_CALL_S)
    try:
        watchdog.run(SimpleNamespace(loop=loop["id"], drain=True, seat=seat, conflicts=False),
                     watchdog.WATCHDOG_BUDGET_S)
    except gh.GateBudgetExceeded:
        print("drain did not finish; inspect the queue before retrying")
        return 2
    except SystemExit as exc:
        # The script refuses a bad loop with sys.exit(1); in-process that is an exception, not
        # an exit, and letting it escape would unwind out of the caller's action.
        code = exc.code if isinstance(exc.code, int) else 1
        print(f"drain stopped: {code}; inspect the queue before retrying")
        return code or 1
    finally:
        gh._GATE.clear()
        gh._GATE.update(budget)
    print("drain requested; the watchdog returned, but it may have stopped early (see any line "
          "above) and queue and run outcomes are unconfirmed — inspect status")
    return 0


RECONSIDER_MAX = 2000


def _reconsider_refusal(loop, st, number, pr, head, reason) -> str:
    """Why a maintainer's reconsider (#1334) cannot start here, or ``""``. One per head: a head
    already reconsidered is refused (the ledger's turn key would keep only the first anyway).
    "Has a verdict" is read from the gate's own view (``review_asks.gate_reviews``, #1370)."""
    from .run_supervisor import RECONSIDER_KEY
    if not isinstance(reason, str) or not reason.strip():
        return "a reconsider needs a reason (what the head must meet)"
    if len(reason) > RECONSIDER_MAX:
        return f"the reason is longer than {RECONSIDER_MAX} characters"
    reviews = review_asks.gate_reviews(loop, st, number, head)
    if not isinstance(reviews, list):
        return "the reviews could not be read"
    if not gate.reviewed_at_head(reviews, loop, head):
        return ("this head has no reviewer's verdict yet — `hermes dk review` (without "
                "--reconsider) asks for one")
    if _reviewer_runs(loop["repo"], number, head, RECONSIDER_KEY):
        return "this head was already reconsidered (one per head) — a new head gets its own review"
    return review_asks.reconsider_cap_refusal(loop, st, number, pr, head, reviews)


def _reviewer_runs(repo: str, number: int, head: str, turn_key: str | None = None) -> int:
    """How many reviewer runs the ledger holds for this head (read-only; 0 without a ledger)."""
    from . import run_supervisor
    db = run_supervisor.production_ledger()
    if not db.exists():
        return 0
    con = read_only(db)
    try:
        if turn_key is not None:
            return con.execute("SELECT COUNT(*) FROM runs WHERE repo=? AND pr=? AND head=? AND "
                               "seat='reviewer' AND turn_key=?",
                               (repo, number, head, turn_key)).fetchone()[0]
        return con.execute("SELECT COUNT(*) FROM runs WHERE repo=? AND pr=? AND head=? AND "
                           "seat='reviewer'", (repo, number, head)).fetchone()[0]
    finally:
        con.close()


def _gate_unfinished(emit, args, payload, exc, started, what):
    """The gate crashed, refused its input or ran out of budget in-process: record it in the
    gate-failure ledger, as the webhook path's ``gate_failures.run`` does (#1455), and say so
    (#1046 review F1: named by its type, never its text). Returns the exit code."""
    from . import gate_failures
    where = gate_failures.record_in_process("gate_reviewer", payload, exc, started)
    saved = ("recorded in the gate-failure ledger" if where
             else "could NOT be recorded in the gate-failure ledger")
    emit(f"#{args.pr} @ {args.head[:7]}: the gate did not finish — {what}; {saved}, "
         "check `explain` before asking again")
    return 2


def _review(loop, args, emit=print):
    from . import migrate
    if migrate.migrating() is not None:
        # A migration holds the install (#431): no gate acts under names being moved.
        emit(f"#{args.pr}: a migration holds the install — nothing started; ask again after it")
        return 2
    pr = gh.pr(loop, args.pr)
    if not isinstance(pr, dict) or pr.get("number") != args.pr:
        emit(f"#{args.pr}: the PR could not be read from GitHub — nothing started")
        return 2
    head = args.head = (pr.get("head") or {}).get("sha") or ""
    expected = str(getattr(args, "expected_head", None) or "").strip().lower()
    if expected and head.lower() != expected:
        # The confirmation was for another head (#1046): never ask for a review of this one.
        emit(f"#{args.pr}: refused: the PR is at {head[:7]} now, not the {expected[:7]} you "
             "confirmed — nothing started; preview again")
        return 2
    st = state_mod.state_for(loop)
    key = gate.seat_key(loop, args.pr)
    reconsider = getattr(args, "reconsider", None)
    if reconsider is not None:
        refusal = _reconsider_refusal(loop, st, args.pr, pr, head, reconsider)
        if refusal:
            emit(f"#{args.pr} @ {head[:7]}: {refusal}; nothing started")
            return 1
    if getattr(args, "another_round", False):
        refused = review_asks.grant_another_round(loop, args, pr, head, st, emit)
        if refused is not None:
            return refused

    def queued():
        entry = (st.queue_all().get("reviewer") or {}).get(key) or {}
        return entry if entry.get("head") == head else None
    before = (_reviewer_runs(loop["repo"], args.pr, head), queued())
    payload = {"action": "ready_for_review", "number": args.pr, "pull_request": pr,
               "repository": {"full_name": loop["repo"]},
               "sender": {"login": loop.get("read_token") or "operator"}}
    if reconsider is None:
        return _ask_gate(loop, args, st, key, head, before, queued, payload, emit)
    return review_asks.reconsider_ask(
        st, args.pr, head, reconsider,
        lambda: _ask_gate(loop, args, st, key, head, before, queued, payload, emit),
        lambda: _reviewer_runs(loop["repo"], args.pr, head, run_supervisor.RECONSIDER_KEY))


def _ask_gate(loop, args, st, key, head, before, queued, payload, emit) -> int:
    """``_review``'s gate step: run the reviewer gate on ``payload`` and say what it did (the
    exit code). A gate that crashed, refused its input or ran out of budget is reported by
    ``_gate_unfinished``; any other answer by ``_review_outcome``."""
    reconsider = getattr(args, "reconsider", None)
    started = time.monotonic()
    holds = []          # the turns the gate held instead of queuing, with its own rows (#1369)
    try:
        gate_lines = _run_reviewer(payload, reconsider=reconsider, holds=holds) \
            if reconsider is not None else _run_reviewer(payload)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            return _gate_unfinished(emit, args, payload, exc, started, "no answer")
        gate_lines = []                 # a clean exit: the gate finished, with no lines to show
    except (gh.GateBudgetExceeded, TimeoutError) as exc:
        return _gate_unfinished(emit, args, payload, exc, started, "no answer")
    except Exception as exc:  # noqa: BLE001 - a crashed gate is an answer too, never a traceback
        return _gate_unfinished(emit, args, payload, exc, started,
                                f"it crashed ({type(exc).__name__})")
    from . import gate_failures
    gate_failures.resolve_in_process("gate_reviewer", payload, started)
    reasons = logged(gate_lines if isinstance(gate_lines, list) else [])
    return _review_outcome(loop, args, st, key, head, before, queued, holds, reasons, emit)


def _review_outcome(loop, args, st, key, head, before, queued, holds, reasons, emit) -> int:
    """What ``_review`` says once the gate has answered, and its exit code: a turn the ledger
    committed, a reconsider the gate held instead (#1369: only the row this call's gate wrote,
    from ``holds``, is ever touched), a plain review waiting in the seat queue, or a decline.
    ``before`` is the ledger count and queue row read before the gate ran."""
    reconsider = getattr(args, "reconsider", None)
    committed = _reviewer_runs(loop["repo"], args.pr, head) > before[0]
    if committed and reasons and "turn held:" in reasons[-1]:
        emit(f"#{args.pr} @ {head[:7]}: review not verified — {reasons[-1]}")
        return 2
    if committed:
        from .run_supervisor import record_operator_action
        record_operator_action(loop["repo"], args.pr, "reconsider" if reconsider is not None
                               else "review")
        emit(f"#{args.pr} @ {head[:7]}: review queued for the isolated worker "
              f"(`hermes dk explain --loop {loop['id']} --pr {args.pr}` follows it)")
        return 0
    held = [h for h in holds if h.get("seat") == "reviewer" and h.get("key") == key]
    if reconsider is not None and held:          # held, so never replayed (#1369)
        return review_asks.reconsider_held(emit, st, held[-1], args, head, loop)
    waiting = queued()
    if reconsider is None and waiting and waiting != before[1]:
        from .run_supervisor import record_operator_action
        record_operator_action(loop["repo"], args.pr, "review")
        reason = waiting.get("reason") or "the reviewer seat is busy"
        emit(f"#{args.pr} @ {head[:7]}: review queued, waiting — {reason}")
        return 0
    reason = reasons[-1] if reasons else "the reviewer gate declined without a reason"
    emit(f"#{args.pr} @ {head[:7]}: no review started — {reason}")
    return 1
