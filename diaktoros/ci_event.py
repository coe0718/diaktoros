"""A finished CI run, acted on when GitHub reports it (#681), not at the next watchdog sweep.

GitHub sends ``check_suite`` ``completed`` when every check one app runs on a commit is done. With
``ci_webhook`` on, the fixer's route and hook carry it (``config.hook_events``), or the App's
webhook does, and the fixer gate hands it here. The delivery is a nudge, never the truth (#679):
only the commit it names is taken from it. GitHub is re-read, and the watchdog's own passes run
for that commit alone, under the sweep's per-loop lock, so the two never act on one commit at once:

* the base branch's current head → ``main_check.sweep``: the ``main_red`` notice;
* an open loop PR whose current head is that commit → ``ci_fix.sweep`` for that PR: the
  ``ci_failed`` notice, and the CI-fix turn within ``ci_fix_cap``. One turn per head, however many
  suites finish for it: ``ci_fix`` keys the turn by head, and the ledger by its turn key.

A suite on any other commit (an older head, a branch with no loop PR) does nothing. A review held
for CI needs nothing here: its worker re-reads CI every ``CI_POLL_S`` (#241). No delivery starts a
turn the sweep would not have queued, and the sweep stays the backstop for one that never arrives.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time

from . import ci_fix, config, gh, hostdirs, main_check
from .util import log, register_lock, silence

# The gateway gives a gate 30 seconds (``gate_failures``): wait this long for a running sweep's
# lock, then leave the commit to that sweep, which reads CI itself.
LOCK_WAIT_S = 20.0
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
    with sweep_lock(st, LOCK_WAIT_S) as held:
        if not held:
            silence(f"check_suite @ {head[:7]}: a watchdog sweep is running and reads CI itself")
        acted = act(loop, st, head, numbers)
    if not acted:
        silence(f"check_suite @ {head[:7]}: not the base's head nor an open loop PR's head")
    silence(f"check_suite @ {head[:7]}: " + "; ".join(acted), "accepted")


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
    return acted


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
