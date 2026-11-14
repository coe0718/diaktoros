"""The host merges the base into a loop PR that fell behind, and a clean merge keeps its approval
(#1785, ``update_behind``, off unless set).

GitHub reports a PR ``behind`` when its base requires branches to be up to date and has moved on.
Such a loop PR waited for a person or a fixer turn, and its new head cost a full review round even
when the merge touched nothing the PR changed. With the setting on, the sweep and the conflict scan
hand each behind loop PR (never a review-only author's: that is ``review_only_update``) to ``act``:

* **The merge.** With no turn of the PR queued or in flight, no change request at the head (the
  fixer's to answer, or a ruling's at the cap), no hold for a person or escalation, and no
  adjudicator's ACCEPT still being acted on (its approval or dismissal not yet settled, #2364:
  a merge moves the head out from under both), the host merges the base's tip
  into the head as the fixer seat, with a lease on the head (``safe_push.update_branch``, as for a
  review-only PR). Only a clean merge that brings no workflow change is pushed; one that is not is
  remembered for the (head, base tip) pair and not tried again.
* **The approval carries over.** When the head before was ready to merge but for being behind (the
  reviewer's approval at the head, or the adjudicator's on its ACCEPT ruling, #2364, and nothing
  else ``ready`` names: CI, the shadow, a hold, a change request still to dismiss), and the merge is an exact tree identity (``safe_push.tree_identity``: the base's new
  commits touch no path the PR changed, and the merged tree is the base's with the PR's diff
  applied), the host records the new head as carried from the old one, before it pushes. ``gate``
  then counts that approval at the new head (``carried``), so the reviewer gate queues no review
  there, and ``ready``, the merge check, ``explain`` and the shadow all read the same record. CI
  must still pass at the new head. Anything short of that is reviewed as usual.

The carried approval is read live, from the PR's reviews: once the approval at the old head is
dismissed, or a later verdict lands there, nothing is carried. A carried ruled approval also needs
its ACCEPT to stay the PR's newest ruling at the old head (``gate.ruled_approvals``), so a later
ruling voids it at the new head too, and the watchdog raises no second breach for a spent cap.
"""
from __future__ import annotations

import contextvars
import time

from . import broker, ci, config, gate, gh, observer, safe_push, transition
from . import state as state_mod
from .util import log

FILE = "carried-approvals.json"
TRIED = "behind-updates.json"
KEEP = 200
# A head carried from a head that was itself carried: followed this deep at most.
CHAIN_MAX = 8
BEHIND = "not mergeable: GitHub reports behind"      # ``ready``'s reason for a behind head
IN_FLIGHT = ("pending", "waiting", "claimed", "launching", "running", "uncertain")
_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("base_merge_depth", default=0)


def _records(loop: dict) -> dict:
    try:
        st = state_mod.LoopState(loop)
        data = st.kv_get(st.dir / FILE, {})
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def carried(loop: dict, reviews: list, head: str) -> list:
    """The approval the host's base merge carried to ``head``, as a copy at ``head`` marked
    ``carried_from``; ``[]`` when there is none. ``gate`` counts it with the head's own reviews.
    The approval must still be the old head's latest effective verdict, the recorded review (the
    reviewer's, or the adjudicator's on its ACCEPT), on the recorded PR: the carried one is never
    more than the old head's own."""
    depth = _DEPTH.get()
    record = _records(loop).get(head) if head and depth < CHAIN_MAX else None
    if not isinstance(record, dict) or not isinstance(reviews, list):
        return []
    old = str(record.get("from") or "")
    token = _DEPTH.set(depth + 1)
    try:
        latest = gate.latest_effective_review_at_head(reviews, loop, old) if old else None
    finally:
        _DEPTH.reset(token)
    if (latest is None or gh.review_state(latest) != "APPROVED"
            or latest.get("id") != record.get("review")
            or gate._review_pr(latest, loop["repo"]) != record.get("number")):
        return []
    return [{**latest, "commit_id": head, "carried_from": old}]


def said(loop: dict, review) -> str:
    """``explain``'s words for a carried approval, or ''."""
    old = review.get("carried_from") if isinstance(review, dict) else None
    if not old:
        return ""
    return (f" (carried from {str(old)[:7]}: the host merged {loop['base']} in, touching nothing "
            "the PR changed)")


def act(loop: dict, st, conflicts: list, conflict_live: dict) -> list:
    """Bring each behind loop PR in ``conflicts`` up to date (see the module) and return the rest,
    the real conflicts and the review-only PRs, for ``watch_conflicts.act_on_conflicts``."""
    rest = []
    for item in conflicts:
        number, head, _base_sha, author = item
        live = conflict_live.get(number) or {}
        if live.get("mergeable_state") != "behind" or author in config.review_only(loop):
            rest.append(item)
            continue
        try:
            update(loop, st, number, head, live)
        except Exception as exc:
            log(f"#{number} @ {head[:7]} not brought up to date: {type(exc).__name__}: {exc}")
    return rest


def _waits(loop: dict, st, number: int, head: str, live: dict, reviews) -> str:
    """Why the host does not merge the base into this behind head now, or ''. ``reviews`` are
    the head's effective reviews (None: unreadable)."""
    pr_head = live.get("head") or {}
    if not config.unattended_fixer_push_enabled(loop):
        return "unattended fixer pushes are off"
    if (pr_head.get("repo") or {}).get("full_name") != loop["repo"] or not pr_head.get("ref"):
        return "a fork branch, which the host cannot push"
    busy = in_flight(loop, number)
    if busy:
        return busy
    if isinstance(st.human_hold(number, head), dict):
        return "the head waits for a person"
    if gate.escalated(st.breach_get(number), head):
        return "the head is escalated"
    if not isinstance(reviews, list):
        return "its reviews could not be read"
    latest = gate.latest_effective_review_at_head(reviews, loop, head)
    if latest is not None and gh.review_state(latest) == "CHANGES_REQUESTED":
        # The fixer answers it, or a ruling does at the cap (#1907): a merge would move the head
        # under either, and the merged head would be reviewed with the findings still open.
        return "changes are requested at the head"
    # #2364: the sweep approves and dismisses at this head; a merge before both settle strands
    # the overruled change request at a head no ruling names any more. Read whichever verdict is
    # now the latest: a write still on the wire holds the head too. At a head the approval was
    # carried to, no change request is left to dismiss: nothing waits.
    from . import ruled_approval
    return ruled_approval.unsettled(loop, number, head, reviews,
                                    ruled=gate.is_ruled_approval(latest, loop))


def in_flight(loop: dict, number: int) -> str:
    """'' when no turn of this PR is queued or in flight, else what is (an unreadable ledger
    is not idle). The review-only update waits on it too (#2468)."""
    from .run_supervisor import production_ledger, views
    path = production_ledger()
    if not path.exists():
        return ""
    con = views.read_only_connect(path)
    if con is None:
        return "the run ledger is unreadable"
    try:
        marks = ",".join("?" * len(IN_FLIGHT))
        row = con.execute(f"SELECT seat, state FROM runs WHERE repo=? AND pr=? AND state IN ({marks}) "
                          "LIMIT 1", (loop["repo"], number, *IN_FLIGHT)).fetchone()
    finally:
        con.close()
    return f"a {row[0]} turn is {row[1]}" if row else ""


class TurnHold:
    """A merge push's hold on its PR (``run_supervisor.merge_hold``, #2468). ``take`` is the
    push's ``before_push``: it holds the PR in the one transaction that finds no turn of it
    queued or in flight, so none is claimed until the push is done; or it stops the push
    (``BrokerDenied``: the next sweep tries again). ``release`` after the push, whatever came
    of it; a hold never released expires on its own."""

    def __init__(self, loop: dict, number: int, head: str):
        self.loop, self.number, self.head, self.token = loop, number, head, ""

    def take(self, *_push) -> None:
        from .run_supervisor import Supervisor, merge_hold, production_ledger
        path = production_ledger()
        # The host's own open, as a gate enqueue or the watchdog's sweep makes it: it creates a
        # missing ledger (one may be created and claimed before the push) or brings one from
        # before an upgrade up to date (``merge_holds``), so the hold serializes with any claim.
        # A worker never does this; every worker is spawned by a host that already has.
        try:
            Supervisor(path)
        except Exception as exc:
            raise broker.BrokerDenied(f"the run ledger could not be opened ({type(exc).__name__}): "
                                      "the merge waits for it") from exc
        self.token, busy = merge_hold.take(path, self.loop["repo"], self.number, self.head,
                                           IN_FLIGHT)
        if busy:
            raise broker.BrokerDenied(f"{busy}: the merge waits for it")

    def release(self) -> None:
        if not self.token:
            return
        from .run_supervisor import merge_hold, production_ledger
        try:
            merge_hold.release(production_ledger(), self.loop["repo"], self.number, self.token)
        except Exception as exc:
            log(f"#{self.number}: merge hold not released ({type(exc).__name__}); it expires "
                f"in {merge_hold.HOLD_S // 60} minutes")
        self.token = ""


def source(loop: dict, st, head: str, live: dict, raw, reviews):
    """``(review, '')``: the approval at ``head`` that may carry over (the reviewer's, or the
    adjudicator's on its ACCEPT), the head being ready to merge but for being behind; else
    ``(None, why not)``. ``raw`` is the PR's review list and ``reviews`` the effective ones."""
    latest = gate.latest_effective_review_at_head(reviews, loop, head) if isinstance(reviews, list) else None
    if latest is None or gh.review_state(latest) != "APPROVED":
        return None, f"{head[:7]} was not approved by the reviewer or on an ACCEPT ruling"
    from . import ready
    left = [text for _kind, text in ready.assess_kinds(loop, st, live, raw, ci.read(loop, head), live)
            if text != BEHIND]
    if left:
        return None, f"{head[:7]} was not ready to merge but for being behind: {left[0]}"
    return latest, ""


def update(loop: dict, st, number: int, head: str, live: dict) -> str:
    """Merge the base into one behind loop PR, carrying its approval when it may; the new head,
    or '' with the reason logged."""
    from . import watch_conflicts
    tip = watch_conflicts.base_tip(loop)
    key = f"{number}:{head}:{tip}"
    tried = st.kv_get(st.dir / TRIED, {})
    tried = tried if isinstance(tried, dict) else {}
    raw = gh.reviews(loop, number) if tip and key not in tried else None
    reviews = transition.effective_reviews(loop, st, number, head, raw) if isinstance(raw, list) else None
    why = (f"{loop['base']}'s tip is unreadable" if not tip else
           f"already tried at {tip[:7]}: {tried[key]}" if key in tried else
           _waits(loop, st, number, head, live, reviews))
    if why:
        log(f"#{number} @ {head[:7]} behind {loop['base']}, not merged in: {why}")
        return ""
    approval, no_carry = source(loop, st, head, live, raw, reviews)
    carry: dict = {}
    hold = TurnHold(loop, number, head)

    def record(new_head: str, base: str, identity: str) -> None:
        hold.take()
        if approval is not None and not identity:
            _record(st, new_head, {"number": number, "from": head, "review": approval.get("id"),
                                   "base": base, "at": time.time()})
            carry["done"] = True
        else:
            carry["why"] = no_carry or identity

    try:
        done = safe_push.update_branch(loop, repo=loop["repo"], number=number, head=head,
                                       branch=str((live.get("head") or {}).get("ref") or ""),
                                       behind=True, before_push=record)
    except safe_push.NotClean as exc:
        _remember(st, tried, key, str(exc))
        log(f"#{number} @ {head[:7]} behind {loop['base']}, not merged in: {exc}")
        return ""
    except broker.BrokerDenied as exc:       # a PushFailure too: the next sweep tries again
        log(f"#{number} @ {head[:7]} behind {loop['base']}, merge push stopped: {exc}")
        return ""
    finally:
        hold.release()
    new = done["new_head"]
    log(f"#{number} @ {head[:7]} merged {loop['base']} in: {new[:7]}"
        + (" (approval carried over)" if carry.get("done") else ""))
    _notify(loop, st, number, head, new, carry)
    return new


def _notify(loop: dict, st, number: int, head: str, new: str, carry: dict) -> None:
    base = loop["base"]
    if carry.get("done"):
        outcome = (f"merged {base} into #{number} at {new[:7]}; the approval at {head[:7]} carries "
                   f"over, since the merge touched nothing the PR changed")
        next_turn = "none: the merge check turns green once CI passes at the new head"
    else:
        outcome = (f"merged {base} into #{number} at {new[:7]}; review resumes"
                   + (f" (no approval carried: {carry['why']})" if carry.get("why") else ""))
        next_turn = "reviewer: the new head is reviewed as usual"
    observer.notify(loop, st, "updated", number, new, identity="updated", outcome=outcome,
                    next_turn=next_turn)


def _record(st, new_head: str, entry: dict) -> None:
    """Record a carried approval before the push (#1785): written strictly, so no merge is pushed
    without it. The newest ``KEEP`` are kept."""
    path = st.dir / FILE
    data = st.kv_get(path, {})
    data = data if isinstance(data, dict) else {}
    data[new_head] = entry
    newest = sorted(data.items(), key=lambda item: (item[1] or {}).get("at") or 0)[-KEEP:]
    st.kv_set_strict(path, dict(newest))


def _remember(st, tried: dict, key: str, why: str) -> None:
    """A merge that is not clean, kept for its (head, base tip) pair: not tried again."""
    tried[key] = why[:200]
    st.kv_set(st.dir / TRIED, dict(list(tried.items())[-KEEP:]))
