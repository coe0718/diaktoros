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
still gets a read. The drain takes notes until none is left, so one written while it acts is not
stranded when its own handler stops waiting. No delivery starts a turn the sweep would not have queued, and the sweep stays
the backstop for one that never arrives.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time

from . import ci, ci_fix, config, gh, hostdirs, main_check, review_kick
from .util import log, register_lock, silence

# The gateway gives a gate 30 seconds (``gate_failures``): wait this long for a running sweep's
# lock, then leave the commit to that sweep, which reads CI itself.
LOCK_WAIT_S = 20.0
NUDGES = "ci-nudges.json"     # suites noted for the lock's holder, newest last
NUDGES_MAX = 50
DRAIN_PASSES = 5              # a drain's takes of the notes; any left wait for the next holder
# The watchdog's per-loop sweep lock (``watchdog.sweep_loop``), in the loop's state directory.
SWEEP_LOCK = register_lock("sweep.lock")
_SHA = re.compile(r"[0-9a-f]{40}")


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
    note(st, head, numbers)
    with sweep_lock(st, LOCK_WAIT_S) as held:
        if not held:
            silence(f"check_suite @ {head[:7]}: noted for the running watchdog sweep, which acts "
                    "on it before it ends", "accepted")
        acted = drain(loop, st)
    if not acted:
        silence(f"check_suite @ {head[:7]}: not the base's head nor an open loop PR's head")
    silence(f"check_suite @ {head[:7]}: " + "; ".join(acted), "accepted")


def note(st, head: str, numbers: list[int]) -> None:
    """Note a finished suite for whoever holds the sweep lock next (``drain``)."""
    path = st.dir / NUDGES
    with st.locked():
        noted = st.kv_get(path, [])
        noted = noted if isinstance(noted, list) else []
        st.kv_set(path, (noted + [{"head": head, "numbers": numbers}])[-NUDGES_MAX:])


def drain(loop: dict, st, log=log) -> list[str]:
    """Act on every noted suite, once per commit per take, under the sweep lock its caller
    holds: the handler that took it, or the watchdog's sweep as its last step. Notes written
    while it acts are taken next (review on #1992, F4): their handler may have stopped waiting
    for the lock. What was acted on."""
    acted = []
    for _ in range(DRAIN_PASSES):
        heads = _take(st)
        if not heads:
            break
        for head, numbers in heads.items():
            try:
                acted += act(loop, st, head, numbers)
            except Exception as exc:          # one unreadable commit does not drop the rest
                log(f"check_suite @ {head[:7]} not acted on: {type(exc).__name__}: {exc}")
    return acted


def _take(st) -> dict[str, list[int]]:
    """The noted suites, cleared: the PR numbers named for each commit."""
    path = st.dir / NUDGES
    with st.locked():
        noted = st.kv_get(path, [])
        st.kv_set(path, [])
    heads: dict[str, list[int]] = {}
    for entry in noted if isinstance(noted, list) else []:
        head = entry.get("head") if isinstance(entry, dict) else None
        if isinstance(head, str) and _SHA.fullmatch(head):
            heads.setdefault(head, []).extend(n for n in entry.get("numbers") or []
                                              if type(n) is int and n > 0)
    return heads


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
    sup = run_supervisor.Supervisor(run_supervisor.production_ledger(), create=False)
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
