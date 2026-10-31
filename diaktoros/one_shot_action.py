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
from typing import cast

from . import config, gate, gh, run_supervisor, state as state_mod, transition
from .run_supervisor import views
from .ledger import read_only
from .util import logged, now_iso

_LOCK = threading.RLock()
_SECRET = secrets.token_bytes(32)
_ACTIONS = ("review", "escalate", "drain", "cancel")
# The seats a confirmed cancel may name (#1148); the other actions take reviewer or fixer only.
_CANCEL_SEATS = ("reviewer", "fixer", "adjudicator", "triage", "issue_fixer", "shadow")


def _identity(loop, action, pr, seat, snapshot):
    raw = json.dumps([loop, action, pr, seat, snapshot], sort_keys=True,
                     separators=(",", ":"), default=str).encode()
    return hmac.new(_SECRET, raw, hashlib.sha256).hexdigest()


def public_plan(proposal):
    """Only fixed categories and validated identifiers cross the Desktop boundary."""
    return {key: proposal[key] for key in
            ("action", "pr", "seat", "eligible", "head", "reason", "confirmation")}


def _ledger_rows(loop, number):
    db = run_supervisor.production_ledger()
    if not db.exists():
        return []
    con = read_only(db)
    try:
        return con.execute("SELECT id,seat,head,state,updated FROM runs WHERE repo=? AND pr=? "
                           "ORDER BY id", (loop["repo"], number)).fetchall()
    finally:
        con.close()


def escalation_blockers(loop, number, rows=None) -> tuple[int, bool]:
    """What keeps ``escalate`` from sending this PR for a ruling now: its non-shadow runs in
    flight on any head, and whether one's outcome is unknown. ``explain`` asks the same before it
    names the command (#1481 review)."""
    rows = _ledger_rows(loop, number) if rows is None else rows
    return (sum(row[1] != "shadow" and row[3] in run_supervisor.ACTIVE for row in rows),
            any(row[3] in ("uncertain", "quarantined") for row in rows))


def plan(loop, action, pr=None, seat=None):
    """Read-only offer; an opaque, process-local confirmation binds the observed facts.

    A host restart invalidates outstanding confirmations. CLI-only grant, reason and
    whole-seat drain semantics are intentionally not expanded into Desktop authority.
    """
    seats = _CANCEL_SEATS if action == "cancel" else ("reviewer", "fixer")
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
        decision = _escalation_decision(loop, pr, live=live)
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
    if type(pr) is not int or pr <= 0 or (seat is not None and seat not in _CANCEL_SEATS):
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
            code = _escalate(loop, operation, emit=lambda *args: None)
        except Exception:
            return _result("unknown", "operation interrupted; inspect state before retrying", head=head)
        try:
            st = state_mod.state_for(loop)
            # Read the exact target without LoopState's unreadable-as-empty fallback.
            marker = _read_marker(st, loop, pr)
            if not _matches_escalation(marker, getattr(operation, "expected_marker", None)):
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


def _run_reviewer(payload, reconsider=None) -> list[str]:
    """The unchanged webhook gate's decision on one payload, as a call (#1046): it owns every
    eligibility rule. Returns the lines it logged.

    Nothing process-wide changes: no stdin or stdout swap, no signal handler or timer, no
    process budget or decision recorder. The GitHub budget, the failed-read flag and the
    decision recorder are this call's (``gh.gate_scope``, ``util.gate_call``), so concurrent
    calls in one host never see each other's. The budget is the webhook gate's own; there is no
    signal backstop, so a hang outside a GitHub read is bounded by those reads' deadline only.
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
    return _capture(_escalate, loop, SimpleNamespace(pr=pr, reason=reason, expected_head=expected_head,
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
    ``started``, ``left`` (still queued), ``dropped`` (no longer a valid request) or
    ``uncertain`` (a POST that may have been sent). Nothing process-wide changes: the GitHub
    budget, log lines and decision recorder are this call's (``gh.gate_scope``,
    ``util.gate_call``), as for the review entry point (#1046).

    Returns ``{"ok", "status", "sent", "outcomes"}``. ``status`` is ``stale``, ``refused``,
    ``nothing queued``, ``nothing to start`` (every entry was dropped as no longer valid),
    ``sent`` (at least one request sent and answered, none left queued), ``partial`` (some
    left queued) or ``unknown`` (an uncertain POST, or the budget ran out). ``sent`` means the
    route accepted the POST, never "started", and is not success (``ok`` is false): nothing
    ties a run in the ledger to one POST, so whether a run started is the run ledger's to say
    (``status``), as for ``hermes dk drain``, until the gate acknowledges the claim (#1417).
    """
    from . import migrate, util
    out = {"ok": False, "status": "refused", "sent": 0, "outcomes": []}
    if not _snapshot_shape(expected):
        # None is the unconfirmed sweep's (#1407 review, F2): never a confirmation.
        out["outcomes"].append({"key": "", "number": None, "outcome": "refused",
                                "why": "no confirmed queue snapshot"})
        return out
    if seat not in ("reviewer", "fixer"):
        out["outcomes"].append({"key": "", "number": None, "outcome": "refused",
                                "why": "unknown drain seat"})
        return out
    if migrate.migrating() is not None:
        out["outcomes"].append({"key": "", "number": None, "outcome": "refused",
                                "why": "a migration holds the install"})
        return out
    armed, _error = gate.hooks_read(loop)
    if not armed:
        out["outcomes"].append({"key": "", "number": None, "outcome": "refused",
                                "why": "hooks are paused or unreadable — nothing drained"})
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


def _snapshot_shape(expected) -> bool:
    """A confirmed queue snapshot's shape: a list of ``[key, head, at]`` (``drain_snapshot``)."""
    return isinstance(expected, list) and all(
        isinstance(row, list) and len(row) == 3 and isinstance(row[0], str)
        and isinstance(row[1], str) and isinstance(row[2], (int, float))
        and not isinstance(row[2], bool) for row in expected)


def _drain(loop, seat):
    """CLI-only whole-seat request: the sweep ran, which is not proof a run finished.

    ``docs/commands.md`` makes this command propagate its script's exit code and says it does not
    wait for completion, so a completed sweep is 0 with the caveat printed — reporting 2 for every
    drain would claim a refusal that did not happen, and 2 is the documented refused-input code.
    A refused loop file (the script calls ``sys.exit(1)``) and a spent GitHub budget stay
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
    print("drain requested; the sweep ran, but queue and run outcomes are unconfirmed — "
          "inspect status")
    return 0


RECONSIDER_MAX = 2000


def _reconsider_refusal(loop, number, pr, head, reason) -> str:
    """Why a maintainer's reconsider (#1334) cannot start here, or ``""``. One per head: a head
    already reconsidered is refused (the ledger's turn key would keep only the first anyway)."""
    from .run_supervisor import RECONSIDER_KEY
    if not isinstance(reason, str) or not reason.strip():
        return "a reconsider needs a reason (what the head must meet)"
    if len(reason) > RECONSIDER_MAX:
        return f"the reason is longer than {RECONSIDER_MAX} characters"
    reviews = gh.reviews(loop, number)
    if not isinstance(reviews, list):
        return "the reviews could not be read"
    if not gate.reviewed_at_head(reviews, loop, head):
        return ("this head has no reviewer's verdict yet — `hermes dk review` (without "
                "--reconsider) asks for one")
    if _reviewer_runs(loop["repo"], number, head, RECONSIDER_KEY):
        return "this head was already reconsidered (one per head) — a new head gets its own review"
    return ""


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
        refusal = _reconsider_refusal(loop, args.pr, pr, head, reconsider)
        if refusal:
            emit(f"#{args.pr} @ {head[:7]}: {refusal}; nothing started")
            return 1
    if getattr(args, "another_round", False):
        # One more verdict on a review-only PR that hit its cap. The operator running this on the
        # host is the maintainer; the grant is recorded against this head and verdict count, so it
        # buys exactly one verdict and a repeat of the command changes nothing.
        author = ((pr.get("user") or {}).get("login") or "").lower()
        if author not in config.review_only(loop):
            emit(f"#{args.pr}: {author or 'its author'} is not review-only — only a review-only "
                  "PR has a review cap to lift; nothing granted")
            return 1
        reviews = gh.reviews(loop, args.pr)
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

    def queued():
        entry = (st.queue_all().get("reviewer") or {}).get(key) or {}
        return entry if entry.get("head") == head else None
    before = (_reviewer_runs(loop["repo"], args.pr, head), queued())
    payload = {"action": "ready_for_review", "number": args.pr, "pull_request": pr,
               "repository": {"full_name": loop["repo"]},
               "sender": {"login": loop.get("read_token") or "operator"}}
    if reconsider is not None:
        # The reason goes with the turn into the reviewer's prompt (#1334); written first.
        st.reconsider_record(args.pr, head, reconsider)
    try:
        gate_lines = _run_reviewer(payload, reconsider=reconsider) if reconsider is not None \
            else _run_reviewer(payload)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            emit(f"#{args.pr} @ {head[:7]}: the gate did not finish — "
                  "no answer; check `explain` before asking again")
            return 2
    except (gh.GateBudgetExceeded, TimeoutError):
        emit(f"#{args.pr} @ {head[:7]}: the gate did not finish — "
              "no answer; check `explain` before asking again")
        return 2
    except Exception as exc:  # noqa: BLE001 - a crashed gate is an answer too, never a traceback
        # The webhook path's gate_failures.run turns a crash into exit 2; in-process the same
        # outcome is reported here (#1046 review F1), named by its type, never its text.
        emit(f"#{args.pr} @ {head[:7]}: the gate did not finish — it crashed "
             f"({type(exc).__name__}); check `explain` before asking again")
        return 2
    reasons = logged(gate_lines if isinstance(gate_lines, list) else [])
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
    waiting = queued()
    if waiting and waiting != before[1]:
        from .run_supervisor import record_operator_action
        record_operator_action(loop["repo"], args.pr, "reconsider" if reconsider is not None
                               else "review")
        reason = waiting.get("reason") or "the reviewer seat is busy"
        emit(f"#{args.pr} @ {head[:7]}: review queued, waiting — {reason}")
        return 0
    reason = reasons[-1] if reasons else "the reviewer gate declined without a reason"
    emit(f"#{args.pr} @ {head[:7]}: no review started — {reason}")
    return 1


def _read_marker(st, loop, number):
    if not st.breach.exists():
        return {}
    data = json.loads(st.breach.read_text())
    if not isinstance(data, dict):
        raise ValueError("invalid marker state")
    marker = data.get(gate.seat_key(loop, number), {})
    if not isinstance(marker, dict):
        raise ValueError("invalid target marker")
    return marker


def _escalation_decision(loop, number, *, live=None):
    """The single read-only selector for both operator interfaces.

    Unknown ledger outcomes and corrupt marker stores now refuse CLI escalation too:
    absence of trustworthy facts is not permission to enqueue another writer.
    """
    details = {"code": 1, "reason": "", "head": "", "confirmation": ""}

    def refuse(reason, code=1, *, public_reason="escalation not eligible; inspect state on the host"):
        # Terminal copy may name operator-configured paths/logins; the page gets categories.
        details.update(code=code, reason=reason, public_reason=public_reason)
        return details

    say = f"#{number}"
    if not str((loop.get("adjudicator") or {}).get("route") or ""):
        return refuse(f"{say}: refused — this loop has no adjudicator; turn it on with "
                      f"`hermes dk set --loop {loop['id']} --adjudicator-profile PROFILE`")
    pr = live if live is not None else gh.pr(loop, number)
    if not isinstance(pr, dict) or pr.get("number") != number:
        return refuse(f"{say}: the PR could not be read from GitHub — nothing escalated", 2)
    head = str((pr.get("head") or {}).get("sha") or "")
    details["head"] = head
    author = str((pr.get("user") or {}).get("login") or "").lower()
    say = f"#{number} @ {head[:7]}"
    if (pr.get("state") != "open" or pr.get("draft") or len(head) != 40
            or any(c not in "0123456789abcdefABCDEF" for c in head)
            or (pr.get("base") or {}).get("ref") != loop["base"]
            or (author not in loop["fixers"] and author not in config.review_only(loop))):
        # #1165: only authors the reviewer reviews, since escalation needs a verdict at the head.
        # A maintainer's PR gets none unless they are review-only too (then it is eligible here).
        eligible = ", ".join([*loop["fixers"], *sorted(config.review_only(loop))])
        return refuse(f"{say}: refused — only an open, ready PR by a fixer or review-only author "
                      f"({eligible}) on {loop['base']} can be escalated")
    st = state_mod.state_for(loop)
    reviews = transition.effective_reviews(loop, st, number, head, gh.reviews(loop, number))
    if not isinstance(reviews, list):
        return refuse(f"{say}: the reviews could not be read — nothing escalated", 2)
    latest = gate.latest_effective_review_at_head(reviews, loop, head)
    if latest is not None and gh.review_state(latest) == "APPROVED":
        return refuse(f"{say}: refused — approved at this head; there is nothing to rule on",
                      public_reason="approved at this head; nothing to rule on")
    verdicts = len(gate.verdicts(reviews, loop))
    if verdicts < 1:
        return refuse(f"{say}: refused — no verdict yet; there is nothing to rule on")
    marker = _read_marker(st, loop, number)
    if marker.get("head") == head and marker.get("status") in gate.STANDING:
        return refuse(f"{say}: already awaiting a ruling ({marker.get('status')})")
    rows = _ledger_rows(loop, number)
    busy, unknown = escalation_blockers(loop, number, rows)
    if busy:
        # #1246: the host CLI records it for the next changes-requested verdict instead; Desktop
        # binds a confirmation to the marker it writes now, so it still refuses.
        # Pending only for a fixer's PR: with fixer pushes on its turns leave no gap. A review-only
        # or maintainer PR has no fixer turn, so it is free once this run ends (#1154 + #1246).
        details.update(busy=busy, state=st, fixer_pr=author in loop["fixers"])
        return refuse(f"{say}: refused — {busy} run(s) for this PR are in flight; escalate once "
                      "they finish (a queued review or fix is retired by the escalation)",
                      public_reason="a run is in flight; the host CLI can queue the escalation "
                                    "for the next changes-requested verdict")
    if unknown:
        return refuse(f"{say}: run outcome unknown; reconcile before escalation")
    details.update(code=0, public_reason="", state=st, verdicts=verdicts,
                   confirmation=_identity(loop, "escalate", number, None, [pr, reviews, marker, rows]))
    return details


class _ConfirmedEscalationState:
    """Bind reservation to the selected facts inside the existing breach lock.

    Never replace the chosen head/count/config with newly observed facts. The gate's
    own callback still runs first; the shared selector may only veto its reservation.
    """
    def __init__(self, st, loop, number, confirmation):
        self._state, self._loop = st, loop
        self._number, self._confirmation = number, confirmation

    def __getattr__(self, name):
        return getattr(self._state, name)

    def breach_deliver(self, number, entry, current, send, reserved=None):
        def unchanged():
            if not current():
                return False
            try:
                # Reload rather than trust a request's cached loop after a host edit.
                loop = config.load_id(self._loop["id"])
                decision = _escalation_decision(loop, self._number)
                return (decision["code"] == 0 and
                        hmac.compare_digest(decision["confirmation"], self._confirmation))
            except Exception:
                return False
        return self._state.breach_deliver(number, entry, unchanged, send, reserved=reserved)


def _matches_escalation(marker, expected):
    return (isinstance(expected, dict) and gate.escalated(marker, expected["head"])
            and all(marker.get(key) == value for key, value in expected.items()))


def _escalate(loop, args, emit=print):
    number = args.pr
    from . import escalate_pending
    if getattr(args, "withdraw", False):
        try:
            withdrawn = escalate_pending.take(state_mod.state_for(loop), number)
        except Exception as exc:                  # strict (#1262): say so, never a traceback
            emit(f"#{number}: the pending escalation could not be withdrawn ({type(exc).__name__}: "
                 f"{exc}) — it still stands; retry")
            return 2
        emit(f"#{number}: pending escalation withdrawn "
             f"({withdrawn.get('reason') or 'no reason recorded'})" if withdrawn   # #1263
             else f"#{number}: no pending escalation to withdraw")
        return 0
    try:
        decision = _escalation_decision(loop, number)
    except Exception:
        emit(f"#{number}: escalation facts unavailable — nothing escalated")
        return 2
    if (decision["code"] and decision.get("busy") and decision.get("fixer_pr")
            and getattr(args, "expected_confirmation", None) is None):
        # #1246: runs are in flight, and with fixer pushes on there is never a gap between them.
        # Recorded for the next changes-requested verdict (the fixer gate writes the breach then;
        # an approval drops it). Never a Desktop confirmation: that one binds a marker written now.
        why = " ".join(str(args.reason or "").split())[:200] or "the operator asked for a ruling"
        try:
            escalate_pending.record(decision["state"], number, reason=why, head=decision["head"])
        except Exception as exc:
            emit(f"#{number}: the pending escalation could not be recorded ({type(exc).__name__}: "
                 f"{exc}) — nothing escalated")                                       # #1262
            return 2
        emit(f"#{number} @ {decision['head'][:7]}: {decision['busy']} run(s) in flight — escalation "
             "pending: the next changes-requested verdict goes to the adjudicator instead of the "
             "fixer; an approval drops it (it lapses in 24h; `hermes dk escalate --loop "
             f"{loop['id']} --pr {number} --withdraw` cancels it)")
        return 0
    if decision["code"]:
        emit(decision["reason"])
        return decision["code"]
    head, verdicts, st = decision["head"], decision["verdicts"], decision["state"]
    say = f"#{number} @ {head[:7]}"
    expected = getattr(args, "expected_confirmation", None)
    if (getattr(args, "expected_head", None) not in (None, head) or
            (expected is not None and not hmac.compare_digest(expected, decision["confirmation"]))):
        emit("refused: confirmed escalation facts changed")
        return 1
    if expected is not None:
        st = _ConfirmedEscalationState(st, loop, number, expected)
    why = " ".join(str(args.reason or "").split())[:200] or "the operator asked for a ruling"
    escalation = {"by": "operator", "at": now_iso(), "reason": why,
                  "operation_id": secrets.token_hex(16)}
    args.expected_marker = {"pr": number, "head": head, "rounds": verdicts, "cap": loop["cap"],
                            "reason": f"escalated: {why}", "escalated": escalation}
    outcome = gate.breach(loop, cast(state_mod.LoopState, st), number, head, verdicts, f"escalated: {why}",
                          escalated=escalation)
    if outcome == "stale":
        emit(f"{say}: the PR moved while escalating — nothing escalated")
        return 1
    try:
        marker = _read_marker(st, loop, number)
    except Exception:
        marker = {}
    if not _matches_escalation(marker, args.expected_marker):
        emit(f"{say}: escalation outcome unknown — inspect before retrying")
        return 2
    if marker.get("status") == "delivery-pending":
        emit(f"{say}: head parked; adjudicator delivery pending — not confirmed enqueued")
        return 2
    if outcome == "new":
        from .run_supervisor import record_operator_action
        record_operator_action(loop["repo"], number, "escalate")
    try:
        escalate_pending.take(state_mod.state_for(loop), number)   # this escalation supersedes it
    except Exception as exc:
        # The head is parked (above); only the stale pending record could not be cleared. Say both,
        # rather than a traceback that hides that the escalation landed (shadow on #1265).
        emit(f"{say}: escalated at {verdicts}/{loop['cap']} verdicts, but its pending record could "
             f"not be cleared ({type(exc).__name__}: {exc}); `hermes dk escalate --loop "
             f"{loop['id']} --pr {number} --withdraw` clears it")
        return 2
    emit(f"{say}: escalated at {verdicts}/{loop['cap']} verdicts — adjudicator turn "
          f"{'enqueued' if outcome in ('new', 'retry') else outcome}; the PR is parked at this "
          f"head (`hermes dk explain --loop {loop['id']} --pr {number}` follows it)")
    return 0
