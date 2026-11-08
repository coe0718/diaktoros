"""The operator's escalation (``hermes dk escalate`` and the Desktop's confirmed escalate): the
read-only selector both interfaces share, and the operation that parks a head for a ruling.

Moved from ``one_shot_action`` (which still owns the plan/confirm entry points and calls these)
to keep that file under the size ratchet (#1349).
"""
from __future__ import annotations

import hmac
import json
import secrets
from typing import cast

from . import config, gate, gh, run_supervisor, state as state_mod, transition
from .ledger import read_only
from .util import now_iso


def _identity(loop, action, pr, seat, snapshot):
    """``one_shot_action._identity``: one process-local secret signs every offer (cancel too)."""
    from . import one_shot_action
    return one_shot_action._identity(loop, action, pr, seat, snapshot)


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
    escalation = {"by": gate.OPERATOR_ESCALATION, "at": now_iso(), "reason": why,
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
