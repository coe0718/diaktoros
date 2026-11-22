"""A finished CI run, acted on when GitHub reports it (#681), not at the next watchdog sweep.

GitHub sends ``check_suite`` ``completed`` when every check one app runs on a commit is done. With
``ci_webhook`` on, the fixer's route and hook carry it (``config.hook_events``), or the App's
webhook does, and the fixer gate hands it here. The delivery is a nudge, never the truth (#679):
only the commit it names is taken from it. GitHub is re-read, and the watchdog's own passes run
for that commit alone, under the sweep's per-loop lock, so the two never act on one commit at once:

* the base branch's current head → ``main_check.sweep``: the ``main_red`` notice;
* an open loop PR whose current head is that commit → ``ci_fix.sweep`` for that PR: the
  ``ci_failed`` notice, and the CI-fix turn within ``ci_fix_cap``. One turn per head, however many
  suites finish for it: ``ci_fix`` keys the turn by head, and the ledger by its turn key. A review
  held for CI at that head is made due now, and the worker woken: its claim reads CI again.
  Green after a CI fix with no review turn at all at that head, the review request is delivered
  to the reviewer gate (``request_review``).

A suite on any other commit (an older head, a branch with no loop PR) does nothing. Each suite is
noted (``NUDGES``) before the lock is taken: a sweep that holds it acts on every note as its last
step (``drain``, from ``watchdog.sweep_loop``), so a suite that finished after that sweep read CI
still gets a read. Whoever lets the lock go then looks for notes once more and, if any, takes it
back without waiting (``handoff``, #1996), so a note whose handler stopped waiting just before
the release is not left for the next sweep. Both are bounded (``DRAIN_PASSES``, ``HANDOFFS``):
a stream of suites cannot hold the lock, and notes past the bound stay noted for the next sweep
or delivery. No delivery starts a turn the sweep would not have queued, and the sweep stays the
backstop for one that never arrives.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import time

from . import ci, ci_fix, config, gh, hostdirs, main_check, review_kick
from .ci_nudges import NUDGES, NUDGES_MAX, _SHA, _take, note, taken  # noqa: F401
from .util import log, register_lock, silence

# The gateway gives a gate 30 seconds (``gate_failures``): wait this long for a running sweep's
# lock, then leave the commit to that sweep, which reads CI itself.
LOCK_WAIT_S = 20.0
DRAIN_PASSES = 5              # a drain's takes of the notes; any left stay noted
HANDOFFS = 3                  # times a holder that let the lock go takes it back for notes left
# The watchdog's per-loop sweep lock (``watchdog.sweep_loop``), in the loop's state directory.
SWEEP_LOCK = register_lock("sweep.lock")


def gate(loop: dict, st, payload: dict) -> None:
    """The fixer gate's path for a ``check_suite`` delivery. Always ends in a silence."""
    if not config.ci_webhook(loop):
        silence(f"check_suite: ci_webhook is off for {loop['id']}; the watchdog sweep reads CI")
    if payload.get("action") != "completed":
        silence(f"check_suite {payload.get('action')!r}: only a completed suite is acted on")
    suite = payload.get("check_suite")
    head = suite.get("head_sha") if isinstance(suite, dict) else None
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        silence("check_suite: payload has no head sha")
    numbers = [pr.get("number") for pr in suite.get("pull_requests") or []
               if isinstance(pr, dict) and type(pr.get("number")) is int and pr["number"] > 0]
    token = note(st, head, numbers)
    by_head: dict[str, list[str]] = {}
    with sweep_lock(st, LOCK_WAIT_S) as held:
        if held:
            drain(loop, st, by_head=by_head)
            if head not in by_head and token not in taken(st):
                # Its note left the queue untaken (``NUDGES_MAX`` dropped it): read it here, as
                # the drain would have (F4). Past ``TAKEN_MAX`` this can repeat a holder's read
                # of the commit, never a turn (``ci_fix`` keys it by head).
                by_head[head] = _act(loop, st, head, numbers, log)
    if not held:
        silence(f"check_suite @ {head[:7]}: noted for the running watchdog sweep, which takes "
                "it when it lets the lock go (or the next sweep does)", "accepted")
    handoff(loop, st, by_head=by_head)
    # This delivery's record is its own commit's alone (review on #2577, F3): commits noted by
    # other deliveries, taken here, are logged, never made this one's verdict.
    for other, acted in by_head.items():
        if other != head and acted:
            log(f"check_suite @ {other[:7]}, noted by another delivery: " + "; ".join(acted))
    if head not in by_head:
        silence(f"check_suite @ {head[:7]}: noted; another holder of the sweep lock took it "
                "and acts on it", "accepted")
    if not by_head[head]:
        silence(f"check_suite @ {head[:7]}: not the base's head nor an open loop PR's head")
    silence(f"check_suite @ {head[:7]}: " + "; ".join(dict.fromkeys(by_head[head])), "accepted")


def drain(loop: dict, st, log=log, by_head: dict | None = None) -> list[str]:
    """Act on every noted suite, once per commit per take, under the sweep lock its caller
    holds: the handler that took it, or the watchdog's sweep as its last step. Notes written
    while it acts are taken next (review on #1992, F4), up to ``DRAIN_PASSES`` takes; any left
    are ``handoff``'s once the lock is let go. What was acted on; ``by_head`` (if given) gets
    each commit taken, with what was acted on for it, so a handler records its own alone."""
    acted, by_head = [], {} if by_head is None else by_head
    for _ in range(DRAIN_PASSES):
        heads = _take(st)
        if not heads:
            break
        for head, numbers in heads.items():
            done = _act(loop, st, head, numbers, log)
            acted += done
            by_head.setdefault(head, []).extend(done)
    return acted


def _act(loop: dict, st, head: str, numbers: list[int], log=log) -> list[str]:
    """``act``, with one unreadable commit logged (nothing acted on), not dropping the rest."""
    try:
        return act(loop, st, head, numbers)
    except Exception as exc:
        log(f"check_suite @ {head[:7]} not acted on: {type(exc).__name__}: {exc}")
        return []


def handoff(loop: dict, st, log=log, by_head: dict | None = None) -> list[str]:
    """Run by whoever just let the sweep lock go (#1996): while notes are left, take the lock back
    without waiting and ``drain``. A note written before the release (its handler may have
    stopped waiting, trusting the holder) is seen here; one written after it finds the lock free
    for its own handler, or held by a holder that runs this in turn. ``HANDOFFS`` bounds it: what
    is still noted then waits for the next sweep or delivery. What was acted on (``by_head``
    as ``drain``'s)."""
    acted = []
    for _ in range(HANDOFFS):
        with st.locked():
            noted = st.kv_get(st.dir / NUDGES, [])
        if not noted:
            break
        with sweep_lock(st, 0) as held:
            if not held:                      # another holder: it runs this when it lets go
                break
            acted += drain(loop, st, log, by_head)
    return acted


def act(loop: dict, st, head: str, numbers: list[int]) -> list[str]:
    """What the sweep would do for ``head`` alone; what it acted on, for the decision record."""
    acted = []
    base = gh.api(loop, f"/repos/{loop['repo']}/commits/{loop['base']}",
                  login=loop.get("read_token"))
    if isinstance(base, dict) and base.get("sha") == head:
        main_check.sweep(loop, st, log)
        acted.append(f"{loop['base']}'s head: main check run")
    prs = prs_at(loop, head, numbers)
    if prs:
        ci_fix.sweep(loop, st, prs, log)
        acted.append("CI read for " + ", ".join(f"#{pr['number']}" for pr in prs))
        if wake_held_reviews(loop, head, prs):
            acted.append("reviews held for CI woken")
        asked = [pr["number"] for pr in prs if request_review(loop, pr, head)]
        if asked:
            acted.append("review requested for " + ", ".join(f"#{n}" for n in asked))
    return acted


def wake_held_reviews(loop: dict, head: str, prs: list[dict]) -> int:
    """Make the reviews held for CI at ``head`` due now and run the worker's recovery, which
    claims them: the claim re-reads CI and holds one whose checks still run (#241)."""
    from . import gate, run_supervisor
    db = run_supervisor.production_ledger()
    if not os.path.exists(db):             # no ledger yet: no turn, so none held (#1997)
        return 0
    sup = run_supervisor.Supervisor(db, create=False)
    released = sum(sup.release_ci_holds(loop["repo"], pr["number"], head) for pr in prs)
    if released:
        gate.resume_isolated(loop)
    return released


def request_review(loop: dict, pr: dict, head: str) -> bool:
    """Green after a CI fix, with no reviewer turn at this head (review on #1992, F1): deliver
    the review request the fix's push made, which GitHub did not announce or this host did not
    receive. The watchdog's own delivery (``review_kick``, #532), on the same terms: the
    reviewer seat still requested (an App's always is), and the gate's rules and the ledger's
    dedup decide, so it starts nothing a delivered request would not."""
    from . import run_supervisor
    number = pr["number"]
    if not any(r["head"] != head and r["push_confirmed"]
               for r in ci_fix.rows(loop["repo"], number)):
        return False
    view = ci.gating(ci.read(loop, head), config.required_checks(loop))
    if view is None or not view.green:
        return False
    if not (review_kick.already_requested(loop, pr) or config.reviewer_is_app(loop)):
        return False
    sup = run_supervisor.Supervisor(run_supervisor.production_ledger(), create=False)
    if sup.has_turn(loop["repo"], number, head, "reviewer"):
        return False
    author = str((pr.get("user") or {}).get("login") or "")
    return review_kick.kick(loop, pr, head, author, f"ci-review-{number}")


def prs_at(loop: dict, head: str, numbers: list[int]) -> list[dict]:
    """The open loop PRs whose current head is ``head``, each re-read from GitHub.

    The suite's ``pull_requests`` names them when the branch is in this repository; a fork's PR
    is not listed there, so with none named the open PRs are listed instead (one bounded read).
    """
    if numbers:
        found = [gh.pr(loop, number) for number in dict.fromkeys(numbers)]
    else:
        found = gh.open_prs(loop) or []
    return [pr for pr in found
            if isinstance(pr, dict) and pr.get("state", "open") == "open"
            and (pr.get("head") or {}).get("sha") == head and config.loop_pr(loop, pr)]


@contextlib.contextmanager
def sweep_lock(st, wait_s: float, sleep=time.sleep):
    """``SWEEP_LOCK``, waited for up to ``wait_s``; yields whether it is held."""
    hostdirs.ensure(st.dir)
    fd = os.open(st.dir / SWEEP_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                sleep(0.5)
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
